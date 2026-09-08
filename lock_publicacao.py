"""Mutex distribuído para todos os escritores das filas IG/FB.

O lock é uma branch efêmera com commit único. A criação da ref no GitHub é
atômica: somente um escritor consegue publicá-la. A remoção usa
``--force-with-lease`` com o SHA adquirido, portanto nunca apaga o lock de outro
processo. Locks abandonados há mais de oito horas podem ser recuperados pelo
mesmo protocolo CAS.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


LOCK_REF = "refs/heads/lock-publicacao-instagram-facebook"
# Todos os titulares automáticos têm limite duro de no máximo seis horas. Duas
# horas de folga impedem roubo de lock legítimo e permitem recuperar uma queda
# no mesmo dia.
LOCK_EXPIRA_SEGUNDOS = 8 * 60 * 60
GIT_TIMEOUT_SEGUNDOS = 120


def git(
    repositorio: Path,
    *argumentos: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    ambiente = dict(os.environ)
    ambiente.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        }
    )
    if env:
        ambiente.update(env)
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
            f"git {' '.join(argumentos)} excedeu {GIT_TIMEOUT_SEGUNDOS}s."
        ) from erro
    if check and resultado.returncode:
        detalhe = (resultado.stderr or resultado.stdout).strip()
        raise RuntimeError(f"git {' '.join(argumentos)} falhou: {detalhe}")
    return resultado


def sha_remoto(repositorio: Path) -> str | None:
    resultado = git(repositorio, "ls-remote", "--heads", "origin", LOCK_REF)
    linha = resultado.stdout.strip()
    return linha.split()[0] if linha else None


def criar_commit_lock(repositorio: Path, dono: str) -> str:
    git(repositorio, "fetch", "--quiet", "origin", "main")
    arvore = git(repositorio, "rev-parse", "origin/main^{tree}").stdout.strip()
    ambiente = dict(os.environ)
    instante = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ambiente.update(
        {
            "GIT_AUTHOR_NAME": "Lock publicação IGFB",
            "GIT_AUTHOR_EMAIL": "lock@local.invalid",
            "GIT_COMMITTER_NAME": "Lock publicação IGFB",
            "GIT_COMMITTER_EMAIL": "lock@local.invalid",
        }
    )
    mensagem = f"lock:{dono}:{instante}:{uuid.uuid4().hex}"
    return git(
        repositorio,
        "commit-tree",
        arvore,
        "-p",
        "origin/main",
        "-m",
        mensagem,
        env=ambiente,
    ).stdout.strip()


def idade_lock(repositorio: Path, sha: str) -> int:
    # Busque a ref anunciada, não um SHA que pode deixar de ser alcançável se
    # outro processo trocar a branch entre ls-remote e fetch.
    busca = git(
        repositorio,
        "fetch",
        "--quiet",
        "origin",
        LOCK_REF,
        check=False,
    )
    if busca.returncode:
        return 0
    observado = git(repositorio, "rev-parse", "FETCH_HEAD").stdout.strip()
    if observado != sha:
        return 0
    epoch = int(
        git(repositorio, "show", "-s", "--format=%ct", observado).stdout.strip()
    )
    return max(0, int(time.time()) - epoch)


def remover_se_for_o_mesmo(repositorio: Path, sha: str) -> bool:
    if sha_remoto(repositorio) != sha:
        return False
    resultado = git(
        repositorio,
        "push",
        f"--force-with-lease={LOCK_REF}:{sha}",
        "origin",
        f":{LOCK_REF}",
        check=False,
    )
    # O lease torna o próprio push a comparação-atômica. Não consulte a ref
    # novamente aqui: outro escritor pode adquiri-la logo após nossa remoção.
    if resultado.returncode == 0:
        return True
    # Uma queda de conexão pode esconder a resposta de um DELETE aceito. Se a
    # ref já não aponta para nosso SHA, nossa posse terminou; nunca tente apagar
    # uma eventual ref nova.
    try:
        return sha_remoto(repositorio) != sha
    except RuntimeError:
        return False


def salvar_token(arquivo_token: Path, sha: str) -> None:
    arquivo_token.parent.mkdir(parents=True, exist_ok=True)
    temporario = arquivo_token.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporario.write_text(sha + "\n", encoding="ascii")
        os.replace(temporario, arquivo_token)
    finally:
        temporario.unlink(missing_ok=True)


def adquirir(
    repositorio: Path,
    dono: str,
    arquivo_token: Path,
    timeout_segundos: int,
) -> str:
    limite = time.monotonic() + timeout_segundos
    while True:
        existente = sha_remoto(repositorio)
        if existente:
            if idade_lock(repositorio, existente) > LOCK_EXPIRA_SEGUNDOS:
                if remover_se_for_o_mesmo(repositorio, existente):
                    continue
        else:
            candidato = criar_commit_lock(repositorio, dono)
            # Grave a evidência antes do push. Se a resposta ou a confirmação
            # de rede se perder, o finally do chamador ainda consegue liberar
            # exatamente este candidato.
            salvar_token(arquivo_token, candidato)
            while True:
                try:
                    git(
                        repositorio,
                        "push",
                        f"--force-with-lease={LOCK_REF}:",
                        "origin",
                        f"{candidato}:{LOCK_REF}",
                        check=False,
                    )
                except RuntimeError:
                    # O servidor pode ter aceitado a criação antes da perda de
                    # resposta. A consulta remota abaixo é a fonte de verdade.
                    pass
                try:
                    confirmado = sha_remoto(repositorio)
                except RuntimeError:
                    confirmado = None
                if confirmado == candidato:
                    github_output = os.getenv("GITHUB_OUTPUT", "").strip()
                    if github_output:
                        with Path(github_output).open("a", encoding="utf-8") as saida:
                            saida.write(f"lock_sha={candidato}\n")
                    print(f"LOCK_ADQUIRIDO={candidato}")
                    return candidato
                if confirmado:
                    # Outro candidato venceu a criação atômica.
                    arquivo_token.unlink(missing_ok=True)
                    break
                if time.monotonic() >= limite:
                    raise RuntimeError(
                        "Não foi possível confirmar se o lock remoto foi criado; "
                        "o token local foi preservado para liberação segura."
                    )
                time.sleep(5)
        if time.monotonic() >= limite:
            raise RuntimeError(
                "Timeout aguardando o lock remoto de publicação IG/FB."
            )
        time.sleep(5)


def liberar(repositorio: Path, arquivo_token: Path) -> None:
    if not arquivo_token.is_file():
        print("LOCK_NAO_ADQUIRIDO")
        return
    sha = arquivo_token.read_text(encoding="ascii").strip()
    if not sha:
        raise RuntimeError("O arquivo de token do lock está vazio.")
    remoto = sha_remoto(repositorio)
    if remoto is None:
        arquivo_token.unlink(missing_ok=True)
        print(f"LOCK_JA_LIBERADO={sha}")
        return
    if remoto != sha:
        # O lock pertence agora a outro processo. Apague apenas o token local e
        # sinalize a perda de posse; a ref alheia jamais é removida.
        arquivo_token.unlink(missing_ok=True)
        raise RuntimeError(
            "O lock remoto mudou de dono; a ref atual foi preservada."
        )
    if not remover_se_for_o_mesmo(repositorio, sha):
        # Preserve o token para que a liberação possa ser repetida depois de
        # uma falha transitória de rede/autenticação.
        raise RuntimeError("Não foi possível liberar o lock remoto adquirido.")
    arquivo_token.unlink(missing_ok=True)
    print(f"LOCK_LIBERADO={sha}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("acao", choices=("adquirir", "liberar"))
    parser.add_argument("--repositorio", type=Path, default=Path.cwd())
    parser.add_argument("--dono", default=f"local-{os.getpid()}")
    parser.add_argument("--arquivo-token", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    if args.timeout < 0 or args.timeout > 3600:
        parser.error("--timeout deve ficar entre 0 e 3600 segundos")
    if args.acao == "adquirir":
        adquirir(args.repositorio, args.dono, args.arquivo_token, args.timeout)
    else:
        liberar(args.repositorio, args.arquivo_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
