"""Ambiente do Alembic: o banco vem de DATABASE_URL (nucleo/config.py) e o esquema de nucleo/banco/modelos.py.

Roda de dois jeitos:
- pela linha de comando (`alembic upgrade head`, no deploy/entrypoint.sh), abrindo a própria conexão;
- de dentro do programa (`nucleo.banco.iniciar()`), que passa a conexão em `config.attributes["connection"]`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

RAIZ = Path(__file__).resolve().parents[3]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from nucleo.banco.modelos import Base  # noqa: E402
from nucleo.config import config as configuracao  # noqa: E402

cfg = context.config
alvo = Base.metadata


def _url() -> str:
    return cfg.get_main_option("sqlalchemy.url") or configuracao().banco_url


def _configurar(conexao) -> None:
    context.configure(connection=conexao, target_metadata=alvo, compare_type=True,
                      render_as_batch=conexao.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()


def offline() -> None:
    context.configure(url=_url(), target_metadata=alvo, literal_binds=True,
                      dialect_opts={"paramstyle": "named"}, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def online() -> None:
    if (conexao := cfg.attributes.get("connection")) is not None:
        _configurar(conexao)
        return
    motor = create_engine(_url(), poolclass=pool.NullPool)
    with motor.connect() as conexao:
        _configurar(conexao)
        conexao.commit()


if context.is_offline_mode():
    offline()
else:
    online()
