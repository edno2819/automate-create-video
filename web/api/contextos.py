"""Contextos salvos: lista, edição (texto e descrição dos anexos), anexos e os arquivos para a tela mostrar.

As regras ficam em web/contextos.py; aqui só a tradução para HTTP: id ou anexo desconhecido é 404, operação
recusada (contexto antigo, anexo de tipo errado) é 400.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile

from web import contextos as servico
from web.api.comum import exigir, servir

roteador = APIRouter()


def contexto_ou_erro(acao, *args):
    try:
        return acao(*args)
    except KeyError:
        raise HTTPException(404, "contexto ou anexo não encontrado") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@roteador.get("/api/contextos")
def listar_contextos():
    return servico.listar()


@roteador.post("/api/contextos")
def criar_contexto(dados: dict = Body(...)):
    nome = dados.get("nome")
    exigir(isinstance(nome, str) and nome.strip(), "o nome é obrigatório")
    texto = dados.get("texto") if isinstance(dados.get("texto"), str) else ""
    return contexto_ou_erro(servico.criar, nome, texto.strip())


@roteador.get("/api/contextos/{id_}")
def ver_contexto(id_: str):
    c = contexto_ou_erro(servico.ler, id_)
    if c is None:
        raise HTTPException(404, "contexto não encontrado")
    return c


@roteador.put("/api/contextos/{id_}")
def salvar_contexto(id_: str, dados: dict = Body(...)):
    nome = dados.get("nome") if isinstance(dados.get("nome"), str) else ""
    texto = dados.get("texto") if isinstance(dados.get("texto"), str) else ""
    descricoes = dados.get("descricoes") if isinstance(dados.get("descricoes"), dict) else {}
    return contexto_ou_erro(servico.salvar, id_, nome, texto, descricoes)


@roteador.delete("/api/contextos/{id_}")
def apagar_contexto(id_: str):
    contexto_ou_erro(servico.apagar, id_)
    return {"ok": True}


@roteador.post("/api/contextos/{id_}/anexos")
def anexar_ao_contexto(id_: str, arquivo: UploadFile = File(...), descricao: str = Form("")):
    return contexto_ou_erro(servico.anexar, id_, arquivo.filename or "arquivo", arquivo.file, descricao)


@roteador.delete("/api/contextos/{id_}/anexos/{nome}")
def remover_anexo_do_contexto(id_: str, nome: str):
    return contexto_ou_erro(servico.remover_anexo, id_, nome)


@roteador.get("/api/contextos/{id_}/arquivos/{nome}")
def anexo_do_contexto(id_: str, nome: str):
    caminho = contexto_ou_erro(servico.arquivo, id_, nome)
    if caminho is None:
        raise HTTPException(404, "anexo não encontrado")
    return servir(caminho)
