"""Publica pacotes vencidos de Stories no Instagram e Facebook.

Cada pacote representa um Story diário das 09:00 (horário de Brasília) e pode
conter várias partes sequenciais de até 59 segundos. O estado é persistido após
cada tentativa em cada rede, para que uma repetição nunca publique novamente a
rede que já foi confirmada pela Meta.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests

from publicar import (
    BRT,
    PLATAFORMAS,
    baixar_midia,
    graph_get,
    graph_post,
    obrigatoria,
)

ROOT = Path(__file__).resolve().parent
FILA_FILE = ROOT / "fila" / "fila-stories.json"
HORARIO_STORY = "09:00"
LIMITE_PARTE_SEGUNDOS = 59.0
DURACAO_MINIMA_PARTE_SEGUNDOS = 3.0
TAMANHO_MAXIMO_PARTE_BYTES = 100_000_000
MAX_PACOTES_POR_EXECUCAO_PADRAO = 1
MAX_PARTES_POR_PACOTE = 10

RESULTADO_PUBLICADO = "PUBLICADO"
RESULTADO_NENHUM_DEVIDO = "NENHUM_STORY_DEVIDO"
RESULTADO_RECONCILIADO = "RECONCILIADO_SEM_NOVA_PUBLICACAO"
RESULTADO_DIAGNOSTICO = "DIAGNOSTICO_META_OK"
RESULTADO_FALHA = "FALHA"
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
STATUS_ITEM = {"pendente", "concluido"}
STATUS_REDE = {"pendente", "erro", "incerto", "publicado"}


class PublicacaoFacebookIncerta(RuntimeError):
    """O finish foi aceito, mas o Story ainda não apareceu como publicado."""


class PublicacaoInstagramIncerta(RuntimeError):
    """O media_publish pode ter sido aceito, mas a resposta não o comprovou."""


class ContainerInstagramTerminal(RuntimeError):
    """O container falhou antes de qualquer chamada irreversível de publish."""

    def __init__(self, container_id: str, status: dict):
        self.container_id = container_id
        self.status = status
        super().__init__(f"Container Instagram terminal: {status}")


def aguardar_container_instagram(container_id: str, token: str) -> None:
    """Aguarda o processamento e distingue falha terminal de falha transitória."""
    for tentativa in range(36):
        status = graph_get(
            container_id,
            {"fields": "status_code,status", "access_token": token},
        )
        codigo = str(status.get("status_code", "")).upper()
        print(f"Instagram Story [{tentativa + 1}/36]: {codigo}")
        if codigo == "FINISHED":
            return
        if codigo in {"ERROR", "EXPIRED"}:
            raise ContainerInstagramTerminal(container_id, status)
        time.sleep(10)
    raise TimeoutError("Instagram demorou mais de seis minutos para processar o Story.")


def validar_contas_meta() -> dict:
    """Confirma a conta BUSINESS do Instagram e o token da Página."""
    ig_token = obrigatoria("IG_ACCESS_TOKEN")
    ig_id = obrigatoria("IG_BUSINESS_ID")
    instagram = graph_get(
        ig_id,
        {"fields": "id,username,account_type", "access_token": ig_token},
    )
    tipo = str(instagram.get("account_type", "")).upper()
    if tipo != "BUSINESS":
        raise RuntimeError(
            "Stories via Instagram API exigem conta BUSINESS; "
            f"a Meta informou {tipo or 'tipo ausente'}."
        )
    if str(instagram.get("id", "")) != str(ig_id):
        raise RuntimeError("A Meta retornou uma conta Instagram diferente da configurada.")
    username = str(instagram.get("username", "")).strip().lstrip("@").casefold()
    username_esperado = (
        os.getenv("IG_EXPECTED_USERNAME", "codigodavirada_br")
        .strip()
        .lstrip("@")
        .casefold()
    )
    if not username_esperado or username != username_esperado:
        raise RuntimeError(
            "A conta Instagram configurada não é a conta esperada: "
            f"@{username or 'ausente'} (esperado @{username_esperado or 'ausente'})."
        )

    fb_token_sistema = obrigatoria("FB_PAGE_ACCESS_TOKEN")
    page_id = obrigatoria("FB_PAGE_ID")
    facebook = graph_get(
        page_id,
        {
            "fields": "id,name,access_token,instagram_business_account{id,username}",
            "access_token": fb_token_sistema,
        },
    )
    if str(facebook.get("id", "")) != str(page_id) or not facebook.get(
        "access_token"
    ):
        raise RuntimeError(
            "A Meta não confirmou a Página ou o Page Access Token configurado."
        )
    conta_vinculada = facebook.get("instagram_business_account") or {}
    if str(conta_vinculada.get("id", "")) != str(ig_id):
        raise RuntimeError(
            "A Página Facebook não está vinculada ao IG_BUSINESS_ID "
            "configurado; publicação cruzada foi bloqueada."
        )
    page_id_esperado = os.getenv("FB_EXPECTED_PAGE_ID", "").strip()
    if page_id_esperado and str(facebook.get("id", "")) != page_id_esperado:
        raise RuntimeError(
            "A Página Facebook configurada não é a esperada: "
            f"{facebook.get('id', 'ausente')} (esperado {page_id_esperado})."
        )
    resultado = {
        "instagram": {
            "id": str(instagram["id"]),
            "username": str(instagram.get("username", "")),
            "account_type": tipo,
        },
        "facebook": {
            "id": str(facebook["id"]),
            "name": str(facebook.get("name", "")),
            "instagram_vinculado_id": str(conta_vinculada["id"]),
        },
    }
    print(
        "Contas Meta validadas: "
        f"Instagram @{resultado['instagram']['username']} ({tipo}); "
        f"Facebook {resultado['facebook']['name']} ({resultado['facebook']['id']})."
    )
    return resultado


def salvar_fila(fila: dict) -> None:
    validar_fila(fila)
    temporario = FILA_FILE.with_name(f".{FILA_FILE.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporario.open("w", encoding="utf-8", newline="\n") as arquivo:
            arquivo.write(json.dumps(fila, ensure_ascii=False, indent=2) + "\n")
            arquivo.flush()
            os.fsync(arquivo.fileno())
        os.replace(temporario, FILA_FILE)
    finally:
        temporario.unlink(missing_ok=True)


def publicar_instagram(parte: dict) -> str:
    token = obrigatoria("IG_ACCESS_TOKEN")
    ig_id = obrigatoria("IG_BUSINESS_ID")
    dados_instagram = parte["instagram"]
    container_id = str(dados_instagram.get("container_id", "")).strip()
    if dados_instagram.get("publish_iniciado_em"):
        raise PublicacaoInstagramIncerta(
            "O media_publish do Instagram teve resposta incerta. O container "
            f"{container_id or 'sem ID'} foi preservado e não será republicado "
            "automaticamente; confirme o Story na conta antes de intervir."
        )
    if not container_id:
        # O Instagram busca a URL diretamente. Antes de entregar essa URL à
        # Meta, baixe e confira SHA/tamanho para não publicar asset divergente.
        caminho_validado = baixar_midia(parte["midia"])
        caminho_validado.unlink(missing_ok=True)
        container = graph_post(
            f"{ig_id}/media",
            {
                "media_type": "STORIES",
                "video_url": parte["midia"]["url_publica"],
                "access_token": token,
            },
        )
        container_id = str(container.get("id", "")).strip()
        if not container_id:
            raise RuntimeError(f"Container do Story do Instagram sem ID: {container}")
        dados_instagram["container_id"] = container_id
    try:
        aguardar_container_instagram(container_id, token)
    except ContainerInstagramTerminal as erro:
        # Nenhum media_publish foi chamado. Esse container não pode voltar à
        # vida, portanto é seguro descartá-lo e criar outro na próxima rodada.
        dados_instagram["container_terminal"] = {
            "id": erro.container_id,
            "status": str(erro.status.get("status_code", "")).upper(),
            "detectado_em": datetime.now(BRT).isoformat(timespec="seconds"),
        }
        dados_instagram.pop("container_id", None)
        dados_instagram.pop("publish_iniciado_em", None)
        raise RuntimeError(
            "O container do Instagram terminou em ERROR/EXPIRED antes do publish; "
            "ele foi descartado e um novo container poderá ser criado na próxima execução."
        ) from erro
    dados_instagram["publish_iniciado_em"] = datetime.now(BRT).isoformat(
        timespec="seconds"
    )
    try:
        publicado = graph_post(
            f"{ig_id}/media_publish",
            {"creation_id": container_id, "access_token": token},
        )
    except Exception as erro:
        raise PublicacaoInstagramIncerta(
            "A resposta do media_publish do Instagram não comprovou se o Story "
            f"foi publicado (container_id={container_id}): {erro}"
        ) from erro
    if not publicado.get("id"):
        raise PublicacaoInstagramIncerta(
            "Instagram não retornou o ID do Story após media_publish; "
            f"container_id={container_id}: {publicado}"
        )
    return str(publicado["id"])


def validar_midias_antes_de_publicar(pacotes: list[dict]) -> None:
    """Baixa e confere todas as partes antes de publicar a primeira."""
    validadas: list[Path] = []
    try:
        for pacote in pacotes:
            for parte in pacote["partes"]:
                validadas.append(baixar_midia(parte["midia"]))
    finally:
        for caminho in validadas:
            caminho.unlink(missing_ok=True)


def aguardar_story_facebook(
    page_id: str,
    token: str,
    post_id: str,
    video_id: str,
    desde_iso: str,
) -> dict:
    if not str(post_id).strip() and not str(video_id).strip():
        raise PublicacaoFacebookIncerta(
            "A reconciliação do Facebook exige post_id ou video_id."
        )
    try:
        timeout = int(os.getenv("FB_STORY_CONFIRM_TIMEOUT_SECONDS", "300"))
    except ValueError as erro:
        raise RuntimeError(
            "FB_STORY_CONFIRM_TIMEOUT_SECONDS deve ser um número inteiro."
        ) from erro
    if timeout < 0 or timeout > 600:
        raise RuntimeError(
            "FB_STORY_CONFIRM_TIMEOUT_SECONDS deve ficar entre 0 e 600."
        )
    try:
        desde = int(datetime.fromisoformat(desde_iso).timestamp()) - 120
    except (TypeError, ValueError):
        desde = int(datetime.now(BRT).timestamp()) - 600

    inicio = time.monotonic()
    espera = 5
    while True:
        cursor: str | None = None
        cursores_vistos: set[str] = set()
        candidatos: list[dict] = []
        for _ in range(10):
            parametros = {
                "fields": "creation_time,media_id,media_type,post_id,status,url",
                "status": json.dumps(["PUBLISHED", "ARCHIVED"]),
                "since": str(desde),
                "limit": "100",
                "access_token": token,
            }
            if cursor:
                parametros["after"] = cursor
            resposta = graph_get(f"{page_id}/stories", parametros)
            for story in resposta.get("data", []):
                if str(story.get("status", "")).upper() not in {
                    "PUBLISHED",
                    "ARCHIVED",
                }:
                    continue
                post_retornado = str(story.get("post_id", "")).strip()
                media_retornada = str(story.get("media_id", "")).strip()
                post_confere = bool(post_id) and post_retornado == str(post_id)
                media_confere = bool(video_id) and media_retornada == str(video_id)
                post_contradiz = bool(post_id and post_retornado) and not post_confere
                media_contradiz = (
                    bool(video_id and media_retornada) and not media_confere
                )
                if (post_confere or media_confere) and not (
                    post_contradiz or media_contradiz
                ):
                    candidatos.append(story)
            paging = resposta.get("paging", {})
            proximo = str(paging.get("next", "")).strip()
            novo_cursor = str(paging.get("cursors", {}).get("after", "")).strip()
            if not proximo or not novo_cursor or novo_cursor in cursores_vistos:
                break
            cursores_vistos.add(novo_cursor)
            cursor = novo_cursor

        identidades = {
            (
                str(story.get("post_id", "")),
                str(story.get("media_id", "")),
            )
            for story in candidatos
        }
        if len(identidades) == 1:
            return candidatos[0]
        if len(identidades) > 1:
            raise PublicacaoFacebookIncerta(
                "A Página retornou mais de um Story compatível com os IDs do finish; "
                "a reconciliação foi mantida como incerta para evitar falso positivo."
            )

        decorrido = time.monotonic() - inicio
        if decorrido >= timeout:
            break
        pausa = min(espera, max(0.0, timeout - decorrido))
        time.sleep(pausa)
        espera = min(30, espera * 2)

    raise PublicacaoFacebookIncerta(
        "O Facebook aceitou o finish, mas o Story ainda não apareceu como "
        f"PUBLISHED/ARCHIVED na Página (post_id={post_id}, video_id={video_id}). "
        "A próxima execução reconciliará esses IDs e, no máximo uma vez, "
        "poderá repetir o finish sobre o mesmo video_id; nunca fará novo upload."
    )


def finalizar_story_facebook(
    page_id: str,
    token: str,
    dados_facebook: dict,
    video_id: str,
) -> str:
    """Executa o finish e preserva evidência mesmo se a resposta se perder."""
    tentativas = int(dados_facebook.get("finish_tentativas", 0) or 0) + 1
    instante = datetime.now(BRT).isoformat(timespec="seconds")
    dados_facebook.update(
        {
            "video_id": str(video_id),
            "finish_tentativas": tentativas,
            "finish_iniciado_em": instante,
        }
    )
    try:
        fim = graph_post(
            f"{page_id}/video_stories",
            {
                "upload_phase": "finish",
                "video_id": video_id,
                "access_token": token,
            },
        )
    except Exception as erro:
        raise PublicacaoFacebookIncerta(
            "A resposta do finish do Facebook não chegou; o video_id foi "
            f"preservado sem novo upload (tentativa {tentativas}): {erro}"
        ) from erro
    if not fim.get("success") or not fim.get("post_id"):
        raise PublicacaoFacebookIncerta(
            "O finish não trouxe confirmação conclusiva; o video_id foi "
            f"preservado sem novo upload (tentativa {tentativas}): {fim}"
        )
    post_id = str(fim["post_id"])
    dados_facebook.update(
        {
            "post_id_candidato": post_id,
            "finish_em": datetime.now(BRT).isoformat(timespec="seconds"),
        }
    )
    return post_id


def publicar_facebook(parte: dict) -> str:
    token_sistema = obrigatoria("FB_PAGE_ACCESS_TOKEN")
    page_id = obrigatoria("FB_PAGE_ID")
    token = graph_get(
        page_id,
        {"fields": "access_token", "access_token": token_sistema},
    ).get("access_token")
    if not token:
        raise RuntimeError("A Meta não retornou o token de acesso da Página.")

    dados_facebook = parte["facebook"]
    video_anterior = str(dados_facebook.get("video_id", "")).strip()
    post_anterior = str(dados_facebook.get("post_id_candidato", "")).strip()
    finish_anterior = str(
        dados_facebook.get("finish_em")
        or dados_facebook.get("finish_iniciado_em")
        or ""
    ).strip()
    if video_anterior and finish_anterior:
        try:
            story = aguardar_story_facebook(
                page_id,
                token,
                post_anterior,
                video_anterior,
                finish_anterior,
            )
        except PublicacaoFacebookIncerta:
            # Se o primeiro finish não chegou à Meta, repeti-lo uma única vez
            # sobre o MESMO video_id é recuperável e não cria novo upload.
            tentativas = int(dados_facebook.get("finish_tentativas", 1) or 1)
            if post_anterior or tentativas >= 2:
                raise
            post_anterior = finalizar_story_facebook(
                page_id, token, dados_facebook, video_anterior
            )
            finish_anterior = str(dados_facebook["finish_em"])
            story = aguardar_story_facebook(
                page_id,
                token,
                post_anterior,
                video_anterior,
                finish_anterior,
            )
        post_confirmado = str(
            story.get("post_id")
            or post_anterior
            or story.get("media_id")
            or video_anterior
        ).strip()
        if not post_confirmado:
            raise PublicacaoFacebookIncerta(
                "O Story foi localizado como PUBLISHED/ARCHIVED, mas a Meta não devolveu "
                "post_id nem media_id."
            )
        dados_facebook["post_id_candidato"] = post_confirmado
        dados_facebook["id_tipo"] = (
            "post_id" if story.get("post_id") or post_anterior else "media_id"
        )
        if story.get("url"):
            dados_facebook["url"] = str(story["url"])
        return post_confirmado

    caminho_video = baixar_midia(parte["midia"])
    try:
        inicio = graph_post(
            f"{page_id}/video_stories",
            {"upload_phase": "start", "access_token": token},
        )
        video_id = inicio.get("video_id")
        upload_url = inicio.get("upload_url")
        if not video_id or not upload_url:
            raise RuntimeError(f"Facebook não iniciou o upload do Story: {inicio}")
        dados_facebook["video_id"] = str(video_id)

        tamanho = caminho_video.stat().st_size
        with caminho_video.open("rb") as arquivo:
            resposta = requests.post(
                upload_url,
                headers={
                    "Authorization": f"OAuth {token}",
                    "offset": "0",
                    "file_size": str(tamanho),
                },
                data=arquivo,
                timeout=900,
            )
        if not resposta.ok:
            raise RuntimeError(
                "Facebook falhou no upload do Story "
                f"({resposta.status_code}): {resposta.text}"
            )

        post_id = finalizar_story_facebook(
            page_id, token, dados_facebook, str(video_id)
        )
        finish_em = str(dados_facebook["finish_em"])
        story = aguardar_story_facebook(
            page_id,
            token,
            post_id,
            str(video_id),
            finish_em,
        )
        if story.get("url"):
            dados_facebook["url"] = str(story["url"])
        dados_facebook["id_tipo"] = "post_id"
        return post_id
    finally:
        caminho_video.unlink(missing_ok=True)


def executar_rede(parte: dict, plataforma: str, funcao) -> str | None:
    dados = parte[plataforma]
    if dados.get("status") == "publicado":
        return None
    try:
        publicacao_id = str(funcao(parte))
        dados.update(
            {
                "status": "publicado",
                "id": publicacao_id,
                "publicado_em": datetime.now(BRT).isoformat(timespec="seconds"),
            }
        )
        dados.pop("erro", None)
        return publicacao_id
    except Exception as erro:
        status_erro = "incerto" if isinstance(
            erro, (PublicacaoFacebookIncerta, PublicacaoInstagramIncerta)
        ) else "erro"
        dados.update(
            {
                "status": status_erro,
                "erro": str(erro),
                "ultima_tentativa_em": datetime.now(BRT).isoformat(timespec="seconds"),
            }
        )
        print(f"ERRO {plataforma}, parte {parte.get('ordem', '?')}: {erro}")
        return None


def limite_por_execucao() -> int:
    try:
        limite = int(
            os.getenv(
                "MAX_PACOTES_POR_EXECUCAO",
                str(MAX_PACOTES_POR_EXECUCAO_PADRAO),
            )
        )
    except ValueError as erro:
        raise RuntimeError("MAX_PACOTES_POR_EXECUCAO deve ser um número inteiro.") from erro
    if limite != 1:
        raise RuntimeError(
            "MAX_PACOTES_POR_EXECUCAO deve ser exatamente 1 para preservar "
            "a cadência de um pacote de Stories por dia."
        )
    return limite


def validar_fila(fila: dict) -> None:
    pacotes = fila.get("pacotes")
    if not isinstance(pacotes, list):
        raise RuntimeError("A fila de Stories precisa conter uma lista 'pacotes'.")

    ids: set[str] = set()
    datas: set[str] = set()
    origens: set[str] = set()
    assets: set[str] = set()
    for pacote in pacotes:
        pacote_id = str(pacote.get("id", "")).strip()
        data = str(pacote.get("data", "")).strip()
        horario = str(pacote.get("horario", HORARIO_STORY)).strip()
        if not pacote_id or pacote_id in ids:
            raise RuntimeError(f"ID de pacote ausente ou repetido: {pacote_id!r}.")
        ids.add(pacote_id)
        try:
            datetime.strptime(data, "%Y-%m-%d")
        except ValueError as erro:
            raise RuntimeError(f"Data inválida no pacote {pacote_id}: {data!r}.") from erro
        if data in datas:
            raise RuntimeError(f"Há mais de um pacote de Stories em {data}.")
        datas.add(data)
        if horario != HORARIO_STORY:
            raise RuntimeError(
                f"Horário inválido no pacote {pacote_id}: Stories devem ser às {HORARIO_STORY}."
            )

        status_pacote = str(pacote.get("status", ""))
        if status_pacote not in STATUS_ITEM:
            raise RuntimeError(
                f"Status inválido no pacote {pacote_id}: {status_pacote!r}."
            )
        origem = pacote.get("origem")
        if not isinstance(origem, dict) or not str(origem.get("arquivo", "")).strip():
            raise RuntimeError(f"Origem ausente no pacote {pacote_id}.")
        origem_sha = str(origem.get("sha256", "")).strip().lower()
        if not HEX_SHA256.fullmatch(origem_sha):
            raise RuntimeError(f"SHA-256 da origem inválido no pacote {pacote_id}.")
        if origem_sha in origens:
            raise RuntimeError(f"SHA-256 de origem repetido no pacote {pacote_id}.")
        origens.add(origem_sha)

        partes = pacote.get("partes")
        if not isinstance(partes, list) or not partes:
            raise RuntimeError(f"Pacote {pacote_id} não contém partes.")
        if len(partes) > MAX_PARTES_POR_PACOTE:
            raise RuntimeError(
                f"Pacote {pacote_id} excede o limite de "
                f"{MAX_PARTES_POR_PACOTE} partes."
            )
        ordens = [parte.get("ordem") for parte in partes]
        if ordens != list(range(1, len(partes) + 1)):
            raise RuntimeError(
                f"Partes do pacote {pacote_id} devem estar na ordem contínua 1..{len(partes)}."
            )
        for parte in partes:
            ordem = parte["ordem"]
            status_parte = str(parte.get("status", ""))
            if status_parte not in STATUS_ITEM:
                raise RuntimeError(
                    f"Status inválido em {pacote_id}, parte {ordem}: {status_parte!r}."
                )
            midia = parte.get("midia")
            if not isinstance(midia, dict):
                raise RuntimeError(f"Mídia ausente em {pacote_id}, parte {ordem}.")
            try:
                duracao = float(midia.get("duracao_segundos", 0))
            except (TypeError, ValueError) as erro:
                raise RuntimeError(
                    f"Duração inválida em {pacote_id}, parte {ordem}."
                ) from erro
            if (
                not math.isfinite(duracao)
                or duracao < DURACAO_MINIMA_PARTE_SEGUNDOS
                or duracao > LIMITE_PARTE_SEGUNDOS
            ):
                raise RuntimeError(
                    f"Parte {ordem} de {pacote_id} tem {duracao:.3f}s; "
                    f"o intervalo aceito é {DURACAO_MINIMA_PARTE_SEGUNDOS:.0f}–"
                    f"{LIMITE_PARTE_SEGUNDOS:.0f}s."
                )
            for campo in ("asset", "url_publica", "sha256", "tamanho_bytes"):
                if not midia.get(campo):
                    raise RuntimeError(
                        f"Campo midia.{campo} ausente em {pacote_id}, parte {ordem}."
                    )
            asset = str(midia["asset"]).strip()
            if asset in assets:
                raise RuntimeError(f"Asset repetido na fila: {asset}.")
            assets.add(asset)
            sha_midia = str(midia["sha256"]).strip().lower()
            if not HEX_SHA256.fullmatch(sha_midia):
                raise RuntimeError(
                    f"SHA-256 inválido em {pacote_id}, parte {ordem}."
                )
            try:
                tamanho = int(midia["tamanho_bytes"])
            except (TypeError, ValueError) as erro:
                raise RuntimeError(
                    f"Tamanho inválido em {pacote_id}, parte {ordem}."
                ) from erro
            if tamanho <= 0 or tamanho > TAMANHO_MAXIMO_PARTE_BYTES:
                raise RuntimeError(
                    f"Tamanho inválido em {pacote_id}, parte {ordem}: {tamanho}."
                )
            if not str(midia["url_publica"]).startswith("https://"):
                raise RuntimeError(
                    f"URL pública inválida em {pacote_id}, parte {ordem}."
                )
            redes_publicadas = True
            for plataforma in PLATAFORMAS:
                dados_rede = parte.get(plataforma)
                if not isinstance(dados_rede, dict):
                    raise RuntimeError(
                        f"Estado de {plataforma} ausente em {pacote_id}, parte {ordem}."
                    )
                status_rede = str(dados_rede.get("status", ""))
                if status_rede not in STATUS_REDE:
                    raise RuntimeError(
                        f"Status de {plataforma} inválido em {pacote_id}, parte {ordem}: "
                        f"{status_rede!r}."
                    )
                if status_rede == "publicado" and not str(
                    dados_rede.get("id", "")
                ).strip():
                    raise RuntimeError(
                        f"ID de {plataforma} ausente em {pacote_id}, parte {ordem}."
                    )
                if status_rede == "incerto" and plataforma == "facebook" and not (
                    str(dados_rede.get("video_id", "")).strip()
                    and str(
                        dados_rede.get("finish_em")
                        or dados_rede.get("finish_iniciado_em")
                        or ""
                    ).strip()
                ):
                    raise RuntimeError(
                        f"Facebook incerto sem IDs de reconciliação em "
                        f"{pacote_id}, parte {ordem}."
                    )
                if status_rede == "incerto" and plataforma == "instagram" and not (
                    str(dados_rede.get("container_id", "")).strip()
                    and str(dados_rede.get("publish_iniciado_em", "")).strip()
                ):
                    raise RuntimeError(
                        f"Instagram incerto sem container/tentativa de publish em "
                        f"{pacote_id}, parte {ordem}."
                    )
                redes_publicadas = redes_publicadas and status_rede == "publicado"
            if status_parte == "concluido" and not redes_publicadas:
                raise RuntimeError(
                    f"Parte {ordem} de {pacote_id} está concluída sem confirmação das duas redes."
                )
        if status_pacote == "concluido" and not all(
            parte.get("status") == "concluido"
            and all(
                parte[plataforma].get("status") == "publicado"
                for plataforma in PLATAFORMAS
            )
            for parte in partes
        ):
            raise RuntimeError(
                f"Pacote {pacote_id} está concluído sem todas as partes confirmadas."
            )


def proximos_pacotes(fila: dict, agora: datetime | None = None) -> list[dict]:
    validar_fila(fila)
    limite = limite_por_execucao()
    agora = agora or datetime.now(BRT)
    data_forcada = os.getenv("DATA_PUBLICACAO", "").strip()
    pacotes = fila["pacotes"]
    hora_story, minuto_story = (int(parte) for parte in HORARIO_STORY.split(":"))
    antes_do_horario = (agora.hour, agora.minute) < (hora_story, minuto_story)
    hoje = agora.astimezone(BRT).date().isoformat()
    publicou_hoje = any(
        str(pacote.get("concluido_em", "")).startswith(hoje)
        for pacote in pacotes
        if pacote.get("status") == "concluido"
    )
    if data_forcada:
        if antes_do_horario:
            raise RuntimeError(
                f"DATA_PUBLICACAO não pode antecipar Stories antes das {HORARIO_STORY}."
            )
        if publicou_hoje:
            raise RuntimeError(
                "DATA_PUBLICACAO não pode publicar um segundo pacote no mesmo dia."
            )
        try:
            datetime.strptime(data_forcada, "%Y-%m-%d")
        except ValueError as erro:
            raise RuntimeError("DATA_PUBLICACAO deve usar o formato AAAA-MM-DD.") from erro
        encontrados = [
            pacote
            for pacote in pacotes
            if pacote["data"] == data_forcada and pacote.get("status") != "concluido"
        ]
        if len(encontrados) > 1:
            raise RuntimeError("A fila tem mais de um pacote de Stories nesta data.")
        if encontrados:
            alvo = encontrados[0]
            agendado_alvo = datetime.fromisoformat(
                f"{alvo['data']}T{alvo.get('horario', HORARIO_STORY)}:00"
            ).replace(tzinfo=BRT)
            if agendado_alvo > agora:
                raise RuntimeError(
                    "DATA_PUBLICACAO não pode antecipar um pacote futuro."
                )
            anteriores = [
                pacote
                for pacote in pacotes
                if pacote.get("status") != "concluido"
                and (
                    pacote["data"],
                    pacote.get("horario", HORARIO_STORY),
                    pacote.get("id", ""),
                )
                < (
                    alvo["data"],
                    alvo.get("horario", HORARIO_STORY),
                    alvo.get("id", ""),
                )
            ]
            if anteriores:
                primeiro = min(
                    anteriores,
                    key=lambda pacote: (
                        pacote["data"],
                        pacote.get("horario", HORARIO_STORY),
                        pacote.get("id", ""),
                    ),
                )
                raise RuntimeError(
                    "DATA_PUBLICACAO não pode pular o pacote pendente anterior "
                    f"{primeiro['data']} ({primeiro['id']})."
                )
        return encontrados[:1]

    # Stories seguem a cadência editorial de um pacote por dia. Uma fila
    # atrasada é recuperada em dias sucessivos, nunca despejada em sequência no
    # mesmo dia. Antes das 09:00 também não se antecipa um pacote vencido.
    if antes_do_horario:
        return []
    if publicou_hoje:
        return []
    devidos: list[tuple[datetime, str, dict]] = []
    for pacote in pacotes:
        if pacote.get("status") == "concluido":
            continue
        agendado = datetime.fromisoformat(
            f"{pacote['data']}T{pacote.get('horario', HORARIO_STORY)}:00"
        ).replace(tzinfo=BRT)
        if agendado <= agora:
            devidos.append((agendado, pacote["id"], pacote))
    devidos.sort(key=lambda item: (item[0], item[1]))
    return [pacote for _, _, pacote in devidos[:limite]]


def proximo_pacote_pendente(fila: dict) -> dict | None:
    pendentes = [
        pacote for pacote in fila.get("pacotes", [])
        if pacote.get("status") != "concluido"
    ]
    if not pendentes:
        return None
    return min(
        pendentes,
        key=lambda pacote: (
            pacote["data"],
            pacote.get("horario", HORARIO_STORY),
            pacote.get("id", ""),
        ),
    )


def parte_com_erro(parte: dict) -> bool:
    return any(
        parte[plataforma].get("status") in {"erro", "incerto"}
        for plataforma in PLATAFORMAS
    )


def erros_da_parte(pacote: dict, parte: dict) -> list[str]:
    erros = []
    for plataforma in PLATAFORMAS:
        dados = parte[plataforma]
        if dados.get("status") in {"erro", "incerto"}:
            erros.append(
                f"{pacote['id']}, parte {parte['ordem']}, {plataforma}: "
                f"{dados.get('erro', 'erro sem detalhe')}"
            )
    return erros


def processar_fila(fila: dict, agora: datetime | None = None) -> dict:
    pacotes = proximos_pacotes(fila, agora=agora)
    if not pacotes:
        print("Nenhum pacote de Stories pendente e devido para publicação.")
        return {
            "resultado": RESULTADO_NENHUM_DEVIDO,
            "pacotes_devidos": 0,
            "pacotes_concluidos": 0,
            "partes_concluidas": 0,
            "publicacoes": [],
            "erros": [],
            "proximo": proximo_pacote_pendente(fila),
        }

    # O pacote inteiro é conferido antes da primeira publicação. Depois, a
    # conta é validada antes de qualquer efeito na Meta. Assim um asset ausente
    # não produz sequência truncada, e uma conta Creator/credencial trocada não
    # publica apenas no Facebook.
    validar_midias_antes_de_publicar(pacotes)
    # A conta é validada antes de qualquer efeito externo. Assim uma conta
    # Creator/credencial trocada não publica apenas no Facebook e deixa o
    # pacote permanentemente parcial.
    validar_contas_meta()

    publicacoes: list[dict] = []
    pacotes_concluidos = 0
    partes_concluidas = 0
    print(f"{len(pacotes)} pacote(s) de Stories vencido(s) serão processado(s).")

    for indice_pacote, pacote in enumerate(pacotes, start=1):
        print(
            f"Processando pacote [{indice_pacote}/{len(pacotes)}] "
            f"{pacote['data']} {pacote.get('horario', HORARIO_STORY)} ({pacote['id']})"
        )
        for parte in pacote["partes"]:
            instagram_id = executar_rede(parte, "instagram", publicar_instagram)
            if instagram_id:
                publicacoes.append(
                    {
                        "pacote": pacote["id"],
                        "parte": parte["ordem"],
                        "rede": "Instagram",
                        "id": instagram_id,
                    }
                )
            salvar_fila(fila)

            facebook_id = executar_rede(parte, "facebook", publicar_facebook)
            if facebook_id:
                publicacoes.append(
                    {
                        "pacote": pacote["id"],
                        "parte": parte["ordem"],
                        "rede": "Facebook",
                        "id": facebook_id,
                    }
                )
            salvar_fila(fila)

            concluiu = all(
                parte[plataforma].get("status") == "publicado"
                for plataforma in PLATAFORMAS
            )
            if concluiu and parte.get("status") != "concluido":
                parte.update(
                    {
                        "status": "concluido",
                        "concluido_em": datetime.now(BRT).isoformat(timespec="seconds"),
                    }
                )
                partes_concluidas += 1
            salvar_fila(fila)

            if parte_com_erro(parte):
                print(
                    "Processamento interrompido para preservar a ordem das partes; "
                    "a rede pendente será retomada na próxima execução."
                )
                return {
                    "resultado": RESULTADO_FALHA,
                    "pacotes_devidos": len(pacotes),
                    "pacotes_concluidos": pacotes_concluidos,
                    "partes_concluidas": partes_concluidas,
                    "publicacoes": publicacoes,
                    "erros": erros_da_parte(pacote, parte),
                    "pacote_falho": pacote,
                    "parte_falha": parte,
                    "proximo": proximo_pacote_pendente(fila),
                }

        if all(parte.get("status") == "concluido" for parte in pacote["partes"]):
            if pacote.get("status") != "concluido":
                pacote.update(
                    {
                        "status": "concluido",
                        "concluido_em": datetime.now(BRT).isoformat(timespec="seconds"),
                    }
                )
                pacotes_concluidos += 1
            salvar_fila(fila)

    return {
        "resultado": RESULTADO_PUBLICADO if publicacoes else RESULTADO_RECONCILIADO,
        "pacotes_devidos": len(pacotes),
        "pacotes_concluidos": pacotes_concluidos,
        "partes_concluidas": partes_concluidas,
        "publicacoes": publicacoes,
        "erros": [],
        "proximo": proximo_pacote_pendente(fila),
    }


def mensagem_resultado(relatorio: dict) -> str:
    resultado = relatorio["resultado"]
    publicacoes = relatorio.get("publicacoes", [])
    instagram = sum(item["rede"] == "Instagram" for item in publicacoes)
    facebook = sum(item["rede"] == "Facebook" for item in publicacoes)
    if resultado == RESULTADO_PUBLICADO:
        return (
            f"Publicação confirmada pela Meta: {relatorio['pacotes_concluidos']} "
            f"pacote(s) e {relatorio['partes_concluidas']} parte(s) concluído(s); "
            f"{instagram} no Instagram e {facebook} no Facebook nesta execução."
        )
    if resultado == RESULTADO_NENHUM_DEVIDO:
        return "Nenhuma publicação realizada: não havia pacote de Stories pendente e devido."
    if resultado == RESULTADO_RECONCILIADO:
        return "Fila de Stories reconciliada, sem nova publicação nesta execução."
    if resultado == RESULTADO_DIAGNOSTICO:
        return "Contas Meta validadas; nenhuma publicação ou limpeza foi realizada."
    detalhes = "; ".join(relatorio.get("erros", [])) or "erro sem detalhe"
    return f"Falha na publicação de Stories: {detalhes}."


def resumo_markdown(relatorio: dict) -> str:
    titulos = {
        RESULTADO_PUBLICADO: "✅ PUBLICAÇÃO DE STORIES CONFIRMADA",
        RESULTADO_NENHUM_DEVIDO: "🟡 NENHUM STORY DEVIDO — NADA FOI PUBLICADO",
        RESULTADO_RECONCILIADO: "🟡 FILA DE STORIES RECONCILIADA — NADA NOVO FOI PUBLICADO",
        RESULTADO_DIAGNOSTICO: "🔎 DIAGNÓSTICO META APROVADO — NADA FOI PUBLICADO",
        RESULTADO_FALHA: "❌ FALHA NA PUBLICAÇÃO DE STORIES",
    }
    publicacoes = relatorio.get("publicacoes", [])
    instagram = sum(item["rede"] == "Instagram" for item in publicacoes)
    facebook = sum(item["rede"] == "Facebook" for item in publicacoes)
    linhas = [
        f"## {titulos.get(relatorio['resultado'], relatorio['resultado'])}",
        "",
        mensagem_resultado(relatorio),
        f"Executado em `{relatorio['executado_em']}` (horário de Brasília).",
        "",
        "| Estado | Pacotes devidos | Pacotes concluídos | Partes concluídas | Instagram | Facebook |",
        "|---|---:|---:|---:|---:|---:|",
        (
            f"| `{relatorio['resultado']}` | {relatorio.get('pacotes_devidos', 0)} | "
            f"{relatorio.get('pacotes_concluidos', 0)} | "
            f"{relatorio.get('partes_concluidas', 0)} | {instagram} | {facebook} |"
        ),
    ]
    if publicacoes:
        linhas.extend(
            [
                "",
                "### IDs confirmados pela Meta",
                "",
                "| Pacote | Parte | Rede | ID |",
                "|---|---:|---|---|",
            ]
        )
        linhas.extend(
            f"| `{item['pacote']}` | {item['parte']} | {item['rede']} | `{item['id']}` |"
            for item in publicacoes
        )
    if relatorio.get("erros"):
        linhas.extend(["", "### Erros", ""])
        linhas.extend(f"- {erro}" for erro in relatorio["erros"])
    contas = relatorio.get("contas")
    if isinstance(contas, dict):
        instagram = contas.get("instagram", {})
        facebook = contas.get("facebook", {})
        linhas.extend(
            [
                "",
                "### Contas confirmadas (sem expor tokens)",
                "",
                f"- Instagram: `@{instagram.get('username', '')}` — "
                f"`{instagram.get('account_type', '')}` — ID `{instagram.get('id', '')}`",
                f"- Facebook: `{facebook.get('name', '')}` — ID `{facebook.get('id', '')}`",
            ]
        )
    proximo = relatorio.get("proximo")
    if proximo:
        linhas.extend(
            [
                "",
                f"Próximo pacote pendente: `{proximo['data']} "
                f"{proximo.get('horario', HORARIO_STORY)}` (`{proximo['id']}`).",
            ]
        )
    return "\n".join(linhas) + "\n"


def registrar_resultado(relatorio: dict) -> None:
    relatorio = dict(relatorio)
    relatorio.setdefault(
        "executado_em",
        datetime.now(BRT).isoformat(timespec="seconds"),
    )
    publicacoes = relatorio.get("publicacoes", [])
    saidas = {
        "resultado": relatorio["resultado"],
        "mensagem": mensagem_resultado(relatorio),
        "pacotes_devidos": relatorio.get("pacotes_devidos", 0),
        "pacotes_concluidos": relatorio.get("pacotes_concluidos", 0),
        "partes_concluidas": relatorio.get("partes_concluidas", 0),
        "instagram_publicados": sum(
            item["rede"] == "Instagram" for item in publicacoes
        ),
        "facebook_publicados": sum(item["rede"] == "Facebook" for item in publicacoes),
        "executado_em": relatorio["executado_em"],
    }
    print(f"RESULTADO_INEQUIVOCO={relatorio['resultado']}")
    print(saidas["mensagem"])

    github_output = os.getenv("GITHUB_OUTPUT", "").strip()
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as arquivo:
            for nome, valor in saidas.items():
                arquivo.write(f"{nome}={str(valor).replace(chr(10), ' ')}\n")

    github_summary = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if github_summary:
        with Path(github_summary).open("a", encoding="utf-8") as arquivo:
            arquivo.write(resumo_markdown(relatorio))


def relatorio_falha_inesperada(erro: Exception) -> dict:
    return {
        "resultado": RESULTADO_FALHA,
        "pacotes_devidos": 0,
        "pacotes_concluidos": 0,
        "partes_concluidas": 0,
        "publicacoes": [],
        "erros": [f"erro inesperado: {erro}"],
        "proximo": None,
    }


def main() -> int:
    try:
        fila = json.loads(FILA_FILE.read_text(encoding="utf-8"))
        if os.getenv("DIAGNOSTICAR_CONTAS_META", "").strip().lower() in {
            "1",
            "true",
            "sim",
        }:
            contas = validar_contas_meta()
            relatorio = {
                "resultado": RESULTADO_DIAGNOSTICO,
                "pacotes_devidos": 0,
                "pacotes_concluidos": 0,
                "partes_concluidas": 0,
                "publicacoes": [],
                "erros": [],
                "contas": contas,
                "proximo": proximo_pacote_pendente(fila),
            }
        else:
            relatorio = processar_fila(fila)
    except Exception as erro:
        registrar_resultado(relatorio_falha_inesperada(erro))
        return 1
    registrar_resultado(relatorio)
    return 1 if relatorio["resultado"] == RESULTADO_FALHA else 0


if __name__ == "__main__":
    raise SystemExit(main())
