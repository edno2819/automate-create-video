"""Claude pela API Messages (/v1/messages), com cache de prompt.

O cache marca o sistema e o fim do histórico: cada passo de um agente relê a conversa inteira, e a releitura
do que já foi visto sai cerca de 10× mais barata. A busca na web usa a ferramenta nativa web_search, que pode
voltar pausada (pause_turn) e continua com a mesma mensagem.
"""

from __future__ import annotations

import json
from typing import Sequence

from nucleo.provedores import http
from nucleo.provedores.llm.base import (MAX_SAIDA, PEDIDO_BUSCA, TIMEOUT_LISTA_S, Chamada, Conversa, Passo,
                                        ProvedorLLM, Resultado, b64, com_fontes, somar_uso, uso_vazio)

API = "https://api.anthropic.com/v1/messages"
MODELOS = "https://api.anthropic.com/v1/models?limit=1000"
VERSAO = "2023-06-01"


def cabecalhos(chave: str) -> dict:
    return {"x-api-key": chave, "anthropic-version": VERSAO}


def blocos_imagem(imagens: Sequence[tuple[str, bytes]]) -> list[dict]:
    return [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64(d)}} for mime, d in imagens]


def uso_de(r: dict) -> dict:
    u = r.get("usage") or {}
    return {"entrada": u.get("input_tokens", 0), "saida": u.get("output_tokens", 0),
            "cache_leitura": u.get("cache_read_input_tokens") or 0,
            "cache_escrita": u.get("cache_creation_input_tokens") or 0,
            "buscas": (u.get("server_tool_use") or {}).get("web_search_requests", 0)}


class ConversaAnthropic(Conversa):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.mensagens: list[dict] = []

    def usuario(self, texto, imagens=()):
        self.mensagens.append({"role": "user", "content": [{"type": "text", "text": texto}, *blocos_imagem(imagens)]})

    def com_cache(self) -> list[dict]:
        """Marca o fim do histórico para o cache de prompt: cada passo relê tudo, e a releitura fica 10× mais barata."""
        mensagens = json.loads(json.dumps(self.mensagens))
        ultimo = mensagens[-1]["content"]
        if isinstance(ultimo, list) and ultimo:
            ultimo[-1]["cache_control"] = {"type": "ephemeral"}
        return mensagens

    def corpo(self) -> dict:
        corpo = {"model": self.modelo, "max_tokens": self.max_saida, "messages": self.com_cache(),
                 "system": [{"type": "text", "text": self.sistema, "cache_control": {"type": "ephemeral"}}]}
        if self.ferramentas:
            corpo["tools"] = [{"name": f["nome"], "description": f["descricao"], "input_schema": f["parametros"]}
                              for f in self.ferramentas]
        return corpo

    def passo(self):
        r = http.pedir_json(API, self.corpo(), cabecalhos(self.chave), provedor="anthropic")
        conteudo = r.get("content") or []
        self.mensagens.append({"role": "assistant", "content": conteudo})
        return Passo(texto="\n".join(b["text"] for b in conteudo if b.get("type") == "text").strip(),
                     chamadas=[Chamada(b["id"], b["name"], b.get("input") or {}) for b in conteudo
                               if b.get("type") == "tool_use"],
                     uso=uso_de(r), cortado=r.get("stop_reason") == "max_tokens")

    def resultados(self, lista):
        self.mensagens.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r.id,
             "content": [{"type": "text", "text": r.texto or "(sem saída)"}, *blocos_imagem(r.imagens)]} for r in lista]})


class ProvedorAnthropic(ProvedorLLM):
    id = "anthropic"
    nome = "Claude (Anthropic)"

    def conversa(self, modelo, sistema, ferramentas, max_saida=MAX_SAIDA):
        return ConversaAnthropic(self.chave, modelo, sistema, ferramentas, max_saida)

    def buscar_na_web(self, modelo, consulta):
        mensagens: list[dict] = [{"role": "user", "content": PEDIDO_BUSCA.format(consulta=consulta)}]
        uso, textos, fontes = uso_vazio(), [], {}
        for _ in range(4):  # pause_turn: a busca longa volta pausada e continua com a mesma mensagem
            r = http.pedir_json(API, {"model": modelo, "max_tokens": 4096, "messages": mensagens,
                                      "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]},
                                cabecalhos(self.chave), provedor="anthropic")
            u = uso_de(r)
            somar_uso(uso, {"entrada": u["entrada"], "saida": u["saida"], "buscas": u["buscas"]})
            for b in r.get("content") or []:
                if b.get("type") == "text":
                    textos.append(b["text"])
                    for c in b.get("citations") or []:
                        fontes.setdefault(c.get("url", ""), c.get("title", ""))
            if r.get("stop_reason") != "pause_turn":
                break
            mensagens.append({"role": "assistant", "content": r["content"]})
        return com_fontes("".join(textos), fontes), uso

    def listar_modelos(self):
        r = http.pedir_json(MODELOS, None, cabecalhos(self.chave), metodo="GET", timeout=TIMEOUT_LISTA_S,
                            provedor="anthropic")
        return [m["id"] for m in r.get("data") or []]
