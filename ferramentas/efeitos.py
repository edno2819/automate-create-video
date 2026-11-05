"""Biblioteca de efeitos sonoros: os sons curtos (impacto, whoosh, batida de coração...) que o diretor visual põe em
momentos da narração e a montagem mistura ao áudio do vídeo.

O usuário cadastra cada efeito com um nome e um arquivo de áudio em Configurações > Efeitos sonoros
(web/api/efeitos.py). Tudo fica em biblioteca/efeitos/:

    efeitos.json   {"efeitos": [{"id": "impacto-grave", "nome": "Impacto grave", "arquivo": "impacto-grave.mp3",
                                 "duracao_s": 1.8}]}
    <arquivo>      o áudio de cada efeito

O id sai do nome no cadastro e nunca muda: as cenas guardam o id, e renomear muda só o nome. Uma cena pede um
efeito em 08-cenas/<bloco>.json (biblioteca/formato-cenas.md):

    "efeito": {"id": "impacto-grave", "palavra": "morto", "volume": 80}

Este módulo lê o índice e normaliza o pedido da cena; cenas.py confere, montar_video.py e combinar.py misturam.
Só a biblioteca padrão: os scripts de produção o importam.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from brief import espaco

PASTA = "biblioteca/efeitos/"
INDICE = f"{PASTA}efeitos.json"
VOLUME_PADRAO = 100


def lista(dados) -> list[dict]:
    """Os efeitos válidos de um índice já lido (o JSON de efeitos.json): com id, nome e um arquivo da própria pasta."""
    itens = dados.get("efeitos") if isinstance(dados, dict) else None
    validos = []
    for e in itens if isinstance(itens, list) else []:
        if not isinstance(e, dict) or not all(isinstance(e.get(k), str) and e[k].strip() for k in ("id", "nome", "arquivo")):
            continue
        if "/" in e["arquivo"] or "\\" in e["arquivo"] or e["arquivo"].startswith("."):
            continue  # o índice só aponta para arquivos desta pasta
        validos.append(e)
    return validos


def catalogo(raiz: Path | None = None) -> dict[str, dict]:
    """id -> efeito, pelo índice da cópia local ({} sem biblioteca de efeitos)."""
    try:
        dados = json.loads(((raiz or espaco()) / INDICE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {e["id"]: e for e in lista(dados)}


def arquivo(efeito: dict, raiz: Path | None = None) -> Path:
    return (raiz or espaco()) / PASTA / efeito["arquivo"]


def forma(palavra: str) -> str:
    """Forma de comparação de uma palavra: minúsculas, sem acento, só letras e dígitos."""
    sem_acento = "".join(c for c in unicodedata.normalize("NFD", str(palavra).lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[\W_]+", "", sem_acento)


def da_cena(valor) -> dict | None:
    """O efeito pedido por uma cena no formato completo ({id, palavra, volume}); aceita também só o id, como texto.
    None quando a cena não pede efeito. O que vier malformado volta como está, para cenas.py acusar."""
    if valor is None or valor == "" or valor == {}:
        return None
    if isinstance(valor, str):
        return {"id": valor.strip(), "palavra": None, "volume": VOLUME_PADRAO}
    if not isinstance(valor, dict):
        return valor
    palavra = valor.get("palavra")
    return {"id": str(valor.get("id") or "").strip(), "palavra": palavra.strip() if isinstance(palavra, str) and palavra.strip() else None,
            "volume": VOLUME_PADRAO if valor.get("volume") is None else valor["volume"]}
