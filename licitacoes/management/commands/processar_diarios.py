"""
Management Command: processar_diarios

Estratégia Ultra-Econômica de RPD / RPM (Super-Batching 250K TPM + Triagem Local):
1. Fase 1 — Triagem Local 100% Gratuita (Zero IA):
   - Verifica deduplicação dupla (`SHA-256` binário + `SHA-256` do texto extraído via PyMuPDF).
   - Aplica filtro local PyMuPDF + Regex página a página.
   - PDFs duplicados ou sem nenhuma página com palavras-chave do nicho são arquivados/removidos
     imediatamente consumindo **0 requisições ao Google Gemini (0 RPM / 0 RPD)**.
2. Fase 2 — Empacotamento em Super-Batch na Janela de Contexto (250.000 TPM):
   - Agrupa as páginas promissoras de múltiplos PDFs (até `GEMINI_SUPERBATCH_MAX_FILES` arquivos
     ou `GEMINI_SUPERBATCH_MAX_CHARS` ≈ 450.000 caracteres / ~112.000 tokens) em **uma única chamada**
     estruturada ao Google Gemini, reduzindo o consumo de RPD e RPM em até 80–90%.
   - Aplica espaçamento mínimo obrigatório (`GEMINI_MIN_INTERVAL_BETWEEN_CALLS_SEC`, padrão 20s)
     entre chamadas consecutivas e balanceia a carga entre `gemini-3.5-flash-lite` (500 RPD) e
     `gemini-3.1-flash-lite` (500 RPD), seguidos pelos modelos de reserva.
"""

from __future__ import annotations

import datetime
import logging
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

import boto3
import pymupdf as fitz  # PyMuPDF
from botocore.client import BaseClient
from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser
from django.db import transaction
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from licitacoes.deduplicacao import (
    calcular_sha256_bytes,
    calcular_sha256_texto_pdf,
    registrar_diario_processado,
    sincronizar_hashes_locais_existentes,
)
from licitacoes.models import DiarioProcessado, Licitacao
from licitacoes.pipeline_progress import (
    adicionar_log_progresso,
    aguardar_slot_rpm_se_necessario,
    atualizar_etapa_arquivo,
    concluir_um_arquivo,
    definir_total_arquivos,
    finalizar_progresso,
    iniciar_progresso,
    ordenar_modelos_para_chamada,
    registrar_cooldown_modelo,
    registrar_erro_arquivo,
    registrar_uso_modelo,
)
from licitacoes.storage_upload import s3_credenciais_validas

logger = logging.getLogger(__name__)


# ==============================================================================
# 1. Regras de Filtragem (Regex do Nicho de Negócio)
# ==============================================================================
KEYWORDS_PATTERNS = [
    r"\btecnologia\s+da\s+informacao\b",
    r"\bmanutencao\s+de\s+computadores\b",
    r"\binfraestrutura\s+de\s+ti\b",
    r"\bformacao\s+(?:em\s+)?computacao\b",
    r"\blivros?\s+didaticos?\b",
    r"\btecnologias?\b",
    r"\bcomputacao\b",
    r"\bcomputador(?:es)?\b",
    r"\blaboratorios?\b",
    r"\binformatica\b",
    r"\bsoftware(?:s)?\b",
    r"\bhardware(?:s)?\b",
    r"\bti\b",
]

COMPILED_NICHE_REGEX = re.compile(
    "|".join(KEYWORDS_PATTERNS),
    flags=re.IGNORECASE,
)


def normalizar_texto_para_regex(texto: str) -> str:
    """
    Remove acentos (ex: 'Computação' -> 'Computacao', 'Informática' -> 'Informatica')
    para garantir que o Regex capture variações com ou sem acentuação nos PDFs.
    """
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# ==============================================================================
# 2. Schema Pydantic para Extração Estruturada no Google Gemini (Suporte a Super-Batch)
# ==============================================================================
class LicitacaoExtraidaSchema(BaseModel):
    documento_id: str = Field(
        default="DOC_1",
        description=(
            "Identificador exato do documento de onde esta licitação foi extraída "
            "(ex: 'DOC_1', 'DOC_2'), conforme o cabeçalho 'INÍCIO DO DOCUMENTO [DOC_X]'."
        ),
    )
    cidade: str = Field(
        description="Nome da cidade/município do órgão licitante (ex: 'São Paulo - SP' ou 'Curitiba')."
    )
    orgao_emissor: str = Field(
        description="Nome do órgão público emissor (ex: 'Secretaria Municipal de Educação', 'Prefeitura Municipal')."
    )
    data_publicacao: str = Field(
        description="Data de publicação do Diário Oficial no formato ISO YYYY-MM-DD."
    )
    modalidade: str = Field(
        description="Modalidade da licitação (ex: 'Pregão Eletrônico', 'Tomada de Preços', 'Dispensa de Licitação', 'Concorrência')."
    )
    objeto_licitacao: str = Field(
        description="Descrição clara e completa do objeto da licitação relacionada a TI, computação, laboratórios ou livros didáticos."
    )
    paginas_referencia: str = Field(
        description="Números das páginas onde esta licitação específica aparece, separados por vírgula (ex: '14, 15')."
    )


class ResultadoExtracaoDiarioSchema(BaseModel):
    licitacoes: List[LicitacaoExtraidaSchema] = Field(
        default_factory=list,
        description="Lista de licitações do nicho de Tecnologia/Computação/Educação encontradas nos documentos enviados.",
    )


