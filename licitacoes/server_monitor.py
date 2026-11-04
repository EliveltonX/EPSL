"""
Módulo de Monitoramento de Recursos do Servidor, Disco Local, Banco de Dados e Supabase Storage.
Fornece telemetria em tempo real (estilo Gerenciador de Tarefas / Dashboard de Servidor) para `/licitacoes/servidor/`.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import psutil
from django.conf import settings
from django.db import connection
from django.db.models import Count

from licitacoes.models import Licitacao
from licitacoes.storage_upload import _obter_s3_client

# Cache leve em memória para evitar listagens pesadas no S3 a cada 3 segundos de polling
_STORAGE_CACHE: Dict[str, Any] = {
    "timestamp": 0.0,
    "data": None,
}
STORAGE_CACHE_TTL_SECONDS = 12.0


def formatar_bytes(num_bytes: int | float) -> str:
    """Converte bytes em representação humana (B, KB, MB, GB, TB)."""
    valor = float(max(num_bytes, 0))
    for unidade in ("B", "KB", "MB", "GB", "TB"):
        if valor < 1024.0 or unidade == "TB":
            if unidade == "B":
                return f"{int(valor)} B"
            return f"{valor:.2f} {unidade}"
        valor /= 1024.0
    return f"{valor:.2f} TB"


def formatar_duracao(segundos: float) -> str:
    """Formata segundos em `Xd Xh Xm Xs`."""
    total = int(max(segundos, 0))
    dias, resto = divmod(total, 86400)
    horas, resto = divmod(resto, 3600)
    minutos, segs = divmod(resto, 60)
    partes = []
    if dias > 0:
        partes.append(f"{dias}d")
    if horas > 0 or dias > 0:
        partes.append(f"{horas}h")
    if minutos > 0 or horas > 0 or dias > 0:
        partes.append(f"{minutos}m")
    partes.append(f"{segs}s")
    return " ".join(partes)


def coletar_metricas_hardware() -> Dict[str, Any]:
    """Coleta CPU, núcleos, RAM, Swap, Rede, Uptime e Top Processos via `psutil`."""
    agora = time.time()
    boot_time = psutil.boot_time()
    uptime_sistema = agora - boot_time

    proc_atual = psutil.Process(os.getpid())
    uptime_processo = agora - proc_atual.create_time()
    mem_proc_rss = proc_atual.memory_info().rss

    # CPU
    cpu_por_nucleo = psutil.cpu_percent(interval=0.12, percpu=True)
    cpu_total = (
        round(sum(cpu_por_nucleo) / len(cpu_por_nucleo), 1)
        if cpu_por_nucleo
        else psutil.cpu_percent(interval=None)
    )
    freq = psutil.cpu_freq()
    freq_mhz = round(freq.current, 0) if freq else 0

    try:
        load_1, load_5, load_15 = os.getloadavg()
    except Exception:
        load_1, load_5, load_15 = 0.0, 0.0, 0.0

    # Memória RAM & Swap
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()

    # Rede
    net = psutil.net_io_counters()

    # Top Processos por Memória / CPU
    processos_lista: List[Dict[str, Any]] = []
    for p in psutil.process_iter(
        ["pid", "name", "username", "cpu_percent", "memory_percent", "memory_info", "status"]
    ):
        try:
            info = p.info
            rss_bytes = info["memory_info"].rss if info.get("memory_info") else 0
            processos_lista.append(
                {
                    "pid": info["pid"],
                    "name": info.get("name") or "processo",
                    "username": info.get("username") or "-",
                    "cpu_percent": round(float(info.get("cpu_percent") or 0.0), 1),
                    "memory_percent": round(float(info.get("memory_percent") or 0.0), 1),
                    "rss_bytes": rss_bytes,
                    "rss_human": formatar_bytes(rss_bytes),
                    "status": info.get("status") or "running",
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    processos_lista.sort(
        key=lambda x: (x["cpu_percent"], x["rss_bytes"]), reverse=True
    )
    top_processos = processos_lista[:10]

    return {
        "sistema": {
            "hostname": socket.gethostname(),
            "os": f"{platform.system()} {platform.release()}",
            "arquitetura": platform.machine(),
            "python_version": platform.python_version(),
            "modo_app": "DEPLOY (Produção Coolify)" if settings.IS_DEPLOY else "TESTE LOCAL",
            "uptime_sistema_seg": int(uptime_sistema),
            "uptime_sistema_human": formatar_duracao(uptime_sistema),
            "uptime_django_human": formatar_duracao(uptime_processo),
            "django_rss_human": formatar_bytes(mem_proc_rss),
            "load_avg": [round(load_1, 2), round(load_5, 2), round(load_15, 2)],
        },
        "cpu": {
            "percent_total": cpu_total,
            "per_core": [round(c, 1) for c in cpu_por_nucleo],
            "nucleos_fisicos": psutil.cpu_count(logical=False) or 1,
            "nucleos_logicos": psutil.cpu_count(logical=True) or 1,
            "freq_mhz": freq_mhz,
        },
        "memoria": {
            "percent": round(vm.percent, 1),
            "total_bytes": vm.total,
            "total_human": formatar_bytes(vm.total),
            "usado_bytes": vm.used,
            "usado_human": formatar_bytes(vm.used),
            "disponivel_bytes": vm.available,
            "disponivel_human": formatar_bytes(vm.available),
            "cached_human": formatar_bytes(getattr(vm, "cached", 0)),
            "swap_percent": round(sw.percent, 1),
            "swap_usado_human": formatar_bytes(sw.used),
            "swap_total_human": formatar_bytes(sw.total),
        },
        "rede": {
            "bytes_enviados": net.bytes_sent,
            "bytes_enviados_human": formatar_bytes(net.bytes_sent),
            "bytes_recebidos": net.bytes_recv,
            "bytes_recebidos_human": formatar_bytes(net.bytes_recv),
            "pacotes_enviados": net.packets_sent,
            "pacotes_recebidos": net.packets_recv,
        },
        "top_processos": top_processos,
    }


def coletar_discos_servidor() -> List[Dict[str, Any]]:
    """Lista o armazenamento das partições montadas no servidor + pasta raiz do projeto."""
    discos: List[Dict[str, Any]] = []
    vistos = set()

    # Garante sempre monitorar o disco onde o projeto EPSL está instalado
    caminhos_prioritarios = [
        ("Disco do Projeto EPSL", str(settings.BASE_DIR)),
        ("Raiz do Servidor (/)", "/"),
    ]
    for rotulo, caminho in caminhos_prioritarios:
        try:
            uso = shutil.disk_usage(caminho)
            chave = (uso.total, uso.used)
            vistos.add(chave)
            pct = round((uso.used / uso.total) * 100, 1) if uso.total else 0.0
            discos.append(
                {
                    "dispositivo": rotulo,
                    "ponto_montagem": caminho,
                    "fstype": "sistema",
                    "total_bytes": uso.total,
                    "total_human": formatar_bytes(uso.total),
                    "usado_bytes": uso.used,
                    "usado_human": formatar_bytes(uso.used),
                    "livre_bytes": uso.free,
                    "livre_human": formatar_bytes(uso.free),
                    "percent": pct,
                }
            )
        except Exception:
            continue

    for part in psutil.disk_partitions(all=False):
        if "loop" in part.device or part.fstype in ("squashfs", "tmpfs", "devtmpfs"):
            continue
        try:
            uso = psutil.disk_usage(part.mountpoint)
            chave = (uso.total, uso.used)
            if chave in vistos:
                continue
            vistos.add(chave)
            discos.append(
                {
                    "dispositivo": part.device,
                    "ponto_montagem": part.mountpoint,
                    "fstype": part.fstype,
                    "total_bytes": uso.total,
                    "total_human": formatar_bytes(uso.total),
                    "usado_bytes": uso.used,
                    "usado_human": formatar_bytes(uso.used),
                    "livre_bytes": uso.free,
                    "livre_human": formatar_bytes(uso.free),
                    "percent": round(uso.percent, 1),
                }
            )
        except Exception:
            continue

    return discos


def coletar_metricas_banco() -> Dict[str, Any]:
    """Coleta tamanho do banco de dados (PostgreSQL Supabase ou SQLite) e estatísticas de `Licitacao`."""
    engine = settings.DATABASES["default"]["ENGINE"]
    db_name = str(settings.DATABASES["default"]["NAME"])
    tamanho_bytes = 0
    tamanho_human = "N/D"
    status_conexao = "Conectado"

    try:
        if "sqlite3" in engine:
            db_path = Path(db_name)
            if db_path.exists():
                tamanho_bytes = db_path.stat().st_size
                tamanho_human = formatar_bytes(tamanho_bytes)
        elif "postgresql" in engine:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_database_size(current_database());")
                row = cursor.fetchone()
                if row and row[0] is not None:
                    tamanho_bytes = int(row[0])
                    tamanho_human = formatar_bytes(tamanho_bytes)
        elif "mysql" in engine:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT SUM(data_length + index_length) FROM information_schema.tables WHERE table_schema = DATABASE();"
                )
                row = cursor.fetchone()
                if row and row[0] is not None:
                    tamanho_bytes = int(row[0])
                    tamanho_human = formatar_bytes(tamanho_bytes)
    except Exception as exc:
        status_conexao = f"Aviso: {exc}"

    total_licitacoes = Licitacao.objects.count()
    por_tag_qs = (
        Licitacao.objects.values("tag_analise")
        .annotate(total=Count("id"))
        .order_by("-total")
    )
    mapa_tags = {
        "favorito": 0,
        "em_analise": 0,
        "sem_conformidade": 0,
        "ignorado": 0,
        "sem_marcacao": 0,
    }
    for row in por_tag_qs:
        chave = row["tag_analise"] or "sem_marcacao"
        mapa_tags[chave] = row["total"]

    return {
        "engine": engine.split(".")[-1].upper(),
        "nome_banco": Path(db_name).name if "sqlite3" in engine else db_name,
        "status": status_conexao,
        "tamanho_bytes": tamanho_bytes,
        "tamanho_human": tamanho_human,
        "total_licitacoes": total_licitacoes,
        "por_tag": mapa_tags,
    }


def coletar_metricas_storage(forcar_atualizacao: bool = False) -> Dict[str, Any]:
    """
    Inspeciona o armazenamento interno de PDFs:
    - Em modo `deploy` (ou S3 ativo): consulta o bucket do Supabase Storage (`novos_diarios/` e `diarios_processados/`).
    - Em modo `test`: analisa a pasta `storage_local/` (`novos_diarios/` e `diarios_processados/`).
    """
    agora = time.time()
    if (
        not forcar_atualizacao
        and _STORAGE_CACHE["data"] is not None
        and (agora - _STORAGE_CACHE["timestamp"]) < STORAGE_CACHE_TTL_SECONDS
    ):
        return _STORAGE_CACHE["data"]

    use_s3 = (not settings.USE_LOCAL_STORAGE) or bool(
        getattr(settings, "FORCE_S3_DIRECT_UPLOAD", False)
    )
    input_prefix = settings.SUPABASE_INPUT_PREFIX.strip("/")
    processed_prefix = settings.SUPABASE_PROCESSED_PREFIX.strip("/")

    novos_qtd = 0
    novos_bytes = 0
    processados_qtd = 0
    processados_bytes = 0
    arquivos_recentes: List[Dict[str, Any]] = []
    por_mes: Dict[str, Dict[str, int]] = {}
    status_storage = "Online"
    tipo_storage = (
        f"Supabase Storage S3 (Bucket: {settings.SUPABASE_STORAGE_BUCKET})"
        if use_s3
        else f"Storage Local ({settings.LOCAL_STORAGE_ROOT})"
    )

    if use_s3:
        try:
            s3 = _obter_s3_client()
            bucket = settings.SUPABASE_STORAGE_BUCKET
            paginator = s3.get_paginator("list_objects_v2")
            todos_objetos = []

            for prefix in (f"{input_prefix}/", f"{processed_prefix}/"):
                for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                    for obj in page.get("Contents", []):
                        key: str = obj["Key"]
                        if not key.lower().endswith(".pdf"):
                            continue
                        size = int(obj.get("Size", 0))
                        last_mod = obj.get("LastModified")
                        todos_objetos.append((key, size, last_mod))

                        if key.startswith(f"{input_prefix}/"):
                            novos_qtd += 1
                            novos_bytes += size
                        elif key.startswith(f"{processed_prefix}/"):
                            processados_qtd += 1
                            processados_bytes += size
                            partes = key.split("/")
                            mes_ref = (
                                f"{partes[1]}/{partes[2]}"
                                if len(partes) >= 4
                                else "Outros"
                            )
                            if mes_ref not in por_mes:
                                por_mes[mes_ref] = {"qtd": 0, "bytes": 0}
                            por_mes[mes_ref]["qtd"] += 1
                            por_mes[mes_ref]["bytes"] += size

            todos_objetos.sort(
                key=lambda x: x[2].timestamp() if x[2] else 0.0, reverse=True
            )
            for key, size, last_mod in todos_objetos[:12]:
                pasta = (
                    "Fila (novos_diarios)"
                    if key.startswith(f"{input_prefix}/")
                    else "Processados"
                )
                arquivos_recentes.append(
                    {
                        "nome": key.split("/")[-1],
                        "caminho": key,
                        "pasta": pasta,
                        "tamanho_human": formatar_bytes(size),
                        "modificado": (
                            last_mod.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M")
                            if last_mod
                            else "-"
                        ),
                    }
                )
        except Exception as exc:
            status_storage = f"Erro ao consultar S3: {exc}"
    else:
        root = Path(settings.LOCAL_STORAGE_ROOT)
        dir_novos = root / input_prefix
        dir_proc = root / processed_prefix
        todos_arquivos = []

        if dir_novos.exists():
            for p in dir_novos.rglob("*.pdf"):
                if p.is_file():
                    st = p.stat()
                    novos_qtd += 1
                    novos_bytes += st.st_size
                    todos_arquivos.append((p, st.st_size, st.st_mtime, "Fila (novos_diarios)"))

        if dir_proc.exists():
            for p in dir_proc.rglob("*.pdf"):
                if p.is_file():
                    st = p.stat()
                    processados_qtd += 1
                    processados_bytes += st.st_size
                    rel = p.relative_to(dir_proc).parts
                    mes_ref = f"{rel[0]}/{rel[1]}" if len(rel) >= 3 else "Geral"
                    if mes_ref not in por_mes:
                        por_mes[mes_ref] = {"qtd": 0, "bytes": 0}
                    por_mes[mes_ref]["qtd"] += 1
                    por_mes[mes_ref]["bytes"] += st.st_size
                    todos_arquivos.append((p, st.st_size, st.st_mtime, f"Processados ({mes_ref})"))

        todos_arquivos.sort(key=lambda x: x[2], reverse=True)
        for p, size, mtime, pasta in todos_arquivos[:12]:
            arquivos_recentes.append(
                {
                    "nome": p.name,
                    "caminho": str(p.relative_to(root)) if root in p.parents else p.name,
                    "pasta": pasta,
                    "tamanho_human": formatar_bytes(size),
                    "modificado": datetime.fromtimestamp(mtime).strftime("%d/%m/%Y %H:%M"),
                }
            )

    total_pdfs = novos_qtd + processados_qtd
    total_bytes = novos_bytes + processados_bytes

    resumo_meses = [
        {
            "periodo": periodo,
            "qtd": info["qtd"],
            "tamanho_human": formatar_bytes(info["bytes"]),
        }
        for periodo, info in sorted(por_mes.items(), reverse=True)
    ]

    resultado = {
        "tipo_storage": tipo_storage,
        "status": status_storage,
        "novos_diarios": {
            "qtd": novos_qtd,
            "bytes": novos_bytes,
            "human": formatar_bytes(novos_bytes),
        },
        "diarios_processados": {
            "qtd": processados_qtd,
            "bytes": processados_bytes,
            "human": formatar_bytes(processados_bytes),
        },
        "total": {
            "qtd": total_pdfs,
            "bytes": total_bytes,
            "human": formatar_bytes(total_bytes),
        },
        "por_mes": resumo_meses,
        "arquivos_recentes": arquivos_recentes,
    }

    _STORAGE_CACHE["timestamp"] = agora
    _STORAGE_CACHE["data"] = resultado
    return resultado


def obter_snapshot_completo(forcar_storage: bool = False) -> Dict[str, Any]:
    """Retorna o pacote completo de telemetria para a Dashboard e API JSON."""
    return {
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "hardware": coletar_metricas_hardware(),
        "discos": coletar_discos_servidor(),
        "banco": coletar_metricas_banco(),
        "storage": coletar_metricas_storage(forcar_atualizacao=forcar_storage),
    }

