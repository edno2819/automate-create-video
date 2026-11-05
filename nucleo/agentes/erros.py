"""Erros do motor de agentes (reexportados por nucleo.agentes.motor, que é onde os fluxos os procuram)."""

from __future__ import annotations


class Cancelado(Exception):
    """O usuário cancelou a execução: o motor para no próximo passo e mata os processos que abriu."""


class ErroMotor(Exception):
    """Falha de um agente (passou do limite de turnos, não entregou o formato pedido...) ou de uma ferramenta.

    Erro de ferramenta não derruba o agente: vira o texto do resultado e o modelo corrige o pedido."""
