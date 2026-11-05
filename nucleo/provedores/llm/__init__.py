"""LLMs dos provedores (Claude, GPT e Gemini) atrás de uma interface só: veja base.py.

Atalhos por id de provedor, com a chave cadastrada (nucleo.chaves) quando nenhuma é passada:

    from nucleo.provedores import llm
    conversa = llm.nova_conversa("gemini", "gemini-3.8-flash", sistema, ferramentas)
    texto, uso = llm.buscar_na_web("openai", "gpt-6-luna", "higiene na idade média")
    modelos = llm.listar_modelos("anthropic")

O pacote tem o mesmo nome da fábrica `nucleo.provedores.llm(...)`: quando ele é importado, o Python troca o
atributo do pacote pai pelo módulo, então o módulo é chamável e faz o mesmo que a fábrica (como voz e imagem).
"""

from __future__ import annotations

import sys
import types

from nucleo import provedores as _provedores
from nucleo.provedores.erros import ErroProvedor
from nucleo.provedores.llm.base import (FORA_DO_CHAT, MAX_SAIDA, PEDIDO_BUSCA, Chamada, Conversa, Passo, ProvedorLLM,
                                        Resultado, somar_uso, uso_vazio)

PROVEDORES_LLM = _provedores.LLM

__all__ = ["FORA_DO_CHAT", "MAX_SAIDA", "PEDIDO_BUSCA", "PROVEDORES_LLM", "Chamada", "Conversa", "ErroProvedor", "Passo",
           "ProvedorLLM", "Resultado", "buscar_na_web", "classe", "listar_modelos", "nova_conversa", "provedor",
           "somar_uso", "uso_vazio"]


def classe(id_: str) -> type[ProvedorLLM]:
    if id_ not in PROVEDORES_LLM:
        raise ErroProvedor(f"provedor de LLM desconhecido: {id_}", id_)
    return _provedores.classe("llm", id_)


def provedor(id_: str, chave: str | None = None) -> ProvedorLLM:
    """O provedor pelo id (anthropic, openai, gemini). Sem chave cadastrada: SemChave."""
    return classe(id_)(chave or _provedores._chave(id_))


def nova_conversa(id_: str, modelo: str, sistema: str, ferramentas: list[dict], max_saida: int = MAX_SAIDA,
                  chave: str | None = None) -> Conversa:
    return provedor(id_, chave).conversa(modelo, sistema, ferramentas, max_saida)


def buscar_na_web(id_: str, modelo: str, consulta: str, chave: str | None = None) -> tuple[str, dict]:
    return provedor(id_, chave).buscar_na_web(modelo, consulta)


def listar_modelos(id_: str, chave: str | None = None) -> list[str]:
    return provedor(id_, chave).listar_modelos()


class _ModuloChamavel(types.ModuleType):
    def __call__(self, id_: str, chave: str | None = None) -> ProvedorLLM:
        return provedor(id_, chave)


sys.modules[__name__].__class__ = _ModuloChamavel
