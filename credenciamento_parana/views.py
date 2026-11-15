import html
import json
import urllib.request
from collections import Counter
from pathlib import Path

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from credenciamento_parana.dados_anexo1 import sincronizar_escolas_credenciamento
from credenciamento_parana.models import (
    EscolaCredenciamento,
    OfertaCursoEscola,
    TabelaPrecoCurso,
)


def _serializar_escolas_com_cursos() -> list[dict]:
    """
    Garante que o banco contenha as 147 escolas, 214 ofertas de cursos do Anexo I
    e a Tabela de Preços (`TabelaPrecoCurso`), retornando a lista completa estruturada
    para o mapa Leaflet, cálculo de taxa de deslocamento e listagem dinâmica.
    """
    sincronizar_escolas_credenciamento(forcar_atualizacao=False)
    mapa_precos = {
        (p.nome_curso, p.tipo_modalidade): p
        for p in TabelaPrecoCurso.objects.all()
    }
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

            preco_obj = mapa_precos.get((of.nome_curso, of.tipo_modalidade))
            vh_aluno = float(preco_obj.valor_hora_aluno) if preco_obj else 0.0
            vh_turma = float(preco_obj.valor_hora_turma) if preco_obj else 0.0
            v_turma_ano = float(preco_obj.valor_turma_ano) if preco_obj else 0.0
            v_lucro_1a3 = float(preco_obj.lucro_ano_1a3) if preco_obj else 0.0
            v_lucro_4a5 = float(preco_obj.lucro_ano_4a5) if preco_obj else 0.0
            v_lucro_medio = float(preco_obj.lucro_medio_5anos) if preco_obj else 0.0
            alunos_ref = int(preco_obj.alunos_por_turma) if preco_obj else 35

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
                    "alunos_por_turma": alunos_ref,
                    "valor_hora_aluno": vh_aluno,
                    "valor_hora_turma": vh_turma,
                    "valor_turma_ano": v_turma_ano,
                    "lucro_ano_1a3": v_lucro_1a3,
                    "lucro_ano_4a5": v_lucro_4a5,
                    "lucro_medio_5anos": v_lucro_medio,
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


_TILE_CACHE_DIR = Path("/tmp/epsl_osm_tiles_cache")
_OSM_HEADERS = {
    "User-Agent": "EPSL-SEED-PR-Credenciamento/1.0 (+https://epsl.eliveltonpardini.com.br; contato@eliveltonpardini.com.br)",
    "Referer": "https://epsl.eliveltonpardini.com.br/credenciamento/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


@login_required
@require_GET
def servir_tile_mapa(
    request: HttpRequest, provider: str, z: int, x: int, y: int
) -> HttpResponse:
    """
    Proxy e Cache local em disco para tiles do OpenStreetMap (`osm`, `osm_de`, `esri_street`).
    Evita o erro 'Access blocked: app is not following the tile usage policy' causado quando
    o navegador omite o header Referer (`Referrer-Policy: same-origin` ou bloqueadores) e
    reduz drasticamente requisições repetidas aos servidores comunitários do OSM.
    """
    if z < 0 or z > 20 or x < 0 or y < 0:
        raise Http404("Coordenada de tile inválida.")

    provider_clean = (provider or "osm").strip().lower()
    if provider_clean not in ("osm", "osm_de", "esri_street"):
        provider_clean = "osm"

    cache_file = _TILE_CACHE_DIR / provider_clean / str(z) / f"{x}_{y}.png"
    if cache_file.exists():
        try:
            tile_bytes = cache_file.read_bytes()
            # A imagem "Access blocked" do OSM tem 6672 ou 6725 bytes; nunca servimos ela do cache
            if len(tile_bytes) not in (6672, 6725) and len(tile_bytes) > 100:
                resp = HttpResponse(tile_bytes, content_type="image/png")
                resp["Cache-Control"] = "public, max-age=604800, immutable"
                return resp
        except Exception:
            pass

    urls_tentativa = []
    if provider_clean == "osm":
        urls_tentativa = [
            f"https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            f"https://tile.openstreetmap.de/{z}/{x}/{y}.png",
            f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        ]
    elif provider_clean == "osm_de":
        urls_tentativa = [
            f"https://tile.openstreetmap.de/{z}/{x}/{y}.png",
            f"https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        ]
    else:
        urls_tentativa = [
            f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
            f"https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        ]

    for url in urls_tentativa:
        try:
            req = urllib.request.Request(url, headers=_OSM_HEADERS)
            with urllib.request.urlopen(req, timeout=6) as upstream:
                data = upstream.read()
                if data and len(data) not in (6672, 6725):
                    try:
                        cache_file.parent.mkdir(parents=True, exist_ok=True)
                        cache_file.write_bytes(data)
                    except Exception:
                        pass
                    resp = HttpResponse(data, content_type="image/png")
                    resp["Cache-Control"] = "public, max-age=604800, immutable"
                    return resp
        except Exception:
            continue

    raise Http404("Tile indisponível no momento.")


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

    opcoes_cursos_html = ""
    for curso_nome in sorted(contagem_cursos.keys()):
        qtd_esc = contagem_cursos[curso_nome]
        curso_esc = html.escape(curso_nome)
        opcoes_cursos_html += (
            f'<label class="ms-item" data-search="{curso_esc.lower()}">'
            f'<input type="checkbox" name="ms-curso" value="{curso_esc}" onchange="aoMudarMultiSelect(\'cursos\')">'
            f'<span class="ms-item-text">{curso_esc}</span>'
            f'<span class="ms-item-count">{qtd_esc} esc.</span>'
            f'</label>'
        )

    opcoes_municipios_html = ""
    for mun_nome in sorted(contagem_municipios.keys()):
        qtd_esc = contagem_municipios[mun_nome]
        mun_esc = html.escape(mun_nome)
        opcoes_municipios_html += (
            f'<label class="ms-item" data-search="{mun_esc.lower()}">'
            f'<input type="checkbox" name="ms-municipio" value="{mun_esc}" onchange="aoMudarMultiSelect(\'municipios\')">'
            f'<span class="ms-item-text">{mun_esc}</span>'
            f'<span class="ms-item-count">{qtd_esc} esc.</span>'
            f'</label>'
        )

    opcoes_nucleos_html = ""
    for nuc_nome in sorted(contagem_nucleos.keys()):
        qtd_esc = contagem_nucleos[nuc_nome]
        nuc_esc = html.escape(nuc_nome)
        opcoes_nucleos_html += (
            f'<label class="ms-item" data-search="{nuc_esc.lower()}">'
            f'<input type="checkbox" name="ms-nucleo" value="{nuc_esc}" onchange="aoMudarMultiSelect(\'nucleos\')">'
            f'<span class="ms-item-text">{nuc_esc}</span>'
            f'<span class="ms-item-count">{qtd_esc} esc.</span>'
            f'</label>'
        )

    escolas_json_str = json.dumps(escolas_data, ensure_ascii=False)

    pagina_html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="referrer" content="origin-when-cross-origin">
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

        /* Custom Multi-Select Dropdowns */
        .ms-dropdown {{
            position: relative;
            width: 100%;
        }}
        .ms-trigger {{
            width: 100%;
            padding: 9px 12px;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            font-size: 0.86rem;
            background: #ffffff;
            color: #0f172a;
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 8px;
            text-align: left;
            transition: border-color 0.15s, box-shadow 0.15s;
        }}
        .ms-trigger:hover {{
            border-color: #94a3b8;
        }}
        .ms-trigger.has-selection {{
            border-color: var(--primary);
            background: #eff6ff;
            font-weight: 700;
            color: #1e40af;
        }}
        .ms-trigger-text {{
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            flex: 1;
        }}
        .ms-count-badge {{
            background: var(--primary);
            color: #ffffff;
            font-size: 0.72rem;
            font-weight: 800;
            padding: 2px 7px;
            border-radius: 999px;
            display: none;
        }}
        .ms-trigger.has-selection .ms-count-badge {{
            display: inline-block;
        }}
        .ms-menu {{
            display: none;
            position: absolute;
            top: calc(100% + 4px);
            left: 0;
            right: 0;
            min-width: 280px;
            background: #ffffff;
            border: 1px solid #cbd5e1;
            border-radius: 10px;
            box-shadow: 0 12px 28px rgba(15, 23, 42, 0.18);
            z-index: 1500;
            padding: 8px;
        }}
        .ms-dropdown.open .ms-menu {{
            display: block;
        }}
        .ms-search-box {{
            width: 100%;
            padding: 7px 10px;
            border: 1px solid #cbd5e1;
            border-radius: 6px;
            font-size: 0.82rem;
            margin-bottom: 6px;
            outline: none;
        }}
        .ms-search-box:focus {{
            border-color: var(--primary);
        }}
        .ms-actions {{
            display: flex;
            justify-content: space-between;
            gap: 6px;
            margin-bottom: 6px;
            padding-bottom: 6px;
            border-bottom: 1px solid #e2e8f0;
        }}
        .ms-action-btn {{
            flex: 1;
            padding: 4px 8px;
            font-size: 0.75rem;
            font-weight: 700;
            border-radius: 6px;
            border: 1px solid #cbd5e1;
            background: #f8fafc;
            color: #334155;
            cursor: pointer;
        }}
        .ms-action-btn:hover {{
            background: #e2e8f0;
        }}
        .ms-list {{
            max-height: 230px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 2px;
        }}
        .ms-item {{
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 6px 8px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.81rem;
            font-weight: 500 !important;
            color: #1e293b !important;
            user-select: none;
        }}
        .ms-item:hover {{
            background: #f1f5f9;
        }}
        .ms-item input[type="checkbox"] {{
            width: 15px;
            height: 15px;
            accent-color: var(--primary);
            cursor: pointer;
            margin: 0;
        }}
        .ms-item-text {{
            flex: 1;
            line-height: 1.25;
        }}
        .ms-item-count {{
            font-size: 0.72rem;
            color: #64748b;
            background: #f1f5f9;
            padding: 1px 6px;
            border-radius: 999px;
            font-weight: 700;
        }}

        /* Active Selected Filter Pills */
        .active-filters-bar {{
            display: none;
            flex-wrap: wrap;
            gap: 6px;
            margin-top: 12px;
            padding: 10px 12px;
            background: #eff6ff;
            border: 1px solid #bfdbfe;
            border-radius: 8px;
            align-items: center;
        }}
        .active-filters-bar.visible {{
            display: flex;
        }}
        .active-pill {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: #ffffff;
            border: 1px solid #93c5fd;
            color: #1e3a8a;
            padding: 3px 9px;
            border-radius: 999px;
            font-size: 0.77rem;
            font-weight: 700;
        }}
        .active-pill button {{
            background: none;
            border: none;
            color: #dc2626;
            font-weight: 800;
            cursor: pointer;
            padding: 0 2px;
            font-size: 0.82rem;
            line-height: 1;
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
        .pricing-rate-panel {{
            margin-top: 14px;
            padding: 12px 14px;
            background: linear-gradient(135deg, #f0fdf4, #eff6ff);
            border: 1px solid #bfdbfe;
            border-radius: 10px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }}
        .pricing-rate-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 8px;
        }}
        .pricing-rate-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(195px, 1fr));
            gap: 10px;
            align-items: end;
        }}
        .rate-badge {{
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 3px 9px;
            border-radius: 999px;
            font-size: 0.74rem;
            font-weight: 800;
        }}
        .rate-badge-zero {{
            background: #dcfce7;
            color: #166534;
            border: 1px solid #86efac;
        }}
        .rate-badge-mid {{
            background: #fef3c7;
            color: #92400e;
            border: 1px solid #fcd34d;
        }}
        .rate-badge-max {{
            background: #fee2e2;
            color: #991b1b;
            border: 1px solid #fca5a5;
        }}
        .price-tag-box {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: #f0fdf4;
            border: 1px solid #bbf7d0;
            color: #166534;
            padding: 3px 8px;
            border-radius: 6px;
            font-size: 0.76rem;
            font-weight: 700;
        }}
        .price-tag-zero {{
            display: inline-flex;
            align-items: center;
            gap: 5px;
            background: #f8fafc;
            border: 1px solid #cbd5e1;
            color: #64748b;
            padding: 2px 7px;
            border-radius: 6px;
            font-size: 0.74rem;
            font-weight: 600;
        }}
    </style>
