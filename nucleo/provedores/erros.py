"""Erros comuns a todos os provedores (LLM, voz, imagem).

Os scripts e os fluxos tratam estes erros pelo tipo, sem saber de qual provedor vieram:

    SemCota     a conta não tem cota ou crédito (no Gemini, o projeto do Google sem faturamento)
    Bloqueado   o filtro de conteúdo do provedor recusou o pedido (o fluxo suaviza o prompt e tenta de novo)
    SemChave    não há chave cadastrada para o provedor
    ErroProvedor qualquer outra falha (HTTP, rede, resposta sem o que foi pedido)
"""

from __future__ import annotations


class ErroProvedor(Exception):
    """Falha numa chamada a um provedor. `provedor` diz quem falhou, para a mensagem na tela."""

    def __init__(self, mensagem: str, provedor: str | None = None, status: int | None = None):
        super().__init__(mensagem)
        self.provedor = provedor
        self.status = status


class SemCota(ErroProvedor):
    pass


class Bloqueado(ErroProvedor):
    pass


class SemChave(ErroProvedor):
    pass
