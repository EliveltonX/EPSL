import html
import io
from pathlib import Path

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.management import call_command
from django.db.models import Q
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseRedirect,
    JsonResponse,
)
from django.middleware.csrf import get_token
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from licitacoes.models import Licitacao


@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def home_status(request: HttpRequest) -> HttpResponse:
    """
    Página inicial pública (`/`):
    - Exibe sempre as informações de status do sistema (Modo, Banco, Storage e Total de Licitações).
    - Exibe o formulário de Login embutido para usuários não autenticados.
    - Para usuários já autenticados, exibe atalhos para as páginas restritas (/licitacoes/, /admin/) e Logout.
    """
    erro_login = ""
    next_url = request.GET.get("next") or request.POST.get("next") or ""

    if request.method == "POST" and not request.user.is_authenticated:
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            if next_url and url_has_allowed_host_and_scheme(
                url=next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                return HttpResponseRedirect(next_url)
            return HttpResponseRedirect("/")
        else:
            erro_login = "Usuário ou senha inválidos. Tente novamente."

    modo = "DEPLOY (Produção Coolify)" if settings.IS_DEPLOY else "TESTE LOCAL"
    db_engine = settings.DATABASES["default"]["ENGINE"]
    db_warning = getattr(settings, "DB_CONNECTION_WARNING", "")
    bloco_db_warning = (
        f'<div class="alert alert-warning" style="margin-top:12px;">⚠️ <strong>Aviso de Conexão PostgreSQL:</strong> {html.escape(db_warning)}</div>'
        if db_warning
        else ""
    )
    total_licitacoes = Licitacao.objects.count()

    if settings.USE_LOCAL_STORAGE:
        input_dir = (
            Path(settings.LOCAL_STORAGE_ROOT)
            / settings.SUPABASE_INPUT_PREFIX.strip("/")
        )
        pdfs_pendentes = (
            len(list(input_dir.glob("*.pdf"))) if input_dir.exists() else 0
        )
        storage_info = f"Pasta Local ({input_dir}) — {pdfs_pendentes} PDF(s) na fila"
    else:
        storage_info = (
            f"Supabase Storage (Bucket: {settings.SUPABASE_STORAGE_BUCKET})"
        )

    csrf_token = get_token(request)
    escaped_next = html.escape(next_url)

    if request.user.is_authenticated:
        username_esc = html.escape(request.user.get_username())
        bloco_auth = f"""
        <div class="card auth-card logged-in">
            <h2>🔐 Sessão Autenticada</h2>
            <p>Olá, <strong>{username_esc}</strong>! Você tem acesso completo às páginas protegidas do sistema.</p>
            <div class="actions">
                <a href="/licitacoes/" class="btn btn-primary">📄 Abrir Painel de Licitações</a>
                <a href="/admin/" class="btn btn-secondary">⚙️ Django Admin (/admin/)</a>
            </div>
            <form method="post" action="/logout/" style="margin-top: 16px;">
                <input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">
                <button type="submit" class="btn btn-danger">Sair (Logout)</button>
            </form>
        </div>
        """
    else:
        aviso_restrito = ""
        if next_url:
            aviso_restrito = (
                f'<div class="alert alert-warning">'
                f"🔒 Faça login abaixo para acessar a página protegida <code>{escaped_next}</code>."
                f"</div>"
            )
        alerta_erro = (
            f'<div class="alert alert-error">{html.escape(erro_login)}</div>'
            if erro_login
            else ""
        )
        bloco_auth = f"""
        <div class="card auth-card">
            <h2>🔑 Login do Sistema</h2>
            <p class="muted">As demais páginas do sistema são protegidas e exclusivas para usuários autenticados.</p>
            {aviso_restrito}
            {alerta_erro}
            <form method="post" action="/">
                <input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">
                <input type="hidden" name="next" value="{escaped_next}">
                <div class="form-group">
                    <label for="id_username">Usuário</label>
                    <input type="text" id="id_username" name="username" required autofocus placeholder="Digite seu usuário">
                </div>
                <div class="form-group">
                    <label for="id_password">Senha</label>
                    <input type="password" id="id_password" name="password" required placeholder="Digite sua senha">
                </div>
                <button type="submit" class="btn btn-primary" style="width: 100%;">Entrar no Sistema</button>
            </form>
        </div>
        """

    pagina_html = f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>EPSL — Automação de Busca em Diários Oficiais</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 960px; margin: 36px auto; padding: 0 20px; color: #1f2937; background: #f3f4f6; line-height: 1.6; }}
        h1 {{ margin-bottom: 8px; color: #111827; }}
        h2 {{ margin-top: 0; font-size: 1.2rem; color: #111827; }}
        .grid {{ display: grid; grid-template-columns: 1.2fr 1fr; gap: 24px; margin-top: 20px; align-items: start; }}
        @media (max-width: 768px) {{ .grid {{ grid-template-columns: 1fr; }} }}
        .card {{ border: 1px solid #e5e7eb; border-radius: 12px; padding: 24px; background: #ffffff; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
        .badge {{ display: inline-block; padding: 4px 10px; border-radius: 6px; font-weight: 600; font-size: 0.85rem; background: #dbeafe; color: #1e40af; }}
        code {{ background: #f3f4f6; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }}
        .muted {{ color: #6b7280; font-size: 0.92rem; margin-top: -4px; }}
        .form-group {{ margin-bottom: 14px; }}
        .form-group label {{ display: block; font-weight: 600; margin-bottom: 4px; font-size: 0.9rem; }}
        .form-group input {{ width: 100%; padding: 10px 12px; border: 1px solid #d1d5db; border-radius: 8px; font-size: 0.95rem; }}
        .form-group input:focus {{ outline: none; border-color: #2563eb; box-shadow: 0 0 0 3px rgba(37,99,235,0.15); }}
        .btn {{ display: inline-block; padding: 10px 16px; border-radius: 8px; font-weight: 600; font-size: 0.95rem; text-decoration: none; border: none; cursor: pointer; text-align: center; }}
        .btn-primary {{ background: #2563eb; color: #ffffff; }}
        .btn-primary:hover {{ background: #1d4ed8; }}
        .btn-secondary {{ background: #e5e7eb; color: #1f2937; }}
        .btn-secondary:hover {{ background: #d1d5db; }}
        .btn-danger {{ background: #ef4444; color: #ffffff; }}
        .btn-danger:hover {{ background: #dc2626; }}
        .actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }}
        .alert {{ padding: 10px 12px; border-radius: 8px; margin-bottom: 14px; font-size: 0.9rem; }}
        .alert-error {{ background: #fee2e2; color: #991b1b; border: 1px solid #fecaca; }}
        .alert-warning {{ background: #fef3c7; color: #92400e; border: 1px solid #fde68a; }}
    </style>
</head>
<body>
    <h1>EPSL — Automação de Busca em Diários Oficiais</h1>
    <div class="grid">
        <div class="card">
            <h2>📊 Informações de Status do Sistema</h2>
            <p><strong>Modo de Operação:</strong> <span class="badge">{modo}</span></p>
            <p><strong>Banco de Dados Ativo:</strong> <code>{db_engine}</code></p>
            {bloco_db_warning}
            <p><strong>Storage Configurado:</strong> {storage_info}</p>
            <p><strong>Licitações Cadastradas no Banco:</strong> <strong>{total_licitacoes}</strong></p>
        </div>
        {bloco_auth}
    </div>
</body>
</html>"""
    return HttpResponse(pagina_html)


@require_http_methods(["GET", "POST"])
def logout_view(request: HttpRequest) -> HttpResponseRedirect:
    """Encerra a sessão do usuário e retorna para a página inicial."""
    logout(request)
    return HttpResponseRedirect("/")


@login_required
@ensure_csrf_cookie
@require_GET
def painel_licitacoes(request: HttpRequest) -> HttpResponse:
    """
    Página interna protegida (`/licitacoes/`) acessível apenas para usuários logados.
    Permite consultar e filtrar as licitações extraídas e disparar o processamento manual.
    """
    busca = request.GET.get("q", "").strip()
    qs = Licitacao.objects.all()
    if busca:
        qs = qs.filter(
            Q(cidade__icontains=busca)
            | Q(orgao_emissor__icontains=busca)
            | Q(modalidade__icontains=busca)
            | Q(objeto_licitacao__icontains=busca)
        )

    licitacoes = qs[:100]
    csrf_token = get_token(request)
    busca_esc = html.escape(busca)
    username_esc = html.escape(request.user.get_username())

    linhas_html = ""
    for item in licitacoes:
        linhas_html += f"""
        <tr>
            <td><strong>{html.escape(item.cidade)}</strong></td>
            <td>{html.escape(item.orgao_emissor)}</td>
            <td>{item.data_publicacao.strftime('%d/%m/%Y')}</td>
            <td><span class="badge">{html.escape(item.modalidade)}</span></td>
            <td>{html.escape(item.objeto_licitacao)}</td>
            <td>{html.escape(item.paginas_referencia)}</td>
            <td><a href="{html.escape(item.link_documento)}" target="_blank" rel="noopener">Abrir PDF</a></td>
        </tr>
        """

    if not linhas_html:
        linhas_html = (
            '<tr><td colspan="7" style="text-align:center; padding: 24px; color: #6b7280;">'
            "Nenhuma licitação encontrada no banco de dados.</td></tr>"
        )

    pagina_html = f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <title>Painel de Licitações — EPSL</title>
    <style>
        body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 1180px; margin: 30px auto; padding: 0 20px; color: #1f2937; background: #f3f4f6; }}
        .topbar {{ display: flex; justify-content: space-between; align-items: center; background: #ffffff; padding: 16px 24px; border-radius: 12px; border: 1px solid #e5e7eb; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }}
        .card {{ background: #ffffff; padding: 24px; border-radius: 12px; border: 1px solid #e5e7eb; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 0.92rem; }}
        th, td {{ border-bottom: 1px solid #e5e7eb; padding: 12px 10px; text-align: left; vertical-align: top; }}
        th {{ background: #f9fafb; font-weight: 600; }}
        .badge {{ display: inline-block; padding: 3px 8px; border-radius: 6px; font-size: 0.8rem; background: #dbeafe; color: #1e40af; font-weight: 600; }}
        .btn {{ display: inline-block; padding: 8px 14px; border-radius: 8px; font-weight: 600; font-size: 0.9rem; text-decoration: none; border: none; cursor: pointer; }}
        .btn-primary {{ background: #2563eb; color: #fff; }}
        .btn-secondary {{ background: #e5e7eb; color: #1f2937; }}
        .search-bar {{ display: flex; gap: 8px; margin-bottom: 12px; }}
        .search-bar input {{ flex: 1; padding: 9px 12px; border: 1px solid #d1d5db; border-radius: 8px; }}
        a {{ color: #2563eb; font-weight: 600; text-decoration: none; }}
    </style>
</head>
<body>
    <div class="topbar">
        <div>
            <strong>EPSL — Painel Protegido de Licitações</strong>
            <span style="color:#6b7280; margin-left: 10px;">(Usuário: {username_esc})</span>
        </div>
        <div style="display:flex; gap:10px; align-items:center;">
            <a href="/" class="btn btn-secondary">🏠 Página Inicial</a>
            <a href="/admin/" class="btn btn-secondary">⚙️ Admin</a>
            <form method="post" action="/licitacoes/processar-web/" style="margin:0;">
                <input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">
                <button type="submit" class="btn btn-primary">▶️ Processar Novos Diários Agora</button>
            </form>
        </div>
    </div>

    <div class="card">
        <form method="get" action="/licitacoes/" class="search-bar">
            <input type="text" name="q" value="{busca_esc}" placeholder="Filtrar por cidade, órgão emissor, modalidade ou objeto...">
            <button type="submit" class="btn btn-primary">Buscar</button>
            <a href="/licitacoes/" class="btn btn-secondary">Limpar</a>
        </form>

        <table>
            <thead>
                <tr>
                    <th>Cidade</th>
                    <th>Órgão Emissor</th>
                    <th>Publicação</th>
                    <th>Modalidade</th>
                    <th>Objeto da Licitação</th>
                    <th>Páginas</th>
                    <th>Documento</th>
                </tr>
            </thead>
            <tbody>
                {linhas_html}
            </tbody>
        </table>
    </div>
</body>
</html>"""
    return HttpResponse(pagina_html)


@login_required
@require_POST
def acionar_processamento_web(request: HttpRequest) -> HttpResponse:
    """Permite que um usuário logado dispare o comando `processar_diarios` pela interface web."""
    out = io.StringIO()
    err = io.StringIO()
    status_titulo = "Processamento Concluído com Sucesso"
    try:
        call_command("processar_diarios", stdout=out, stderr=err)
    except Exception as exc:
        status_titulo = f"Erro durante o processamento: {exc}"

    log_str = html.escape(out.getvalue() or "Nenhuma saída registrada.")
    err_str = html.escape(err.getvalue())
    bloco_erro = f"<h3>Erros:</h3><pre>{err_str}</pre>" if err_str else ""

    return HttpResponse(
        f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <title>Resultado do Processamento — EPSL</title>
    <style>
        body {{ font-family: system-ui, sans-serif; max-width: 860px; margin: 40px auto; padding: 0 20px; background: #f3f4f6; }}
        .card {{ background: #fff; padding: 24px; border-radius: 12px; border: 1px solid #e5e7eb; }}
        pre {{ background: #111827; color: #f9fafb; padding: 16px; border-radius: 8px; overflow-x: auto; white-space: pre-wrap; }}
        .btn {{ display: inline-block; padding: 10px 16px; background: #2563eb; color: #fff; border-radius: 8px; text-decoration: none; font-weight: 600; }}
    </style>
</head>
<body>
    <div class="card">
        <h2>{html.escape(status_titulo)}</h2>
        <h3>Log de Execução:</h3>
        <pre>{log_str}</pre>
        {bloco_erro}
        <p style="margin-top: 20px;">
            <a href="/licitacoes/" class="btn">← Voltar ao Painel de Licitações</a>
            <a href="/" class="btn" style="background:#4b5563; margin-left:8px;">🏠 Página Inicial</a>
        </p>
    </div>
</body>
</html>"""
    )


@login_required
@require_GET
def servir_media_protegida(request: HttpRequest, rel_path: str) -> FileResponse:
    """Serve os PDFs da pasta local `storage_local/` apenas para usuários autenticados."""
    root = Path(settings.MEDIA_ROOT).resolve()
    target = (root / rel_path).resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        raise Http404("Arquivo não encontrado.")
    return FileResponse(target.open("rb"), content_type="application/pdf")


@csrf_exempt
@require_POST
def acionar_processamento_diarios(request: HttpRequest) -> JsonResponse:
    """
    Endpoint HTTP (API) para acionamento via n8n/Cron (com `X-Pipeline-Token`) ou usuário autenticado.
    """
    expected_token = settings.PIPELINE_TRIGGER_TOKEN
    received_token = request.headers.get("X-Pipeline-Token", "")
    token_valido = bool(expected_token and received_token == expected_token)

    if not (request.user.is_authenticated or token_valido):
        return JsonResponse(
            {"status": "erro", "mensagem": "Acesso não autorizado. Faça login ou envie X-Pipeline-Token válido."},
            status=403,
        )

    out = io.StringIO()
    err = io.StringIO()
    try:
        call_command("processar_diarios", stdout=out, stderr=err)
        return JsonResponse(
            {
                "status": "sucesso",
                "log_execucao": out.getvalue(),
                "erros": err.getvalue(),
            }
        )
    except Exception as exc:
        return JsonResponse(
            {
                "status": "erro",
                "mensagem": str(exc),
                "log_execucao": out.getvalue(),
            },
            status=500,
        )
