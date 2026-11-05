"""Uso de uma chamada a um provedor, normalizado para o cálculo de custo.

LLM: {"entrada", "saida", "cache_leitura", "cache_escrita", "buscas"} (tokens; entrada sem o cache).
Voz e imagem guardam o uso cru da API (o usageMetadata do Gemini, o usage da OpenAI) mais `caracteres`,
`segundos`, `tamanho` e `item`, como sempre gravaram em custos.jsonl; ferramentas/custos.py sabe calcular os dois.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def uso_vazio() -> dict:
    return {"entrada": 0, "saida": 0, "cache_leitura": 0, "cache_escrita": 0, "buscas": 0}


def somar_uso(total: dict, uso: dict) -> dict:
    for k, v in uso.items():
        if isinstance(v, (int, float)):
            total[k] = total.get(k, 0) + (v or 0)
    return total


@dataclass
class Uso:
    """Uma chamada paga, como os provedores de voz e imagem avisam pelo callback `ao_usar`."""
    servico: str                  # "narracao" | "imagem" | "llm" | "busca"
    provedor: str
    modelo: str
    bruto: dict = field(default_factory=dict)
    caracteres: int | None = None
    segundos: float | None = None
    tamanho: str | None = None
    item: str | None = None
