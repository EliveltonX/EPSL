from django.urls import path
from licitacoes.views import (
    acionar_processamento_diarios,
    acionar_processamento_web,
    painel_licitacoes,
)

urlpatterns = [
    path("", painel_licitacoes, name="painel_licitacoes"),
    path("processar-web/", acionar_processamento_web, name="processar_diarios_web"),
    path("processar/", acionar_processamento_diarios, name="processar_diarios_webhook"),
]
