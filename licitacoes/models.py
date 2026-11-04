from django.db import models


class Licitacao(models.Model):
    """
    Model principal do sistema EPSL para armazenamento de licitações extraídas
    de Diários Oficiais via PyMuPDF + Regex + Google Gemini.

    Decisão de Arquitetura:
    - PostgreSQL (Supabase) estritamente relacional, SEM campos JSON.
    """

    class TagAnalise(models.TextChoices):
        SEM_MARCACAO = "", "Sem Marcação"
        FAVORITO = "favorito", "Favorito"
        EM_ANALISE = "em_analise", "Em Análise"
        SEM_CONFORMIDADE = "sem_conformidade", "Sem Conformidade"
        IGNORADO = "ignorado", "Ignorado"

    cidade = models.CharField(
        max_length=150,
        db_index=True,
        verbose_name="Cidade",
        help_text="Município de origem do órgão emissor (indexado para buscas rápidas).",
    )
    orgao_emissor = models.CharField(
        max_length=255,
        verbose_name="Órgão Emissor",
        help_text="Entidade ou órgão público responsável pela publicação (ex: Prefeitura, Secretaria).",
    )
    data_publicacao = models.DateField(
        db_index=True,
        verbose_name="Data de Publicação",
        help_text="Data de publicação do Diário Oficial.",
    )
    modalidade = models.CharField(
        max_length=100,
        verbose_name="Modalidade",
        help_text="Modalidade da licitação (ex: Pregão Eletrônico, Tomada de Preços, Dispensa).",
    )
    objeto_licitacao = models.TextField(
        verbose_name="Objeto da Licitação",
        help_text="Escopo detalhado da contratação extraído e estruturado pela IA.",
    )
    paginas_referencia = models.CharField(
        max_length=100,
        verbose_name="Páginas de Referência",
        help_text='Números das páginas do PDF onde o aviso/edital foi encontrado (ex: "14, 15").',
    )
    link_documento = models.URLField(
        max_length=500,
        verbose_name="Link do Documento",
        help_text="URL no Supabase Storage apontando para o PDF em diarios_processados/{YYYY}/{MM}/.",
    )
    tag_analise = models.CharField(
        max_length=30,
        choices=TagAnalise.choices,
        default=TagAnalise.SEM_MARCACAO,
        blank=True,
        db_index=True,
        verbose_name="Tag de Análise",
        help_text="Marcação de situação pós-análise (Favorito, Em Análise, Sem Conformidade, Ignorado).",
    )

    class Meta:
        db_table = "licitacoes"
        verbose_name = "Licitação"
        verbose_name_plural = "Licitações"
        ordering = ["-data_publicacao", "cidade"]
        indexes = [
            models.Index(fields=["cidade", "data_publicacao"], name="idx_licitacao_cidade_data"),
            models.Index(fields=["tag_analise", "data_publicacao"], name="idx_licitacao_tag_data"),
        ]

    def __str__(self) -> str:
        return f"[{self.cidade}] {self.modalidade} - {self.orgao_emissor} ({self.data_publicacao})"
