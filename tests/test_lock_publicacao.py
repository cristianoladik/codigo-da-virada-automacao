import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import lock_publicacao


def git(repositorio: Path, *argumentos: str) -> str:
    resultado = subprocess.run(
        ("git", "-C", str(repositorio), *argumentos),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return resultado.stdout.strip()


class LockPublicacaoTest(unittest.TestCase):
    def setUp(self):
        self.temporario = tempfile.TemporaryDirectory()
        raiz = Path(self.temporario.name)
        self.repositorio = raiz / "trabalho"
        self.remoto = raiz / "remoto.git"
        self.repositorio.mkdir()
        subprocess.run(
            ("git", "init", "--bare", str(self.remoto)),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        git(self.repositorio, "init", "-b", "main")
        git(self.repositorio, "config", "user.name", "Teste")
        git(self.repositorio, "config", "user.email", "teste@local.invalid")
        (self.repositorio / "README.md").write_text("teste\n", encoding="utf-8")
        git(self.repositorio, "add", "README.md")
        git(self.repositorio, "commit", "-m", "inicial")
        git(self.repositorio, "remote", "add", "origin", str(self.remoto))
        git(self.repositorio, "push", "-u", "origin", "main")

    def tearDown(self):
        self.temporario.cleanup()

    def test_duas_aquisicoes_concorrentes_tem_um_unico_dono(self):
        segundo = Path(self.temporario.name) / "trabalho-2"
        subprocess.run(
            ("git", "clone", "--branch", "main", str(self.remoto), str(segundo)),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        barreira = threading.Barrier(2)

        def tentar(indice: int, repositorio: Path):
            token = Path(self.temporario.name) / f"token-{indice}.txt"
            barreira.wait()
            try:
                return lock_publicacao.adquirir(
                    repositorio, f"dono-{indice}", token, 0
                )
            except RuntimeError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            resultados = list(
                executor.map(
                    lambda item: tentar(*item),
                    ((1, self.repositorio), (2, segundo)),
                )
            )

        vencedores = [sha for sha in resultados if sha]
        self.assertEqual(len(vencedores), 1)
        self.assertEqual(lock_publicacao.sha_remoto(self.repositorio), vencedores[0])
        token_vencedor = next(
            Path(self.temporario.name) / f"token-{indice}.txt"
            for indice, sha in enumerate(resultados, start=1)
            if sha
        )
        lock_publicacao.liberar(self.repositorio, token_vencedor)
        self.assertIsNone(lock_publicacao.sha_remoto(self.repositorio))

    def test_token_antigo_nunca_apaga_lock_de_outro_dono(self):
        token = Path(self.temporario.name) / "token-antigo.txt"
        antigo = lock_publicacao.adquirir(
            self.repositorio, "dono-antigo", token, 0
        )
        novo = lock_publicacao.criar_commit_lock(self.repositorio, "dono-novo")
        git(
            self.repositorio,
            "push",
            f"--force-with-lease={lock_publicacao.LOCK_REF}:{antigo}",
            "origin",
            f"{novo}:{lock_publicacao.LOCK_REF}",
        )

        with self.assertRaisesRegex(RuntimeError, "mudou de dono"):
            lock_publicacao.liberar(self.repositorio, token)

        self.assertFalse(token.exists())
        self.assertEqual(lock_publicacao.sha_remoto(self.repositorio), novo)

    def test_lock_expirado_e_substituido_por_cas(self):
        token_antigo = Path(self.temporario.name) / "token-antigo.txt"
        antigo = lock_publicacao.adquirir(
            self.repositorio, "dono-antigo", token_antigo, 0
        )
        token_novo = Path(self.temporario.name) / "token-novo.txt"
        with patch.object(lock_publicacao, "LOCK_EXPIRA_SEGUNDOS", -1):
            novo = lock_publicacao.adquirir(
                self.repositorio, "dono-novo", token_novo, 5
            )

        self.assertNotEqual(antigo, novo)
        self.assertEqual(lock_publicacao.sha_remoto(self.repositorio), novo)
        lock_publicacao.liberar(self.repositorio, token_novo)


if __name__ == "__main__":
    unittest.main()
