"""Personagens: os salvos na biblioteca (biblioteca/personagens/<slug>/) e os rascunhos em criação
(rascunhos/personagens/<slug>/: a ficha, cada versão gerada com a imagem e o vN.json, e a descrição do usuário).

Criar e gerar são execuções sem projeto, com a chave exclusiva `personagem:<slug>` (uma por rascunho), no fluxo
`personagem` do trabalhador: `{"subetapa": "ficha"}` escreve a ficha (designer-personagem) a partir da descrição e
gera a primeira imagem; `{"subetapa": "gerar", "base", "ajuste"}` gera outra versão (com a base, um ajuste dela).
Aprovar é rápido e determinístico (`ferramentas/personagem.py aprovar`): roda aqui mesmo e a tela já volta com o
personagem na biblioteca.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

from nucleo import espaco, rotas
from nucleo.banco import repositorios
from nucleo.pipeline import execucao, etapas
from web.api.comum import (EXTENSOES_IMAGEM, SLUG, exigir, garantir_se_existe, json_do_espaco, ler_json, ler_texto, raiz,
                           rodar_script, servir, validar_slug, vivos)

roteador = APIRouter()

BIBLIOTECA = "biblioteca/personagens/"
RASCUNHOS = "rascunhos/personagens/"
DESCRICAO = "descricao.md"  # o que o usuário escreveu ao criar (a ficha sai dela)
VERSAO = re.compile(r"^v(\d+)$")
LIMITE_DESCRICAO = 20_000
LIMITE_AJUSTE = 2_000


def chave_exclusiva(slug: str) -> str:
    return f"personagem:{slug}"


def versao_de(registro: dict) -> str:
    return (registro.get("sha256") or str(registro.get("versao") or ""))[:12]


def por_pasta(prefixo: str) -> dict[str, dict[str, dict]]:
    """{slug: {nome do arquivo: registro}} dos arquivos vivos logo abaixo de prefixo/<slug>/."""
    saida: dict[str, dict[str, dict]] = defaultdict(dict)
    for caminho, r in vivos(prefixo).items():
        partes = caminho[len(prefixo):].split("/")
        if len(partes) == 2 and SLUG.fullmatch(partes[0]):
            saida[partes[0]][partes[1]] = r
    return saida


# ---------------------------------------------------------------- biblioteca


def personagens_salvos() -> list[dict]:
    """Os personagens aprovados, pelo índice do armazenamento e o texto do personagem.json (sem baixar as imagens)."""
    saida = []
    for slug, arquivos in sorted(por_pasta(BIBLIOTECA).items()):
        if "personagem.json" not in arquivos:
            continue
        d = json_do_espaco(f"{BIBLIOTECA}{slug}/personagem.json") or {}
        voz = d.get("voz") if isinstance(d.get("voz"), dict) else {}
        estilo = d.get("estilo") if isinstance(d.get("estilo"), dict) else {}
        imagem = next((n for n in sorted(arquivos) if n.startswith("imagem.") and Path(n).suffix.lower() in EXTENSOES_IMAGEM), None)
        geracao = d.get("geracao") if isinstance(d.get("geracao"), dict) else {}
        saida.append({"slug": slug, "nome": d.get("nome") or slug, "resumo": d.get("resumo", ""),
                      "voz": voz.get("voz"), "estilo": voz.get("estilo"), "cor": voz.get("cor"),
                      "descricao": d.get("descricao_en"), "estilo_visual": estilo.get("nome"),
                      "imagem": f"/api/biblioteca/personagens/{slug}/imagem?v={versao_de(arquivos[imagem])}" if imagem else None,
                      "versao_aprovada": geracao.get("versao"), "rascunho": geracao.get("rascunho")})
    return saida


@roteador.get("/api/biblioteca/personagens/{slug}/imagem")
def imagem_personagem(slug: str):
    validar_slug(slug, "personagem")
    arquivos = por_pasta(BIBLIOTECA).get(slug, {})
    imagem = next((n for n in sorted(arquivos) if n.startswith("imagem.") and Path(n).suffix.lower() in EXTENSOES_IMAGEM), None)
    if imagem is None:
        raise HTTPException(404, "personagem sem imagem")
    return servir(f"{BIBLIOTECA}{slug}/{imagem}")


# ---------------------------------------------------------------- rascunhos


def execucoes_do_rascunho(slug: str) -> tuple[dict | None, dict | None]:
    """(a ativa, a última) das execuções do rascunho."""
    chave = chave_exclusiva(slug)
    ativa = next((e for e in repositorios.execucoes_ativas() if e.get("chave_exclusiva") == chave), None)
    ultima = ativa or next((e for e in repositorios.execucoes(None, 300) if e.get("chave_exclusiva") == chave), None)
    return ativa, ultima


def numero(versao: str) -> int:
    m = VERSAO.match(versao or "")
    return int(m.group(1)) if m else -1


def versao_da_base(base) -> str | None:
    """A base de uma versão como o rascunho a nomeia (v2), a partir do caminho que o personagem.py grava."""
    if not isinstance(base, str) or not base:
        return None
    nome = Path(base).stem
    return nome if VERSAO.match(nome) else base


def revisao_da_versao(pasta: Path, versao: str, registro: dict):
    """A autorrevisão da imagem (revisor-visual), no vN.json ou num arquivo ao lado."""
    for chave in ("revisao", "autorrevisao"):
        if isinstance(registro.get(chave), dict):
            return registro[chave]
    for nome in (f"{versao}-revisao.json", f"revisao-{versao}.json"):
        if isinstance(doc := ler_json(pasta / nome), dict):
            return doc
    return None


def versoes_do_rascunho(slug: str, pasta: Path) -> list[dict]:
    saida = []
    for arquivo in sorted(pasta.glob("v*.json"), key=lambda a: numero(a.stem)):
        if not VERSAO.match(arquivo.stem):
            continue
        registro = ler_json(arquivo) or {}
        imagem = registro.get("arquivo") if isinstance(registro.get("arquivo"), str) else None
        if not imagem or not (pasta / imagem).is_file():
            imagem = next((f"{arquivo.stem}.{ext}" for ext in ("png", "jpg", "webp") if (pasta / f"{arquivo.stem}.{ext}").is_file()), None)
        saida.append({"versao": arquivo.stem, "arquivo": imagem,
                      "imagem": f"{RASCUNHOS}{slug}/{imagem}" if imagem else None,
                      "base": versao_da_base(registro.get("base")), "ajuste": registro.get("ajuste"),
                      "modelo": registro.get("modelo"), "provedor": registro.get("provedor"),
                      "gerado_em": registro.get("gerado_em"), "revisao": revisao_da_versao(pasta, arquivo.stem, registro)})
    return saida


def aprovada_no_rascunho(slug: str) -> tuple[bool, str | None, dict]:
    """(está na biblioteca, versão do rascunho que foi aprovada, personagem.json)."""
    d = json_do_espaco(f"{BIBLIOTECA}{slug}/personagem.json")
    if not isinstance(d, dict):
        return False, None, {}
    geracao = d.get("geracao") if isinstance(d.get("geracao"), dict) else {}
    do_rascunho = (geracao.get("rascunho") or "").rstrip("/") in ("", f"{RASCUNHOS}{slug}".rstrip("/"))
    return True, geracao.get("versao") if do_rascunho else None, d


def rascunhos() -> list[dict]:
    """Os rascunhos em criação, para a lista: nome, resumo, quantas versões e a imagem da mais nova."""
    ativas = {e.get("chave_exclusiva") for e in repositorios.execucoes_ativas()}
    na_biblioteca = {p["slug"]: p for p in personagens_salvos()}
    saida = []
    for slug, arquivos in sorted(por_pasta(RASCUNHOS).items()):
        ficha = json_do_espaco(f"{RASCUNHOS}{slug}/ficha.json") if "ficha.json" in arquivos else None
        ficha = ficha if isinstance(ficha, dict) else {}
        versoes = sorted((n[:-5] for n in arquivos if n.endswith(".json") and VERSAO.match(n[:-5])), key=numero)
        imagem = None
        if versoes:
            imagem = next((f"{versoes[-1]}.{ext}" for ext in ("png", "jpg", "webp") if f"{versoes[-1]}.{ext}" in arquivos), None)
        nome = ficha.get("nome")
        if not nome and DESCRICAO in arquivos:
            nome = re.sub(r"^#\s*", "", (repositorios.texto_do_arquivo(f"{RASCUNHOS}{slug}/{DESCRICAO}") or "").split("\n", 1)[0]).strip()
        rodando = chave_exclusiva(slug) in ativas
        salvo = na_biblioteca.get(slug)
        pendente = rodando or bool(salvo and versoes and salvo.get("versao_aprovada") != versoes[-1])
        saida.append({"slug": slug, "nome": nome or slug, "resumo": ficha.get("resumo", ""), "versoes": len(versoes),
                      "voz": (ficha.get("voz") or {}).get("voz") if isinstance(ficha.get("voz"), dict) else None,
                      "imagem": f"/api/arquivos/{RASCUNHOS}{slug}/{imagem}?v={versao_de(arquivos[imagem])}" if imagem else None,
                      "pendente": pendente, "rodando": rodando})
    return saida


@roteador.get("/api/personagens")
def listar_personagens():
    return {"biblioteca": personagens_salvos(), "rascunhos": rascunhos()}


@roteador.get("/api/personagens/rascunhos/{slug}")
def ver_rascunho(slug: str):
    validar_slug(slug, "rascunho")
    espaco.baixar(f"{RASCUNHOS}{slug}/")
    pasta = raiz() / "rascunhos" / "personagens" / slug
    ativa, ultima = execucoes_do_rascunho(slug)
    ficha = ler_json(pasta / "ficha.json")
    descricao = ler_texto(pasta / DESCRICAO)
    versoes = versoes_do_rascunho(slug, pasta) if pasta.is_dir() else []
    if ficha is None and descricao is None and not versoes and ativa is None:
        raise HTTPException(404, "rascunho não encontrado")
    na_biblioteca, aprovada, salvo = aprovada_no_rascunho(slug)
    ficha = ficha if isinstance(ficha, dict) else {}
    nome = ficha.get("nome") or salvo.get("nome")
    if not nome and descricao:
        nome = re.sub(r"^#\s*", "", descricao.split("\n", 1)[0]).strip()
    return {"slug": slug, "nome": nome or slug, "ficha": ficha, "resumo": ficha.get("resumo") or salvo.get("resumo"),
            "descricao_usuario": descricao, "versoes": versoes, "aprovada": aprovada, "na_biblioteca": na_biblioteca,
            "rodando": ativa, "execucao": ultima}


def enfileirar_personagem(slug: str, parametros: dict) -> int:
    try:
        return execucao.enfileirar(None, "personagem", {"slug": slug, **parametros}, chave_exclusiva=chave_exclusiva(slug))
    except repositorios.ErroOcupado:
        raise HTTPException(409, "já há uma execução neste personagem: espere terminar ou cancele") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


def slug_livre_de_personagem(base: str) -> str:
    ocupados = set(por_pasta(RASCUNHOS)) | set(por_pasta(BIBLIOTECA))
    slug, n = base, 2
    while slug in ocupados or (raiz() / "rascunhos" / "personagens" / slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


@roteador.post("/api/personagens")
def criar_personagem(dados: dict = Body(...)):
    """Um personagem novo a partir da descrição: reserva o slug (o rascunho nasce com a descrição do usuário) e
    enfileira a ficha e a primeira imagem."""
    descricao = dados.get("descricao")
    exigir(isinstance(descricao, str) and descricao.strip(), "descreva o personagem")
    exigir(len(descricao) <= LIMITE_DESCRICAO, "a descrição é grande demais")
    nome = dados.get("nome") if isinstance(dados.get("nome"), str) else ""
    nome = nome.strip()[:120]
    problemas = rotas.problemas("personagem")
    exigir(not problemas, "\n".join(problemas))
    slug = slug_livre_de_personagem(etapas.slug_de(nome or descricao, 3 if not nome else 6))
    texto = (f"# {nome}\n\n" if nome else "# (sem nome: o designer propõe)\n\n") + descricao.strip() + "\n"
    espaco.gravar_texto(f"{RASCUNHOS}{slug}/{DESCRICAO}", texto)
    id_ = enfileirar_personagem(slug, {"subetapa": "ficha", "descricao": descricao.strip(), "nome": nome or None})
    return {"slug": slug, "id": id_}


@roteador.post("/api/personagens/rascunhos/{slug}/gerar")
def gerar_versao(slug: str, dados: dict = Body(default={})):
    """Outra versão da imagem: com `base` (vN) e `ajuste`, um ajuste daquela versão; sem eles, uma variação nova a
    partir da ficha."""
    validar_slug(slug, "rascunho")
    espaco.baixar(f"{RASCUNHOS}{slug}/")
    pasta = raiz() / "rascunhos" / "personagens" / slug
    exigir((pasta / "ficha.json").exists(), "o personagem ainda não tem ficha", 404)
    base = dados.get("base") if isinstance(dados.get("base"), str) and dados.get("base") else None
    ajuste = dados.get("ajuste") if isinstance(dados.get("ajuste"), str) else ""
    exigir(len(ajuste) <= LIMITE_AJUSTE, "o ajuste é grande demais")
    if base is not None:
        exigir(VERSAO.match(base) and (pasta / f"{base}.json").exists(), f"não existe a versão {base}")
    exigir(not ajuste.strip() or base, "o ajuste precisa da versão de base")
    problemas = rotas.problemas("personagem")
    exigir(not problemas, "\n".join(problemas))
    id_ = enfileirar_personagem(slug, {"subetapa": "gerar", "base": base, "ajuste": ajuste.strip() or None})
    return {"slug": slug, "id": id_}


@roteador.post("/api/personagens/rascunhos/{slug}/aprovar")
def aprovar_versao(slug: str, dados: dict = Body(...)):
    """Guarda a versão aprovada na biblioteca (imagem, fisico.md, comportamento.md, personagem.json). Trocar um
    personagem que já está lá precisa de `sobrescrever`."""
    validar_slug(slug, "rascunho")
    versao = dados.get("versao")
    exigir(isinstance(versao, str) and VERSAO.match(versao), "versão inválida (use v1, v2...)")
    ativa, _ = execucoes_do_rascunho(slug)
    exigir(ativa is None, "espere a execução deste personagem terminar antes de aprovar", 409)
    espaco.baixar(f"{RASCUNHOS}{slug}/")
    pasta = raiz() / "rascunhos" / "personagens" / slug
    exigir((pasta / "ficha.json").exists() and (pasta / f"{versao}.json").exists(), f"não existe a versão {versao}", 404)
    ficha = ler_json(pasta / "ficha.json") or {}
    destino = ficha.get("slug") if isinstance(ficha.get("slug"), str) and SLUG.fullmatch(ficha.get("slug") or "") else slug
    espaco.baixar(f"{BIBLIOTECA}{destino}/")
    garantir_se_existe("biblioteca/canal.json")
    args = ["aprovar", f"{RASCUNHOS}{slug}", "--versao", versao] + (["--sobrescrever"] if dados.get("sobrescrever") else [])
    codigo, saida = rodar_script("ferramentas/personagem.py", *args)
    espaco.enviar(f"{BIBLIOTECA}{destino}/")
    if codigo != 0:
        raise HTTPException(400, saida or f"personagem.py aprovar saiu com código {codigo}")
    return {"ok": True, "slug": destino, "versao": versao, "saida": saida}
