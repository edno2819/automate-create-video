"""GPT pela API Responses (/v1/responses).

O histórico fica guardado na OpenAI (store: true): cada passo manda só o que é novo (as respostas das
ferramentas, a próxima mensagem) com o previous_response_id do passo anterior. Imagens lidas por uma
ferramenta não cabem num function_call_output: vão numa mensagem do usuário logo depois.
"""

from __future__ import annotations

import json
import re

from nucleo.provedores import http
from nucleo.provedores.llm.base import (FORA_DO_CHAT, MAX_SAIDA, PEDIDO_BUSCA, TIMEOUT_LISTA_S, Chamada, Conversa,
                                        Passo, ProvedorLLM, b64, com_fontes, uso_vazio)

API = "https://api.openai.com/v1/responses"
MODELOS = "https://api.openai.com/v1/models"


def cabecalhos(chave: str) -> dict:
    return {"Authorization": f"Bearer {chave}"}


def entrada_usuario(texto: str, imagens) -> dict:
    partes = [{"type": "input_text", "text": texto}] if texto else []
    partes += [{"type": "input_image", "image_url": f"data:{mime};base64,{b64(d)}"} for mime, d in imagens]
    return {"role": "user", "content": partes}


def uso_de(r: dict) -> dict:
    u = r.get("usage") or {}
    cache = (u.get("input_tokens_details") or {}).get("cached_tokens", 0)
    return {**uso_vazio(), "entrada": u.get("input_tokens", 0) - cache, "saida": u.get("output_tokens", 0),
            "cache_leitura": cache}


class ConversaOpenAI(Conversa):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.anterior: str | None = None  # previous_response_id: o histórico fica guardado na OpenAI
        self.pendentes: list[dict] = []

    def usuario(self, texto, imagens=()):
        self.pendentes.append(entrada_usuario(texto, imagens))

    def corpo(self) -> dict:
        corpo = {"model": self.modelo, "instructions": self.sistema, "input": self.pendentes, "store": True,
                 "max_output_tokens": self.max_saida}
        if self.ferramentas:
            corpo["tools"] = [{"type": "function", "name": f["nome"], "description": f["descricao"],
                               "parameters": f["parametros"], "strict": False} for f in self.ferramentas]
        if self.anterior:
            corpo["previous_response_id"] = self.anterior
        return corpo

    def passo(self):
        r = http.pedir_json(API, self.corpo(), cabecalhos(self.chave), provedor="openai")
        self.anterior, self.pendentes = r.get("id"), []
        textos, chamadas = [], []
        for item in r.get("output") or []:
            if item.get("type") == "message":
                textos += [c.get("text", "") for c in item.get("content") or [] if c.get("type") == "output_text"]
            elif item.get("type") == "function_call":
                try:
                    args = json.loads(item.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {"_erro": f"argumentos que não são JSON: {item.get('arguments', '')[:200]}"}
                chamadas.append(Chamada(item["call_id"], item["name"], args))
        cortado = (r.get("status") == "incomplete"
                   and (r.get("incomplete_details") or {}).get("reason") == "max_output_tokens")
        return Passo("\n".join(textos).strip(), chamadas, uso_de(r), cortado)

    def resultados(self, lista):
        for r in lista:
            self.pendentes.append({"type": "function_call_output", "call_id": r.id, "output": r.texto or "(sem saída)"})
        imagens = [img for r in lista for img in r.imagens]
        if imagens:
            self.pendentes.append(entrada_usuario("Imagens lidas pela ferramenta Read:", imagens))


class ProvedorOpenAI(ProvedorLLM):
    id = "openai"
    nome = "GPT (OpenAI)"

    def conversa(self, modelo, sistema, ferramentas, max_saida=MAX_SAIDA):
        return ConversaOpenAI(self.chave, modelo, sistema, ferramentas, max_saida)

    def buscar_na_web(self, modelo, consulta):
        r = http.pedir_json(API, {"model": modelo, "input": PEDIDO_BUSCA.format(consulta=consulta),
                                  "tools": [{"type": "web_search"}]}, cabecalhos(self.chave), provedor="openai")
        textos, fontes = [], {}
        for item in r.get("output") or []:
            for c in item.get("content") or [] if item.get("type") == "message" else []:
                if c.get("type") == "output_text":
                    textos.append(c["text"])
                    for a in c.get("annotations") or []:
                        if a.get("type") == "url_citation":
                            fontes.setdefault(a.get("url", ""), a.get("title", ""))
        uso = {**uso_de(r), "buscas": sum(1 for i in r.get("output") or [] if i.get("type") == "web_search_call")}
        return com_fontes("\n".join(textos), fontes), uso

    def listar_modelos(self):
        r = http.pedir_json(MODELOS, None, cabecalhos(self.chave), metodo="GET", timeout=TIMEOUT_LISTA_S,
                            provedor="openai")
        ids = [m["id"] for m in sorted(r.get("data") or [], key=lambda m: m.get("created", 0), reverse=True)]
        return [i for i in ids if re.match(r"(gpt-|o\d|chatgpt)", i) and not FORA_DO_CHAT.search(i)]
