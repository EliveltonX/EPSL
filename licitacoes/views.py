import html
import io
import json
import re
import threading
from pathlib import Path

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.management import call_command
from django.db import close_old_connections
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
from licitacoes.pipeline_progress import (
    iniciar_progresso,
    limpar_historico_erros_arquivos,
    obter_snapshot_progresso,
)
from licitacoes.server_monitor import obter_snapshot_completo
from licitacoes.storage_upload import gerar_lote_urls_upload, sanitizar_nome_pdf


def _extrair_numeros_paginas(paginas_ref: str) -> list[int]:
    """
    Extrai os números inteiros de página de uma string como '14, 15' ou 'Pág. 8 e 9',
    preservando a ordem e removendo duplicatas.
    """
    vistos: set[int] = set()
    resultado: list[int] = []
    for match in re.findall(r"\d+", paginas_ref or ""):
        num = int(match)
        if num > 0 and num not in vistos:
            vistos.add(num)
            resultado.append(num)
    return resultado


def _url_pdf_com_pagina(url_base: str, pagina: int) -> str:
    """
    Adiciona o fragmento padrão PDF Open Parameters (`#page=N`) ao link do documento
    para que o visualizador de PDF do navegador abra diretamente na página da licitação.
    """
    base_sem_hash = (url_base or "").split("#")[0]
    if not base_sem_hash:
        return ""
    if pagina > 0:
        return f"{base_sem_hash}#page={pagina}"
    return base_sem_hash


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
                <a href="/licitacoes/servidor/" class="btn btn-secondary">🖥️ Monitor do Servidor & Storage</a>
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
        pags_item = _extrair_numeros_paginas(item.paginas_referencia)
        pag_ini_item = pags_item[0] if pags_item else 1
        link_pdf_com_pag = _url_pdf_com_pagina(item.link_documento, pag_ini_item)
        rotulo_abrir_pdf = (
            f"Abrir PDF (Pág. {pag_ini_item})" if pags_item else "Abrir PDF"
        )
        linhas_html += f"""
        <tr class="clickable-row" onclick="window.location.href='/licitacoes/{item.id}/'" title="Clique na linha para abrir os detalhes completos desta licitação já na página {pag_ini_item} do PDF">
            <td onclick="event.stopPropagation();">{select_tag_html}</td>
            <td><strong>{html.escape(item.cidade)}</strong></td>
            <td>{html.escape(item.orgao_emissor)}</td>
            <td>{item.data_publicacao.strftime('%d/%m/%Y')}</td>
            <td><span class="badge">{html.escape(item.modalidade)}</span></td>
            <td>{html.escape(objeto_resumo)}</td>
            <td>{html.escape(item.paginas_referencia)}</td>
            <td onclick="event.stopPropagation();">
                <a href="{html.escape(link_pdf_com_pag)}" target="_blank" rel="noopener">{rotulo_abrir_pdf}</a>
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
            <a href="/licitacoes/servidor/" class="btn btn-secondary">🖥️ Monitor do Servidor</a>
            <a href="/admin/" class="btn btn-secondary">⚙️ Admin</a>
            <form method="post" action="/licitacoes/processar-web/" style="margin:0;" onsubmit="const b=this.querySelector('button'); b.disabled=true; b.textContent='⏳ Iniciando Análise...';">
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
    de uma licitação, marcação de sua Tag pós-análise (`Favorito`, `Em Análise`,
    `Sem Conformidade`, `Ignorado`) e abertura automática do PDF incorporado
    diretamente na página correta da licitação (`#page=N`).
    """
    item = get_object_or_404(Licitacao, pk=pk)
    csrf_token = get_token(request)
    meta_atual = TAG_META.get(item.tag_analise, TAG_META[""])

    paginas_lista = _extrair_numeros_paginas(item.paginas_referencia)
    pagina_inicial = paginas_lista[0] if paginas_lista else 1
    url_base_sem_hash = (item.link_documento or "").split("#")[0]
    url_pdf_inicial = _url_pdf_com_pagina(url_base_sem_hash, pagina_inicial)

    botoes_paginas_html = ""
    if paginas_lista:
        for idx_p, num_p in enumerate(paginas_lista):
            cls_ativo = "page-btn-active" if idx_p == 0 else ""
            icone_p = "🎯" if idx_p == 0 else "📄"
            botoes_paginas_html += (
                f'<button type="button" id="btn-pag-{num_p}" '
                f'class="page-btn {cls_ativo}" onclick="irParaPaginaPdf({num_p})">'
                f"{icone_p} Pág. {num_p}</button>"
            )
    else:
        botoes_paginas_html = (
            '<button type="button" id="btn-pag-1" class="page-btn page-btn-active" '
            'onclick="irParaPaginaPdf(1)">📄 Pág. 1</button>'
        )

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
        .pdf-nav-bar {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; background: #eff6ff; border: 1px solid #bfdbfe; padding: 10px 14px; border-radius: 10px; margin-bottom: 12px; }}
        .page-btn {{ padding: 6px 12px; border-radius: 8px; border: 1px solid #93c5fd; background: #ffffff; color: #1e40af; font-weight: 700; font-size: 0.86rem; cursor: pointer; transition: all 0.15s; }}
        .page-btn:hover {{ background: #dbeafe; }}
        .page-btn-active {{ background: #2563eb !important; color: #ffffff !important; border-color: #1d4ed8 !important; }}
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
            <a id="linkTopoPdf" href="{html.escape(url_pdf_inicial)}" target="_blank" rel="noopener" class="btn btn-primary">📄 Abrir PDF Original (Pág. {pagina_inicial})</a>
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
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; flex-wrap:wrap; gap:10px;">
            <h3 style="margin:0;">📖 Visualização do Diário Oficial — Aberto na Página <span id="lblPaginaAtual">{pagina_inicial}</span> (Ref: {html.escape(item.paginas_referencia)})</h3>
            <a id="linkTelaCheiaPdf" href="{html.escape(url_pdf_inicial)}" target="_blank" rel="noopener" class="btn btn-secondary">Abrir Página {pagina_inicial} em Tela Cheia ↗</a>
        </div>

        <div class="pdf-nav-bar">
            <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                <span style="font-size:0.88rem; font-weight:700; color:#1e3a8a;">Ir para página da licitação:</span>
                {botoes_paginas_html}
            </div>
            <div style="display:flex; align-items:center; gap:6px;">
                <label for="inputIrPagina" style="font-size:0.85rem; color:#1e3a8a; font-weight:600;">Pular p/ pág:</label>
                <input type="number" id="inputIrPagina" min="1" value="{pagina_inicial}" style="width:72px; padding:5px 8px; border:1px solid #93c5fd; border-radius:6px; font-weight:600;">
                <button type="button" class="page-btn" onclick="irParaPaginaPdf(parseInt(document.getElementById('inputIrPagina').value || '1', 10))">Ir</button>
            </div>
        </div>

        <div id="containerPdfEmbed">
            <iframe id="iframePdfDocumento" src="{html.escape(url_pdf_inicial)}" style="width:100%; height:650px; border:1px solid #d1d5db; border-radius:8px;"></iframe>
        </div>
    </div>

    <script>
        const csrfToken = "{csrf_token}";
        const urlBasePdf = {json.dumps(url_base_sem_hash)};
        const metaPorTag = {{
            "": {{ label: "Sem Marcação", icon: "⚪", cls: "tag-none" }},
            "favorito": {{ label: "Favorito", icon: "⭐", cls: "tag-favorito" }},
            "em_analise": {{ label: "Em Análise", icon: "🔍", cls: "tag-analise" }},
            "sem_conformidade": {{ label: "Sem Conformidade", icon: "⚠️", cls: "tag-inconformidade" }},
            "ignorado": {{ label: "Ignorado", icon: "🚫", cls: "tag-ignorado" }}
        }};

        function irParaPaginaPdf(numPagina) {{
            const pag = Math.max(1, parseInt(numPagina, 10) || 1);
            const novaUrl = urlBasePdf + "#page=" + pag;

            document.getElementById("lblPaginaAtual").textContent = pag;
            document.getElementById("inputIrPagina").value = pag;

            const linkTopo = document.getElementById("linkTopoPdf");
            if (linkTopo) {{
                linkTopo.href = novaUrl;
                linkTopo.textContent = `📄 Abrir PDF Original (Pág. ${{pag}})`;
            }}
            const linkCheia = document.getElementById("linkTelaCheiaPdf");
            if (linkCheia) {{
                linkCheia.href = novaUrl;
                linkCheia.textContent = `Abrir Página ${{pag}} em Tela Cheia ↗`;
            }}

            document.querySelectorAll(".pdf-nav-bar .page-btn").forEach(b => b.classList.remove("page-btn-active"));
            const btnPag = document.getElementById("btn-pag-" + pag);
            if (btnPag) btnPag.classList.add("page-btn-active");

            // Recria o elemento iframe para forçar o leitor nativo de PDF do navegador a saltar para #page=N
            const container = document.getElementById("containerPdfEmbed");
            if (container) {{
                container.innerHTML = `<iframe id="iframePdfDocumento" src="${{novaUrl}}" style="width:100%; height:650px; border:1px solid #d1d5db; border-radius:8px;"></iframe>`;
            }}
        }}

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


def _executar_pipeline_em_background() -> None:
    """Executa o management command `processar_diarios` em thread separada atualizando o progresso ao vivo."""
    close_old_connections()
    out = io.StringIO()
    err = io.StringIO()
    try:
        call_command("processar_diarios", stdout=out, stderr=err)
    except Exception:
        pass
    finally:
        close_old_connections()


@login_required
@require_GET
def status_processamento_json(request: HttpRequest) -> JsonResponse:
    """Retorna o estado atual da barra de progresso, logs, erros e uso das cotas Gemini."""
    return JsonResponse({"status": "sucesso", "progresso": obter_snapshot_progresso()})


@login_required
@require_http_methods(["GET", "POST"])
def acionar_processamento_web(request: HttpRequest) -> HttpResponse:
    """
    Inicia o processamento de novos diários em background (quando chamado via POST) e
    exibe imediatamente uma tela interativa com Barra de Carregamento ao Vivo (0% a 100%),
    contadores em tempo real, tabela de cotas dos modelos Gemini (RPM/RPD) e console de logs/erros.
    """
    snap_atual = obter_snapshot_progresso()
    if request.method == "POST" and not snap_atual.get("em_execucao"):
        iniciar_progresso(total_arquivos=0)
        worker = threading.Thread(
            target=_executar_pipeline_em_background,
            daemon=True,
            name="epsl-processar-diarios-worker",
        )
        worker.start()
        snap_atual = obter_snapshot_progresso()

    snap_inicial_json = json.dumps(snap_atual)

    return HttpResponse(
        f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Processando Diários Oficiais — EPSL</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 1020px; margin: 30px auto; padding: 0 20px; background: #f3f4f6; color: #1f2937; line-height: 1.5; }}
        .card {{ background: #fff; padding: 24px; border-radius: 12px; border: 1px solid #e5e7eb; margin-bottom: 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
        .progress-outer {{ width: 100%; height: 26px; background: #e5e7eb; border-radius: 999px; overflow: hidden; margin: 14px 0 8px 0; position: relative; }}
        .progress-inner {{ height: 100%; width: 5%; background: linear-gradient(90deg, #2563eb, #3b82f6); border-radius: 999px; transition: width 0.35s ease, background 0.3s ease; display: flex; align-items: center; justify-content: center; color: #fff; font-size: 0.82rem; font-weight: 700; }}
        .progress-inner.running {{ background: linear-gradient(90deg, #2563eb 25%, #60a5fa 50%, #2563eb 75%); background-size: 200% 100%; animation: shimmer 1.8s infinite linear; }}
        @keyframes shimmer {{ 0% {{ background-position: 200% 0; }} 100% {{ background-position: -200% 0; }} }}
        .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin: 16px 0; }}
        .kpi-box {{ background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px 14px; }}
        .kpi-label {{ font-size: 0.78rem; color: #6b7280; font-weight: 700; text-transform: uppercase; }}
        .kpi-val {{ font-size: 1.35rem; font-weight: 800; color: #111827; margin-top: 2px; }}
        pre {{ background: #0f172a; color: #e2e8f0; padding: 16px; border-radius: 10px; overflow-x: auto; white-space: pre-wrap; max-height: 380px; overflow-y: auto; font-size: 0.86rem; margin-top: 8px; }}
        .err-box {{ background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; padding: 14px 16px; border-radius: 10px; margin-top: 14px; display: none; }}
        .btn {{ display: inline-block; padding: 10px 16px; background: #2563eb; color: #fff; border-radius: 8px; text-decoration: none; font-weight: 600; border: none; cursor: pointer; }}
        .btn-secondary {{ background: #4b5563; color: #fff; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 0.86rem; margin-top: 8px; }}
        th, td {{ border-bottom: 1px solid #e5e7eb; padding: 8px 10px; text-align: left; }}
        th {{ background: #f9fafb; font-size: 0.76rem; text-transform: uppercase; color: #6b7280; }}
        .badge {{ display: inline-block; padding: 3px 8px; border-radius: 6px; font-size: 0.76rem; font-weight: 700; background: #dbeafe; color: #1e40af; }}
        .badge-ok {{ background: #d1fae5; color: #065f46; }}
        .badge-warn {{ background: #fef3c7; color: #92400e; }}
    </style>
</head>
<body>
    <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
            <h2 id="tituloStatus" style="margin:0;">⏳ Analisando Diários Oficiais em Tempo Real...</h2>
            <span id="badgeModeloAtivo" class="badge">IA: Carregando...</span>
        </div>

        <div class="progress-outer">
            <div id="barraProgresso" class="progress-inner running" style="width:5%;">5%</div>
        </div>
        <div style="display:flex; justify-content:space-between; font-size:0.9rem; color:#4b5563; font-weight:600;">
            <span id="lblArquivoAtual">Preparando fila...</span>
            <span id="lblEtapaAtual">Conectando ao Storage...</span>
        </div>

        <div class="kpi-grid">
            <div class="kpi-box">
                <div class="kpi-label">📄 Arquivos Processados</div>
                <div class="kpi-val" id="kpiArquivos">0 / 0</div>
            </div>
            <div class="kpi-box">
                <div class="kpi-label">🏛️ Licitações Extraídas</div>
                <div class="kpi-val" id="kpiLicitacoes" style="color:#059669;">0</div>
            </div>
            <div class="kpi-box">
                <div class="kpi-label">🛡️ Duplicados Bloqueados</div>
                <div class="kpi-val" id="kpiDuplicados" style="color:#d97706;">0</div>
            </div>
            <div class="kpi-box">
                <div class="kpi-label">⚠️ Erros Detectados</div>
                <div class="kpi-val" id="kpiErros" style="color:#dc2626;">0</div>
            </div>
        </div>

        <div id="caixaErros" class="err-box">
            <strong>⚠️ Erros Registrados durante a Execução:</strong>
            <pre id="preErros" style="background:#7f1d1d; color:#fecaca; max-height:180px; margin-top:8px;"></pre>
        </div>

        <h3 style="margin-bottom:4px;">🖥️ Console de Execução ao Vivo:</h3>
        <pre id="preLogs">Iniciando pipeline...</pre>

        <div style="margin-top: 18px; display:flex; gap:10px; flex-wrap:wrap;">
            <a href="/licitacoes/" class="btn">📄 Voltar ao Painel de Licitações</a>
            <a href="/licitacoes/upload/" class="btn btn-secondary" style="background:#059669;">📤 Upload de PDFs</a>
            <a href="/" class="btn btn-secondary">🏠 Página Inicial</a>
        </div>
    </div>

    <div class="card">
        <h3 style="margin-top:0;">🤖 Balanceador de Cotas Google Gemini (Ordem de Fallback por Limite RPM/RPD)</h3>
        <table>
            <thead>
                <tr>
                    <th>Ordem</th>
                    <th>Modelo Gemini</th>
                    <th>Uso por Minuto (RPM)</th>
                    <th>Tokens/Min (TPM)</th>
                    <th>Uso Diário (RPD)</th>
                    <th>Status</th>
                </tr>
            </thead>
            <tbody id="tbodyCotas"></tbody>
        </table>
    </div>

    <script>
        let estado = {snap_inicial_json};
        let timerPolling = null;

        function atualizarTela(p) {{
            const pct = Math.max(0, Math.min(100, p.percentual || 0));
            const barra = document.getElementById("barraProgresso");
            barra.style.width = Math.max(pct, 4) + "%";
            barra.textContent = pct + "%";

            document.getElementById("lblArquivoAtual").textContent = p.arquivo_atual || "Aguardando...";
            document.getElementById("lblEtapaAtual").textContent = p.etapa_atual || "";
            document.getElementById("badgeModeloAtivo").textContent = "Modelo Ativo: " + (p.modelo_ativo || "gemini-3.5-flash-lite");

            document.getElementById("kpiArquivos").textContent = `${{p.arquivos_processados || 0}} / ${{p.total_arquivos || 0}}`;
            document.getElementById("kpiLicitacoes").textContent = p.licitacoes_extraidas || 0;
            document.getElementById("kpiDuplicados").textContent = p.duplicados_bloqueados || 0;
            document.getElementById("kpiErros").textContent = p.erros_qtd || 0;

            const preLogs = document.getElementById("preLogs");
            const estavaNoFinal = (preLogs.scrollHeight - preLogs.scrollTop - preLogs.clientHeight) < 40;
            preLogs.textContent = (p.logs && p.logs.length > 0) ? p.logs.join("\\n") : "Aguardando saída...";
            if (estavaNoFinal) preLogs.scrollTop = preLogs.scrollHeight;

            const caixaErros = document.getElementById("caixaErros");
            if (p.erros && p.erros.length > 0) {{
                caixaErros.style.display = "block";
                document.getElementById("preErros").textContent = p.erros.join("\\n");
            }} else {{
                caixaErros.style.display = "none";
            }}

            if (p.cotas_modelos) {{
                document.getElementById("tbodyCotas").innerHTML = p.cotas_modelos.map((c, idx) => {{
                    const stBadge = c.disponivel
                        ? `<span class="badge badge-ok">Disponível</span>`
                        : `<span class="badge badge-warn">Em Cooldown (${{c.em_cooldown_seg}}s)</span>`;
                    return `
                        <tr>
                            <td><strong>#${{idx + 1}}</strong></td>
                            <td><code>${{c.model}}</code></td>
                            <td><strong>${{c.rpm_usado}}</strong> / ${{c.rpm_limite}} RPM</td>
                            <td>${{(c.tpm_limite / 1000)}}K TPM</td>
                            <td><strong>${{c.rpd_usado}}</strong> / ${{c.rpd_limite}} RPD</td>
                            <td>${{stBadge}}</td>
                        </tr>
                    `;
                }}).join("");
            }}

            const titulo = document.getElementById("tituloStatus");
            if (!p.em_execucao && p.concluido) {{
                barra.classList.remove("running");
                if (p.erros_qtd > 0) {{
                    barra.style.background = "#d97706";
                    titulo.textContent = "⚠️ Processamento Finalizado com Avisos/Erros";
                }} else {{
                    barra.style.background = "#059669";
                    titulo.textContent = "✅ Processamento Concluído com Sucesso!";
                }}
                if (timerPolling) {{
                    clearInterval(timerPolling);
                    timerPolling = null;
                }}
            }} else {{
                barra.classList.add("running");
                titulo.textContent = "⏳ Analisando Diários Oficiais em Tempo Real...";
            }}
        }}

        async function consultarStatus() {{
            try {{
                const r = await fetch("/licitacoes/processar-web/status/", {{
                    headers: {{ "X-Requested-With": "XMLHttpRequest" }}
                }});
                if (!r.ok) return;
                const d = await r.json();
                if (d.status === "sucesso" && d.progresso) {{
                    atualizarTela(d.progresso);
                }}
            }} catch (e) {{}}
        }}

        atualizarTela(estado);
        if (estado.em_execucao || !estado.concluido) {{
            timerPolling = setInterval(consultarStatus, 1000);
        }}
    </script>
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
            • <strong>🛡️ Proteção Anti-Duplicata (Economia de IA):</strong> O sistema calcula a impressão digital criptográfica (<code>SHA-256</code>) de cada PDF antes do envio. Se dois funcionários enviarem o <strong>mesmo Diário Oficial (mesmo com nomes de arquivo diferentes)</strong>, o sistema reconhece que o conteúdo é idêntico e bloqueia o reenvio automaticamente!<br>
            • <strong>Proteção Anti-Colisão de Nomes:</strong> Caso você envie um PDF inédito que por coincidência tenha o mesmo nome de outro arquivo antigo, ele será renomeado automaticamente (ex: <code>diario_2.pdf</code>).
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

        async function calcularSha256Arquivo(file) {{
            try {{
                if (!window.crypto || !window.crypto.subtle) return "";
                const buffer = await file.arrayBuffer();
                const hashBuffer = await window.crypto.subtle.digest("SHA-256", buffer);
                const hashArray = Array.from(new Uint8Array(hashBuffer));
                return hashArray.map(b => b.toString(16).padStart(2, "0")).join("");
            }} catch (e) {{
                return "";
            }}
        }}

        function adicionarArquivos(fileList) {{
            for (const file of fileList) {{
                if (!file.name.toLowerCase().endsWith(".pdf")) continue;
                filaArquivos.push({{
                    file: file,
                    sha256: "",
                    nomeFinal: "Aguardando verificação...",
                    foiRenomeado: false,
                    duplicado: false,
                    motivoDuplicado: "",
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
                let badgeDestino = `<code>${{item.nomeFinal}}</code>`;
                if (item.duplicado) {{
                    badgeDestino = `<span class="badge badge-err" title="${{item.motivoDuplicado}}">🚫 ${{item.motivoDuplicado}}</span>`;
                }} else if (item.foiRenomeado) {{
                    badgeDestino = `<span class="badge badge-renamed" title="Renomeado automaticamente para evitar conflito">Renomeado: ${{item.nomeFinal}}</span>`;
                }}
                const badgeStatusCls = item.duplicado ? "badge badge-err" : (item.concluido ? "badge badge-ok" : "badge");
                return `
                    <tr>
                        <td><strong>${{item.file.name}}</strong></td>
                        <td id="nome-final-${{idx}}">${{badgeDestino}}</td>
                        <td>${{formatarBytes(item.file.size)}}</td>
                        <td>
                            <div class="progress-wrap">
                                <div id="prog-${{idx}}" class="progress-bar" style="width:${{item.progresso}}%; ${{item.duplicado ? 'background:#ef4444;' : ''}}"></div>
                            </div>
                        </td>
                        <td id="status-${{idx}}"><span class="${{badgeStatusCls}}">${{item.status}}</span></td>
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

            // 1. Calcula o hash SHA-256 de cada arquivo no navegador e pede as URLs pré-assinadas ao Django
            for (const p of pendentes) {{
                if (!p.item.sha256) {{
                    p.item.status = "Verificando autenticidade (SHA-256)...";
                }}
            }}
            renderizarTabela();

            for (const p of pendentes) {{
                if (!p.item.sha256) {{
                    p.item.sha256 = await calcularSha256Arquivo(p.item.file);
                }}
            }}

            const resp = await fetch("/licitacoes/upload/presign/", {{
                method: "POST",
                headers: {{
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken
                }},
                body: JSON.stringify({{
                    arquivos: pendentes.map(p => ({{
                        filename: p.item.file.name,
                        sha256: p.item.sha256
                    }}))
                }})
            }});

            const dados = await resp.json();
            if (!resp.ok || dados.status !== "sucesso") {{
                alert("Erro ao gerar URLs de upload: " + (dados.mensagem || "Falha inesperada"));
                document.getElementById("btnIniciarUpload").disabled = false;
                return;
            }}

            // Atualiza os nomes finais, renomeações e bloqueia arquivos duplicados
            const aptosParaEnvio = [];
            dados.arquivos.forEach((info, i) => {{
                const idx = pendentes[i].index;
                filaArquivos[idx].nomeFinal = info.nome_final;
                filaArquivos[idx].foiRenomeado = info.foi_renomeado;
                filaArquivos[idx].duplicado = Boolean(info.duplicado);
                filaArquivos[idx].motivoDuplicado = info.motivo_duplicado || "";
                filaArquivos[idx].uploadUrl = info.upload_url;
                filaArquivos[idx].method = info.method;

                if (info.duplicado) {{
                    filaArquivos[idx].concluido = true;
                    filaArquivos[idx].progresso = 100;
                    filaArquivos[idx].status = "Duplicado Bloqueado";
                }} else {{
                    aptosParaEnvio.push(pendentes[i]);
                }}
            }});
            renderizarTabela();

            // 2. Faz o envio direto em paralelo apenas dos arquivos inéditos (máximo 3 simultâneos)
            const limiteConcorrencia = 3;
            let cursor = 0;

            async function worker() {{
                while (cursor < aptosParaEnvio.length) {{
                    const atual = aptosParaEnvio[cursor++];
                    await enviarArquivoDireto(atual.index);
                }}
            }}

            const workers = [];
            for (let i = 0; i < Math.min(limiteConcorrencia, aptosParaEnvio.length); i++) {{
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
                        let detalheS3 = "";
                        try {{
                            const matchMsg = (xhr.responseText || "").match(/<Message>([^<]+)<\\/Message>/i);
                            if (matchMsg && matchMsg[1]) detalheS3 = ": " + matchMsg[1];
                        }} catch (e) {{}}
                        item.status = "Erro (" + xhr.status + ")" + detalheS3;
                        if (st) st.innerHTML = `<span class="badge badge-err" title="${{item.status}}">Falha (${{xhr.status}})${{detalheS3}}</span>`;
                    }}
                    resolve();
                }};

                xhr.onerror = () => {{
                    const st = document.getElementById(`status-${{idx}}`);
                    item.status = "Erro de Rede/CORS (S3)";
                    if (st) st.innerHTML = `<span class="badge badge-err">Erro S3/CORS</span>`;
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
    Recebe uma lista JSON de arquivos (`{"arquivos": [{"filename": "a.pdf", "sha256": "..."}]}`
    ou `{"filenames": ["a.pdf", "b.pdf"]}`) e retorna as URLs de upload direto
    já com verificação anti-duplicata (SHA-256) e anti-colisão de nomes (`_2.pdf`, `_3.pdf`).
    """
    try:
        payload = json.loads(request.body.decode("utf-8"))
        lista_entrada = payload.get("arquivos") or payload.get("filenames") or []
        if not isinstance(lista_entrada, list) or not lista_entrada:
            return JsonResponse(
                {"status": "erro", "mensagem": "Informe uma lista de arquivos PDF válida."},
                status=400,
            )

        instrucoes = gerar_lote_urls_upload(lista_entrada)
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
    Endpoint de escrita direta em stream exclusivo para o modo de teste local (`APP_ENV=test`).
    Bloqueado em produção (`APP_ENV=deploy`), onde os arquivos vão exclusivamente para o Supabase Storage S3.
    """
    if not settings.USE_LOCAL_STORAGE:
        return JsonResponse(
            {
                "status": "erro",
                "mensagem": "Armazenamento local desativado em produção (APP_ENV=deploy). Use exclusivamente o Supabase Storage S3.",
            },
            status=403,
        )

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


@login_required
@require_GET
def metricas_servidor_json(request: HttpRequest) -> JsonResponse:
    """
    Endpoint JSON (`/licitacoes/servidor/metricas/`) consumido a cada 3 segundos
    pela Dashboard do Servidor para atualizar CPU, RAM, Discos, Processos, Banco,
    Supabase Storage, Fila de Artigos Aguardando Análise e Histórico de Erros.
    Aceita `?refresh_storage=1` para forçar recontagem imediata do bucket S3 / pasta local.
    """
    forcar_storage = request.GET.get("refresh_storage") == "1"
    dados = obter_snapshot_completo(forcar_storage=forcar_storage)
    return JsonResponse({"status": "sucesso", "dados": dados})


@login_required
@require_POST
def limpar_erros_analise_json(request: HttpRequest) -> JsonResponse:
    """Limpa a lista persistente de artigos que apresentaram erro de análise."""
    limpar_historico_erros_arquivos()
    return JsonResponse({"status": "sucesso"})


@login_required
@ensure_csrf_cookie
@require_GET
def painel_servidor(request: HttpRequest) -> HttpResponse:
    """
    Dashboard de Monitoramento do Servidor, Storage e Fila de Análise IA (`/licitacoes/servidor/`)
    inspirada no Gerenciador de Tarefas do Windows:
    - Status em tempo real da Análise IA, Agendador Automático (Cron 10 min), Super-Batching (250K TPM),
      lista de Artigos Aguardando Análise (`novos_diarios/`) e Relatório de Artigos com Erro.
    - Gráficos históricos em tempo real (CPU % e RAM %) e uso individual por núcleo (vCPU).
    - Consumo de Memória RAM, Swap, Rede (TX/RX e velocidade instantânea) e Uptime.
    - Armazenamento físico do Servidor (partições e discos locais).
    - Armazenamento interno do Supabase Storage / Local (`novos_diarios/`, `diarios_processados/{YYYY}/{MM}/`)
      e tamanho físico do Banco de Dados (PostgreSQL/SQLite).
    - Tabela de Top Processos ativos no servidor/container.
    """
    snapshot_inicial = json.dumps(obter_snapshot_completo(forcar_storage=False))
    csrf_token = get_token(request)
    username_esc = html.escape(request.user.get_username())

    pagina_html = f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Monitor do Servidor & Storage — EPSL</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 1280px; margin: 24px auto; padding: 0 20px; color: #1f2937; background: #0f172a; line-height: 1.5; }}
        .topbar {{ display: flex; justify-content: space-between; align-items: center; background: #1e293b; color: #f8fafc; padding: 16px 24px; border-radius: 12px; border: 1px solid #334155; margin-bottom: 18px; flex-wrap: wrap; gap: 12px; }}
        .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 18px; }}
        .grid-4 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; margin-bottom: 18px; }}
        @media (max-width: 900px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}
        .card {{ background: #1e293b; color: #f8fafc; padding: 20px; border-radius: 12px; border: 1px solid #334155; box-shadow: 0 4px 12px rgba(0,0,0,0.2); }}
        .card h2, .card h3 {{ margin-top: 0; color: #f8fafc; font-size: 1.08rem; display: flex; justify-content: space-between; align-items: center; }}
        .kpi-card {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 16px 18px; color: #f8fafc; }}
        .kpi-label {{ font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; color: #94a3b8; font-weight: 700; }}
        .kpi-value {{ font-size: 1.55rem; font-weight: 800; margin-top: 4px; color: #38bdf8; }}
        .kpi-sub {{ font-size: 0.82rem; color: #cbd5e1; margin-top: 4px; }}
        .btn {{ display: inline-block; padding: 8px 14px; border-radius: 8px; font-weight: 600; font-size: 0.88rem; text-decoration: none; border: none; cursor: pointer; }}
        .btn-primary {{ background: #2563eb; color: #fff; }}
        .btn-primary:hover {{ background: #1d4ed8; }}
        .btn-secondary {{ background: #334155; color: #f8fafc; border: 1px solid #475569; }}
        .btn-secondary:hover {{ background: #475569; }}
        .btn-success {{ background: #059669; color: #fff; }}
        .bar-bg {{ width: 100%; height: 12px; background: #0f172a; border-radius: 999px; overflow: hidden; border: 1px solid #334155; margin: 6px 0; }}
        .bar-fill {{ height: 100%; border-radius: 999px; transition: width 0.35s ease; background: #38bdf8; }}
        .bar-green {{ background: linear-gradient(90deg, #10b981, #059669); }}
        .bar-amber {{ background: linear-gradient(90deg, #f59e0b, #d97706); }}
        .bar-red {{ background: linear-gradient(90deg, #ef4444, #dc2626); }}
        .cores-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 8px; margin-top: 12px; }}
        .core-box {{ background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 8px 10px; font-size: 0.8rem; }}
        canvas.sparkline {{ width: 100%; height: 120px; background: #0f172a; border: 1px solid #334155; border-radius: 8px; display: block; margin-top: 8px; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 0.86rem; margin-top: 10px; }}
        th, td {{ border-bottom: 1px solid #334155; padding: 8px 10px; text-align: left; color: #e2e8f0; }}
        th {{ background: #0f172a; color: #94a3b8; font-weight: 700; text-transform: uppercase; font-size: 0.75rem; }}
        .badge {{ display: inline-block; padding: 3px 8px; border-radius: 6px; font-size: 0.76rem; font-weight: 700; background: #1e3a8a; color: #93c5fd; }}
        .badge-live {{ background: #065f46; color: #6ee7b7; animation: pulse 2s infinite; }}
        .badge-warn {{ background: #78350f; color: #fde68a; }}
        .badge-err {{ background: #7f1d1d; color: #fecaca; }}
        @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.65; }} }}
        .disk-item {{ background: #0f172a; border: 1px solid #334155; border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; }}
        .muted {{ color: #94a3b8; font-size: 0.84rem; }}
    </style>
</head>
<body>
    <div class="topbar">
        <div>
            <strong style="font-size:1.15rem;">🖥️ Gerenciador de Recursos do Servidor & Storage Supabase</strong>
            <span id="statusLiveBadge" class="badge badge-live" style="margin-left:10px;">● AO VIVO (3s)</span>
            <span class="muted" style="margin-left:10px;">Última leitura: <strong id="lblTimestamp" style="color:#f8fafc;">--:--:--</strong> | Usuário: {username_esc}</span>
        </div>
        <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <button id="btnToggleLive" class="btn btn-secondary" onclick="alternarTempoReal()">⏸️ Pausar Tempo Real</button>
            <button class="btn btn-primary" onclick="buscarMetricas(true)">🔄 Recalcular Storage Agora</button>
            <a href="/licitacoes/" class="btn btn-secondary">📄 Painel de Licitações</a>
            <a href="/licitacoes/upload/" class="btn btn-success">📤 Upload de PDFs</a>
            <a href="/" class="btn btn-secondary">🏠 Início</a>
        </div>
    </div>

    <!-- KPIs Rápidos no Topo -->
    <div class="grid-4">
        <div class="kpi-card">
            <div class="kpi-label">⚡ Uso Total de CPU</div>
            <div class="kpi-value" id="kpiCpu">--%</div>
            <div class="kpi-sub" id="kpiCpuSub">-- vCPUs | Load: --</div>
        </div>
        <div class="kpi-card">
            <div class="kpi-label">🧠 Memória RAM Física</div>
            <div class="kpi-value" id="kpiRam">--%</div>
            <div class="kpi-sub" id="kpiRamSub">-- / -- (Django: --)</div>
        </div>
        <div class="kpi-card">
            <div class="kpi-label">📦 Storage de PDFs (Supabase / Local)</div>
            <div class="kpi-value" id="kpiStorageTotal">--</div>
            <div class="kpi-sub" id="kpiStorageSub">-- na fila | -- processados</div>
        </div>
        <div class="kpi-card">
            <div class="kpi-label">🗄️ Banco de Dados (<span id="kpiDbEngine">DB</span>)</div>
            <div class="kpi-value" id="kpiDbSize">--</div>
            <div class="kpi-sub" id="kpiDbSub">-- licitações cadastradas</div>
        </div>
    </div>

    <!-- NOVO PAINEL: Status da Análise IA, Agendador 10 min, Artigos Aguardando e Artigos com Erro -->
    <div class="card" style="margin-bottom:18px; border-color:#3b82f6;">
        <h2>
            <span>🤖 Status da Análise IA, Agendador Automático (Cron 10 min) & Fila de Artigos</span>
            <span id="badgeStatusAnalise" class="badge badge-live">Carregando...</span>
        </h2>

        <div class="grid-4" style="margin-top:12px; margin-bottom:14px;">
            <div class="disk-item" style="margin:0;">
                <div class="kpi-label">⏰ Próximo Ciclo Automático</div>
                <div style="font-size:1.3rem; font-weight:800; color:#38bdf8; margin-top:4px;" id="txtProximoCiclo">--m --s</div>
                <div class="muted" id="txtInfoScheduler">Verifica novos_diarios/ a cada 10 min</div>
            </div>
            <div class="disk-item" style="margin:0;">
                <div class="kpi-label">📥 Artigos Aguardando Análise</div>
                <div style="font-size:1.3rem; font-weight:800; color:#fbbf24; margin-top:4px;" id="txtFilaAguardandoQtd">0 artigo(s)</div>
                <div class="muted" id="txtEstimativaTempo">Ritmo suave anti-bloqueio</div>
            </div>
            <div class="disk-item" style="margin:0;">
                <div class="kpi-label">🚨 Artigos com Erro na Análise</div>
                <div style="font-size:1.3rem; font-weight:800; color:#34d399; margin-top:4px;" id="txtErrosAnaliseQtd">0 erro(s)</div>
                <div class="muted" id="txtErrosAnaliseSub">Nenhum artigo com falha</div>
            </div>
            <div class="disk-item" style="margin:0;">
                <div class="kpi-label">🛡️ Economia Super-Batch (250K TPM)</div>
                <div style="font-size:1.3rem; font-weight:800; color:#a78bfa; margin-top:4px;" id="txtEconomiaIa">0 req poupadas</div>
                <div class="muted" id="txtSuperbatchesSub">0 chamadas IA realizadas</div>
            </div>
        </div>

        <!-- Barra de Progresso ao Vivo (exibida durante ou após execução) -->
        <div class="disk-item" style="margin-bottom:14px;">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
                <div>
                    <strong id="txtEtapaAnalise" style="color:#f8fafc;">Aguardando próximo ciclo...</strong>
                    <span class="muted" id="txtArquivoAnalise" style="margin-left:8px;"></span>
                </div>
                <div style="display:flex; gap:8px; align-items:center;">
                    <form method="post" action="/licitacoes/processar-web/" style="margin:0;">
                        <input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">
                        <button type="submit" class="btn btn-primary" style="padding:6px 12px; font-size:0.82rem;">▶️ Executar Análise Agora</button>
                    </form>
                    <a href="/licitacoes/processar-web/" class="btn btn-secondary" style="padding:6px 12px; font-size:0.82rem;">📺 Abrir Console da IA</a>
                </div>
            </div>
            <div class="bar-bg" style="height:14px; margin-top:8px;">
                <div id="barProgressoIa" class="bar-fill bar-green" style="width:0%;"></div>
            </div>
            <div class="muted" style="margin-top:4px; display:flex; justify-content:space-between; flex-wrap:wrap;">
                <span id="txtUltimoResumoCron">Último ciclo: --</span>
                <span id="txtModeloAtivoIa">Modelo preferencial: gemini-3.5-flash-lite</span>
            </div>
        </div>

        <div class="grid-2" style="margin-bottom:0;">
            <!-- Coluna 1: Artigos Aguardando Análise em novos_diarios/ -->
            <div class="disk-item" style="margin-bottom:0;">
                <h3 style="margin-top:0; font-size:0.96rem;">
                    <span>📥 Artigos Aguardando Análise (novos_diarios/)</span>
                    <span id="badgeFilaCount" class="badge">0 na fila</span>
                </h3>
                <div style="max-height:230px; overflow-y:auto;">
                    <table>
                        <thead>
                            <tr>
                                <th>Artigo / Diário PDF</th>
                                <th>Tamanho</th>
                                <th>Enviado em</th>
                                <th>Situação</th>
                            </tr>
                        </thead>
                        <tbody id="tbodyArtigosAguardando"></tbody>
                    </table>
                </div>
            </div>

            <!-- Coluna 2: Artigos que Tiveram Erro na Análise -->
            <div class="disk-item" style="margin-bottom:0;">
                <h3 style="margin-top:0; font-size:0.96rem;">
                    <span>🚨 Histórico de Artigos com Erro na Análise</span>
                    <button id="btnLimparErros" type="button" class="btn btn-secondary" style="padding:3px 9px; font-size:0.75rem; display:none;" onclick="limparErrosAnalise()">🧹 Limpar Avisos</button>
                </h3>
                <div style="max-height:230px; overflow-y:auto;">
                    <table>
                        <thead>
                            <tr>
                                <th>Artigo PDF</th>
                                <th>Tentativas</th>
                                <th>Horário</th>
                                <th>Motivo do Erro</th>
                            </tr>
                        </thead>
                        <tbody id="tbodyArtigosComErro"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Tabela Compacta de Cotas RPM / RPD dos Modelos Gemini -->
        <div style="margin-top:14px;">
            <h3 style="margin-bottom:4px; font-size:0.94rem;">📊 Uso Atual das Cotas Google AI Studio (Balanceamento RPM / TPM / RPD)</h3>
            <table>
                <thead>
                    <tr>
                        <th>Prioridade</th>
                        <th>Modelo Gemini</th>
                        <th>Uso por Minuto (RPM)</th>
                        <th>Janela de Contexto (TPM)</th>
                        <th>Uso Diário (RPD)</th>
                        <th>Disponibilidade</th>
                    </tr>
                </thead>
                <tbody id="tbodyCotasServidor"></tbody>
            </table>
        </div>
    </div>

    <!-- Gráficos estilo Gerenciador de Tarefas do Windows (CPU & Memória) -->
    <div class="grid-2">
        <div class="card">
            <h2>
                <span>📈 Desempenho da CPU (Histórico 60s + Núcleos)</span>
                <span id="txtCpuFreq" class="badge">-- MHz</span>
            </h2>
            <canvas id="canvasCpu" class="sparkline" width="560" height="120"></canvas>
            <div class="cores-grid" id="gridCores"></div>
        </div>

        <div class="card">
            <h2>
                <span>🧠 Consumo de Memória RAM, Swap & Rede</span>
                <span id="txtSwapInfo" class="badge">Swap: --</span>
            </h2>
            <canvas id="canvasRam" class="sparkline" width="560" height="120"></canvas>
            <div style="margin-top:14px;">
                <div style="display:flex; justify-content:space-between; font-size:0.86rem;">
                    <span>Memória Usada: <strong id="txtRamUsada">--</strong></span>
                    <span>Disponível: <strong id="txtRamLivre">--</strong> | Cache: <strong id="txtRamCache">--</strong></span>
                </div>
                <div class="bar-bg"><div id="barRam" class="bar-fill bar-green" style="width:0%;"></div></div>
            </div>
            <div style="margin-top:12px; display:grid; grid-template-columns:1fr 1fr; gap:10px;">
                <div class="core-box">
                    <div class="muted">🌐 Tráfego Recebido (RX)</div>
                    <strong id="txtNetRx" style="font-size:1rem; color:#38bdf8;">--</strong>
                    <div class="muted" id="txtNetRxRate">Velocidade: 0 KB/s</div>
                </div>
                <div class="core-box">
                    <div class="muted">🌐 Tráfego Enviado (TX)</div>
                    <strong id="txtNetTx" style="font-size:1rem; color:#a78bfa;">--</strong>
                    <div class="muted" id="txtNetTxRate">Velocidade: 0 KB/s</div>
                </div>
            </div>
        </div>
    </div>

    <!-- Armazenamento Interno do Supabase/Storage + Discos do Servidor -->
    <div class="grid-2">
        <div class="card">
            <h2>
                <span>☁️ Armazenamento Interno de PDFs & Banco Supabase</span>
                <span id="badgeStorageStatus" class="badge">Online</span>
            </h2>
            <p class="muted" style="margin-top:-6px;" id="txtStorageTipo">--</p>

            <div class="grid-2" style="margin-bottom:12px;">
                <div class="disk-item">
                    <div class="kpi-label">📥 Fila de Entrada (novos_diarios/)</div>
                    <div style="font-size:1.3rem; font-weight:800; color:#fbbf24; margin-top:4px;" id="txtNovosQtd">0 PDFs</div>
                    <div class="muted">Espaço ocupado: <strong id="txtNovosBytes" style="color:#f8fafc;">0 B</strong></div>
                </div>
                <div class="disk-item">
                    <div class="kpi-label">✅ Arquivados (diarios_processados/)</div>
                    <div style="font-size:1.3rem; font-weight:800; color:#34d399; margin-top:4px;" id="txtProcQtd">0 PDFs</div>
                    <div class="muted">Espaço ocupado: <strong id="txtProcBytes" style="color:#f8fafc;">0 B</strong></div>
                </div>
            </div>

            <h3>📅 Distribuição por Mês (diarios_processados/{'{YYYY}/{MM}'})</h3>
            <table>
                <thead>
                    <tr>
                        <th>Período (Ano/Mês)</th>
                        <th>Qtd. de Diários</th>
                        <th>Volume Armazenado</th>
                    </tr>
                </thead>
                <tbody id="tbodyPorMes"></tbody>
            </table>

            <h3 style="margin-top:16px;">🏷️ Licitações no Banco por Tag Pós-Análise</h3>
            <div id="boxTagsBanco" style="display:flex; gap:8px; flex-wrap:wrap;"></div>
        </div>

        <div class="card">
            <h2>
                <span>💽 Discos Físicos e Partições do Servidor</span>
                <span id="txtUptimeSrv" class="badge">Uptime: --</span>
            </h2>
            <p class="muted" style="margin-top:-6px;" id="txtHostInfo">--</p>
            <div id="listaDiscos"></div>

            <h3 style="margin-top:18px;">📄 Últimos Arquivos PDF no Storage</h3>
            <table>
                <thead>
                    <tr>
                        <th>Arquivo</th>
                        <th>Local</th>
                        <th>Tamanho</th>
                        <th>Data</th>
                    </tr>
                </thead>
                <tbody id="tbodyArquivosRecentes"></tbody>
            </table>
        </div>
    </div>

    <!-- Gerenciador de Processos Ativos -->
    <div class="card">
        <h2>
            <span>⚙️ Processos em Execução no Servidor / Container (Top 10 por Consumo)</span>
            <span id="txtDjangoRss" class="badge">Processo Django: --</span>
        </h2>
        <table>
            <thead>
                <tr>
                    <th>PID</th>
                    <th>Processo</th>
                    <th>Usuário</th>
                    <th>Status</th>
                    <th>% CPU</th>
                    <th>% RAM</th>
                    <th>Memória Física (RSS)</th>
                </tr>
            </thead>
            <tbody id="tbodyProcessos"></tbody>
        </table>
    </div>

    <script>
        let dadosAtuais = {snapshot_inicial};
        let tempoRealAtivo = true;
        let historicoCpu = [];
        let historicoRam = [];
        const MAX_PONTOS = 30;
        let ultimoRx = null;
        let ultimoTx = null;
        let ultimoTempoRede = null;

        function classeCorBarra(pct) {{
            if (pct >= 85) return "bar-fill bar-red";
            if (pct >= 70) return "bar-fill bar-amber";
            return "bar-fill bar-green";
        }}

        function desenharGrafico(canvasId, dados, corLinha, corFundo) {{
            const canvas = document.getElementById(canvasId);
            if (!canvas) return;
            const ctx = canvas.getContext("2d");
            const w = canvas.width;
            const h = canvas.height;
            ctx.clearRect(0, 0, w, h);

            // Grade de fundo estilo Task Manager
            ctx.strokeStyle = "#1e293b";
            ctx.lineWidth = 1;
            for (let y = 0; y <= h; y += 30) {{
                ctx.beginPath();
                ctx.moveTo(0, y);
                ctx.lineTo(w, y);
                ctx.stroke();
            }}
            for (let x = 0; x <= w; x += 40) {{
                ctx.beginPath();
                ctx.moveTo(x, 0);
                ctx.lineTo(x, h);
                ctx.stroke();
            }}

            if (dados.length === 0) return;

            const passoX = w / (MAX_PONTOS - 1);
            ctx.beginPath();
            dados.forEach((val, idx) => {{
                const x = idx * passoX;
                const y = h - (Math.min(Math.max(val, 0), 100) / 100) * (h - 10) - 5;
                if (idx === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            }});
            ctx.strokeStyle = corLinha;
            ctx.lineWidth = 2.5;
            ctx.stroke();

            // Preenchimento sob a curva
            ctx.lineTo((dados.length - 1) * passoX, h);
            ctx.lineTo(0, h);
            ctx.closePath();
            ctx.fillStyle = corFundo;
            ctx.fill();
        }}

        function formatarVelocidade(bytesPorSeg) {{
            if (bytesPorSeg < 1024) return Math.round(bytesPorSeg) + " B/s";
            if (bytesPorSeg < 1048576) return (bytesPorSeg / 1024).toFixed(1) + " KB/s";
            return (bytesPorSeg / 1048576).toFixed(2) + " MB/s";
        }}

        function renderizarDashboard(snap) {{
            document.getElementById("lblTimestamp").textContent = snap.timestamp;
            const hw = snap.hardware;
            const st = snap.storage;
            const db = snap.banco;

            // Histórico para os gráficos
            historicoCpu.push(hw.cpu.percent_total);
            if (historicoCpu.length > MAX_PONTOS) historicoCpu.shift();
            historicoRam.push(hw.memoria.percent);
            if (historicoRam.length > MAX_PONTOS) historicoRam.shift();

            desenharGrafico("canvasCpu", historicoCpu, "#38bdf8", "rgba(56, 189, 248, 0.18)");
            desenharGrafico("canvasRam", historicoRam, "#a78bfa", "rgba(167, 139, 250, 0.18)");

            // KPIs Topo
            document.getElementById("kpiCpu").textContent = hw.cpu.percent_total + "%";
            document.getElementById("kpiCpuSub").textContent =
                `${{hw.cpu.nucleos_logicos}} vCPUs (${{hw.cpu.nucleos_fisicos}} físicos) | Load: ${{hw.sistema.load_avg.join(" / ")}}`;

            document.getElementById("kpiRam").textContent = hw.memoria.percent + "%";
            document.getElementById("kpiRamSub").textContent =
                `${{hw.memoria.usado_human}} / ${{hw.memoria.total_human}} (Django: ${{hw.sistema.django_rss_human}})`;

            document.getElementById("kpiStorageTotal").textContent =
                `${{st.total.human}} (${{st.total.qtd}} PDFs)`;
            document.getElementById("kpiStorageSub").textContent =
                `${{st.novos_diarios.qtd}} na fila | ${{st.diarios_processados.qtd}} processados`;

            document.getElementById("kpiDbEngine").textContent = db.engine;
            document.getElementById("kpiDbSize").textContent = db.tamanho_human;
            document.getElementById("kpiDbSub").textContent =
                `${{db.total_licitacoes}} licitação(ões) em '${{db.nome_banco}}'`;

            // CPU por núcleo
            document.getElementById("txtCpuFreq").textContent = hw.cpu.freq_mhz ? `${{hw.cpu.freq_mhz}} MHz` : `${{hw.cpu.nucleos_logicos}} vCPUs`;
            const gridCores = document.getElementById("gridCores");
            gridCores.innerHTML = hw.cpu.per_core.map((pct, i) => `
                <div class="core-box">
                    <div style="display:flex; justify-content:space-between;">
                        <span>Core #${{i}}</span>
                        <strong>${{pct}}%</strong>
                    </div>
                    <div class="bar-bg" style="height:7px; margin:4px 0 0 0;">
                        <div class="${{classeCorBarra(pct)}}" style="width:${{pct}}%;"></div>
                    </div>
                </div>
            `).join("");

            // Memória & Rede
            document.getElementById("txtSwapInfo").textContent =
                `Swap: ${{hw.memoria.swap_usado_human}} / ${{hw.memoria.swap_total_human}} (${{hw.memoria.swap_percent}}%)`;
            document.getElementById("txtRamUsada").textContent = hw.memoria.usado_human;
            document.getElementById("txtRamLivre").textContent = hw.memoria.disponivel_human;
            document.getElementById("txtRamCache").textContent = hw.memoria.cached_human;
            const barRam = document.getElementById("barRam");
            barRam.style.width = hw.memoria.percent + "%";
            barRam.className = classeCorBarra(hw.memoria.percent);

            const agoraMs = Date.now();
            if (ultimoRx !== null && ultimoTempoRede !== null) {{
                const dt = Math.max((agoraMs - ultimoTempoRede) / 1000, 0.5);
                const rxRate = Math.max((hw.rede.bytes_recebidos - ultimoRx) / dt, 0);
                const txRate = Math.max((hw.rede.bytes_enviados - ultimoTx) / dt, 0);
                document.getElementById("txtNetRxRate").textContent = "Velocidade: " + formatarVelocidade(rxRate);
                document.getElementById("txtNetTxRate").textContent = "Velocidade: " + formatarVelocidade(txRate);
            }}
            ultimoRx = hw.rede.bytes_recebidos;
            ultimoTx = hw.rede.bytes_enviados;
            ultimoTempoRede = agoraMs;

            document.getElementById("txtNetRx").textContent =
                `${{hw.rede.bytes_recebidos_human}} (${{hw.rede.pacotes_recebidos}} pkts)`;
            document.getElementById("txtNetTx").textContent =
                `${{hw.rede.bytes_enviados_human}} (${{hw.rede.pacotes_enviados}} pkts)`;

            // Storage Supabase / Local
            document.getElementById("txtStorageTipo").textContent = st.tipo_storage;
            document.getElementById("badgeStorageStatus").textContent = st.status;
            document.getElementById("txtNovosQtd").textContent = `${{st.novos_diarios.qtd}} arquivo(s) PDF`;
            document.getElementById("txtNovosBytes").textContent = st.novos_diarios.human;
            document.getElementById("txtProcQtd").textContent = `${{st.diarios_processados.qtd}} arquivo(s) PDF`;
            document.getElementById("txtProcBytes").textContent = st.diarios_processados.human;

            const tbodyPorMes = document.getElementById("tbodyPorMes");
            if (st.por_mes.length === 0) {{
                tbodyPorMes.innerHTML = `<tr><td colspan="3" class="muted">Nenhum PDF arquivado em diarios_processados/ ainda.</td></tr>`;
            }} else {{
                tbodyPorMes.innerHTML = st.por_mes.map(m => `
                    <tr>
                        <td><strong>${{m.periodo}}</strong></td>
                        <td>${{m.qtd}} PDF(s)</td>
                        <td>${{m.tamanho_human}}</td>
                    </tr>
                `).join("");
            }}

            // Tags no banco
            const pt = db.por_tag;
            document.getElementById("boxTagsBanco").innerHTML = `
                <span class="badge" style="background:#78350f; color:#fde68a;">⭐ Favorito: ${{pt.favorito || 0}}</span>
                <span class="badge" style="background:#1e3a8a; color:#93c5fd;">🔍 Em Análise: ${{pt.em_analise || 0}}</span>
                <span class="badge" style="background:#7f1d1d; color:#fecaca;">⚠️ Sem Conformidade: ${{pt.sem_conformidade || 0}}</span>
                <span class="badge" style="background:#374151; color:#e5e7eb;">🚫 Ignorado: ${{pt.ignorado || 0}}</span>
                <span class="badge" style="background:#0f172a; color:#cbd5e1; border:1px solid #475569;">⚪ Sem Marcação: ${{pt.sem_marcacao || 0}}</span>
            `;

            // Discos do Servidor
            document.getElementById("txtUptimeSrv").textContent = `Uptime: ${{hw.sistema.uptime_sistema_human}}`;
            document.getElementById("txtHostInfo").textContent =
                `Host: ${{hw.sistema.hostname}} | OS: ${{hw.sistema.os}} (${{hw.sistema.arquitetura}}) | Python ${{hw.sistema.python_version}} | Modo: ${{hw.sistema.modo_app}}`;

            document.getElementById("listaDiscos").innerHTML = snap.discos.map(d => `
                <div class="disk-item">
                    <div style="display:flex; justify-content:space-between; font-size:0.88rem;">
                        <strong>💿 ${{d.dispositivo}} <span class="muted">(${{d.ponto_montagem}})</span></strong>
                        <span><strong>${{d.percent}}%</strong> usado (${{d.usado_human}} / ${{d.total_human}} — Livre: ${{d.livre_human}})</span>
                    </div>
                    <div class="bar-bg">
                        <div class="${{classeCorBarra(d.percent)}}" style="width:${{d.percent}}%;"></div>
                    </div>
                </div>
            `).join("");

            // Arquivos recentes
            const tbodyArq = document.getElementById("tbodyArquivosRecentes");
            if (st.arquivos_recentes.length === 0) {{
                tbodyArq.innerHTML = `<tr><td colspan="4" class="muted">Nenhum PDF encontrado no storage.</td></tr>`;
            }} else {{
                tbodyArq.innerHTML = st.arquivos_recentes.map(a => `
                    <tr>
                        <td><code>${{a.nome}}</code></td>
                        <td>${{a.pasta}}</td>
                        <td>${{a.tamanho_human}}</td>
                        <td>${{a.modificado}}</td>
                    </tr>
                `).join("");
            }}

            // Top Processos
            document.getElementById("txtDjangoRss").textContent =
                `Processo Django Atual: ${{hw.sistema.django_rss_human}} (Uptime: ${{hw.sistema.uptime_django_human}})`;
            document.getElementById("tbodyProcessos").innerHTML = hw.top_processos.map(p => `
                <tr>
                    <td><code>${{p.pid}}</code></td>
                    <td><strong>${{p.name}}</strong></td>
                    <td>${{p.username}}</td>
                    <td><span class="badge">${{p.status}}</span></td>
                    <td><strong>${{p.cpu_percent}}%</strong></td>
                    <td>${{p.memory_percent}}%</td>
                    <td>${{p.rss_human}}</td>
                </tr>
            `).join("");

            // Status da Análise IA, Agendador 10 min, Artigos Aguardando e Artigos com Erro
            const ia = snap.analise_ia || {{}};
            const sched = ia.scheduler || {{}};
            const arqAguardando = st.arquivos_aguardando || [];
            const arqComErro = ia.lista_arquivos_com_erro || [];
            const mapaErrosPorNome = {{}};
            arqComErro.forEach(item => {{ mapaErrosPorNome[item.nome_arquivo] = item; }});

            const badgeAnalise = document.getElementById("badgeStatusAnalise");
            const barIa = document.getElementById("barProgressoIa");
            if (ia.em_execucao) {{
                badgeAnalise.textContent = `⚡ ANALISANDO AGORA (${{ia.percentual || 0}}%)`;
                badgeAnalise.className = "badge badge-warn";
                barIa.style.width = `${{Math.max(ia.percentual || 5, 5)}}%`;
                barIa.className = "bar-fill bar-amber";
            }} else if (arqComErro.length > 0) {{
                badgeAnalise.textContent = `⚠️ ATENÇÃO: ${{arqComErro.length}} ARTIGO(S) COM ERRO`;
                badgeAnalise.className = "badge badge-err";
                barIa.style.width = `${{ia.percentual || 0}}%`;
                barIa.className = "bar-fill bar-red";
            }} else {{
                badgeAnalise.textContent = sched.ativo
                    ? `🟢 AGENDADOR ATIVO (Ciclo ${{sched.intervalo_minutos || 10}} min)`
                    : "⏸️ AGENDADOR PAUSADO";
                badgeAnalise.className = "badge badge-live";
                barIa.style.width = `${{ia.percentual || 0}}%`;
                barIa.className = "bar-fill bar-green";
            }}

            document.getElementById("txtProximoCiclo").textContent =
                ia.em_execucao ? "Executando agora..." : (sched.proximo_ciclo_human || "--m --s");
            document.getElementById("txtInfoScheduler").textContent =
                `Ciclo a cada ${{sched.intervalo_minutos || 10}} min (máx. ${{sched.max_pdfs_por_ciclo || 3}} PDFs IA/ciclo)`;

            const qtdFila = arqAguardando.length;
            const maxCiclo = Math.max(sched.max_pdfs_por_ciclo || 3, 1);
            const intMin = sched.intervalo_minutos || 10;
            const ciclosEstimados = Math.ceil(qtdFila / maxCiclo);
            const minEstimados = ciclosEstimados * intMin;
            document.getElementById("txtFilaAguardandoQtd").textContent = `${{qtdFila}} artigo(s) PDF`;
            document.getElementById("badgeFilaCount").textContent = `${{qtdFila}} na fila`;
            document.getElementById("txtEstimativaTempo").textContent =
                qtdFila === 0
                    ? "Fila zerada — nenhum artigo pendente"
                    : `Estimativa econômica: ~${{minEstimados}} min (${{ciclosEstimados}} ciclo(s) de ${{intMin}} min)`;

            const elErrosQtd = document.getElementById("txtErrosAnaliseQtd");
            const elErrosSub = document.getElementById("txtErrosAnaliseSub");
            const btnLimparErros = document.getElementById("btnLimparErros");
            elErrosQtd.textContent = `${{arqComErro.length}} artigo(s)`;
            if (arqComErro.length > 0) {{
                elErrosQtd.style.color = "#f87171";
                elErrosSub.textContent = "Verifique a tabela de erros abaixo";
                btnLimparErros.style.display = "inline-block";
            }} else {{
                elErrosQtd.style.color = "#34d399";
                elErrosSub.textContent = "Nenhum artigo com erro registrado";
                btnLimparErros.style.display = "none";
            }}

            document.getElementById("txtEconomiaIa").textContent =
                `${{ia.economia_requisicoes_ia || 0}} req IA poupadas`;
            document.getElementById("txtSuperbatchesSub").textContent =
                `${{ia.superbatches_enviados || 0}} Super-Batch(es) enviado(s) | ${{ia.duplicados_bloqueados || 0}} duplicado(s)`;

            document.getElementById("txtEtapaAnalise").textContent =
                ia.etapa_atual || "Ocioso — aguardando próximo ciclo.";
            document.getElementById("txtArquivoAnalise").textContent =
                ia.arquivo_atual ? `[${{ia.arquivo_atual}}]` : "";
            document.getElementById("txtUltimoResumoCron").textContent =
                `${{sched.ultimo_resumo || "Aguardando primeiro ciclo"}} (Última checagem: ${{sched.ultimo_ciclo_em || "--"}})`;
            document.getElementById("txtModeloAtivoIa").textContent =
                `Modelo ativo: ${{ia.modelo_ativo || "gemini-3.5-flash-lite"}}`;

            // Tabela de Artigos Aguardando Análise (novos_diarios/)
            const tbodyAgu = document.getElementById("tbodyArtigosAguardando");
            if (arqAguardando.length === 0) {{
                tbodyAgu.innerHTML = `<tr><td colspan="4" style="color:#34d399; font-weight:600;">✅ Nenhum artigo aguardando análise em novos_diarios/.</td></tr>`;
            }} else {{
                tbodyAgu.innerHTML = arqAguardando.map((a, i) => {{
                    const errInfo = mapaErrosPorNome[a.nome];
                    const badgeSit = errInfo
                        ? `<span class="badge badge-err" title="${{errInfo.erro}}">⚠️ Erro anterior (${{errInfo.tentativas}}x)</span>`
                        : (i < maxCiclo
                            ? `<span class="badge badge-live">🎯 Próximo lote (10 min)</span>`
                            : `<span class="badge">⏳ Na fila (#${{i + 1}})</span>`);
                    return `
                        <tr>
                            <td><code>${{a.nome}}</code></td>
                            <td>${{a.tamanho_human}}</td>
                            <td>${{a.modificado}}</td>
                            <td>${{badgeSit}}</td>
                        </tr>
                    `;
                }}).join("");
            }}

            // Tabela de Artigos que Tiveram Erro
            const tbodyErr = document.getElementById("tbodyArtigosComErro");
            if (arqComErro.length === 0) {{
                tbodyErr.innerHTML = `<tr><td colspan="4" style="color:#34d399; font-weight:600;">✅ Nenhum artigo apresentou erro de análise.</td></tr>`;
            }} else {{
                tbodyErr.innerHTML = arqComErro.map(e => `
                    <tr>
                        <td><code>${{e.nome_arquivo}}</code></td>
                        <td><span class="badge badge-err">${{e.tentativas}}x</span></td>
                        <td>${{e.horario}}</td>
                        <td style="color:#fca5a5; font-size:0.8rem;">${{e.erro}}</td>
                    </tr>
                `).join("");
            }}

            // Tabela de Cotas Gemini no Servidor
            const tbodyCotasSrv = document.getElementById("tbodyCotasServidor");
            if (ia.cotas_modelos && tbodyCotasSrv) {{
                tbodyCotasSrv.innerHTML = ia.cotas_modelos.map((c, idx) => {{
                    const stBadge = c.disponivel
                        ? `<span class="badge badge-live">Disponível</span>`
                        : `<span class="badge badge-warn">Cooldown (${{c.em_cooldown_seg}}s)</span>`;
                    return `
                        <tr>
                            <td><strong>#${{idx + 1}}</strong></td>
                            <td><code>${{c.model}}</code></td>
                            <td><strong>${{c.rpm_usado}}</strong> / ${{c.rpm_limite}} RPM</td>
                            <td>${{(c.tpm_limite / 1000)}}K TPM</td>
                            <td><strong>${{c.rpd_usado}}</strong> / ${{c.rpd_limite}} RPD</td>
                            <td>${{stBadge}}</td>
                        </tr>
                    `;
                }}).join("");
            }}
        }}

        async function limparErrosAnalise() {{
            try {{
                const resp = await fetch("/licitacoes/servidor/limpar-erros/", {{
                    method: "POST",
                    headers: {{
                        "X-CSRFToken": "{csrf_token}",
                        "X-Requested-With": "XMLHttpRequest"
                    }}
                }});
                if (resp.ok) buscarMetricas(false);
            }} catch (e) {{}}
        }}

        async function buscarMetricas(forcarStorage = false) {{
            try {{
                const url = forcarStorage
                    ? "/licitacoes/servidor/metricas/?refresh_storage=1"
                    : "/licitacoes/servidor/metricas/";
                const resp = await fetch(url, {{ headers: {{ "X-Requested-With": "XMLHttpRequest" }} }});
                if (!resp.ok) return;
                const json = await resp.json();
                if (json.status === "sucesso" && json.dados) {{
                    dadosAtuais = json.dados;
                    renderizarDashboard(dadosAtuais);
                }}
            }} catch (e) {{
                console.warn("Falha ao atualizar métricas:", e);
            }}
        }}

        function alternarTempoReal() {{
            tempoRealAtivo = !tempoRealAtivo;
            const btn = document.getElementById("btnToggleLive");
            const badge = document.getElementById("statusLiveBadge");
            if (tempoRealAtivo) {{
                btn.textContent = "⏸️ Pausar Tempo Real";
                badge.textContent = "● AO VIVO (3s)";
                badge.className = "badge badge-live";
                buscarMetricas(false);
            }} else {{
                btn.textContent = "▶️ Retomar Tempo Real";
                badge.textContent = "⏸️ PAUSADO";
                badge.className = "badge";
            }}
        }}

        // Inicializa imediatamente e agenda polling a cada 3 segundos
        renderizarDashboard(dadosAtuais);
        setInterval(() => {{
            if (tempoRealAtivo) buscarMetricas(false);
        }}, 3000);
    </script>
</body>
</html>"""
    return HttpResponse(pagina_html)
