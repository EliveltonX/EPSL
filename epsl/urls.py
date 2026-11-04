from django.contrib import admin
from django.urls import include, path

from licitacoes.views import (
    acionar_processamento_diarios,
    home_status,
    logout_view,
    painel_servidor,
    servir_media_protegida,
)

urlpatterns = [
    # Página inicial pública: exibe status + formulário de login
    path("", home_status, name="home_status"),
    path("logout/", logout_view, name="logout"),
    # Rotas protegidas por autenticação (LoginRequiredMiddleware + @login_required)
    path("servidor/", painel_servidor, name="painel_servidor_atalho"),
    path("licitacoes/", include("licitacoes.urls")),
    path("api/licitacoes/processar/", acionar_processamento_diarios, name="api_processar_diarios"),
    path("media/<path:rel_path>", servir_media_protegida, name="servir_media_protegida"),
    path("admin/", admin.site.urls),
]