</head>
<body>
    <!-- Topbar -->
    <div class="topbar">
        <div class="topbar-title">
            <h1>🗺️ Mapeamento de Escolas e Cursos — Credenciamento SEED/PR (Anexo I)</h1>
            <span>Protocolo nº 26.748.363-9 • 147 Escolas Estaduais • Preços H/Aluno & Taxa de Deslocamento Regional • Usuário: <strong>{username_esc}</strong></span>
        </div>
        <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
            <a href="/" class="btn btn-secondary">🏠 Início</a>
            <a href="/licitacoes/" class="btn btn-secondary">📄 Painel de Licitações</a>
            <a href="/licitacoes/upload/" class="btn btn-secondary">📤 Upload de PDFs</a>
            <a href="/licitacoes/servidor/" class="btn btn-secondary">🖥️ Monitor do Servidor</a>
            <a href="/admin/credenciamento_parana/tabelaprecocurso/" class="btn btn-emerald" target="_blank">💰 Preços H/Aluno (Admin)</a>
            <a href="/admin/credenciamento_parana/escolacredenciamento/" class="btn btn-secondary" target="_blank">⚙️ Admin Escolas</a>
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
        <div class="kpi-card" style="border-color:#bbf7d0; background:#f0fdf4;">
            <span class="kpi-label" style="color:#166534;">💰 Receita Anual (c/ Taxa Desloc.)</span>
            <span class="kpi-val" id="kpi-receita-anual" style="color:#15803d; font-size:1.25rem;">R$ 0,00</span>
            <span class="kpi-sub" id="kpi-lucro-sub" style="color:#166534;">Lucro Médio 5 anos: R$ 0,00</span>
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

            <!-- 4. Curso Técnico (Multi-Select) -->
            <div class="filter-group">
                <label>🎓 Cursos Técnicos (Múltipla Escolha)</label>
                <div class="ms-dropdown" id="ms-dropdown-cursos">
                    <button type="button" class="ms-trigger" id="ms-trigger-cursos" onclick="alternarMenuMultiSelect('cursos', event)">
                        <span class="ms-trigger-text" id="ms-label-cursos">Todos os Cursos (28)</span>
                        <span class="ms-count-badge" id="ms-badge-cursos">0</span>
                        <span>▾</span>
                    </button>
                    <div class="ms-menu" onclick="event.stopPropagation()">
                        <input type="text" class="ms-search-box" placeholder="Filtrar cursos (ex: Gastronomia, Eletro)..."
                               oninput="filtrarOpcoesMultiSelect('cursos', this.value)">
                        <div class="ms-actions">
                            <button type="button" class="ms-action-btn" onclick="marcarTodosMultiSelect('cursos', true)">✅ Marcar visíveis</button>
                            <button type="button" class="ms-action-btn" onclick="marcarTodosMultiSelect('cursos', false)">🧹 Limpar cursos</button>
                        </div>
                        <div class="ms-list" id="ms-list-cursos">
                            {opcoes_cursos_html}
                        </div>
                    </div>
                </div>
            </div>

            <!-- 5. Modalidade / Modelo (Multi-Select) -->
            <div class="filter-group">
                <label>📘 Modalidades (Múltipla Escolha)</label>
                <div class="ms-dropdown" id="ms-dropdown-modalidades">
                    <button type="button" class="ms-trigger" id="ms-trigger-modalidades" onclick="alternarMenuMultiSelect('modalidades', event)">
                        <span class="ms-trigger-text" id="ms-label-modalidades">Todas as Modalidades</span>
                        <span class="ms-count-badge" id="ms-badge-modalidades">0</span>
                        <span>▾</span>
                    </button>
                    <div class="ms-menu" onclick="event.stopPropagation()">
                        <div class="ms-actions">
                            <button type="button" class="ms-action-btn" onclick="marcarTodosMultiSelect('modalidades', true)">✅ Marcar todas</button>
                            <button type="button" class="ms-action-btn" onclick="marcarTodosMultiSelect('modalidades', false)">🧹 Limpar</button>
                        </div>
                        <div class="ms-list" id="ms-list-modalidades">
                            <label class="ms-item" data-search="medio integrado ensino medio tecnico">
                                <input type="checkbox" name="ms-modalidade" value="integrado" onchange="aoMudarMultiSelect('modalidades')">
                                <span class="ms-item-text">📘 Médio Integrado</span>
                            </label>
                            <label class="ms-item" data-search="subsequente prof tecnico pos medio">
                                <input type="checkbox" name="ms-modalidade" value="subsequente" onchange="aoMudarMultiSelect('modalidades')">
                                <span class="ms-item-text">📙 Prof/Técnico (Subsequente)</span>
                            </label>
                            <label class="ms-item" data-search="integral tempo integral">
                                <input type="checkbox" name="ms-modalidade" value="integral_turno" onchange="aoMudarMultiSelect('modalidades')">
                                <span class="ms-item-text">⏱️ Regime em Tempo Integral</span>
                            </label>
                            <label class="ms-item" data-search="ambas integrado e subsequente">
                                <input type="checkbox" name="ms-modalidade" value="ambas_modalidades" onchange="aoMudarMultiSelect('modalidades')">
                                <span class="ms-item-text">📗 Escolas com Ambas (Integrado + Subseq.)</span>
                            </label>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 6. Turno (Multi-Select) -->
            <div class="filter-group">
                <label>🕒 Turnos de Oferta (Múltipla Escolha)</label>
                <div class="ms-dropdown" id="ms-dropdown-turnos">
                    <button type="button" class="ms-trigger" id="ms-trigger-turnos" onclick="alternarMenuMultiSelect('turnos', event)">
                        <span class="ms-trigger-text" id="ms-label-turnos">Todos os Turnos</span>
                        <span class="ms-count-badge" id="ms-badge-turnos">0</span>
                        <span>▾</span>
                    </button>
                    <div class="ms-menu" onclick="event.stopPropagation()">
                        <div class="ms-actions">
                            <button type="button" class="ms-action-btn" onclick="marcarTodosMultiSelect('turnos', true)">✅ Marcar todos</button>
                            <button type="button" class="ms-action-btn" onclick="marcarTodosMultiSelect('turnos', false)">🧹 Limpar</button>
                        </div>
                        <div class="ms-list" id="ms-list-turnos">
                            <label class="ms-item" data-search="manha">
                                <input type="checkbox" name="ms-turno" value="Manhã" onchange="aoMudarMultiSelect('turnos')">
                                <span class="ms-item-text">🌅 Manhã</span>
                            </label>
                            <label class="ms-item" data-search="tarde">
                                <input type="checkbox" name="ms-turno" value="Tarde" onchange="aoMudarMultiSelect('turnos')">
                                <span class="ms-item-text">☀️ Tarde</span>
                            </label>
                            <label class="ms-item" data-search="noite">
                                <input type="checkbox" name="ms-turno" value="Noite" onchange="aoMudarMultiSelect('turnos')">
                                <span class="ms-item-text">🌙 Noite</span>
                            </label>
                            <label class="ms-item" data-search="integral">
                                <input type="checkbox" name="ms-turno" value="Integral" onchange="aoMudarMultiSelect('turnos')">
                                <span class="ms-item-text">⏱️ Integral</span>
                            </label>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 7. Município (Multi-Select) -->
            <div class="filter-group">
                <label>🏙️ Municípios (Múltipla Escolha)</label>
                <div class="ms-dropdown" id="ms-dropdown-municipios">
                    <button type="button" class="ms-trigger" id="ms-trigger-municipios" onclick="alternarMenuMultiSelect('municipios', event)">
                        <span class="ms-trigger-text" id="ms-label-municipios">Todos os Municípios (34)</span>
                        <span class="ms-count-badge" id="ms-badge-municipios">0</span>
                        <span>▾</span>
                    </button>
                    <div class="ms-menu" onclick="event.stopPropagation()">
                        <input type="text" class="ms-search-box" placeholder="Filtrar cidades (ex: Curitiba, Londrina)..."
                               oninput="filtrarOpcoesMultiSelect('municipios', this.value)">
                        <div class="ms-actions">
                            <button type="button" class="ms-action-btn" onclick="marcarTodosMultiSelect('municipios', true)">✅ Marcar visíveis</button>
                            <button type="button" class="ms-action-btn" onclick="marcarTodosMultiSelect('municipios', false)">🧹 Limpar cidades</button>
                        </div>
                        <div class="ms-list" id="ms-list-municipios">
                            {opcoes_municipios_html}
                        </div>
                    </div>
                </div>
            </div>

            <!-- 8. Núcleo Regional de Educação (Multi-Select) -->
            <div class="filter-group">
                <label>🏛️ Núcleos Regionais - NRE (Múltipla Escolha)</label>
                <div class="ms-dropdown" id="ms-dropdown-nucleos">
                    <button type="button" class="ms-trigger" id="ms-trigger-nucleos" onclick="alternarMenuMultiSelect('nucleos', event)">
                        <span class="ms-trigger-text" id="ms-label-nucleos">Todos os NREs (25)</span>
                        <span class="ms-count-badge" id="ms-badge-nucleos">0</span>
                        <span>▾</span>
                    </button>
                    <div class="ms-menu" onclick="event.stopPropagation()">
                        <input type="text" class="ms-search-box" placeholder="Filtrar NRE..."
                               oninput="filtrarOpcoesMultiSelect('nucleos', this.value)">
                        <div class="ms-actions">
                            <button type="button" class="ms-action-btn" onclick="marcarTodosMultiSelect('nucleos', true)">✅ Marcar visíveis</button>
                            <button type="button" class="ms-action-btn" onclick="marcarTodosMultiSelect('nucleos', false)">🧹 Limpar NREs</button>
                        </div>
                        <div class="ms-list" id="ms-list-nucleos">
                            {opcoes_nucleos_html}
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Barra de Pílulas dos Filtros Ativos -->
        <div class="active-filters-bar" id="active-filters-bar"></div>

        <!-- Atalhos Rápidos de Cursos (Clique para Somar/Alternar Múltiplos Cursos) -->
        <div class="quick-chips">
            <span style="font-size:0.78rem; font-weight:700; color:#475569;">⚡ Atalhos Rápidos (clique em vários para combinar):</span>
            <button type="button" class="chip-btn active" onclick="selecionarChipCurso('')">Todos (147)</button>
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

        <!-- Painel de Taxa Percentual de Deslocamento por Densidade Regional e Preço Hora/Aluno -->
        <div class="pricing-rate-panel">
            <div class="pricing-rate-header">
                <div>
                    <strong style="font-size:0.9rem; color:#0f172a;">🚗 Taxa de Deslocamento por Densidade Regional &amp; Preço Hora/Aluno</strong>
                    <span style="font-size:0.78rem; color:#475569; margin-left:6px;">
                        Calculada somente sobre os <strong>cursos filtrados</strong> dentro do raio. Quanto menos cursos próximos, maior a taxa percentual.
                    </span>
                </div>
                <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                    <label style="font-size:0.78rem; font-weight:700; color:#1e40af; display:flex; align-items:center; gap:5px; cursor:pointer;">
                        <input type="checkbox" id="chk-mostrar-raio" checked onchange="atualizarCirculoRaioSeAtivo()">
                        ⭕ Desenhar círculo do raio no mapa ao clicar numa escola
                    </label>
                    <a href="/admin/credenciamento_parana/tabelaprecocurso/" target="_blank"
                       style="font-size:0.76rem; font-weight:700; color:#15803d; text-decoration:none; background:#dcfce7; padding:4px 10px; border-radius:6px; border:1px solid #86efac;">
                        ⚙️ Editar Preços (R$ 0,00) no Django Admin ↗
                    </a>
                </div>
            </div>
            <div class="pricing-rate-grid">
                <div class="filter-group">
                    <label for="cfg-taxa-max">📈 Taxa Máxima de Deslocamento (%)</label>
                    <input type="number" id="cfg-taxa-max" class="filter-input" value="20" min="0" max="300" step="0.5"
                           oninput="aplicarFiltros(true)">
                </div>
                <div class="filter-group">
                    <label for="cfg-raio-km">📏 Raio de Proximidade (km)</label>
                    <input type="number" id="cfg-raio-km" class="filter-input" value="200" min="1" max="1500" step="10"
                           oninput="aplicarFiltros(true); atualizarCirculoRaioSeAtivo();">
                </div>
                <div class="filter-group">
                    <label for="cfg-cursos-min">🔴 Qtd. Mín. Cursos no Raio (Atinge Taxa Máx.)</label>
                    <input type="number" id="cfg-cursos-min" class="filter-input" value="1" min="1" max="100" step="1"
                           oninput="aplicarFiltros(true)">
                </div>
                <div class="filter-group">
                    <label for="cfg-cursos-zero">🟢 Qtd. Máx. Cursos no Raio (Zera a Taxa = 0%)</label>
                    <input type="number" id="cfg-cursos-zero" class="filter-input" value="3" min="1" max="200" step="1"
                           oninput="aplicarFiltros(true)">
                </div>
            </div>
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
                        <th style="width:65px;">Item</th>
                        <th style="width:180px;">Município / Núcleo (NRE)</th>
                        <th style="width:240px;">Estabelecimento / Cód. MEC</th>
                        <th style="width:230px;">Endereço / Contato (Anexo IV)</th>
                        <th style="width:185px;">Proximidade &amp; Taxa Desloc.</th>
                        <th>Cursos Ofertados, Preço Hora/Aluno (Base &rarr; c/ Taxa), Turma/Ano e Lucro</th>
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
        let radiusCircleLayer = null;
        let escolaFocadaRaio = null;
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

        function formatarMoedaBRL(valor) {{
            const num = Number(valor || 0);
            return num.toLocaleString("pt-BR", {{ style: "currency", currency: "BRL" }});
        }}

        function distanciaHaversineKm(lat1, lon1, lat2, lon2) {{
            const R = 6371.0; // Raio médio da Terra em km
            const toRad = deg => (deg * Math.PI) / 180.0;
            const dLat = toRad(lat2 - lat1);
            const dLon = toRad(lon2 - lon1);
            const a =
                Math.sin(dLat / 2) * Math.sin(dLat / 2) +
                Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) *
                Math.sin(dLon / 2) * Math.sin(dLon / 2);
            const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
            return R * c;
        }}

        function obterConfigTaxaDeslocamento() {{
            const taxaMaxRaw = parseFloat(document.getElementById("cfg-taxa-max")?.value ?? "20");
            const raioKmRaw = parseFloat(document.getElementById("cfg-raio-km")?.value ?? "200");
            const cursosMinRaw = parseInt(document.getElementById("cfg-cursos-min")?.value ?? "1", 10);
            const cursosZeroRaw = parseInt(document.getElementById("cfg-cursos-zero")?.value ?? "3", 10);

            return {{
                taxaMaximaPct: Number.isFinite(taxaMaxRaw) && taxaMaxRaw >= 0 ? taxaMaxRaw : 20,
                raioKm: Number.isFinite(raioKmRaw) && raioKmRaw > 0 ? raioKmRaw : 200,
                cursosMinTaxaMax: Number.isFinite(cursosMinRaw) && cursosMinRaw >= 1 ? cursosMinRaw : 1,
                cursosMaxZeraTaxa: Number.isFinite(cursosZeroRaw) && cursosZeroRaw >= 1 ? cursosZeroRaw : 3
            }};
        }}

        function calcularTaxasDeslocamento(escolasVisiveis, filtros) {{
            const cfg = obterConfigTaxaDeslocamento();
            const temFiltroCurso = temFiltroCursoOuTurnoOuModalidade(filtros);

            // 1. Para cada escola visível, conta quantos cursos FILTRADOS ela possui
            for (const esc of escolasVisiveis) {{
                const cursosQuePassam = temFiltroCurso
                    ? esc.cursos.filter(c => cursoPassaFiltroIndividual(c, filtros))
                    : esc.cursos;
                // Se a escola entrou pelo filtro "ambas_modalidades", garante ao menos os cursos da escola
                esc._qtdCursosFiltradosLocal = cursosQuePassam.length > 0 ? cursosQuePassam.length : esc.cursos.length;
            }}

            // 2. Para cada escola visível, soma os cursos filtrados de todas as escolas visíveis dentro do raio (ex: 200km)
            for (const escA of escolasVisiveis) {{
                let totalCursosNoRaio = 0;
                let totalEscolasNoRaio = 0;

                for (const escB of escolasVisiveis) {{
                    const distKm = (escA.id === escB.id)
                        ? 0
                        : distanciaHaversineKm(escA.latitude, escA.longitude, escB.latitude, escB.longitude);
                    if (distKm <= cfg.raioKm) {{
                        totalCursosNoRaio += escB._qtdCursosFiltradosLocal;
                        totalEscolasNoRaio += 1;
                    }}
                }}

                let taxaPct = 0.0;
                if (cfg.taxaMaximaPct > 0) {{
                    if (totalCursosNoRaio <= cfg.cursosMinTaxaMax) {{
                        taxaPct = cfg.taxaMaximaPct;
                    }} else if (totalCursosNoRaio >= cfg.cursosMaxZeraTaxa || cfg.cursosMaxZeraTaxa <= cfg.cursosMinTaxaMax) {{
                        taxaPct = 0.0;
                    }} else {{
                        const proporcao = (cfg.cursosMaxZeraTaxa - totalCursosNoRaio) / (cfg.cursosMaxZeraTaxa - cfg.cursosMinTaxaMax);
                        taxaPct = cfg.taxaMaximaPct * proporcao;
                    }}
                }}

                escA._cursosNoRaio = totalCursosNoRaio;
                escA._escolasNoRaio = totalEscolasNoRaio;
                escA._taxaDeslocamentoPct = Math.round(taxaPct * 100) / 100;
                escA._fatorTaxa = 1.0 + (escA._taxaDeslocamentoPct / 100.0);
                escA._raioKmUsado = cfg.raioKm;
            }}
        }}

        function montarBadgeTaxaDeslocamento(escola) {{
            const taxa = Number(escola._taxaDeslocamentoPct || 0);
            const qtdCursos = Number(escola._cursosNoRaio || 0);
            const qtdEscolas = Number(escola._escolasNoRaio || 1);
            const raioKm = Number(escola._raioKmUsado || 200);
            const cfg = obterConfigTaxaDeslocamento();

            let cls = "rate-badge-zero";
            let icone = "🟢";
            if (taxa > 0 && taxa >= cfg.taxaMaximaPct - 0.01) {{
                cls = "rate-badge-max";
                icone = "🔴";
            }} else if (taxa > 0) {{
                cls = "rate-badge-mid";
                icone = "🟡";
            }}

            const taxaStr = taxa.toLocaleString("pt-BR", {{ minimumFractionDigits: 1, maximumFractionDigits: 2 }});
            return `
                <div style="display:flex; flex-direction:column; gap:4px;">
                    <span class="rate-badge ${{cls}}">
                        ${{icone}} Taxa Desloc.: +${{taxaStr}}%
                    </span>
                    <span style="font-size:0.74rem; color:#475569; font-weight:600;">
                        📍 <strong>${{qtdCursos}}</strong> curso(s) filtrado(s) em <strong>${{raioKm}} km</strong> (${{qtdEscolas}} esc.)
                    </span>
                </div>
            `;
        }}

        function desenharCirculoRaioEscola(escola) {{
            if (!map) return;
            escolaFocadaRaio = escola;
            if (radiusCircleLayer) {{
                map.removeLayer(radiusCircleLayer);
                radiusCircleLayer = null;
            }}
            const mostrar = document.getElementById("chk-mostrar-raio")?.checked;
            if (!mostrar || !escola) return;

            const cfg = obterConfigTaxaDeslocamento();
            const taxa = Number(escola._taxaDeslocamentoPct || 0);
            let corCirculo = "#16a34a";
            if (taxa > 0 && taxa >= cfg.taxaMaximaPct - 0.01) {{
                corCirculo = "#dc2626";
            }} else if (taxa > 0) {{
                corCirculo = "#d97706";
            }}

            radiusCircleLayer = L.circle([escola.latitude, escola.longitude], {{
                radius: cfg.raioKm * 1000,
                color: corCirculo,
                weight: 2,
                dashArray: "6, 6",
                fillColor: corCirculo,
                fillOpacity: 0.08,
                interactive: false
            }}).addTo(map);
        }}

        function atualizarCirculoRaioSeAtivo() {{
            if (escolaFocadaRaio) {{
                desenharCirculoRaioEscola(escolaFocadaRaio);
            }}
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

        const MS_CONFIG = {{
            cursos: {{ inputName: "ms-curso", defaultLabel: "Todos os Cursos (28)", icon: "🎓" }},
            modalidades: {{ inputName: "ms-modalidade", defaultLabel: "Todas as Modalidades", icon: "📘" }},
            turnos: {{ inputName: "ms-turno", defaultLabel: "Todos os Turnos", icon: "🕒" }},
            municipios: {{ inputName: "ms-municipio", defaultLabel: "Todos os Municípios (34)", icon: "🏙️" }},
            nucleos: {{ inputName: "ms-nucleo", defaultLabel: "Todos os NREs (25)", icon: "🏛️" }}
        }};

        const MODALIDADE_LABELS = {{
            "integrado": "Médio Integrado",
            "subsequente": "Prof/Técnico (Subsequente)",
            "integral_turno": "Regime Integral",
            "ambas_modalidades": "Ambas Modalidades"
        }};

        function obterValoresMarcados(groupKey) {{
            const cfg = MS_CONFIG[groupKey];
            if (!cfg) return [];
            const checkboxes = document.querySelectorAll(`input[name="${{cfg.inputName}}"]:checked`);
            return Array.from(checkboxes).map(cb => cb.value);
        }}

        function alternarMenuMultiSelect(groupKey, event) {{
            if (event) event.stopPropagation();
            const targetDropdown = document.getElementById(`ms-dropdown-${{groupKey}}`);
            const wasOpen = targetDropdown.classList.contains("open");
            document.querySelectorAll(".ms-dropdown.open").forEach(el => el.classList.remove("open"));
            if (!wasOpen) {{
                targetDropdown.classList.add("open");
                const searchInput = targetDropdown.querySelector(".ms-search-box");
                if (searchInput) {{
                    setTimeout(() => searchInput.focus(), 30);
                }}
            }}
        }}

        document.addEventListener("click", () => {{
            document.querySelectorAll(".ms-dropdown.open").forEach(el => el.classList.remove("open"));
        }});

        function filtrarOpcoesMultiSelect(groupKey, termo) {{
            const q = normalizarTexto(termo);
            const listEl = document.getElementById(`ms-list-${{groupKey}}`);
            if (!listEl) return;
            listEl.querySelectorAll(".ms-item").forEach(labelEl => {{
                const alvo = normalizarTexto(labelEl.getAttribute("data-search") || labelEl.textContent);
                labelEl.style.display = (!q || alvo.includes(q)) ? "flex" : "none";
            }});
        }}

        function marcarTodosMultiSelect(groupKey, marcar) {{
            const listEl = document.getElementById(`ms-list-${{groupKey}}`);
            if (!listEl) return;
            listEl.querySelectorAll(".ms-item").forEach(labelEl => {{
                if (marcar && labelEl.style.display === "none") return;
                const cb = labelEl.querySelector('input[type="checkbox"]');
                if (cb) cb.checked = marcar;
            }});
            aoMudarMultiSelect(groupKey);
        }}

        function atualizarRotuloMultiSelect(groupKey) {{
            const cfg = MS_CONFIG[groupKey];
            const selecionados = obterValoresMarcados(groupKey);
            const trigger = document.getElementById(`ms-trigger-${{groupKey}}`);
            const labelEl = document.getElementById(`ms-label-${{groupKey}}`);
            const badgeEl = document.getElementById(`ms-badge-${{groupKey}}`);

            if (selecionados.length === 0) {{
                trigger.classList.remove("has-selection");
                labelEl.textContent = cfg.defaultLabel;
                badgeEl.textContent = "0";
            }} else {{
                trigger.classList.add("has-selection");
                badgeEl.textContent = String(selecionados.length);
                if (selecionados.length === 1) {{
                    const val = selecionados[0];
                    labelEl.textContent = groupKey === "modalidades" ? (MODALIDADE_LABELS[val] || val) : val;
                }} else {{
                    const nomes = selecionados.map(v => groupKey === "modalidades" ? (MODALIDADE_LABELS[v] || v) : v);
                    labelEl.textContent = `${{nomes.slice(0, 2).join(", ")}}${{nomes.length > 2 ? " +" + (nomes.length - 2) : ""}}`;
                }}
            }}
        }}

        function aoMudarMultiSelect(groupKey) {{
            atualizarRotuloMultiSelect(groupKey);
            aplicarFiltros();
        }}

        function removerItemMultiSelect(groupKey, valor) {{
            const cfg = MS_CONFIG[groupKey];
            if (!cfg) return;
            const checkboxes = document.querySelectorAll(`input[name="${{cfg.inputName}}"]`);
            checkboxes.forEach(cb => {{
                if (cb.value === valor) cb.checked = false;
            }});
            aoMudarMultiSelect(groupKey);
        }}

        function temFiltroCursoOuTurnoOuModalidade(filtros) {{
            const modPorCurso = filtros.modalidades.filter(m => m !== "ambas_modalidades");
            return Boolean(
                filtros.cursos.length > 0 ||
                filtros.turnos.length > 0 ||
                modPorCurso.length > 0
            );
        }}

        function cursoPassaFiltroIndividual(curso, filtros) {{
            if (filtros.cursos.length > 0 && !filtros.cursos.includes(curso.nome_curso)) {{
                return false;
            }}
            if (filtros.turnos.length > 0 && !filtros.turnos.includes(curso.turno)) {{
                return false;
            }}
            const modPorCurso = filtros.modalidades.filter(m => m !== "ambas_modalidades");
            if (modPorCurso.length > 0) {{
                let bateAlgumaModalidade = false;
                if (modPorCurso.includes("integrado") && curso.tipo_modalidade === "integrado") {{
                    bateAlgumaModalidade = true;
                }}
                if (modPorCurso.includes("subsequente") && curso.tipo_modalidade === "subsequente") {{
                    bateAlgumaModalidade = true;
                }}
                if (modPorCurso.includes("integral_turno") && curso.turno === "Integral") {{
                    bateAlgumaModalidade = true;
                }}
                if (!bateAlgumaModalidade) {{
                    return false;
                }}
            }}
            if (filtros.buscaGeral) {{
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
            const destacarCursos = filtrosAtivos ? temFiltroCursoOuTurnoOuModalidade(filtrosAtivos) : false;
            const fatorTaxa = Number(escola._fatorTaxa || 1.0);
            const taxaPct = Number(escola._taxaDeslocamentoPct || 0.0);

            for (const c of escola.cursos) {{
                const isMatch = destacarCursos ? cursoPassaFiltroIndividual(c, filtrosAtivos) : false;
                const badgeMod = c.tipo_modalidade === "subsequente"
                    ? `<span class="badge badge-subsequente">Subsequente</span>`
                    : `<span class="badge badge-integrado">Médio Integrado</span>`;
                const badgeTurno = `<span class="badge ${{classeBadgeTurno(c.turno)}}">${{escaparHtml(c.turno)}}</span>`;

                const vhBase = Number(c.valor_hora_aluno || 0);
                let blocoPrecoHtml = "";
                if (vhBase > 0) {{
                    const vhFinal = vhBase * fatorTaxa;
                    const vTurmaAnoFinal = Number(c.valor_turma_ano || 0) * fatorTaxa * Number(c.turmas || 1);
                    const vLucroMedioFinal = Number(c.lucro_medio_5anos || 0) * fatorTaxa * Number(c.turmas || 1);
                    blocoPrecoHtml = `
                        <div style="margin-top:5px; padding:5px 7px; background:#f0fdf4; border:1px solid #bbf7d0; border-radius:6px; font-size:0.74rem; color:#14532d;">
                            <div>💰 <strong>H/Aluno:</strong> ${{formatarMoedaBRL(vhBase)}} &rarr; <strong style="color:#15803d;">${{formatarMoedaBRL(vhFinal)}}</strong> ${{taxaPct > 0 ? `<span style="color:#b45309;">(+${{taxaPct.toLocaleString("pt-BR")}}%)</span>` : `(s/ taxa)`}}</div>
                            <div style="margin-top:2px;">📊 <strong>Turma/Ano (${{c.turmas}}t):</strong> ${{formatarMoedaBRL(vTurmaAnoFinal)}} | <strong>Lucro Méd.:</strong> ${{formatarMoedaBRL(vLucroMedioFinal)}}</div>
                        </div>
                    `;
                }} else {{
                    blocoPrecoHtml = `
                        <div style="margin-top:5px; font-size:0.72rem; color:#64748b;">
                            💰 <strong>H/Aluno:</strong> R$ 0,00 <a href="/admin/credenciamento_parana/tabelaprecocurso/" target="_blank" style="color:#2563eb; text-decoration:none; font-weight:600;">(definir no Admin ↗)</a>
                        </div>
                    `;
                }}

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
                        ${{blocoPrecoHtml}}
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
                        <div style="margin-top:6px;">
                            ${{montarBadgeTaxaDeslocamento(escola)}}
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
            const osmLayer = L.tileLayer("/credenciamento/tiles/osm/{{z}}/{{x}}/{{y}}.png", {{
                maxZoom: 19,
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
            }});
            const osmDeLayer = L.tileLayer("/credenciamento/tiles/osm_de/{{z}}/{{x}}/{{y}}.png", {{
                maxZoom: 19,
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> DE'
            }});
            const esriStreetLayer = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}", {{
                maxZoom: 19,
                attribution: 'Tiles &copy; Esri — OpenStreetMap, HERE, Garmin'
            }});
            const esriSat = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}", {{
                maxZoom: 19,
                attribution: 'Tiles &copy; Esri'
            }});

            map = L.map("leaflet-map", {{
                center: [-24.65, -51.55],
                zoom: 7,
                layers: [osmLayer]
            }});

            L.control.layers({{
                "🛣️ OpenStreetMap Oficial (Cache Local Anti-Bloqueio)": osmLayer,
                "🗺️ OpenStreetMap Alternativo (OSM DE)": osmDeLayer,
                "🏙️ Mapa de Ruas Nítido (Esri Street Map)": esriStreetLayer,
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
                cursos: obterValoresMarcados("cursos"),
                modalidades: obterValoresMarcados("modalidades"),
                turnos: obterValoresMarcados("turnos"),
                municipios: obterValoresMarcados("municipios"),
                nucleos: obterValoresMarcados("nucleos"),
                apenasFavoritos: filtroApenasFavoritos
            }};
        }}

        function renderizarPilulasFiltrosAtivos(filtros) {{
            const bar = document.getElementById("active-filters-bar");
            if (!bar) return;
            const pills = [];

            for (const c of filtros.cursos) {{
                pills.push(`<span class="active-pill">🎓 ${{escaparHtml(c)}} <button type="button" title="Remover" onclick="removerItemMultiSelect('cursos', '${{escaparHtml(c)}}')">✖</button></span>`);
            }}
            for (const m of filtros.modalidades) {{
                const lbl = MODALIDADE_LABELS[m] || m;
                pills.push(`<span class="active-pill">📘 ${{escaparHtml(lbl)}} <button type="button" title="Remover" onclick="removerItemMultiSelect('modalidades', '${{escaparHtml(m)}}')">✖</button></span>`);
            }}
            for (const t of filtros.turnos) {{
                pills.push(`<span class="active-pill">🕒 ${{escaparHtml(t)}} <button type="button" title="Remover" onclick="removerItemMultiSelect('turnos', '${{escaparHtml(t)}}')">✖</button></span>`);
            }}
            for (const mun of filtros.municipios) {{
                pills.push(`<span class="active-pill">🏙️ ${{escaparHtml(mun)}} <button type="button" title="Remover" onclick="removerItemMultiSelect('municipios', '${{escaparHtml(mun)}}')">✖</button></span>`);
            }}
            for (const nuc of filtros.nucleos) {{
                pills.push(`<span class="active-pill">🏛️ ${{escaparHtml(nuc)}} <button type="button" title="Remover" onclick="removerItemMultiSelect('nucleos', '${{escaparHtml(nuc)}}')">✖</button></span>`);
            }}

            if (pills.length === 0) {{
                bar.classList.remove("visible");
                bar.innerHTML = "";
            }} else {{
                bar.classList.add("visible");
                bar.innerHTML = `<span style="font-size:0.78rem; font-weight:800; color:#1e40af; margin-right:4px;">Filtros Selecionados (${{pills.length}}):</span>` + pills.join("");
            }}
        }}

        function escolaPassaNosFiltros(escola, filtros) {{
            if (filtros.apenasFavoritos && !escola.favorito) {{
                return false;
            }}
            if (filtros.municipios.length > 0 && !filtros.municipios.includes(escola.municipio)) {{
                return false;
            }}
            if (filtros.nucleos.length > 0 && !filtros.nucleos.includes(escola.nucleo_regional)) {{
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

            // Se marcou exclusivamente "ambas_modalidades", exige que a escola tenha Integrado E Subsequente
            const modPorCurso = filtros.modalidades.filter(m => m !== "ambas_modalidades");
            if (filtros.modalidades.includes("ambas_modalidades") && modPorCurso.length === 0) {{
                if (!(escola.tem_integrado && escola.tem_subsequente)) {{
                    return false;
                }}
            }}

            // Verifica se pelo menos 1 oferta de curso da escola satisfaz os filtros de curso/modalidade/turno combinados
            if (temFiltroCursoOuTurnoOuModalidade(filtros)) {{
                const algumCursoBate = escola.cursos.some(c => cursoPassaFiltroIndividual(c, filtros));
                const bateAmbas = filtros.modalidades.includes("ambas_modalidades") && escola.tem_integrado && escola.tem_subsequente;
                if (!algumCursoBate && !bateAmbas) {{
                    return false;
                }}
            }}

            if (filtros.buscaGeral) {{
                const nomesCursos = escola.cursos.map(c => `${{c.nome_curso}} ${{c.modalidade}} ${{c.turno}}`).join(" ");
                const textoCompleto = normalizarTexto(
                    `${{escola.item_edital}} ${{escola.nome_escola}} ${{escola.codigo_mec}} ${{escola.municipio}} ${{escola.nucleo_regional}} ${{escola.endereco}} ${{escola.cep}} ${{escola.responsavel}} ${{escola.email}} ${{nomesCursos}}`
                );
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
            calcularTaxasDeslocamento(escolasFiltradasAtuais, filtros);

            atualizarMarcadoresNoMapa(escolasFiltradasAtuais, filtros, isInitialLoad);
            atualizarTabelaAbaixoDoMapa(escolasFiltradasAtuais, filtros);
            atualizarKPIsEFavoritos(escolasFiltradasAtuais, filtros);
            atualizarChipsAtivos(filtros.cursos);
            renderizarPilulasFiltrosAtivos(filtros);
            atualizarCirculoRaioSeAtivo();
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
                marker.bindPopup(() => montarHtmlPopup(esc, filtros), {{ maxWidth: 410 }});
                marker.on("click", () => desenharCirculoRaioEscola(esc));
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
            if (radiusCircleLayer) {{
                map.removeLayer(radiusCircleLayer);
                radiusCircleLayer = null;
                escolaFocadaRaio = null;
            }}
            map.flyTo([-24.65, -51.55], 7, {{ duration: 0.7 }});
        }}

        function atualizarKPIsEFavoritos(escolasVisiveis, filtros) {{
            const totalEscolas = ESCOLAS_DATA.length;
            const cidadesVisiveis = new Set(escolasVisiveis.map(e => e.municipio)).size;

            let ofertasVisiveis = 0;
            let turmasVisiveis = 0;
            let chTotalVisivel = 0;
            let receitaAnualTotalComTaxa = 0.0;
            let lucroMedioAnualComTaxa = 0.0;

            const temFiltroCurso = temFiltroCursoOuTurnoOuModalidade(filtros);

            for (const esc of escolasVisiveis) {{
                const fator = Number(esc._fatorTaxa || 1.0);
                for (const c of esc.cursos) {{
                    if (!temFiltroCurso || cursoPassaFiltroIndividual(c, filtros)) {{
                        ofertasVisiveis += 1;
                        turmasVisiveis += c.turmas;
                        chTotalVisivel += c.ch_total_oferta;
                        receitaAnualTotalComTaxa += Number(c.valor_turma_ano || 0) * fator * Number(c.turmas || 1);
                        lucroMedioAnualComTaxa += Number(c.lucro_medio_5anos || 0) * fator * Number(c.turmas || 1);
                    }}
                }}
            }}

            const favoritas = ESCOLAS_DATA.filter(e => e.favorito);

            document.getElementById("kpi-escolas").textContent = `${{escolasVisiveis.length}} / ${{totalEscolas}}`;
            document.getElementById("kpi-cidades-sub").textContent = `Em ${{cidadesVisiveis}} município(s) do Paraná`;
            document.getElementById("kpi-cursos").textContent = `${{ofertasVisiveis}}`;
            document.getElementById("kpi-turmas").textContent = `${{turmasVisiveis}} turma(s)`;
            document.getElementById("kpi-ch-sub").textContent = `${{chTotalVisivel.toLocaleString("pt-BR")}} horas anuais (total)`;
            document.getElementById("kpi-receita-anual").textContent = formatarMoedaBRL(receitaAnualTotalComTaxa);
            document.getElementById("kpi-lucro-sub").textContent = `Lucro Médio 5 anos: ${{formatarMoedaBRL(lucroMedioAnualComTaxa)}}`;
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
                const taxaPct = Number(esc._taxaDeslocamentoPct || 0);
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
                        <div style="font-size:0.73rem; color:#15803d; margin-top:4px; font-weight:700;">
                            🚗 Taxa na filtragem: +${{taxaPct.toLocaleString("pt-BR")}}% (${{esc._cursosNoRaio || 0}} curso(s) no raio)
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
                        <td colspan="7" style="text-align:center; padding:36px; color:#64748b; font-size:0.95rem;">
                            🔍 Nenhuma escola encontrada para a combinação atual de filtros. Clique em <strong>🧹 Limpar Filtros</strong> para restaurar todas as 147 escolas.
                        </td>
                    </tr>
                `;
                return;
            }}

            const temFiltroCurso = temFiltroCursoOuTurnoOuModalidade(filtros);

            let rowsHtml = "";
            for (const esc of escolasVisiveis) {{
                const fatorTaxa = Number(esc._fatorTaxa || 1.0);
                const taxaPct = Number(esc._taxaDeslocamentoPct || 0.0);
                let cursosRowsHtml = "";

                for (const c of esc.cursos) {{
                    const bateFiltro = temFiltroCurso && cursoPassaFiltroIndividual(c, filtros);
                    const badgeMod = c.tipo_modalidade === "subsequente"
                        ? `<span class="badge badge-subsequente">Subsequente</span>`
                        : `<span class="badge badge-integrado">Médio Integrado</span>`;
                    const badgeTurno = `<span class="badge ${{classeBadgeTurno(c.turno)}}">${{escaparHtml(c.turno)}}</span>`;

                    const vhBase = Number(c.valor_hora_aluno || 0);
                    let celulaPrecoHtml = "";
                    if (vhBase > 0) {{
                        const vhFinal = vhBase * fatorTaxa;
                        const vhTurmaFinal = Number(c.valor_hora_turma || 0) * fatorTaxa;
                        const vTurmaAnoFinal = Number(c.valor_turma_ano || 0) * fatorTaxa * Number(c.turmas || 1);
                        const vLucroMedioFinal = Number(c.lucro_medio_5anos || 0) * fatorTaxa * Number(c.turmas || 1);

                        celulaPrecoHtml = `
                            <div class="price-tag-box">
                                <span>H/Aluno Base: <strong>${{formatarMoedaBRL(vhBase)}}</strong></span>
                                <span>&rarr;</span>
                                <span style="color:#15803d;">c/ Taxa (+${{taxaPct.toLocaleString("pt-BR")}}%): <strong>${{formatarMoedaBRL(vhFinal)}}</strong></span>
                            </div>
                            <div style="font-size:0.74rem; color:#334155; margin-top:4px;">
                                H/Turma: <strong>${{formatarMoedaBRL(vhTurmaFinal)}}</strong> •
                                Turma/Ano (${{c.turmas}}t): <strong>${{formatarMoedaBRL(vTurmaAnoFinal)}}</strong> •
                                Lucro Méd. (5a): <strong style="color:#15803d;">${{formatarMoedaBRL(vLucroMedioFinal)}}</strong>
                            </div>
                        `;
                    }} else {{
                        celulaPrecoHtml = `
                            <span class="price-tag-zero">
                                💰 H/Aluno: R$ 0,00
                                <a href="/admin/credenciamento_parana/tabelaprecocurso/" target="_blank" style="color:#2563eb; text-decoration:none; margin-left:4px;">(editar no Admin ↗)</a>
                            </span>
                        `;
                    }}

                    cursosRowsHtml += `
                        <tr class="${{bateFiltro ? 'course-highlight' : ''}}">
                            <td style="width:135px;">${{badgeMod}}</td>
                            <td style="font-weight:700; color:#0f172a; width:195px;">${{escaparHtml(c.nome_curso)}}</td>
                            <td style="width:85px;">${{badgeTurno}}</td>
                            <td style="width:75px; text-align:center;">
                                <strong>${{c.turmas}}</strong> turma(s)
                                <div style="font-size:0.7rem; color:#64748b;">${{c.ch_anual_curso}}h</div>
                            </td>
                            <td>
                                ${{celulaPrecoHtml}}
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
                            ${{montarBadgeTaxaDeslocamento(esc)}}
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
            calcularTaxasDeslocamento(escolasFiltradasAtuais, filtros);

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

            desenharCirculoRaioEscola(escola);

            let marker = markersByItem.get(itemEdital);
            if (!marker) {{
                // Se estava oculta pelo filtro atual, cria/abre temporariamente
                const filtros = obterFiltrosAtivos();
                marker = L.marker([escola.latitude, escola.longitude], {{
                    icon: iconeMarcadorEscola(escola),
                    zIndexOffset: 1000
                }}).addTo(markersLayer);
                marker.bindPopup(() => montarHtmlPopup(escola, filtros), {{ maxWidth: 410 }});
                markersByItem.set(itemEdital, marker);
            }}

            map.flyTo([escola.latitude, escola.longitude], 12, {{ duration: 0.75 }});
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
            const checkboxes = document.querySelectorAll('input[name="ms-curso"]');
            if (!nomeCurso) {{
                checkboxes.forEach(cb => cb.checked = false);
            }} else {{
                checkboxes.forEach(cb => {{
                    if (cb.value === nomeCurso) {{
                        cb.checked = !cb.checked;
                    }}
                }});
            }}
            aoMudarMultiSelect("cursos");
        }}

        function atualizarChipsAtivos(cursosSelecionados) {{
            const chips = document.querySelectorAll(".quick-chips .chip-btn");
            chips.forEach(btn => {{
                const onclickAttr = btn.getAttribute("onclick") || "";
                if (cursosSelecionados.length === 0 && onclickAttr.includes("''")) {{
                    btn.classList.add("active");
                }} else {{
                    let ativo = false;
                    for (const c of cursosSelecionados) {{
                        if (onclickAttr.includes(`'${{c}}'`)) {{
                            ativo = true;
                            break;
                        }}
                    }}
                    if (ativo) btn.classList.add("active");
                    else btn.classList.remove("active");
                }}
            }});
        }}

        function limparTodosFiltros() {{
            document.getElementById("f-busca-geral").value = "";
            document.getElementById("f-escola").value = "";
            document.getElementById("f-endereco").value = "";
            Object.keys(MS_CONFIG).forEach(groupKey => {{
                const cfg = MS_CONFIG[groupKey];
                document.querySelectorAll(`input[name="${{cfg.inputName}}"]`).forEach(cb => cb.checked = false);
                const listEl = document.getElementById(`ms-list-${{groupKey}}`);
                if (listEl) listEl.querySelectorAll(".ms-item").forEach(el => el.style.display = "flex");
                const dropEl = document.getElementById(`ms-dropdown-${{groupKey}}`);
                if (dropEl) {{
                    const s = dropEl.querySelector(".ms-search-box");
                    if (s) s.value = "";
                }}
                atualizarRotuloMultiSelect(groupKey);
            }});
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
                "CH Anual Coord", "CH Anual Sup Tecnico",
                "Cursos Filtrados no Raio", "Raio (km)", "Taxa Deslocamento (%)",
                "H/Aluno Base (R$)", "H/Aluno c/ Taxa (R$)",
                "H/Turma c/ Taxa (R$)", "Turma/Ano Total c/ Taxa (R$)", "Lucro Medio 5a c/ Taxa (R$)",
                "Latitude", "Longitude"
            ];
            const linhas = [cabecalho.join(";")];
            const filtros = obterFiltrosAtivos();
            const temFiltroCurso = temFiltroCursoOuTurnoOuModalidade(filtros);

            for (const esc of escolasFiltradasAtuais) {{
                const fator = Number(esc._fatorTaxa || 1.0);
                const taxaPct = Number(esc._taxaDeslocamentoPct || 0.0);
                for (const c of esc.cursos) {{
                    if (temFiltroCurso && !cursoPassaFiltroIndividual(c, filtros)) continue;
                    const vhBase = Number(c.valor_hora_aluno || 0);
                    const vhComTaxa = (vhBase * fator).toFixed(2).replace(".", ",");
                    const vhTurmaComTaxa = (Number(c.valor_hora_turma || 0) * fator).toFixed(2).replace(".", ",");
                    const vTurmaAnoComTaxa = (Number(c.valor_turma_ano || 0) * fator * Number(c.turmas || 1)).toFixed(2).replace(".", ",");
                    const vLucroMedioComTaxa = (Number(c.lucro_medio_5anos || 0) * fator * Number(c.turmas || 1)).toFixed(2).replace(".", ",");

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
                        esc._cursosNoRaio || 0,
                        esc._raioKmUsado || 200,
                        taxaPct.toFixed(2).replace(".", ","),
                        vhBase.toFixed(2).replace(".", ","),
                        vhComTaxa,
                        vhTurmaComTaxa,
                        vTurmaAnoComTaxa,
                        vLucroMedioComTaxa,
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
    resp = HttpResponse(pagina_html)
    resp["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp

