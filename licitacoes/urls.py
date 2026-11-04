from django.urls import path
from licitacoes.views import (
    acionar_processamento_diarios,
    acionar_processamento_web,
    atualizar_tag_licitacao,
    detalhe_licitacao,
    gerar_urls_upload_presign,
    pagina_upload_diarios,
    painel_licitacoes,
    receber_stream_pdf_local,
)

urlpatterns = [
    path("", painel_licitacoes, name="painel_licitacoes"),
    path("<int:pk>/", detalhe_licitacao, name="detalhe_licitacao"),
    path("<int:pk>/tag/", atualizar_tag_licitacao, name="atualizar_tag_licitacao"),
    path("upload/", pagina_upload_diarios, name="pagina_upload_diarios"),
    path("upload/presign/", gerar_urls_upload_presign, name="gerar_urls_upload_presign"),
    path(
        "upload/stream-local/<str:filename>",
        receber_stream_pdf_local,
        name="receber_stream_pdf_local",
    ),
    path("processar-web/", acionar_processamento_web, name="processar_diarios_web"),
    path("processar/", acionar_processamento_diarios, name="processar_diarios_webhook"),
]
