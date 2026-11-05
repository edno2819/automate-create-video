"""Biblioteca de efeitos sonoros (Configurações > Efeitos sonoros): cada efeito tem um nome e um arquivo de áudio, e o
diretor visual escolhe entre eles os que tocam em momentos do vídeo (o formato está em ferramentas/efeitos.py).

O índice (biblioteca/efeitos/efeitos.json) muda sob trava no banco: dois cadastros ao mesmo tempo não se perdem. O id
sai do nome no cadastro e nunca muda, porque as cenas guardam o id; renomear muda só o nome. O áudio sai por
/api/arquivos/biblioteca/efeitos/<arquivo>.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile

from nucleo import banco, espaco
from nucleo.pipeline import etapas, verificacoes
from web.api.comum import (EXTENSOES_AUDIO, exigir, existe_no_espaco, ffmpeg_disponivel, gravar_upload, json_do_espaco,
                           validar_slug)

import efeitos  # noqa: E402 - ferramentas/efeitos.py: o comum põe ferramentas/ no caminho

roteador = APIRouter()

TRAVA = "biblioteca:efeitos"
LIMITE_EFEITO_MB = 20
TAMANHO_NOME = 80


def ler() -> list[dict]:
    return efeitos.lista(json_do_espaco(efeitos.INDICE))


def gravar(lista: list[dict]) -> None:
    espaco.gravar_texto(efeitos.INDICE, json.dumps({"efeitos": lista}, ensure_ascii=False, indent=2) + "\n")


def publico(e: dict) -> dict:
    return {"id": e["id"], "nome": e["nome"], "arquivo": e["arquivo"], "duracao_s": e.get("duracao_s"),
            "caminho": efeitos.PASTA + e["arquivo"]}


def achar(lista: list[dict], id_: str) -> dict:
    validar_slug(id_, "efeito")
    e = next((e for e in lista if e["id"] == id_), None)
    if e is None:
        raise HTTPException(404, "efeito não encontrado")
    return e


def nome_livre(nome, lista: list[dict], menos: str | None = None) -> str:
    """O nome limpo (espaços juntados, até TAMANHO_NOME), sem repetir o de outro efeito: o diretor visual escolhe pelo
    nome, e dois iguais não se distinguem."""
    nome = " ".join(str(nome or "").split())[:TAMANHO_NOME]
    exigir(nome, "dê um nome ao efeito")
    exigir(all(efeitos.forma(e["nome"]) != efeitos.forma(nome) for e in lista if e["id"] != menos),
           f"já existe um efeito chamado {nome}")
    return nome


@roteador.get("/api/efeitos")
def listar():
    return {"efeitos": sorted((publico(e) for e in ler()), key=lambda e: e["nome"].lower()),
            "extensoes": sorted(EXTENSOES_AUDIO), "limite_mb": LIMITE_EFEITO_MB}


@roteador.post("/api/efeitos")
def criar(nome: str = Form(""), arquivo: UploadFile = File(...)):
    """Cadastra um efeito: grava o áudio em biblioteca/efeitos/<id>.<ext>, mede a duração e o põe no índice."""
    ext = Path(arquivo.filename or "").suffix.lower()
    exigir(ext in EXTENSOES_AUDIO, f"o efeito precisa ser um áudio ({', '.join(sorted(EXTENSOES_AUDIO))})")
    with banco.trava(TRAVA):
        lista = ler()
        nome = nome_livre(nome, lista)
        base = etapas.slug_de(nome, 10) if re.search(r"[a-z0-9]", efeitos.forma(nome)) else "efeito"
        id_, n = base, 2
        while any(e["id"] == id_ for e in lista) or existe_no_espaco(f"{efeitos.PASTA}{id_}{ext}"):
            id_, n = f"{base}-{n}", n + 1
        caminho = f"{efeitos.PASTA}{id_}{ext}"
        duracao = verificacoes.duracao_midia(gravar_upload(caminho, arquivo.file, LIMITE_EFEITO_MB))
        if duracao is None and ffmpeg_disponivel():
            espaco.apagar(caminho)
            raise HTTPException(400, "não consegui ler esse arquivo como áudio")
        e = {"id": id_, "nome": nome, "arquivo": f"{id_}{ext}", "duracao_s": round(duracao, 2) if duracao else None}
        gravar([*lista, e])
    return publico(e)


@roteador.put("/api/efeitos/{id_}")
def renomear(id_: str, dados: dict = Body(...)):
    with banco.trava(TRAVA):
        lista = ler()
        e = achar(lista, id_)
        e["nome"] = nome_livre(dados.get("nome"), lista, menos=id_)
        gravar(lista)
    return publico(e)


@roteador.delete("/api/efeitos/{id_}")
def apagar(id_: str):
    """Tira o efeito do índice e apaga o áudio. As cenas que o pediam ficam sem ele na próxima montagem (com aviso)."""
    with banco.trava(TRAVA):
        lista = ler()
        e = achar(lista, id_)
        gravar([x for x in lista if x["id"] != id_])
        espaco.apagar(efeitos.PASTA + e["arquivo"])
    return {"ok": True}
