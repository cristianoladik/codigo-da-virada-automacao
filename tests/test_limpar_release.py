import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import limpar_release


def midia(asset, removida=False):
    dados = {
        "asset": asset,
        "sha256": hashlib.sha256(asset.encode("utf-8")).hexdigest(),
        "tamanho_bytes": 1000 + len(asset),
    }
    if removida:
        dados["removido_da_release_em"] = "2026-09-08T10:00:00-03:00"
    return dados


class AtivosConcluidosTest(unittest.TestCase):
    def test_encontra_reel_somente_com_ids_confirmados(self):
        confirmada = midia("reel.mp4")
        fila = {
            "conteudos": [
                {
                    "status": "concluido",
                    "midia": confirmada,
                    "instagram": {"status": "publicado", "id": "ig"},
                    "facebook": {"status": "publicado", "id": "fb"},
                },
                {
                    "status": "concluido",
                    "midia": midia("sem-id.mp4"),
                    "instagram": {"status": "publicado"},
                    "facebook": {"status": "publicado", "id": "fb"},
                },
                {"status": "pendente", "midia": midia("pendente.mp4")},
                {"status": "concluido", "midia": midia("removido.mp4", removida=True)},
            ]
        }
        self.assertEqual(limpar_release.ativos_concluidos(fila), [confirmada])

    def test_asset_referenciado_duas_vezes_cancela_limpeza(self):
        fila = {
            "conteudos": [
                {"status": "pendente", "midia": midia("repetido.mp4")},
                {"status": "pendente", "midia": midia("repetido.mp4")},
            ]
        }
        with self.assertRaisesRegex(RuntimeError, "mais de uma vez"):
            limpar_release.ativos_concluidos(fila)

    def test_encontra_todas_as_partes_confirmadas_de_pacote_concluido(self):
        primeira = midia("story-01.mp4")
        segunda = midia("story-02.mp4")
        fila = {
            "pacotes": [
                {
                    "status": "concluido",
                    "partes": [
                        {
                            "status": "concluido",
                            "midia": primeira,
                            "instagram": {"status": "publicado", "id": "ig-1"},
                            "facebook": {"status": "publicado", "id": "fb-1"},
                        },
                        {
                            "status": "concluido",
                            "midia": segunda,
                            "instagram": {"status": "publicado", "id": "ig-2"},
                            "facebook": {"status": "publicado", "id": "fb-2"},
                        },
                    ],
                },
                {
                    "status": "pendente",
                    "partes": [
                        {
                            "status": "concluido",
                            "midia": midia("nao-remover.mp4"),
                            "instagram": {"status": "publicado", "id": "ig"},
                            "facebook": {"status": "publicado", "id": "fb"},
                        }
                    ],
                },
            ]
        }
        self.assertEqual(limpar_release.ativos_concluidos(fila), [primeira, segunda])

    def test_nao_remove_parte_sem_confirmacao_nas_duas_redes(self):
        fila = {
            "pacotes": [
                {
                    "status": "concluido",
                    "partes": [
                        {
                            "status": "concluido",
                            "midia": midia("incompleto.mp4"),
                            "instagram": {"status": "publicado", "id": "ig"},
                            "facebook": {"status": "erro"},
                        }
                    ],
                }
            ]
        }
        with self.assertRaisesRegex(RuntimeError, "Pacote concluído inconsistente"):
            limpar_release.ativos_concluidos(fila)

    def test_pacote_concluido_parcialmente_nao_libera_nenhum_asset(self):
        fila = {
            "pacotes": [
                {
                    "id": "story-inconsistente",
                    "status": "concluido",
                    "partes": [
                        {
                            "status": "concluido",
                            "midia": midia("confirmada.mp4"),
                            "instagram": {"status": "publicado", "id": "ig"},
                            "facebook": {"status": "publicado", "id": "fb"},
                        },
                        {
                            "status": "pendente",
                            "midia": midia("pendente.mp4"),
                            "instagram": {"status": "pendente"},
                            "facebook": {"status": "pendente"},
                        },
                    ],
                }
            ]
        }
        with self.assertRaisesRegex(RuntimeError, "Pacote concluído inconsistente"):
            limpar_release.ativos_concluidos(fila)


