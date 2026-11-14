import html
import json
from collections import Counter

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from credenciamento_parana.dados_anexo1 import sincronizar_escolas_credenciamento
from credenciamento_parana.models import EscolaCredenciamento, OfertaCursoEscola


def _serializar_escolas_com_cursos() -> list[dict]:
    """
    Garante que o banco contenha as 147 escolas e 214 ofertas de cursos do Anexo I
    e retorna a lista completa estruturada para o mapa Leaflet e listagem dinâmica.
    """
    sincronizar_escolas_credenciamento(forcar_atualizacao=False)
    escolas_qs = EscolaCredenciamento.objects.prefetch_related("ofertas_cursos").order_by("item_edital")

    resultado = []
    for esc in escolas_qs:
        cursos_list = []
        modalidades_set = set()
        tipos_mod_set = set()
        turnos_set = set()
        ch_total_escola = 0
        turmas_total_escola = 0

        for of in esc.ofertas_cursos.all():
            ch_total_oferta = (
                int(of.ch_anual_curso)
                + int(of.ch_anual_coord)
                + int(of.ch_anual_sup_tecnico)
            ) * int(of.turmas)
            ch_total_escola += ch_total_oferta
            turmas_total_escola += int(of.turmas)
            modalidades_set.add(of.modalidade)
            tipos_mod_set.add(of.tipo_modalidade)
            turnos_set.add(of.turno)

            cursos_list.append(
                {
                    "id": of.id,
                    "modalidade": of.modalidade,
                    "tipo_modalidade": of.tipo_modalidade,
                    "nome_curso": of.nome_curso,
                    "ch_anual_curso": of.ch_anual_curso,
                    "ch_anual_coord": of.ch_anual_coord,
                    "ch_anual_sup_tecnico": of.ch_anual_sup_tecnico,
                    "turmas": of.turmas,
                    "turno": of.turno,
                    "ch_total_oferta": ch_total_oferta,
                }
            )

        resultado.append(
            {
                "id": esc.id,
                "item_edital": esc.item_edital,
                "nucleo_regional": esc.nucleo_regional,
                "municipio": esc.municipio,
                "nome_escola": esc.nome_escola,
                "codigo_mec": esc.codigo_mec,
                "endereco": esc.endereco,
                "cep": esc.cep,
                "telefone": esc.telefone,
                "email": esc.email,
                "responsavel": esc.responsavel,
                "latitude": esc.latitude,
                "longitude": esc.longitude,
                "favorito": esc.favorito,
                "observacoes": esc.observacoes,
                "cursos": cursos_list,
                "qtd_cursos": len(cursos_list),
                "turmas_total": turmas_total_escola,
                "ch_total_anual": ch_total_escola,
                "tem_integrado": "integrado" in tipos_mod_set,
                "tem_subsequente": "subsequente" in tipos_mod_set,
                "turnos": sorted(turnos_set),
                "modalidades": sorted(modalidades_set),
            }
        )
    return resultado


@login_required
@require_GET
def dados_escolas_json(request: HttpRequest) -> JsonResponse:
    """Retorna todas as 147 escolas e seus cursos em formato JSON para consumo da UI."""
    escolas = _serializar_escolas_com_cursos()
    return JsonResponse({"ok": True, "total_escolas": len(escolas), "escolas": escolas})


@login_required
@require_POST
def alternar_favorito_escola(request: HttpRequest, pk: int) -> JsonResponse:
    """
    Marca ou desmarca uma escola (marcador do mapa) como Favorito no banco de dados.
    Aceita `{ "favorito": true/false }` opcional no body ou inverte o valor atual.
    """
    escola = get_object_or_404(EscolaCredenciamento, pk=pk)
    novo_valor = not escola.favorito
    if request.body:
        try:
            payload = json.loads(request.body.decode("utf-8"))
            if "favorito" in payload and isinstance(payload["favorito"], bool):
                novo_valor = payload["favorito"]
        except Exception:
            pass

    escola.favorito = novo_valor
    escola.save(update_fields=["favorito", "atualizado_em"])
    total_favoritos = EscolaCredenciamento.objects.filter(favorito=True).count()
    return JsonResponse(
        {
            "ok": True,
            "id": escola.id,
            "item_edital": escola.item_edital,
            "favorito": escola.favorito,
            "total_favoritos": total_favoritos,
        }
    )


@login_required
@require_POST
def salvar_observacao_escola(request: HttpRequest, pk: int) -> JsonResponse:
    """Salva uma observação rápida vinculada a uma escola específica do credenciamento."""
    escola = get_object_or_404(EscolaCredenciamento, pk=pk)
    observacoes = ""
    if request.body:
        try:
            payload = json.loads(request.body.decode("utf-8"))
            observacoes = str(payload.get("observacoes", "")).strip()
        except Exception:
            pass
    escola.observacoes = observacoes
    escola.save(update_fields=["observacoes", "atualizado_em"])
    return JsonResponse(
        {
            "ok": True,
            "id": escola.id,
            "item_edital": escola.item_edital,
            "observacoes": escola.observacoes,
        }
    )


@login_required
@require_POST
def resincronizar_dados_anexo1(request: HttpRequest) -> JsonResponse:
    """Força a ressincronização das 147 escolas e 214 cursos do Anexo I preservando favoritos."""
    info = sincronizar_escolas_credenciamento(forcar_atualizacao=True)
    return JsonResponse({"ok": True, **info})


