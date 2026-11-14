from django.urls import path

from credenciamento_parana.views import (
    alternar_favorito_escola,
    dados_escolas_json,
    mapa_credenciamento_parana,
    resincronizar_dados_anexo1,
    salvar_observacao_escola,
)

urlpatterns = [
    path("", mapa_credenciamento_parana, name="mapa_credenciamento_parana"),
    path("api/escolas/", dados_escolas_json, name="dados_escolas_credenciamento_json"),
    path(
        "escola/<int:pk>/favorito/",
        alternar_favorito_escola,
        name="alternar_favorito_escola",
    ),
    path(
        "escola/<int:pk>/observacao/",
        salvar_observacao_escola,
        name="salvar_observacao_escola",
    ),
    path(
        "resincronizar/",
        resincronizar_dados_anexo1,
        name="resincronizar_dados_anexo1",
    ),
]

