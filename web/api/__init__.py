"""A API HTTP da interface (docs/arquitetura.md, seção API HTTP), um roteador por assunto.

O `comum` vem primeiro: ele põe ferramentas/ no caminho de importação (pedido.py e custos.py são importados pelo
nome, como os scripts fazem) antes dos roteadores que usam esses módulos.
"""

from __future__ import annotations

from web.api import comum  # noqa: F401 - primeiro, pelo caminho de importação
from web.api import (arquivos, biblioteca, configuracoes, contextos, desempenho, edicao, efeitos, execucoes,
                     personagens, projetos, referencias, resultados)

ROTEADORES = [
    configuracoes.roteador,
    efeitos.roteador,
    biblioteca.roteador,
    contextos.roteador,
    projetos.roteador,
    resultados.roteador,
    edicao.roteador,
    execucoes.roteador,
    desempenho.roteador,
    personagens.roteador,
    referencias.roteador,
    arquivos.roteador,
]
