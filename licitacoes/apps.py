from django.apps import AppConfig


class LicitacoesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "licitacoes"
    verbose_name = "Licitações (Diários Oficiais)"

    def ready(self) -> None:
        from licitacoes.scheduler_worker import iniciar_agendador_se_habilitado

        iniciar_agendador_se_habilitado()
