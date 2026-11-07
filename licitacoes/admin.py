from django.contrib import admin
from licitacoes.models import DiarioProcessado, Licitacao


@admin.register(Licitacao)
class LicitacaoAdmin(admin.ModelAdmin):
    list_display = (
        "tag_analise",
        "cidade",
        "orgao_emissor",
        "modalidade",
        "data_publicacao",
        "paginas_referencia",
    )
    list_display_links = ("cidade", "orgao_emissor")
    list_editable = ("tag_analise",)
    list_filter = ("tag_analise", "cidade", "modalidade", "data_publicacao")
    search_fields = ("cidade", "orgao_emissor", "objeto_licitacao", "anotacoes")
    date_hierarchy = "data_publicacao"


@admin.register(DiarioProcessado)
class DiarioProcessadoAdmin(admin.ModelAdmin):
    list_display = (
        "nome_arquivo",
        "sha256_arquivo",
        "total_paginas",
        "qtd_licitacoes_extraidas",
        "tamanho_bytes",
        "processado_em",
    )
    search_fields = ("nome_arquivo", "sha256_arquivo", "sha256_texto")
    readonly_fields = ("sha256_arquivo", "sha256_texto", "processado_em")
