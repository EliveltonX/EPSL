"""
Management Command: processar_diarios

Suporta automaticamente dois modos de operação (definidos por `APP_ENV` no settings.py):
1. Modo TESTE (`APP_ENV=test` / `USE_LOCAL_STORAGE=True`):
   - Lê arquivos PDF da pasta local `storage_local/novos_diarios/`.
   - Move os arquivos processados para `storage_local/diarios_processados/{YYYY}/{MM}/{arquivo.pdf}`.
   - Salva os dados estruturados no banco MySQL local.
2. Modo DEPLOY (`APP_ENV=deploy` / `USE_LOCAL_STORAGE=False`):
   - Lê arquivos PDF do Supabase Storage (`novos_diarios/`) via `boto3`.
   - Move os arquivos processados para `diarios_processados/{YYYY}/{MM}/{arquivo.pdf}` no bucket.
   - Salva os dados estruturados no banco PostgreSQL do Supabase.
"""

from __future__ import annotations

import datetime
import logging
import re
import shutil
import unicodedata
from pathlib import Path
from typing import List, Optional
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
# 2. Schema Pydantic para Extração Estruturada no Google Gemini
# ==============================================================================
class LicitacaoExtraidaSchema(BaseModel):
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
        description="Lista de licitações do nicho de Tecnologia/Computação/Educação encontradas nas páginas enviadas.",
    )


