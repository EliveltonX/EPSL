from django.urls import path
from licitacoes.views import (
    acionar_processamento_diarios,
    acionar_processamento_web,
    atualizar_tag_licitacao,
    detalhe_licitacao,
    gerar_urls_upload_presign,
    limpar_erros_analise_json,
    metricas_servidor_json,
    pagina_upload_diarios,
    painel_licitacoes,
    painel_servidor,
    receber_stream_pdf_local,
    salvar_anotacoes_licitacao,
    status_processamento_json,
)

urlpatterns = [
    path("", painel_licitacoes, name="painel_licitacoes"),
    path("servidor/", painel_servidor, name="painel_servidor"),
    path("servidor/metricas/", metricas_servidor_json, name="metricas_servidor_json"),
    path(
        "servidor/limpar-erros/",
        limpar_erros_analise_json,
        name="limpar_erros_analise_json",
    ),
    path("<int:pk>/", detalhe_licitacao, name="detalhe_licitacao"),
    path("<int:pk>/tag/", atualizar_tag_licitacao, name="atualizar_tag_licitacao"),
    path(
        "<int:pk>/anotacoes/",
        salvar_anotacoes_licitacao,
        name="salvar_anotacoes_licitacao",
    ),
    path("upload/", pagina_upload_diarios, name="pagina_upload_diarios"),
    path("upload/presign/", gerar_urls_upload_presign, name="gerar_urls_upload_presign"),
    path(
        "upload/stream-local/<str:filename>",
        receber_stream_pdf_local,
        name="receber_stream_pdf_local",
    ),
    path("processar-web/", acionar_processamento_web, name="processar_diarios_web"),
    path("processar-web/status/", status_processamento_json, name="status_processamento_json"),
    path("processar/", acionar_processamento_diarios, name="processar_diarios_webhook"),
]
