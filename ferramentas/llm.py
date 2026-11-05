"""Compatibilidade: o cliente dos LLMs mudou para nucleo/provedores/llm (um módulo por provedor).

Quem ainda importa `llm` de ferramentas/ continua funcionando com os mesmos nomes:

    sys.path.insert(0, str(RAIZ / "ferramentas"))
    import llm
    conversa = llm.nova_conversa("openai", "gpt-6.1-sol", sistema, ferramentas)

ErroLLM é o ErroProvedor comum a todos os provedores (SemCota, Bloqueado e SemChave são subclasses dele).
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from nucleo.provedores.erros import ErroProvedor as ErroLLM  # noqa: E402
from nucleo.provedores.llm import (FORA_DO_CHAT, PROVEDORES_LLM, Chamada, Conversa, Passo, Resultado,  # noqa: E402
                                   buscar_na_web, listar_modelos, nova_conversa, somar_uso, uso_vazio)

__all__ = ["FORA_DO_CHAT", "PROVEDORES_LLM", "Chamada", "Conversa", "ErroLLM", "Passo", "Resultado", "buscar_na_web",
           "listar_modelos", "nova_conversa", "somar_uso", "uso_vazio"]
