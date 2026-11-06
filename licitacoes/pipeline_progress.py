"""
Gerenciador de Progresso em Tempo Real da Pipeline (`processar_diarios`) e
Balanceador de Cotas de Modelos Google Gemini (RPM / TPM / RPD).

Baseado na tabela oficial de limites (`Docs_Vault/Arquitetura/ANOTAÇÕES.md`):
- gemini-3.5-flash-lite : 15 RPM | 250K TPM | 500 RPD
- gemini-3.1-flash-lite : 15 RPM | 250K TPM | 500 RPD
- gemini-3.6-flash      :  5 RPM | 250K TPM |  20 RPD
- gemini-3-flash-preview:  5 RPM | 250K TPM |  20 RPD
- gemini-3.8-flash      :  5 RPM | 250K TPM |  20 RPD
"""

from __future__ import annotations

import datetime
import threading
import time
from typing import Any, Dict, List

from django.conf import settings

_LOCK = threading.Lock()

# Estado global em memória do processamento atual (para a barra de carregamento ao vivo)
_ESTADO_PROGRESSO: Dict[str, Any] = {
    "em_execucao": False,
    "concluido": False,
    "iniciado_em": "",
    "finalizado_em": "",
    "total_arquivos": 0,
    "arquivos_processados": 0,
    "percentual": 0,
    "arquivo_atual": "",
    "etapa_atual": "Aguardando início...",
    "modelo_ativo": "",
    "licitacoes_extraidas": 0,
    "duplicados_bloqueados": 0,
    "erros_qtd": 0,
    "logs": [],
    "erros": [],
}

# Controle de Rate Limit (RPM nos últimos 60s e RPD no dia atual) por modelo
_DATA_HOJE: datetime.date = datetime.date.today()
_TIMESTAMPS_RPM: Dict[str, List[float]] = {}
_CONTADOR_RPD: Dict[str, int] = {}
_COOLDOWN_ATE: Dict[str, float] = {}


def _resetar_rpd_se_mudou_dia() -> None:
    global _DATA_HOJE
    hoje = datetime.date.today()
    if hoje != _DATA_HOJE:
        _DATA_HOJE = hoje
        _CONTADOR_RPD.clear()
        _COOLDOWN_ATE.clear()


def obter_tabela_cotas() -> List[Dict[str, Any]]:
    """Retorna a lista de modelos configurados com uso atual de RPM (últimos 60s) e RPD (hoje)."""
    agora = time.time()
    _resetar_rpd_se_mudou_dia()
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
    resultado = []
    for item in quotas_cfg:
        m = item["model"]
        hist = [t for t in _TIMESTAMPS_RPM.get(m, []) if (agora - t) < 60.0]
        _TIMESTAMPS_RPM[m] = hist
        rpd_usado = _CONTADOR_RPD.get(m, 0)
        cooldown_restante = max(0.0, _COOLDOWN_ATE.get(m, 0.0) - agora)
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


def ordenar_modelos_para_chamada() -> List[Dict[str, Any]]:
    """
    Retorna os modelos ordenados priorizando:
    1. Modelos com cota RPM e RPD livre agora e sem cooldown.
    2. Maior folga de RPD/RPM (mantendo a ordem da tabela `GEMINI_MODELS_QUOTAS`).
    """
    with _LOCK:
        status_modelos = obter_tabela_cotas()

    disponiveis = [m for m in status_modelos if m["disponivel"]]
    indisponiveis = [
        m for m in status_modelos if not m["disponivel"] and m["rpd_usado"] < m["rpd_limite"]
    ]
    return disponiveis + indisponiveis


def aguardar_slot_rpm_se_necessario(model_name: str, rpm_limite: int) -> float:
    """
    Se `model_name` já atingiu `rpm_limite` nos últimos 60 segundos, calcula quantos
    segundos faltam para liberar a janela de 1 minuto.
    """
    agora = time.time()
    with _LOCK:
        hist = [t for t in _TIMESTAMPS_RPM.get(model_name, []) if (agora - t) < 60.0]
        _TIMESTAMPS_RPM[model_name] = hist
        if len(hist) < rpm_limite:
            return 0.0
        mais_antigo = min(hist)
        espera = max(0.5, 60.2 - (agora - mais_antigo))
        return espera


def registrar_uso_modelo(model_name: str) -> None:
    """Contabiliza 1 requisição realizada com sucesso no RPM (janela 60s) e RPD (dia)."""
    agora = time.time()
    with _LOCK:
        _resetar_rpd_se_mudou_dia()
        hist = [t for t in _TIMESTAMPS_RPM.get(model_name, []) if (agora - t) < 60.0]
        hist.append(agora)
        _TIMESTAMPS_RPM[model_name] = hist
        _CONTADOR_RPD[model_name] = _CONTADOR_RPD.get(model_name, 0) + 1
        _ESTADO_PROGRESSO["modelo_ativo"] = model_name


