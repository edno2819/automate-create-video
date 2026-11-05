"""Interface do armazenamento de objetos: a fonte da verdade dos arquivos de projetos/, pedidos/, rascunhos/ e
das partes da biblioteca que mudam (docs/arquitetura.md, seção Armazenamento).

Três implementações: MinIO e AWS S3 (s3.py, a mesma classe com o boto3) e uma pasta local (local.py), para
desenvolvimento e testes. As chaves dos objetos são os caminhos lógicos (relativos à raiz, com `/`) com o
prefixo da configuração na frente; quem calcula a chave é o nucleo.espaco.
"""

from __future__ import annotations

import mimetypes
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar

# tipos que o mimetypes não conhece (ou conhece com outro nome) e que o pipeline grava
for _ext, _tipo in ((".md", "text/markdown"), (".jsonl", "application/x-ndjson"), (".yaml", "application/yaml"),
                    (".yml", "application/yaml"), (".ass", "text/x-ssa"), (".srt", "application/x-subrip"),
                    (".webp", "image/webp"), (".wav", "audio/wav"), (".mp3", "audio/mpeg")):
    mimetypes.add_type(_tipo, _ext)


def mime_de(caminho: str | Path) -> str:
    return mimetypes.guess_type(str(caminho))[0] or "application/octet-stream"


@dataclass(frozen=True)
class Objeto:
    chave: str
    tamanho: int
    etag: str | None
    modificado: datetime | None


class Armazenamento(ABC):
    tipo: ClassVar[str]

    @abstractmethod
    def garantir_bucket(self) -> None:
        """Deixa o lugar dos objetos pronto (cria o bucket se faltar e a permissão deixar)."""

    @abstractmethod
    def enviar(self, chave: str, origem: Path, mime: str | None = None, metadados: dict | None = None) -> None: ...

    @abstractmethod
    def enviar_bytes(self, chave: str, dados: bytes, mime: str | None = None) -> None: ...

    @abstractmethod
    def baixar(self, chave: str, destino: Path) -> None:
        """Grava o objeto em `destino` de forma atômica (temporário + replace). FileNotFoundError se não existe."""

    @abstractmethod
    def ler_bytes(self, chave: str) -> bytes:
        """FileNotFoundError se não existe."""

    @abstractmethod
    def existe(self, chave: str) -> bool: ...

    @abstractmethod
    def apagar(self, chave: str) -> None:
        """Apagar o que não existe não é erro."""

    @abstractmethod
    def listar(self, prefixo: str) -> Iterator[Objeto]: ...

    def url_assinada(self, chave: str, expira_s: int = 3600) -> str | None:
        return None

    def descricao(self) -> str:
        """Uma linha para a tela de sistema (sem segredos)."""
        return self.tipo
