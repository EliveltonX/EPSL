"""
Gerenciador de Progresso em Tempo Real da Pipeline (`processar_diarios`),
Histórico de Erros de Análise, Estado do Agendador (Cron 10 min) e
Balanceador Ultra-Econômico de Cotas Google Gemini (RPM / TPM / RPD).

Sincroniza o estado em `/tmp/epsl_pipeline_state.json` (com lock POSIX `fcntl`)
para que múltiplos workers do Gunicorn em produção compartilhem exatamente a mesma
barra de progresso, contadores de cota, fila de erros e relógio do agendador.

Baseado na tabela oficial de limites (`Docs_Vault/Arquitetura/ANOTAÇÕES.md`):
- gemini-3.5-flash-lite : 15 RPM | 250K TPM | 500 RPD
- gemini-3.1-flash-lite : 15 RPM | 250K TPM | 500 RPD
- gemini-3.6-flash      :  5 RPM | 250K TPM |  20 RPD
- gemini-3-flash-preview:  5 RPM | 250K TPM |  20 RPD
- gemini-3.8-flash      :  5 RPM | 250K TPM |  20 RPD
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from django.conf import settings

_LOCK = threading.Lock()
_STATE_FILE = Path(tempfile.gettempdir()) / "epsl_pipeline_state.json"
_LOCK_FILE = Path(tempfile.gettempdir()) / "epsl_pipeline_state.lock"


def _estado_padrao() -> Dict[str, Any]:
    intervalo = int(getattr(settings, "PIPELINE_CRON_INTERVAL_MINUTES", 10))
    max_por_ciclo = int(getattr(settings, "PIPELINE_MAX_GEMINI_FILES_PER_CRON", 3))
    cron_ativo = bool(getattr(settings, "PIPELINE_CRON_ENABLED", True))
    return {
        "em_execucao": False,
        "concluido": False,
        "origem_execucao": "Aguardando",
        "iniciado_em": "",
        "iniciado_ts": 0.0,
        "finalizado_em": "",
        "total_arquivos": 0,
        "arquivos_processados": 0,
        "percentual": 0,
        "arquivo_atual": "",
        "etapa_atual": "Ocioso — aguardando próximo ciclo ou acionamento manual.",
        "modelo_ativo": getattr(settings, "GEMINI_MODEL", "gemini-3.5-flash-lite"),
        "licitacoes_extraidas": 0,
        "duplicados_bloqueados": 0,
        "sem_match_regex": 0,
        "superbatches_enviados": 0,
        "economia_requisicoes_ia": 0,
        "erros_qtd": 0,
        "logs": [],
        "erros": [],
        # Mapa persistente de arquivos que falharam: {nome_arquivo: {nome_arquivo, object_key, erro, horario, tentativas}}
        "arquivos_com_erro": {},
        # Controle de cotas por dia/minuto compartilhado entre processos
        "data_cotas": datetime.date.today().isoformat(),
        "timestamps_rpm": {},
        "contador_rpd": {},
        "cooldown_ate": {},
        "ultima_chamada_gemini_ts": 0.0,
        # Estado do Agendador Automático (Cron a cada 10 minutos)
        "scheduler": {
            "ativo": cron_ativo,
            "intervalo_minutos": intervalo,
            "max_pdfs_por_ciclo": max_por_ciclo,
            "ultimo_ciclo_em": "Ainda não executado",
            "proximo_ciclo_ts": time.time() + (intervalo * 60),
            "ultimo_resumo": "Agendador iniciado — monitorando novos_diarios/ a cada 10 min.",
            "total_ciclos": 0,
        },
    }


def _ler_estado_disco() -> Dict[str, Any]:
    padrao = _estado_padrao()
    if not _STATE_FILE.exists():
        return padrao
    try:
        dados = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(dados, dict):
            return padrao
        for k, v in padrao.items():
            if k not in dados:
                dados[k] = v
        if isinstance(dados.get("scheduler"), dict):
            for sk, sv in padrao["scheduler"].items():
                dados["scheduler"].setdefault(sk, sv)
        else:
            dados["scheduler"] = padrao["scheduler"]

        # Se em_execucao ficou preso há mais de 45 minutos (ex: crash de container), destrava automaticamente
        if dados.get("em_execucao") and dados.get("iniciado_ts"):
            if (time.time() - float(dados["iniciado_ts"])) > 2700:
                dados["em_execucao"] = False
                dados["concluido"] = True
                dados["etapa_atual"] = "Execução anterior expirada por timeout (destravada)."

        # Reseta contadores RPD quando vira o dia
        hoje_iso = datetime.date.today().isoformat()
        if dados.get("data_cotas") != hoje_iso:
            dados["data_cotas"] = hoje_iso
            dados["contador_rpd"] = {}
            dados["cooldown_ate"] = {}

        return dados
    except Exception:
        return padrao


def _salvar_estado_disco(dados: Dict[str, Any]) -> None:
    try:
        dados["logs"] = list(dados.get("logs", [])[-160:])
        dados["erros"] = list(dados.get("erros", [])[-60:])
        tmp_path = _STATE_FILE.with_suffix(f".tmp.{os.getpid()}")
        tmp_path.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(_STATE_FILE)
    except Exception:
        pass


class _ContextoEstadoAtomico:
    """Context manager que adquire lock de thread e lock de arquivo (`fcntl`) para leitura/escrita atômica."""

    def __enter__(self) -> Dict[str, Any]:
        _LOCK.acquire()
        self._lock_fd = open(_LOCK_FILE, "a+", encoding="utf-8")
        try:
            fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        self.estado = _ler_estado_disco()
        return self.estado

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if exc_type is None:
                _salvar_estado_disco(self.estado)
        finally:
            try:
                fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._lock_fd.close()
            except Exception:
                pass
            _LOCK.release()


def _montar_tabela_cotas(estado: Dict[str, Any]) -> List[Dict[str, Any]]:
    agora = time.time()
    quotas_cfg = getattr(
        settings,
        "GEMINI_MODELS_QUOTAS",
        [
            {"model": "gemini-3.5-flash-lite", "rpm": 15, "tpm": 250_000, "rpd": 500},
            {"model": "gemini-3.1-flash-lite", "rpm": 15, "tpm": 250_000, "rpd": 500},
            {"model": "gemini-3.6-flash", "rpm": 5, "tpm": 250_000, "rpd": 20},
            {"model": "gemini-3-flash-preview", "rpm": 5, "tpm": 250_000, "rpd": 20},
            {"model": "gemini-3.8-flash", "rpm": 5, "tpm": 250_000, "rpd": 20},
        ],
    )
    ts_rpm = estado.setdefault("timestamps_rpm", {})
    cnt_rpd = estado.setdefault("contador_rpd", {})
    cd_ate = estado.setdefault("cooldown_ate", {})

    resultado = []
    for item in quotas_cfg:
        m = item["model"]
        hist = [float(t) for t in ts_rpm.get(m, []) if (agora - float(t)) < 60.0]
        ts_rpm[m] = hist
        rpd_usado = int(cnt_rpd.get(m, 0))
        cooldown_restante = max(0.0, float(cd_ate.get(m, 0.0)) - agora)
        resultado.append(
            {
                "model": m,
                "rpm_usado": len(hist),
                "rpm_limite": item["rpm"],
                "tpm_limite": item["tpm"],
                "rpd_usado": rpd_usado,
                "rpd_limite": item["rpd"],
                "em_cooldown_seg": round(cooldown_restante, 1),
                "disponivel": (
                    len(hist) < item["rpm"]
                    and rpd_usado < item["rpd"]
                    and cooldown_restante <= 0.0
                ),
            }
        )
    return resultado


def obter_tabela_cotas() -> List[Dict[str, Any]]:
    """Retorna a lista de modelos configurados com uso atual de RPM (últimos 60s) e RPD (hoje)."""
    with _ContextoEstadoAtomico() as estado:
        return _montar_tabela_cotas(estado)


def ordenar_modelos_para_chamada() -> List[Dict[str, Any]]:
    """
    Retorna os modelos ordenados priorizando:
    1. Modelos com cota RPM e RPD livre agora e sem cooldown.
    2. Balanceamento ativo entre os dois modelos de 500 RPD (`gemini-3.5-flash-lite` e `gemini-3.1-flash-lite`)
       escolhendo primeiro aquele com menor uso no minuto/dia atual.
    """
    with _ContextoEstadoAtomico() as estado:
        status_modelos = _montar_tabela_cotas(estado)

    disponiveis = [m for m in status_modelos if m["disponivel"]]
    # Entre modelos de mesma capacidade máxima (ex: os dois flash-lite de 500 RPD),
    # prioriza o que tiver menor fração de RPD/RPM usada no momento para distribuir a carga igualmente
    disponiveis.sort(
        key=lambda m: (
            -m["rpd_limite"],
            m["rpm_usado"] / max(m["rpm_limite"], 1),
            m["rpd_usado"] / max(m["rpd_limite"], 1),
        )
    )
    indisponiveis = [
        m for m in status_modelos if not m["disponivel"] and m["rpd_usado"] < m["rpd_limite"]
    ]
    return disponiveis + indisponiveis


def aguardar_slot_rpm_se_necessario(model_name: str, rpm_limite: int) -> float:
    """
    Calcula quantos segundos aguardar antes de disparar a próxima requisição Gemini,
    combinando:
    1. O espaçamento mínimo econômico entre chamadas (`GEMINI_MIN_INTERVAL_BETWEEN_CALLS_SEC`, padrão 20s).
    2. A janela deslizante de 60 segundos do modelo (`rpm_limite`).
    """
    agora = time.time()
    intervalo_min_global = float(
        getattr(settings, "GEMINI_MIN_INTERVAL_BETWEEN_CALLS_SEC", 20.0)
    )
    with _ContextoEstadoAtomico() as estado:
        ts_rpm = estado.setdefault("timestamps_rpm", {})
        hist = [float(t) for t in ts_rpm.get(model_name, []) if (agora - float(t)) < 60.0]
        ts_rpm[model_name] = hist

        ultima_global = float(estado.get("ultima_chamada_gemini_ts") or 0.0)
        espera_cadencia = (
            max(0.0, intervalo_min_global - (agora - ultima_global))
            if ultima_global > 0
            else 0.0
        )

        espera_rpm = 0.0
        if len(hist) >= rpm_limite:
            mais_antigo = min(hist)
            espera_rpm = max(0.5, 60.5 - (agora - mais_antigo))

        return round(max(espera_cadencia, espera_rpm), 1)


def registrar_uso_modelo(model_name: str, arquivos_no_lote: int = 1) -> None:
    """
    Contabiliza 1 requisição realizada com sucesso no RPM (janela 60s) e RPD (dia),
    além de registrar quantas chamadas de IA foram economizadas pelo Super-Batching.
    """
    agora = time.time()
    with _ContextoEstadoAtomico() as estado:
        ts_rpm = estado.setdefault("timestamps_rpm", {})
        cnt_rpd = estado.setdefault("contador_rpd", {})
        hist = [float(t) for t in ts_rpm.get(model_name, []) if (agora - float(t)) < 60.0]
        hist.append(agora)
        ts_rpm[model_name] = hist
        cnt_rpd[model_name] = int(cnt_rpd.get(model_name, 0)) + 1
        estado["ultima_chamada_gemini_ts"] = agora
        estado["modelo_ativo"] = model_name
        estado["superbatches_enviados"] = int(estado.get("superbatches_enviados", 0)) + 1
        if arquivos_no_lote > 1:
            estado["economia_requisicoes_ia"] = int(
                estado.get("economia_requisicoes_ia", 0)
            ) + (arquivos_no_lote - 1)


def registrar_cooldown_modelo(model_name: str, segundos: float = 30.0) -> None:
    """Coloca um modelo em cooldown temporário quando retorna 429 (Quota) ou 503 (Sobrecarga)."""
    with _ContextoEstadoAtomico() as estado:
        cd_ate = estado.setdefault("cooldown_ate", {})
        cd_ate[model_name] = time.time() + segundos


# ==============================================================================
# Funções de Atualização da Barra de Carregamento, Erros e Agendador
# ==============================================================================
def iniciar_progresso(total_arquivos: int = 0, origem: str = "Manual / Web") -> None:
    with _ContextoEstadoAtomico() as estado:
        estado.update(
            {
                "em_execucao": True,
                "concluido": False,
                "origem_execucao": origem,
                "iniciado_em": datetime.datetime.now().strftime("%H:%M:%S"),
                "iniciado_ts": time.time(),
                "finalizado_em": "",
                "total_arquivos": total_arquivos,
                "arquivos_processados": 0,
                "percentual": 2 if total_arquivos == 0 else 5,
                "arquivo_atual": "Listando arquivos em novos_diarios/...",
                "etapa_atual": f"Iniciando ({origem}) — conectando ao Storage...",
                "modelo_ativo": getattr(settings, "GEMINI_MODEL", "gemini-3.5-flash-lite"),
                "licitacoes_extraidas": 0,
                "duplicados_bloqueados": 0,
                "sem_match_regex": 0,
                "superbatches_enviados": 0,
                "erros_qtd": 0,
                "logs": [],
                "erros": [],
            }
        )


def definir_total_arquivos(total: int) -> None:
    with _ContextoEstadoAtomico() as estado:
        estado["total_arquivos"] = total
        if total == 0:
            estado["percentual"] = 100
            estado["etapa_atual"] = "Nenhum arquivo PDF pendente na fila."


def atualizar_etapa_arquivo(
    indice_1_based: int,
    nome_arquivo: str,
    etapa: str,
) -> None:
    with _ContextoEstadoAtomico() as estado:
        total = max(int(estado.get("total_arquivos") or 1), 1)
        proc = int(estado.get("arquivos_processados") or 0)
        pct_base = int((max(indice_1_based - 1, proc) / total) * 100)
        estado["arquivo_atual"] = f"({indice_1_based}/{total}) {nome_arquivo}"
        estado["etapa_atual"] = etapa
        estado["percentual"] = max(5, min(95, pct_base + int(45 / total)))


def concluir_um_arquivo(
    nome_arquivo: str = "",
    licitacoes_adicionadas: int = 0,
    foi_duplicado: bool = False,
    sem_match_regex: bool = False,
    teve_erro: bool = False,
) -> None:
    with _ContextoEstadoAtomico() as estado:
        proc = int(estado.get("arquivos_processados") or 0) + 1
        total = max(int(estado.get("total_arquivos") or 1), proc)
        estado["arquivos_processados"] = proc
        estado["licitacoes_extraidas"] = (
            int(estado.get("licitacoes_extraidas") or 0) + licitacoes_adicionadas
        )
        if foi_duplicado:
            estado["duplicados_bloqueados"] = (
                int(estado.get("duplicados_bloqueados") or 0) + 1
            )
            estado["economia_requisicoes_ia"] = (
                int(estado.get("economia_requisicoes_ia") or 0) + 1
            )
        if sem_match_regex:
            estado["sem_match_regex"] = int(estado.get("sem_match_regex") or 0) + 1
            estado["economia_requisicoes_ia"] = (
                int(estado.get("economia_requisicoes_ia") or 0) + 1
            )
        if teve_erro:
            estado["erros_qtd"] = int(estado.get("erros_qtd") or 0) + 1
        elif nome_arquivo:
            # Se o arquivo foi processado com sucesso agora, remove-o da lista de artigos com erro
            erros_map = estado.setdefault("arquivos_com_erro", {})
            erros_map.pop(nome_arquivo, None)

        estado["percentual"] = min(99, int((proc / total) * 100))


def registrar_erro_arquivo(
    nome_arquivo: str, object_key: str, mensagem_erro: str
) -> None:
    """Registra de forma persistente que um PDF específico teve erro durante a análise."""
    horario = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    with _ContextoEstadoAtomico() as estado:
        erros_map = estado.setdefault("arquivos_com_erro", {})
        anterior = erros_map.get(nome_arquivo, {})
        tentativas = int(anterior.get("tentativas") or 0) + 1
        erros_map[nome_arquivo] = {
            "nome_arquivo": nome_arquivo,
            "object_key": object_key,
            "erro": str(mensagem_erro)[:400],
            "horario": horario,
            "tentativas": tentativas,
        }


def limpar_historico_erros_arquivos() -> None:
    """Limpa a lista persistente de arquivos que apresentaram erro."""
    with _ContextoEstadoAtomico() as estado:
        estado["arquivos_com_erro"] = {}
        estado["erros"] = []
        estado["erros_qtd"] = 0


def atualizar_status_scheduler(
    ultimo_resumo: str,
    proximo_ciclo_ts: float | None = None,
    incrementar_ciclo: bool = False,
) -> None:
    """Atualiza os metadados do agendador automático de 10 minutos."""
    intervalo = int(getattr(settings, "PIPELINE_CRON_INTERVAL_MINUTES", 10))
    max_por_ciclo = int(getattr(settings, "PIPELINE_MAX_GEMINI_FILES_PER_CRON", 3))
    with _ContextoEstadoAtomico() as estado:
        sched = estado.setdefault("scheduler", {})
        sched["ativo"] = bool(getattr(settings, "PIPELINE_CRON_ENABLED", True))
        sched["intervalo_minutos"] = intervalo
        sched["max_pdfs_por_ciclo"] = max_por_ciclo
        sched["ultimo_ciclo_em"] = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        if proximo_ciclo_ts is not None:
            sched["proximo_ciclo_ts"] = proximo_ciclo_ts
        sched["ultimo_resumo"] = ultimo_resumo
        if incrementar_ciclo:
            sched["total_ciclos"] = int(sched.get("total_ciclos") or 0) + 1


def adicionar_log_progresso(linha: str, is_erro: bool = False) -> None:
    texto = linha.rstrip()
    if not texto:
        return
    with _ContextoEstadoAtomico() as estado:
        logs = estado.setdefault("logs", [])
        logs.append(texto)
        if is_erro:
            erros = estado.setdefault("erros", [])
            erros.append(texto)


def finalizar_progresso(resumo_final: str = "Processamento finalizado!") -> None:
    with _ContextoEstadoAtomico() as estado:
        estado["em_execucao"] = False
        estado["concluido"] = True
        estado["percentual"] = 100
        estado["etapa_atual"] = resumo_final
        estado["finalizado_em"] = datetime.datetime.now().strftime("%H:%M:%S")


def obter_snapshot_progresso() -> Dict[str, Any]:
    with _ContextoEstadoAtomico() as estado:
        snap = dict(estado)
        snap["logs"] = list(estado.get("logs", [])[-150:])
        snap["erros"] = list(estado.get("erros", [])[-50:])
        snap["cotas_modelos"] = _montar_tabela_cotas(estado)

        erros_map = estado.get("arquivos_com_erro") or {}
        lista_erros_arq = sorted(
            erros_map.values(),
            key=lambda x: x.get("horario", ""),
            reverse=True,
        )
        snap["lista_arquivos_com_erro"] = lista_erros_arq
        snap["total_arquivos_com_erro"] = len(lista_erros_arq)

        sched = dict(estado.get("scheduler") or {})
        prox_ts = float(sched.get("proximo_ciclo_ts") or 0.0)
        restante_seg = max(0, int(prox_ts - time.time()))
        mins, segs = divmod(restante_seg, 60)
        sched["proximo_ciclo_seg"] = restante_seg
        sched["proximo_ciclo_human"] = f"{mins:02d}m {segs:02d}s"
        snap["scheduler"] = sched
        return snap
