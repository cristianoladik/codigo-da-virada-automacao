"""Remove da release assets já confirmados nas duas redes.

Aceita tanto a fila de Reels (``conteudos[].midia``) quanto a fila de pacotes de
Stories (``pacotes[].partes[].midia``). O marcador de remoção fica na própria
mídia e torna a limpeza idempotente.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BRT = timezone(timedelta(hours=-3))


def redes_confirmadas(item: dict) -> bool:
    return all(
        item.get(plataforma, {}).get("status") == "publicado"
        and str(item.get(plataforma, {}).get("id", "")).strip()
        for plataforma in ("instagram", "facebook")
    )


def todas_as_midias(fila: dict) -> list[dict]:
    midias = [
        item["midia"]
        for item in fila.get("conteudos", [])
        if isinstance(item.get("midia"), dict)
    ]
    midias.extend(
        parte["midia"]
        for pacote in fila.get("pacotes", [])
        for parte in pacote.get("partes", [])
        if isinstance(parte.get("midia"), dict)
    )
    return midias


def salvar_fila_atomico(caminho: Path, fila: dict) -> None:
    temporario = caminho.with_name(f".{caminho.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporario.open("w", encoding="utf-8", newline="\n") as arquivo:
            arquivo.write(json.dumps(fila, ensure_ascii=False, indent=2) + "\n")
            arquivo.flush()
            os.fsync(arquivo.fileno())
        os.replace(temporario, caminho)
    finally:
        temporario.unlink(missing_ok=True)


def ativos_concluidos(fila: dict) -> list[dict]:
    midias: list[dict] = []
    referencias = Counter(
        str(midia.get("asset", "")).strip()
        for midia in todas_as_midias(fila)
        if str(midia.get("asset", "")).strip()
    )
    repetidos = sorted(asset for asset, total in referencias.items() if total > 1)
    if repetidos:
        raise RuntimeError(
            "Asset referenciado mais de uma vez; limpeza cancelada: "
            + ", ".join(repetidos)
        )

    def adicionar(midia: dict) -> None:
        asset = str(midia.get("asset", "")).strip()
        if not asset or midia.get("removido_da_release_em"):
            return
        midias.append(midia)

    for item in fila.get("conteudos", []):
        if (
            item.get("status") == "concluido"
            and redes_confirmadas(item)
            and isinstance(item.get("midia"), dict)
        ):
            adicionar(item["midia"])

    for pacote in fila.get("pacotes", []):
        if pacote.get("status") != "concluido":
            continue
        partes = pacote.get("partes")
        if not isinstance(partes, list) or not partes or not all(
            parte.get("status") == "concluido"
            and redes_confirmadas(parte)
            and isinstance(parte.get("midia"), dict)
            for parte in partes
        ):
            raise RuntimeError(
                f"Pacote concluído inconsistente; limpeza cancelada: {pacote.get('id', 'sem-id')}"
            )
        for parte in partes:
            if isinstance(parte.get("midia"), dict):
                adicionar(parte["midia"])

    return midias


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fila", type=Path, required=True)
    args = parser.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    tag = os.getenv("RELEASE_TAG", "fila-instagram-facebook")
    fila = json.loads(args.fila.read_text(encoding="utf-8"))
    midias = ativos_concluidos(fila)
    if not midias:
        return
    cabecalhos = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    release = requests.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", headers=cabecalhos, timeout=30)
    release.raise_for_status()
    por_nome = {asset["name"]: asset for asset in release.json().get("assets", [])}
    for midia in midias:
        asset = por_nome.get(midia["asset"])
        if asset:
            digest_esperado = str(midia.get("sha256", "")).strip().lower()
            digest_release = str(asset.get("digest", "")).removeprefix("sha256:").strip().lower()
            try:
                tamanho_esperado = int(midia.get("tamanho_bytes", 0))
                tamanho_release = int(asset.get("size", -1))
            except (TypeError, ValueError) as erro:
                raise RuntimeError(
                    f"Tamanho inválido ao conferir {midia['asset']}."
                ) from erro
            if (
                len(digest_esperado) != 64
                or digest_release != digest_esperado
                or tamanho_esperado <= 0
                or tamanho_release != tamanho_esperado
            ):
                raise RuntimeError(
                    f"SHA-256 ou tamanho divergente; asset preservado: {midia['asset']}"
                )
            resposta = requests.delete(f"https://api.github.com/repos/{repo}/releases/assets/{asset['id']}", headers=cabecalhos, timeout=30)
            if resposta.status_code != 204:
                raise RuntimeError(f"Não foi possível remover {midia['asset']}: HTTP {resposta.status_code} {resposta.text}")
        midia["removido_da_release_em"] = datetime.now(BRT).isoformat()
        # Grava após cada asset. Se a limpeza seguinte falhar, o workflow ainda
        # consegue persistir os marcadores das remoções já confirmadas.
        salvar_fila_atomico(args.fila, fila)


if __name__ == "__main__":
    main()
