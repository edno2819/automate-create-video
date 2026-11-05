"""Armazenamento de objetos escolhido pela configuração (MinIO, AWS S3 ou pasta local).

    from nucleo.armazenamento import armazenamento
    a = armazenamento()                 # o mesmo objeto no processo inteiro
    a.enviar("projetos/x/10-video/x.mp4", Path("..."))

Quase ninguém usa isto direto: os arquivos dos projetos passam pelo nucleo.espaco, que mantém o índice no banco.
"""

from __future__ import annotations

import threading

from nucleo.armazenamento.base import Armazenamento, Objeto, mime_de
from nucleo.config import config

__all__ = ["Armazenamento", "Objeto", "armazenamento", "mime_de", "redefinir"]

_instancia: Armazenamento | None = None
_trava = threading.Lock()


def _criar() -> Armazenamento:
    c = config()
    if c.armazenamento == "local":
        from nucleo.armazenamento.local import LocalArmazenamento
        return LocalArmazenamento(c.pasta_objetos_local)
    from nucleo.armazenamento.s3 import S3Armazenamento
    return S3Armazenamento(c.bucket, tipo=c.armazenamento, endpoint=c.s3_endpoint, regiao=c.s3_regiao,
                           chave=c.s3_chave, segredo=c.s3_segredo)


def armazenamento() -> Armazenamento:
    global _instancia
    with _trava:
        if _instancia is None:
            _instancia = _criar()
        return _instancia


def redefinir() -> None:
    """Esquece a instância (testes que trocam a configuração)."""
    global _instancia
    with _trava:
        _instancia = None
