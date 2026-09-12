"""Publica as peças pendentes e vencidas de Reels no Instagram e Facebook.

As mídias não entram no histórico Git. Cada item aponta para um asset temporário
da release ``fila-instagram-facebook``; o runner o baixa somente para o upload
resumível da Página do Facebook. Uma execução recupera atrasos processando os
itens vencidos em ordem cronológica, até um limite de segurança configurável.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
FILA_FILE = ROOT / "fila" / "fila-reels.json"
BRT = timezone(timedelta(hours=-3))
GRAPH_BASE = f"https://graph.facebook.com/{os.getenv('META_GRAPH_VERSION', 'v23.0')}"
PLATAFORMAS = ("instagram", "facebook")
FACEBOOK_ATIVO = True  # pages_manage_posts liberado em Standard Access (05/09/2026)
MAX_ITENS_POR_EXECUCAO_PADRAO = 10
RESULTADO_PUBLICADO = "PUBLICADO"
RESULTADO_NENHUM_DEVIDO = "NENHUM_REEL_DEVIDO"
RESULTADO_RECONCILIADO = "RECONCILIADO_SEM_NOVA_PUBLICACAO"
RESULTADO_FALHA = "FALHA"


def obrigatoria(nome: str) -> str:
    valor = os.getenv(nome, "").strip()
    if not valor:
        raise RuntimeError(f"Segredo obrigatório ausente: {nome}")
    return valor


def salvar_fila(fila: dict) -> None:
    FILA_FILE.write_text(json.dumps(fila, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def graph_post(caminho: str, dados: dict, timeout: int = 60) -> dict:
    resposta = requests.post(f"{GRAPH_BASE}/{caminho}", data=dados, timeout=timeout)
    if not resposta.ok:
        raise RuntimeError(f"Meta HTTP {resposta.status_code}: {resposta.text}")
    return resposta.json()


def graph_get(caminho: str, parametros: dict, timeout: int = 30) -> dict:
    resposta = requests.get(f"{GRAPH_BASE}/{caminho}", params=parametros, timeout=timeout)
    if not resposta.ok:
        raise RuntimeError(f"Meta HTTP {resposta.status_code}: {resposta.text}")
    return resposta.json()


def aguardar_instagram(container_id: str, token: str) -> None:
    for tentativa in range(36):
        status = graph_get(container_id, {"fields": "status_code,status", "access_token": token})
        codigo = status.get("status_code", "")
        print(f"Instagram [{tentativa + 1}/36]: {codigo}")
        if codigo == "FINISHED":
            return
        if codigo in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Instagram não processou o Reel: {status}")
        time.sleep(10)
    raise TimeoutError("Instagram demorou mais de seis minutos para processar.")


def baixar_midia(midia: dict) -> Path:
    """Baixa e confere o asset transitório antes do upload ao Facebook."""
    url = midia.get("url_publica", "")
    nome = midia.get("asset", "")
    if not url or not nome:
        raise RuntimeError("A fila não tem URL pública e asset da mídia.")
    destino = Path(os.getenv("MEDIA_CACHE_DIR", ROOT / ".cache")) / nome
    destino.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    tamanho = 0
    with requests.get(url, stream=True, timeout=(30, 900)) as resposta:
        if not resposta.ok:
            raise RuntimeError(f"Não foi possível baixar {nome}: HTTP {resposta.status_code}")
        with destino.open("wb") as arquivo:
            for bloco in resposta.iter_content(chunk_size=1024 * 1024):
                if bloco:
                    arquivo.write(bloco)
                    digest.update(bloco)
                    tamanho += len(bloco)
    if midia.get("sha256") and digest.hexdigest().lower() != midia["sha256"].lower():
        destino.unlink(missing_ok=True)
        raise RuntimeError(f"SHA-256 divergente para {nome}.")
    if midia.get("tamanho_bytes") and tamanho != int(midia["tamanho_bytes"]):
        destino.unlink(missing_ok=True)
        raise RuntimeError(f"Tamanho divergente para {nome}.")
    return destino


def publicar_instagram(item: dict) -> str:
    token, ig_id = obrigatoria("IG_ACCESS_TOKEN"), obrigatoria("IG_BUSINESS_ID")
    midia = item["midia"]
    container = graph_post(f"{ig_id}/media", {
        "media_type": "REELS", "video_url": midia["url_publica"],
        "caption": item["instagram"]["legenda"], "share_to_feed": "true", "access_token": token,
    })
    container_id = container.get("id")
    if not container_id:
        raise RuntimeError(f"Container do Instagram sem ID: {container}")
    aguardar_instagram(container_id, token)
    publicado = graph_post(f"{ig_id}/media_publish", {"creation_id": container_id, "access_token": token})
    if not publicado.get("id"):
        raise RuntimeError(f"Instagram não retornou o post: {publicado}")
    return str(publicado["id"])


def publicar_facebook(item: dict) -> str:
    token_sistema, page_id = obrigatoria("FB_PAGE_ACCESS_TOKEN"), obrigatoria("FB_PAGE_ID")
    token = graph_get(page_id, {"fields": "access_token", "access_token": token_sistema}).get("access_token")
    if not token:
        raise RuntimeError("A Meta não retornou o token de acesso da Página.")
    caminho_video = baixar_midia(item["midia"])
    try:
        inicio = graph_post(f"{page_id}/video_reels", {"upload_phase": "start", "access_token": token})
        video_id, upload_url = inicio.get("video_id"), inicio.get("upload_url")
        if not video_id or not upload_url:
            raise RuntimeError(f"Facebook não iniciou o upload: {inicio}")
        tamanho = caminho_video.stat().st_size
        with caminho_video.open("rb") as arquivo:
            resposta = requests.post(upload_url, headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(tamanho)}, data=arquivo, timeout=900)
        if not resposta.ok:
            raise RuntimeError(f"Facebook falhou no upload ({resposta.status_code}): {resposta.text}")
        fim = graph_post(f"{page_id}/video_reels", {
            "upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED",
            "description": item["facebook"]["legenda"], "access_token": token,
        })
        if not fim.get("success"):
            raise RuntimeError(f"Facebook não confirmou a publicação: {fim}")
        return str(video_id)
    finally:
        caminho_video.unlink(missing_ok=True)


def executar(item: dict, plataforma: str, funcao) -> str | None:
    dados = item[plataforma]
    if dados.get("status") == "publicado":
        return None
    try:
        publicacao_id = str(funcao(item))
        dados.update({"status": "publicado", "id": publicacao_id, "publicado_em": datetime.now(BRT).isoformat()})
        dados.pop("erro", None)
        return publicacao_id
    except Exception as erro:
        dados.update(
            {
                "status": "erro",
                "erro": str(erro),
                "ultima_tentativa_em": datetime.now(BRT).isoformat(),
                "tentativas": int(dados.get("tentativas", 0)) + 1,
            }
        )
        print(f"ERRO {plataforma}: {erro}")
        return None


def limite_por_execucao() -> int:
    try:
        limite = int(os.getenv("MAX_ITENS_POR_EXECUCAO", str(MAX_ITENS_POR_EXECUCAO_PADRAO)))
    except ValueError as erro:
        raise RuntimeError("MAX_ITENS_POR_EXECUCAO deve ser um número inteiro.") from erro
    if limite < 1:
        raise RuntimeError("MAX_ITENS_POR_EXECUCAO deve ser maior que zero.")
    return limite


def proximos_itens(fila: dict, agora: datetime | None = None) -> list[dict]:
    data_forcada = os.getenv("DATA_PUBLICACAO", "").strip()
    horario_forcado = os.getenv("HORARIO_PUBLICACAO", "").strip()
    if bool(data_forcada) != bool(horario_forcado):
        raise RuntimeError("Informe data e horário juntos para executar manualmente.")
    conteudos = fila.get("conteudos", [])
    if data_forcada:
        encontrados = [x for x in conteudos if x["data"] == data_forcada and x["horario"] == horario_forcado and x.get("status") != "concluido"]
        if len(encontrados) > 1:
            raise RuntimeError("A fila tem mais de um Reel para esta data e horário.")
        return encontrados[:1]
    agora = agora or datetime.now(BRT)
    devidos = []
    for item in conteudos:
        if item.get("status") == "concluido":
            continue
        agendado = datetime.fromisoformat(f"{item['data']}T{item['horario']}:00").replace(tzinfo=BRT)
        if agendado <= agora:
            devidos.append((agendado, item))
    devidos.sort(key=lambda par: par[0])
    return [item for _, item in devidos[:limite_por_execucao()]]


MAX_TENTATIVAS_POR_ITEM = 3

# Erro que nunca vai passar por mais que se tente: o arquivo em si nao serve.
# O 2207082 foi o que prendeu a fila em 11/09/2026, com o video 23.mp4 corrompido.
MARCAS_DE_ERRO_PERMANENTE = (
    "2207082",   # Media upload has failed
    "2207026",   # formato de video nao suportado
    "2207020",   # midia invalida ou corrompida
    "media upload has failed",
    "unsupported",
    "invalid media",
    "não suportado",
)


def erro_permanente(item: dict) -> str | None:
    """Diz por que este item nunca vai publicar, ou None se ainda vale tentar."""

    for plataforma in ("instagram", "facebook"):
        dados = item.get(plataforma) or {}
        if dados.get("status") != "erro":
            continue
        texto = str(dados.get("erro", "")).lower()
        for marca in MARCAS_DE_ERRO_PERMANENTE:
            if marca in texto:
                return f"{plataforma}: o arquivo foi recusado ({marca})"
        if int(dados.get("tentativas", 0)) >= MAX_TENTATIVAS_POR_ITEM:
            return f"{plataforma}: falhou {dados['tentativas']} vezes seguidas"
    return None


def item_com_erro(item: dict) -> bool:
    return item["instagram"].get("status") == "erro" or (
        FACEBOOK_ATIVO and item["facebook"].get("status") == "erro"
    )


def proximo_item_pendente(fila: dict) -> dict | None:
    pendentes = [item for item in fila.get("conteudos", []) if item.get("status") != "concluido"]
    if not pendentes:
        return None
    return min(pendentes, key=lambda item: (item["data"], item["horario"], item.get("id", "")))


def erros_do_item(item: dict) -> list[str]:
    erros = []
    for plataforma in PLATAFORMAS:
        if item[plataforma].get("status") == "erro":
            erros.append(f"{plataforma}: {item[plataforma].get('erro', 'erro sem detalhe')}")
    return erros


def processar_fila(fila: dict, agora: datetime | None = None) -> dict:
    itens = proximos_itens(fila, agora=agora)
    if not itens:
        print("Nenhum Reel pendente e devido para publicação.")
        return {
            "resultado": RESULTADO_NENHUM_DEVIDO,
            "reels_devidos": 0,
            "reels_concluidos": 0,
            "publicacoes": [],
            "erros": [],
            "proximo": proximo_item_pendente(fila),
        }

    publicacoes = []
    postos_de_lado = []
    adiados = []
    reels_concluidos = 0
    print(f"{len(itens)} Reel(s) vencido(s) serão processado(s) nesta execução.")
    for indice, item in enumerate(itens, start=1):
        print(f"Processando [{indice}/{len(itens)}] {item['data']} {item['horario']} ({item['id']})")

        instagram_id = executar(item, "instagram", publicar_instagram)
        if instagram_id:
            publicacoes.append({"reel": item["id"], "rede": "Instagram", "id": instagram_id})
        salvar_fila(fila)

        if FACEBOOK_ATIVO:
            facebook_id = executar(item, "facebook", publicar_facebook)
            if facebook_id:
                publicacoes.append({"reel": item["id"], "rede": "Facebook", "id": facebook_id})
        else:
            item["facebook"]["status"] = "pausado"
            item["facebook"].pop("erro", None)
        salvar_fila(fila)

        concluiu = item["instagram"].get("status") == "publicado" and (
            not FACEBOOK_ATIVO or item["facebook"].get("status") == "publicado"
        )
        if concluiu:
            item.update({"status": "concluido", "concluido_em": datetime.now(BRT).isoformat()})
            reels_concluidos += 1
        salvar_fila(fila)

        if item_com_erro(item):
            # Regra do Cristiano em 12/09/2026: nunca travar a fila. Antes daqui
            # o robo voltava e o item ficava barrando todos os outros, sem prazo.
            motivo = erro_permanente(item)
            if motivo:
                item["status"] = "com_defeito"
                item["motivo_defeito"] = motivo
                item["posto_de_lado_em"] = datetime.now(BRT).isoformat()
                print(f"POSTO DE LADO: {item['id']} — {motivo}")
                postos_de_lado.append({"reel": item["id"], "motivo": motivo})
            else:
                print(f"FALHOU, tentaremos de novo: {item['id']}")
                adiados.append({"reel": item["id"], "erros": erros_do_item(item)})
            salvar_fila(fila)
            continue

    # Qualquer item que falhou deixa a rodada VERMELHA, mesmo que outros tenham
    # publicado. A fila anda, mas o Cristiano precisa enxergar que algo deu errado.
    if postos_de_lado or adiados:
        resultado_final = RESULTADO_FALHA
    elif publicacoes:
        resultado_final = RESULTADO_PUBLICADO
    else:
        resultado_final = RESULTADO_RECONCILIADO
    return {
        "resultado": resultado_final,
        "reels_devidos": len(itens),
        "reels_concluidos": reels_concluidos,
        "publicacoes": publicacoes,
        "postos_de_lado": postos_de_lado,
        "adiados": adiados,
        "erros": [e for a in adiados for e in a["erros"]],
        "proximo": proximo_item_pendente(fila),
    }


def mensagem_resultado(relatorio: dict) -> str:
    resultado = relatorio["resultado"]
    publicacoes = relatorio.get("publicacoes", [])
    instagram = sum(publicacao["rede"] == "Instagram" for publicacao in publicacoes)
    facebook = sum(publicacao["rede"] == "Facebook" for publicacao in publicacoes)
    if resultado == RESULTADO_PUBLICADO:
        return (
            f"Publicação confirmada pela Meta: {relatorio['reels_concluidos']} Reel(s) concluído(s), "
            f"{instagram} no Instagram e {facebook} no Facebook nesta execução."
        )
    if resultado == RESULTADO_NENHUM_DEVIDO:
        return "Nenhuma publicação realizada: não havia Reel pendente e devido."
    if resultado == RESULTADO_RECONCILIADO:
        return "Fila reconciliada, mas nenhuma nova publicação foi realizada nesta execução."
    detalhes = "; ".join(relatorio.get("erros", [])) or "erro sem detalhe"
    return f"Falha na publicação: {detalhes}."


def resumo_markdown(relatorio: dict) -> str:
    titulos = {
        RESULTADO_PUBLICADO: "✅ PUBLICAÇÃO CONFIRMADA",
        RESULTADO_NENHUM_DEVIDO: "🟡 NENHUM REEL DEVIDO — NADA FOI PUBLICADO",
        RESULTADO_RECONCILIADO: "🟡 FILA RECONCILIADA — NADA NOVO FOI PUBLICADO",
        RESULTADO_FALHA: "❌ FALHA NA PUBLICAÇÃO",
    }
    publicacoes = relatorio.get("publicacoes", [])
    instagram = sum(publicacao["rede"] == "Instagram" for publicacao in publicacoes)
    facebook = sum(publicacao["rede"] == "Facebook" for publicacao in publicacoes)
    linhas = [
        f"## {titulos.get(relatorio['resultado'], relatorio['resultado'])}",
        "",
        mensagem_resultado(relatorio),
        f"Executado em `{relatorio['executado_em']}` (horário de Brasília).",
        "",
        "| Estado | Reels devidos | Reels concluídos | Instagram | Facebook |",
        "|---|---:|---:|---:|---:|",
        (
            f"| `{relatorio['resultado']}` | {relatorio.get('reels_devidos', 0)} | "
            f"{relatorio.get('reels_concluidos', 0)} | {instagram} | {facebook} |"
        ),
    ]
    if publicacoes:
        linhas.extend(["", "### IDs confirmados pela Meta", "", "| Reel | Rede | ID |", "|---|---|---|"])
        linhas.extend(
            f"| `{publicacao['reel']}` | {publicacao['rede']} | `{publicacao['id']}` |"
            for publicacao in publicacoes
        )
    if relatorio.get("erros"):
        linhas.extend(["", "### Erros", ""])
        linhas.extend(f"- {erro}" for erro in relatorio["erros"])
    proximo = relatorio.get("proximo")
    if proximo:
        linhas.extend([
            "",
            f"Próximo item pendente: `{proximo['data']} {proximo['horario']}` (`{proximo['id']}`).",
        ])
    return "\n".join(linhas) + "\n"


def registrar_resultado(relatorio: dict) -> None:
    relatorio = dict(relatorio)
    relatorio.setdefault("executado_em", datetime.now(BRT).isoformat(timespec="seconds"))
    mensagem = mensagem_resultado(relatorio)
    publicacoes = relatorio.get("publicacoes", [])
    saidas = {
        "resultado": relatorio["resultado"],
        "mensagem": mensagem,
        "reels_devidos": relatorio.get("reels_devidos", 0),
        "reels_concluidos": relatorio.get("reels_concluidos", 0),
        "instagram_publicados": sum(publicacao["rede"] == "Instagram" for publicacao in publicacoes),
        "facebook_publicados": sum(publicacao["rede"] == "Facebook" for publicacao in publicacoes),
        "executado_em": relatorio["executado_em"],
    }
    print(f"RESULTADO_INEQUIVOCO={relatorio['resultado']}")
    print(mensagem)
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
        "reels_devidos": 0,
        "reels_concluidos": 0,
        "publicacoes": [],
        "erros": [f"erro inesperado: {erro}"],
        "proximo": None,
    }


def main() -> int:
    try:
        fila = json.loads(FILA_FILE.read_text(encoding="utf-8"))
        relatorio = processar_fila(fila)
    except Exception as erro:
        registrar_resultado(relatorio_falha_inesperada(erro))
        raise
    registrar_resultado(relatorio)
    return 1 if relatorio["resultado"] == RESULTADO_FALHA else 0


if __name__ == "__main__":
    raise SystemExit(main())
