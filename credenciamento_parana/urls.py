from django.urls import path

from credenciamento_parana.views import (
    alternar_favorito_escola,
    dados_escolas_json,
    mapa_credenciamento_parana,
    resincronizar_dados_anexo1,
    salvar_observacao_escola,
    servir_tile_mapa,
)

urlpatterns = [
    path("", mapa_credenciamento_parana, name="mapa_credenciamento_parana"),
    path(
        "tiles/<str:provider>/<int:z>/<int:x>/<int:y>.png",
        servir_tile_mapa,
        name="servir_tile_mapa",
    ),
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

