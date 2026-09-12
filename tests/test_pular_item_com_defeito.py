"""A fila nunca pode travar num item que nao publica.

Regra do Cristiano em 12/09/2026. Um video corrompido prendeu 194 itens: o
Facebook publicou, o Instagram recusou com o erro 2207082, e o robo parava a fila
inteira "para preservar a ordem". Ficou assim por um dia, sem ninguem perceber.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import publicar  # noqa: E402


def item(data: str, horario: str, ident: str) -> dict:
    return {
        "id": ident,
        "data": data,
        "horario": horario,
        "status": "pendente",
        "instagram": {"status": "pendente"},
        "facebook": {"status": "pendente"},
        "midia": {"url_publica": "https://exemplo/v.mp4"},
    }


class PularItemComDefeitoTest(unittest.TestCase):
    def processar(self, fila: dict) -> dict:
        with patch.object(publicar, "salvar_fila"):
            return publicar.processar_fila(fila)

    def test_erro_de_arquivo_recusado_poe_o_item_de_lado_e_segue(self) -> None:
        ruim = item("2026-09-07", "09:00", "reel-ruim")
        bom = item("2026-09-07", "21:00", "reel-bom")
        fila = {"conteudos": [ruim, bom]}

        def instagram(alvo: dict) -> str:
            if alvo["id"] == "reel-ruim":
                raise RuntimeError(
                    "Instagram não processou o Reel: "
                    "{'status': 'Error: Media upload has failed with error code 2207082'}"
                )
            return "ig-ok"

        with patch.object(publicar, "publicar_instagram", side_effect=instagram):
            with patch.object(publicar, "publicar_facebook", return_value="fb-ok"):
                relatorio = self.processar(fila)

        self.assertEqual(ruim["status"], "com_defeito", "o item ruim tem de sair da fila")
        self.assertIn("2207082", ruim["motivo_defeito"])
        self.assertEqual(bom["status"], "concluido", "o item bom tem de publicar mesmo assim")
        self.assertEqual(relatorio["reels_concluidos"], 1)
        self.assertEqual(len(relatorio["postos_de_lado"]), 1)

    def test_falha_passageira_nao_poe_de_lado_mas_nao_trava(self) -> None:
        instavel = item("2026-09-07", "09:00", "reel-instavel")
        seguinte = item("2026-09-07", "21:00", "reel-seguinte")
        fila = {"conteudos": [instavel, seguinte]}

        def instagram(alvo: dict) -> str:
            if alvo["id"] == "reel-instavel":
                raise RuntimeError("connection reset by peer")
            return "ig-ok"

        with patch.object(publicar, "publicar_instagram", side_effect=instagram):
            with patch.object(publicar, "publicar_facebook", return_value="fb-ok"):
                relatorio = self.processar(fila)

        self.assertEqual(instavel["status"], "pendente", "falha de rede merece nova tentativa")
        self.assertEqual(instavel["instagram"]["tentativas"], 1)
        self.assertEqual(seguinte["status"], "concluido", "o seguinte nao pode ficar preso")
        self.assertEqual(len(relatorio["adiados"]), 1)

    def test_depois_de_tres_tentativas_o_item_sai_da_frente(self) -> None:
        teimoso = item("2026-09-07", "09:00", "reel-teimoso")
        teimoso["instagram"]["tentativas"] = 2
        fila = {"conteudos": [teimoso]}

        with patch.object(publicar, "publicar_instagram", side_effect=RuntimeError("deu ruim de novo")):
            with patch.object(publicar, "publicar_facebook", return_value="fb-ok"):
                self.processar(fila)

        self.assertEqual(teimoso["status"], "com_defeito")
        self.assertIn("3 vezes", teimoso["motivo_defeito"])

    def test_rodada_com_falha_fica_vermelha_mesmo_publicando_outro(self) -> None:
        ruim = item("2026-09-07", "09:00", "reel-ruim")
        bom = item("2026-09-07", "21:00", "reel-bom")
        fila = {"conteudos": [ruim, bom]}

        def instagram(alvo: dict) -> str:
            if alvo["id"] == "reel-ruim":
                raise RuntimeError("Media upload has failed with error code 2207082")
            return "ig-ok"

        with patch.object(publicar, "publicar_instagram", side_effect=instagram):
            with patch.object(publicar, "publicar_facebook", return_value="fb-ok"):
                relatorio = self.processar(fila)

        self.assertEqual(
            relatorio["resultado"],
            publicar.RESULTADO_FALHA,
            "o Cristiano precisa enxergar que algo deu errado, mesmo com a fila andando",
        )


if __name__ == "__main__":
    unittest.main()
