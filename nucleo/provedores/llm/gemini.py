"""Gemini pela API generateContent.

O histórico guarda o conteúdo do modelo exatamente como veio: as partes de raciocínio levam assinaturas
(thoughtSignature) que precisam voltar intactas no passo seguinte, senão a API recusa a chamada de ferramenta.
O limite de saída é o dobro do pedido porque, no Gemini, os tokens de raciocínio contam nele.

As ferramentas vão em `parameters` (o subconjunto OpenAPI que o Gemini aceita). Um esquema com palavras fora
desse subconjunto (additionalProperties, oneOf, $ref, const, tipo em lista...), como os esquemas de entrega
estruturada, vai em `parametersJsonSchema`, que aceita JSON Schema inteiro.
"""

from __future__ import annotations

import re

from nucleo.provedores import http
from nucleo.provedores.erros import Bloqueado
from nucleo.provedores.llm.base import (FORA_DO_CHAT, MAX_SAIDA, PEDIDO_BUSCA, TIMEOUT_LISTA_S, Chamada, Conversa,
                                        Passo, ProvedorLLM, b64, com_fontes, uso_vazio)

API = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
MODELOS = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
OPENAPI = {"type", "format", "description", "nullable", "enum", "maxItems", "minItems", "properties", "required",
           "minProperties", "maxProperties", "minLength", "maxLength", "pattern", "example", "anyOf",
           "propertyOrdering", "default", "items", "minimum", "maximum", "title"}


def cabecalhos(chave: str) -> dict:
    return {"x-goog-api-key": chave}


def partes_imagem(imagens) -> list[dict]:
    return [{"inlineData": {"mimeType": mime, "data": b64(d)}} for mime, d in imagens]


def so_openapi(esquema) -> bool:
    """O esquema cabe no subconjunto OpenAPI do campo `parameters`?"""
    if isinstance(esquema, list):
        return all(so_openapi(e) for e in esquema)
    if not isinstance(esquema, dict):
        return True
    if set(esquema) - OPENAPI or isinstance(esquema.get("type"), list):
        return False
    filhos = list((esquema.get("properties") or {}).values()) + list(esquema.get("anyOf") or [])
    if "items" in esquema:
        filhos.append(esquema["items"])
    return all(so_openapi(f) for f in filhos)


def declaracao(f: dict) -> dict:
    campo = "parameters" if so_openapi(f["parametros"]) else "parametersJsonSchema"
    return {"name": f["nome"], "description": f["descricao"], campo: f["parametros"]}


def bloqueio(r: dict) -> None:
    if motivo := (r.get("promptFeedback") or {}).get("blockReason"):
        raise Bloqueado(f"pedido bloqueado pelo filtro do Gemini ({motivo})", "gemini")


class ConversaGemini(Conversa):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.conteudos: list[dict] = []
        self.contador = 0

    def usuario(self, texto, imagens=()):
        self.conteudos.append({"role": "user", "parts": [{"text": texto}, *partes_imagem(imagens)]})

    def corpo(self) -> dict:
        corpo = {"systemInstruction": {"parts": [{"text": self.sistema}]}, "contents": self.conteudos,
                 "generationConfig": {"maxOutputTokens": self.max_saida * 2}}
        if self.ferramentas:
            corpo["tools"] = [{"functionDeclarations": [declaracao(f) for f in self.ferramentas]}]
        return corpo

    def passo(self):
        r = http.pedir_json(API.format(modelo=self.modelo), self.corpo(), cabecalhos(self.chave), provedor="gemini")
        bloqueio(r)
        candidato = (r.get("candidates") or [{}])[0]
        conteudo = candidato.get("content") or {"role": "model", "parts": []}
        conteudo.setdefault("role", "model")
        conteudo.setdefault("parts", [])
        self.conteudos.append(conteudo)  # como veio: as assinaturas de raciocínio precisam voltar intactas
        textos, chamadas = [], []
        for parte in conteudo["parts"]:
            if "functionCall" in parte:
                fc = parte["functionCall"]
                self.contador += 1
                chamadas.append(Chamada(fc.get("id") or f"{fc['name']}-{self.contador}", fc["name"], fc.get("args") or {}))
            elif parte.get("text") and not parte.get("thought"):
                textos.append(parte["text"])
        u = r.get("usageMetadata") or {}
        cache = u.get("cachedContentTokenCount", 0)
        uso = {**uso_vazio(), "entrada": u.get("promptTokenCount", 0) - cache + u.get("toolUsePromptTokenCount", 0),
               "saida": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0), "cache_leitura": cache}
        return Passo("\n".join(textos).strip(), chamadas, uso, candidato.get("finishReason") == "MAX_TOKENS")

    def resultados(self, lista):
        partes = []
        for r in lista:
            resposta = {"name": r.nome, "response": {"resultado": r.texto or "(sem saída)"}}
            if not re.fullmatch(rf"{re.escape(r.nome)}-\d+", r.id):  # o id inventado aqui não volta para a API
                resposta["id"] = r.id
            partes.append({"functionResponse": resposta})
        partes += partes_imagem([img for r in lista for img in r.imagens])
        self.conteudos.append({"role": "user", "parts": partes})


class ProvedorGemini(ProvedorLLM):
    id = "gemini"
    nome = "Gemini (Google)"

    def conversa(self, modelo, sistema, ferramentas, max_saida=MAX_SAIDA):
        return ConversaGemini(self.chave, modelo, sistema, ferramentas, max_saida)

    def buscar_na_web(self, modelo, consulta):
        r = http.pedir_json(API.format(modelo=modelo),
                            {"contents": [{"role": "user", "parts": [{"text": PEDIDO_BUSCA.format(consulta=consulta)}]}],
                             "tools": [{"google_search": {}}]}, cabecalhos(self.chave), provedor="gemini")
        bloqueio(r)
        candidato = (r.get("candidates") or [{}])[0]
        texto = "".join(p.get("text", "") for p in (candidato.get("content") or {}).get("parts", []) if not p.get("thought"))
        fontes: dict[str, str] = {}
        for pedaco in (candidato.get("groundingMetadata") or {}).get("groundingChunks") or []:
            web = pedaco.get("web") or {}
            fontes.setdefault(web.get("uri", ""), web.get("title", ""))
        u = r.get("usageMetadata") or {}
        uso = {**uso_vazio(), "entrada": u.get("promptTokenCount", 0) + u.get("toolUsePromptTokenCount", 0),
               "saida": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0),
               "buscas": 1 if (candidato.get("groundingMetadata") or {}).get("webSearchQueries") else 0}
        return com_fontes(texto, fontes), uso

    def listar_modelos(self):
        r = http.pedir_json(MODELOS, None, cabecalhos(self.chave), metodo="GET", timeout=TIMEOUT_LISTA_S,
                            provedor="gemini")
        return [m["name"].removeprefix("models/") for m in r.get("models") or []
                if "generateContent" in (m.get("supportedGenerationMethods") or [])
                and m["name"].startswith("models/gemini") and not FORA_DO_CHAT.search(m["name"])]
