"""Gemini TTS pela API REST (generateContent com resposta em áudio).

O estilo vai num prompt estruturado (perfil, notas de direção, transcrição): em teste, foi o único formato em
que o modelo não leu a instrução em voz alta. O texto desse prompt não muda: o cache da narração e a
qualidade dependem dele.
"""

from __future__ import annotations

import base64
import json

from nucleo.provedores import http
from nucleo.provedores.erros import ErroProvedor
from nucleo.provedores.voz.base import GENERO_VOZES, VOZES_GEMINI, ProvedorVoz, Voz, para_wav, vozes_de

API = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"


def prompt_de_voz(texto: str, perfil: str, estilo: str) -> str:
    return f"# PERFIL DE ÁUDIO\n{perfil}\n\n## NOTAS DE DIREÇÃO\n{estilo}\n\n## TRANSCRIÇÃO\n{texto}"


class GeminiTTS(ProvedorVoz):
    id = "gemini"
    nome = "Gemini TTS"
    modelo_padrao = "gemini-3.8-flash-tts"
    voz_padrao = "Charon"
    modelos = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts", "gemini-3.1-flash-tts-preview",
               "gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts")

    @classmethod
    def catalogo(cls) -> list[Voz]:
        return vozes_de(VOZES_GEMINI, GENERO_VOZES)

    def corpo(self, texto: str, voz: str, perfil: str, estilo: str) -> dict:
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt_de_voz(texto, perfil, estilo)}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                                     "voiceName": self.voz_equivalente(voz)}}}},
        }

    def sintetizar(self, texto: str, voz: str, perfil: str, estilo: str) -> bytes:
        bruto, _ = http.pedir(API.format(modelo=self.modelo), json.dumps(self.corpo(texto, voz, perfil, estilo)).encode(),
                              {"Content-Type": "application/json", "x-goog-api-key": self.chave},
                              timeout=300, provedor=self.id, espera_inicial=10)
        try:
            dados = json.loads(bruto)
        except json.JSONDecodeError:
            raise ErroProvedor(f"resposta que não é JSON: {bruto[:200]!r}", self.id) from None
        self._usou(dados.get("usageMetadata") or {}, caracteres=len(texto))
        candidato = (dados.get("candidates") or [{}])[0]
        for parte in (candidato.get("content") or {}).get("parts", []):
            if "inlineData" in parte:
                return para_wav(base64.b64decode(parte["inlineData"]["data"]), parte["inlineData"].get("mimeType", ""))
        raise ErroProvedor(f"nenhum áudio na resposta (finishReason={candidato.get('finishReason', '?')})", self.id)
