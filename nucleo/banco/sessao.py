"""Conexão com o banco: o motor (um por processo), as sessões, a criação do esquema e as travas entre processos.

Postgres em produção (`postgresql+psycopg://`), com o esquema criado e atualizado pelo Alembic; SQLite em
desenvolvimento e nos testes, com `create_all` (e o Alembic carimbado na última versão, para um
`alembic upgrade head` posterior não tentar criar de novo o que já existe).

As travas (`trava(nome)`) protegem o que é compartilhado entre processos e não pertence a um projeto
(a biblioteca: técnicas, lições). No Postgres, `pg_advisory_lock` numa conexão dedicada; no SQLite, uma trava
de thread mais `fcntl` num arquivo em dados/travas/.
"""

from __future__ import annotations

import fcntl
import hashlib
import re
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from nucleo import RAIZ
from nucleo.config import config

ALEMBIC_INI = RAIZ / "alembic.ini"

_motor: Engine | None = None
_fabrica: sessionmaker | None = None
_disponivel: bool | None = None
_trava_motor = threading.Lock()


def agora() -> datetime:
    return datetime.now(timezone.utc)


def e_sqlite() -> bool:
    return config().banco_e_sqlite


def _arquivo_sqlite(url: str) -> Path | None:
    caminho = url.split("///", 1)[1] if "///" in url else ""
    return Path(caminho) if caminho and caminho != ":memory:" else None


def motor() -> Engine:
    """O motor do processo, criado na primeira chamada a partir de DATABASE_URL."""
    global _motor, _fabrica
    with _trava_motor:
        if _motor is None:
            url = config().banco_url
            if url.startswith("sqlite"):
                if arquivo := _arquivo_sqlite(url):
                    arquivo.parent.mkdir(parents=True, exist_ok=True)
                _motor = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False})

                @event.listens_for(_motor, "connect")
                def _pragmas(con, _registro):
                    cur = con.cursor()
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA foreign_keys=ON")
                    cur.execute("PRAGMA busy_timeout=30000")
                    cur.close()
            else:
                _motor = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10,
                                       connect_args={"connect_timeout": 10})
            _fabrica = sessionmaker(_motor, expire_on_commit=False)
        return _motor


def redefinir() -> None:
    """Fecha o motor (testes que trocam DATABASE_URL; processos filhos depois de um fork)."""
    global _motor, _fabrica, _disponivel
    with _trava_motor:
        if _motor is not None:
            _motor.dispose()
        _motor, _fabrica, _disponivel = None, None, None


@contextmanager
def sessao() -> Iterator[Session]:
    """Uma sessão com commit no fim e rollback na exceção."""
    motor()
    s = _fabrica()
    try:
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


def _config_alembic(url: str):
    from alembic.config import Config
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(RAIZ / "nucleo" / "banco" / "migracoes"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def iniciar() -> None:
    """Cria ou atualiza o esquema. Idempotente; vários processos podem chamar ao mesmo tempo."""
    global _disponivel
    from alembic import command

    from nucleo.banco.modelos import Base

    url = config().banco_url
    if url.startswith("sqlite"):
        m = motor()
        Base.metadata.create_all(m)
        with m.begin() as con:
            versao = inspect(con).has_table("alembic_version") and con.execute(
                text("SELECT version_num FROM alembic_version")).first()
        if not versao:
            cfg = _config_alembic(url)
            with m.begin() as con:
                cfg.attributes["connection"] = con
                command.stamp(cfg, "head")
    else:
        with trava("migracao-do-esquema"):
            cfg = _config_alembic(url)
            with motor().begin() as con:
                cfg.attributes["connection"] = con
                command.upgrade(cfg, "head")
    _disponivel = True


def disponivel() -> bool:
    """O banco responde e tem o esquema? Para os scripts que rodam sem servidor (custo, chaves) seguirem sem ele.

    Um SQLite que ainda não existe conta como indisponível: um script avulso não cria um banco vazio.
    O resultado fica guardado no processo (um banco fora do ar não é testado de novo a cada chamada).
    """
    global _disponivel
    if _disponivel is not None:
        return _disponivel
    url = config().banco_url
    if url.startswith("sqlite") and (arquivo := _arquivo_sqlite(url)) and not arquivo.exists():
        _disponivel = False
        return False
    try:
        with motor().connect() as con:
            _disponivel = inspect(con).has_table("execucoes")
    except Exception as e:  # noqa: BLE001 - qualquer falha de conexão vale como "sem banco"
        print(f"aviso: banco indisponível ({type(e).__name__}); seguindo sem ele", file=sys.stderr)
        _disponivel = False
    return _disponivel


# ---------------------------------------------------------------------------------------------- travas

class _TravaLocal:
    """Trava reentrante por nome dentro do processo, com a contagem de quem a segura (a externa só na 1ª vez)."""

    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.profundidade = 0
        self.recurso = None


_travas: dict[str, _TravaLocal] = {}
_travas_guarda = threading.Lock()


def _local(nome: str) -> _TravaLocal:
    with _travas_guarda:
        return _travas.setdefault(nome, _TravaLocal())


def _nome_de_arquivo(nome: str) -> str:
    limpo = re.sub(r"[^a-z0-9_-]+", "-", nome.lower()).strip("-")[:60] or "trava"
    return f"{limpo}-{hashlib.sha1(nome.encode()).hexdigest()[:8]}.lock"


@contextmanager
def trava(nome: str) -> Iterator[None]:
    """Exclusão mútua entre processos (e threads) pelo nome. Reentrante na mesma thread."""
    t = _local(nome)
    with t.rlock:
        if t.profundidade == 0:
            t.recurso = _adquirir(nome)
        t.profundidade += 1
        try:
            yield
        finally:
            t.profundidade -= 1
            if t.profundidade == 0:
                recurso, t.recurso = t.recurso, None
                _liberar(nome, recurso)


def _adquirir(nome: str):
    if config().banco_e_sqlite:
        pasta = config().raiz / "dados" / "travas"
        pasta.mkdir(parents=True, exist_ok=True)
        arquivo = open(pasta / _nome_de_arquivo(nome), "a+b")  # noqa: SIM115 - fica aberto enquanto a trava dura
        fcntl.flock(arquivo.fileno(), fcntl.LOCK_EX)
        return arquivo
    con = motor().connect()
    try:
        con.execute(text("SELECT pg_advisory_lock(hashtext(:n))"), {"n": nome})
        con.commit()
    except BaseException:
        con.close()
        raise
    return con


def _liberar(nome: str, recurso) -> None:
    if recurso is None:
        return
    if config().banco_e_sqlite:
        try:
            fcntl.flock(recurso.fileno(), fcntl.LOCK_UN)
        finally:
            recurso.close()
        return
    try:
        recurso.execute(text("SELECT pg_advisory_unlock(hashtext(:n))"), {"n": nome})
        recurso.commit()
    except BaseException:
        # a trava é da sessão do Postgres: uma conexão que não destravou não pode voltar ao pool
        recurso.invalidate()
        raise
    finally:
        recurso.close()
