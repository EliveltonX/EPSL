"""
Módulo de Deduplicação Inteligente de Diários Oficiais (Dupla Camada: Binária + Textual).

Objetivo:
Impedir que um mesmo Diário Oficial — mesmo que tenha sido renomeado por outro funcionário
ou baixado novamente com metadados PDF diferentes — seja reenviado ou reprocessado pelo
Google Gemini, eliminando gastos duplicados com tokens de IA e armazenamento.

Camadas de Proteção:
1. No Upload Web (`/licitacoes/upload/`):
   - O navegador calcula o hash SHA-256 do arquivo via Web Crypto API antes mesmo de enviar os bytes.
   - O endpoint `/licitacoes/upload/presign/` consulta `DiarioProcessado` e a fila `novos_diarios/`,
     bloqueando o upload de arquivos repetidos na hora.
2. No Pipeline (`processar_diarios`):
   - Camada 1 (Binária): Verifica o `sha256_arquivo` dos bytes brutos do PDF antes de abrir o documento.
   - Camada 2 (Textual/Semântica via PyMuPDF): Extrai e normaliza o texto completo do PDF localmente
     e calcula o `sha256_texto`. Se o conteúdo textual for idêntico a um diário já analisado,
     aborta a chamada ao Google Gemini (custo zero de IA) e remove a duplicata da fila.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pymupdf as fitz
from django.conf import settings

from licitacoes.models import DiarioProcessado


def calcular_sha256_bytes(pdf_bytes: bytes) -> str:
    """Retorna o hash SHA-256 hexadecimal (64 caracteres) dos bytes brutos do PDF."""
    return hashlib.sha256(pdf_bytes).hexdigest().lower()


def _normalizar_texto_completo(texto: str) -> str:
    """
    Normaliza o texto extraído do PDF (remove acentos, converte para minúsculas e
    colapsa espaços/quebras de linha) para gerar uma assinatura textual imune a
    pequenas variações de metadados do PDF.
    """
    nfkd = unicodedata.normalize("NFKD", texto)
    sem_acentos = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", sem_acentos).strip().lower()


def calcular_sha256_texto_pdf(pdf_bytes: bytes) -> Tuple[str, int]:
    """
    Lê todas as páginas do PDF localmente com PyMuPDF (custo zero de IA) e retorna
    `(sha256_texto_normalizado, total_paginas)`.
    """
    partes_texto = []
    total_paginas = 0
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        total_paginas = len(doc)
        for page_index in range(total_paginas):
            page = doc.load_page(page_index)
            txt = page.get_text("text") or ""
            if txt.strip():
                partes_texto.append(_normalizar_texto_completo(txt))

    texto_consolidado = "\n".join(partes_texto)
    if not texto_consolidado:
        return "", total_paginas

    sha_texto = hashlib.sha256(texto_consolidado.encode("utf-8")).hexdigest().lower()
    return sha_texto, total_paginas


def sincronizar_hashes_locais_existentes() -> None:
    """
    Garante que quaisquer PDFs já arquivados anteriormente em `storage_local/diarios_processados/`
    (antes da criação da tabela `DiarioProcessado`) tenham seus hashes SHA-256 registrados.
    """
    if not settings.USE_LOCAL_STORAGE:
        return

    root = Path(settings.LOCAL_STORAGE_ROOT)
    proc_dir = root / settings.SUPABASE_PROCESSED_PREFIX.strip("/")
    if not proc_dir.exists():
        return

    hashes_conhecidos = set(
        DiarioProcessado.objects.values_list("sha256_arquivo", flat=True)
    )

    for pdf_path in proc_dir.rglob("*.pdf"):
        if not pdf_path.is_file():
            continue
        try:
            pdf_bytes = pdf_path.read_bytes()
            sha_bin = calcular_sha256_bytes(pdf_bytes)
            if sha_bin in hashes_conhecidos:
                continue
            sha_txt, num_pags = calcular_sha256_texto_pdf(pdf_bytes)
            rel_path = pdf_path.relative_to(root)
            DiarioProcessado.objects.get_or_create(
                sha256_arquivo=sha_bin,
                defaults={
                    "nome_arquivo": pdf_path.name,
                    "sha256_texto": sha_txt,
                    "tamanho_bytes": len(pdf_bytes),
                    "total_paginas": num_pags,
                    "link_documento": f"{settings.LOCAL_MEDIA_BASE_URL}/{rel_path}",
                },
            )
            hashes_conhecidos.add(sha_bin)
        except Exception:
            continue


def verificar_duplicidade_por_hash(
    sha256_arquivo: str,
    sha256_texto: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Verifica se um PDF já foi processado anteriormente (no banco `DiarioProcessado`)
    ou se um arquivo idêntico já está aguardando na fila `novos_diarios/`.
    Retorna um dicionário com os detalhes do arquivo original caso seja duplicado, ou `None`.
    """
    sha_bin = (sha256_arquivo or "").strip().lower()
    sha_txt = (sha256_texto or "").strip().lower()

    sincronizar_hashes_locais_existentes()

    # 1. Busca pelo Hash Binário exato em DiarioProcessado
    if sha_bin:
        existente_bin = DiarioProcessado.objects.filter(sha256_arquivo=sha_bin).first()
        if existente_bin:
            return {
                "motivo": "Hash binário SHA-256 idêntico a um Diário Oficial já processado",
                "arquivo_original": existente_bin.nome_arquivo,
                "processado_em": existente_bin.processado_em.strftime("%d/%m/%Y %H:%M"),
                "link_documento": existente_bin.link_documento,
                "origem": "diarios_processados",
            }

    # 2. Busca pelo Hash do Texto Normalizado em DiarioProcessado (caso metadados PDF mudem)
    if sha_txt:
        existente_txt = DiarioProcessado.objects.filter(sha256_texto=sha_txt).first()
        if existente_txt:
            return {
                "motivo": "Conteúdo textual 100% idêntico a um Diário Oficial já processado",
                "arquivo_original": existente_txt.nome_arquivo,
                "processado_em": existente_txt.processado_em.strftime("%d/%m/%Y %H:%M"),
                "link_documento": existente_txt.link_documento,
                "origem": "diarios_processados",
            }

    # 3. No modo local, verifica também se já há um arquivo idêntico aguardando na fila `novos_diarios/`
    if sha_bin and settings.USE_LOCAL_STORAGE:
        input_dir = (
            Path(settings.LOCAL_STORAGE_ROOT)
            / settings.SUPABASE_INPUT_PREFIX.strip("/")
        )
        if input_dir.exists():
            for p in input_dir.glob("*.pdf"):
                if p.is_file():
                    try:
                        if calcular_sha256_bytes(p.read_bytes()) == sha_bin:
                            return {
                                "motivo": "Arquivo idêntico já está na fila 'novos_diarios/' aguardando processamento",
                                "arquivo_original": p.name,
                                "processado_em": "Na fila atual",
                                "link_documento": "",
                                "origem": "novos_diarios",
                            }
                    except Exception:
                        continue

    return None


def registrar_diario_processado(
    nome_arquivo: str,
    sha256_arquivo: str,
    sha256_texto: str,
    tamanho_bytes: int,
    total_paginas: int,
    qtd_licitacoes: int,
    link_documento: str,
) -> DiarioProcessado:
    """Salva ou atualiza o registro de hash do Diário Oficial recém-processado."""
    obj, _ = DiarioProcessado.objects.update_or_create(
        sha256_arquivo=sha256_arquivo.lower(),
        defaults={
            "nome_arquivo": nome_arquivo[:255],
            "sha256_texto": (sha256_texto or "").lower(),
            "tamanho_bytes": tamanho_bytes,
            "total_paginas": total_paginas,
            "qtd_licitacoes_extraidas": qtd_licitacoes,
            "link_documento": link_documento[:500],
        },
    )
    return obj

