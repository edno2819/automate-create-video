"""Modelos de imagem do Gemini (Nano Banana), por generateContent.

O corpo é o de sempre: o texto e as fichas de referência numa mensagem só (`[{"text"}, *inlineData]`), com a
proporção e o tamanho em `imageConfig`. Pedido recusado pelo filtro (`promptFeedback.blockReason`) vira
`Bloqueado`, para o fluxo suavizar o prompt e tentar de novo; resposta sem imagem vira `ErroProvedor` com o
finishReason e o texto que o modelo devolveu no lugar.
"""

from __future__ import annotations

import base64
import json

from nucleo.provedores import http
from nucleo.provedores.erros import Bloqueado, ErroProvedor
from nucleo.provedores.imagem.base import EXTENSOES, ImagemGerada, PedidoImagem, ProvedorImagem

API = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"


def inline(ref) -> dict:
    return {"inlineData": {"mimeType": ref.mime, "data": base64.b64encode(ref.dados).decode()}}


class GeminiImagem(ProvedorImagem):
    id = "gemini"
    nome = "Gemini (Nano Banana)"
    modelo_padrao = "gemini-3.1-flash-image"
    modelos = ("gemini-3.1-flash-image", "gemini-3.1-flash-lite-image", "gemini-3-pro-image")
    modelos_por_qualidade = {"rascunho": "gemini-3.1-flash-lite-image", "padrao": "gemini-3.1-flash-image",
                             "maxima": "gemini-3-pro-image"}
    max_referencias = 6
    tamanhos = ("1K", "2K", "4K")

    def corpo(self, pedido: PedidoImagem) -> dict:
        config: dict = {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": pedido.proporcao}}
        if pedido.tamanho:
            config["imageConfig"]["imageSize"] = pedido.tamanho
        partes = [{"text": pedido.texto}, *(inline(r) for r in pedido.referencias)]
        return {"contents": [{"role": "user", "parts": partes}], "generationConfig": config}

    def gerar(self, pedido: PedidoImagem) -> ImagemGerada:
        bruto, _ = http.pedir(API.format(modelo=self.modelo), json.dumps(self.corpo(pedido)).encode(),
                              {"Content-Type": "application/json", "x-goog-api-key": self.chave},
                              timeout=180, provedor=self.id, espera_inicial=10)
        try:
            dados = json.loads(bruto)
        except json.JSONDecodeError:
            raise ErroProvedor(f"resposta que não é JSON: {bruto[:200]!r}", self.id) from None
        if bloqueio := (dados.get("promptFeedback") or {}).get("blockReason"):
            raise Bloqueado(f"pedido bloqueado pelo filtro do Gemini ({bloqueio})", self.id)
        candidato = (dados.get("candidates") or [{}])[0]
        textos = []
        for parte in (candidato.get("content") or {}).get("parts", []):
            if "inlineData" in parte:
                mime = parte["inlineData"].get("mimeType", "image/png")
                uso = dados.get("usageMetadata") or {}
                self._usou(uso, pedido.tamanho)
                return ImagemGerada(base64.b64decode(parte["inlineData"]["data"]), EXTENSOES.get(mime, "png"), uso)
            if parte.get("text"):
                textos.append(parte["text"])
        motivo = candidato.get("finishReason", "sem motivo")
        if motivo in ("SAFETY", "PROHIBITED_CONTENT", "IMAGE_SAFETY", "BLOCKLIST"):
            raise Bloqueado(f"imagem recusada pelo filtro do Gemini (finishReason={motivo})", self.id)
        raise ErroProvedor(f"nenhuma imagem na resposta (finishReason={motivo}) {' '.join(textos)[:200]}".strip(), self.id)
