import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import publicar_stories


def parte(ordem, duracao=30.0, sufixo="base"):
    return {
        "ordem": ordem,
        "status": "pendente",
        "midia": {
            "asset": f"story-{sufixo}-parte-{ordem:02d}.mp4",
            "url_publica": f"https://example.test/story-{sufixo}-parte-{ordem:02d}.mp4",
            "sha256": f"{ordem:02x}" * 32,
            "tamanho_bytes": 1000 + ordem,
            "duracao_segundos": duracao,
        },
        "instagram": {"status": "pendente"},
        "facebook": {"status": "pendente"},
    }


def pacote(data, identificador=None, status="pendente", quantidade_partes=1):
    identificador = identificador or f"story-{data}"
    return {
        "id": identificador,
        "data": data,
        "horario": publicar_stories.HORARIO_STORY,
        "status": status,
        "origem": {
            "arquivo": f"{data}.mp4",
            "sha256": (data.replace("-", "") + "0" * 64)[:64],
        },
        "partes": [
            parte(indice, sufixo=identificador)
            for indice in range(1, quantidade_partes + 1)
        ],
    }


class SelecaoEValidacaoTest(unittest.TestCase):
    def setUp(self):
        self.agora = datetime(2026, 9, 10, 12, 0, tzinfo=publicar_stories.BRT)

    def test_recupera_um_pacote_vencido_por_dia_em_ordem(self):
        fila = {
            "pacotes": [
                pacote("2026-09-11"),
                pacote("2026-09-09"),
                pacote("2026-09-06"),
                pacote("2026-09-08"),
                pacote("2026-09-07"),
            ]
        }
        with patch.dict(os.environ, {"MAX_PACOTES_POR_EXECUCAO": "1"}, clear=True):
            encontrados = publicar_stories.proximos_pacotes(fila, agora=self.agora)
        self.assertEqual(
            [item["data"] for item in encontrados],
            ["2026-09-06"],
        )

    def test_nao_publica_segundo_pacote_no_mesmo_dia_nem_antes_das_nove(self):
        concluido = pacote("2026-09-08", status="concluido")
        concluido["concluido_em"] = "2026-09-10T09:03:00-03:00"
        concluido["partes"][0].update(
            {
                "status": "concluido",
                "instagram": {"status": "publicado", "id": "ig"},
                "facebook": {"status": "publicado", "id": "fb"},
            }
        )
        pendente = pacote("2026-09-09")
        with patch.dict(os.environ, {"MAX_PACOTES_POR_EXECUCAO": "1"}, clear=True):
            self.assertEqual(
                publicar_stories.proximos_pacotes(
                    {"pacotes": [concluido, pendente]}, agora=self.agora
                ),
                [],
            )
            antes_das_nove = self.agora.replace(hour=8, minute=59)
            self.assertEqual(
                publicar_stories.proximos_pacotes(
                    {"pacotes": [pendente]}, agora=antes_das_nove
                ),
                [],
            )

    def test_execucao_manual_nao_pula_pacote_anterior(self):
        fila = {"pacotes": [pacote("2026-09-08"), pacote("2026-09-09")]}
        with patch.dict(
            os.environ,
            {"DATA_PUBLICACAO": "2026-09-09", "MAX_PACOTES_POR_EXECUCAO": "1"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "não pode pular"):
                publicar_stories.proximos_pacotes(fila, agora=self.agora)

    def test_execucao_manual_seleciona_o_pacote_mais_antigo(self):
        fila = {"pacotes": [pacote("2026-09-08"), pacote("2026-09-09")]}
        with patch.dict(
            os.environ,
            {"DATA_PUBLICACAO": "2026-09-08", "MAX_PACOTES_POR_EXECUCAO": "1"},
            clear=True,
        ):
            encontrados = publicar_stories.proximos_pacotes(fila, agora=self.agora)
        self.assertEqual([item["data"] for item in encontrados], ["2026-09-08"])

    def test_execucao_manual_nao_fura_horario_data_ou_cadencia(self):
        futuro = pacote("2026-09-11")
        with patch.dict(
            os.environ,
            {"DATA_PUBLICACAO": "2026-09-11", "MAX_PACOTES_POR_EXECUCAO": "1"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "pacote futuro"):
                publicar_stories.proximos_pacotes(
                    {"pacotes": [futuro]}, agora=self.agora
                )

        anterior = pacote("2026-09-09")
        with patch.dict(
            os.environ,
            {"DATA_PUBLICACAO": "2026-09-09", "MAX_PACOTES_POR_EXECUCAO": "1"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "antes das 09:00"):
                publicar_stories.proximos_pacotes(
                    {"pacotes": [anterior]},
                    agora=self.agora.replace(hour=8, minute=59),
                )

    def test_rejeita_mais_de_um_pacote_no_mesmo_dia(self):
        fila = {
            "pacotes": [
                pacote("2026-09-08", "story-a"),
                pacote("2026-09-08", "story-b"),
            ]
        }
        with self.assertRaisesRegex(RuntimeError, "mais de um pacote"):
            publicar_stories.validar_fila(fila)

    def test_rejeita_partes_fora_de_ordem(self):
        item = pacote("2026-09-08", quantidade_partes=2)
        item["partes"].reverse()
        with self.assertRaisesRegex(RuntimeError, "ordem contínua"):
            publicar_stories.validar_fila({"pacotes": [item]})

    def test_rejeita_pacote_com_partes_demais(self):
        item = pacote(
            "2026-09-08",
            quantidade_partes=publicar_stories.MAX_PARTES_POR_PACOTE + 1,
        )
        with self.assertRaisesRegex(RuntimeError, "excede o limite"):
            publicar_stories.validar_fila({"pacotes": [item]})

    def test_rejeita_parte_acima_de_59_segundos(self):
        item = pacote("2026-09-08")
        item["partes"][0]["midia"]["duracao_segundos"] = 59.001
        with self.assertRaisesRegex(RuntimeError, "intervalo aceito é 3–59s"):
            publicar_stories.validar_fila({"pacotes": [item]})

    def test_rejeita_parte_curta_ou_acima_de_100_mb(self):
        curta = pacote("2026-09-08")
        curta["partes"][0]["midia"]["duracao_segundos"] = 2.999
        with self.assertRaisesRegex(RuntimeError, "intervalo aceito é 3–59s"):
            publicar_stories.validar_fila({"pacotes": [curta]})

        grande = pacote("2026-09-08")
        grande["partes"][0]["midia"]["tamanho_bytes"] = 100_000_001
        with self.assertRaisesRegex(RuntimeError, "Tamanho inválido"):
            publicar_stories.validar_fila({"pacotes": [grande]})

    def test_rejeita_origem_sem_arquivo_e_duracao_nao_finita(self):
        sem_arquivo = pacote("2026-09-08")
        sem_arquivo["origem"].pop("arquivo")
        with self.assertRaisesRegex(RuntimeError, "Origem ausente"):
            publicar_stories.validar_fila({"pacotes": [sem_arquivo]})

        for duracao in (float("nan"), float("inf"), float("-inf")):
            item = pacote("2026-09-08")
            item["partes"][0]["midia"]["duracao_segundos"] = duracao
            with self.subTest(duracao=duracao):
                with self.assertRaisesRegex(RuntimeError, "tem .*s"):
                    publicar_stories.validar_fila({"pacotes": [item]})

    def test_rejeita_limite_de_execucao_invalido(self):
        for valor in ("0", "2", "10"):
            with self.subTest(valor=valor), patch.dict(
                os.environ, {"MAX_PACOTES_POR_EXECUCAO": valor}, clear=True
            ):
                with self.assertRaisesRegex(RuntimeError, "exatamente 1"):
                    publicar_stories.limite_por_execucao()

    def test_rejeita_origem_e_asset_repetidos(self):
        primeiro = pacote("2026-09-08")
        segundo = pacote("2026-09-09")
        segundo["origem"]["sha256"] = primeiro["origem"]["sha256"]
        with self.assertRaisesRegex(RuntimeError, "origem repetido"):
            publicar_stories.validar_fila({"pacotes": [primeiro, segundo]})

        segundo = pacote("2026-09-09")
        segundo["partes"][0]["midia"]["asset"] = primeiro["partes"][0]["midia"]["asset"]
        with self.assertRaisesRegex(RuntimeError, "Asset repetido"):
            publicar_stories.validar_fila({"pacotes": [primeiro, segundo]})

    def test_rejeita_conclusao_sem_ids_das_duas_redes(self):
        item = pacote("2026-09-08", status="concluido")
        item["partes"][0]["status"] = "concluido"
        item["partes"][0]["instagram"] = {"status": "publicado", "id": "ig"}
        with self.assertRaisesRegex(RuntimeError, "concluída sem confirmação"):
            publicar_stories.validar_fila({"pacotes": [item]})


class PublicacaoApiTest(unittest.TestCase):
    def test_preflight_de_todas_as_partes_falha_antes_da_meta(self):
        story = pacote("2026-09-08", quantidade_partes=2)
        with tempfile.TemporaryDirectory() as temporario:
            primeira = Path(temporario) / "primeira.mp4"
            primeira.write_bytes(b"ok")
            with patch.object(
                publicar_stories,
                "baixar_midia",
                side_effect=[primeira, RuntimeError("asset 2 ausente")],
            ), patch.object(
                publicar_stories, "validar_contas_meta"
            ) as contas, patch.object(
                publicar_stories, "publicar_instagram"
            ) as instagram, patch.object(
                publicar_stories, "publicar_facebook"
            ) as facebook:
                with self.assertRaisesRegex(RuntimeError, "asset 2 ausente"):
                    publicar_stories.processar_fila(
                        {"pacotes": [story]},
                        agora=datetime(2026, 9, 8, 12, 0, tzinfo=publicar_stories.BRT),
                    )

            self.assertFalse(primeira.exists())
        contas.assert_not_called()
        instagram.assert_not_called()
        facebook.assert_not_called()

    def test_preflight_confirma_identidade_vinculo_instagram_e_pagina(self):
        segredos = {
            "IG_ACCESS_TOKEN": "ig-token",
            "IG_BUSINESS_ID": "ig-id",
            "FB_PAGE_ACCESS_TOKEN": "fb-token",
            "FB_PAGE_ID": "page-id",
        }
        with patch.object(
            publicar_stories, "obrigatoria", side_effect=lambda nome: segredos[nome]
        ), patch.object(
            publicar_stories,
            "graph_get",
            side_effect=[
                {
                    "id": "ig-id",
                    "username": "codigodavirada_br",
                },
                {
                    "id": "page-id",
                    "name": "Código da Virada",
                    "access_token": "page-token",
                    "instagram_business_account": {
                        "id": "ig-id",
                        "username": "codigodavirada_br",
                    },
                },
            ],
        ):
            resultado = publicar_stories.validar_contas_meta()

        self.assertEqual(
            resultado["instagram"]["vinculo"], "instagram_business_account"
        )
        self.assertEqual(resultado["facebook"]["id"], "page-id")

    def test_preflight_rejeita_id_instagram_divergente_antes_de_consultar_pagina(self):
        segredos = {"IG_ACCESS_TOKEN": "ig-token", "IG_BUSINESS_ID": "ig-id"}
        with patch.object(
            publicar_stories, "obrigatoria", side_effect=lambda nome: segredos[nome]
        ), patch.object(
            publicar_stories,
            "graph_get",
            return_value={
                "id": "outro-ig-id",
                "username": "codigodavirada_br",
            },
        ) as consultar:
            with self.assertRaisesRegex(RuntimeError, "conta Instagram diferente"):
                publicar_stories.validar_contas_meta()

        consultar.assert_called_once()

    def test_preflight_rejeita_conta_business_com_username_errado(self):
        segredos = {"IG_ACCESS_TOKEN": "ig-token", "IG_BUSINESS_ID": "ig-id"}
        with patch.object(
            publicar_stories, "obrigatoria", side_effect=lambda nome: segredos[nome]
        ), patch.object(
            publicar_stories,
            "graph_get",
            return_value={
                "id": "ig-id",
                "username": "outra_conta",
            },
        ) as consultar:
            with self.assertRaisesRegex(RuntimeError, "conta esperada"):
                publicar_stories.validar_contas_meta()

        consultar.assert_called_once()

    def test_confirma_facebook_no_edge_de_stories_publicados(self):
        resposta = {
            "data": [
                {
                    "status": "published",
                    "post_id": "post-1",
                    "media_id": "video-1",
                    "url": "https://facebook.test/post-1",
                }
            ]
        }
        with patch.dict(
            os.environ, {"FB_STORY_CONFIRM_TIMEOUT_SECONDS": "0"}, clear=True
        ), patch.object(
            publicar_stories, "graph_get", return_value=resposta
        ) as consultar:
            story = publicar_stories.aguardar_story_facebook(
                "pagina",
                "token",
                "post-1",
                "video-1",
                "2026-09-08T09:00:00-03:00",
            )

        self.assertEqual(story["url"], "https://facebook.test/post-1")
        self.assertEqual(consultar.call_args.args[0], "pagina/stories")
        self.assertEqual(
            consultar.call_args.args[1]["status"],
            json.dumps(["PUBLISHED", "ARCHIVED"]),
        )

    def test_confirma_facebook_archived_como_prova_historica(self):
        resposta = {
            "data": [
                {
                    "status": "ARCHIVED",
                    "post_id": "post-1",
                    "media_id": "video-1",
                }
            ]
        }
        with patch.dict(
            os.environ, {"FB_STORY_CONFIRM_TIMEOUT_SECONDS": "0"}, clear=True
        ), patch.object(publicar_stories, "graph_get", return_value=resposta):
            story = publicar_stories.aguardar_story_facebook(
                "pagina",
                "token",
                "post-1",
                "video-1",
                "2026-09-08T09:00:00-03:00",
            )
        self.assertEqual(story["status"], "ARCHIVED")

    def test_facebook_nao_confirma_story_ausente_ou_midia_divergente(self):
        resposta = {
            "data": [
                {
                    "status": "PUBLISHED",
                    "post_id": "outro-post",
                    "media_id": "outro-video",
                }
            ]
        }
        with patch.dict(
            os.environ, {"FB_STORY_CONFIRM_TIMEOUT_SECONDS": "0"}, clear=True
        ), patch.object(publicar_stories, "graph_get", return_value=resposta):
            with self.assertRaises(publicar_stories.PublicacaoFacebookIncerta):
                publicar_stories.aguardar_story_facebook(
                    "pagina",
                    "token",
                    "post-1",
                    "video-1",
                    "2026-09-08T09:00:00-03:00",
                )

    def test_facebook_nao_casa_ids_vazios_com_story_alheio(self):
        resposta = {
            "data": [
                {
                    "status": "PUBLISHED",
                    "media_id": "outro-video",
                }
            ]
        }
        with patch.dict(
            os.environ, {"FB_STORY_CONFIRM_TIMEOUT_SECONDS": "0"}, clear=True
        ), patch.object(publicar_stories, "graph_get", return_value=resposta):
            with self.assertRaises(publicar_stories.PublicacaoFacebookIncerta):
                publicar_stories.aguardar_story_facebook(
                    "pagina",
                    "token",
                    "",
                    "video-esperado",
                    "2026-09-08T09:00:00-03:00",
                )

    def test_facebook_rejeita_candidato_com_um_id_correto_e_outro_divergente(self):
        resposta = {
            "data": [
                {
                    "status": "PUBLISHED",
                    "post_id": "post-esperado",
                    "media_id": "outro-video",
                }
            ]
        }
        with patch.dict(
            os.environ, {"FB_STORY_CONFIRM_TIMEOUT_SECONDS": "0"}, clear=True
        ), patch.object(publicar_stories, "graph_get", return_value=resposta):
            with self.assertRaises(publicar_stories.PublicacaoFacebookIncerta):
                publicar_stories.aguardar_story_facebook(
                    "pagina",
                    "token",
                    "post-esperado",
                    "video-esperado",
                    "2026-09-08T09:00:00-03:00",
                )

    def test_confirmacao_facebook_percorre_paginacao(self):
        paginas = [
            {
                "data": [],
                "paging": {
                    "next": "https://graph.test/proxima",
                    "cursors": {"after": "cursor-2"},
                },
            },
            {
                "data": [
                    {
                        "status": "PUBLISHED",
                        "post_id": "post-1",
                        "media_id": "video-1",
                    }
                ]
            },
        ]
        with patch.dict(
            os.environ, {"FB_STORY_CONFIRM_TIMEOUT_SECONDS": "0"}, clear=True
        ), patch.object(
            publicar_stories, "graph_get", side_effect=paginas
        ) as consultar:
            story = publicar_stories.aguardar_story_facebook(
                "pagina",
                "token",
                "post-1",
                "video-1",
                "2026-09-08T09:00:00-03:00",
            )

        self.assertEqual(story["post_id"], "post-1")
        self.assertEqual(consultar.call_args_list[1].args[1]["after"], "cursor-2")

    def test_instagram_usa_media_type_stories(self):
        item = parte(1)
        with tempfile.TemporaryDirectory() as temporario:
            cache = Path(temporario) / "parte.mp4"
            cache.write_bytes(b"video-validado")
            with patch.object(
                publicar_stories,
                "obrigatoria",
                side_effect=lambda nome: {"IG_ACCESS_TOKEN": "token", "IG_BUSINESS_ID": "ig"}[nome],
            ):
                with patch.object(publicar_stories, "baixar_midia", return_value=cache) as baixar:
                    with patch.object(
                        publicar_stories,
                        "graph_post",
                        side_effect=[{"id": "container"}, {"id": "story-ig"}],
                    ) as graph_post_mock:
                        with patch.object(
                            publicar_stories, "aguardar_container_instagram"
                        ) as aguardar:
                            resultado = publicar_stories.publicar_instagram(item)

            self.assertFalse(cache.exists())
            baixar.assert_called_once_with(item["midia"])

        self.assertEqual(resultado, "story-ig")
        self.assertEqual(graph_post_mock.call_args_list[0].args[0], "ig/media")
        self.assertEqual(graph_post_mock.call_args_list[0].args[1]["media_type"], "STORIES")
        aguardar.assert_called_once_with("container", "token")

    def test_instagram_resposta_incerta_preserva_container_e_nao_republica(self):
        item = parte(1)
        with tempfile.TemporaryDirectory() as temporario:
            cache = Path(temporario) / "parte.mp4"
            cache.write_bytes(b"video-validado")
            with patch.object(
                publicar_stories,
                "obrigatoria",
                side_effect=lambda nome: {
                    "IG_ACCESS_TOKEN": "token",
                    "IG_BUSINESS_ID": "ig",
                }[nome],
            ), patch.object(
                publicar_stories, "baixar_midia", return_value=cache
            ), patch.object(
                publicar_stories,
                "graph_post",
                side_effect=[{"id": "container-incerto"}, RuntimeError("timeout")],
            ), patch.object(publicar_stories, "aguardar_container_instagram"):
                resultado = publicar_stories.executar_rede(
                    item, "instagram", publicar_stories.publicar_instagram
                )

        self.assertIsNone(resultado)
        self.assertEqual(item["instagram"]["status"], "incerto")
        self.assertEqual(item["instagram"]["container_id"], "container-incerto")
        self.assertIn("publish_iniciado_em", item["instagram"])
        publicar_stories.validar_fila(
            {"pacotes": [{**pacote("2026-09-08"), "partes": [item]}]}
        )

        with patch.object(publicar_stories, "obrigatoria", return_value="x"), patch.object(
            publicar_stories, "graph_post"
        ) as publicar_novamente, patch.object(
            publicar_stories, "baixar_midia"
        ) as baixar_novamente:
            with self.assertRaises(publicar_stories.PublicacaoInstagramIncerta):
                publicar_stories.publicar_instagram(item)
        publicar_novamente.assert_not_called()
        baixar_novamente.assert_not_called()

    def test_instagram_descarta_container_terminal_antes_do_publish(self):
        item = parte(1)
        item["instagram"]["container_id"] = "container-morto"
        erro_terminal = publicar_stories.ContainerInstagramTerminal(
            "container-morto", {"status_code": "ERROR", "status": "codec"}
        )
        with patch.object(
            publicar_stories,
            "obrigatoria",
            side_effect=lambda nome: {
                "IG_ACCESS_TOKEN": "token",
                "IG_BUSINESS_ID": "ig",
            }[nome],
        ), patch.object(
            publicar_stories,
            "aguardar_container_instagram",
            side_effect=erro_terminal,
        ), patch.object(publicar_stories, "graph_post") as publicar:
            with self.assertRaisesRegex(RuntimeError, "descartado"):
                publicar_stories.publicar_instagram(item)

        publicar.assert_not_called()
        self.assertNotIn("container_id", item["instagram"])
        self.assertEqual(
            item["instagram"]["container_terminal"]["id"], "container-morto"
        )

    def test_facebook_usa_video_stories_e_remove_cache(self):
        item = parte(1)
        resposta_upload = Mock(ok=True, status_code=200, text="ok")
        with tempfile.TemporaryDirectory() as temporario:
            cache = Path(temporario) / "parte.mp4"
            cache.write_bytes(b"video")
            with patch.object(
                publicar_stories,
                "obrigatoria",
                side_effect=lambda nome: {
                    "FB_PAGE_ACCESS_TOKEN": "token-sistema",
                    "FB_PAGE_ID": "pagina",
                }[nome],
            ):
                with patch.object(
                    publicar_stories,
                    "graph_get",
                    return_value={"access_token": "token-pagina"},
                ):
                    with patch.object(
                        publicar_stories,
                        "graph_post",
                        side_effect=[
                            {"video_id": "video-1", "upload_url": "https://upload.test"},
                            {"success": True, "post_id": "story-fb"},
                        ],
                    ) as graph_post_mock:
                        with patch.object(publicar_stories, "baixar_midia", return_value=cache):
                            with patch.object(
                                publicar_stories.requests,
                                "post",
                                return_value=resposta_upload,
                            ), patch.object(
                                publicar_stories,
                                "aguardar_story_facebook",
                                return_value={
                                    "status": "PUBLISHED",
                                    "post_id": "story-fb",
                                    "media_id": "video-1",
                                    "url": "https://facebook.test/story-fb",
                                },
                            ) as aguardar:
                                resultado = publicar_stories.publicar_facebook(item)

            self.assertFalse(cache.exists())

        self.assertEqual(resultado, "story-fb")
        self.assertEqual(
            [chamada.args[0] for chamada in graph_post_mock.call_args_list],
            ["pagina/video_stories", "pagina/video_stories"],
        )
        aguardar.assert_called_once()
        self.assertEqual(item["facebook"]["video_id"], "video-1")
        self.assertEqual(item["facebook"]["url"], "https://facebook.test/story-fb")

    def test_facebook_incerto_reconcilia_ids_sem_novo_upload(self):
        item = parte(1)
        item["facebook"].update(
            {
                "status": "incerto",
                "video_id": "video-anterior",
                "post_id_candidato": "post-anterior",
                "finish_em": "2026-09-08T09:00:00-03:00",
            }
        )
        with patch.object(
            publicar_stories,
            "obrigatoria",
            side_effect=lambda nome: {
                "FB_PAGE_ACCESS_TOKEN": "token-sistema",
                "FB_PAGE_ID": "pagina",
            }[nome],
        ), patch.object(
            publicar_stories,
            "graph_get",
            return_value={"access_token": "token-pagina"},
        ), patch.object(
            publicar_stories,
            "aguardar_story_facebook",
            return_value={
                "status": "PUBLISHED",
                "post_id": "post-anterior",
                "media_id": "video-anterior",
                "url": "https://facebook.test/post-anterior",
            },
        ), patch.object(publicar_stories, "baixar_midia") as baixar, patch.object(
            publicar_stories, "graph_post"
        ) as graph_post_mock:
            resultado = publicar_stories.publicar_facebook(item)

        self.assertEqual(resultado, "post-anterior")
        baixar.assert_not_called()
        graph_post_mock.assert_not_called()

    def test_resposta_perdida_no_finish_reconcilia_video_sem_reupload(self):
        item = parte(1)
        resposta_upload = Mock(ok=True, status_code=200, text="ok")
        segredos = {
            "FB_PAGE_ACCESS_TOKEN": "token-sistema",
            "FB_PAGE_ID": "pagina",
        }
        with tempfile.TemporaryDirectory() as temporario:
            cache = Path(temporario) / "parte.mp4"
            cache.write_bytes(b"video")
            with patch.object(
                publicar_stories, "obrigatoria", side_effect=lambda nome: segredos[nome]
            ), patch.object(
                publicar_stories,
                "graph_get",
                return_value={"access_token": "token-pagina"},
            ), patch.object(
                publicar_stories,
                "graph_post",
                side_effect=[
                    {"video_id": "video-perdido", "upload_url": "https://upload.test"},
                    RuntimeError("timeout no finish"),
                ],
            ), patch.object(
                publicar_stories, "baixar_midia", return_value=cache
            ), patch.object(
                publicar_stories.requests, "post", return_value=resposta_upload
            ):
                resultado = publicar_stories.executar_rede(
                    item, "facebook", publicar_stories.publicar_facebook
                )

            self.assertFalse(cache.exists())
        self.assertIsNone(resultado)
        self.assertEqual(item["facebook"]["status"], "incerto")
        self.assertEqual(item["facebook"]["video_id"], "video-perdido")
        self.assertNotIn("post_id_candidato", item["facebook"])
        self.assertIn("finish_iniciado_em", item["facebook"])

        with patch.object(
            publicar_stories, "obrigatoria", side_effect=lambda nome: segredos[nome]
        ), patch.object(
            publicar_stories,
            "graph_get",
            return_value={"access_token": "token-pagina"},
        ), patch.object(
            publicar_stories,
            "aguardar_story_facebook",
            return_value={
                "status": "PUBLISHED",
                "post_id": "post-recuperado",
                "media_id": "video-perdido",
            },
        ), patch.object(publicar_stories, "baixar_midia") as baixar, patch.object(
            publicar_stories, "graph_post"
        ) as graph_post_mock:
            recuperado = publicar_stories.publicar_facebook(item)

        self.assertEqual(recuperado, "post-recuperado")
        baixar.assert_not_called()
        graph_post_mock.assert_not_called()

    def test_timeout_facebook_fica_incerto_e_nao_perde_ids(self):
        item = parte(1)
        item["facebook"].update(
            {
                "video_id": "video-1",
                "post_id_candidato": "post-1",
                "finish_em": "2026-09-08T09:00:00-03:00",
            }
        )

        def incerto(_parte):
            raise publicar_stories.PublicacaoFacebookIncerta("ainda não apareceu")

        resultado = publicar_stories.executar_rede(item, "facebook", incerto)

        self.assertIsNone(resultado)
        self.assertEqual(item["facebook"]["status"], "incerto")
        self.assertEqual(item["facebook"]["video_id"], "video-1")
        publicar_stories.validar_fila(
            {"pacotes": [{**pacote("2026-09-08"), "partes": [item]}]}
        )

    def test_finish_pode_ser_repetido_uma_vez_no_mesmo_video_sem_reupload(self):
        item = parte(1)
        item["facebook"].update(
            {
                "status": "incerto",
                "video_id": "video-existente",
                "finish_tentativas": 1,
                "finish_iniciado_em": "2026-09-08T09:00:00-03:00",
            }
        )
        segredos = {
            "FB_PAGE_ACCESS_TOKEN": "token-sistema",
            "FB_PAGE_ID": "pagina",
        }
        with patch.object(
            publicar_stories, "obrigatoria", side_effect=lambda nome: segredos[nome]
        ), patch.object(
            publicar_stories,
            "graph_get",
            return_value={"access_token": "token-pagina"},
        ), patch.object(
            publicar_stories,
            "aguardar_story_facebook",
            side_effect=[
                publicar_stories.PublicacaoFacebookIncerta("finish não chegou"),
                {
                    "status": "PUBLISHED",
                    "post_id": "post-recuperado",
                    "media_id": "video-existente",
                },
            ],
        ) as aguardar, patch.object(
            publicar_stories,
            "graph_post",
            return_value={"success": True, "post_id": "post-recuperado"},
        ) as finalizar, patch.object(publicar_stories, "baixar_midia") as baixar:
            resultado = publicar_stories.publicar_facebook(item)

        self.assertEqual(resultado, "post-recuperado")
        self.assertEqual(item["facebook"]["finish_tentativas"], 2)
        self.assertEqual(aguardar.call_count, 2)
        baixar.assert_not_called()
        finalizar.assert_called_once()
        self.assertEqual(finalizar.call_args.args[1]["video_id"], "video-existente")


class ResultadoExecucaoTest(unittest.TestCase):
    def setUp(self):
        self.agora = datetime(2026, 9, 10, 12, 0, tzinfo=publicar_stories.BRT)

    def processar(self, fila):
        ambiente = {"MAX_PACOTES_POR_EXECUCAO": "1"}
        with patch.dict(os.environ, ambiente, clear=True):
            with patch.object(publicar_stories, "salvar_fila") as salvar, patch.object(
                publicar_stories, "validar_contas_meta"
            ), patch.object(
                publicar_stories, "validar_midias_antes_de_publicar"
            ):
                with redirect_stdout(io.StringIO()):
                    relatorio = publicar_stories.processar_fila(fila, agora=self.agora)
        return relatorio, salvar

    def test_informa_que_nada_foi_publicado_quando_nao_ha_story_devido(self):
        fila = {"pacotes": [pacote("2026-09-11")]}
        relatorio, salvar = self.processar(fila)
        self.assertEqual(relatorio["resultado"], publicar_stories.RESULTADO_NENHUM_DEVIDO)
        self.assertEqual(relatorio["publicacoes"], [])
        self.assertEqual(relatorio["proximo"]["data"], "2026-09-11")
        salvar.assert_not_called()

    def test_publica_partes_em_ordem_e_persiste_apos_cada_rede(self):
        story = pacote("2026-09-09", quantidade_partes=2)
        fila = {"pacotes": [story]}
        with patch.object(
            publicar_stories,
            "publicar_instagram",
            side_effect=lambda item: f"ig-{item['ordem']}",
        ) as instagram:
            with patch.object(
                publicar_stories,
                "publicar_facebook",
                side_effect=lambda item: f"fb-{item['ordem']}",
            ) as facebook:
                relatorio, salvar = self.processar(fila)

        self.assertEqual(relatorio["resultado"], publicar_stories.RESULTADO_PUBLICADO)
        self.assertEqual(relatorio["pacotes_concluidos"], 1)
        self.assertEqual(relatorio["partes_concluidas"], 2)
        self.assertEqual([chamada.args[0]["ordem"] for chamada in instagram.call_args_list], [1, 2])
        self.assertEqual([chamada.args[0]["ordem"] for chamada in facebook.call_args_list], [1, 2])
        self.assertEqual(salvar.call_count, 7)
        self.assertEqual(story["status"], "concluido")
        self.assertTrue(all(item["status"] == "concluido" for item in story["partes"]))

    def test_nao_repete_rede_que_ja_confirmou(self):
        story = pacote("2026-09-09")
        story["partes"][0]["instagram"].update(
            {"status": "publicado", "id": "ig-anterior"}
        )
        with patch.object(publicar_stories, "publicar_instagram") as instagram:
            with patch.object(
                publicar_stories,
                "publicar_facebook",
                return_value="fb-novo",
            ) as facebook:
                relatorio, _ = self.processar({"pacotes": [story]})

        instagram.assert_not_called()
        facebook.assert_called_once()
        self.assertEqual(relatorio["resultado"], publicar_stories.RESULTADO_PUBLICADO)
        self.assertEqual(
            relatorio["publicacoes"],
            [
                {
                    "pacote": "story-2026-09-09",
                    "parte": 1,
                    "rede": "Facebook",
                    "id": "fb-novo",
                }
            ],
        )

    def test_falha_em_uma_rede_persiste_a_outra_e_para_proximo_pacote(self):
        primeiro = pacote("2026-09-08")
        segundo = pacote("2026-09-09")
        fila = {"pacotes": [segundo, primeiro]}
        with patch.object(
            publicar_stories,
            "publicar_instagram",
            side_effect=RuntimeError("Meta recusou"),
        ) as instagram:
            with patch.object(
                publicar_stories,
                "publicar_facebook",
                return_value="fb-confirmado",
            ) as facebook:
                relatorio, salvar = self.processar(fila)

        self.assertEqual(relatorio["resultado"], publicar_stories.RESULTADO_FALHA)
        self.assertEqual(relatorio["pacotes_concluidos"], 0)
        self.assertEqual(len(relatorio["publicacoes"]), 1)
        self.assertIn("instagram: Meta recusou", relatorio["erros"][0])
        instagram.assert_called_once()
        facebook.assert_called_once()
        self.assertEqual(salvar.call_count, 3)
        self.assertEqual(segundo["status"], "pendente")

    def test_reconcilia_confirmacoes_existentes_sem_nova_publicacao(self):
        story = pacote("2026-09-09")
        story["partes"][0]["instagram"].update({"status": "publicado", "id": "ig"})
        story["partes"][0]["facebook"].update({"status": "publicado", "id": "fb"})
        with patch.object(publicar_stories, "publicar_instagram") as instagram:
            with patch.object(publicar_stories, "publicar_facebook") as facebook:
                relatorio, _ = self.processar({"pacotes": [story]})

        instagram.assert_not_called()
        facebook.assert_not_called()
        self.assertEqual(relatorio["resultado"], publicar_stories.RESULTADO_RECONCILIADO)
        self.assertEqual(relatorio["publicacoes"], [])
        self.assertEqual(story["status"], "concluido")

    def test_main_retorna_1_para_falha_e_zero_nos_demais_resultados(self):
        casos = [
            (publicar_stories.RESULTADO_NENHUM_DEVIDO, 0),
            (publicar_stories.RESULTADO_PUBLICADO, 0),
            (publicar_stories.RESULTADO_RECONCILIADO, 0),
            (publicar_stories.RESULTADO_DIAGNOSTICO, 0),
            (publicar_stories.RESULTADO_FALHA, 1),
        ]
        for resultado, esperado in casos:
            relatorio = {
                "resultado": resultado,
                "pacotes_devidos": 0,
                "pacotes_concluidos": 0,
                "partes_concluidas": 0,
                "publicacoes": [],
                "erros": [],
                "proximo": None,
            }
            with self.subTest(resultado=resultado):
                with patch.object(publicar_stories, "FILA_FILE") as fila_file:
                    fila_file.read_text.return_value = '{"pacotes": []}'
                    with patch.object(
                        publicar_stories,
                        "processar_fila",
                        return_value=relatorio,
                    ):
                        with patch.object(publicar_stories, "registrar_resultado"):
                            self.assertEqual(publicar_stories.main(), esperado)

    def test_modo_diagnostico_nao_processa_fila_mesmo_com_pacote_devido(self):
        fila = {"pacotes": [pacote("2026-09-01")]}
        contas = {
            "instagram": {"vinculo": "instagram_business_account"},
            "facebook": {"id": "pagina"},
        }
        with patch.dict(
            os.environ, {"DIAGNOSTICAR_CONTAS_META": "true"}, clear=True
        ), patch.object(publicar_stories, "FILA_FILE") as fila_file, patch.object(
            publicar_stories, "validar_contas_meta", return_value=contas
        ), patch.object(publicar_stories, "processar_fila") as processar, patch.object(
            publicar_stories, "registrar_resultado"
        ) as registrar:
            fila_file.read_text.return_value = json.dumps(fila)
            codigo = publicar_stories.main()

        self.assertEqual(codigo, 0)
        processar.assert_not_called()
        self.assertEqual(
            registrar.call_args.args[0]["resultado"],
            publicar_stories.RESULTADO_DIAGNOSTICO,
        )

    def test_grava_github_output_e_summary_inequivocos(self):
        relatorio = {
            "resultado": publicar_stories.RESULTADO_NENHUM_DEVIDO,
            "pacotes_devidos": 0,
            "pacotes_concluidos": 0,
            "partes_concluidas": 0,
            "publicacoes": [],
            "erros": [],
            "proximo": pacote("2026-09-11"),
        }
        with tempfile.TemporaryDirectory() as temporario:
            output = Path(temporario) / "output.txt"
            summary = Path(temporario) / "summary.md"
            with patch.dict(
                os.environ,
                {
                    "GITHUB_OUTPUT": str(output),
                    "GITHUB_STEP_SUMMARY": str(summary),
                },
                clear=True,
            ):
                with redirect_stdout(io.StringIO()):
                    publicar_stories.registrar_resultado(relatorio)
            saidas = output.read_text(encoding="utf-8")
            resumo = summary.read_text(encoding="utf-8")

        self.assertIn("resultado=NENHUM_STORY_DEVIDO", saidas)
        self.assertIn("instagram_publicados=0", saidas)
        self.assertIn("NENHUM STORY DEVIDO — NADA FOI PUBLICADO", resumo)
        self.assertIn("2026-09-11 09:00", resumo)


if __name__ == "__main__":
    unittest.main()
