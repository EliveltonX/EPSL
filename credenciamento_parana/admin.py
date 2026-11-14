from django.contrib import admin

from credenciamento_parana.models import EscolaCredenciamento, OfertaCursoEscola


class OfertaCursoEscolaInline(admin.TabularInline):
    model = OfertaCursoEscola
    extra = 0


@admin.register(EscolaCredenciamento)
class EscolaCredenciamentoAdmin(admin.ModelAdmin):
    list_display = (
        "item_edital",
        "favorito",
        "municipio",
        "nucleo_regional",
        "nome_escola",
        "codigo_mec",
        "endereco",
        "telefone",
    )
    list_filter = ("favorito", "nucleo_regional", "municipio")
    search_fields = (
        "nome_escola",
        "municipio",
        "nucleo_regional",
        "endereco",
        "codigo_mec",
        "responsavel",
        "ofertas_cursos__nome_curso",
    )
    inlines = [OfertaCursoEscolaInline]


@admin.register(OfertaCursoEscola)
class OfertaCursoEscolaAdmin(admin.ModelAdmin):
    list_display = (
        "escola",
        "nome_curso",
        "modalidade",
        "tipo_modalidade",
        "turno",
        "turmas",
        "ch_anual_curso",
        "ch_anual_coord",
        "ch_anual_sup_tecnico",
    )
    list_filter = ("tipo_modalidade", "turno", "nome_curso", "escola__municipio")
    search_fields = ("nome_curso", "escola__nome_escola", "escola__municipio", "escola__endereco")

