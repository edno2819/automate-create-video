"""Compatibilidade: as chaves de API agora moram em nucleo/chaves.py (ambiente > banco cifrado > .env legado).

Os scripts antigos continuam funcionando com:

    sys.path.insert(0, str(RAIZ / "ferramentas"))
    import chaves
    chave = chaves.ler("openai")
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from nucleo.chaves import *  # noqa: E402,F401,F403
from nucleo.chaves import (LINHA, PROVEDORES, ambiente, gravar, ler, ler_env, mascarar, origem,  # noqa: E402,F401
                           variaveis)
