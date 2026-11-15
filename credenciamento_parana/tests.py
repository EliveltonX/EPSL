from decimal import Decimal
import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from credenciamento_parana.dados_anexo1 import sincronizar_escolas_credenciamento
from credenciamento_parana.models import (
    EscolaCredenciamento,
    OfertaCursoEscola,
    TabelaPrecoCurso,
)


class CredenciamentoParanaTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="tester", password="password123")
        self.client.force_login(self.user)

    def test_sincronizacao_147_escolas_e_214_cursos(self):
        info = sincronizar_escolas_credenciamento()
        self.assertEqual(info["escolas_sincronizadas"], 147)
        self.assertEqual(info["cursos_sincronizados"], 214)
        self.assertEqual(EscolaCredenciamento.objects.count(), 147)
        self.assertEqual(OfertaCursoEscola.objects.count(), 214)

    def test_pagina_mapa_leaflet_e_favorito_api(self):
        resp = self.client.get("/credenciamento/")
        self.assertEqual(resp.status_code, 200)
        conteudo = resp.content.decode("utf-8")
        self.assertIn("leaflet.js", conteudo)
        self.assertIn("NILO CAIRO, C E-EF M N PROFIS", conteudo)
        self.assertIn("INTELIGÊNCIA ARTIFICIAL", conteudo)

        escola = EscolaCredenciamento.objects.get(item_edital=2)
        self.assertFalse(escola.favorito)

        resp_fav = self.client.post(
            f"/credenciamento/escola/{escola.id}/favorito/",
            data=json.dumps({"favorito": True}),
            content_type="application/json",
        )
        self.assertEqual(resp_fav.status_code, 200)
        dados_fav = resp_fav.json()
        self.assertTrue(dados_fav["ok"])
        self.assertTrue(dados_fav["favorito"])
        self.assertEqual(dados_fav["total_favoritos"], 1)

        escola.refresh_from_db()
        self.assertTrue(escola.favorito)

    def test_proxy_tile_osm_retorna_png_valido(self):
        resp_tile = self.client.get("/credenciamento/tiles/osm/7/45/73.png")
        self.assertEqual(resp_tile.status_code, 200)
        self.assertEqual(resp_tile["Content-Type"], "image/png")
        self.assertNotIn(len(resp_tile.content), (6672, 6725))
        self.assertGreater(len(resp_tile.content), 1000)

    def test_tabela_precos_cursos_csv_e_edicao_admin(self):
        sincronizar_escolas_credenciamento()
        preco_ia_int = TabelaPrecoCurso.objects.get(
            nome_curso="INTELIGÊNCIA ARTIFICIAL", tipo_modalidade="integrado"
        )
        self.assertEqual(preco_ia_int.valor_hora_aluno, Decimal("25.85"))
        self.assertEqual(preco_ia_int.valor_hora_turma, Decimal("904.77"))
        self.assertEqual(preco_ia_int.valor_turma_ano, Decimal("361907.18"))

        preco_ds_sub = TabelaPrecoCurso.objects.get(
            nome_curso="DESENVOLVIMENTO DE SISTEMAS", tipo_modalidade="subsequente"
        )
        self.assertEqual(preco_ds_sub.valor_hora_aluno, Decimal("13.57"))

        # Curso sem preço no CSV inicia com 0.00 e recalcula ao ser alterado no Admin
        preco_eletro = TabelaPrecoCurso.objects.get(
            nome_curso="ELETROTÉCNICA", tipo_modalidade="integrado"
        )
        self.assertEqual(preco_eletro.valor_hora_aluno, Decimal("0.00"))
        preco_eletro.valor_hora_aluno = Decimal("20.00")
        preco_eletro.save()
        preco_eletro.refresh_from_db()
        self.assertEqual(preco_eletro.valor_hora_turma, Decimal("700.00"))
        self.assertEqual(preco_eletro.valor_turma_ano, Decimal("280000.00"))



