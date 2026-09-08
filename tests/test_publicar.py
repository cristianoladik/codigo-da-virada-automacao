import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import publicar


def item(data, horario, status="pendente"):
    return {
        "id": f"{data}-{horario}",
        "data": data,
        "horario": horario,
        "status": status,
        "instagram": {"status": "pendente"},
        "facebook": {"status": "pendente"},
    }


class ProximosItensTest(unittest.TestCase):
    def setUp(self):
        self.agora = datetime(2026, 9, 7, 22, 0, tzinfo=publicar.BRT)
        self.fila = {
            "conteudos": [
                item("2026-09-08", "09:00"),
                item("2026-09-07", "21:00"),
                item("2026-09-07", "09:00"),
                item("2026-09-06", "21:00", status="concluido"),
            ]
        }

    def test_retorna_todos_os_vencidos_em_ordem(self):
        with patch.dict(os.environ, {"MAX_ITENS_POR_EXECUCAO": "10"}, clear=False):
            encontrados = publicar.proximos_itens(self.fila, agora=self.agora)
        self.assertEqual([x["horario"] for x in encontrados], ["09:00", "21:00"])

    def test_respeita_limite_de_seguranca(self):
        with patch.dict(os.environ, {"MAX_ITENS_POR_EXECUCAO": "1"}, clear=False):
            encontrados = publicar.proximos_itens(self.fila, agora=self.agora)
        self.assertEqual(len(encontrados), 1)
        self.assertEqual(encontrados[0]["horario"], "09:00")

    def test_execucao_manual_seleciona_somente_o_horario_pedido(self):
        variaveis = {"DATA_PUBLICACAO": "2026-09-07", "HORARIO_PUBLICACAO": "21:00"}
        with patch.dict(os.environ, variaveis, clear=False):
            encontrados = publicar.proximos_itens(self.fila, agora=self.agora)
        self.assertEqual([x["horario"] for x in encontrados], ["21:00"])

    def test_rejeita_limite_invalido(self):
        with patch.dict(os.environ, {"MAX_ITENS_POR_EXECUCAO": "0"}, clear=False):
            with self.assertRaises(RuntimeError):
                publicar.proximos_itens(self.fila, agora=self.agora)