class LimpezaIntegradaTest(unittest.TestCase):
    def test_divergencia_de_sha_preserva_asset_e_nao_marca_remocao(self):
        dados_midia = midia("story-divergente.mp4")
        fila = {
            "pacotes": [
                {
                    "status": "concluido",
                    "partes": [
                        {
                            "status": "concluido",
                            "midia": dados_midia,
                            "instagram": {"status": "publicado", "id": "ig"},
                            "facebook": {"status": "publicado", "id": "fb"},
                        }
                    ],
                }
            ]
        }
        resposta_release = Mock()
        resposta_release.raise_for_status.return_value = None
        resposta_release.json.return_value = {
            "assets": [
                {
                    "name": "story-divergente.mp4",
                    "id": 201,
                    "digest": "sha256:" + "f" * 64,
                    "size": dados_midia["tamanho_bytes"],
                }
            ]
        }

        with tempfile.TemporaryDirectory() as temporario:
            fila_path = Path(temporario) / "fila-stories.json"
            fila_path.write_text(json.dumps(fila), encoding="utf-8")
            with patch.dict(
                os.environ,
                {"GITHUB_REPOSITORY": "conta/repositorio", "GITHUB_TOKEN": "token"},
                clear=True,
            ), patch("sys.argv", ["limpar_release.py", "--fila", str(fila_path)]), patch.object(
                limpar_release.requests, "get", return_value=resposta_release
            ), patch.object(limpar_release.requests, "delete") as remover:
                with self.assertRaisesRegex(RuntimeError, "asset preservado"):
                    limpar_release.main()
            resultado = json.loads(fila_path.read_text(encoding="utf-8"))

        remover.assert_not_called()
        self.assertNotIn(
            "removido_da_release_em",
            resultado["pacotes"][0]["partes"][0]["midia"],
        )

    def test_remove_assets_de_story_e_grava_marcadores(self):
        fila = {
            "pacotes": [
                {
                    "status": "concluido",
                    "partes": [
                        {
                            "status": "concluido",
                            "midia": midia("story-01.mp4"),
                            "instagram": {"status": "publicado", "id": "ig-1"},
                            "facebook": {"status": "publicado", "id": "fb-1"},
                        },
                        {
                            "status": "concluido",
                            "midia": midia("story-02.mp4"),
                            "instagram": {"status": "publicado", "id": "ig-2"},
                            "facebook": {"status": "publicado", "id": "fb-2"},
                        },
                    ],
                }
            ]
        }
        resposta_release = Mock()
        resposta_release.raise_for_status.return_value = None
        resposta_release.json.return_value = {
            "assets": [
                {"name": "story-01.mp4", "id": 101, "digest": "sha256:" + midia("story-01.mp4")["sha256"], "size": midia("story-01.mp4")["tamanho_bytes"]},
                {"name": "story-02.mp4", "id": 102, "digest": "sha256:" + midia("story-02.mp4")["sha256"], "size": midia("story-02.mp4")["tamanho_bytes"]},
            ]
        }
        resposta_delete = Mock(status_code=204, text="")

        with tempfile.TemporaryDirectory() as temporario:
            fila_path = Path(temporario) / "fila-stories.json"
            fila_path.write_text(json.dumps(fila), encoding="utf-8")
            ambiente = {
                "GITHUB_REPOSITORY": "conta/repositorio",
                "GITHUB_TOKEN": "token",
                "RELEASE_TAG": "fila-instagram-facebook",
            }
            with patch.dict(os.environ, ambiente, clear=True):
                with patch(
                    "sys.argv",
                    ["limpar_release.py", "--fila", str(fila_path)],
                ):
                    with patch.object(
                        limpar_release.requests,
                        "get",
                        return_value=resposta_release,
                    ):
                        with patch.object(
                            limpar_release.requests,
                            "delete",
                            return_value=resposta_delete,
                        ) as remover:
                            limpar_release.main()

            resultado = json.loads(fila_path.read_text(encoding="utf-8"))

        self.assertEqual(remover.call_count, 2)
        for parte in resultado["pacotes"][0]["partes"]:
            self.assertIn("removido_da_release_em", parte["midia"])

    def test_preserva_marcador_do_asset_anterior_se_limpeza_seguinte_falhar(self):
        fila = {
            "pacotes": [
                {
                    "status": "concluido",
                    "partes": [
                        {
                            "status": "concluido",
                            "midia": midia("story-01.mp4"),
                            "instagram": {"status": "publicado", "id": "ig-1"},
                            "facebook": {"status": "publicado", "id": "fb-1"},
                        },
                        {
                            "status": "concluido",
                            "midia": midia("story-02.mp4"),
                            "instagram": {"status": "publicado", "id": "ig-2"},
                            "facebook": {"status": "publicado", "id": "fb-2"},
                        },
                    ],
                }
            ]
        }
        resposta_release = Mock()
        resposta_release.raise_for_status.return_value = None
        resposta_release.json.return_value = {
            "assets": [
                {"name": "story-01.mp4", "id": 101, "digest": "sha256:" + midia("story-01.mp4")["sha256"], "size": midia("story-01.mp4")["tamanho_bytes"]},
                {"name": "story-02.mp4", "id": 102, "digest": "sha256:" + midia("story-02.mp4")["sha256"], "size": midia("story-02.mp4")["tamanho_bytes"]},
            ]
        }
        sucesso = Mock(status_code=204, text="")
        falha = Mock(status_code=500, text="falha simulada")

        with tempfile.TemporaryDirectory() as temporario:
            fila_path = Path(temporario) / "fila-stories.json"
            fila_path.write_text(json.dumps(fila), encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "GITHUB_REPOSITORY": "conta/repositorio",
                    "GITHUB_TOKEN": "token",
                },
                clear=True,
            ):
                with patch(
                    "sys.argv",
                    ["limpar_release.py", "--fila", str(fila_path)],
                ):
                    with patch.object(
                        limpar_release.requests,
                        "get",
                        return_value=resposta_release,
                    ):
                        with patch.object(
                            limpar_release.requests,
                            "delete",
                            side_effect=[sucesso, falha],
                        ):
                            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                                limpar_release.main()
            resultado = json.loads(fila_path.read_text(encoding="utf-8"))

        primeira, segunda = resultado["pacotes"][0]["partes"]
        self.assertIn("removido_da_release_em", primeira["midia"])
        self.assertNotIn("removido_da_release_em", segunda["midia"])


if __name__ == "__main__":
    unittest.main()
