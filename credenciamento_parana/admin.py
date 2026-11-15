from django.contrib import admin

from credenciamento_parana.models import (
    EscolaCredenciamento,
    OfertaCursoEscola,
    TabelaPrecoCurso,
)


@admin.register(TabelaPrecoCurso)
class TabelaPrecoCursoAdmin(admin.ModelAdmin):
    list_display = (
        "nome_curso",
        "tipo_modalidade",
        "ch_anual_referencia",
        "alunos_por_turma",
        "valor_hora_aluno",
        "valor_hora_turma",
        "valor_turma_ano",
        "lucro_ano_1a3",
        "lucro_ano_4a5",
        "lucro_medio_5anos",
        "atualizado_em",
    )
    list_editable = (
        "valor_hora_aluno",
        "valor_hora_turma",
        "valor_turma_ano",
        "lucro_ano_1a3",
        "lucro_ano_4a5",
        "lucro_medio_5anos",
    )
    list_filter = ("tipo_modalidade", "ch_anual_referencia")
    search_fields = ("nome_curso",)
    ordering = ("nome_curso", "tipo_modalidade")



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

