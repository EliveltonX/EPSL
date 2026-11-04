"""
Módulo de Upload Direto (Presigned S3 URLs / Stream Direto) e Prevenção de Colisão de Nomes.

Decisões de Arquitetura (ver `.agents/decisions/ADR-002_Upload_Direto_S3_Multiplos_PDFs_AntiColisao.md`):
1. O protocolo S3 e o Supabase Storage S3 NÃO renomeiam automaticamente arquivos com o mesmo
   nome (sobrescrevem no PutObject/CopyObject ou retornam erro 409).
2. Por isso, este módulo verifica previamente a existência do nome tanto em `novos_diarios/`
   quanto em `diarios_processados/` (inclusive subpastas `{YYYY}/{MM}/`) e aplica renomeação
   incremental (`arquivo_2.pdf`, `arquivo_3.pdf`, ...).
3. Para evitar que o servidor Django trave recebendo ou processando múltiplos PDFs grandes,
   o Django atua apenas como emissor de URLs pré-assinadas (`generate_presigned_url` PUT),
   permitindo que o navegador envie múltiplos PDFs diretamente ao S3/Storage.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple
from urllib.parse import quote

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from django.conf import settings

from licitacoes.deduplicacao import verificar_duplicidade_por_hash


def sanitizar_nome_pdf(nome_original: str) -> str:
    """Limpa caracteres especiais mantendo apenas letras, números, '_', '-' e extensão '.pdf'."""
    apenas_nome = Path(nome_original).name.strip()
    stem = Path(apenas_nome).stem
    stem_limpo = re.sub(r"[^a-zA-Z0-9_-]+", "_", stem).strip("_") or "diario_oficial"
    return f"{stem_limpo[:120]}.pdf"


def _obter_s3_client() -> BaseClient:
    return boto3.client(
        "s3",
        endpoint_url=settings.SUPABASE_S3_ENDPOINT_URL,
        aws_access_key_id=settings.SUPABASE_S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.SUPABASE_S3_SECRET_ACCESS_KEY,
        region_name=settings.SUPABASE_S3_REGION_NAME,
        config=Config(signature_version="s3v4"),
    )


def _listar_nomes_existentes_local() -> Set[str]:
    """
    Retorna o conjunto de todos os nomes de arquivos .pdf (em lowercase) já existentes
    em `storage_local/novos_diarios/` e em qualquer subpasta de `storage_local/diarios_processados/`.
    """
    root = Path(settings.LOCAL_STORAGE_ROOT)
    existentes: Set[str] = set()
    if not root.exists():
        return existentes

    for pdf_path in root.rglob("*.pdf"):
        if pdf_path.is_file():
            existentes.add(pdf_path.name.lower())
    return existentes


def _listar_nomes_existentes_s3(s3_client: BaseClient, bucket_name: str) -> Set[str]:
    """
    Retorna todos os nomes de arquivos .pdf já existentes em `novos_diarios/`
    e em `diarios_processados/` no bucket S3 / Supabase Storage.
    """
    existentes: Set[str] = set()
    paginator = s3_client.get_paginator("list_objects_v2")
    for prefix in (settings.SUPABASE_INPUT_PREFIX, settings.SUPABASE_PROCESSED_PREFIX):
        try:
            for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key: str = obj["Key"]
                    if key.lower().endswith(".pdf"):
                        filename = key.split("/")[-1].lower()
                        existentes.add(filename)
        except Exception:
            continue
    return existentes


def _escolher_nome_sem_colisao(
    nome_limpo: str, nomes_ocupados: Set[str]
) -> Tuple[str, bool]:
    """
    Se `nome_limpo` já estiver em `nomes_ocupados` (seja em `novos_diarios/`, em
    `diarios_processados/` ou no mesmo lote de múltiplos arquivos), gera `nome_2.pdf`, `nome_3.pdf`, etc.
    Retorna `(nome_final, foi_renomeado)`.
    """
    if nome_limpo.lower() not in nomes_ocupados:
        nomes_ocupados.add(nome_limpo.lower())
        return nome_limpo, False

    stem = Path(nome_limpo).stem
    suffix = Path(nome_limpo).suffix or ".pdf"
    contador = 2
    while True:
        candidato = f"{stem}_{contador}{suffix}"
        if candidato.lower() not in nomes_ocupados:
            nomes_ocupados.add(candidato.lower())
            return candidato, True
        contador += 1


def gerar_lote_urls_upload(
    filenames: List[Any],
) -> List[Dict[str, Any]]:
    """
    Para uma lista de arquivos enviados pelo navegador (strings com nomes ou dicts `{"filename": ..., "sha256": ...}`):
    1. Verifica se o hash SHA-256 do arquivo já existe em `DiarioProcessado`, na fila `novos_diarios/`
       ou no próprio lote de envio (mesmo que o arquivo tenha sido renomeado por outro funcionário).
       Se for duplicado, retorna `duplicado=True` e bloqueia a geração de URL de upload para economizar IA e Storage.
    2. Caso seja inédito, sanitiza e verifica colisão de nomes com `novos_diarios/` e `diarios_processados/`
       (`_2.pdf`, `_3.pdf`) e retorna a URL de upload (`PUT`).
    """
    use_s3 = (not settings.USE_LOCAL_STORAGE) or bool(
        getattr(settings, "FORCE_S3_DIRECT_UPLOAD", False)
    )
    input_prefix = settings.SUPABASE_INPUT_PREFIX.strip("/")

    s3_client = None
    bucket_name = settings.SUPABASE_STORAGE_BUCKET

    if use_s3:
        s3_client = _obter_s3_client()
        nomes_ocupados = _listar_nomes_existentes_s3(s3_client, bucket_name)
    else:
        nomes_ocupados = _listar_nomes_existentes_local()

    hashes_vistos_no_lote: Dict[str, str] = {}
    instrucoes_upload: List[Dict[str, Any]] = []

    for item in filenames:
        if isinstance(item, dict):
            original_name = str(item.get("filename") or item.get("name") or "diario.pdf")
            sha256_hash = str(item.get("sha256") or "").strip().lower()
        else:
            original_name = str(item)
            sha256_hash = ""

        # 1. Verificação Anti-Duplicata por Conteúdo (SHA-256)
        if sha256_hash:
            if sha256_hash in hashes_vistos_no_lote:
                arq_lote = hashes_vistos_no_lote[sha256_hash]
                instrucoes_upload.append(
                    {
                        "nome_original": original_name,
                        "nome_final": original_name,
                        "foi_renomeado": False,
                        "duplicado": True,
                        "arquivo_original_duplicado": arq_lote,
                        "motivo_duplicado": f"Duplicado neste mesmo lote (idêntico a '{arq_lote}')",
                        "object_key": "",
                        "upload_url": "",
                        "method": "SKIP",
                        "modo_destino": "Bloqueado (Duplicata no Lote)",
                    }
                )
                continue

            dup_info = verificar_duplicidade_por_hash(sha256_arquivo=sha256_hash)
            if dup_info is not None:
                arq_orig = dup_info["arquivo_original"]
                instrucoes_upload.append(
                    {
                        "nome_original": original_name,
                        "nome_final": original_name,
                        "foi_renomeado": False,
                        "duplicado": True,
                        "arquivo_original_duplicado": arq_orig,
                        "motivo_duplicado": f"Diário já existente ('{arq_orig}' - {dup_info['motivo']})",
                        "object_key": "",
                        "upload_url": "",
                        "method": "SKIP",
                        "modo_destino": "Bloqueado (Diário Já Existente)",
                    }
                )
                continue

            hashes_vistos_no_lote[sha256_hash] = original_name

        # 2. Verificação Anti-Colisão de Nome (_2.pdf, _3.pdf)
        nome_limpo = sanitizar_nome_pdf(original_name)
        nome_final, foi_renomeado = _escolher_nome_sem_colisao(
            nome_limpo, nomes_ocupados
        )
        object_key = f"{input_prefix}/{nome_final}"

        if use_s3 and s3_client is not None:
            upload_url = s3_client.generate_presigned_url(
                ClientMethod="put_object",
                Params={
                    "Bucket": bucket_name,
                    "Key": object_key,
                    "ContentType": "application/pdf",
                },
                ExpiresIn=3600,
            )
            modo_destino = "S3 Presigned Direct Upload (Supabase Storage / S3)"
        else:
            encoded_name = quote(nome_final)
            upload_url = f"/licitacoes/upload/stream-local/{encoded_name}"
            modo_destino = "Direct Stream Local Storage (novos_diarios/)"

        instrucoes_upload.append(
            {
                "nome_original": original_name,
                "nome_final": nome_final,
                "foi_renomeado": foi_renomeado,
                "duplicado": False,
                "arquivo_original_duplicado": "",
                "motivo_duplicado": "",
                "object_key": object_key,
                "upload_url": upload_url,
                "method": "PUT",
                "modo_destino": modo_destino,
            }
        )

    return instrucoes_upload

