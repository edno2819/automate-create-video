"""Arquivos da biblioteca e dos rascunhos (imagens dos personagens em criação, esqueletos...) para a tela mostrar.

Só os prefixos da lista branca saem por aqui; os de projeto têm a rota própria (/api/projetos/{slug}/arquivos/...) e
os anexos dos contextos também (/api/contextos/{id}/arquivos/...). Nada fora da raiz, nada com `..`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from web.api.comum import caminho_dentro, servir

roteador = APIRouter()

PERMITIDOS = ("biblioteca/", "rascunhos/")


@roteador.get("/api/arquivos/{caminho:path}")
def arquivo(caminho: str):
    prefixo = next((p for p in PERMITIDOS if caminho.startswith(p)), None)
    if prefixo is None:
        raise HTTPException(404, "arquivo não encontrado")
    return servir(caminho_dentro(prefixo, caminho[len(prefixo):]))
