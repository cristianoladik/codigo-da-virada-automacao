import json
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publicar-stories.yml"
WORKFLOW_REELS = ROOT / ".github" / "workflows" / "publicar.yml"
FILA = ROOT / "fila" / "fila-stories.json"


class WorkflowStoriesTest(unittest.TestCase):
    def test_workflow_e_exclusivo_de_stories_e_nao_pode_ser_substituido_por_reels(self):
        texto = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('cron: "10 0-5,7-23 * * *"', texto)
        self.assertIn("group: publicacao-instagram-facebook-stories", texto)
        self.assertIn(
            "group: publicacao-instagram-facebook-reels",
            WORKFLOW_REELS.read_text(encoding="utf-8"),
        )
        self.assertIn("ref: main", texto)
        self.assertIn("run: python publicar_stories.py", texto)
        self.assertNotIn("run: python publicar.py\n", texto)

    def test_estado_e_persistido_antes_e_depois_da_limpeza(self):
        texto = WORKFLOW.read_text(encoding="utf-8")
        antes = texto.index("Persistir estado antes de remover assets")
        limpeza = texto.index("Remover partes temporárias já publicadas")
        depois = texto.index("Persistir marcadores da limpeza")
        self.assertLess(antes, limpeza)
        self.assertLess(limpeza, depois)
        self.assertGreaterEqual(texto.count("git add fila/fila-stories.json"), 2)

    def test_diagnostico_meta_nao_publica_nem_limpa(self):
        texto = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("DIAGNOSTICAR_CONTAS_META", texto)
        self.assertIn("DIAGNOSTICO_META_OK", texto)
        passo_persistencia = texto.split(
            "Persistir estado antes de remover assets", 1
        )[1].split("Remover partes temporárias já publicadas", 1)[0]
        self.assertIn("resultado != 'DIAGNOSTICO_META_OK'", passo_persistencia)
        self.assertIn("inputs.diagnosticar_contas_meta != true", passo_persistencia)

    def test_fila_ativa_tem_contrato_diario(self):
        fila = json.loads(FILA.read_text(encoding="utf-8"))
        self.assertEqual(fila["pacotes_por_dia"], 1)
        self.assertEqual(fila["horario"], "09:00")
        self.assertEqual(fila["limite_parte_segundos"], 59)

        datas = []
        ids = set()
        for pacote in fila["pacotes"]:
            self.assertNotIn(pacote["id"], ids)
            ids.add(pacote["id"])
            datas.append(date.fromisoformat(pacote["data"]))
            self.assertEqual(pacote["horario"], "09:00")
            self.assertIn(
                pacote["status"],
                {"pendente", "em_andamento", "erro", "concluido"},
            )
            self.assertGreaterEqual(len(pacote["partes"]), 1)
            self.assertLessEqual(len(pacote["partes"]), 10)
            self.assertEqual(
                [parte["ordem"] for parte in pacote["partes"]],
                list(range(1, len(pacote["partes"]) + 1)),
            )

        self.assertEqual(datas, sorted(datas))
        self.assertEqual(len(datas), len(set(datas)))

    def test_reels_tambem_persiste_o_marcador_depois_da_limpeza(self):
        texto = WORKFLOW_REELS.read_text(encoding="utf-8")
        antes = texto.index("Salvar resultado")
        limpeza = texto.index("Remover mídia temporária já publicada")
        depois = texto.index("Persistir marcadores da limpeza")
        self.assertLess(antes, limpeza)
        self.assertLess(limpeza, depois)
        self.assertGreaterEqual(texto.count("git add fila/fila-reels.json"), 2)

    def test_workflows_reservam_janela_para_o_escritor_local(self):
        stories = WORKFLOW.read_text(encoding="utf-8")
        reels = WORKFLOW_REELS.read_text(encoding="utf-8")
        self.assertNotIn('cron: "10 6 * * *"', stories)
        self.assertIn('cron: "7,57 6 * * *"', reels)
        self.assertIn('cron: "0 12 * * *"', reels)
        self.assertIn('cron: "7 12 * * *"', reels)
        self.assertNotIn('cron: "17 12 * * *"', reels)

    def test_todos_os_workflows_usam_o_lock_remoto_e_atualizam_main_depois(self):
        for caminho in (WORKFLOW, WORKFLOW_REELS):
            with self.subTest(workflow=caminho.name):
                texto = caminho.read_text(encoding="utf-8")
                adquirir = texto.index("lock_publicacao.py adquirir")
                atualizar = texto.index("git merge --ff-only origin/main")
                processar = texto.index("id: publicacao")
                liberar = texto.index("lock_publicacao.py liberar")
                self.assertLess(adquirir, atualizar)
                self.assertLess(atualizar, processar)
                self.assertGreater(liberar, processar)
                self.assertIn("name: Liberar lock remoto das filas IG/FB", texto)
                bloco_liberar = texto.split(
                    "name: Liberar lock remoto das filas IG/FB", 1
                )[1]
                self.assertIn("steps.persistencia.outcome != 'failure'", bloco_liberar)
                self.assertGreaterEqual(
                    texto.count("python push_confirmado.py --repositorio ."), 2
                )

    def test_falha_operacional_de_reels_deixa_action_vermelha(self):
        texto = WORKFLOW_REELS.read_text(encoding="utf-8")
        passo = texto.split(
            'name: "Resultado adicional: FALHA APÓS PROCESSAR A FILA"', 1
        )[1]
        self.assertIn("persistencia_limpeza.outcome == 'failure'", passo)
        self.assertIn("exit 1", passo)


if __name__ == "__main__":
    unittest.main()
