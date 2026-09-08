import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import push_confirmado


def git(repositorio: Path, *argumentos: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repositorio), *argumentos),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


class PushConfirmadoTest(unittest.TestCase):
    def setUp(self):
        self.temporario = tempfile.TemporaryDirectory()
        raiz = Path(self.temporario.name)
        self.repo = raiz / "repo"
        self.remoto = raiz / "remoto.git"
        self.repo.mkdir()
        subprocess.run(
            ("git", "init", "--bare", str(self.remoto)),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Teste")
        git(self.repo, "config", "user.email", "teste@local.invalid")
        (self.repo / "fila.json").write_text("{}\n", encoding="utf-8")
        git(self.repo, "add", "fila.json")
        git(self.repo, "commit", "-m", "inicial")
        git(self.repo, "remote", "add", "origin", str(self.remoto))

    def tearDown(self):
        self.temporario.cleanup()

    def test_push_e_confirmado_pelo_sha_remoto(self):
        sha = push_confirmado.push_confirmado(self.repo, tentativas=1)
        self.assertEqual(sha, git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(sha, push_confirmado.sha_main_remoto(self.repo))

    def test_resposta_de_push_perdida_ainda_e_sucesso_se_sha_chegou(self):
        original = push_confirmado.git
        chamadas = {"push": 0}

        def resposta_perdida(repositorio, *argumentos, **kwargs):
            if argumentos and argumentos[0] == "push":
                chamadas["push"] += 1
                original(repositorio, *argumentos, check=True)
                return subprocess.CompletedProcess(argumentos, 1, "", "timeout")
            return original(repositorio, *argumentos, **kwargs)

        with patch.object(push_confirmado, "git", side_effect=resposta_perdida):
            sha = push_confirmado.push_confirmado(self.repo, tentativas=1)

        self.assertEqual(chamadas["push"], 1)
        self.assertEqual(sha, git(self.repo, "rev-parse", "HEAD"))

    def test_falha_sem_sha_remoto_nao_declara_sucesso(self):
        with patch.object(
            push_confirmado,
            "git",
            side_effect=[
                subprocess.CompletedProcess([], 0, "a" * 40 + "\n", ""),
                subprocess.CompletedProcess([], 1, "", "falhou"),
                subprocess.CompletedProcess([], 0, "b" * 40 + "\trefs/heads/main\n", ""),
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "Não foi possível confirmar"):
                push_confirmado.push_confirmado(self.repo, tentativas=1)


if __name__ == "__main__":
    unittest.main()
