from typing import Callable
from urllib.parse import quote

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect


class LoginRequiredMiddleware:
    """
    Garante que todas as páginas do sistema EPSL exijam usuário autenticado,
    exceto:
    - A página inicial `/` (que exibe as informações públicas de status e o form de login);
    - A rota `/logout/`;
    - Arquivos estáticos (`/static/`);
    - O webhook `/api/licitacoes/processar/` quando autenticado via `X-Pipeline-Token`.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        path = request.path_info

        # 1. Permite acesso livre à página inicial (onde fica o form de login e o status) e estáticos
        if path in ("/", "/logout/") or path.startswith("/static/"):
            return self.get_response(request)

        # 2. Permite o webhook do n8n/Cron caso envie o header X-Pipeline-Token válido
        if path == "/api/licitacoes/processar/":
            expected_token = getattr(settings, "PIPELINE_TRIGGER_TOKEN", "")
            received_token = request.headers.get("X-Pipeline-Token", "")
            if expected_token and received_token == expected_token:
                return self.get_response(request)

        # 3. Qualquer outra página exige usuário autenticado
        if not request.user.is_authenticated:
            login_url = settings.LOGIN_URL
            next_url = quote(request.get_full_path(), safe="/?=&")
            return HttpResponseRedirect(f"{login_url}?next={next_url}")

        return self.get_response(request)

