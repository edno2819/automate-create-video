"""Espera o banco responder e aplica as migrações antes de a web, o trabalhador ou a migração subirem.

No docker-compose, a web e o trabalhador sobem juntos (e a web pode ter várias réplicas): todos chegam aqui ao
mesmo tempo, às vezes antes de o Postgres aceitar conexões. Este script tenta conectar até ESPERA_BANCO_S
segundos e então chama `nucleo.banco.iniciar()`, que no Postgres roda o `alembic upgrade head` sob uma trava
consultiva (só um contêiner migra; os outros encontram o esquema pronto) e no SQLite cria o esquema.

    python deploy/preparar_banco.py            # espera até ESPERA_BANCO_S segundos (padrão 90)

Saída 0 com o banco pronto; 1 se o banco não respondeu a tempo ou a migração falhou. Nunca mostra a senha.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))


def sem_senha(url: str) -> str:
    """A URL para mensagens, sem usuário e senha."""
    if "@" not in url:
        return url
    esquema, _, resto = url.partition("://")
    return f"{esquema}://…@{resto.rsplit('@', 1)[1]}"


def esperar(url: str, limite_s: float, pausa_max_s: float = 2.0) -> None:
    """Tenta conectar até o banco responder; levanta TimeoutError com o último erro se o prazo acabar."""
    from sqlalchemy import create_engine, text

    engine = create_engine(url)
    inicio, tentativa, ultimo = time.monotonic(), 0, None
    try:
        while True:
            tentativa += 1
            try:
                with engine.connect() as con:
                    con.execute(text("SELECT 1"))
                if tentativa > 1:
                    print(f"banco respondeu depois de {tentativa} tentativas", file=sys.stderr)
                return
            except Exception as e:  # noqa: BLE001 — qualquer falha de conexão vale nova tentativa
                ultimo = e
            if time.monotonic() - inicio >= limite_s:
                # só o tipo do erro: a mensagem do driver pode trazer o endereço com usuário
                raise TimeoutError(f"o banco em {sem_senha(url)} não respondeu em {limite_s:.0f} s "
                                   f"({type(ultimo).__name__})")
            if tentativa == 1:
                print(f"esperando o banco em {sem_senha(url)}…", file=sys.stderr)
            time.sleep(min(pausa_max_s, 0.25 * tentativa))
    finally:
        engine.dispose()


def main() -> int:
    from nucleo import banco
    from nucleo.config import config

    url = config().banco_url
    try:
        if not url.startswith("sqlite"):
            esperar(url, float(os.environ.get("ESPERA_BANCO_S") or 90))
        banco.iniciar()
    except TimeoutError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"erro: não consegui preparar o banco ({type(e).__name__}: {e})", file=sys.stderr)
        return 1
    print(f"banco pronto: {sem_senha(url)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
