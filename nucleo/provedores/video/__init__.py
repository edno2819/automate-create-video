"""Provedores de vídeo (animar as imagens das cenas) e o que a tela de configuração mostra.

    from nucleo.provedores import video
    v = video("gemini", "gemini-omni-1.1-flash")      # ProvedorVideo, com a chave cadastrada
    video.info()                                       # [{id, nome, modelo_padrao, modelos, resolucoes, ...}]

Como em nucleo.provedores.imagem, o módulo é chamável e faz o mesmo que a fábrica `nucleo.provedores.video(...)`.
"""

from __future__ import annotations

import sys
import types
from typing import Callable

from nucleo import provedores as _provedores
from nucleo.provedores.video.base import PedidoVideo, ProvedorVideo, VideoGerado

PROVEDORES = _provedores.VIDEO

__all__ = ["PROVEDORES", "PedidoVideo", "ProvedorVideo", "VideoGerado", "classe", "criar", "info"]


def classe(provedor: str) -> type[ProvedorVideo]:
    return _provedores.classe("video", provedor)


def criar(provedor: str, modelo: str | None = None, ao_usar: Callable[..., None] | None = None,
          chave: str | None = None) -> ProvedorVideo:
    """O provedor pronto para animar. SemChave se não há chave cadastrada."""
    return classe(provedor)(modelo=modelo, chave=chave or _provedores._chave(provedor), ao_usar=ao_usar)


def info() -> list[dict]:
    """Os provedores para a tela de configuração."""
    saida = []
    for id_ in PROVEDORES:
        cls = classe(id_)
        saida.append({"id": id_, "nome": cls.nome, "modelo_padrao": cls.modelo_padrao, "modelos": list(cls.modelos),
                      "resolucoes": list(cls.resolucoes), "resolucao_padrao": cls.resolucao_padrao,
                      "proporcoes": list(cls.proporcoes), "precisa_chave": cls.precisa_chave})
    return saida


class _ModuloChamavel(types.ModuleType):
    def __call__(self, provedor: str, modelo: str | None = None, ao_usar: Callable[..., None] | None = None,
                 chave: str | None = None) -> ProvedorVideo:
        return criar(provedor, modelo, ao_usar, chave)


sys.modules[__name__].__class__ = _ModuloChamavel
