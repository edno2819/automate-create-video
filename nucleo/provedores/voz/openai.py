"""TTS da OpenAI (/v1/audio/speech), em PCM de 24 kHz.

O perfil e o estilo vão em `instructions`, que o gpt-4o-mini-tts segue; os modelos tts-1 não aceitam o campo.
A API não devolve o uso: o custo é estimado pela duração do áudio (preço por minuto em biblioteca/precos.json).
"""

from __future__ import annotations

from nucleo.provedores import http
from nucleo.provedores.voz.base import (GENERO_OPENAI, VOZES_OPENAI, ProvedorVoz, Voz, duracao_bytes, para_wav,
                                        vozes_de)

API = "https://api.openai.com/v1/audio/speech"


class OpenAITTS(ProvedorVoz):
    id = "openai"
    nome = "OpenAI TTS"
    modelo_padrao = "gpt-4o-mini-tts"
    voz_padrao = "cedar"
    modelos = ("gpt-4o-mini-tts", "tts-1", "tts-1-hd")

    @classmethod
    def catalogo(cls) -> list[Voz]:
        return vozes_de(VOZES_OPENAI, GENERO_OPENAI)

    def corpo(self, texto: str, voz: str, perfil: str, estilo: str) -> dict:
        corpo = {"model": self.modelo, "voice": self.voz_equivalente(voz), "input": texto, "response_format": "pcm"}
        if not self.modelo.startswith("tts-1"):
            corpo["instructions"] = f"{perfil}\n\n{estilo}".strip()
        return corpo

    def sintetizar(self, texto: str, voz: str, perfil: str, estilo: str) -> bytes:
        dados = http.pedir_binario(API, self.corpo(texto, voz, perfil, estilo), {"Authorization": f"Bearer {self.chave}"},
                                   provedor=self.id)
        wav = para_wav(dados, "audio/L16;rate=24000")
        self._usou({}, segundos=round(duracao_bytes(wav), 2), caracteres=len(texto))
        return wav