# ==============================================================================
# 3. Management Command do Django
# ==============================================================================
class Command(BaseCommand):
    help = (
        "Varre a pasta 'novos_diarios/' (em pasta local no modo teste ou Supabase Storage no modo deploy), "
        "filtra páginas com PyMuPDF + Regex, extrai dados estruturados com Google Gemini, "
        "salva no banco de dados e move o PDF para 'diarios_processados/'."
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
            help="Limita a quantidade máxima de arquivos PDF processados nesta execução (0 = todos).",
        )

    def handle(self, *args, **options) -> None:
        dry_run: bool = options["dry_run"]
        max_files: int = options["max_files"]
        use_local: bool = settings.USE_LOCAL_STORAGE

        bucket_name: str = settings.SUPABASE_STORAGE_BUCKET
        input_prefix: str = settings.SUPABASE_INPUT_PREFIX
        processed_prefix: str = settings.SUPABASE_PROCESSED_PREFIX

        modo_str = (
            f"TESTE LOCAL (MySQL + Pasta '{settings.LOCAL_STORAGE_ROOT}')"
            if use_local
            else f"DEPLOY PRODUÇÃO (PostgreSQL Supabase + Bucket '{bucket_name}')"
        )
        self.stdout.write(
            self.style.NOTICE(
                f"Iniciando processamento de Diários Oficiais | Modo: {modo_str}"
            )
        )

        s3_client: Optional[BaseClient] = None
        if use_local:
            pdf_keys = self._listar_pdfs_locais(input_prefix, processed_prefix)
        else:
            s3_client = self._get_s3_client()
            pdf_keys = self._listar_pdfs_s3(s3_client, bucket_name, input_prefix)

        if not pdf_keys:
            self.stdout.write(
                self.style.SUCCESS("Nenhum arquivo PDF pendente em 'novos_diarios/'.")
            )
            return

        if max_files > 0:
            pdf_keys = pdf_keys[:max_files]

        self.stdout.write(f"Encontrados {len(pdf_keys)} arquivo(s) PDF para processar.")

        for object_key in pdf_keys:
            try:
                self._processar_arquivo_pdf(
                    use_local=use_local,
                    s3_client=s3_client,
                    bucket_name=bucket_name,
                    object_key=object_key,
                    processed_prefix=processed_prefix,
                    dry_run=dry_run,
                )
            except Exception as exc:
                logger.exception("Erro ao processar o arquivo %s: %s", object_key, exc)
                self.stderr.write(
                    self.style.ERROR(f"[ERRO] Falha ao processar '{object_key}': {exc}")
                )

    # --------------------------------------------------------------------------
    # Métodos Auxiliares: Armazenamento Local em Pasta (Modo Teste)
    # --------------------------------------------------------------------------
    def _listar_pdfs_locais(self, input_prefix: str, processed_prefix: str) -> List[str]:
        """
        Garante que as pastas locais `novos_diarios/` e `diarios_processados/` existam
        dentro de `LOCAL_STORAGE_ROOT` e retorna a lista de caminhos dos PDFs pendentes.
        """
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
        """
        Se já existir um arquivo com o mesmo nome na pasta de destino local,
        adiciona um sufixo incremental (_2, _3, ...) antes da extensão .pdf.
        """
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
        """
        Move o PDF da pasta local `storage_local/novos_diarios/{arquivo.pdf}` para
        `storage_local/diarios_processados/{YYYY}/{MM}/{arquivo.pdf}` (renomeando
        automaticamente caso já exista arquivo com o mesmo nome) e retorna a URL local.
        """
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
        return f"{settings.LOCAL_MEDIA_BASE_URL}/{encoded_path}"

    # --------------------------------------------------------------------------
    # Métodos Auxiliares: Supabase Storage via S3 / boto3 (Modo Deploy)
    # --------------------------------------------------------------------------
    def _get_s3_client(self) -> BaseClient:
        """Inicializa o cliente boto3 apontando para o endpoint S3 do Supabase Storage."""
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
        """Lista todas as chaves .pdf dentro de `novos_diarios/` no Supabase Storage."""
        paginator = s3_client.get_paginator("list_objects_v2")
        pdf_keys: List[str] = []

        for page in paginator.paginate(Bucket=bucket_name, Prefix=input_prefix):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                if key.lower().endswith(".pdf") and key != input_prefix:
                    pdf_keys.append(key)

        return pdf_keys

    def _s3_objeto_existe(
        self, s3_client: BaseClient, bucket_name: str, key: str
    ) -> bool:
        """Verifica se uma chave (Key) já existe no bucket S3/Supabase Storage."""
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
        """
        O protocolo S3 / Supabase Storage NÃO renomeia arquivos automaticamente (ele sobrescreve
        no PutObject/CopyObject ou retorna erro 409). Esta função verifica se a chave já existe
        em `diarios_processados/{YYYY}/{MM}/` e, se existir, adiciona `_2`, `_3`, etc.
        """
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
        """
        Move o PDF no Supabase Storage de `novos_diarios/{arquivo.pdf}` para
        `diarios_processados/{YYYY}/{MM}/{arquivo.pdf}` (garantindo nome único sem sobrescrever)
        e retorna o link público.
        """
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
    # Métodos Auxiliares: PyMuPDF + Regex + Gemini
    # --------------------------------------------------------------------------
    def _extrair_paginas_promissoras(
        self, pdf_bytes: bytes
    ) -> List[tuple[int, str, List[str]]]:
        """
        Abre o PDF em memória com PyMuPDF (fitz) e retorna apenas as páginas cujo texto
        casa com o Regex do nosso nicho de negócio.
        """
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

    def _extrair_licitacoes_com_gemini(
        self,
        nome_arquivo: str,
        paginas_promissoras: List[tuple[int, str, List[str]]],
    ) -> List[LicitacaoExtraidaSchema]:
        """
        Envia exclusivamente o texto das páginas pré-filtradas pelo Regex para a API
        do Google Gemini e retorna a lista estruturada de licitações.
        """
        if not settings.GEMINI_API_KEY:
            raise RuntimeError(
                "A variável de ambiente GEMINI_API_KEY não está configurada."
            )

        client = genai.Client(
            api_key=settings.GEMINI_API_KEY,
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )

        blocos_paginas = []
        for num_pag, texto, termos in paginas_promissoras:
            blocos_paginas.append(
                f"=== PÁGINA {num_pag} (Termos detectados: {', '.join(termos)}) ===\n{texto}"
            )
        conteudo_paginas = "\n\n".join(blocos_paginas)

        prompt = (
            "Você é um especialista em análise de Diários Oficiais e Licitações Públicas no Brasil.\n"
            f"Abaixo estão as páginas pré-filtradas do arquivo '{nome_arquivo}'.\n"
            "Identifique TODAS as licitações, avisos de edital, pregões, chamadas públicas, dispensas ou "
            "contratações que tenham relação com nosso nicho: Tecnologia, Computação, Computadores, "
            "Laboratórios de Informática, TI, Tecnologia da Informação, Manutenção de Computadores, "
            "Livros Didáticos, Formação em Computação, Software, Hardware ou Infraestrutura de TI.\n\n"
            "Regras importantes:\n"
            "1. Ignore atos administrativos sem relação com compras/contratações/licitações (ex: nomeações de cargos).\n"
            "2. Se uma mesma licitação começar em uma página e continuar na seguinte, consolide em um único item "
            "e informe ambas as páginas em 'paginas_referencia' (ex: '14, 15').\n"
            "3. Caso nenhuma licitação válida do nosso nicho esteja presente nas páginas, retorne a lista vazia.\n\n"
            f"{conteudo_paginas}"
        )

        modelos_candidatos = []
        for m in getattr(settings, "GEMINI_FALLBACK_MODELS", [settings.GEMINI_MODEL]):
            if m and m not in modelos_candidatos:
                modelos_candidatos.append(m)

        response = None
        ultimo_erro = None
        for modelo_atual in modelos_candidatos:
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
                self.stdout.write(f"   Extraído com sucesso utilizando o modelo: {modelo_atual}")
                break
            except Exception as exc:
                ultimo_erro = exc
                self.stdout.write(
                    self.style.WARNING(
                        f"   [Aviso] Modelo '{modelo_atual}' indisponível ({exc}). Tentando próximo modelo de fallback..."
                    )
                )

        if response is None:
            raise RuntimeError(f"Todos os modelos Gemini falharam. Último erro: {ultimo_erro}")

        if not response.text:
            return []

        resultado = ResultadoExtracaoDiarioSchema.model_validate_json(response.text)
        return resultado.licitacoes

    def _parse_data_publicacao(self, data_str: str) -> datetime.date:
        """Converte a data retornada pela IA (YYYY-MM-DD ou DD/MM/YYYY) em `datetime.date`."""
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
        """Lê os bytes do PDF a partir da pasta local (teste) ou do Supabase Storage (deploy)."""
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
        """Move o PDF processado na pasta local (teste) ou no Supabase Storage (deploy)."""
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
        """Remove de `novos_diarios/` um PDF que foi identificado como duplicata de outro já processado."""
        if use_local:
            p = Path(object_key)
            if p.exists():
                p.unlink()
            return
        assert s3_client is not None
        s3_client.delete_object(Bucket=bucket_name, Key=object_key)

    # --------------------------------------------------------------------------
    # Orquestração por Arquivo PDF
    # --------------------------------------------------------------------------
    def _processar_arquivo_pdf(
        self,
        use_local: bool,
        s3_client: Optional[BaseClient],
        bucket_name: str,
        object_key: str,
        processed_prefix: str,
        dry_run: bool = False,
    ) -> None:
        self.stdout.write(f"\n-> Lendo '{object_key}'...")
        pdf_bytes = self._ler_bytes_pdf(
            use_local=use_local,
            s3_client=s3_client,
            bucket_name=bucket_name,
            object_key=object_key,
        )

        # Passo 0: Deduplicação Dupla (Hash Binário SHA-256 + Hash do Conteúdo Textual Normalizado)
        sincronizar_hashes_locais_existentes()
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
            self.stdout.write(
                self.style.WARNING(
                    f"   [DUPLICADO BLOQUEADO - ECONOMIA DE IA] O arquivo '{Path(object_key).name}' "
                    f"é idêntico ao diário já processado '{duplicado_db.nome_arquivo}' "
                    f"({tipo_match} | Processado em: {data_proc_str}).\n"
                    f"   -> Envio ao Google Gemini CANCELADO para evitar custo duplicado de IA!"
                )
            )
            if not dry_run:
                self._remover_pdf_duplicado_entrada(
                    use_local=use_local,
                    s3_client=s3_client,
                    bucket_name=bucket_name,
                    object_key=object_key,
                )
                self.stdout.write(
                    f"   -> Cópia redundante removida de 'novos_diarios/'. Documento original mantido em: {duplicado_db.link_documento}"
                )
            return

        # Passo 1: PyMuPDF + Regex
        paginas_promissoras = self._extrair_paginas_promissoras(pdf_bytes)
        nums_paginas = [str(p[0]) for p in paginas_promissoras]

        if not paginas_promissoras:
            self.stdout.write(
                self.style.WARNING(
                    f"   Nenhuma página de '{object_key}' conteve palavras-chave do nicho."
                )
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
                self.stdout.write(
                    f"   PDF arquivado sem ocorrências em: {link_arquivado}"
                )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"   Filtro Regex encontrou indícios nas páginas: {', '.join(nums_paginas)}"
            )
        )

        if dry_run:
            self.stdout.write(
                self.style.NOTICE(
                    "   [DRY-RUN] Pulando chamada ao Gemini e escrita no banco/Storage."
                )
            )
            return

        # Passo 2: Chamada ao Google Gemini com as páginas promissoras
        nome_arquivo = Path(object_key).name
        licitacoes_extraidas = self._extrair_licitacoes_com_gemini(
            nome_arquivo=nome_arquivo,
            paginas_promissoras=paginas_promissoras,
        )

        # Define a data de referência para a pasta {YYYY}/{MM}
        data_referencia = (
            self._parse_data_publicacao(licitacoes_extraidas[0].data_publicacao)
            if licitacoes_extraidas
            else datetime.date.today()
        )

        # Passo 3: Move o PDF para diarios_processados/{YYYY}/{MM}/{arquivo.pdf}
        link_documento = self._mover_pdf_processado(
            use_local=use_local,
            s3_client=s3_client,
            bucket_name=bucket_name,
            object_key=object_key,
            processed_prefix=processed_prefix,
            data_ref=data_referencia,
        )

        # Passo 4: Salva os registros estruturados no banco e registra o Hash Anti-Duplicata
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

            self.stdout.write(
                self.style.SUCCESS(
                    f"   [OK] {qtd_salvas} licitação(ões) salva(s) | PDF movido para: {link_documento}"
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"   Gemini analisou as páginas mas descartou como falso-positivo. PDF movido para: {link_documento}"
                )
            )

        registrar_diario_processado(
            nome_arquivo=Path(link_documento).name,
            sha256_arquivo=sha256_arquivo,
            sha256_texto=sha256_texto,
            tamanho_bytes=len(pdf_bytes),
            total_paginas=total_paginas,
            qtd_licitacoes=qtd_salvas,
            link_documento=link_documento,
        )