@login_required
@ensure_csrf_cookie
@require_GET
def mapa_credenciamento_parana(request: HttpRequest) -> HttpResponse:
    """
    Página interativa de Mapeamento Geográfico (Leaflet) e Filtragem Rica das 147 Escolas
    e 214 Ofertas de Cursos do Termo de Referência Inicial de Credenciamento (SEED/PR - Anexo I e IV).
    """
    escolas_data = _serializar_escolas_com_cursos()
    csrf_token = get_token(request)
    username_esc = html.escape(request.user.get_username())

    # Estatísticas para popular os seletores de filtro com contagens reais
    contagem_cursos = Counter()
    contagem_municipios = Counter()
    contagem_nucleos = Counter()
    contagem_turnos = Counter()

    for esc in escolas_data:
        contagem_municipios[esc["municipio"]] += 1
        contagem_nucleos[esc["nucleo_regional"]] += 1
        cursos_vistos_na_escola = set()
        turnos_vistos_na_escola = set()
        for c in esc["cursos"]:
            cursos_vistos_na_escola.add(c["nome_curso"])
            turnos_vistos_na_escola.add(c["turno"])
        for nome_c in cursos_vistos_na_escola:
            contagem_cursos[nome_c] += 1
        for nome_t in turnos_vistos_na_escola:
            contagem_turnos[nome_t] += 1

    opcoes_cursos_html = '<option value="">🎓 Todos os Cursos (28 cursos)</option>'
    for curso_nome in sorted(contagem_cursos.keys()):
        qtd_esc = contagem_cursos[curso_nome]
        curso_esc = html.escape(curso_nome)
        opcoes_cursos_html += (
            f'<option value="{curso_esc}">{curso_esc} ({qtd_esc} esc.)</option>'
        )

    opcoes_municipios_html = '<option value="">🏙️ Todos os Municípios (34 cidades)</option>'
    for mun_nome in sorted(contagem_municipios.keys()):
        qtd_esc = contagem_municipios[mun_nome]
        mun_esc = html.escape(mun_nome)
        opcoes_municipios_html += (
            f'<option value="{mun_esc}">{mun_esc} ({qtd_esc} esc.)</option>'
        )

    opcoes_nucleos_html = '<option value="">🏛️ Todos os Núcleos Regionais (25 NREs)</option>'
    for nuc_nome in sorted(contagem_nucleos.keys()):
        qtd_esc = contagem_nucleos[nuc_nome]
        nuc_esc = html.escape(nuc_nome)
        opcoes_nucleos_html += (
            f'<option value="{nuc_esc}">{nuc_esc} ({qtd_esc} esc.)</option>'
        )

    escolas_json_str = json.dumps(escolas_data, ensure_ascii=False)

    pagina_html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Mapa de Credenciamento SEED/PR (147 Escolas) — EPSL</title>
    <!-- Leaflet 1.9.4 CSS -->
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
          integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin=""/>
    <style>
        :root {{
            --bg: #f1f5f9;
            --card-bg: #ffffff;
            --border: #e2e8f0;
            --text: #0f172a;
            --muted: #64748b;
            --primary: #2563eb;
            --primary-dark: #1d4ed8;
            --emerald: #059669;
            --purple: #7c3aed;
            --amber: #d97706;
            --amber-light: #fef3c7;
            --danger: #dc2626;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 20px;
        }}
        .topbar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--card-bg);
            padding: 14px 22px;
            border-radius: 12px;
            border: 1px solid var(--border);
            margin-bottom: 16px;
            box-shadow: 0 2px 6px rgba(15, 23, 42, 0.04);
            flex-wrap: wrap;
            gap: 12px;
        }}
        .topbar-title {{
            display: flex;
            flex-direction: column;
            gap: 2px;
        }}
        .topbar-title h1 {{
            font-size: 1.22rem;
            margin: 0;
            color: #0f172a;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .topbar-title span {{
            font-size: 0.83rem;
            color: var(--muted);
        }}
        .btn {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 8px 14px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 600;
            font-size: 0.86rem;
            border: 1px solid transparent;
            cursor: pointer;
            transition: all 0.15s ease;
        }}
        .btn-primary {{ background: var(--primary); color: #fff; }}
        .btn-primary:hover {{ background: var(--primary-dark); }}
        .btn-secondary {{ background: #f8fafc; color: #334155; border-color: #cbd5e1; }}
        .btn-secondary:hover {{ background: #e2e8f0; }}
        .btn-emerald {{ background: var(--emerald); color: #fff; }}
        .btn-emerald:hover {{ background: #047857; }}
        .btn-amber {{ background: #f59e0b; color: #fff; }}
        .btn-amber:hover {{ background: #d97706; }}
        .btn-fav-filter {{
            background: #fffbeb;
            color: #b45309;
            border: 2px solid #f59e0b;
            font-weight: 700;
        }}
        .btn-fav-filter.active {{
            background: #f59e0b;
            color: #ffffff;
            box-shadow: 0 0 0 3px rgba(245, 158, 11, 0.25);
        }}

        /* KPI Summary Cards */
        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(185px, 1fr));
            gap: 12px;
            margin-bottom: 16px;
        }}
        .kpi-card {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 12px 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.03);
            display: flex;
            flex-direction: column;
            gap: 4px;
        }}
        .kpi-card .kpi-label {{
            font-size: 0.76rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            color: var(--muted);
            font-weight: 700;
        }}
        .kpi-card .kpi-val {{
            font-size: 1.45rem;
            font-weight: 800;
            color: #0f172a;
        }}
        .kpi-card .kpi-sub {{
            font-size: 0.78rem;
            color: var(--muted);
        }}

        /* Rich Filter Panel */
        .filter-panel {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 18px 20px;
            margin-bottom: 16px;
            box-shadow: 0 2px 8px rgba(15, 23, 42, 0.04);
        }}
        .filter-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 14px;
            flex-wrap: wrap;
            gap: 10px;
        }}
        .filter-header h2 {{
            margin: 0;
            font-size: 1.02rem;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .filter-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 12px;
        }}
        .filter-group {{
            display: flex;
            flex-direction: column;
            gap: 5px;
        }}
        .filter-group label {{
            font-size: 0.79rem;
            font-weight: 700;
            color: #334155;
            display: flex;
            align-items: center;
            gap: 5px;
        }}
        .filter-input, .filter-select {{
            padding: 9px 12px;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            font-size: 0.88rem;
            background: #fff;
            color: #0f172a;
            outline: none;
            transition: border-color 0.15s, box-shadow 0.15s;
            width: 100%;
        }}
        .filter-input:focus, .filter-select:focus {{
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.14);
        }}

        /* Quick Chips Bar */
        .quick-chips {{
            display: flex;
            flex-wrap: wrap;
            gap: 7px;
            margin-top: 14px;
            padding-top: 12px;
            border-top: 1px dashed #e2e8f0;
            align-items: center;
        }}
        .chip-btn {{
            padding: 5px 11px;
            border-radius: 999px;
            font-size: 0.78rem;
            font-weight: 600;
            border: 1px solid #cbd5e1;
            background: #f8fafc;
            color: #334155;
            cursor: pointer;
            transition: all 0.15s;
        }}
        .chip-btn:hover {{
            background: #e2e8f0;
        }}
        .chip-btn.active {{
            background: var(--primary);
            color: #fff;
            border-color: var(--primary);
        }}

        /* Map + Favorites Layout */
        .map-workspace {{
            display: grid;
            grid-template-columns: 1fr 330px;
            gap: 16px;
            margin-bottom: 20px;
        }}
        @media (max-width: 1120px) {{
            .map-workspace {{
                grid-template-columns: 1fr;
            }}
        }}
        .map-card {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 2px 8px rgba(15, 23, 42, 0.05);
            display: flex;
            flex-direction: column;
        }}
        .map-toolbar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 16px;
            background: #f8fafc;
            border-bottom: 1px solid var(--border);
            flex-wrap: wrap;
            gap: 10px;
        }}
        .map-legend {{
            display: flex;
            align-items: center;
            gap: 14px;
            font-size: 0.8rem;
            font-weight: 600;
            color: #334155;
            flex-wrap: wrap;
        }}
        .legend-dot {{
            display: inline-block;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 5px;
            vertical-align: middle;
        }}
        #leaflet-map {{
            width: 100%;
            height: 620px;
            z-index: 1;
        }}

        /* Custom Leaflet Markers */
        .epsl-pin-wrapper {{
            background: transparent;
            border: none;
        }}
        .epsl-pin {{
            display: flex;
            align-items: center;
            justify-content: center;
            width: 30px;
            height: 30px;
            border-radius: 50% 50% 50% 0;
            transform: rotate(-45deg);
            box-shadow: 0 3px 8px rgba(0, 0, 0, 0.32);
            border: 2px solid #ffffff;
            transition: transform 0.15s ease;
        }}
        .epsl-pin span {{
            transform: rotate(45deg);
            color: #ffffff;
            font-weight: 800;
            font-size: 0.72rem;
            line-height: 1;
            letter-spacing: -0.02em;
        }}
        .epsl-pin.pin-integrado {{
            background: linear-gradient(135deg, #2563eb, #1d4ed8);
        }}
        .epsl-pin.pin-subsequente {{
            background: linear-gradient(135deg, #7c3aed, #5b21b6);
        }}
        .epsl-pin.pin-ambos {{
            background: linear-gradient(135deg, #059669, #047857);
        }}
        .epsl-pin.pin-favorito {{
            background: linear-gradient(135deg, #f59e0b, #d97706);
            width: 36px;
            height: 36px;
            border: 2.5px solid #fef08a;
            box-shadow: 0 0 0 4px rgba(245, 158, 11, 0.35), 0 4px 10px rgba(0, 0, 0, 0.35);
            z-index: 999 !important;
        }}
        .epsl-pin.pin-favorito span {{
            font-size: 0.82rem;
        }}

        /* Favorites Side Drawer */
        .favorites-drawer {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 12px;
            display: flex;
            flex-direction: column;
            max-height: 675px;
            box-shadow: 0 2px 8px rgba(15, 23, 42, 0.04);
        }}
        .favorites-header {{
            padding: 14px 16px;
            border-bottom: 1px solid var(--border);
            background: linear-gradient(135deg, #fffbeb, #fef3c7);
            border-radius: 12px 12px 0 0;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .favorites-header h3 {{
            margin: 0;
            font-size: 0.96rem;
            color: #92400e;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .favorites-list {{
            padding: 12px;
            overflow-y: auto;
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }}
        .fav-item-card {{
            border: 1px solid #fde68a;
            background: #fffbeb;
            border-radius: 9px;
            padding: 10px 12px;
            font-size: 0.82rem;
            transition: all 0.15s;
            cursor: pointer;
        }}
        .fav-item-card:hover {{
            border-color: #f59e0b;
            box-shadow: 0 2px 6px rgba(245, 158, 11, 0.15);
        }}
        .fav-item-top {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 6px;
            margin-bottom: 4px;
        }}
        .fav-item-title {{
            font-weight: 700;
            color: #0f172a;
            line-height: 1.25;
        }}

        /* Popup Styling inside Leaflet */
        .leaflet-popup-content-wrapper {{
            border-radius: 12px;
            padding: 2px;
            box-shadow: 0 10px 25px rgba(15, 23, 42, 0.18);
        }}
        .leaflet-popup-content {{
            margin: 12px 14px;
            min-width: 310px;
            max-width: 380px;
            font-family: inherit;
        }}
        .popup-header {{
            border-bottom: 1px solid #e2e8f0;
            padding-bottom: 8px;
            margin-bottom: 8px;
        }}
        .popup-badges {{
            display: flex;
            gap: 6px;
            align-items: center;
            margin-bottom: 4px;
            flex-wrap: wrap;
        }}
        .badge {{
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 2px 8px;
            border-radius: 999px;
            font-size: 0.73rem;
            font-weight: 700;
        }}
        .badge-item {{ background: #e0e7ff; color: #1e40af; }}
        .badge-mec {{ background: #f1f5f9; color: #475569; }}
        .badge-integrado {{ background: #dbeafe; color: #1d4ed8; }}
        .badge-subsequente {{ background: #ede9fe; color: #6d28d9; }}
        .badge-turno-manha {{ background: #fef3c7; color: #b45309; }}
        .badge-turno-tarde {{ background: #ffedd5; color: #c2410c; }}
        .badge-turno-noite {{ background: #e0e7ff; color: #3730a3; }}
        .badge-turno-integral {{ background: #d1fae5; color: #065f46; }}
        .popup-courses {{
            max-height: 190px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 6px;
            margin: 8px 0;
        }}
        .popup-course-row {{
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 7px;
            padding: 6px 8px;
            font-size: 0.78rem;
        }}
        .popup-course-row.course-match {{
            border-color: #60a5fa;
            background: #eff6ff;
        }}

        /* Filtered Table Section Below Map */
        .results-section {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 2px 8px rgba(15, 23, 42, 0.04);
        }}
        .results-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 14px;
            flex-wrap: wrap;
            gap: 12px;
        }}
        .results-header h2 {{
            margin: 0;
            font-size: 1.12rem;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .table-wrapper {{
            overflow-x: auto;
        }}
        table.schools-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.86rem;
        }}
        table.schools-table th, table.schools-table td {{
            padding: 12px 10px;
            border-bottom: 1px solid var(--border);
            text-align: left;
            vertical-align: top;
        }}
        table.schools-table th {{
            background: #f8fafc;
            color: #334155;
            font-weight: 700;
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.03em;
            position: sticky;
            top: 0;
            z-index: 2;
        }}
        table.schools-table tr.school-row:hover {{
            background: #f8fafc;
        }}
        table.schools-table tr.school-row.row-favorito {{
            background: #fffdf5;
        }}
        table.schools-table tr.school-row.row-favorito:hover {{
            background: #fef9e7;
        }}
        .star-btn {{
            background: #f8fafc;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            padding: 6px 10px;
            cursor: pointer;
            font-size: 0.85rem;
            font-weight: 700;
            color: #475569;
            display: inline-flex;
            align-items: center;
            gap: 5px;
            transition: all 0.15s;
        }}
        .star-btn:hover {{
            border-color: #f59e0b;
            color: #b45309;
            background: #fffbeb;
        }}
        .star-btn.is-fav {{
            background: #fef3c7;
            border-color: #f59e0b;
            color: #b45309;
        }}
        .course-mini-table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 4px;
            font-size: 0.8rem;
        }}
        .course-mini-table td {{
            padding: 5px 7px;
            border: 1px solid #e2e8f0;
            background: #ffffff;
        }}
        .course-mini-table tr.course-highlight td {{
            background: #eff6ff;
            border-color: #bfdbfe;
        }}
    </style>
</head>
<body>
    <!-- Topbar -->
    <div class="topbar">
        <div class="topbar-title">
            <h1>🗺️ Mapeamento de Escolas e Cursos — Credenciamento SEED/PR (Anexo I)</h1>
            <span>Protocolo nº 26.748.363-9 • 147 Escolas Estaduais • 22 Cursos Integrados & 13 Cursos Subsequentes • Usuário: <strong>{username_esc}</strong></span>
        </div>
        <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
            <a href="/" class="btn btn-secondary">🏠 Início</a>
            <a href="/licitacoes/" class="btn btn-secondary">📄 Painel de Licitações</a>
            <a href="/licitacoes/upload/" class="btn btn-secondary">📤 Upload de PDFs</a>
            <a href="/licitacoes/servidor/" class="btn btn-secondary">🖥️ Monitor do Servidor</a>
            <a href="/admin/credenciamento_parana/escolacredenciamento/" class="btn btn-secondary">⚙️ Admin Escolas</a>
        </div>
    </div>

    <!-- KPI Cards -->
    <div class="kpi-grid">
        <div class="kpi-card">
            <span class="kpi-label">🏫 Escolas na Filtragem</span>
            <span class="kpi-val" id="kpi-escolas">147 / 147</span>
            <span class="kpi-sub" id="kpi-cidades-sub">Em 34 municípios do Paraná</span>
        </div>
        <div class="kpi-card">
            <span class="kpi-label">🎓 Ofertas de Cursos Visíveis</span>
            <span class="kpi-val" id="kpi-cursos">214</span>
            <span class="kpi-sub" id="kpi-modalidades-sub">Médio Integrado + Subsequente</span>
        </div>
        <div class="kpi-card">
            <span class="kpi-label">👥 Total de Turmas & CH Anual</span>
            <span class="kpi-val" id="kpi-turmas">215 turmas</span>
            <span class="kpi-sub" id="kpi-ch-sub">Soma CH Anual (hora-relógio)</span>
        </div>
        <div class="kpi-card" style="border-color:#fde68a; background:#fffdf7;">
            <span class="kpi-label" style="color:#b45309;">⭐ Escolas Favoritas Marcadas</span>
            <span class="kpi-val" id="kpi-favoritos" style="color:#d97706;">0</span>
            <span class="kpi-sub">Salvas no banco para verificação</span>
        </div>
    </div>

    <!-- Rich Filter & Search Panel -->
    <div class="filter-panel">
        <div class="filter-header">
            <h2>🔎 Busca Rica e Filtragem Multicritério (Mapa + Lista Sincronizados)</h2>
            <div style="display:flex; gap:8px; flex-wrap:wrap;">
                <button type="button" id="btn-toggle-favoritos" class="btn btn-fav-filter" onclick="alternarFiltroApenasFavoritos()">
                    ⭐ Verificar Apenas Favoritos (<span id="badge-count-fav">0</span>)
                </button>
                <button type="button" class="btn btn-primary" onclick="ajustarZoomAosFiltrados()">
                    🎯 Enquadrar Filtrados no Mapa
                </button>
                <button type="button" class="btn btn-secondary" onclick="limparTodosFiltros()">
                    🧹 Limpar Filtros
                </button>
            </div>
        </div>

        <div class="filter-grid">
            <!-- 1. Busca Geral -->
            <div class="filter-group">
                <label for="f-busca-geral">🔍 Busca Livre (Escola, Curso, Cidade, MEC...)</label>
                <input type="text" id="f-busca-geral" class="filter-input"
                       placeholder="Ex: Inteligência Artificial, Curitiba, 41025997..."
                       oninput="aplicarFiltros()">
            </div>

            <!-- 2. Nome da Escola -->
            <div class="filter-group">
                <label for="f-escola">🏫 Nome da Escola ou Cód. MEC</label>
                <input type="text" id="f-escola" class="filter-input"
                       placeholder="Ex: Nilo Cairo, Tiradentes, Polivalente..."
                       oninput="aplicarFiltros()">
            </div>

            <!-- 3. Endereço / Bairro / CEP -->
            <div class="filter-group">
                <label for="f-endereco">📍 Endereço, Logradouro, Bairro ou CEP</label>
                <input type="text" id="f-endereco" class="filter-input"
                       placeholder="Ex: Emiliano Perneta, Centro, CIC, Boqueirão..."
                       oninput="aplicarFiltros()">
            </div>

            <!-- 4. Curso Técnico -->
            <div class="filter-group">
                <label for="f-curso">🎓 Filtrar por Curso Técnico</label>
                <select id="f-curso" class="filter-select" onchange="aplicarFiltros()">
                    {opcoes_cursos_html}
                </select>
            </div>

            <!-- 5. Modalidade / Modelo (Integrado / Integral vs Subsequente) -->
            <div class="filter-group">
                <label for="f-modalidade">📘 Modalidade (Integrado / Subsequente / Integral)</label>
                <select id="f-modalidade" class="filter-select" onchange="aplicarFiltros()">
                    <option value="">Todas as Modalidades</option>
                    <option value="integrado">📘 Médio Integrado (Ensino Médio + Técnico)</option>
                    <option value="subsequente">📙 Prof/Técnico (Subsequente - Pós-Médio)</option>
                    <option value="integral_turno">⏱️ Regime em Tempo Integral (Turno Integral)</option>
                    <option value="integrado_ou_integral">📘 Médio Integrado OU Turno Integral</option>
                    <option value="ambas_modalidades">📗 Escolas com Ambas Modalidades (Integrado + Subsequente)</option>
                </select>
            </div>

            <!-- 6. Turno -->
            <div class="filter-group">
                <label for="f-turno">🕒 Filtrar por Turno de Oferta</label>
                <select id="f-turno" class="filter-select" onchange="aplicarFiltros()">
                    <option value="">Todos os Turnos (Manhã, Tarde, Noite, Integral)</option>
                    <option value="Manhã">🌅 Manhã</option>
                    <option value="Tarde">☀️ Tarde</option>
                    <option value="Noite">🌙 Noite</option>
                    <option value="Integral">⏱️ Integral</option>
                </select>
            </div>

            <!-- 7. Município -->
            <div class="filter-group">
                <label for="f-municipio">🏙️ Filtrar por Município</label>
                <select id="f-municipio" class="filter-select" onchange="aplicarFiltros()">
                    {opcoes_municipios_html}
                </select>
            </div>

            <!-- 8. Núcleo Regional de Educação -->
            <div class="filter-group">
                <label for="f-nucleo">🏛️ Núcleo Regional de Educação (NRE)</label>
                <select id="f-nucleo" class="filter-select" onchange="aplicarFiltros()">
                    {opcoes_nucleos_html}
                </select>
            </div>
        </div>

        <!-- Atalhos Rápidos de Cursos do Nicho TI / Tecnologia -->
        <div class="quick-chips">
            <span style="font-size:0.78rem; font-weight:700; color:#475569;">⚡ Atalhos Rápidos de Cursos:</span>
            <button type="button" class="chip-btn" onclick="selecionarChipCurso('')">Todos (147)</button>
            <button type="button" class="chip-btn" onclick="selecionarChipCurso('DESENVOLVIMENTO DE SISTEMAS')">💻 Desenvolvimento de Sistemas</button>
            <button type="button" class="chip-btn" onclick="selecionarChipCurso('INTELIGÊNCIA ARTIFICIAL')">🤖 Inteligência Artificial</button>
            <button type="button" class="chip-btn" onclick="selecionarChipCurso('AUTOMAÇÃO INDUSTRIAL')">⚙️ Automação Industrial</button>
            <button type="button" class="chip-btn" onclick="selecionarChipCurso('MECATRÔNICA')">🦾 Mecatrônica</button>
            <button type="button" class="chip-btn" onclick="selecionarChipCurso('ELETROTÉCNICA')">⚡ Eletrotécnica</button>
            <button type="button" class="chip-btn" onclick="selecionarChipCurso('ELETROMECÂNICA')">🔧 Eletromecânica</button>
            <button type="button" class="chip-btn" onclick="selecionarChipCurso('DESIGN GRÁFICO')">🎨 Design Gráfico</button>
            <button type="button" class="chip-btn" onclick="selecionarChipCurso('ENFERMAGEM')">🩺 Enfermagem</button>
            <button type="button" class="chip-btn" onclick="selecionarChipCurso('GASTRONOMIA')">🍽️ Gastronomia</button>
        </div>
    </div>

    <!-- Map + Favorites Verification Panel -->
    <div class="map-workspace" id="secao-mapa">
        <div class="map-card">
            <div class="map-toolbar">
                <div class="map-legend">
                    <span><i class="legend-dot" style="background:#f59e0b; border:2px solid #fef08a;"></i>⭐ Escola Favorita</span>
                    <span><i class="legend-dot" style="background:#2563eb;"></i>📘 Médio Integrado</span>
                    <span><i class="legend-dot" style="background:#7c3aed;"></i>📙 Subsequente</span>
                    <span><i class="legend-dot" style="background:#059669;"></i>📗 Integrado + Subsequente</span>
                </div>
                <div style="display:flex; gap:8px; align-items:center;">
                    <label style="font-size:0.8rem; display:flex; align-items:center; gap:5px; cursor:pointer; color:#334155; font-weight:600;">
                        <input type="checkbox" id="chk-auto-zoom" checked> Auto-enquadrar ao filtrar
                    </label>
                    <button type="button" class="btn btn-secondary" style="padding:5px 10px; font-size:0.78rem;" onclick="visaoGeralParana()">
                        🗺️ Visão Paraná Inteiro
                    </button>
                </div>
            </div>
            <div id="leaflet-map"></div>
        </div>

        <!-- Painel Lateral de Verificação de Favoritos -->
        <div class="favorites-drawer">
            <div class="favorites-header">
                <h3>⭐ Meus Favoritos (<span id="drawer-fav-count">0</span>)</h3>
                <button type="button" class="btn btn-amber" style="padding:4px 10px; font-size:0.76rem;" onclick="alternarFiltroApenasFavoritos()" id="btn-drawer-filter-fav">
                    Filtrar no Mapa
                </button>
            </div>
            <div style="padding:8px 12px; background:#fffbeb; border-bottom:1px solid #fef3c7; font-size:0.76rem; color:#92400e;">
                Clique na estrela <strong>⭐</strong> em qualquer marcador do mapa ou na tabela abaixo para salvar uma escola como favorita e verificá-la rapidamente aqui.
            </div>
            <div class="favorites-list" id="favorites-drawer-list">
                <!-- Preenchido via JS -->
            </div>
        </div>
    </div>

    <!-- Filtered Results Table Below the Map -->
    <div class="results-section" id="secao-lista-filtrada">
        <div class="results-header">
            <div>
                <h2>📋 Escolas e Cursos Exibidos na Filtragem Atual (<span id="titulo-contagem-escolas">147</span> escolas / <span id="titulo-contagem-cursos">214</span> ofertas)</h2>
                <p style="margin:4px 0 0 0; font-size:0.84rem; color:var(--muted);">
                    Role para baixo para conferir todos os detalhes das escolas que estão aparecendo no mapa acima. Clique em <strong>🎯 Ver no Mapa</strong> para voar até o marcador da escola.
                </p>
            </div>
            <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                <button type="button" class="btn btn-emerald" onclick="exportarFiltradosCSV()">
                    📥 Exportar Lista Filtrada (CSV / Excel)
                </button>
            </div>
        </div>

        <div class="table-wrapper">
            <table class="schools-table">
                <thead>
                    <tr>
                        <th style="width:125px;">Favorito / Mapa</th>
                        <th style="width:70px;">Item</th>
                        <th style="width:190px;">Município / Núcleo (NRE)</th>
                        <th style="width:270px;">Estabelecimento / Cód. MEC</th>
                        <th style="width:270px;">Endereço / Contato (Anexo IV)</th>
                        <th>Cursos Ofertados na Escola (Modalidade, Curso, Turno, Turmas e CH Anual)</th>
                    </tr>
                </thead>
                <tbody id="tabela-escolas-body">
                    <!-- Renderizado dinamicamente em sincronia com o mapa -->
                </tbody>
            </table>
        </div>
    </div>

    <!-- Leaflet 1.9.4 JS -->
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
            integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>

    <script>
        const CSRF_TOKEN = "{csrf_token}";
        const ESCOLAS_DATA = {escolas_json_str};

        let map = null;
        let markersLayer = null;
        const markersByItem = new Map();
        let filtroApenasFavoritos = false;
        let escolasFiltradasAtuais = [...ESCOLAS_DATA];

        // Remove acentos para busca insensível a acentuação e caixa
        function normalizarTexto(str) {{
            return String(str || "")
                .normalize("NFD")
                .replace(/[\\u0300-\\u036f]/g, "")
                .toLowerCase()
                .trim();
        }}

        function escaparHtml(str) {{
            const div = document.createElement("div");
            div.textContent = str == null ? "" : String(str);
            return div.innerHTML;
        }}

        function classeBadgeTurno(turno) {{
            const t = normalizarTexto(turno);
            if (t.includes("integral")) return "badge-turno-integral";
            if (t.includes("noite")) return "badge-turno-noite";
            if (t.includes("tarde")) return "badge-turno-tarde";
            return "badge-turno-manha";
        }}

        function iconeMarcadorEscola(escola) {{
            let cls = "pin-integrado";
            if (escola.favorito) {{
                cls = "pin-favorito";
            }} else if (escola.tem_integrado && escola.tem_subsequente) {{
                cls = "pin-ambos";
            }} else if (escola.tem_subsequente) {{
                cls = "pin-subsequente";
            }}
            const label = escola.favorito ? `⭐${{escola.item_edital}}` : `${{escola.item_edital}}`;
            const size = escola.favorito ? 36 : 30;
            return L.divIcon({{
                className: "epsl-pin-wrapper",
                html: `<div class="epsl-pin ${{cls}}" title="Item ${{escola.item_edital}} — ${{escaparHtml(escola.nome_escola)}} (${{escaparHtml(escola.municipio)}})"><span>${{label}}</span></div>`,
                iconSize: [size, size],
                iconAnchor: [size / 2, size],
                popupAnchor: [0, -size + 4]
            }});
        }}

        function cursoPassaFiltroIndividual(curso, filtros) {{
            if (filtros.curso && curso.nome_curso !== filtros.curso) {{
                return false;
            }}
            if (filtros.turno && curso.turno !== filtros.turno) {{
                return false;
            }}
            if (filtros.modalidade === "integrado" && curso.tipo_modalidade !== "integrado") {{
                return false;
            }}
            if (filtros.modalidade === "subsequente" && curso.tipo_modalidade !== "subsequente") {{
                return false;
            }}
            if (filtros.modalidade === "integral_turno" && curso.turno !== "Integral") {{
                return false;
            }}
            if (filtros.modalidade === "integrado_ou_integral" && !(curso.tipo_modalidade === "integrado" || curso.turno === "Integral")) {{
                return false;
            }}
            if (filtros.buscaGeral) {{
                // Se a busca geral bateu no nome do curso, destaca
                const textoCurso = normalizarTexto(`${{curso.nome_curso}} ${{curso.modalidade}} ${{curso.turno}}`);
                if (textoCurso.includes(filtros.buscaGeral)) {{
                    return true;
                }}
            }}
            return true;
        }}

        function montarHtmlPopup(escola, filtrosAtivos) {{
            const gmapsUrl = `https://www.google.com/maps/search/?api=1&query=${{encodeURIComponent(escola.endereco + ", " + escola.municipio + " - PR")}}`;
            let cursosHtml = "";
            for (const c of escola.cursos) {{
                const isMatch = filtrosAtivos && (filtrosAtivos.curso || filtrosAtivos.turno || filtrosAtivos.modalidade)
                    ? cursoPassaFiltroIndividual(c, filtrosAtivos)
                    : false;
                const badgeMod = c.tipo_modalidade === "subsequente"
                    ? `<span class="badge badge-subsequente">Subsequente</span>`
                    : `<span class="badge badge-integrado">Médio Integrado</span>`;
                const badgeTurno = `<span class="badge ${{classeBadgeTurno(c.turno)}}">${{escaparHtml(c.turno)}}</span>`;

                cursosHtml += `
                    <div class="popup-course-row ${{isMatch ? 'course-match' : ''}}">
                        <div style="font-weight:700; color:#0f172a; margin-bottom:3px;">🎓 ${{escaparHtml(c.nome_curso)}}</div>
                        <div style="display:flex; gap:5px; flex-wrap:wrap; align-items:center; margin-bottom:3px;">
                            ${{badgeMod}}
                            ${{badgeTurno}}
                            <span style="font-size:0.74rem; color:#475569;"><strong>${{c.turmas}}</strong> turma(s)</span>
                        </div>
                        <div style="font-size:0.73rem; color:#64748b;">
                            CH Curso: <strong>${{c.ch_anual_curso}}h</strong> | Coord.: <strong>${{c.ch_anual_coord}}h</strong> | Sup. Téc.: <strong>${{c.ch_anual_sup_tecnico}}h</strong>
                        </div>
                    </div>
                `;
            }}

            const btnFavLabel = escola.favorito ? "⭐ Favorito (Clique p/ remover)" : "☆ Marcar como Favorito";
            const btnFavStyle = escola.favorito
                ? "background:#fef3c7; color:#b45309; border:1px solid #f59e0b;"
                : "background:#f8fafc; color:#334155; border:1px solid #cbd5e1;";

            const contatoInfo = (escola.telefone || escola.email || escola.responsavel)
                ? `<div style="font-size:0.76rem; color:#475569; background:#f8fafc; padding:6px 8px; border-radius:6px; margin-bottom:6px; border:1px solid #e2e8f0;">
                    ${{escola.responsavel ? `<div>👤 <strong>Resp.:</strong> ${{escaparHtml(escola.responsavel)}}</div>` : ""}}
                    ${{escola.telefone ? `<div>📞 <strong>Tel.:</strong> ${{escaparHtml(escola.telefone)}}</div>` : ""}}
                    ${{escola.email ? `<div>✉️ <strong>E-mail:</strong> <a href="mailto:${{escaparHtml(escola.email)}}">${{escaparHtml(escola.email)}}</a></div>` : ""}}
                   </div>`
                : "";

            return `
                <div>
                    <div class="popup-header">
                        <div class="popup-badges">
                            <span class="badge badge-item">Item #${{escola.item_edital}}</span>
                            <span class="badge badge-mec">MEC: ${{escaparHtml(escola.codigo_mec)}}</span>
                            <span class="badge" style="background:#f1f5f9; color:#0f172a;">🏙️ ${{escaparHtml(escola.municipio)}}</span>
                        </div>
                        <div style="font-weight:800; font-size:0.95rem; color:#0f172a; line-height:1.25;">
                            ${{escaparHtml(escola.nome_escola)}}
                        </div>
                        <div style="font-size:0.78rem; color:#475569; margin-top:4px;">
                            📍 ${{escaparHtml(escola.endereco)}}${{escola.cep ? ` • CEP: ${{escaparHtml(escola.cep)}}` : ""}}
                        </div>
                        <div style="font-size:0.74rem; color:#64748b; margin-top:2px;">
                            🏛️ NRE: ${{escaparHtml(escola.nucleo_regional)}}
                        </div>
                    </div>
                    ${{contatoInfo}}
                    <div style="font-size:0.78rem; font-weight:700; color:#334155;">
                        Cursos Ofertados (${{escola.cursos.length}}):
                    </div>
                    <div class="popup-courses">
                        ${{cursosHtml}}
                    </div>
                    <div style="display:flex; gap:6px; margin-top:8px; flex-wrap:wrap;">
                        <button type="button" onclick="alternarFavorito(${{escola.id}})"
                                style="flex:1; padding:7px 10px; border-radius:7px; font-weight:700; font-size:0.78rem; cursor:pointer; ${{btnFavStyle}}">
                            ${{btnFavLabel}}
                        </button>
                        <button type="button" onclick="rolarParaEscolaNaTabela(${{escola.item_edital}})"
                                style="padding:7px 10px; border-radius:7px; font-weight:700; font-size:0.78rem; cursor:pointer; background:#eff6ff; color:#1d4ed8; border:1px solid #bfdbfe;">
                            📋 Ver na Lista
                        </button>
                        <a href="${{gmapsUrl}}" target="_blank" rel="noopener"
                           style="padding:7px 10px; border-radius:7px; font-weight:700; font-size:0.78rem; text-decoration:none; background:#f0fdf4; color:#15803d; border:1px solid #bbf7d0;">
                            🗺️ Maps
                        </a>
                    </div>
                </div>
            `;
        }}

        function inicializarMapa() {{
            const osmLayer = L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
                maxZoom: 19,
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
            }});
            const cartoVoyager = L.tileLayer("https://{{s}}.basemaps.cartocdn.com/rastertiles/voyager/{{z}}/{{x}}/{{y}}{{r}}.png", {{
                maxZoom: 19,
                attribution: '&copy; OpenStreetMap &copy; CARTO'
            }});
            const esriSat = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}", {{
                maxZoom: 19,
                attribution: 'Tiles &copy; Esri'
            }});

            map = L.map("leaflet-map", {{
                center: [-24.65, -51.55],
                zoom: 7,
                layers: [cartoVoyager]
            }});

            L.control.layers({{
                "🗺️ Mapa Nítido (Carto Voyager)": cartoVoyager,
                "🛣️ OpenStreetMap Padrão": osmLayer,
                "🛰️ Satélite (Esri World Imagery)": esriSat
            }}, null, {{ position: "topright" }}).addTo(map);

            markersLayer = L.layerGroup().addTo(map);
            aplicarFiltros(true);
        }}

        function obterFiltrosAtivos() {{
            return {{
                buscaGeral: normalizarTexto(document.getElementById("f-busca-geral").value),
                escola: normalizarTexto(document.getElementById("f-escola").value),
                endereco: normalizarTexto(document.getElementById("f-endereco").value),
                curso: document.getElementById("f-curso").value,
                modalidade: document.getElementById("f-modalidade").value,
                turno: document.getElementById("f-turno").value,
                municipio: document.getElementById("f-municipio").value,
                nucleo: document.getElementById("f-nucleo").value,
                apenasFavoritos: filtroApenasFavoritos
            }};
        }}

        function escolaPassaNosFiltros(escola, filtros) {{
            if (filtros.apenasFavoritos && !escola.favorito) {{
                return false;
            }}
            if (filtros.municipio && escola.municipio !== filtros.municipio) {{
                return false;
            }}
            if (filtros.nucleo && escola.nucleo_regional !== filtros.nucleo) {{
                return false;
            }}
            if (filtros.escola) {{
                const alvoEscola = normalizarTexto(`${{escola.nome_escola}} ${{escola.codigo_mec}} item ${{escola.item_edital}}`);
                if (!alvoEscola.includes(filtros.escola)) {{
                    return false;
                }}
            }}
            if (filtros.endereco) {{
                const alvoEnd = normalizarTexto(`${{escola.endereco}} ${{escola.cep}} ${{escola.municipio}}`);
                if (!alvoEnd.includes(filtros.endereco)) {{
                    return false;
                }}
            }}
            if (filtros.modalidade === "ambas_modalidades") {{
                if (!(escola.tem_integrado && escola.tem_subsequente)) {{
                    return false;
                }}
            }}

            // Verifica se pelo menos 1 oferta de curso da escola satisfaz os filtros de curso/modalidade/turno combinados
            const temFiltroDeCurso = Boolean(
                filtros.curso ||
                filtros.turno ||
                (filtros.modalidade && filtros.modalidade !== "ambas_modalidades")
            );
            if (temFiltroDeCurso) {{
                const algumCursoBate = escola.cursos.some(c => cursoPassaFiltroIndividual(c, filtros));
                if (!algumCursoBate) {{
                    return false;
                }}
            }}

            if (filtros.buscaGeral) {{
                const nomesCursos = escola.cursos.map(c => `${{c.nome_curso}} ${{c.modalidade}} ${{c.turno}}`).join(" ");
                const textoCompleto = normalizarTexto(
                    `${{escola.item_edital}} ${{escola.nome_escola}} ${{escola.codigo_mec}} ${{escola.municipio}} ${{escola.nucleo_regional}} ${{escola.endereco}} ${{escola.cep}} ${{escola.responsavel}} ${{escola.email}} ${{nomesCursos}}`
                );
                // Permite buscar múltiplos termos separados por espaço
                const termos = filtros.buscaGeral.split(/\\s+/).filter(Boolean);
                for (const termo of termos) {{
                    if (!textoCompleto.includes(termo)) {{
                        return false;
                    }}
                }}
            }}

            return true;
        }}

        function aplicarFiltros(isInitialLoad = false) {{
            const filtros = obterFiltrosAtivos();
            escolasFiltradasAtuais = ESCOLAS_DATA.filter(esc => escolaPassaNosFiltros(esc, filtros));

            atualizarMarcadoresNoMapa(escolasFiltradasAtuais, filtros, isInitialLoad);
            atualizarTabelaAbaixoDoMapa(escolasFiltradasAtuais, filtros);
            atualizarKPIsEFavoritos(escolasFiltradasAtuais, filtros);
            atualizarChipsAtivos(filtros.curso);
        }}

        function atualizarMarcadoresNoMapa(escolasVisiveis, filtros, isInitialLoad) {{
            markersLayer.clearLayers();
            markersByItem.clear();

            const coords = [];
            for (const esc of escolasVisiveis) {{
                const marker = L.marker([esc.latitude, esc.longitude], {{
                    icon: iconeMarcadorEscola(esc),
                    zIndexOffset: esc.favorito ? 1000 : 0
                }});
                marker.bindPopup(() => montarHtmlPopup(esc, filtros), {{ maxWidth: 390 }});
                marker.addTo(markersLayer);
                markersByItem.set(esc.item_edital, marker);
                coords.push([esc.latitude, esc.longitude]);
            }}

            const autoZoom = document.getElementById("chk-auto-zoom").checked;
            if (!isInitialLoad && autoZoom && coords.length > 0) {{
                ajustarZoomAosFiltrados();
            }}
        }}

        function ajustarZoomAosFiltrados() {{
            if (!map || escolasFiltradasAtuais.length === 0) return;
            const latLngs = escolasFiltradasAtuais.map(e => [e.latitude, e.longitude]);
            if (latLngs.length === 1) {{
                map.flyTo(latLngs[0], 15, {{ duration: 0.8 }});
                const m = markersByItem.get(escolasFiltradasAtuais[0].item_edital);
                if (m) m.openPopup();
            }} else {{
                const bounds = L.latLngBounds(latLngs);
                map.flyToBounds(bounds, {{ padding: [40, 40], maxZoom: 14, duration: 0.7 }});
            }}
        }}

        function visaoGeralParana() {{
            if (!map) return;
            map.flyTo([-24.65, -51.55], 7, {{ duration: 0.7 }});
        }}

        function atualizarKPIsEFavoritos(escolasVisiveis, filtros) {{
            const totalEscolas = ESCOLAS_DATA.length;
            const cidadesVisiveis = new Set(escolasVisiveis.map(e => e.municipio)).size;

            let ofertasVisiveis = 0;
            let turmasVisiveis = 0;
            let chTotalVisivel = 0;

            const temFiltroCurso = Boolean(filtros.curso || filtros.turno || (filtros.modalidade && filtros.modalidade !== "ambas_modalidades"));

            for (const esc of escolasVisiveis) {{
                for (const c of esc.cursos) {{
                    if (!temFiltroCurso || cursoPassaFiltroIndividual(c, filtros)) {{
                        ofertasVisiveis += 1;
                        turmasVisiveis += c.turmas;
                        chTotalVisivel += c.ch_total_oferta;
                    }}
                }}
            }}

            const favoritas = ESCOLAS_DATA.filter(e => e.favorito);

            document.getElementById("kpi-escolas").textContent = `${{escolasVisiveis.length}} / ${{totalEscolas}}`;
            document.getElementById("kpi-cidades-sub").textContent = `Em ${{cidadesVisiveis}} município(s) do Paraná`;
            document.getElementById("kpi-cursos").textContent = `${{ofertasVisiveis}}`;
            document.getElementById("kpi-turmas").textContent = `${{turmasVisiveis}} turma(s)`;
            document.getElementById("kpi-ch-sub").textContent = `${{chTotalVisivel.toLocaleString("pt-BR")}} horas anuais (total)`;
            document.getElementById("kpi-favoritos").textContent = `${{favoritas.length}}`;
            document.getElementById("badge-count-fav").textContent = `${{favoritas.length}}`;
            document.getElementById("drawer-fav-count").textContent = `${{favoritas.length}}`;

            document.getElementById("titulo-contagem-escolas").textContent = `${{escolasVisiveis.length}}`;
            document.getElementById("titulo-contagem-cursos").textContent = `${{ofertasVisiveis}}`;

            const btnFav = document.getElementById("btn-toggle-favoritos");
            const btnDrawerFav = document.getElementById("btn-drawer-filter-fav");
            if (filtroApenasFavoritos) {{
                btnFav.classList.add("active");
                btnDrawerFav.textContent = "Mostrar Todas (147)";
            }} else {{
                btnFav.classList.remove("active");
                btnDrawerFav.textContent = "Filtrar no Mapa";
            }}

            renderizarGavetaFavoritos(favoritas);
        }}

        function renderizarGavetaFavoritos(favoritas) {{
            const container = document.getElementById("favorites-drawer-list");
            if (favoritas.length === 0) {{
                container.innerHTML = `
                    <div style="text-align:center; padding:32px 14px; color:#64748b; font-size:0.84rem;">
                        <div style="font-size:2rem; margin-bottom:8px;">⭐</div>
                        <strong>Nenhuma escola favoritada ainda.</strong>
                        <p style="margin-top:6px;">Clique no botão <strong>☆ Favoritar</strong> em qualquer marcador do mapa ou na lista abaixo para guardar suas escolas de interesse aqui.</p>
                    </div>
                `;
                return;
            }}

            let htmlFavs = "";
            for (const esc of favoritas) {{
                const resumoCursos = esc.cursos.map(c => `${{c.nome_curso}} (${{c.turno}})`).join(" • ");
                htmlFavs += `
                    <div class="fav-item-card" onclick="focarEscolaNoMapa(${{esc.item_edital}})">
                        <div class="fav-item-top">
                            <span class="badge badge-item">Item #${{esc.item_edital}}</span>
                            <button type="button" onclick="event.stopPropagation(); alternarFavorito(${{esc.id}});"
                                    style="background:none; border:none; cursor:pointer; font-size:0.85rem; color:#b45309; font-weight:700;"
                                    title="Remover dos favoritos">
                                ✖ Remover
                            </button>
                        </div>
                        <div class="fav-item-title">${{escaparHtml(esc.nome_escola)}}</div>
                        <div style="color:#475569; font-size:0.76rem; margin-top:3px;">
                            🏙️ <strong>${{escaparHtml(esc.municipio)}}</strong> • 📍 ${{escaparHtml(esc.endereco)}}
                        </div>
                        <div style="color:#1d4ed8; font-size:0.74rem; margin-top:4px; font-weight:600;">
                            🎓 ${{escaparHtml(resumoCursos)}}
                        </div>
                    </div>
                `;
            }}
            container.innerHTML = htmlFavs;
        }}

        function atualizarTabelaAbaixoDoMapa(escolasVisiveis, filtros) {{
            const tbody = document.getElementById("tabela-escolas-body");
            if (escolasVisiveis.length === 0) {{
                tbody.innerHTML = `
                    <tr>
                        <td colspan="6" style="text-align:center; padding:36px; color:#64748b; font-size:0.95rem;">
                            🔍 Nenhuma escola encontrada para a combinação atual de filtros. Clique em <strong>🧹 Limpar Filtros</strong> para restaurar todas as 147 escolas.
                        </td>
                    </tr>
                `;
                return;
            }}

            const temFiltroCurso = Boolean(filtros.curso || filtros.turno || (filtros.modalidade && filtros.modalidade !== "ambas_modalidades"));

            let rowsHtml = "";
            for (const esc of escolasVisiveis) {{
                let cursosRowsHtml = "";
                for (const c of esc.cursos) {{
                    const bateFiltro = temFiltroCurso && cursoPassaFiltroIndividual(c, filtros);
                    const badgeMod = c.tipo_modalidade === "subsequente"
                        ? `<span class="badge badge-subsequente">Subsequente</span>`
                        : `<span class="badge badge-integrado">Médio Integrado</span>`;
                    const badgeTurno = `<span class="badge ${{classeBadgeTurno(c.turno)}}">${{escaparHtml(c.turno)}}</span>`;

                    cursosRowsHtml += `
                        <tr class="${{bateFiltro ? 'course-highlight' : ''}}">
                            <td style="width:155px;">${{badgeMod}}</td>
                            <td style="font-weight:700; color:#0f172a;">${{escaparHtml(c.nome_curso)}}</td>
                            <td style="width:95px;">${{badgeTurno}}</td>
                            <td style="width:80px; text-align:center;"><strong>${{c.turmas}}</strong> turma(s)</td>
                            <td style="width:210px; font-size:0.75rem; color:#475569;">
                                Curso: <strong>${{c.ch_anual_curso}}h</strong> | Coord: <strong>${{c.ch_anual_coord}}h</strong> | Sup: <strong>${{c.ch_anual_sup_tecnico}}h</strong>
                            </td>
                        </tr>
                    `;
                }}

                const favClass = esc.favorito ? "is-fav" : "";
                const favText = esc.favorito ? "⭐ Favorito" : "☆ Favoritar";
                const gmapsUrl = `https://www.google.com/maps/search/?api=1&query=${{encodeURIComponent(esc.endereco + ", " + esc.municipio + " - PR")}}`;

                rowsHtml += `
                    <tr id="escola-row-${{esc.item_edital}}" class="school-row ${{esc.favorito ? 'row-favorito' : ''}}">
                        <td>
                            <div style="display:flex; flex-direction:column; gap:6px;">
                                <button type="button" class="star-btn ${{favClass}}" onclick="alternarFavorito(${{esc.id}})">
                                    ${{favText}}
                                </button>
                                <button type="button" class="btn btn-secondary" style="padding:5px 9px; font-size:0.78rem; justify-content:center;"
                                        onclick="focarEscolaNoMapa(${{esc.item_edital}}, true)">
                                    🎯 Ver no Mapa
                                </button>
                            </div>
                        </td>
                        <td>
                            <span class="badge badge-item" style="font-size:0.84rem; padding:4px 9px;">#${{esc.item_edital}}</span>
                        </td>
                        <td>
                            <div style="font-weight:800; color:#0f172a; font-size:0.9rem;">${{escaparHtml(esc.municipio)}}</div>
                            <div style="font-size:0.77rem; color:#64748b; margin-top:3px;">NRE: ${{escaparHtml(esc.nucleo_regional)}}</div>
                        </td>
                        <td>
                            <div style="font-weight:700; color:#1e293b; line-height:1.3;">${{escaparHtml(esc.nome_escola)}}</div>
                            <div style="margin-top:5px;">
                                <span class="badge badge-mec">Cód. MEC: ${{escaparHtml(esc.codigo_mec)}}</span>
                            </div>
                        </td>
                        <td>
                            <div style="font-weight:600; color:#334155; line-height:1.3;">📍 ${{escaparHtml(esc.endereco)}}</div>
                            ${{esc.cep ? `<div style="font-size:0.76rem; color:#64748b; margin-top:2px;">CEP: ${{escaparHtml(esc.cep)}}</div>` : ""}}
                            ${{esc.responsavel ? `<div style="font-size:0.76rem; color:#475569; margin-top:4px;">👤 ${{escaparHtml(esc.responsavel)}}</div>` : ""}}
                            ${{esc.telefone ? `<div style="font-size:0.76rem; color:#475569;">📞 ${{escaparHtml(esc.telefone)}}</div>` : ""}}
                            ${{esc.email ? `<div style="font-size:0.76rem;"><a href="mailto:${{escaparHtml(esc.email)}}" style="color:#2563eb;">✉️ ${{escaparHtml(esc.email)}}</a></div>` : ""}}
                            <div style="margin-top:4px;">
                                <a href="${{gmapsUrl}}" target="_blank" rel="noopener" style="font-size:0.75rem; color:#059669; font-weight:600; text-decoration:none;">
                                    🗺️ Abrir no Google Maps ↗
                                </a>
                            </div>
                        </td>
                        <td>
                            <table class="course-mini-table">
                                <tbody>
                                    ${{cursosRowsHtml}}
                                </tbody>
                            </table>
                        </td>
                    </tr>
                `;
            }}

            tbody.innerHTML = rowsHtml;
        }}

        async function alternarFavorito(escolaId) {{
            const escola = ESCOLAS_DATA.find(e => e.id === escolaId);
            if (!escola) return;

            const novoEstado = !escola.favorito;
            escola.favorito = novoEstado;

            // Atualiza imediatamente no mapa, nos KPIs e na tabela
            const filtros = obterFiltrosAtivos();
            escolasFiltradasAtuais = ESCOLAS_DATA.filter(esc => escolaPassaNosFiltros(esc, filtros));

            const marker = markersByItem.get(escola.item_edital);
            if (marker) {{
                if (filtros.apenasFavoritos && !escola.favorito) {{
                    markersLayer.removeLayer(marker);
                    markersByItem.delete(escola.item_edital);
                }} else {{
                    marker.setIcon(iconeMarcadorEscola(escola));
                    marker.setZIndexOffset(escola.favorito ? 1000 : 0);
                    if (marker.isPopupOpen()) {{
                        marker.setPopupContent(montarHtmlPopup(escola, filtros));
                    }}
                }}
            }} else if (escolaPassaNosFiltros(escola, filtros)) {{
                atualizarMarcadoresNoMapa(escolasFiltradasAtuais, filtros, true);
            }}

            atualizarTabelaAbaixoDoMapa(escolasFiltradasAtuais, filtros);
            atualizarKPIsEFavoritos(escolasFiltradasAtuais, filtros);

            try {{
                await fetch(`/credenciamento/escola/${{escolaId}}/favorito/`, {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/json",
                        "X-CSRFToken": CSRF_TOKEN,
                        "X-Requested-With": "XMLHttpRequest"
                    }},
                    body: JSON.stringify({{ favorito: novoEstado }})
                }});
            }} catch (err) {{
                console.error("Erro ao persistir favorito:", err);
            }}
        }}

        function alternarFiltroApenasFavoritos() {{
            filtroApenasFavoritos = !filtroApenasFavoritos;
            aplicarFiltros();
        }}

        function focarEscolaNoMapa(itemEdital, rolarParaMapa = false) {{
            const escola = ESCOLAS_DATA.find(e => e.item_edital === itemEdital);
            if (!escola || !map) return;

            if (rolarParaMapa) {{
                document.getElementById("secao-mapa").scrollIntoView({{ behavior: "smooth", block: "start" }});
            }}

            let marker = markersByItem.get(itemEdital);
            if (!marker) {{
                // Se estava oculta pelo filtro atual, cria/abre temporariamente
                const filtros = obterFiltrosAtivos();
                marker = L.marker([escola.latitude, escola.longitude], {{
                    icon: iconeMarcadorEscola(escola),
                    zIndexOffset: 1000
                }}).addTo(markersLayer);
                marker.bindPopup(() => montarHtmlPopup(escola, filtros), {{ maxWidth: 390 }});
                markersByItem.set(itemEdital, marker);
            }}

            map.flyTo([escola.latitude, escola.longitude], 16, {{ duration: 0.75 }});
            setTimeout(() => {{
                marker.openPopup();
            }}, 400);
        }}

        function rolarParaEscolaNaTabela(itemEdital) {{
            const row = document.getElementById(`escola-row-${{itemEdital}}`);
            if (row) {{
                row.scrollIntoView({{ behavior: "smooth", block: "center" }});
                row.style.transition = "background 0.3s";
                const oldBg = row.style.background;
                row.style.background = "#fef08a";
                setTimeout(() => {{
                    row.style.background = oldBg;
                }}, 1800);
            }}
        }}

        function selecionarChipCurso(nomeCurso) {{
            const sel = document.getElementById("f-curso");
            sel.value = nomeCurso;
            aplicarFiltros();
        }}

        function atualizarChipsAtivos(cursoAtual) {{
            const chips = document.querySelectorAll(".quick-chips .chip-btn");
            chips.forEach(btn => {{
                const onclickAttr = btn.getAttribute("onclick") || "";
                if (cursoAtual === "" && onclickAttr.includes("''")) {{
                    btn.classList.add("active");
                }} else if (cursoAtual && onclickAttr.includes(`'${{cursoAtual}}'`)) {{
                    btn.classList.add("active");
                }} else {{
                    btn.classList.remove("active");
                }}
            }});
        }}

        function limparTodosFiltros() {{
            document.getElementById("f-busca-geral").value = "";
            document.getElementById("f-escola").value = "";
            document.getElementById("f-endereco").value = "";
            document.getElementById("f-curso").value = "";
            document.getElementById("f-modalidade").value = "";
            document.getElementById("f-turno").value = "";
            document.getElementById("f-municipio").value = "";
            document.getElementById("f-nucleo").value = "";
            filtroApenasFavoritos = false;
            aplicarFiltros(true);
            visaoGeralParana();
        }}

        function exportarFiltradosCSV() {{
            if (escolasFiltradasAtuais.length === 0) {{
                alert("Nenhuma escola na filtragem atual para exportar.");
                return;
            }}
            const cabecalho = [
                "Item", "Favorito", "Nucleo Regional", "Municipio", "Escola",
                "Cod. MEC", "Endereco", "CEP", "Telefone", "Email", "Responsavel",
                "Modalidade", "Curso", "Turno", "Turmas", "CH Anual Curso",
                "CH Anual Coord", "CH Anual Sup Tecnico", "Latitude", "Longitude"
            ];
            const linhas = [cabecalho.join(";")];
            const filtros = obterFiltrosAtivos();
            const temFiltroCurso = Boolean(filtros.curso || filtros.turno || (filtros.modalidade && filtros.modalidade !== "ambas_modalidades"));

            for (const esc of escolasFiltradasAtuais) {{
                for (const c of esc.cursos) {{
                    if (temFiltroCurso && !cursoPassaFiltroIndividual(c, filtros)) continue;
                    const cols = [
                        esc.item_edital,
                        esc.favorito ? "SIM" : "NAO",
                        `"${{(esc.nucleo_regional || "").replace(/"/g, '""')}}"`,
                        `"${{(esc.municipio || "").replace(/"/g, '""')}}"`,
                        `"${{(esc.nome_escola || "").replace(/"/g, '""')}}"`,
                        `"${{(esc.codigo_mec || "").replace(/"/g, '""')}}"`,
                        `"${{(esc.endereco || "").replace(/"/g, '""')}}"`,
                        `"${{(esc.cep || "").replace(/"/g, '""')}}"`,
                        `"${{(esc.telefone || "").replace(/"/g, '""')}}"`,
                        `"${{(esc.email || "").replace(/"/g, '""')}}"`,
                        `"${{(esc.responsavel || "").replace(/"/g, '""')}}"`,
                        `"${{(c.modalidade || "").replace(/"/g, '""')}}"`,
                        `"${{(c.nome_curso || "").replace(/"/g, '""')}}"`,
                        `"${{(c.turno || "").replace(/"/g, '""')}}"`,
                        c.turmas,
                        c.ch_anual_curso,
                        c.ch_anual_coord,
                        c.ch_anual_sup_tecnico,
                        esc.latitude,
                        esc.longitude
                    ];
                    linhas.push(cols.join(";"));
                }}
            }}

            const csvContent = "\\uFEFF" + linhas.join("\\n");
            const blob = new Blob([csvContent], {{ type: "text/csv;charset=utf-8;" }});
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `credenciamento_parana_filtrado_${{escolasFiltradasAtuais.length}}_escolas.csv`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        }}

        document.addEventListener("DOMContentLoaded", inicializarMapa);
    </script>
</body>
</html>"""
    return HttpResponse(pagina_html)
