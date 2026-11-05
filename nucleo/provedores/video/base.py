"""Interface dos provedores de vídeo: animar uma imagem parada num clipe curto, com a imagem como primeiro quadro.

O texto do pedido (o movimento, o que não pode mudar, sem fala nem música) é montado por quem pede
(ferramentas/animacao.py), igual para todos os provedores; o adaptador só traduz para a API. O áudio que o modelo
gerar junto com o vídeo não é usado: a montagem fica só com a imagem.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, ClassVar

from nucleo.provedores.imagem.base import Referencia
from nucleo.provedores.uso import Uso


@dataclass(frozen=True)
class PedidoVideo:
    texto: str
    imagem: Referencia          # o primeiro quadro
    proporcao: str = "16:9"
    resolucao: str = "720p"
    segundos: int = 6           # a duração pedida (o provedor pode devolver um pouco mais ou menos)
    ultimo_quadro: Referencia | None = None   # numa transição, a imagem da cena seguinte: o clipe termina nela


@dataclass(frozen=True)
class VideoGerado:
    dados: bytes                # mp4
    uso: dict


class ProvedorVideo(ABC):
    id: ClassVar[str]
    nome: ClassVar[str]
    modelo_padrao: ClassVar[str]
    modelos: ClassVar[tuple[str, ...]]
    resolucoes: ClassVar[tuple[str, ...]]
    resolucao_padrao: ClassVar[str] = "720p"
    proporcoes: ClassVar[tuple[str, ...]]      # as que o modelo gera; nas outras, a cena fica com a imagem parada
    precisa_chave: ClassVar[bool] = True

    def __init__(self, modelo: str | None = None, chave: str | None = None,
                 ao_usar: Callable[[Uso], None] | None = None):
        self.modelo = modelo or self.modelo_padrao
        self.chave = chave
        self.ao_usar = ao_usar

    def _usou(self, bruto: dict, pedido: PedidoVideo) -> None:
        if self.ao_usar:
            self.ao_usar(Uso(servico="animacao", provedor=self.id, modelo=self.modelo, bruto=bruto,
                             segundos=pedido.segundos, tamanho=pedido.resolucao))

    @abstractmethod
    def animar(self, pedido: PedidoVideo) -> VideoGerado:
        """O clipe pedido. Levanta SemCota, Bloqueado (filtro de conteúdo) ou ErroProvedor."""
