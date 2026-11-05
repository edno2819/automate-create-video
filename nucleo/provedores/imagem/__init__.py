"""Provedores de imagem: escolha do modelo pela qualidade do pedido e o que a tela de configuração mostra.

    from nucleo.provedores import imagem
    img = imagem("gemini", "gemini-3.1-flash-image")       # ProvedorImagem, com a chave cadastrada
    imagem.modelo_para("openai", "rascunho")               # "gpt-image-1-mini"
    imagem.info()                                          # [{id, nome, modelo_padrao, modelos_por_qualidade, ...}]

O pacote tem o mesmo nome da fábrica `nucleo.provedores.imagem(...)`: quando ele é importado, o Python troca o
atributo do pacote pai pelo módulo, então o módulo é chamável e faz o mesmo que a fábrica.
"""

from __future__ import annotations

import sys
import types
from typing import Callable

from nucleo import provedores as _provedores
from nucleo.provedores.imagem.base import (QUALIDADES, ImagemGerada, PedidoImagem, ProvedorImagem, Referencia)

PROVEDORES = _provedores.IMAGEM

__all__ = ["PROVEDORES", "QUALIDADES", "ImagemGerada", "PedidoImagem", "ProvedorImagem", "Referencia", "classe",
           "criar", "info", "modelo_para"]


def classe(provedor: str) -> type[ProvedorImagem]:
    return _provedores.classe("imagem", provedor)


def criar(provedor: str, modelo: str | None = None, ao_usar: Callable[..., None] | None = None,
          chave: str | None = None) -> ProvedorImagem:
    """O provedor pronto para gerar. SemChave se não há chave cadastrada."""
    return classe(provedor)(modelo=modelo, chave=chave or _provedores._chave(provedor), ao_usar=ao_usar)


def modelo_para(provedor: str, qualidade: str | None) -> str:
    """O modelo do provedor para a qualidade do pedido (visual.qualidade), ou o padrão dele."""
    return classe(provedor).modelo_da_qualidade(qualidade)


def info() -> list[dict]:
    """Os provedores para a tela de configuração."""
    saida = []
    for id_ in PROVEDORES:
        cls = classe(id_)
        saida.append({"id": id_, "nome": cls.nome, "modelo_padrao": cls.modelo_padrao, "modelos": list(cls.modelos),
                      "modelos_por_qualidade": dict(cls.modelos_por_qualidade), "max_referencias": cls.max_referencias,
                      "tamanhos": list(cls.tamanhos), "precisa_chave": cls.precisa_chave})
    return saida


class _ModuloChamavel(types.ModuleType):
    def __call__(self, provedor: str, modelo: str | None = None, ao_usar: Callable[..., None] | None = None,
                 chave: str | None = None) -> ProvedorImagem:
        return criar(provedor, modelo, ao_usar, chave)


sys.modules[__name__].__class__ = _ModuloChamavel
