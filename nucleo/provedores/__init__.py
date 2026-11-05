"""Registro dos provedores: quem implementa cada interface (LLM, voz, imagem, vídeo).

    from nucleo import provedores
    llm = provedores.llm("openai")                    # ProvedorLLM, com a chave cadastrada
    tts = provedores.voz("gemini", "gemini-3.8-flash-tts", ao_usar=registrar)
    img = provedores.imagem("gemini", "gemini-3.1-flash-image")
    vid = provedores.video("gemini")                 # anima as imagens das cenas

Os módulos de cada provedor são importados só quando usados: o servidor web não precisa carregar o que a
etapa não usa. Acrescentar um provedor = uma classe que segue a base + uma linha no mapa abaixo.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Callable

from nucleo.provedores.erros import SemChave

if TYPE_CHECKING:
    from nucleo.provedores.imagem.base import ProvedorImagem
    from nucleo.provedores.llm.base import ProvedorLLM
    from nucleo.provedores.video.base import ProvedorVideo
    from nucleo.provedores.voz.base import ProvedorVoz

LLM = ("anthropic", "openai", "gemini")
VOZ = ("gemini", "openai", "elevenlabs", "espeak")
IMAGEM = ("gemini", "openai")
VIDEO = ("gemini",)

_CLASSES = {
    "llm": {"anthropic": "nucleo.provedores.llm.anthropic:ProvedorAnthropic",
            "openai": "nucleo.provedores.llm.openai:ProvedorOpenAI",
            "gemini": "nucleo.provedores.llm.gemini:ProvedorGemini"},
    "voz": {"gemini": "nucleo.provedores.voz.gemini:GeminiTTS",
            "openai": "nucleo.provedores.voz.openai:OpenAITTS",
            "elevenlabs": "nucleo.provedores.voz.elevenlabs:ElevenLabsTTS",
            "espeak": "nucleo.provedores.voz.espeak:EspeakTTS"},
    "imagem": {"gemini": "nucleo.provedores.imagem.gemini:GeminiImagem",
               "openai": "nucleo.provedores.imagem.openai:OpenAIImagem"},
    "video": {"gemini": "nucleo.provedores.video.gemini:GeminiVideo"},
}


def classe(tipo: str, provedor: str) -> type:
    try:
        caminho = _CLASSES[tipo][provedor]
    except KeyError:
        raise ValueError(f"provedor de {tipo} desconhecido: {provedor}") from None
    modulo, nome = caminho.split(":")
    return getattr(import_module(modulo), nome)


def _chave(provedor: str) -> str:
    from nucleo import chaves
    chave = chaves.ler(provedor)
    if not chave:
        nome = chaves.PROVEDORES.get(provedor, {}).get("nome", provedor)
        raise SemChave(f"sem chave de API para {nome}: cadastre em Configurações > Chaves de API", provedor)
    return chave


def llm(provedor: str, chave: str | None = None) -> "ProvedorLLM":
    return classe("llm", provedor)(chave or _chave(provedor))


def voz(fornecedor: str, modelo: str | None = None, ao_usar: Callable[..., None] | None = None,
        chave: str | None = None) -> "ProvedorVoz":
    cls = classe("voz", fornecedor)
    if getattr(cls, "precisa_chave", True) and not chave:
        chave = _chave(fornecedor)
    return cls(modelo=modelo, chave=chave, ao_usar=ao_usar)


def imagem(provedor: str, modelo: str | None = None, ao_usar: Callable[..., None] | None = None,
           chave: str | None = None) -> "ProvedorImagem":
    return classe("imagem", provedor)(modelo=modelo, chave=chave or _chave(provedor), ao_usar=ao_usar)


def video(provedor: str, modelo: str | None = None, ao_usar: Callable[..., None] | None = None,
          chave: str | None = None) -> "ProvedorVideo":
    return classe("video", provedor)(modelo=modelo, chave=chave or _chave(provedor), ao_usar=ao_usar)


# llm, voz, imagem e video também são subpacotes (atalhos, catálogo de vozes, info para a tela...). Quando um subpacote é
# importado, o Python troca o atributo deste pacote pelo módulo: por isso eles são módulos chamáveis, que fazem o
# mesmo que as fábricas acima, e são importados já aqui, para `provedores.voz(...)` e `provedores.voz.catalogo(...)`
# funcionarem em qualquer ordem de importação.
import_module("nucleo.provedores.llm")
import_module("nucleo.provedores.voz")
import_module("nucleo.provedores.imagem")
import_module("nucleo.provedores.video")
