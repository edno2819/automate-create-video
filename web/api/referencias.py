"""Referências: analisar um vídeo que performou bem (o esqueleto vai para biblioteca/esqueletos/) e aprovar as
técnicas novas que a análise sugerir, que só entram em biblioteca/tecnicas.json com o sim do usuário.

A análise é uma execução sem projeto (fluxo `referencia`, agente analista-referencias), uma por vídeo (a chave
exclusiva é `referencia:<video_id>`, que a fila põe). A aprovação é do nucleo.pipeline.fluxos.aprovar_tecnicas, sob a trava das técnicas, e
nunca renomeia um id que já existe.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Body, HTTPException

from nucleo import espaco, rotas
from nucleo.banco import repositorios
from nucleo.pipeline import execucao, fluxos
from web.api.comum import exigir, garantir_se_existe, ler_json, raiz

roteador = APIRouter()

ESQUELETOS = "biblioteca/esqueletos/"
TECNICAS = "biblioteca/tecnicas.json"
ETAPA = "referencia"
ARQUIVO = re.compile(r"^[a-z0-9][a-z0-9._-]*\.json$", re.I)
VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([\w-]{11})")
LIMITE_URL = 500
CAMPOS_DA_TELA = ("fonte", "formato", "premissa", "promessa_do_titulo", "template_abstrato", "tecnicas_detectadas",
                  "tecnicas_novas_sugeridas", "tecnicas_aprovadas")


def video_id(url: str) -> str | None:
    if m := VIDEO_ID.search(url):
        return m.group(1)
    return url if re.fullmatch(r"[\w-]{11}", url) else None


def ids_da_biblioteca() -> set[str]:
    local = garantir_se_existe(TECNICAS)
    doc = ler_json(local) if local else None
    return {t["id"] for t in (doc or {}).get("tecnicas") or [] if isinstance(t, dict) and t.get("id")}


def esqueletos() -> list[dict]:
    """Os esqueletos da biblioteca, com as técnicas sugeridas e quais delas já estão em tecnicas.json."""
    espaco.baixar(ESQUELETOS)
    na_biblioteca = ids_da_biblioteca()
    saida = []
    for arquivo in sorted((raiz() / "biblioteca" / "esqueletos").glob("*.json")):
        doc = ler_json(arquivo)
        if not isinstance(doc, dict):
            continue
        item = {"arquivo": arquivo.name, **{k: doc[k] for k in CAMPOS_DA_TELA if k in doc}}
        sugeridas = [t.get("id") if isinstance(t, dict) else t for t in doc.get("tecnicas_novas_sugeridas") or []]
        aprovadas = {t for t in doc.get("tecnicas_aprovadas") or [] if isinstance(t, str)}
        item["tecnicas_aprovadas"] = sorted(aprovadas | {t for t in sugeridas if t in na_biblioteca})
        saida.append(item)
    return sorted(saida, key=lambda e: str((e.get("fonte") or {}).get("analisado_em") or ""), reverse=True)


def execucoes_de_referencia() -> tuple[dict | None, dict | None]:
    """(a análise que está na fila ou rodando, a última delas)."""
    def de_referencia(e: dict) -> bool:
        return e.get("projeto_slug") is None and e.get("etapa") == ETAPA

    ativa = next((e for e in repositorios.execucoes_ativas() if de_referencia(e)), None)
    ultima = ativa or next((e for e in repositorios.execucoes(None, 300) if de_referencia(e)), None)
    return ativa, ultima


@roteador.get("/api/referencias")
def listar_referencias():
    ativa, ultima = execucoes_de_referencia()
    return {"esqueletos": esqueletos(), "rodando": ativa, "execucao": ultima}


@roteador.post("/api/referencias")
def analisar(dados: dict = Body(...)):
    url = dados.get("url")
    exigir(isinstance(url, str) and url.strip(), "cole o link do vídeo")
    url = url.strip()
    exigir(len(url) <= LIMITE_URL, "link grande demais")
    vid = video_id(url)
    exigir(vid, "não reconheci o link: use um endereço de vídeo do YouTube (watch?v=, youtu.be, shorts)")
    espaco.baixar(ESQUELETOS)
    existente = next((a.name for a in (raiz() / "biblioteca" / "esqueletos").glob(f"*{vid}.json")), None)
    exigir(existente is None, f"esse vídeo já foi analisado: {existente}", 409)
    problemas = rotas.problemas("referencia")
    exigir(not problemas, "\n".join(problemas))
    try:
        id_ = execucao.enfileirar(None, ETAPA, {"url": url, "video_id": vid})
    except repositorios.ErroOcupado:
        raise HTTPException(409, "esse vídeo já está sendo analisado: espere terminar ou cancele") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"id": id_, "video_id": vid}


@roteador.post("/api/referencias/{arquivo}/tecnicas")
def aprovar_tecnicas(arquivo: str, dados: dict = Body(...)):
    """As técnicas sugeridas no esqueleto que o usuário aprovou entram em biblioteca/tecnicas.json."""
    exigir(ARQUIVO.fullmatch(arquivo or ""), "esqueleto não encontrado", 404)
    ids = dados.get("aprovar")
    exigir(isinstance(ids, list) and ids and all(isinstance(i, str) and i for i in ids), "diga quais técnicas aprovar")
    espaco.baixar(ESQUELETOS)
    garantir_se_existe(TECNICAS)
    exigir((raiz() / "biblioteca" / "esqueletos" / arquivo).is_file(), "esqueleto não encontrado", 404)
    try:
        resultado = fluxos.aprovar_tecnicas(arquivo, list(dict.fromkeys(ids)))
    except (KeyError, LookupError, FileNotFoundError) as e:
        raise HTTPException(404, f"técnica ou esqueleto não encontrado: {e}") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    espaco.enviar(TECNICAS)
    espaco.enviar(f"{ESQUELETOS}{arquivo}")
    return {"ok": True, "aprovadas": resultado}