def registrar_cooldown_modelo(model_name: str, segundos: float = 30.0) -> None:
    """Coloca um modelo em cooldown temporário quando retorna 429 (Quota) ou 503 (Sobrecarga)."""
    with _LOCK:
        _COOLDOWN_ATE[model_name] = time.time() + segundos


# ==============================================================================
# Funções de Atualização da Barra de Carregamento (Progresso ao Vivo)
# ==============================================================================
def iniciar_progresso(total_arquivos: int = 0) -> None:
    with _LOCK:
        _ESTADO_PROGRESSO.update(
            {
                "em_execucao": True,
                "concluido": False,
                "iniciado_em": datetime.datetime.now().strftime("%H:%M:%S"),
                "finalizado_em": "",
                "total_arquivos": total_arquivos,
                "arquivos_processados": 0,
                "percentual": 2 if total_arquivos == 0 else 5,
                "arquivo_atual": "Listando arquivos em novos_diarios/...",
                "etapa_atual": "Conectando ao Storage e verificando fila...",
                "modelo_ativo": getattr(settings, "GEMINI_MODEL", "gemini-3.5-flash-lite"),
                "licitacoes_extraidas": 0,
                "duplicados_bloqueados": 0,
                "erros_qtd": 0,
                "logs": [],
                "erros": [],
            }
        )


def definir_total_arquivos(total: int) -> None:
    with _LOCK:
        _ESTADO_PROGRESSO["total_arquivos"] = total
        if total == 0:
            _ESTADO_PROGRESSO["percentual"] = 100
            _ESTADO_PROGRESSO["etapa_atual"] = "Nenhum arquivo PDF pendente na fila."


def atualizar_etapa_arquivo(
    indice_1_based: int,
    nome_arquivo: str,
    etapa: str,
) -> None:
    with _LOCK:
        total = max(_ESTADO_PROGRESSO["total_arquivos"], 1)
        pct_base = int(((indice_1_based - 1) / total) * 100)
        _ESTADO_PROGRESSO["arquivo_atual"] = f"({indice_1_based}/{total}) {nome_arquivo}"
        _ESTADO_PROGRESSO["etapa_atual"] = etapa
        _ESTADO_PROGRESSO["percentual"] = max(5, min(95, pct_base + int(50 / total)))


def concluir_um_arquivo(
    licitacoes_adicionadas: int = 0,
    foi_duplicado: bool = False,
    teve_erro: bool = False,
) -> None:
    with _LOCK:
        proc = _ESTADO_PROGRESSO["arquivos_processados"] + 1
        total = max(_ESTADO_PROGRESSO["total_arquivos"], proc)
        _ESTADO_PROGRESSO["arquivos_processados"] = proc
        _ESTADO_PROGRESSO["licitacoes_extraidas"] += licitacoes_adicionadas
        if foi_duplicado:
            _ESTADO_PROGRESSO["duplicados_bloqueados"] += 1
        if teve_erro:
            _ESTADO_PROGRESSO["erros_qtd"] += 1
        _ESTADO_PROGRESSO["percentual"] = min(99, int((proc / total) * 100))


def adicionar_log_progresso(linha: str, is_erro: bool = False) -> None:
    texto = linha.rstrip()
    if not texto:
        return
    with _LOCK:
        _ESTADO_PROGRESSO["logs"].append(texto)
        if is_erro:
            _ESTADO_PROGRESSO["erros"].append(texto)


def finalizar_progresso(resumo_final: str = "Processamento finalizado!") -> None:
    with _LOCK:
        _ESTADO_PROGRESSO["em_execucao"] = False
        _ESTADO_PROGRESSO["concluido"] = True
        _ESTADO_PROGRESSO["percentual"] = 100
        _ESTADO_PROGRESSO["etapa_atual"] = resumo_final
        _ESTADO_PROGRESSO["finalizado_em"] = datetime.datetime.now().strftime("%H:%M:%S")


def obter_snapshot_progresso() -> Dict[str, Any]:
    with _LOCK:
        snap = dict(_ESTADO_PROGRESSO)
        snap["logs"] = list(_ESTADO_PROGRESSO["logs"][-150:])
        snap["erros"] = list(_ESTADO_PROGRESSO["erros"][-50:])
        snap["cotas_modelos"] = obter_tabela_cotas()
        return snap
