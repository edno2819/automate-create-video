"""Cliente único para os LLMs dos provedores (Claude, GPT e Gemini), com chamada de ferramentas.

Cada provedor tem uma Conversa que guarda o histórico no formato da própria API e expõe a mesma interface:

    conversa = nova_conversa("openai", "gpt-6.1-sol", sistema, ferramentas)
    conversa.usuario("Leia o brief e escreva o dossiê.")
    passo = conversa.passo()              # texto, chamadas de ferramenta e uso de tokens
    conversa.resultados([Resultado(...)])  # a resposta de cada chamada, e o próximo passo

As ferramentas são declaradas uma vez, em JSON Schema ({"nome", "descricao", "parametros"}), e cada Conversa
traduz. A busca na web usa a busca nativa de cada provedor (buscar_na_web), numa chamada separada. O uso volta
normalizado: entrada (sem cache), saida, cache_leitura, cache_escrita e buscas, para o custo em custos.py.

APIs: Anthropic Messages (/v1/messages), OpenAI Responses (/v1/responses) e Gemini generateContent. Só usa a
biblioteca padrão.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import chaves

PROVEDORES_LLM = ("anthropic", "openai", "gemini")
TIMEOUT_S = 600
MAX_SAIDA = 16000


class ErroLLM(Exception):
    pass


@dataclass
class Chamada:
    id: str
    nome: str
    args: dict


@dataclass
class Resultado:
    id: str
    nome: str
    texto: str
    imagens: list[tuple[str, bytes]] = field(default_factory=list)  # (mime, bytes)


@dataclass
class Passo:
    texto: str
    chamadas: list[Chamada]
    uso: dict
    cortado: bool = False  # a resposta parou no limite de tokens de saída


def uso_vazio() -> dict:
    return {"entrada": 0, "saida": 0, "cache_leitura": 0, "cache_escrita": 0, "buscas": 0}


def somar_uso(total: dict, uso: dict) -> dict:
    for k, v in uso.items():
        total[k] = total.get(k, 0) + (v or 0)
    return total


# ---------------------------------------------------------------- HTTP


def http(url: str, corpo: dict | None, cabecalhos: dict, metodo: str = "POST", timeout: int = TIMEOUT_S) -> dict:
    """JSON de ida e volta, com novas tentativas em limite de taxa e erro do servidor."""
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(url, data=dados, method=metodo,
                                 headers={"Content-Type": "application/json", **cabecalhos})
    espera = 5
    for tentativa in range(6):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            texto = e.read().decode("utf-8", "replace")
            try:
                erro = json.loads(texto).get("error") or json.loads(texto)
                msg = erro.get("message") if isinstance(erro, dict) else str(erro)
            except (json.JSONDecodeError, AttributeError):
                msg = texto[:300]
            if e.code in (429, 500, 502, 503, 504, 529) and tentativa < 5 and "limit: 0" not in (msg or ""):
                m = re.search(r"retry in (\d+(?:\.\d+)?)s", msg or "")
                time.sleep(min(90, float(m.group(1)) + 1) if m else espera)
                espera = min(90, espera * 2)
                continue
            raise ErroLLM(f"HTTP {e.code}: {(msg or '')[:400]}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if tentativa < 5:
                time.sleep(espera)
                espera = min(90, espera * 2)
                continue
            raise ErroLLM(f"rede: {e}") from None
    raise ErroLLM("sem resposta")


def chave_de(provedor: str) -> str:
    chave = chaves.ler(provedor)
    if not chave:
        raise ErroLLM(f"sem chave de API para {chaves.PROVEDORES[provedor]['nome']}: cadastre em Configurações > Chaves de API")
    return chave


def b64(dados: bytes) -> str:
    return base64.b64encode(dados).decode()


# ---------------------------------------------------------------- conversas


class Conversa:
    provedor = ""

    def __init__(self, modelo: str, sistema: str, ferramentas: list[dict]):
        self.modelo, self.sistema, self.ferramentas = modelo, sistema, ferramentas
        self.chave = chave_de(self.provedor)

    def usuario(self, texto: str, imagens: list[tuple[str, bytes]] = ()) -> None:
        raise NotImplementedError

    def passo(self) -> Passo:
        raise NotImplementedError

    def resultados(self, lista: list[Resultado]) -> None:
        raise NotImplementedError


class ConversaAnthropic(Conversa):
    provedor = "anthropic"
    API = "https://api.anthropic.com/v1/messages"

    def __init__(self, *a):
        super().__init__(*a)
        self.mensagens: list[dict] = []

    def cabecalhos(self) -> dict:
        return {"x-api-key": self.chave, "anthropic-version": "2023-06-01"}

    @staticmethod
    def blocos_imagem(imagens) -> list[dict]:
        return [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64(d)}} for mime, d in imagens]

    def usuario(self, texto, imagens=()):
        self.mensagens.append({"role": "user", "content": [{"type": "text", "text": texto}, *self.blocos_imagem(imagens)]})

    def com_cache(self) -> list[dict]:
        """Marca o fim do histórico para o cache de prompt: cada passo relê tudo, e a releitura fica 10× mais barata."""
        mensagens = json.loads(json.dumps(self.mensagens))
        ultimo = mensagens[-1]["content"]
        if isinstance(ultimo, list) and ultimo:
            ultimo[-1]["cache_control"] = {"type": "ephemeral"}
        return mensagens

    def passo(self):
        corpo = {"model": self.modelo, "max_tokens": MAX_SAIDA, "messages": self.com_cache(),
                 "system": [{"type": "text", "text": self.sistema, "cache_control": {"type": "ephemeral"}}],
                 "tools": [{"name": f["nome"], "description": f["descricao"], "input_schema": f["parametros"]}
                           for f in self.ferramentas]}
        r = http(self.API, corpo, self.cabecalhos())
        conteudo = r.get("content") or []
        self.mensagens.append({"role": "assistant", "content": conteudo})
        u = r.get("usage") or {}
        uso = {"entrada": u.get("input_tokens", 0), "saida": u.get("output_tokens", 0),
               "cache_leitura": u.get("cache_read_input_tokens") or 0, "cache_escrita": u.get("cache_creation_input_tokens") or 0,
               "buscas": (u.get("server_tool_use") or {}).get("web_search_requests", 0)}
        return Passo(texto="\n".join(b["text"] for b in conteudo if b.get("type") == "text").strip(),
                     chamadas=[Chamada(b["id"], b["name"], b.get("input") or {}) for b in conteudo if b.get("type") == "tool_use"],
                     uso=uso, cortado=r.get("stop_reason") == "max_tokens")

    def resultados(self, lista):
        self.mensagens.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r.id,
             "content": [{"type": "text", "text": r.texto or "(sem saída)"}, *self.blocos_imagem(r.imagens)]} for r in lista]})


class ConversaOpenAI(Conversa):
    provedor = "openai"
    API = "https://api.openai.com/v1/responses"

    def __init__(self, *a):
        super().__init__(*a)
        self.anterior: str | None = None  # previous_response_id: o histórico fica guardado na OpenAI
        self.pendentes: list[dict] = []

    @staticmethod
    def entrada_usuario(texto, imagens) -> dict:
        partes = [{"type": "input_text", "text": texto}] if texto else []
        partes += [{"type": "input_image", "image_url": f"data:{mime};base64,{b64(d)}"} for mime, d in imagens]
        return {"role": "user", "content": partes}

    def usuario(self, texto, imagens=()):
        self.pendentes.append(self.entrada_usuario(texto, imagens))

    def passo(self):
        corpo = {"model": self.modelo, "instructions": self.sistema, "input": self.pendentes, "store": True,
                 "max_output_tokens": MAX_SAIDA,
                 "tools": [{"type": "function", "name": f["nome"], "description": f["descricao"], "parameters": f["parametros"],
                            "strict": False} for f in self.ferramentas]}
        if self.anterior:
            corpo["previous_response_id"] = self.anterior
        r = http(self.API, corpo, {"Authorization": f"Bearer {self.chave}"})
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
        u = r.get("usage") or {}
        cache = (u.get("input_tokens_details") or {}).get("cached_tokens", 0)
        uso = {"entrada": u.get("input_tokens", 0) - cache, "saida": u.get("output_tokens", 0), "cache_leitura": cache,
               "cache_escrita": 0, "buscas": 0}
        cortado = r.get("status") == "incomplete" and (r.get("incomplete_details") or {}).get("reason") == "max_output_tokens"
        return Passo("\n".join(textos).strip(), chamadas, uso, cortado)

    def resultados(self, lista):
        for r in lista:
            self.pendentes.append({"type": "function_call_output", "call_id": r.id, "output": r.texto or "(sem saída)"})
        imagens = [img for r in lista for img in r.imagens]
        if imagens:
            self.pendentes.append(self.entrada_usuario("Imagens lidas pela ferramenta Read:", imagens))


class ConversaGemini(Conversa):
    provedor = "gemini"
    API = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"

    def __init__(self, *a):
        super().__init__(*a)
        self.conteudos: list[dict] = []
        self.contador = 0

    @staticmethod
    def partes_imagem(imagens) -> list[dict]:
        return [{"inlineData": {"mimeType": mime, "data": b64(d)}} for mime, d in imagens]

    def usuario(self, texto, imagens=()):
        self.conteudos.append({"role": "user", "parts": [{"text": texto}, *self.partes_imagem(imagens)]})

    def passo(self):
        corpo = {"systemInstruction": {"parts": [{"text": self.sistema}]}, "contents": self.conteudos,
                 "tools": [{"functionDeclarations": [{"name": f["nome"], "description": f["descricao"],
                                                      "parameters": f["parametros"]} for f in self.ferramentas]}],
                 "generationConfig": {"maxOutputTokens": MAX_SAIDA * 2}}
        r = http(self.API.format(modelo=self.modelo), corpo, {"x-goog-api-key": self.chave})
        if bloqueio := (r.get("promptFeedback") or {}).get("blockReason"):
            raise ErroLLM(f"pedido bloqueado pelo filtro do Gemini ({bloqueio})")
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
        uso = {"entrada": u.get("promptTokenCount", 0) - cache + u.get("toolUsePromptTokenCount", 0),
               "saida": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0), "cache_leitura": cache,
               "cache_escrita": 0, "buscas": 0}
        return Passo("\n".join(textos).strip(), chamadas, uso, candidato.get("finishReason") == "MAX_TOKENS")

    def resultados(self, lista):
        partes = []
        for r in lista:
            resposta = {"name": r.nome, "response": {"resultado": r.texto or "(sem saída)"}}
            if not re.fullmatch(rf"{re.escape(r.nome)}-\d+", r.id):  # o id inventado aqui não volta para a API
                resposta["id"] = r.id
            partes.append({"functionResponse": resposta})
        partes += self.partes_imagem([img for r in lista for img in r.imagens])
        self.conteudos.append({"role": "user", "parts": partes})


CONVERSAS = {"anthropic": ConversaAnthropic, "openai": ConversaOpenAI, "gemini": ConversaGemini}


def nova_conversa(provedor: str, modelo: str, sistema: str, ferramentas: list[dict]) -> Conversa:
    if provedor not in CONVERSAS:
        raise ErroLLM(f"provedor de LLM desconhecido: {provedor}")
    return CONVERSAS[provedor](modelo, sistema, ferramentas)


# ---------------------------------------------------------------- busca na web


PEDIDO_BUSCA = ("Pesquise na web e responda em português com o que encontrar sobre a consulta abaixo: fatos, números, "
                "datas e nomes, cada um com a URL da fonte. Diga quando não achar nada confiável. Consulta: {consulta}")


def buscar_na_web(provedor: str, modelo: str, consulta: str) -> tuple[str, dict]:
    """Uma busca com a ferramenta nativa do provedor. Devolve o texto com as fontes e o uso."""
    chave = chave_de(provedor)
    pergunta = PEDIDO_BUSCA.format(consulta=consulta)
    fontes: dict[str, str] = {}
    if provedor == "anthropic":
        mensagens = [{"role": "user", "content": pergunta}]
        uso, textos = uso_vazio(), []
        for _ in range(4):  # pause_turn: a busca longa volta pausada e continua com a mesma mensagem
            r = http(ConversaAnthropic.API, {"model": modelo, "max_tokens": 4096, "messages": mensagens,
                                             "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]},
                     {"x-api-key": chave, "anthropic-version": "2023-06-01"})
            u = r.get("usage") or {}
            somar_uso(uso, {"entrada": u.get("input_tokens", 0), "saida": u.get("output_tokens", 0),
                            "buscas": (u.get("server_tool_use") or {}).get("web_search_requests", 0)})
            for b in r.get("content") or []:
                if b.get("type") == "text":
                    textos.append(b["text"])
                    for c in b.get("citations") or []:
                        fontes.setdefault(c.get("url", ""), c.get("title", ""))
            if r.get("stop_reason") != "pause_turn":
                break
            mensagens.append({"role": "assistant", "content": r["content"]})
        texto = "".join(textos)
    elif provedor == "openai":
        r = http(ConversaOpenAI.API, {"model": modelo, "input": pergunta, "tools": [{"type": "web_search"}]},
                 {"Authorization": f"Bearer {chave}"})
        textos = []
        for item in r.get("output") or []:
            for c in item.get("content") or [] if item.get("type") == "message" else []:
                if c.get("type") == "output_text":
                    textos.append(c["text"])
                    for a in c.get("annotations") or []:
                        if a.get("type") == "url_citation":
                            fontes.setdefault(a.get("url", ""), a.get("title", ""))
        u = r.get("usage") or {}
        cache = (u.get("input_tokens_details") or {}).get("cached_tokens", 0)
        uso = {**uso_vazio(), "entrada": u.get("input_tokens", 0) - cache, "saida": u.get("output_tokens", 0),
               "cache_leitura": cache, "buscas": sum(1 for i in r.get("output") or [] if i.get("type") == "web_search_call")}
        texto = "\n".join(textos)
    elif provedor == "gemini":
        r = http(ConversaGemini.API.format(modelo=modelo),
                 {"contents": [{"role": "user", "parts": [{"text": pergunta}]}], "tools": [{"google_search": {}}]},
                 {"x-goog-api-key": chave})
        candidato = (r.get("candidates") or [{}])[0]
        texto = "".join(p.get("text", "") for p in (candidato.get("content") or {}).get("parts", []) if not p.get("thought"))
        for pedaco in (candidato.get("groundingMetadata") or {}).get("groundingChunks") or []:
            web = pedaco.get("web") or {}
            fontes.setdefault(web.get("uri", ""), web.get("title", ""))
        u = r.get("usageMetadata") or {}
        uso = {**uso_vazio(), "entrada": u.get("promptTokenCount", 0) + u.get("toolUsePromptTokenCount", 0),
               "saida": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0),
               "buscas": 1 if (candidato.get("groundingMetadata") or {}).get("webSearchQueries") else 0}
    else:
        raise ErroLLM(f"provedor de LLM desconhecido: {provedor}")
    fontes.pop("", None)
    if fontes:
        texto += "\n\nFontes:\n" + "\n".join(f"- {titulo or url}: {url}" for url, titulo in fontes.items())
    return texto.strip() or "(a busca não devolveu nada)", uso


# ---------------------------------------------------------------- modelos e conexão


FORA_DO_CHAT = re.compile(r"embed|tts|audio|realtime|transcri|whisper|image|imagen|dall-e|moderation|veo|aqa|"
                          r"search|computer-use|live|robotics|native-audio|instruct|babbage|davinci|sora|banana", re.I)


def listar_modelos(provedor: str, chave: str | None = None) -> list[str]:
    """Os modelos de texto que a chave acessa, do mais novo ao mais antigo (pela ordem da API)."""
    chave = chave or chave_de(provedor)
    if provedor == "anthropic":
        r = http("https://api.anthropic.com/v1/models?limit=1000", None,
                 {"x-api-key": chave, "anthropic-version": "2023-06-01"}, metodo="GET", timeout=30)
        return [m["id"] for m in r.get("data") or []]
    if provedor == "openai":
        r = http("https://api.openai.com/v1/models", None, {"Authorization": f"Bearer {chave}"}, metodo="GET", timeout=30)
        ids = [m["id"] for m in sorted(r.get("data") or [], key=lambda m: m.get("created", 0), reverse=True)]
        return [i for i in ids if re.match(r"(gpt-|o\d|chatgpt)", i) and not FORA_DO_CHAT.search(i)]
    if provedor == "gemini":
        r = http("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000", None,
                 {"x-goog-api-key": chave}, metodo="GET", timeout=30)
        return [m["name"].removeprefix("models/") for m in r.get("models") or []
                if "generateContent" in (m.get("supportedGenerationMethods") or [])
                and m["name"].startswith("models/gemini") and not FORA_DO_CHAT.search(m["name"])]
    raise ErroLLM(f"provedor de LLM desconhecido: {provedor}")
