"""Opções do formulário do pedido e o que a biblioteca oferece a ele: vozes de cada fornecedor (com a amostra para
ouvir), músicas, personagens salvos e contextos.

Nada aqui chama API de provedor: as vozes vêm dos catálogos fixos (Gemini, OpenAI, espeak) e do cache das vozes da
conta da ElevenLabs (atualizado quando a chave é testada); as amostras gravadas ficam em biblioteca/vozes/.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

from nucleo.pipeline import etapas
from nucleo.provedores import voz as provedores_voz
from web import contextos
from web.api import formulario
from web.api.comum import EXTENSOES_AUDIO, raiz, servir, vivos
from web.api.personagens import personagens_salvos
from web.api.projetos import etapa_dict

import pedido  # noqa: E402 - ferramentas/pedido.py: o comum põe ferramentas/ no caminho

roteador = APIRouter()

VOZES = "biblioteca/vozes/"
MUSICAS = "biblioteca/musicas/"
NOME_DE_VOZ = re.compile(r"[A-Za-z]+")


def arquivos_na_pasta(prefixo: str, extensoes: frozenset | set) -> list[str]:
    """Os nomes dos arquivos logo abaixo da pasta, no armazenamento ou na cópia local."""
    nomes = {c[len(prefixo):] for c in vivos(prefixo) if "/" not in c[len(prefixo):]}
    local = raiz() / prefixo
    if local.is_dir():
        nomes |= {p.name for p in local.iterdir() if p.is_file() and not p.name.startswith(".")}
    return sorted(n for n in nomes if Path(n).suffix.lower() in extensoes)


def vozes_por_fornecedor() -> dict[str, list[dict]]:
    """As vozes de cada fornecedor de narração, com a amostra para ouvir: as gravadas em biblioteca/vozes/
    (narrar.py --amostras) ou, na ElevenLabs, a prévia que a própria conta oferece."""
    amostras = {Path(n).stem for n in arquivos_na_pasta(VOZES, {".mp3"})}
    saida = {}
    for fornecedor in provedores_voz.FORNECEDORES:
        try:
            catalogo = provedores_voz.catalogo(fornecedor)
        except Exception:  # noqa: BLE001 - um fornecedor que não carrega não derruba o formulário
            catalogo = []
        saida[fornecedor] = [{"nome": v.nome, "descricao": v.descricao, "genero": v.genero or "?",
                              "amostra": v.amostra_url or (f"/api/biblioteca/vozes/{v.nome}" if v.nome in amostras else None)}
                             for v in catalogo]
    return saida


@roteador.get("/api/opcoes")
def opcoes():
    por_fornecedor = vozes_por_fornecedor()
    return {"campos": formulario.campos(), "vozes": por_fornecedor.get("gemini", []), "vozes_por_fornecedor": por_fornecedor,
            "voz_do_canal": pedido.voz_do_canal({}), "musicas": arquivos_na_pasta(MUSICAS, EXTENSOES_AUDIO),
            "contextos": contextos.listar(), "personagens": personagens_salvos(), "cores": pedido.CORES,
            "modos_voz": etapas.MODOS_VOZ, "etapas": [etapa_dict(e) for e in etapas.ETAPAS]}


@roteador.get("/api/biblioteca/vozes/{nome}")
def amostra_voz(nome: str):
    if not NOME_DE_VOZ.fullmatch(nome or ""):
        raise HTTPException(404, "voz sem amostra")
    return servir(f"{VOZES}{nome}.mp3", media_type="audio/mpeg")
