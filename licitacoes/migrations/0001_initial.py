# Generated initial migration for Licitacao model

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Licitacao",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "cidade",
                    models.CharField(
                        db_index=True,
                        help_text="Município de origem do órgão emissor (indexado para buscas rápidas).",
                        max_length=150,
                        verbose_name="Cidade",
                    ),
                ),
                (
                    "orgao_emissor",
                    models.CharField(
                        help_text="Entidade ou órgão público responsável pela publicação (ex: Prefeitura, Secretaria).",
                        max_length=255,
                        verbose_name="Órgão Emissor",
                    ),
                ),
                (
                    "data_publicacao",
                    models.DateField(
                        db_index=True,
                        help_text="Data de publicação do Diário Oficial.",
                        verbose_name="Data de Publicação",
                    ),
                ),
                (
                    "modalidade",
                    models.CharField(
                        help_text="Modalidade da licitação (ex: Pregão Eletrônico, Tomada de Preços, Dispensa).",
                        max_length=100,
                        verbose_name="Modalidade",
                    ),
                ),
                (
                    "objeto_licitacao",
                    models.TextField(
                        help_text="Escopo detalhado da contratação extraído e estruturado pela IA.",
                        verbose_name="Objeto da Licitação",
                    ),
                ),
                (
                    "paginas_referencia",
                    models.CharField(
                        help_text='Números das páginas do PDF onde o aviso/edital foi encontrado (ex: "14, 15").',
                        max_length=100,
                        verbose_name="Páginas de Referência",
                    ),
                ),
                (
                    "link_documento",
                    models.URLField(
                        help_text="URL no Supabase Storage apontando para o PDF em diarios_processados/{YYYY}/{MM}/.",
                        max_length=500,
                        verbose_name="Link do Documento",
                    ),
                ),
            ],
            options={
                "verbose_name": "Licitação",
                "verbose_name_plural": "Licitações",
                "db_table": "licitacoes",
                "ordering": ["-data_publicacao", "cidade"],
                "indexes": [
                    models.Index(
                        fields=["cidade", "data_publicacao"],
                        name="idx_licitacao_cidade_data",
                    ),
                ],
            },
        ),
    ]

