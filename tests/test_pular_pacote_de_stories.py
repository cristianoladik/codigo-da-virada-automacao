"""Um pacote de Stories recusado nao pode deixar o dia sem Story.

Regra do Cristiano em 15/09/2026: quando ha recusa, tentar os proximos da fila
ate conseguir. O pacote recusado vai para "com_defeito" e o proximo pendente
assume o dia, na mesma rodada.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import publicar_stories  # noqa: E402
from tests.test_publicar_stories import pacote  # noqa: E402


class PularPacoteDeStoriesTest(unittest.TestCase):
    def test_pacote_recusado_sai_de_lado_e_o_seguinte_assume_o_dia(self):
        ruim = pacote("2026-09-08", identificador="story-ruim")
        bom = pacote("2026-09-09", identificador="story-bom")
        fila = {"pacotes": [ruim, bom]}

        def instagram(parte):
            if "story-ruim" in parte["midia"]["asset"]:
                raise RuntimeError("Media upload has failed with error code 2207082")
            return "ig-ok"

        with tempfile.TemporaryDirectory() as temporario:
            arquivo = Path(temporario) / "parte.mp4"
            arquivo.write_bytes(b"ok")
            with patch.object(publicar_stories, "salvar_fila"), patch.object(
                publicar_stories, "baixar_midia", return_value=arquivo
            ), patch.object(publicar_stories, "validar_contas_meta"), patch.object(
                publicar_stories, "publicar_instagram", side_effect=instagram
            ), patch.object(publicar_stories, "publicar_facebook", return_value="fb-ok"):
                relatorio = publicar_stories.processar_fila(
                    fila, agora=datetime(2026, 9, 8, 12, 0, tzinfo=publicar_stories.BRT)
                )

        self.assertEqual(ruim["status"], "com_defeito")
        self.assertIn("2207082", ruim["motivo_defeito"])
        self.assertEqual(bom["status"], "concluido", "o pacote seguinte tem de publicar no lugar")
        self.assertEqual((bom["data"], bom["reagendado_de"]), ("2026-09-08", "2026-09-09 09:00"))
        self.assertEqual(relatorio["resultado"], publicar_stories.RESULTADO_FALHA)
        self.assertEqual(len(relatorio["postos_de_lado"]), 1)
        self.assertEqual(relatorio["pacotes_concluidos"], 1)


if __name__ == "__main__":
    unittest.main()
