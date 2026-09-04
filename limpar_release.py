"""Remove da release somente assets cujas duas publicações foram confirmadas."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BRT = timezone(timedelta(hours=-3))


def ativos_concluidos(fila: dict) -> list[dict]:
    return [x["midia"] for x in fila.get("conteudos", []) if x.get("status") == "concluido" and not x["midia"].get("removido_da_release_em")]


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
    por_nome = {asset["name"]: asset["id"] for asset in release.json().get("assets", [])}
    alterou = False
    for midia in midias:
        asset_id = por_nome.get(midia["asset"])
        if asset_id:
            resposta = requests.delete(f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}", headers=cabecalhos, timeout=30)
            if resposta.status_code != 204:
                raise RuntimeError(f"Não foi possível remover {midia['asset']}: HTTP {resposta.status_code} {resposta.text}")
        midia["removido_da_release_em"] = datetime.now(BRT).isoformat()
        alterou = True
    if alterou:
        args.fila.write_text(json.dumps(fila, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