@dataclass
class CandidatoGemini:
    """Representa um PDF já filtrado localmente pelo PyMuPDF + Regex e pronto para entrar num Super-Batch."""

    indice_arquivo: int
    doc_id: str
    object_key: str
    nome_arquivo: str
    tamanho_bytes: int
    sha256_arquivo: str
    sha256_texto: str
    total_paginas: int
    paginas_promissoras: List[tuple[int, str, List[str]]] = field(default_factory=list)
    bloco_texto: str = ""


# ==============================================================================
# 3. Management Command do Django
# ==============================================================================
class Command(BaseCommand):
    help = (
        "Varre a pasta 'novos_diarios/' (em pasta local no modo teste ou Supabase Storage no modo deploy), "
        "filtra páginas com PyMuPDF + Regex, agrupa múltiplos PDFs em Super-Batches aproveitando a janela "
        "de 250K TPM do Google Gemini para minimizar RPD/RPM, salva no banco e move para 'diarios_processados/'."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Executa a leitura e filtragem Regex sem gravar no banco e sem mover os arquivos.",
        )
        parser.add_argument(
            "--max-files",
            type=int,
            default=0,
            help=(
                "Limita a quantidade máxima de arquivos PDF enviados à IA nesta execução "
                "(0 = todos; o agendador de 10 min usa PIPELINE_MAX_GEMINI_FILES_PER_CRON)."
            ),
        )
        parser.add_argument(
            "--origem",
            type=str,
            default="Manual / Web",
            help="Identifica quem disparou a execução (ex: 'Manual / Web' ou 'Agendador Automático (10 min)').",
        )

    def _log(self, msg: str, style_func=None, is_erro: bool = False) -> None:
        adicionar_log_progresso(msg, is_erro=is_erro)
        out_stream = self.stderr if is_erro else self.stdout
        if style_func is not None:
            out_stream.write(style_func(msg))
        else:
            out_stream.write(msg)

    def handle(self, *args, **options) -> None:
        dry_run: bool = options["dry_run"]
        max_files: int = options["max_files"]
        origem: str = options.get("origem") or "Manual / Web"
        use_local: bool = settings.USE_LOCAL_STORAGE

        iniciar_progresso(total_arquivos=0, origem=origem)

        bucket_name: str = settings.SUPABASE_STORAGE_BUCKET
        input_prefix: str = settings.SUPABASE_INPUT_PREFIX
        processed_prefix: str = settings.SUPABASE_PROCESSED_PREFIX

        modo_str = (
            f"TESTE LOCAL (Pasta '{settings.LOCAL_STORAGE_ROOT}')"
            if use_local
            else f"DEPLOY PRODUÇÃO (PostgreSQL Supabase + Bucket '{bucket_name}')"
        )
        self._log(
            f"Iniciando processamento ({origem}) | Modo: {modo_str}",
            style_func=self.style.NOTICE,
        )

        try:
            s3_client: Optional[BaseClient] = None
            if use_local:
                pdf_keys = self._listar_pdfs_locais(input_prefix, processed_prefix)
            else:
                if not s3_credenciais_validas():
                    raise RuntimeError(
                        "As credenciais S3 (SUPABASE_S3_ACCESS_KEY_ID / SUPABASE_S3_SECRET_ACCESS_KEY) "
                        "ainda estão com os valores de exemplo ('sua-access-key-s3'). "
                        "Configure as chaves reais do Supabase S3 nas variáveis de ambiente do Coolify."
                    )
                s3_client = self._get_s3_client()
                pdf_keys = self._listar_pdfs_s3(s3_client, bucket_name, input_prefix)

            if not pdf_keys:
                self._log(
                    "Nenhum arquivo PDF pendente em 'novos_diarios/'.",
                    style_func=self.style.SUCCESS,
                )
                finalizar_progresso("Concluído (nenhum arquivo pendente na fila).")
                return

            # Seleciona os arquivos para este ciclo
            total_fila = len(pdf_keys)
            definir_total_arquivos(
                min(total_fila, max_files) if max_files > 0 else total_fila
            )
            self._log(
                f"Fila 'novos_diarios/': {total_fila} PDF(s) aguardando análise."
            )

            sincronizar_hashes_locais_existentes()

            # ==================================================================
            # FASE 1: Triagem Local Gratuita (SHA-256 + PyMuPDF + Regex)
            # ==================================================================
            candidatos_gemini: List[CandidatoGemini] = []
            arquivos_avaliados = 0

            for object_key in pdf_keys:
                if max_files > 0 and len(candidatos_gemini) >= max_files:
                    restantes = total_fila - arquivos_avaliados
                    self._log(
                        f"\n[Modo Econômico] Limite de {max_files} PDF(s) para IA por ciclo atingido. "
                        f"Os outros {restantes} PDF(s) permanecem na fila para o próximo ciclo de 10 min.",
                        style_func=self.style.NOTICE,
                    )
                    break

                arquivos_avaliados += 1
                nome_curto = Path(object_key).name
                atualizar_etapa_arquivo(
                    arquivos_avaliados,
                    nome_curto,
                    "Fase 1 (Local): Baixando e filtrando com PyMuPDF + Regex...",
                )
                try:
                    cand = self._triar_arquivo_localmente(
                        indice_arquivo=arquivos_avaliados,
                        doc_seq=len(candidatos_gemini) + 1,
                        use_local=use_local,
                        s3_client=s3_client,
                        bucket_name=bucket_name,
                        object_key=object_key,
                        processed_prefix=processed_prefix,
                        dry_run=dry_run,
                    )
                    if cand is not None:
                        candidatos_gemini.append(cand)
                except Exception as exc:
                    logger.exception(
                        "Erro na triagem local do arquivo %s: %s", object_key, exc
                    )
                    self._log(
                        f"[ERRO] Falha ao ler/filtrar '{object_key}': {exc}",
                        style_func=self.style.ERROR,
                        is_erro=True,
                    )
                    registrar_erro_arquivo(nome_curto, object_key, str(exc))
                    concluir_um_arquivo(nome_arquivo=nome_curto, teve_erro=True)

            # Ajusta o total efetivo da barra de progresso para os arquivos avaliados neste ciclo
            definir_total_arquivos(max(arquivos_avaliados, 1))

            if not candidatos_gemini or dry_run:
                finalizar_progresso(
                    "Concluído sem necessidade de chamadas à IA (0 requisições Gemini utilizadas)!"
                )
                return

            # ==================================================================
            # FASE 2: Empacotamento em Super-Batches (Janela 250K TPM) + Gemini
            # ==================================================================
            superbatches = self._agrupar_em_superbatches(candidatos_gemini)
            economia = len(candidatos_gemini) - len(superbatches)
            self._log(
                f"\n[Estratégia Super-Batch 250K TPM] {len(candidatos_gemini)} PDF(s) com indícios "
                f"foram agrupados em apenas {len(superbatches)} chamada(s) ao Google Gemini "
                f"(economia direta de {economia} requisição(ões) RPD/RPM)!",
                style_func=self.style.SUCCESS,
            )

            for idx_lote, lote in enumerate(superbatches, start=1):
                nomes_lote = ", ".join(c.nome_arquivo for c in lote)
                chars_lote = sum(len(c.bloco_texto) for c in lote)
                tokens_estimados = max(1, chars_lote // 4)
                self._log(
                    f"\n-> [Super-Batch {idx_lote}/{len(superbatches)}] Enviando {len(lote)} PDF(s) "
                    f"em uma única requisição (~{tokens_estimados:,} tokens / 250.000 TPM): [{nomes_lote}]"
                )
                try:
                    mapa_licitacoes = self._extrair_superbatch_com_gemini(
                        idx_lote=idx_lote,
                        total_lotes=len(superbatches),
                        lote=lote,
                    )
                    for cand in lote:
                        lics_do_pdf = mapa_licitacoes.get(cand.doc_id, [])
                        self._finalizar_e_arquivar_candidato(
                            cand=cand,
                            licitacoes_extraidas=lics_do_pdf,
                            use_local=use_local,
                            s3_client=s3_client,
                            bucket_name=bucket_name,
                            processed_prefix=processed_prefix,
                        )
                except Exception as exc:
                    logger.exception(
                        "Erro ao processar Super-Batch %s (%s): %s",
                        idx_lote,
                        nomes_lote,
                        exc,
                    )
                    self._log(
                        f"[ERRO] Falha no Super-Batch [{nomes_lote}]: {exc}",
                        style_func=self.style.ERROR,
                        is_erro=True,
                    )
                    for cand in lote:
                        registrar_erro_arquivo(
                            cand.nome_arquivo, cand.object_key, str(exc)
                        )
                        concluir_um_arquivo(
                            nome_arquivo=cand.nome_arquivo, teve_erro=True
                        )
                    if "API_KEY_INVALID" in str(exc) or "COLE_AQUI_SUA_CHAVE" in str(exc):
                        self._log(
                            "[PARADA DE SEGURANÇA] Processamento interrompido pois a GEMINI_API_KEY é inválida no servidor.",
                            style_func=self.style.ERROR,
                            is_erro=True,
                        )
                        break

            finalizar_progresso("Processamento concluído!")
        except Exception as exc:
            self._log(f"[ERRO FATAL] {exc}", style_func=self.style.ERROR, is_erro=True)
            finalizar_progresso(f"Interrompido com erro: {exc}")
            raise

    # --------------------------------------------------------------------------
    # Métodos Auxiliares: Armazenamento Local em Pasta (Modo Teste)
    # --------------------------------------------------------------------------
    def _listar_pdfs_locais(
        self, input_prefix: str, processed_prefix: str
    ) -> List[str]:
        root_dir: Path = Path(settings.LOCAL_STORAGE_ROOT)
        input_dir = root_dir / input_prefix.strip("/")
        processed_dir = root_dir / processed_prefix.strip("/")

        input_dir.mkdir(parents=True, exist_ok=True)
        processed_dir.mkdir(parents=True, exist_ok=True)

        arquivos = sorted(
            str(p)
            for p in input_dir.iterdir()
            if p.is_file() and p.suffix.lower() == ".pdf"
        )
        return arquivos

    def _gerar_nome_unico_local(self, dest_dir: Path, filename: str) -> str:
        candidato = dest_dir / filename
        if not candidato.exists():
            return filename

        stem = Path(filename).stem
        suffix = Path(filename).suffix or ".pdf"
        contador = 2
        while True:
            novo_nome = f"{stem}_{contador}{suffix}"
            if not (dest_dir / novo_nome).exists():
                return novo_nome
            contador += 1

    def _mover_pdf_local_para_processados(
        self,
        source_path_str: str,
        processed_prefix: str,
        data_ref: datetime.date,
    ) -> str:
        source_path = Path(source_path_str)
        prefix_limpo = processed_prefix.strip("/")
        ano = f"{data_ref.year:04d}"
        mes = f"{data_ref.month:02d}"

        dest_dir = Path(settings.LOCAL_STORAGE_ROOT) / prefix_limpo / ano / mes
        dest_dir.mkdir(parents=True, exist_ok=True)

        filename = self._gerar_nome_unico_local(dest_dir, source_path.name)
        dest_path = dest_dir / filename
        shutil.move(str(source_path), str(dest_path))

        relative_url_path = f"{prefix_limpo}/{ano}/{mes}/{filename}"
        encoded_path = quote(relative_url_path, safe="/")
        base_media = (
            "/media"
            if settings.IS_DEPLOY
            else settings.LOCAL_MEDIA_BASE_URL.rstrip("/")
        )
        return f"{base_media}/{encoded_path}"

    # --------------------------------------------------------------------------
    # Métodos Auxiliares: Supabase Storage via S3 / boto3 (Modo Deploy)
    # --------------------------------------------------------------------------
    def _get_s3_client(self) -> BaseClient:
        return boto3.client(
            "s3",
            endpoint_url=settings.SUPABASE_S3_ENDPOINT_URL,
            aws_access_key_id=settings.SUPABASE_S3_ACCESS_KEY_ID,
            aws_secret_access_key=settings.SUPABASE_S3_SECRET_ACCESS_KEY,
            region_name=settings.SUPABASE_S3_REGION_NAME,
        )

    def _listar_pdfs_s3(
        self, s3_client: BaseClient, bucket_name: str, input_prefix: str
    ) -> List[str]:
        paginator = s3_client.get_paginator("list_objects_v2")
        pdf_keys: List[str] = []

        for page in paginator.paginate(Bucket=bucket_name, Prefix=input_prefix):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                if key.lower().endswith(".pdf") and key != input_prefix:
                    pdf_keys.append(key)

        return sorted(pdf_keys)

    def _s3_objeto_existe(
        self, s3_client: BaseClient, bucket_name: str, key: str
    ) -> bool:
        try:
            s3_client.head_object(Bucket=bucket_name, Key=key)
            return True
        except Exception:
            return False

    def _gerar_chave_unica_s3(
        self,
        s3_client: BaseClient,
        bucket_name: str,
        base_prefix: str,
        filename: str,
    ) -> str:
        candidato_key = f"{base_prefix}/{filename}"
        if not self._s3_objeto_existe(s3_client, bucket_name, candidato_key):
            return candidato_key

        stem = Path(filename).stem
        suffix = Path(filename).suffix or ".pdf"
        contador = 2
        while True:
            novo_nome = f"{stem}_{contador}{suffix}"
            nova_key = f"{base_prefix}/{novo_nome}"
            if not self._s3_objeto_existe(s3_client, bucket_name, nova_key):
                return nova_key
            contador += 1

    def _mover_pdf_s3_para_processados(
        self,
        s3_client: BaseClient,
        bucket_name: str,
        source_key: str,
        processed_prefix: str,
        data_ref: datetime.date,
    ) -> str:
        filename = source_key.split("/")[-1]
        prefix_limpo = processed_prefix.strip("/")
        ano = f"{data_ref.year:04d}"
        mes = f"{data_ref.month:02d}"
        base_dest_prefix = f"{prefix_limpo}/{ano}/{mes}"
        dest_key = self._gerar_chave_unica_s3(
            s3_client=s3_client,
            bucket_name=bucket_name,
            base_prefix=base_dest_prefix,
            filename=filename,
        )

        s3_client.copy_object(
            Bucket=bucket_name,
            CopySource={"Bucket": bucket_name, "Key": source_key},
            Key=dest_key,
            ContentType="application/pdf",
        )
        s3_client.delete_object(Bucket=bucket_name, Key=source_key)

        base_public_url = settings.SUPABASE_PUBLIC_STORAGE_URL.rstrip("/")
        encoded_dest_key = quote(dest_key, safe="/")
        return f"{base_public_url}/{bucket_name}/{encoded_dest_key}"

    # --------------------------------------------------------------------------
    # Métodos Auxiliares: PyMuPDF + Regex + Super-Batching Gemini
    # --------------------------------------------------------------------------
    def _extrair_paginas_promissoras(
        self, pdf_bytes: bytes
    ) -> List[tuple[int, str, List[str]]]:
        paginas_filtradas: List[tuple[int, str, List[str]]] = []

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page_index in range(len(doc)):
                page = doc.load_page(page_index)
                texto_original = page.get_text("text") or ""
                if not texto_original.strip():
                    continue

                texto_normalizado = normalizar_texto_para_regex(texto_original)
                matches = COMPILED_NICHE_REGEX.findall(texto_normalizado)
                if matches:
                    termos_unicos = sorted({m.lower() for m in matches})
                    numero_pagina = page_index + 1
                    paginas_filtradas.append(
                        (numero_pagina, texto_original, termos_unicos)
                    )

        return paginas_filtradas

    def _formatar_bloco_documento(
        self,
        doc_id: str,
        nome_arquivo: str,
        paginas_promissoras: List[tuple[int, str, List[str]]],
    ) -> str:
        linhas = [
            "======================================================================",
            f"INÍCIO DO DOCUMENTO [{doc_id}] | ARQUIVO: {nome_arquivo}",
            "======================================================================",
        ]
        for num_pag, texto, termos in paginas_promissoras:
            linhas.append(
                f"=== [{doc_id}] PÁGINA {num_pag} (Termos detectados: {', '.join(termos)}) ===\n{texto}"
            )
        linhas.append(f"=== FIM DO DOCUMENTO [{doc_id}] ({nome_arquivo}) ===")
        return "\n\n".join(linhas)

    def _agrupar_em_superbatches(
        self, candidatos: List[CandidatoGemini]
    ) -> List[List[CandidatoGemini]]:
        """
        Agrupa múltiplos PDFs filtrados em Super-Batches respeitando:
        - `GEMINI_SUPERBATCH_MAX_CHARS` (padrão 450.000 chars ≈ 112.000 tokens, metade da janela de 250K TPM)
        - `GEMINI_SUPERBATCH_MAX_FILES` (padrão 5 PDFs por requisição)
        """
        max_chars = int(getattr(settings, "GEMINI_SUPERBATCH_MAX_CHARS", 450_000))
        max_files = int(getattr(settings, "GEMINI_SUPERBATCH_MAX_FILES", 5))

        superbatches: List[List[CandidatoGemini]] = []
        lote_atual: List[CandidatoGemini] = []
        chars_atual = 0

        for cand in candidatos:
            tam_cand = len(cand.bloco_texto)
            cabe_no_lote = (
                len(lote_atual) < max_files
                and (chars_atual + tam_cand) <= max_chars
            )
            if lote_atual and not cabe_no_lote:
                superbatches.append(lote_atual)
                lote_atual = []
                chars_atual = 0

            lote_atual.append(cand)
            chars_atual += tam_cand

        if lote_atual:
            superbatches.append(lote_atual)

        return superbatches

    def _chamar_gemini_com_fallback(
        self,
        indice_progresso: int,
        rotulo_progresso: str,
        prompt: str,
        arquivos_no_lote: int,
    ) -> List[LicitacaoExtraidaSchema]:
        """Executa 1 chamada ao Google Gemini respeitando cadência mínima e fallback de modelos."""
        chave_api = (settings.GEMINI_API_KEY or "").strip()
        if (
            not chave_api
            or "COLE_AQUI" in chave_api.upper()
            or "SUA-CHAVE" in chave_api.upper()
        ):
            raise RuntimeError(
                "API_KEY_INVALID: A variável GEMINI_API_KEY no Coolify está vazia ou com o texto de exemplo "
                "('COLE_AQUI_SUA_CHAVE_DO_GEMINI'). Cole sua chave real (AQ.Ab8RN6...) nas Environment Variables do EPSL no Coolify."
            )

        client = genai.Client(
            api_key=chave_api,
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )

        modelos_ordenados = ordenar_modelos_para_chamada()
        response = None
        ultimo_erro = None

        for info_modelo in modelos_ordenados:
            modelo_atual = info_modelo["model"]
            rpm_limite = info_modelo["rpm_limite"]
            rpd_limite = info_modelo["rpd_limite"]

            espera_seg = aguardar_slot_rpm_se_necessario(modelo_atual, rpm_limite)
            if espera_seg > 0:
                self._log(
                    f"   [Cadência Econômica] Aguardando {espera_seg:.1f}s antes da próxima chamada ao modelo '{modelo_atual}'..."
                )
                atualizar_etapa_arquivo(
                    indice_progresso,
                    rotulo_progresso,
                    f"Pausa econômica de {espera_seg:.0f}s (preservando RPM/TPM de {modelo_atual})...",
                )
                time.sleep(espera_seg)

            atualizar_etapa_arquivo(
                indice_progresso,
                rotulo_progresso,
                f"Consultando IA em Super-Batch ({modelo_atual} | {arquivos_no_lote} PDF(s) na mesma chamada)...",
            )

            try:
                response = client.models.generate_content(
                    model=modelo_atual,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=ResultadoExtracaoDiarioSchema,
                        temperature=0.1,
                    ),
                )
                registrar_uso_modelo(modelo_atual, arquivos_no_lote=arquivos_no_lote)
                self._log(
                    f"   Super-Batch analisado com sucesso pelo modelo '{modelo_atual}' "
                    f"(1 requisição para {arquivos_no_lote} PDF(s) | RPM: {info_modelo['rpm_usado'] + 1}/{rpm_limite} | RPD: {info_modelo['rpd_usado'] + 1}/{rpd_limite})"
                )
                break
            except Exception as exc:
                ultimo_erro = exc
                erro_str = str(exc)
                if "API_KEY_INVALID" in erro_str or "API key not valid" in erro_str:
                    raise RuntimeError(
                        "API_KEY_INVALID: A chave GEMINI_API_KEY configurada no Coolify não é válida "
                        "(verifique se ficou como 'COLE_AQUI_SUA_CHAVE_DO_GEMINI' e substitua por sua chave real 'AQ.Ab8RN6...')."
                    ) from exc

                cooldown_seg = (
                    65.0
                    if "429" in erro_str or "RESOURCE_EXHAUSTED" in erro_str
                    else 30.0
                )
                registrar_cooldown_modelo(modelo_atual, segundos=cooldown_seg)
                self._log(
                    f"   [Fallback de Cota] Modelo '{modelo_atual}' indisponível/ocupado ({erro_str[:110]}). Alternando para o próximo modelo...",
                    style_func=self.style.WARNING,
                )

        if response is None:
            raise RuntimeError(
                f"Todos os modelos Gemini falharam. Último erro: {ultimo_erro}"
            )

        if not response.text:
            return []

        resultado = ResultadoExtracaoDiarioSchema.model_validate_json(response.text)
        return resultado.licitacoes

    def _extrair_superbatch_com_gemini(
        self,
        idx_lote: int,
        total_lotes: int,
        lote: List[CandidatoGemini],
    ) -> Dict[str, List[LicitacaoExtraidaSchema]]:
        """
        Envia um Super-Batch (1 ou mais PDFs) para o Gemini.
        Caso um único PDF seja extraordinariamente grande (> `GEMINI_SUPERBATCH_MAX_CHARS`),
        divide suas páginas em sub-fatias seguras dentro do limite de 250K TPM.
        """
        max_chars = int(getattr(settings, "GEMINI_SUPERBATCH_MAX_CHARS", 450_000))
        mapa_resultado: Dict[str, List[LicitacaoExtraidaSchema]] = {
            c.doc_id: [] for c in lote
        }

        # Caso especial: 1 único PDF cujas páginas somadas ultrapassam max_chars
        if len(lote) == 1 and len(lote[0].bloco_texto) > max_chars:
            cand = lote[0]
            fatias: List[List[tuple[int, str, List[str]]]] = []
            fatia_atual: List[tuple[int, str, List[str]]] = []
            chars_fatia = 0
            for pag in cand.paginas_promissoras:
                tam_pag = len(pag[1]) + 120
                if fatia_atual and (chars_fatia + tam_pag) > max_chars:
                    fatias.append(fatia_atual)
                    fatia_atual = []
                    chars_fatia = 0
                fatia_atual.append(pag)
                chars_fatia += tam_pag
            if fatia_atual:
                fatias.append(fatia_atual)

            self._log(
                f"   Arquivo extenso '{cand.nome_arquivo}' dividido em {len(fatias)} fatia(s) de contexto."
            )
            for idx_f, pags_fatia in enumerate(fatias, start=1):
                bloco_parcial = self._formatar_bloco_documento(
                    cand.doc_id, cand.nome_arquivo, pags_fatia
                )
                prompt = self._montar_prompt_superbatch([cand], bloco_parcial)
                extraidas = self._chamar_gemini_com_fallback(
                    indice_progresso=cand.indice_arquivo,
                    rotulo_progresso=f"{cand.nome_arquivo} (Parte {idx_f}/{len(fatias)})",
                    prompt=prompt,
                    arquivos_no_lote=1,
                )
                mapa_resultado[cand.doc_id].extend(extraidas)
            return mapa_resultado

        # Caso padrão: Múltiplos PDFs combinados em um único Super-Batch!
        conteudo_combinado = "\n\n".join(c.bloco_texto for c in lote)
        prompt = self._montar_prompt_superbatch(lote, conteudo_combinado)
        rotulo = (
            lote[0].nome_arquivo
            if len(lote) == 1
            else f"Super-Batch {idx_lote}/{total_lotes} ({len(lote)} PDFs)"
        )
        todas_extraidas = self._chamar_gemini_com_fallback(
            indice_progresso=lote[-1].indice_arquivo,
            rotulo_progresso=rotulo,
            prompt=prompt,
            arquivos_no_lote=len(lote),
        )

        if len(lote) == 1:
            mapa_resultado[lote[0].doc_id] = todas_extraidas
            return mapa_resultado

        # Mapeia cada licitação de volta para o seu PDF de origem via `documento_id`
        mapa_por_nome = {c.nome_arquivo.lower(): c.doc_id for c in lote}
        for item in todas_extraidas:
            raw_id = (item.documento_id or "").strip().upper().replace("[", "").replace("]", "")
            if raw_id in mapa_resultado:
                mapa_resultado[raw_id].append(item)
            elif f"DOC_{raw_id}" in mapa_resultado:
                mapa_resultado[f"DOC_{raw_id}"].append(item)
            elif item.documento_id.strip().lower() in mapa_por_nome:
                doc_key = mapa_por_nome[item.documento_id.strip().lower()]
                mapa_resultado[doc_key].append(item)
            else:
                # Fallback seguro para o primeiro documento do lote caso a IA omita o ID
                mapa_resultado[lote[0].doc_id].append(item)

        return mapa_resultado

    def _montar_prompt_superbatch(
        self, lote: List[CandidatoGemini], conteudo_combinado: str
    ) -> str:
        resumo_docs = "\n".join(
            f"- [{c.doc_id}] Arquivo: {c.nome_arquivo}" for c in lote
        )
        return (
            "Você é um especialista em análise de Diários Oficiais e Licitações Públicas no Brasil.\n"
            f"Abaixo estão as páginas pré-filtradas de {len(lote)} Diário(s) Oficial(is):\n"
            f"{resumo_docs}\n\n"
            "Identifique TODAS as licitações, avisos de edital, pregões, chamadas públicas, dispensas ou "
            "contratações que tenham relação com nosso nicho: Tecnologia, Computação, Computadores, "
            "Laboratórios de Informática, TI, Tecnologia da Informação, Manutenção de Computadores, "
            "Livros Didáticos, Formação em Computação, Software, Hardware ou Infraestrutura de TI.\n\n"
            "Regras importantes:\n"
            "1. Preencha obrigatoriamente 'documento_id' com o código exato do documento de origem "
            "(ex: 'DOC_1', 'DOC_2') indicado no cabeçalho 'INÍCIO DO DOCUMENTO [DOC_X]'.\n"
            "2. Ignore atos administrativos sem relação com compras/contratações/licitações (ex: nomeações de cargos).\n"
            "3. Se uma mesma licitação começar em uma página e continuar na seguinte dentro do mesmo documento, "
            "consolide em um único item e informe ambas as páginas em 'paginas_referencia' (ex: '14, 15').\n"
            "4. Caso nenhuma licitação válida do nosso nicho esteja presente, retorne a lista vazia.\n\n"
            f"{conteudo_combinado}"
        )

    def _parse_data_publicacao(self, data_str: str) -> datetime.date:
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.datetime.strptime(data_str.strip(), fmt).date()
            except ValueError:
                continue
        return datetime.date.today()

    def _ler_bytes_pdf(
        self,
        use_local: bool,
        s3_client: Optional[BaseClient],
        bucket_name: str,
        object_key: str,
    ) -> bytes:
        if use_local:
            return Path(object_key).read_bytes()
        assert s3_client is not None
        response = s3_client.get_object(Bucket=bucket_name, Key=object_key)
        return response["Body"].read()

    def _mover_pdf_processado(
        self,
        use_local: bool,
        s3_client: Optional[BaseClient],
        bucket_name: str,
        object_key: str,
        processed_prefix: str,
        data_ref: datetime.date,
    ) -> str:
        if use_local:
            return self._mover_pdf_local_para_processados(
                source_path_str=object_key,
                processed_prefix=processed_prefix,
                data_ref=data_ref,
            )
        assert s3_client is not None
        return self._mover_pdf_s3_para_processados(
            s3_client=s3_client,
            bucket_name=bucket_name,
            source_key=object_key,
            processed_prefix=processed_prefix,
            data_ref=data_ref,
        )

    def _remover_pdf_duplicado_entrada(
        self,
        use_local: bool,
        s3_client: Optional[BaseClient],
        bucket_name: str,
        object_key: str,
    ) -> None:
        if use_local:
            p = Path(object_key)
            if p.exists():
                p.unlink()
            return
        assert s3_client is not None
        s3_client.delete_object(Bucket=bucket_name, Key=object_key)

    # --------------------------------------------------------------------------
    # Fase 1 (Triagem Local) e Fase 2 (Arquivamento Pós-Super-Batch)
    # --------------------------------------------------------------------------
    def _triar_arquivo_localmente(
        self,
        indice_arquivo: int,
        doc_seq: int,
        use_local: bool,
        s3_client: Optional[BaseClient],
        bucket_name: str,
        object_key: str,
        processed_prefix: str,
        dry_run: bool = False,
    ) -> Optional[CandidatoGemini]:
        nome_arquivo = Path(object_key).name
        self._log(f"\n-> [{indice_arquivo}] Triagem local de '{object_key}'...")
        pdf_bytes = self._ler_bytes_pdf(
            use_local=use_local,
            s3_client=s3_client,
            bucket_name=bucket_name,
            object_key=object_key,
        )

        # Passo 0: Deduplicação Dupla (Hash Binário SHA-256 + Hash do Conteúdo Textual Normalizado)
        atualizar_etapa_arquivo(
            indice_arquivo,
            nome_arquivo,
            "Verificando autenticidade SHA-256 (anti-duplicata)...",
        )
        sha256_arquivo = calcular_sha256_bytes(pdf_bytes)
        sha256_texto, total_paginas = calcular_sha256_texto_pdf(pdf_bytes)

        duplicado_db = DiarioProcessado.objects.filter(
            sha256_arquivo=sha256_arquivo
        ).first()
        tipo_match = "Hash Binário SHA-256 exato"

        if duplicado_db is None and sha256_texto:
            duplicado_db = DiarioProcessado.objects.filter(
                sha256_texto=sha256_texto
            ).first()
            tipo_match = "Conteúdo Textual 100% idêntico (mesmo Diário Oficial com outro nome/metadado)"

        if duplicado_db is not None:
            data_proc_str = duplicado_db.processado_em.strftime("%d/%m/%Y %H:%M")
            self._log(
                f"   [DUPLICADO BLOQUEADO - ECONOMIA DE IA] O arquivo '{nome_arquivo}' "
                f"é idêntico ao diário já processado '{duplicado_db.nome_arquivo}' "
                f"({tipo_match} | Processado em: {data_proc_str}).\n"
                f"   -> Envio ao Google Gemini CANCELADO (0 RPM / 0 RPD gastos)!",
                style_func=self.style.WARNING,
            )
            if not dry_run:
                self._remover_pdf_duplicado_entrada(
                    use_local=use_local,
                    s3_client=s3_client,
                    bucket_name=bucket_name,
                    object_key=object_key,
                )
            concluir_um_arquivo(nome_arquivo=nome_arquivo, foi_duplicado=True)
            return None

        # Passo 1: PyMuPDF + Regex
        atualizar_etapa_arquivo(
            indice_arquivo,
            nome_arquivo,
            f"Filtrando {total_paginas} página(s) com PyMuPDF + Regex...",
        )
        paginas_promissoras = self._extrair_paginas_promissoras(pdf_bytes)
        nums_paginas = [str(p[0]) for p in paginas_promissoras]

        if not paginas_promissoras:
            self._log(
                f"   Nenhuma página de '{object_key}' conteve palavras-chave do nicho (0 chamadas IA).",
                style_func=self.style.WARNING,
            )
            if not dry_run:
                link_arquivado = self._mover_pdf_processado(
                    use_local=use_local,
                    s3_client=s3_client,
                    bucket_name=bucket_name,
                    object_key=object_key,
                    processed_prefix=processed_prefix,
                    data_ref=datetime.date.today(),
                )
                registrar_diario_processado(
                    nome_arquivo=Path(link_arquivado).name,
                    sha256_arquivo=sha256_arquivo,
                    sha256_texto=sha256_texto,
                    tamanho_bytes=len(pdf_bytes),
                    total_paginas=total_paginas,
                    qtd_licitacoes=0,
                    link_documento=link_arquivado,
                )
                self._log(f"   PDF arquivado sem ocorrências em: {link_arquivado}")
            concluir_um_arquivo(nome_arquivo=nome_arquivo, sem_match_regex=True)
            return None

        doc_id = f"DOC_{doc_seq}"
        bloco_texto = self._formatar_bloco_documento(
            doc_id=doc_id,
            nome_arquivo=nome_arquivo,
            paginas_promissoras=paginas_promissoras,
        )
        self._log(
            f"   [{doc_id}] Filtro Regex capturou {len(paginas_promissoras)} página(s) ({', '.join(nums_paginas)}) "
            f"[{len(bloco_texto):,} chars] -> Adicionado ao Super-Batch!",
            style_func=self.style.SUCCESS,
        )

        if dry_run:
            concluir_um_arquivo(nome_arquivo=nome_arquivo, licitacoes_adicionadas=0)
            return None

        return CandidatoGemini(
            indice_arquivo=indice_arquivo,
            doc_id=doc_id,
            object_key=object_key,
            nome_arquivo=nome_arquivo,
            tamanho_bytes=len(pdf_bytes),
            sha256_arquivo=sha256_arquivo,
            sha256_texto=sha256_texto,
            total_paginas=total_paginas,
            paginas_promissoras=paginas_promissoras,
            bloco_texto=bloco_texto,
        )

    def _finalizar_e_arquivar_candidato(
        self,
        cand: CandidatoGemini,
        licitacoes_extraidas: List[LicitacaoExtraidaSchema],
        use_local: bool,
        s3_client: Optional[BaseClient],
        bucket_name: str,
        processed_prefix: str,
    ) -> None:
        data_referencia = (
            self._parse_data_publicacao(licitacoes_extraidas[0].data_publicacao)
            if licitacoes_extraidas
            else datetime.date.today()
        )

        atualizar_etapa_arquivo(
            cand.indice_arquivo,
            cand.nome_arquivo,
            "Movendo PDF para diarios_processados/ e salvando licitações no banco...",
        )
        link_documento = self._mover_pdf_processado(
            use_local=use_local,
            s3_client=s3_client,
            bucket_name=bucket_name,
            object_key=cand.object_key,
            processed_prefix=processed_prefix,
            data_ref=data_referencia,
        )

        qtd_salvas = 0
        if licitacoes_extraidas:
            objetos_para_salvar = [
                Licitacao(
                    cidade=item.cidade[:150],
                    orgao_emissor=item.orgao_emissor[:255],
                    data_publicacao=self._parse_data_publicacao(item.data_publicacao),
                    modalidade=item.modalidade[:100],
                    objeto_licitacao=item.objeto_licitacao,
                    paginas_referencia=item.paginas_referencia[:100],
                    link_documento=link_documento,
                )
                for item in licitacoes_extraidas
            ]
            with transaction.atomic():
                Licitacao.objects.bulk_create(objetos_para_salvar)
                qtd_salvas = len(objetos_para_salvar)

            self._log(
                f"   [OK] [{cand.doc_id}] '{cand.nome_arquivo}': {qtd_salvas} licitação(ões) salva(s) | PDF movido para: {link_documento}",
                style_func=self.style.SUCCESS,
            )
        else:
            self._log(
                f"   [{cand.doc_id}] '{cand.nome_arquivo}': Gemini descartou páginas como falso-positivo. PDF movido para: {link_documento}",
                style_func=self.style.WARNING,
            )

        registrar_diario_processado(
            nome_arquivo=Path(link_documento).name,
            sha256_arquivo=cand.sha256_arquivo,
            sha256_texto=cand.sha256_texto,
            tamanho_bytes=cand.tamanho_bytes,
            total_paginas=cand.total_paginas,
            qtd_licitacoes=qtd_salvas,
            link_documento=link_documento,
        )
        concluir_um_arquivo(
            nome_arquivo=cand.nome_arquivo,
            licitacoes_adicionadas=qtd_salvas,
        )
