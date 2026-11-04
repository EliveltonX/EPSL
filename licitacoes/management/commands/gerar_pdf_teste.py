"""
Management Command: gerar_pdf_teste

Gera um arquivo PDF realista de Diário Oficial com 4 páginas usando PyMuPDF:
- Página 1: Atos administrativos sem relação com o nicho (deve ser ignorada pelo Regex).
- Página 2: Aviso de Pregão Eletrônico para Laboratório de Informática e Computadores (deve ser capturada).
- Página 3: Decreto de feriado municipal sem relação com o nicho (deve ser ignorada pelo Regex).
- Página 4: Chamada Pública para Livros Didáticos de Computação e Formação em Tecnologia (deve ser capturada).
"""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser


PAGINAS_EXEMPLO = [
    # Página 1 (Irrelevante para o nicho -> deve ser filtrada fora pelo Regex)
    (
        "DIÁRIO OFICIAL DO MUNICÍPIO DE CURITIBA - PR\n"
        "Data de Publicação: 03/11/2026 | Edição Nº 2.450 | Página 1\n\n"
        "PORTARIA Nº 412/2026 - SECRETARIA DE ADMINISTRAÇÃO\n"
        "O Secretário Municipal de Administração, no uso de suas atribuições legais,\n"
        "RESOLVE nomear a comissão de avaliação de bens patrimoniais móveis do almoxarifado central.\n\n"
        "EXTRATO DE CONTRATO Nº 088/2026\n"
        "Objeto: Aquisição de materiais de limpeza, copos descartáveis e insumos de copa.\n"
    ),
    # Página 2 (Relevante para o nicho -> deve dar match no Regex e ser extraída pelo Gemini)
    (
        "DIÁRIO OFICIAL DO MUNICÍPIO DE CURITIBA - PR\n"
        "Data de Publicação: 03/11/2026 | Edição Nº 2.450 | Página 2\n\n"
        "PREFEITURA MUNICIPAL DE CURITIBA - SECRETARIA MUNICIPAL DE EDUCAÇÃO\n"
        "AVISO DE LICITAÇÃO - PREGÃO ELETRÔNICO Nº 095/2026\n\n"
        "Órgão Emissor: Secretaria Municipal de Educação de Curitiba\n"
        "Modalidade: Pregão Eletrônico\n"
        "Data de Publicação: 2026-11-03\n"
        "Objeto: Contratação de empresa especializada para fornecimento e instalação de 120 computadores,\n"
        "notebooks, montagem de laboratórios de informática, infraestrutura de TI e manutenção de computadores\n"
        "para atender às escolas da rede pública municipal de ensino.\n"
        "Abertura das propostas: 18/11/2026 às 09h00 no portal de compras.\n"
    ),
    # Página 3 (Irrelevante para o nicho -> deve ser filtrada fora pelo Regex)
    (
        "DIÁRIO OFICIAL DO MUNICÍPIO DE CURITIBA - PR\n"
        "Data de Publicação: 03/11/2026 | Edição Nº 2.450 | Página 3\n\n"
        "SECRETARIA MUNICIPAL DE OBRAS E PAVIMENTAÇÃO\n"
        "AVISO DE TOMADA DE PREÇOS Nº 014/2026\n"
        "Objeto: Execução de serviços de recapeamento asfáltico e drenagem pluvial na Avenida Brasil.\n"
    ),
    # Página 4 (Relevante para o nicho -> deve dar match no Regex e ser extraída pelo Gemini)
    (
        "DIÁRIO OFICIAL DO MUNICÍPIO DE CURITIBA - PR\n"
        "Data de Publicação: 03/11/2026 | Edição Nº 2.450 | Página 4\n\n"
        "PREFEITURA MUNICIPAL DE CURITIBA - SECRETARIA MUNICIPAL DE EDUCAÇÃO\n"
        "AVISO DE DISPENSA DE LICITAÇÃO Nº 033/2026\n\n"
        "Órgão Emissor: Secretaria Municipal de Educação de Curitiba\n"
        "Modalidade: Dispensa de Licitação\n"
        "Data de Publicação: 2026-11-03\n"
        "Objeto: Aquisição de livros didáticos de pensamento computacional, software educacional\n"
        "e capacitação para Formação em Computação e Tecnologia da Informação dos professores da rede municipal.\n"
    ),
]


class Command(BaseCommand):
    help = "Gera um PDF de Diário Oficial sintético em storage_local/novos_diarios/ para testar a pipeline do MVP."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--filename",
            type=str,
            default="diario_oficial_teste_mvp.pdf",
            help="Nome do arquivo PDF a ser gerado (padrão: diario_oficial_teste_mvp.pdf).",
        )

    def handle(self, *args, **options) -> None:
        filename: str = options["filename"]
        input_dir = (
            Path(settings.LOCAL_STORAGE_ROOT)
            / settings.SUPABASE_INPUT_PREFIX.strip("/")
        )
        input_dir.mkdir(parents=True, exist_ok=True)
        dest_path = input_dir / filename

        doc = fitz.open()
        for texto_pagina in PAGINAS_EXEMPLO:
            page = doc.new_page(width=595, height=842)  # A4
            rect = fitz.Rect(50, 50, 545, 792)
            page.insert_textbox(
                rect,
                texto_pagina,
                fontsize=11,
                fontname="helv",
            )

        doc.save(str(dest_path))
        doc.close()

        self.stdout.write(
            self.style.SUCCESS(
                f"PDF de teste gerado com sucesso em: {dest_path} (4 páginas: páginas 2 e 4 contêm licitações de TI)."
            )
        )

