"""Conexões com os provedores: as chaves cadastradas, o teste de cada uma e o modelo que roda cada etapa.

As chaves ficam no .env (ferramentas/chaves.py); aqui só passam mascaradas. A escolha de modelo por etapa, a
lista de modelos de cada provedor (do último teste) e o resultado dos testes ficam em dados/conexoes.json.

Modos de uma etapa:
  claude-code      o Claude Code com o login atual, seguindo a skill (o padrão, como sempre foi)
  claude-code-api  o Claude Code cobrando pela chave da Anthropic cadastrada
  anthropic, openai, gemini  o motor de agentes (web/motor.py) com o modelo escolhido, pela API do provedor
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "ferramentas"))
import chaves  # noqa: E402
import llm  # noqa: E402

CONFIG = RAIZ / "dados" / "conexoes.json"
MODOS = {
    "claude-code": "Claude Code (login atual)",
    "claude-code-api": "Claude Code com a chave da API",
    "anthropic": "Claude pela API",
    "openai": "GPT pela API",
    "gemini": "Gemini pela API",
}
# as etapas que usam LLM; a produção usa só para o publicador (o resto são scripts)
ETAPAS_LLM = {
    "sugestoes": "completar o pedido", "pesquisa": "analista, pesquisadores", "angulos": "estrategista",
    "roteiro": "roteirista e crítico", "cortes": "cortador e crítico", "visual": "diretor visual",
    "producao": "scripts e publicador",
}
SUGERIDOS = {"anthropic": "claude-opus-5-5", "openai": "gpt-6.1-sol", "gemini": "gemini-3.8-flash"}
MODELOS_CLAUDE_CODE = ["", "opus", "sonnet", "haiku"]  # "" = o padrão do Claude Code


def ler() -> dict:
    try:
        dados = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        dados = {}
    dados.setdefault("etapas", {})
    dados.setdefault("modelos", {})
    dados.setdefault("testes", {})
    return dados


def gravar(dados: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rota(etapa: str) -> dict:
    """O modo e o modelo da etapa (avaliar segue o roteiro; publicação, a produção)."""
    etapa = {"avaliar": "roteiro", "publicacao": "producao"}.get(etapa, etapa)
    escolha = ler()["etapas"].get(etapa) or {}
    return {"modo": escolha.get("modo") or "claude-code", "modelo": escolha.get("modelo") or ""}


def definir_rotas(novas: dict) -> dict:
    dados = ler()
    for etapa, escolha in novas.items():
        if etapa not in ETAPAS_LLM:
            raise ValueError(f"etapa desconhecida: {etapa}")
        modo = escolha.get("modo") or "claude-code"
        if modo not in MODOS:
            raise ValueError(f"modo desconhecido: {modo}")
        modelo = (escolha.get("modelo") or "").strip()
        if modo in llm.PROVEDORES_LLM and not modelo:
            modelo = SUGERIDOS[modo]
        dados["etapas"][etapa] = {"modo": modo, "modelo": modelo}
    gravar(dados)
    return dados["etapas"]


def problema_da_rota(r: dict) -> str | None:
    """Por que a rota não pode rodar agora (sem chave), ou None."""
    provedor = "anthropic" if r["modo"] == "claude-code-api" else r["modo"]
    if provedor in chaves.PROVEDORES and not chaves.ler(provedor):
        return f"a etapa está configurada para {MODOS[r['modo']]}, mas não há chave de {chaves.PROVEDORES[provedor]['nome']}: cadastre em Configurações > Chaves de API"
    return None


def provedores() -> list[dict]:
    dados = ler()
    saida = []
    for id_, p in chaves.PROVEDORES.items():
        saida.append({"id": id_, "nome": p["nome"], "variavel": p["variavel"], "usos": p["usos"], "painel": p["painel"],
                      "origem": chaves.origem(id_), "mascara": chaves.mascarar(chaves.ler(id_)),
                      "teste": dados["testes"].get(id_), "modelos": (dados["modelos"].get(id_) or {}).get("lista", [])})
    return saida


def _get(url: str, cabecalhos: dict) -> dict:
    req = urllib.request.Request(url, headers=cabecalhos)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def testar(provedor: str) -> dict:
    """Uma chamada barata que só funciona com a chave válida. Os LLMs listam os modelos (a lista alimenta a
    escolha por etapa); a ElevenLabs atualiza a lista de vozes da conta."""
    chave = chaves.ler(provedor)
    if not chave:
        resultado = {"ok": False, "mensagem": "sem chave cadastrada"}
    else:
        try:
            if provedor in llm.PROVEDORES_LLM:
                modelos = llm.listar_modelos(provedor, chave)
                resultado = {"ok": True, "mensagem": f"{len(modelos)} modelos de texto disponíveis"}
                dados = ler()
                dados["modelos"][provedor] = {"lista": modelos, "atualizado_em": datetime.now().isoformat(timespec="seconds")}
                gravar(dados)
            elif provedor == "elevenlabs":
                sys.path.insert(0, str(RAIZ / "skills" / "produzir-video" / "scripts"))
                import narrar
                vozes = narrar.atualizar_vozes_elevenlabs(chave)
                resultado = {"ok": True, "mensagem": f"{len(vozes)} vozes na conta"}
            elif provedor == "youtube":
                d = _get("https://www.googleapis.com/youtube/v3/videos?part=id&chart=mostPopular&maxResults=1&regionCode=BR"
                         f"&key={chave}", {})
                resultado = {"ok": bool(d.get("items") is not None), "mensagem": "a busca de trends usa a API (cota diária de 10.000 unidades)"}
            else:
                resultado = {"ok": False, "mensagem": "provedor desconhecido"}
        except (llm.ErroLLM, urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as e:
            mensagem = str(e)
            if isinstance(e, urllib.error.HTTPError):
                mensagem = f"HTTP {e.code}: chave recusada ou sem permissão" if e.code in (401, 403) else f"HTTP {e.code}"
            resultado = {"ok": False, "mensagem": mensagem.replace(chave, "•••")[:300]}
    resultado["quando"] = datetime.now().isoformat(timespec="seconds")
    dados = ler()
    dados["testes"][provedor] = resultado
    gravar(dados)
    return resultado
