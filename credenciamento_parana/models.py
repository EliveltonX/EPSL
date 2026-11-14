from django.db import models


class EscolaCredenciamento(models.Model):
    """
    Representa cada um dos 147 estabelecimentos de ensino do Anexo I (e contatos do Anexo IV)
    do Termo de Referência Inicial de Credenciamento de Cursos (SEED/PR - Protocolo nº 26.748.363-9).
    Modelagem estrita relacional (Zero JSONField).
    """

    item_edital = models.PositiveIntegerField(
        unique=True,
        db_index=True,
        verbose_name="Item do Edital (1 a 147)",
    )
    nucleo_regional = models.CharField(
        max_length=120,
        db_index=True,
        verbose_name="Núcleo Regional de Educação (NRE)",
    )
    municipio = models.CharField(
        max_length=120,
        db_index=True,
        verbose_name="Município",
    )
    nome_escola = models.CharField(
        max_length=255,
        db_index=True,
        verbose_name="Nome do Estabelecimento de Ensino",
    )
    codigo_mec = models.CharField(
        max_length=20,
        db_index=True,
        verbose_name="Código MEC / INEP",
    )
    endereco = models.CharField(
        max_length=350,
        db_index=True,
        verbose_name="Endereço Completo",
    )
    cep = models.CharField(
        max_length=20,
        blank=True,
        default="",
        verbose_name="CEP (Anexo IV)",
    )
    telefone = models.CharField(
        max_length=120,
        blank=True,
        default="",
        verbose_name="Telefone (Anexo IV)",
    )
    email = models.CharField(
        max_length=180,
        blank=True,
        default="",
        verbose_name="E-mail Institucional (Anexo IV)",
    )
    responsavel = models.CharField(
        max_length=200,
        blank=True,
        default="",
        verbose_name="Responsável pela Instituição (Anexo IV)",
    )
    latitude = models.FloatField(
        db_index=True,
        verbose_name="Latitude (WGS84)",
    )
    longitude = models.FloatField(
        db_index=True,
        verbose_name="Longitude (WGS84)",
    )
    favorito = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="Marcado como Favorito",
    )
    observacoes = models.TextField(
        blank=True,
        default="",
        verbose_name="Observações Internas da Escola",
    )
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "credenciamento_escolas"
        ordering = ["item_edital"]
        verbose_name = "Escola do Credenciamento (Anexo I)"
        verbose_name_plural = "Escolas do Credenciamento (Anexo I)"
        indexes = [
            models.Index(fields=["municipio", "favorito"], name="idx_cred_mun_fav"),
            models.Index(fields=["nucleo_regional"], name="idx_cred_nucleo"),
        ]

    def __str__(self) -> str:
        return f"[Item {self.item_edital}] {self.nome_escola} ({self.municipio})"


class OfertaCursoEscola(models.Model):
    """
    Representa cada oferta de curso técnico vinculada a uma escola do Anexo I,
    com sua modalidade (Médio Integrado ou Prof/Técnico Subsequente), carga horária anual
    (hora-relógio), quantidade de turmas e turno.
    Modelagem estrita relacional (Zero JSONField).
    """

    TIPO_MODALIDADE_CHOICES = [
        ("integrado", "Médio Integrado"),
        ("subsequente", "Prof/Técnico (Subsequente)"),
    ]

    TURNO_CHOICES = [
        ("Manhã", "Manhã"),
        ("Tarde", "Tarde"),
        ("Noite", "Noite"),
        ("Integral", "Integral"),
    ]

    escola = models.ForeignKey(
        EscolaCredenciamento,
        on_delete=models.CASCADE,
        related_name="ofertas_cursos",
        verbose_name="Escola",
    )
    modalidade = models.CharField(
        max_length=80,
        db_index=True,
        verbose_name="Modalidade Original (Anexo I)",
    )
    tipo_modalidade = models.CharField(
        max_length=30,
        choices=TIPO_MODALIDADE_CHOICES,
        db_index=True,
        verbose_name="Tipo de Modalidade (Integrado / Subsequente)",
    )
    nome_curso = models.CharField(
        max_length=160,
        db_index=True,
        verbose_name="Curso Técnico",
    )
    ch_anual_curso = models.PositiveIntegerField(
        default=400,
        verbose_name="CH Anual Curso (h)",
    )
    ch_anual_coord = models.PositiveIntegerField(
        default=200,
        verbose_name="CH Anual Coordenação (h)",
    )
    ch_anual_sup_tecnico = models.PositiveIntegerField(
        default=120,
        verbose_name="CH Anual Suporte Técnico (h)",
    )
    turmas = models.PositiveIntegerField(
        default=1,
        verbose_name="Turmas",
    )
    turno = models.CharField(
        max_length=40,
        choices=TURNO_CHOICES,
        db_index=True,
        verbose_name="Turno",
    )

    class Meta:
        db_table = "credenciamento_ofertas_cursos"
        ordering = ["escola__item_edital", "id"]
        verbose_name = "Oferta de Curso na Escola"
        verbose_name_plural = "Ofertas de Cursos nas Escolas"
        indexes = [
            models.Index(
                fields=["nome_curso", "tipo_modalidade", "turno"],
                name="idx_cred_curso_mod_turno",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.nome_curso} ({self.modalidade} - {self.turno}) — {self.escola.nome_escola}"
