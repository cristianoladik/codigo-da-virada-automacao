import os
import unittest
from datetime import datetime
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


if __name__ == "__main__":
    unittest.main()
