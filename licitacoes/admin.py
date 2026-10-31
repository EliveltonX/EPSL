from django.contrib import admin
from licitacoes.models import Licitacao


@admin.register(Licitacao)
class LicitacaoAdmin(admin.ModelAdmin):
    list_display = (
        "cidade",
        "orgao_emissor",
        "modalidade",
        "data_publicacao",
        "paginas_referencia",
    )
    list_filter = ("cidade", "modalidade", "data_publicacao")
    search_fields = ("cidade", "orgao_emissor", "objeto_licitacao")
    date_hierarchy = "data_publicacao"

