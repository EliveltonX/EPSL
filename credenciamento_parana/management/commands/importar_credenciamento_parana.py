from django.core.management.base import BaseCommand

from credenciamento_parana.dados_anexo1 import sincronizar_escolas_credenciamento


class Command(BaseCommand):
    help = (
        "Importa/sincroniza as 147 escolas e 214 ofertas de cursos do Anexo I e Anexo IV "
        "do Termo de Referência Inicial de Credenciamento de Cursos (SEED/PR)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Força a recriação das ofertas de cursos e atualização de todos os dados das 147 escolas.",
        )

    def handle(self, *args, **options):
        resultado = sincronizar_escolas_credenciamento(
            forcar_atualizacao=bool(options.get("force"))
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Sincronização concluída! Escolas: {resultado['escolas_sincronizadas']} | "
                f"Ofertas de Cursos: {resultado['cursos_sincronizados']}"
            )
        )

