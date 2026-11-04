import html
import io
import json
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
from django.shortcuts import get_object_or_404
from django.middleware.csrf import get_token
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from licitacoes.models import Licitacao
from licitacoes.storage_upload import gerar_lote_urls_upload, sanitizar_nome_pdf


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
                <a href="/licitacoes/upload/" class="btn btn-secondary">📤 Upload de Múltiplos PDFs</a>
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


TAG_META = {
    "": {"label": "Sem Marcação", "icon": "⚪", "cls": "tag-none"},
    "favorito": {"label": "Favorito", "icon": "⭐", "cls": "tag-favorito"},
    "em_analise": {"label": "Em Análise", "icon": "🔍", "cls": "tag-analise"},
    "sem_conformidade": {
        "label": "Sem Conformidade",
        "icon": "⚠️",
        "cls": "tag-inconformidade",
    },
    "ignorado": {"label": "Ignorado", "icon": "🚫", "cls": "tag-ignorado"},
}


def _render_select_tag(item_id: int, current_tag: str) -> str:
    """Renderiza o seletor colorido da 1ª coluna da tabela com stopPropagation."""
    meta = TAG_META.get(current_tag, TAG_META[""])
    opcoes_html = ""
    for val, info in TAG_META.items():
        selected = "selected" if val == current_tag else ""
        opcoes_html += (
            f'<option value="{val}" {selected}>{info["icon"]} {info["label"]}</option>'
        )
    return (
        f'<select id="tag-select-{item_id}" class="tag-select {meta["cls"]}" '
        f'onclick="event.stopPropagation();" '
        f'onchange="atualizarTagLinha(event, {item_id}, this)">'
        f"{opcoes_html}</select>"
    )


