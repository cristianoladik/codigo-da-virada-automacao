"""Faz push de ``HEAD:main`` e confirma o SHA realmente visível no remoto.

Deve ser usado enquanto o escritor possui ``lock_publicacao.py``. Uma resposta
HTTP/Git perdida não vira falso fracasso: o SHA remoto é a fonte de verdade.
Nunca usa force-push e nunca mescla JSON textualmente.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from pathlib import Path


GIT_TIMEOUT_SEGUNDOS = 120


def git(
    repositorio: Path, *argumentos: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    ambiente = dict(os.environ)
    ambiente.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})
    try:
        resultado = subprocess.run(
            ("git", "-C", str(repositorio), *argumentos),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=ambiente,
            timeout=GIT_TIMEOUT_SEGUNDOS,
        )
    except subprocess.TimeoutExpired as erro:
        raise RuntimeError(
            f"git {' '.join(argumentos)} excedeu {GIT_TIMEOUT_SEGUNDOS}s"
        ) from erro
    if check and resultado.returncode:
        detalhe = (resultado.stderr or resultado.stdout).strip()
        raise RuntimeError(f"git {' '.join(argumentos)} falhou: {detalhe}")
    return resultado


def sha_main_remoto(repositorio: Path) -> str | None:
    resposta = git(
        repositorio, "ls-remote", "--heads", "origin", "refs/heads/main"
    ).stdout.strip()
    return resposta.split()[0] if resposta else None


def push_confirmado(repositorio: Path, tentativas: int = 5) -> str:
    if tentativas < 1 or tentativas > 10:
        raise ValueError("tentativas deve ficar entre 1 e 10")
    head = git(repositorio, "rev-parse", "HEAD").stdout.strip()
    ultimo_erro = ""
    for tentativa in range(1, tentativas + 1):
        try:
            envio = git(
                repositorio,
                "push",
                "origin",
                "HEAD:main",
                check=False,
            )
            if envio.returncode:
                ultimo_erro = (envio.stderr or envio.stdout).strip()
        except RuntimeError as erro:
            ultimo_erro = str(erro)

        try:
            remoto = sha_main_remoto(repositorio)
        except RuntimeError as erro:
            remoto = None
            ultimo_erro = str(erro)
        if remoto == head:
            print(f"PUSH_CONFIRMADO={head}")
            return head

        if tentativa < tentativas:
            time.sleep(min(15, 2 ** (tentativa - 1)))

    raise RuntimeError(
        "Não foi possível confirmar HEAD em origin/main após "
        f"{tentativas} tentativa(s). HEAD={head}; detalhe={ultimo_erro or 'ausente'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repositorio", type=Path, default=Path.cwd())
    parser.add_argument("--tentativas", type=int, default=5)
    args = parser.parse_args()
    push_confirmado(args.repositorio, args.tentativas)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
