"""Armazenamento numa pasta local (dados/objetos/ por padrão): desenvolvimento e testes, sem MinIO.

Cada objeto é um arquivo em `<pasta>/<chave>`. As gravações são atômicas (temporário na mesma pasta + replace),
para que um leitor nunca veja um objeto pela metade.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from nucleo.armazenamento.base import Armazenamento, Objeto

SUFIXO_TEMPORARIO = ".parcial"


class LocalArmazenamento(Armazenamento):
    tipo = "local"

    def __init__(self, pasta: Path):
        self.pasta = Path(pasta).resolve()

    def _caminho(self, chave: str) -> Path:
        if not chave or chave.startswith("/") or "\\" in chave or any(p in ("", ".", "..") for p in chave.split("/")):
            raise ValueError(f"chave de objeto inválida: {chave!r}")
        return self.pasta / chave

    def garantir_bucket(self) -> None:
        self.pasta.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _gravar_atomico(destino: Path, copiar) -> None:
        destino.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=destino.parent, prefix=f".{destino.name}.", suffix=SUFIXO_TEMPORARIO)
        try:
            with os.fdopen(fd, "wb") as f:
                copiar(f)
            os.replace(tmp, destino)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def enviar(self, chave: str, origem: Path, mime: str | None = None, metadados: dict | None = None) -> None:
        def copiar(f):
            with open(origem, "rb") as o:
                shutil.copyfileobj(o, f, 1024 * 1024)
        self._gravar_atomico(self._caminho(chave), copiar)

    def enviar_bytes(self, chave: str, dados: bytes, mime: str | None = None) -> None:
        self._gravar_atomico(self._caminho(chave), lambda f: f.write(dados))

    def baixar(self, chave: str, destino: Path) -> None:
        origem = self._caminho(chave)
        if not origem.is_file():
            raise FileNotFoundError(chave)

        def copiar(f):
            with open(origem, "rb") as o:
                shutil.copyfileobj(o, f, 1024 * 1024)
        self._gravar_atomico(Path(destino), copiar)

    def ler_bytes(self, chave: str) -> bytes:
        origem = self._caminho(chave)
        if not origem.is_file():
            raise FileNotFoundError(chave)
        return origem.read_bytes()

    def existe(self, chave: str) -> bool:
        return self._caminho(chave).is_file()

    def apagar(self, chave: str) -> None:
        caminho = self._caminho(chave)
        caminho.unlink(missing_ok=True)
        # tira as pastas que ficaram vazias, como num bucket (onde pasta não existe)
        pai = caminho.parent
        while pai != self.pasta and pai.is_dir():
            try:
                pai.rmdir()
            except OSError:
                break
            pai = pai.parent

    def listar(self, prefixo: str) -> Iterator[Objeto]:
        if not self.pasta.is_dir():
            return
        for raiz, pastas, arquivos in os.walk(self.pasta):
            pastas.sort()
            for nome in sorted(arquivos):
                if nome.endswith(SUFIXO_TEMPORARIO):
                    continue
                caminho = Path(raiz) / nome
                chave = caminho.relative_to(self.pasta).as_posix()
                if not chave.startswith(prefixo):
                    continue
                st = caminho.stat()
                yield Objeto(chave=chave, tamanho=st.st_size,
                             etag=hashlib.md5(f"{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest(),
                             modificado=datetime.fromtimestamp(st.st_mtime, timezone.utc))

    def descricao(self) -> str:
        return f"pasta local {self.pasta}"
