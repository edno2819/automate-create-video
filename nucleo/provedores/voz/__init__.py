"""Fornecedores de voz: catálogo das vozes, troca de voz entre fornecedores e o que a tela de configuração mostra.

    from nucleo.provedores import voz
    tts = voz("gemini", "gemini-3.8-flash-tts", ao_usar=registrar)     # ProvedorVoz, com a chave cadastrada
    voz.catalogo("openai")                                           # [Voz(...)], sem rede nem chave
    voz.voz_equivalente("openai", "Sadaltager")                      # "cedar"? sempre a mesma do mesmo gênero

O pacote tem o mesmo nome da fábrica `nucleo.provedores.voz(...)`: quando ele é importado, o Python troca o
atributo do pacote pai pelo módulo, então o módulo é chamável e faz o mesmo que a fábrica.
"""

from __future__ import annotations

import sys
import types
from typing import Callable, Sequence

from nucleo import provedores as _provedores
from nucleo.provedores.voz.base import (GENERO_OPENAI, GENERO_VOZES, VOZES_GEMINI, VOZES_OPENAI, ProvedorVoz, Voz,
                                        genero_de, para_wav)
from nucleo.provedores.voz.base import voz_equivalente as _equivalente

FORNECEDORES = _provedores.VOZ

__all__ = ["FORNECEDORES", "GENERO_OPENAI", "GENERO_VOZES", "VOZES_GEMINI", "VOZES_OPENAI", "ProvedorVoz", "Voz",
           "atualizar_vozes_elevenlabs", "catalogo", "classe", "criar", "genero_de", "info", "para_wav",
           "voz_equivalente"]


def classe(fornecedor: str) -> type[ProvedorVoz]:
    return _provedores.classe("voz", fornecedor)


def criar(fornecedor: str, modelo: str | None = None, ao_usar: Callable[..., None] | None = None,
          chave: str | None = None) -> ProvedorVoz:
    """O fornecedor pronto para sintetizar. SemChave se ele precisa de chave e não há nenhuma cadastrada."""
    cls = classe(fornecedor)
    if cls.precisa_chave and not chave:
        chave = _provedores._chave(fornecedor)
    return cls(modelo=modelo, chave=chave, ao_usar=ao_usar)


def catalogo(fornecedor: str) -> list[Voz]:
    """As vozes do fornecedor, sem rede: as fixas do Gemini, da OpenAI e do espeak; as da ElevenLabs, do cache."""
    return classe(fornecedor).catalogo()


def voz_equivalente(fornecedor: str | Sequence[Voz], voz: str) -> str:
    """A voz no fornecedor (pelo id ou por uma lista de vozes): a própria, se existe; senão uma do mesmo gênero."""
    vozes = catalogo(fornecedor) if isinstance(fornecedor, str) else fornecedor
    return _equivalente(vozes, voz)


def atualizar_vozes_elevenlabs(chave: str) -> list[Voz]:
    """Busca as vozes da conta na ElevenLabs e atualiza o cache (banco e biblioteca/vozes/elevenlabs.json)."""
    from nucleo.provedores.voz.elevenlabs import atualizar_vozes
    return atualizar_vozes(chave)


def info() -> list[dict]:
    """Os fornecedores para a tela de configuração."""
    saida = []
    for id_ in FORNECEDORES:
        cls = classe(id_)
        saida.append({"id": id_, "nome": cls.nome, "modelos": list(cls.modelos), "modelo_padrao": cls.modelo_padrao,
                      "voz_padrao": cls.voz_padrao, "precisa_chave": cls.precisa_chave,
                      "aceita_instrucoes": cls.aceita_instrucoes})
    return saida


class _ModuloChamavel(types.ModuleType):
    def __call__(self, fornecedor: str, modelo: str | None = None, ao_usar: Callable[..., None] | None = None,
                 chave: str | None = None) -> ProvedorVoz:
        return criar(fornecedor, modelo, ao_usar, chave)


sys.modules[__name__].__class__ = _ModuloChamavel
