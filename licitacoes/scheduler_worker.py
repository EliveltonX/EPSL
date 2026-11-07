"""
Agendador Automático em Background (Cron Interno a cada 10 Minutos) para o Projeto EPSL.

Objetivo:
- Evitar picos agressivos de RPM/RPD no Google AI Studio.
- A cada `PIPELINE_CRON_INTERVAL_MINUTES` (padrão: 10 min), verifica silenciosamente se há
  novos artigos PDF aguardando análise em `novos_diarios/` (local ou Supabase Storage S3).
- Caso existam artigos na fila, processa um lote controlado (`PIPELINE_MAX_GEMINI_FILES_PER_CRON`,
  padrão: 3 PDFs com match no Regex empacotados em um único Super-Batch de contexto 250K TPM),
  garantindo consumo mínimo de recursos mesmo que uma fila grande leve de 1 a 2 horas para zerar.
- Utiliza lock POSIX (`fcntl.flock`) em `/tmp/epsl_scheduler_daemon.lock` para que apenas 1 worker
  do Gunicorn execute o loop do agendador.
"""

from __future__ import annotations

import fcntl
import io
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.core.management import call_command
from django.db import close_old_connections

from licitacoes.pipeline_progress import (
    atualizar_status_scheduler,
    obter_snapshot_progresso,
)
from licitacoes.storage_upload import _obter_s3_client, s3_credenciais_validas

logger = logging.getLogger(__name__)

_SCHEDULER_STARTED = False
_SCHEDULER_LOCK_FD: Optional[io.TextIOWrapper] = None
_DAEMON_LOCK_PATH = Path(tempfile.gettempdir()) / "epsl_scheduler_daemon.lock"


def _contar_pdfs_aguardando_fila() -> int:
    """Verifica rapidamente quantos PDFs estão aguardando em `novos_diarios/` sem iniciar o pipeline."""
    input_prefix = settings.SUPABASE_INPUT_PREFIX.strip("/")
    if settings.USE_LOCAL_STORAGE:
        pasta = Path(settings.LOCAL_STORAGE_ROOT) / input_prefix
        if not pasta.exists():
            return 0
        return sum(
            1 for p in pasta.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"
        )

    if not s3_credenciais_validas():
        return 0

    try:
        s3 = _obter_s3_client()
        bucket = settings.SUPABASE_STORAGE_BUCKET
        paginator = s3.get_paginator("list_objects_v2")
        qtd = 0
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{input_prefix}/"):
            for obj in page.get("Contents", []):
                key: str = obj.get("Key", "")
                if key.lower().endswith(".pdf") and key != f"{input_prefix}/":
                    qtd += 1
        return qtd
    except Exception:
        return 0


def _loop_agendador_10min() -> None:
    """Loop contínuo em background que acorda a cada 10 minutos para processar novos diários de forma econômica."""
    intervalo_min = max(1, int(getattr(settings, "PIPELINE_CRON_INTERVAL_MINUTES", 10)))
    intervalo_seg = intervalo_min * 60
    max_files_ciclo = max(
        1, int(getattr(settings, "PIPELINE_MAX_GEMINI_FILES_PER_CRON", 3))
    )

    # Primeira verificação suave 45 segundos após o boot do servidor
    espera_inicial = 45
    atualizar_status_scheduler(
        ultimo_resumo=(
            f"Agendador ativo (ciclo de {intervalo_min} min | máx. {max_files_ciclo} PDFs IA/ciclo em Super-Batch)."
        ),
        proximo_ciclo_ts=time.time() + espera_inicial,
        incrementar_ciclo=False,
    )
    time.sleep(espera_inicial)

    while True:
        try:
            snap = obter_snapshot_progresso()
            if snap.get("em_execucao"):
                prox_ts = time.time() + intervalo_seg
                atualizar_status_scheduler(
                    ultimo_resumo="Ciclo adiado: já existe uma análise em andamento.",
                    proximo_ciclo_ts=prox_ts,
                    incrementar_ciclo=False,
                )
            else:
                pendentes = _contar_pdfs_aguardando_fila()
                if pendentes <= 0:
                    prox_ts = time.time() + intervalo_seg
                    atualizar_status_scheduler(
                        ultimo_resumo="Fila verificada: 0 artigos aguardando em novos_diarios/ (0 req IA).",
                        proximo_ciclo_ts=prox_ts,
                        incrementar_ciclo=True,
                    )
                else:
                    atualizar_status_scheduler(
                        ultimo_resumo=(
                            f"Executando ciclo automático: {pendentes} PDF(s) na fila "
                            f"(processando até {max_files_ciclo} com IA em Super-Batch)..."
                        ),
                        proximo_ciclo_ts=time.time() + intervalo_seg,
                        incrementar_ciclo=True,
                    )
                    close_old_connections()
                    out = io.StringIO()
                    err = io.StringIO()
                    try:
                        call_command(
                            "processar_diarios",
                            max_files=max_files_ciclo,
                            origem=f"Agendador Automático ({intervalo_min} min)",
                            stdout=out,
                            stderr=err,
                        )
                    finally:
                        close_old_connections()

                    snap_pos = obter_snapshot_progresso()
                    restantes = _contar_pdfs_aguardando_fila()
                    prox_ts = time.time() + intervalo_seg
                    atualizar_status_scheduler(
                        ultimo_resumo=(
                            f"Último ciclo concluído: {snap_pos.get('arquivos_processados', 0)} processado(s), "
                            f"{snap_pos.get('licitacoes_extraidas', 0)} licitação(ões), "
                            f"{snap_pos.get('superbatches_enviados', 0)} chamada(s) IA. "
                            f"Restam {restantes} na fila."
                        ),
                        proximo_ciclo_ts=prox_ts,
                        incrementar_ciclo=False,
                    )
        except Exception as exc:
            logger.warning("Aviso no ciclo do agendador de 10 min: %s", exc)
            atualizar_status_scheduler(
                ultimo_resumo=f"Aviso no último ciclo: {str(exc)[:120]}",
                proximo_ciclo_ts=time.time() + intervalo_seg,
                incrementar_ciclo=False,
            )

        time.sleep(intervalo_seg)


def iniciar_agendador_se_habilitado() -> None:
    """
    Inicia a thread daemon do agendador de 10 minutos caso `PIPELINE_CRON_ENABLED=True`
    e o processo atual não seja um comando administrativo efêmero (`migrate`, `collectstatic`, etc.).
    """
    global _SCHEDULER_STARTED, _SCHEDULER_LOCK_FD

    if _SCHEDULER_STARTED:
        return

    if not getattr(settings, "PIPELINE_CRON_ENABLED", True):
        return

    # Evita iniciar o agendador durante comandos CLI rápidos
    comandos_ignorados = {
        "collectstatic",
        "migrate",
        "makemigrations",
        "check",
        "shell",
        "test",
        "createsuperuser",
        "processar_diarios",
    }
    if any(cmd in sys.argv for cmd in comandos_ignorados):
        return

    # No runserver com autoreload do Django, inicia apenas no processo filho ativo
    if "runserver" in sys.argv and os.environ.get("RUN_MAIN") != "true":
        return

    try:
        fd = open(_DAEMON_LOCK_PATH, "a+", encoding="utf-8")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _SCHEDULER_LOCK_FD = fd
    except (BlockingIOError, OSError):
        # Outro worker do Gunicorn já é o líder do agendador
        return

    _SCHEDULER_STARTED = True
    t = threading.Thread(
        target=_loop_agendador_10min,
        daemon=True,
        name="epsl-cron-10min-scheduler",
    )
    t.start()

