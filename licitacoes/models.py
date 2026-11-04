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


class DiarioProcessado(models.Model):
    """
    Registro de impressão digital (Deduplicação Criptográfica e Textual) de Diários Oficiais já analisados.
    Impede que o mesmo Diário Oficial (mesmo renomeado por outro funcionário ou re-baixado)
    seja enviado novamente para análise no Google Gemini, reduzindo custos de IA.
    """

    nome_arquivo = models.CharField(
        max_length=255,
        verbose_name="Nome do Arquivo Original",
    )
    sha256_arquivo = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        verbose_name="SHA-256 Binário do Arquivo",
        help_text="Hash SHA-256 exato dos bytes do arquivo PDF.",
    )
    sha256_texto = models.CharField(
        max_length=64,
        db_index=True,
        blank=True,
        default="",
        verbose_name="SHA-256 do Texto Normalizado",
        help_text="Hash SHA-256 do conteúdo textual extraído pelo PyMuPDF (detecta diários iguais mesmo com metadados PDF diferentes).",
    )
    tamanho_bytes = models.BigIntegerField(
        default=0,
        verbose_name="Tamanho (Bytes)",
    )
    total_paginas = models.PositiveIntegerField(
        default=0,
        verbose_name="Total de Páginas",
    )
    qtd_licitacoes_extraidas = models.PositiveIntegerField(
        default=0,
        verbose_name="Licitações Extraídas",
    )
    link_documento = models.URLField(
        max_length=500,
        blank=True,
        default="",
        verbose_name="Link do Documento Arquivado",
    )
    processado_em = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name="Processado em",
    )

    class Meta:
        db_table = "diarios_processados_hash"
        verbose_name = "Diário Processado (Hash Anti-Duplicata)"
        verbose_name_plural = "Diários Processados (Hashes Anti-Duplicata)"
        ordering = ["-processado_em"]

    def __str__(self) -> str:
        return f"{self.nome_arquivo} ({self.sha256_arquivo[:12]}...)"