@login_required
@ensure_csrf_cookie
@require_GET
def painel_licitacoes(request: HttpRequest) -> HttpResponse:
    """
    Página interna protegida (`/licitacoes/`) acessível apenas para usuários logados.
    Permite consultar, filtrar por busca textual e por Tag de Análise, marcar tags
    na 1ª coluna e clicar em qualquer linha para abrir a Página de Detalhes.
    """
    busca = request.GET.get("q", "").strip()
    filtro_tag = request.GET.get("tag", "todas").strip()

    qs = Licitacao.objects.all()
    if busca:
        qs = qs.filter(
            Q(cidade__icontains=busca)
            | Q(orgao_emissor__icontains=busca)
            | Q(modalidade__icontains=busca)
            | Q(objeto_licitacao__icontains=busca)
        )

    if filtro_tag == "sem_marcacao":
        qs = qs.filter(tag_analise="")
    elif filtro_tag in ("favorito", "em_analise", "sem_conformidade", "ignorado"):
        qs = qs.filter(tag_analise=filtro_tag)

    licitacoes = qs[:100]
    csrf_token = get_token(request)
    busca_esc = html.escape(busca)
    username_esc = html.escape(request.user.get_username())

    linhas_html = ""
    for item in licitacoes:
        select_tag_html = _render_select_tag(item.id, item.tag_analise)
        objeto_resumo = (
            item.objeto_licitacao[:180] + "..."
            if len(item.objeto_licitacao) > 180
            else item.objeto_licitacao
        )
        linhas_html += f"""
        <tr class="clickable-row" onclick="window.location.href='/licitacoes/{item.id}/'" title="Clique na linha para abrir os detalhes completos desta licitação">
            <td onclick="event.stopPropagation();">{select_tag_html}</td>
            <td><strong>{html.escape(item.cidade)}</strong></td>
            <td>{html.escape(item.orgao_emissor)}</td>
            <td>{item.data_publicacao.strftime('%d/%m/%Y')}</td>
            <td><span class="badge">{html.escape(item.modalidade)}</span></td>
            <td>{html.escape(objeto_resumo)}</td>
            <td>{html.escape(item.paginas_referencia)}</td>
            <td onclick="event.stopPropagation();">
                <a href="{html.escape(item.link_documento)}" target="_blank" rel="noopener">Abrir PDF</a>
            </td>
        </tr>
        """

    if not linhas_html:
        linhas_html = (
            '<tr><td colspan="8" style="text-align:center; padding: 28px; color: #6b7280;">'
            "Nenhuma licitação encontrada para os filtros selecionados.</td></tr>"
        )

    opcoes_filtro = [
        ("todas", "🏷️ Todas as Tags"),
        ("favorito", "⭐ Favorito"),
        ("em_analise", "🔍 Em Análise"),
        ("sem_conformidade", "⚠️ Sem Conformidade"),
        ("ignorado", "🚫 Ignorado"),
        ("sem_marcacao", "⚪ Sem Marcação"),
    ]
    select_filtro_html = ""
    chips_filtro_html = ""
    for val, texto in opcoes_filtro:
        sel = "selected" if filtro_tag == val else ""
        active_cls = "chip-active" if filtro_tag == val else ""
        select_filtro_html += f'<option value="{val}" {sel}>{texto}</option>'
        chips_filtro_html += (
            f'<a href="/licitacoes/?q={quote_plus_safe(busca)}&tag={val}" '
            f'class="chip {active_cls}">{texto}</a>'
        )

    pagina_html = f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <title>Painel de Licitações — EPSL</title>
    <style>
        body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 1240px; margin: 30px auto; padding: 0 20px; color: #1f2937; background: #f3f4f6; }}
        .topbar {{ display: flex; justify-content: space-between; align-items: center; background: #ffffff; padding: 16px 24px; border-radius: 12px; border: 1px solid #e5e7eb; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }}
        .card {{ background: #ffffff; padding: 24px; border-radius: 12px; border: 1px solid #e5e7eb; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 0.91rem; }}
        th, td {{ border-bottom: 1px solid #e5e7eb; padding: 12px 10px; text-align: left; vertical-align: middle; }}
        th {{ background: #f9fafb; font-weight: 600; }}
        tr.clickable-row {{ cursor: pointer; transition: background 0.15s; }}
        tr.clickable-row:hover {{ background: #eff6ff; }}
        .badge {{ display: inline-block; padding: 3px 8px; border-radius: 6px; font-size: 0.8rem; background: #dbeafe; color: #1e40af; font-weight: 600; }}
        .btn {{ display: inline-block; padding: 8px 14px; border-radius: 8px; font-weight: 600; font-size: 0.9rem; text-decoration: none; border: none; cursor: pointer; }}
        .btn-primary {{ background: #2563eb; color: #fff; }}
        .btn-secondary {{ background: #e5e7eb; color: #1f2937; }}
        .search-bar {{ display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }}
        .search-bar input {{ flex: 1; min-width: 220px; padding: 9px 12px; border: 1px solid #d1d5db; border-radius: 8px; }}
        .search-bar select {{ padding: 9px 12px; border: 1px solid #d1d5db; border-radius: 8px; background: #fff; font-weight: 600; }}
        .chips-bar {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }}
        .chip {{ padding: 5px 12px; border-radius: 999px; font-size: 0.83rem; font-weight: 600; background: #f3f4f6; color: #374151; border: 1px solid #d1d5db; text-decoration: none; }}
        .chip:hover {{ background: #e5e7eb; }}
        .chip-active {{ background: #1e40af; color: #ffffff !important; border-color: #1e40af; }}
        .tag-select {{ padding: 6px 8px; border-radius: 8px; font-size: 0.82rem; font-weight: 600; border: 1px solid #d1d5db; cursor: pointer; }}
        .tag-none {{ background: #f9fafb; color: #4b5563; }}
        .tag-favorito {{ background: #fef3c7; color: #92400e; border-color: #f59e0b; }}
        .tag-analise {{ background: #dbeafe; color: #1e40af; border-color: #3b82f6; }}
        .tag-inconformidade {{ background: #fee2e2; color: #991b1b; border-color: #ef4444; }}
        .tag-ignorado {{ background: #e5e7eb; color: #374151; border-color: #9ca3af; }}
        a {{ color: #2563eb; font-weight: 600; text-decoration: none; }}
    </style>
</head>
<body>
    <div class="topbar">
        <div>
            <strong>EPSL — Painel Protegido de Licitações</strong>
            <span style="color:#6b7280; margin-left: 10px;">(Usuário: {username_esc})</span>
        </div>
        <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
            <a href="/" class="btn btn-secondary">🏠 Página Inicial</a>
            <a href="/licitacoes/upload/" class="btn btn-primary" style="background:#059669;">📤 Enviar PDFs (Upload Múltiplo)</a>
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
            <select name="tag">
                {select_filtro_html}
            </select>
            <button type="submit" class="btn btn-primary">Buscar / Filtrar</button>
            <a href="/licitacoes/" class="btn btn-secondary">Limpar</a>
        </form>

        <div class="chips-bar">
            {chips_filtro_html}
        </div>
        <p style="margin: 4px 0 8px 0; font-size: 0.84rem; color: #6b7280;">
            💡 <strong>Dica:</strong> Clique em qualquer linha da tabela para abrir a <strong>Página de Detalhes</strong> da licitação, ou altere a Tag diretamente na 1ª coluna.
        </p>

        <table>
            <thead>
                <tr>
                    <th style="width:175px;">Tag / Situação</th>
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

    <script>
        const csrfToken = "{csrf_token}";
        const classesPorTag = {{
            "": "tag-none",
            "favorito": "tag-favorito",
            "em_analise": "tag-analise",
            "sem_conformidade": "tag-inconformidade",
            "ignorado": "tag-ignorado"
        }};

        async function atualizarTagLinha(event, licitacaoId, selectEl) {{
            event.stopPropagation();
            const novaTag = selectEl.value;
            selectEl.className = "tag-select " + (classesPorTag[novaTag] || "tag-none");

            try {{
                await fetch(`/licitacoes/${{licitacaoId}}/tag/`, {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/json",
                        "X-CSRFToken": csrfToken,
                        "X-Requested-With": "XMLHttpRequest"
                    }},
                    body: JSON.stringify({{ tag_analise: novaTag }})
                }});
            }} catch (err) {{
                alert("Erro ao salvar a tag: " + err);
            }}
        }}
    </script>
</body>
</html>"""
    return HttpResponse(pagina_html)


def quote_plus_safe(val: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(val)


@login_required
@ensure_csrf_cookie
@require_GET
def detalhe_licitacao(request: HttpRequest, pk: int) -> HttpResponse:
    """
    Página de Detalhes (`/licitacoes/<int:pk>/`) para visualização aprofundada
    de uma licitação e marcação de sua Tag pós-análise (`Favorito`, `Em Análise`,
    `Sem Conformidade`, `Ignorado`).
    """
    item = get_object_or_404(Licitacao, pk=pk)
    csrf_token = get_token(request)
    meta_atual = TAG_META.get(item.tag_analise, TAG_META[""])

    botoes_tags_html = ""
    for val, info in TAG_META.items():
        ativo = "tag-btn-active" if item.tag_analise == val else ""
        botoes_tags_html += f"""
        <button type="button"
                onclick="definirTagDetalhe('{val}')"
                id="btn-tag-{val or 'vazio'}"
                class="tag-btn {info['cls']} {ativo}">
            {info['icon']} {info['label']}
        </button>
        """

    pagina_html = f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Detalhes da Licitação #{item.id} — {html.escape(item.cidade)} | EPSL</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 1080px; margin: 30px auto; padding: 0 20px; color: #1f2937; background: #f3f4f6; line-height: 1.6; }}
        .topbar {{ display: flex; justify-content: space-between; align-items: center; background: #ffffff; padding: 16px 24px; border-radius: 12px; border: 1px solid #e5e7eb; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }}
        .card {{ background: #ffffff; padding: 28px; border-radius: 12px; border: 1px solid #e5e7eb; margin-bottom: 20px; }}
        .meta-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin: 20px 0; }}
        .meta-box {{ background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px 16px; }}
        .meta-label {{ font-size: 0.8rem; color: #6b7280; text-transform: uppercase; font-weight: 700; letter-spacing: 0.03em; }}
        .meta-value {{ font-size: 1.02rem; font-weight: 600; color: #111827; margin-top: 4px; }}
        .objeto-box {{ background: #f8fafc; border-left: 4px solid #2563eb; padding: 20px; border-radius: 8px; font-size: 1.05rem; white-space: pre-wrap; color: #1e293b; }}
        .btn {{ display: inline-block; padding: 9px 16px; border-radius: 8px; font-weight: 600; font-size: 0.92rem; text-decoration: none; border: none; cursor: pointer; }}
        .btn-primary {{ background: #2563eb; color: #fff; }}
        .btn-secondary {{ background: #e5e7eb; color: #1f2937; }}
        .tag-bar {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }}
        .tag-btn {{ padding: 9px 16px; border-radius: 999px; font-weight: 600; font-size: 0.9rem; cursor: pointer; border: 2px solid transparent; transition: all 0.15s; opacity: 0.78; }}
        .tag-btn:hover {{ opacity: 1; transform: translateY(-1px); }}
        .tag-btn-active {{ opacity: 1; border-color: #111827 !important; box-shadow: 0 2px 6px rgba(0,0,0,0.12); }}
        .tag-none {{ background: #f3f4f6; color: #4b5563; }}
        .tag-favorito {{ background: #fef3c7; color: #92400e; }}
        .tag-analise {{ background: #dbeafe; color: #1e40af; }}
        .tag-inconformidade {{ background: #fee2e2; color: #991b1b; }}
        .tag-ignorado {{ background: #e5e7eb; color: #374151; }}
    </style>
</head>
<body>
    <div class="topbar">
        <div>
            <strong>🔎 Detalhes da Licitação #{item.id}</strong>
            <span id="badgeTagAtual" class="tag-btn {meta_atual['cls']}" style="margin-left:10px; padding:4px 12px; opacity:1; cursor:default;">
                {meta_atual['icon']} {meta_atual['label']}
            </span>
        </div>
        <div style="display:flex; gap:10px;">
            <a href="/licitacoes/" class="btn btn-secondary">← Voltar para a Lista de Licitações</a>
            <a href="{html.escape(item.link_documento)}" target="_blank" rel="noopener" class="btn btn-primary">📄 Abrir PDF Original em Nova Aba</a>
        </div>
    </div>

    <div class="card">
        <h2 style="margin-top:0;">🏷️ Classificação / Situação Pós-Análise</h2>
        <p style="margin:0; color:#6b7280; font-size:0.92rem;">Selecione uma tag abaixo para classificar esta licitação:</p>
        <div class="tag-bar">
            {botoes_tags_html}
        </div>
        <div id="msgSalvo" style="display:none; margin-top:10px; color:#059669; font-weight:600; font-size:0.9rem;">
            ✓ Tag atualizada com sucesso!
        </div>
    </div>

    <div class="card">
        <h2 style="margin-top:0;">📋 Informações Gerais da Publicação</h2>
        <div class="meta-grid">
            <div class="meta-box">
                <div class="meta-label">Cidade / Município</div>
                <div class="meta-value">{html.escape(item.cidade)}</div>
            </div>
            <div class="meta-box">
                <div class="meta-label">Órgão Emissor</div>
                <div class="meta-value">{html.escape(item.orgao_emissor)}</div>
            </div>
            <div class="meta-box">
                <div class="meta-label">Data de Publicação</div>
                <div class="meta-value">{item.data_publicacao.strftime('%d/%m/%Y')}</div>
            </div>
            <div class="meta-box">
                <div class="meta-label">Modalidade</div>
                <div class="meta-value">{html.escape(item.modalidade)}</div>
            </div>
            <div class="meta-box">
                <div class="meta-label">Páginas de Referência no PDF</div>
                <div class="meta-value">Página(s): {html.escape(item.paginas_referencia)}</div>
            </div>
        </div>

        <h3>📝 Objeto Completo da Licitação (Extraído pela IA)</h3>
        <div class="objeto-box">{html.escape(item.objeto_licitacao)}</div>
    </div>

    <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
            <h3 style="margin:0;">📖 Visualização do Diário Oficial (Páginas de referência: {html.escape(item.paginas_referencia)})</h3>
            <a href="{html.escape(item.link_documento)}" target="_blank" rel="noopener" class="btn btn-secondary">Abrir em Tela Cheia ↗</a>
        </div>
        <iframe src="{html.escape(item.link_documento)}" style="width:100%; height:620px; border:1px solid #d1d5db; border-radius:8px;"></iframe>
    </div>

    <script>
        const csrfToken = "{csrf_token}";
        const metaPorTag = {{
            "": {{ label: "Sem Marcação", icon: "⚪", cls: "tag-none" }},
            "favorito": {{ label: "Favorito", icon: "⭐", cls: "tag-favorito" }},
            "em_analise": {{ label: "Em Análise", icon: "🔍", cls: "tag-analise" }},
            "sem_conformidade": {{ label: "Sem Conformidade", icon: "⚠️", cls: "tag-inconformidade" }},
            "ignorado": {{ label: "Ignorado", icon: "🚫", cls: "tag-ignorado" }}
        }};

        async function definirTagDetalhe(novaTag) {{
            const resp = await fetch("/licitacoes/{item.id}/tag/", {{
                method: "POST",
                headers: {{
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken,
                    "X-Requested-With": "XMLHttpRequest"
                }},
                body: JSON.stringify({{ tag_analise: novaTag }})
            }});

            if (resp.ok) {{
                document.querySelectorAll(".tag-bar .tag-btn").forEach(btn => btn.classList.remove("tag-btn-active"));
                const btnAtivo = document.getElementById("btn-tag-" + (novaTag || "vazio"));
                if (btnAtivo) btnAtivo.classList.add("tag-btn-active");

                const info = metaPorTag[novaTag] || metaPorTag[""];
                const badge = document.getElementById("badgeTagAtual");
                badge.className = "tag-btn " + info.cls;
                badge.textContent = info.icon + " " + info.label;

                const msg = document.getElementById("msgSalvo");
                msg.style.display = "block";
                setTimeout(() => {{ msg.style.display = "none"; }}, 2500);
            }}
        }}
    </script>
</body>
</html>"""
    return HttpResponse(pagina_html)


@login_required
@require_POST
def atualizar_tag_licitacao(request: HttpRequest, pk: int) -> JsonResponse:
    """Atualiza a `tag_analise` de uma licitação específica."""
    item = get_object_or_404(Licitacao, pk=pk)
    try:
        payload = json.loads(request.body.decode("utf-8"))
        nova_tag = str(payload.get("tag_analise", "")).strip()
    except Exception:
        nova_tag = request.POST.get("tag_analise", "").strip()

    tags_validas = set(Licitacao.TagAnalise.values)
    if nova_tag not in tags_validas:
        return JsonResponse(
            {"status": "erro", "mensagem": f"Tag inválida: {nova_tag}"},
            status=400,
        )

    item.tag_analise = nova_tag
    item.save(update_fields=["tag_analise"])
    return JsonResponse(
        {
            "status": "sucesso",
            "id": item.id,
            "tag_analise": item.tag_analise,
            "label": item.get_tag_analise_display(),
        }
    )


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


@login_required
@ensure_csrf_cookie
@require_GET
def pagina_upload_diarios(request: HttpRequest) -> HttpResponse:
    """
    Página protegida (`/licitacoes/upload/`) para envio manual de múltiplos PDFs:
    - Solicita URLs pré-assinadas (`POST /licitacoes/upload/presign/`) apenas com os nomes dos arquivos.
    - Faz o upload dos bytes (`PUT`) diretamente do navegador para o S3/Supabase Storage (ou stream local),
      sem sobrecarregar ou travar o servidor Django com processamento síncrono.
    - Informa ao usuário quando um arquivo foi renomeado automaticamente (`_2.pdf`, `_3.pdf`)
      para evitar conflito com PDFs já existentes em `novos_diarios/` ou `diarios_processados/`.
    """
    csrf_token = get_token(request)
    username_esc = html.escape(request.user.get_username())
    use_s3 = (not settings.USE_LOCAL_STORAGE) or bool(
        getattr(settings, "FORCE_S3_DIRECT_UPLOAD", False)
    )
    modo_arquitetura = (
        f"Upload Direto S3 Presigned URL -> Bucket '{settings.SUPABASE_STORAGE_BUCKET}/novos_diarios/'"
        if use_s3
        else f"Stream Direto Assíncrono -> Pasta '{settings.LOCAL_STORAGE_ROOT}/novos_diarios/'"
    )

    pagina_html = f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Upload de Múltiplos Diários Oficiais — EPSL</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 1080px; margin: 30px auto; padding: 0 20px; color: #1f2937; background: #f3f4f6; line-height: 1.5; }}
        .topbar {{ display: flex; justify-content: space-between; align-items: center; background: #ffffff; padding: 16px 24px; border-radius: 12px; border: 1px solid #e5e7eb; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }}
        .card {{ background: #ffffff; padding: 24px; border-radius: 12px; border: 1px solid #e5e7eb; margin-bottom: 20px; }}
        .btn {{ display: inline-block; padding: 9px 16px; border-radius: 8px; font-weight: 600; font-size: 0.92rem; text-decoration: none; border: none; cursor: pointer; }}
        .btn-primary {{ background: #2563eb; color: #fff; }}
        .btn-primary:hover {{ background: #1d4ed8; }}
        .btn-success {{ background: #059669; color: #fff; }}
        .btn-success:hover {{ background: #047857; }}
        .btn-secondary {{ background: #e5e7eb; color: #1f2937; }}
        .dropzone {{ border: 2px dashed #93c5fd; border-radius: 12px; padding: 40px 20px; text-align: center; background: #eff6ff; cursor: pointer; transition: all 0.2s; }}
        .dropzone.dragover {{ background: #dbeafe; border-color: #2563eb; }}
        .badge {{ display: inline-block; padding: 3px 8px; border-radius: 6px; font-size: 0.8rem; font-weight: 600; background: #dbeafe; color: #1e40af; }}
        .badge-renamed {{ background: #fef3c7; color: #92400e; }}
        .badge-ok {{ background: #d1fae5; color: #065f46; }}
        .badge-err {{ background: #fee2e2; color: #991b1b; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 0.9rem; }}
        th, td {{ border-bottom: 1px solid #e5e7eb; padding: 10px; text-align: left; vertical-align: middle; }}
        th {{ background: #f9fafb; }}
        .progress-wrap {{ width: 100%; background: #e5e7eb; border-radius: 999px; height: 10px; overflow: hidden; }}
        .progress-bar {{ height: 100%; background: #2563eb; width: 0%; transition: width 0.15s; }}
        .info-box {{ background: #f8fafc; border-left: 4px solid #2563eb; padding: 12px 16px; border-radius: 6px; margin-bottom: 18px; font-size: 0.9rem; }}
    </style>
</head>
<body>
    <div class="topbar">
        <div>
            <strong>📤 Upload Direto de Múltiplos Diários Oficiais (PDF)</strong>
            <span style="color:#6b7280; margin-left: 8px;">({username_esc})</span>
        </div>
        <div style="display:flex; gap:10px;">
            <a href="/licitacoes/" class="btn btn-secondary">← Voltar ao Painel de Licitações</a>
            <a href="/" class="btn btn-secondary">🏠 Página Inicial</a>
        </div>
    </div>

    <div class="card">
        <div class="info-box">
            <strong>⚡ Arquitetura Desacoplada (Zero Travamento):</strong> {html.escape(modo_arquitetura)}<br>
            • Os arquivos PDF são enviados em paralelo diretamente para a pasta <code>novos_diarios/</code> <strong>sem disparar a IA durante o upload</strong>.<br>
            • <strong>Proteção Anti-Colisão:</strong> Caso você envie um PDF com o mesmo nome de outro já existente em <code>novos_diarios/</code> ou em <code>diarios_processados/</code>, ele será renomeado automaticamente (ex: <code>diario_2.pdf</code>) para nunca sobrescrever documentos antigos.
        </div>

        <div id="dropzone" class="dropzone" onclick="document.getElementById('fileInput').click()">
            <h3 style="margin:0 0 8px 0; color:#1e40af;">Arraste e solte vários arquivos PDF aqui</h3>
            <p style="margin:0; color:#4b5563;">ou clique para selecionar múltiplos arquivos PDF do seu computador</p>
            <input type="file" id="fileInput" multiple accept=".pdf,application/pdf" style="display:none;">
        </div>

        <div style="margin-top: 16px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
            <span id="resumoFila" style="font-weight:600; color:#374151;">Nenhum arquivo selecionado.</span>
            <div style="display:flex; gap:10px;">
                <button id="btnIniciarUpload" class="btn btn-success" style="display:none;" onclick="iniciarUploads()">🚀 Enviar Todos os PDFs Agora</button>
                <button id="btnLimpar" class="btn btn-secondary" style="display:none;" onclick="limparFila()">Limpar Lista</button>
            </div>
        </div>

        <table id="tabelaArquivos" style="display:none;">
            <thead>
                <tr>
                    <th>Arquivo Selecionado</th>
                    <th>Nome Final no Storage</th>
                    <th>Tamanho</th>
                    <th style="width:220px;">Progresso</th>
                    <th>Status</th>
                </tr>
            </thead>
            <tbody id="corpoTabela"></tbody>
        </table>
    </div>

    <script>
        const csrfToken = "{csrf_token}";
        let filaArquivos = [];

        const dropzone = document.getElementById("dropzone");
        const fileInput = document.getElementById("fileInput");

        dropzone.addEventListener("dragover", (e) => {{
            e.preventDefault();
            dropzone.classList.add("dragover");
        }});
        dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
        dropzone.addEventListener("drop", (e) => {{
            e.preventDefault();
            dropzone.classList.remove("dragover");
            adicionarArquivos(e.dataTransfer.files);
        }});
        fileInput.addEventListener("change", (e) => adicionarArquivos(e.target.files));

        function formatarBytes(bytes) {{
            if (bytes < 1024) return bytes + " B";
            if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
            return (bytes / 1048576).toFixed(2) + " MB";
        }}

        function adicionarArquivos(fileList) {{
            for (const file of fileList) {{
                if (!file.name.toLowerCase().endsWith(".pdf")) continue;
                filaArquivos.push({{
                    file: file,
                    nomeFinal: "Aguardando verificação...",
                    foiRenomeado: false,
                    progresso: 0,
                    status: "Na fila",
                    concluido: false
                }});
            }}
            renderizarTabela();
        }}

        function limparFila() {{
            filaArquivos = [];
            fileInput.value = "";
            renderizarTabela();
        }}

        function renderizarTabela() {{
            const tabela = document.getElementById("tabelaArquivos");
            const corpo = document.getElementById("corpoTabela");
            const btnUpload = document.getElementById("btnIniciarUpload");
            const btnLimpar = document.getElementById("btnLimpar");
            const resumo = document.getElementById("resumoFila");

            if (filaArquivos.length === 0) {{
                tabela.style.display = "none";
                btnUpload.style.display = "none";
                btnLimpar.style.display = "none";
                resumo.textContent = "Nenhum arquivo selecionado.";
                return;
            }}

            tabela.style.display = "table";
            btnUpload.style.display = "inline-block";
            btnLimpar.style.display = "inline-block";
            resumo.textContent = `${{filaArquivos.length}} arquivo(s) PDF preparado(s) para envio.`;

            corpo.innerHTML = filaArquivos.map((item, idx) => {{
                const badgeRename = item.foiRenomeado
                    ? `<span class="badge badge-renamed" title="Renomeado automaticamente para evitar conflito">Renomeado: ${{item.nomeFinal}}</span>`
                    : `<code>${{item.nomeFinal}}</code>`;
                return `
                    <tr>
                        <td><strong>${{item.file.name}}</strong></td>
                        <td id="nome-final-${{idx}}">${{badgeRename}}</td>
                        <td>${{formatarBytes(item.file.size)}}</td>
                        <td>
                            <div class="progress-wrap">
                                <div id="prog-${{idx}}" class="progress-bar" style="width:${{item.progresso}}%;"></div>
                            </div>
                        </td>
                        <td id="status-${{idx}}"><span class="badge">${{item.status}}</span></td>
                    </tr>
                `;
            }}).join("");
        }}

        async function iniciarUploads() {{
            const pendentes = filaArquivos
                .map((item, index) => ({{ item, index }}))
                .filter(x => !x.item.concluido);

            if (pendentes.length === 0) return;

            document.getElementById("btnIniciarUpload").disabled = true;

            // 1. Pede ao Django apenas as URLs pré-assinadas e nomes únicos (sem enviar os bytes ainda)
            const resp = await fetch("/licitacoes/upload/presign/", {{
                method: "POST",
                headers: {{
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken
                }},
                body: JSON.stringify({{
                    filenames: pendentes.map(p => p.item.file.name)
                }})
            }});

            const dados = await resp.json();
            if (!resp.ok || dados.status !== "sucesso") {{
                alert("Erro ao gerar URLs de upload: " + (dados.mensagem || "Falha inesperada"));
                document.getElementById("btnIniciarUpload").disabled = false;
                return;
            }}

            // Atualiza os nomes finais (e se foram renomeados)
            dados.arquivos.forEach((info, i) => {{
                const idx = pendentes[i].index;
                filaArquivos[idx].nomeFinal = info.nome_final;
                filaArquivos[idx].foiRenomeado = info.foi_renomeado;
                filaArquivos[idx].uploadUrl = info.upload_url;
                filaArquivos[idx].method = info.method;
            }});
            renderizarTabela();

            // 2. Faz o envio direto em paralelo (máximo 3 simultâneos para estabilidade de rede)
            const limiteConcorrencia = 3;
            let cursor = 0;

            async function worker() {{
                while (cursor < pendentes.length) {{
                    const atual = pendentes[cursor++];
                    await enviarArquivoDireto(atual.index);
                }}
            }}

            const workers = [];
            for (let i = 0; i < Math.min(limiteConcorrencia, pendentes.length); i++) {{
                workers.push(worker());
            }}
            await Promise.all(workers);
            document.getElementById("btnIniciarUpload").disabled = false;
        }}

        function enviarArquivoDireto(idx) {{
            return new Promise((resolve) => {{
                const item = filaArquivos[idx];
                const xhr = new XMLHttpRequest();
                xhr.open(item.method || "PUT", item.uploadUrl, true);
                xhr.setRequestHeader("Content-Type", "application/pdf");
                if (item.uploadUrl.startsWith("/")) {{
                    xhr.setRequestHeader("X-CSRFToken", csrfToken);
                }}

                xhr.upload.onprogress = (e) => {{
                    if (e.lengthComputable) {{
                        const pct = Math.round((e.loaded / e.total) * 100);
                        item.progresso = pct;
                        const bar = document.getElementById(`prog-${{idx}}`);
                        const st = document.getElementById(`status-${{idx}}`);
                        if (bar) bar.style.width = pct + "%";
                        if (st) st.innerHTML = `<span class="badge">Enviando ${{pct}}%</span>`;
                    }}
                }};

                xhr.onload = () => {{
                    const st = document.getElementById(`status-${{idx}}`);
                    const bar = document.getElementById(`prog-${{idx}}`);
                    if (xhr.status >= 200 && xhr.status < 300) {{
                        item.progresso = 100;
                        item.concluido = true;
                        item.status = "Concluído";
                        if (bar) {{
                            bar.style.width = "100%";
                            bar.style.background = "#059669";
                        }}
                        if (st) st.innerHTML = `<span class="badge badge-ok">✓ Enviado</span>`;
                    }} else {{
                        item.status = "Erro (" + xhr.status + ")";
                        if (st) st.innerHTML = `<span class="badge badge-err">Falha (${{xhr.status}})</span>`;
                    }}
                    resolve();
                }};

                xhr.onerror = () => {{
                    const st = document.getElementById(`status-${{idx}}`);
                    item.status = "Erro de Rede/CORS";
                    if (st) st.innerHTML = `<span class="badge badge-err">Erro de Rede</span>`;
                    resolve();
                }};

                xhr.send(item.file);
            }});
        }}
    </script>
</body>
</html>"""
    return HttpResponse(pagina_html)


@login_required
@require_POST
def gerar_urls_upload_presign(request: HttpRequest) -> JsonResponse:
    """
    Recebe uma lista JSON de nomes de arquivos (`{"filenames": ["a.pdf", "b.pdf"]}`)
    e retorna as URLs de upload direto (S3 Presigned PUT ou Stream Local) já com
    verificação anti-colisão (`_2.pdf`, `_3.pdf`).
    """
    try:
        payload = json.loads(request.body.decode("utf-8"))
        filenames = payload.get("filenames", [])
        if not isinstance(filenames, list) or not filenames:
            return JsonResponse(
                {"status": "erro", "mensagem": "Informe uma lista de arquivos PDF válida."},
                status=400,
            )

        instrucoes = gerar_lote_urls_upload([str(f) for f in filenames])
        return JsonResponse({"status": "sucesso", "arquivos": instrucoes})
    except Exception as exc:
        return JsonResponse(
            {"status": "erro", "mensagem": str(exc)},
            status=500,
        )


@login_required
@require_http_methods(["PUT"])
def receber_stream_pdf_local(request: HttpRequest, filename: str) -> JsonResponse:
    """
    Endpoint de escrita direta em stream para o modo de teste local (`storage_local/novos_diarios/`).
    Grava o corpo binário da requisição PUT direto no disco sem acionar processamento síncrono.
    """
    nome_seguro = sanitizar_nome_pdf(filename)
    input_dir = (
        Path(settings.LOCAL_STORAGE_ROOT)
        / settings.SUPABASE_INPUT_PREFIX.strip("/")
    )
    input_dir.mkdir(parents=True, exist_ok=True)
    dest_path = input_dir / nome_seguro

    with dest_path.open("wb") as f:
        while True:
            chunk = request.read(65536)
            if not chunk:
                break
            f.write(chunk)

    return JsonResponse(
        {
            "status": "sucesso",
            "arquivo": nome_seguro,
            "caminho": str(dest_path),
        },
        status=201,
    )
