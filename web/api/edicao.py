"""A etapa Edição: as peças do vídeo pronto e as exportações, e a conferência do que a tela pede para dublar e
exportar antes de enfileirar (quem dubla e exporta é o trabalhador: nucleo/pipeline/fluxos.py, `dublagem` e
`exportar`).

As peças (nucleo/edicao.py) são o vídeo sem som e sem legendas, as faixas de voz (a narração original e cada
dublagem, com as legendas na língua dela) e as músicas da biblioteca. A tela combina as peças numa prévia, no próprio
navegador, e manda exportar a combinação escolhida.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

from nucleo import chaves, edicao, espaco, idiomas, provedores, rotas
from nucleo.pipeline import etapas, verificacoes
from nucleo.provedores import voz as provedores_voz
from web.api.biblioteca import arquivos_na_pasta
from web.api.comum import (EXTENSOES_AUDIO, exigir, exigir_sem_execucao, existe_no_espaco, ler_json,
                           sincronizar_projeto, validar_slug)
from web.api.projetos import carregar_projeto

roteador = APIRouter()

INICIO_MUSICA = re.compile(r"^\d{1,3}:\d{2}$")
NOME_DE_VOZ = re.compile(r"^[\w .'()-]{1,100}$")


def video_final(pasta: Path) -> str | None:
    videos = list((pasta / "10-video").glob("*.mp4"))
    alvo = pasta / "10-video" / f"{pasta.name}.mp4"
    escolhido = alvo if alvo.exists() else max(videos, key=lambda a: a.stat().st_mtime, default=None)
    return escolhido.relative_to(pasta).as_posix() if escolhido else None


@roteador.get("/api/projetos/{slug}/edicao")
def estado_da_edicao(slug: str):
    validar_slug(slug)
    pasta = sincronizar_projeto(slug)
    b = ler_json(pasta / "00-brief.json") or {}
    base = pasta / edicao.VIDEO_BASE
    musicas = arquivos_na_pasta(edicao.MUSICAS, EXTENSOES_AUDIO)
    original = edicao.idioma_original(b)
    narracao = b.get("narracao") or {}
    return {
        "video": {"arquivo": edicao.VIDEO_BASE, "duracao_s": verificacoes.duracao_midia(base)} if base.exists() else None,
        "video_final": video_final(pasta),
        "idioma_original": original,
        "vozes": edicao.faixas(pasta, b),
        "exportacoes": edicao.exportacoes(pasta),
        "musicas": musicas,
        "musica_do_pedido": edicao.musica_do_pedido(b, musicas),
        "idiomas": [i for i in idiomas.lista() if not idiomas.mesmo(i["id"], original)],
        "narracao": {"fornecedor": rotas.narracao(narracao.get("fornecedor"))[0], "voz": narracao.get("voz")},
        "fornecedores": [{"id": f["id"], "nome": f["nome"], "pronto": not f["precisa_chave"] or bool(chaves.ler(f["id"]))}
                         for f in provedores_voz.info()],
        "legendas_ativas": (b.get("legendas") or {}).get("ativas", True) is not False,
        "formato": b.get("formato"),
    }


@roteador.delete("/api/projetos/{slug}/dublagens/{codigo}")
def apagar_dublagem(slug: str, codigo: str):
    carregar_projeto(slug)
    idioma = idiomas.achar(codigo)
    if idioma is None or idioma.codigo != codigo:
        raise HTTPException(404, "dublagem não encontrada")
    exigir_sem_execucao(slug)
    return {"apagados": espaco.apagar(f"projetos/{slug}/{edicao.DUBLAGENS}/{idioma.codigo}/")}


@roteador.delete("/api/projetos/{slug}/exportacoes/{nome}")
def apagar_exportacao(slug: str, nome: str):
    carregar_projeto(slug)
    if not edicao.NOME_EXPORTACAO.fullmatch(nome or "") or len(nome) > edicao.TAMANHO_NOME:
        raise HTTPException(404, "exportação não encontrada")
    exigir_sem_execucao(slug)
    apagados = 0
    for ext in (*edicao.SAIDAS.values(), "srt", "json"):
        caminho = f"projetos/{slug}/{edicao.EXPORTACOES}/{nome}.{ext}"
        if existe_no_espaco(caminho):
            apagados += espaco.apagar(caminho)
    if not apagados:
        raise HTTPException(404, "exportação não encontrada")
    return {"apagados": apagados}


# ---------------------------------------------------------------- o que a tela pede


def parametros(etapa: str, slug: str, pasta: Path, dados: dict) -> dict:
    """Confere o pedido de dublagem ou de exportação e devolve os parâmetros do fluxo. HTTPException se faltar algo."""
    exigir(etapas.pronta(slug, "producao"), "produza o vídeo antes da edição")
    b = ler_json(pasta / "00-brief.json") or {}
    if etapa == "dublagem":
        return parametros_da_dublagem(pasta, b, dados)
    return parametros_da_exportacao(pasta, b, dados)


def parametros_da_dublagem(pasta: Path, b: dict, dados: dict) -> dict:
    exigir((pasta / "09-narracao" / "tempos.json").exists(),
           "o vídeo não tem narração alinhada: não há fala para dublar")
    pedidos = dados.get("idiomas")
    exigir(isinstance(pedidos, list) and pedidos, "escolha ao menos uma língua")
    exigir(len(pedidos) <= len(idiomas.IDIOMAS), "línguas demais")
    codigos = []
    for c in pedidos:
        idioma = idiomas.achar(c) if isinstance(c, str) else None
        exigir(idioma is not None, f"língua desconhecida: {c}")
        exigir(not idiomas.mesmo(idioma.codigo, edicao.idioma_original(b)), f"o vídeo já é em {idioma.nome}")
        codigos.append(idioma.codigo)
    fornecedor = dados.get("fornecedor") or None
    exigir(fornecedor is None or fornecedor in provedores.VOZ, "fornecedor de voz desconhecido")
    voz = dados.get("voz") or None
    exigir(voz is None or (isinstance(voz, str) and NOME_DE_VOZ.fullmatch(voz)), "nome de voz inválido")
    teste = bool(dados.get("teste"))
    if not teste:
        efetivo, _ = rotas.narracao(fornecedor or (b.get("narracao") or {}).get("fornecedor"))
        classe = provedores.classe("voz", efetivo)
        exigir(not classe.precisa_chave or chaves.ler(efetivo),
               f"a voz da dublagem é do {classe.nome}, sem chave cadastrada: cadastre em Configurações > Chaves de API, "
               "escolha outro fornecedor ou use o modo teste")
    return {"idiomas": list(dict.fromkeys(codigos)), "fornecedor": fornecedor, "voz": voz, "teste": teste,
            "retraduzir": bool(dados.get("retraduzir"))}


def nome_livre(pasta: Path, base: str) -> str:
    nome, n = base, 2
    while (pasta / edicao.EXPORTACOES / f"{nome}.json").exists():
        nome, n = f"{base}-{n}", n + 1
    return nome


def parametros_da_exportacao(pasta: Path, b: dict, dados: dict) -> dict:
    faixas = {f["id"]: f for f in edicao.faixas(pasta, b)}
    voz = dados.get("voz") or None
    exigir(voz is None or (voz in faixas and faixas[voz]["audio"]), "faixa de voz desconhecida ou ainda sem áudio")
    legendas = dados.get("legendas") or None
    exigir(legendas is None or (legendas in faixas and faixas[legendas]["ass"]), "não há legendas dessa faixa para queimar")
    musica = dados.get("musica") or None
    if musica is not None:
        exigir(isinstance(musica, str) and musica.startswith(edicao.MUSICAS) and "/" not in musica[len(edicao.MUSICAS):]
               and ".." not in musica and existe_no_espaco(musica), "música não encontrada na biblioteca de músicas")
    volume = dados.get("volume")
    if volume is not None:
        exigir(isinstance(volume, (int, float)) and not isinstance(volume, bool) and 0 <= volume <= 100,
               "o volume da música vai de 0 a 100")
    inicio = (dados.get("inicio") or "").strip() if isinstance(dados.get("inicio"), str) else ""
    exigir(not inicio or INICIO_MUSICA.fullmatch(inicio), "o início da música é no formato m:ss")
    saidas = dados.get("saidas") or list(edicao.SAIDAS)
    exigir(isinstance(saidas, list) and saidas and set(saidas) <= set(edicao.SAIDAS), "escolha o que exportar: vídeo e/ou áudio")
    exigir("audio" not in saidas or voz or musica, "sem voz e sem música não há áudio para exportar")
    if isinstance(dados.get("nome"), str) and dados["nome"].strip():
        nome = edicao.nome_de_exportacao(dados["nome"])
        exigir(nome, "nome de exportação inválido")
    else:
        partes = [voz or "sem-voz", "com-musica" if musica else "sem-musica"] + ([f"legendas-{legendas}"] if legendas else [])
        nome = nome_livre(pasta, edicao.nome_de_exportacao("-".join(partes)))
    return {"voz": voz, "musica": musica, "volume": volume, "inicio": inicio or None, "legendas": legendas,
            "saidas": [s for s in edicao.SAIDAS if s in saidas], "nome": nome}