class ResultadoExecucaoTest(unittest.TestCase):
    def setUp(self):
        self.agora = datetime(2026, 9, 7, 22, 0, tzinfo=publicar.BRT)

    def processar(self, fila):
        with patch.dict(os.environ, {"MAX_ITENS_POR_EXECUCAO": "10"}, clear=True):
            with patch.object(publicar, "salvar_fila"):
                return publicar.processar_fila(fila, agora=self.agora)

    def test_informa_que_nada_foi_publicado_quando_nao_ha_reel_devido(self):
        fila = {"conteudos": [item("2026-09-08", "09:00")]}

        relatorio = self.processar(fila)

        self.assertEqual(relatorio["resultado"], publicar.RESULTADO_NENHUM_DEVIDO)
        self.assertEqual(relatorio["publicacoes"], [])
        self.assertEqual(relatorio["proximo"]["id"], "2026-09-08-09:00")

    def test_confirma_publicacao_com_ids_separados_por_rede(self):
        reel = item("2026-09-07", "21:00")
        fila = {"conteudos": [reel]}

        with patch.object(publicar, "publicar_instagram", return_value="ig-123"):
            with patch.object(publicar, "publicar_facebook", return_value="fb-456"):
                relatorio = self.processar(fila)

        self.assertEqual(relatorio["resultado"], publicar.RESULTADO_PUBLICADO)
        self.assertEqual(relatorio["reels_concluidos"], 1)
        self.assertEqual(
            relatorio["publicacoes"],
            [
                {"reel": "2026-09-07-21:00", "rede": "Instagram", "id": "ig-123"},
                {"reel": "2026-09-07-21:00", "rede": "Facebook", "id": "fb-456"},
            ],
        )
        self.assertEqual(reel["status"], "concluido")

    def test_falha_fica_vermelha_mesmo_com_uma_rede_confirmada(self):
        reel = item("2026-09-07", "21:00")
        fila = {"conteudos": [reel]}

        with patch.object(publicar, "publicar_instagram", side_effect=RuntimeError("Meta recusou")):
            with patch.object(publicar, "publicar_facebook", return_value="fb-456"):
                relatorio = self.processar(fila)

        self.assertEqual(relatorio["resultado"], publicar.RESULTADO_FALHA)
        self.assertEqual(relatorio["reels_concluidos"], 0)
        self.assertEqual(relatorio["publicacoes"][0]["id"], "fb-456")
        self.assertIn("instagram: Meta recusou", relatorio["erros"])

    def test_reconcilia_item_ja_confirmado_sem_declarar_nova_publicacao(self):
        reel = item("2026-09-07", "21:00")
        reel["instagram"].update({"status": "publicado", "id": "ig-antigo"})
        reel["facebook"].update({"status": "publicado", "id": "fb-antigo"})

        relatorio = self.processar({"conteudos": [reel]})

        self.assertEqual(relatorio["resultado"], publicar.RESULTADO_RECONCILIADO)
        self.assertEqual(relatorio["publicacoes"], [])
        self.assertEqual(relatorio["reels_concluidos"], 1)

    def test_main_retorna_codigo_1_somente_para_falha(self):
        casos = [
            (publicar.RESULTADO_NENHUM_DEVIDO, 0),
            (publicar.RESULTADO_PUBLICADO, 0),
            (publicar.RESULTADO_RECONCILIADO, 0),
            (publicar.RESULTADO_FALHA, 1),
        ]
        for resultado, codigo_esperado in casos:
            relatorio = {
                "resultado": resultado,
                "reels_devidos": 0,
                "reels_concluidos": 0,
                "publicacoes": [],
                "erros": [],
                "proximo": None,
            }
            with self.subTest(resultado=resultado):
                with patch.object(publicar, "FILA_FILE") as fila_file:
                    fila_file.read_text.return_value = '{"conteudos": []}'
                    with patch.object(publicar, "processar_fila", return_value=relatorio):
                        with patch.object(publicar, "registrar_resultado") as registrar:
                            codigo = publicar.main()
                self.assertEqual(codigo, codigo_esperado)
                registrar.assert_called_once_with(relatorio)

    def test_grava_output_e_resumo_inequivocos_do_github(self):
        relatorio = {
            "resultado": publicar.RESULTADO_NENHUM_DEVIDO,
            "reels_devidos": 0,
            "reels_concluidos": 0,
            "publicacoes": [],
            "erros": [],
            "proximo": item("2026-09-08", "09:00"),
        }
        with tempfile.TemporaryDirectory() as temporario:
            output = Path(temporario) / "output.txt"
            summary = Path(temporario) / "summary.md"
            ambiente = {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}

            with patch.dict(os.environ, ambiente, clear=True):
                publicar.registrar_resultado(relatorio)

            saidas = output.read_text(encoding="utf-8")
            resumo = summary.read_text(encoding="utf-8")

        self.assertIn("resultado=NENHUM_REEL_DEVIDO", saidas)
        self.assertIn("instagram_publicados=0", saidas)
        self.assertIn("NENHUM REEL DEVIDO — NADA FOI PUBLICADO", resumo)
        self.assertIn("2026-09-08 09:00", resumo)

    def test_summaries_diferenciam_publicacao_reconciliacao_e_falha(self):
        base = {
            "reels_devidos": 1,
            "reels_concluidos": 1,
            "proximo": None,
            "executado_em": "2026-09-07T23:45:00-03:00",
        }
        publicado = {
            **base,
            "resultado": publicar.RESULTADO_PUBLICADO,
            "publicacoes": [{"reel": "reel-1", "rede": "Instagram", "id": "ig-123"}],
            "erros": [],
        }
        reconciliado = {
            **base,
            "resultado": publicar.RESULTADO_RECONCILIADO,
            "publicacoes": [],
            "erros": [],
        }
        falha = {
            **base,
            "resultado": publicar.RESULTADO_FALHA,
            "reels_concluidos": 0,
            "publicacoes": [],
            "erros": ["instagram: Meta recusou"],
        }

        resumo_publicado = publicar.resumo_markdown(publicado)
        resumo_reconciliado = publicar.resumo_markdown(reconciliado)
        resumo_falha = publicar.resumo_markdown(falha)

        self.assertIn("PUBLICAÇÃO CONFIRMADA", resumo_publicado)
        self.assertIn("ig-123", resumo_publicado)
        self.assertIn("NADA NOVO FOI PUBLICADO", resumo_reconciliado)
        self.assertIn("FALHA NA PUBLICAÇÃO", resumo_falha)
        self.assertIn("instagram: Meta recusou", resumo_falha)


if __name__ == "__main__":
    unittest.main()
