"""Cifra simétrica dos segredos guardados no banco (as chaves de API dos provedores).

O banco pode ir para um backup, um dump ou outra máquina; a chave que decifra fica fora dele: na variável
APP_SEGREDO (uma chave Fernet, ou um texto qualquer, que vira chave pelo SHA-256) ou, sem ela, no arquivo
dados/segredo.key, gerado na primeira vez (chmod 600). Perder o segredo é perder as chaves cadastradas: elas
precisam ser cadastradas de novo na interface.

    from nucleo.seguranca import cifrar, decifrar
    guardado = cifrar("sk-...")
    decifrar(guardado)   # "sk-..."
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import threading
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from nucleo.config import config

ARQUIVO_SEGREDO = Path("dados") / "segredo.key"
_avisou = False
_trava = threading.Lock()


def _material(segredo: str) -> bytes:
    """A chave Fernet: o próprio APP_SEGREDO quando ele já é uma; senão, derivada do texto pelo SHA-256."""
    bruto = segredo.strip().encode()
    try:
        Fernet(bruto)
        return bruto
    except (ValueError, TypeError):
        return base64.urlsafe_b64encode(hashlib.sha256(bruto).digest())


def _segredo_do_arquivo(raiz: Path) -> bytes:
    """Lê dados/segredo.key, ou gera um novo (criação exclusiva: dois processos não geram dois segredos)."""
    global _avisou
    arquivo = raiz / ARQUIVO_SEGREDO
    with _trava:
        if not arquivo.exists():
            arquivo.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(arquivo, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass  # outro processo acabou de gerar: vale o dele
            else:
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
                if not _avisou:
                    print(f"aviso: APP_SEGREDO não definido; gerei {arquivo} para cifrar as chaves de API. "
                          "Guarde-o (ou defina APP_SEGREDO): sem ele as chaves cadastradas não podem ser lidas.",
                          file=sys.stderr)
                    _avisou = True
        return arquivo.read_bytes().strip()


@lru_cache(maxsize=4)
def _fernet(segredo: str | None, raiz: Path) -> Fernet:
    return Fernet(_material(segredo) if segredo else _segredo_do_arquivo(raiz))


def fernet() -> Fernet:
    c = config()
    return _fernet(c.segredo_app, c.raiz)


def cifrar(texto: str) -> str:
    return fernet().encrypt(texto.encode("utf-8")).decode("ascii")


def decifrar(texto: str) -> str:
    """Levanta ValueError quando o segredo atual não abre o texto (APP_SEGREDO trocado ou segredo.key perdido)."""
    try:
        return fernet().decrypt(texto.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as e:
        raise ValueError("não consegui decifrar: o APP_SEGREDO (ou dados/segredo.key) mudou desde a gravação") from e


def mascarar(valor: str | None) -> str | None:
    """A versão que a interface pode mostrar: só os 4 últimos caracteres."""
    return None if not valor else ("•" * 8 + valor[-4:] if len(valor) > 8 else "•" * len(valor))


def redefinir() -> None:
    """Esquece as chaves em memória (testes que trocam APP_SEGREDO ou a raiz)."""
    _fernet.cache_clear()
