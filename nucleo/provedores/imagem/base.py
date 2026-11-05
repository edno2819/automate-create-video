"""Interface dos provedores de imagem.

O texto do pedido (a instrução das fichas de referência, o `Avoid: ...`, o pedido de ajuste do personagem) é
montado por quem pede (ferramentas/producao/gerar_imagens.py, ferramentas/personagem.py), igual para todos os
provedores. O adaptador só traduz para a API: o Gemini manda o texto e as referências numa mensagem só; a
OpenAI manda as referências em images/edits e ajusta a proporção ao tamanho que o modelo sabe gerar.

O que mantém o personagem coerente entre as cenas é aceitar várias imagens de referência: `max_referencias`
diz quantas o provedor recebe por pedido.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, ClassVar

from nucleo.provedores.uso import Uso

MIMES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
EXTENSOES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
QUALIDADES = ("rascunho", "padrao", "maxima")


@dataclass(frozen=True)
class Referencia:
    dados: bytes
    mime: str

    @classmethod
    def de_arquivo(cls, caminho: Path) -> "Referencia":
        return cls(caminho.read_bytes(), MIMES[caminho.suffix[1:].lower()])


@dataclass(frozen=True)
class PedidoImagem:
    texto: str
    referencias: tuple[Referencia, ...] = ()
    proporcao: str = "16:9"
    tamanho: str | None = None       # 1K, 2K, 4K (Gemini 3); os outros escolhem pelo tamanho que sabem gerar
    qualidade: str | None = None     # rascunho, padrao, maxima (visual.qualidade do pedido); o Gemini escolhe pelo modelo


@dataclass(frozen=True)
class ImagemGerada:
    dados: bytes
    extensao: str
    uso: dict


class ProvedorImagem(ABC):
    id: ClassVar[str]
    nome: ClassVar[str]
    modelo_padrao: ClassVar[str]
    modelos: ClassVar[tuple[str, ...]]
    modelos_por_qualidade: ClassVar[dict[str, str]]
    max_referencias: ClassVar[int] = 6
    tamanhos: ClassVar[tuple[str, ...]] = ()
    precisa_chave: ClassVar[bool] = True

    def __init__(self, modelo: str | None = None, chave: str | None = None,
                 ao_usar: Callable[[Uso], None] | None = None):
        self.modelo = modelo or self.modelo_padrao
        self.chave = chave
        self.ao_usar = ao_usar

    @classmethod
    def modelo_da_qualidade(cls, qualidade: str | None) -> str:
        return cls.modelos_por_qualidade.get(qualidade or "", cls.modelo_padrao) or cls.modelo_padrao

    def _usou(self, bruto: dict, tamanho: str | None = None) -> None:
        if self.ao_usar:
            self.ao_usar(Uso(servico="imagem", provedor=self.id, modelo=self.modelo, bruto=bruto, tamanho=tamanho))

    @abstractmethod
    def gerar(self, pedido: PedidoImagem) -> ImagemGerada:
        """A imagem pedida. Levanta SemCota, Bloqueado (filtro de conteúdo) ou ErroProvedor."""
