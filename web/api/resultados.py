"""O que cada etapa produziu, no formato que cada tela mostra, e as decisões do usuário que viram arquivo: o ângulo
e o gancho escolhidos, uma versão editada do roteiro, o roteiro final.

Tudo é lido da cópia local do projeto, sincronizada no começo de cada requisição. As escritas sobem na hora para
o armazenamento e são recusadas (409) enquanto há execução no projeto.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

from nucleo import espaco, roteiro
from nucleo.banco import repositorios, trava
from nucleo.pipeline import etapas
from web.api.comum import (enviar_projeto, exigir, exigir_sem_execucao, ler_json, ler_texto, sincronizar_projeto,
                           validar_slug)
from web.api.projetos import carregar_projeto, ids_da_trilha

import custos  # noqa: E402 - ferramentas/custos.py: o comum põe ferramentas/ no caminho

roteador = APIRouter()

EXTENSOES_CENA = ("png", "jpg", "webp")
# arquivos em que o fluxo da produção guarda a revisão visual (uma rodada, ou a lista delas)
REVISAO = ("09-imagens/revisao.json", "09-revisao-visual.json")
LIMITE_AJUSTES = 4000
LIMITE_GANCHO = 600  # caracteres: o gancho de um vídeo longo tem umas 12 palavras


def trends_ativa(b: dict | None) -> bool:
    return bool(((b or {}).get("trends") or {}).get("ativa", True))


# ---------------------------------------------------------------- pesquisa e ângulos


@roteador.get("/api/projetos/{slug}/pesquisa")
def pesquisa(slug: str):
    validar_slug(slug)
    pasta = sincronizar_projeto(slug)
    b = ler_json(pasta / "00-brief.json") or {}
    esqueletos = [r for r in b.get("referencias", []) if isinstance(r, dict)]
    return {"trends_ativa": trends_ativa(b), "links": (b.get("trends") or {}).get("links") or [],
            "trends": ler_texto(pasta / "02-trends.md"), "dossie": ler_texto(pasta / "02-dossie.md"), "referencias": esqueletos}


@roteador.get("/api/projetos/{slug}/angulos")
def angulos(slug: str):
    validar_slug(slug)
    pasta = sincronizar_projeto(slug)
    return {"angulos": ler_json(pasta / "03-angulos.json"), "resumo": ler_texto(pasta / "03-angulos.md"),
            "escolhido": ler_json(pasta / "04-angulo-escolhido.json")}


@roteador.post("/api/projetos/{slug}/angulos/escolher")
def escolher_angulo(slug: str, dados: dict = Body(...)):
    p = carregar_projeto(slug)
    exigir_sem_execucao(slug)
    pasta = sincronizar_projeto(slug)
    doc = ler_json(pasta / "03-angulos.json") or {}
    angulo = next((a for a in doc.get("angulos", []) if isinstance(a, dict) and a.get("id") == dados.get("id")), None)
    exigir(angulo is not None, "ângulo não encontrado")
    ajustes = dados.get("ajustes") if isinstance(dados.get("ajustes"), str) else ""
    exigir(len(ajustes) <= LIMITE_AJUSTES, "os ajustes passam do tamanho máximo")
    escolhido = {**angulo, "ajustes_do_usuario": ajustes.strip() or None,
                 "escolhido_em": date.today().isoformat(), "escolhido_por": "interface web"}
    espaco.gravar_texto(f"projetos/{slug}/04-angulo-escolhido.json", json.dumps(escolhido, ensure_ascii=False, indent=2) + "\n")
    etapas.registrar(slug, "angulos")
    enviar_projeto(slug)
    if "ganchos" in ids_da_trilha(p):
        repositorios.atualizar_projeto(slug, etapa="ganchos")
    return {"ok": True}


# ---------------------------------------------------------------- ganchos


@roteador.get("/api/projetos/{slug}/ganchos")
def ganchos(slug: str):
    validar_slug(slug)
    pasta = sincronizar_projeto(slug)
    doc = ler_json(pasta / "04-ganchos.json")
    escolhido = ler_json(pasta / "04-gancho-escolhido.json")
    angulo = etapas.hash_de(pasta / "04-angulo-escolhido.json")
    return {"ganchos": doc, "escolhido": escolhido,
            # a lista e a escolha valem para o ângulo escolhido agora; as de outro ângulo aparecem como antigas
            "lista_do_angulo": bool(doc) and doc.get("angulo") == angulo,
            "escolhido_do_angulo": bool(escolhido) and escolhido.get("angulo") == angulo}


@roteador.post("/api/projetos/{slug}/ganchos/escolher")
def escolher_gancho(slug: str, dados: dict = Body(...)):
    """O gancho escolhido, com o texto que o usuário ajustou (se ajustou): é a primeira fala do roteiro."""
    p = carregar_projeto(slug)
    exigir_sem_execucao(slug)
    pasta = sincronizar_projeto(slug)
    doc = ler_json(pasta / "04-ganchos.json") or {}
    gancho = next((g for g in doc.get("ganchos", []) if isinstance(g, dict) and g.get("id") == dados.get("id")), None)
    exigir(gancho is not None, "gancho não encontrado")
    exigir(doc.get("angulo") == etapas.hash_de(pasta / "04-angulo-escolhido.json"),
           "estes ganchos são de outro ângulo: gere os ganchos de novo")
    texto = " ".join(dados["texto"].split()) if isinstance(dados.get("texto"), str) else ""
    exigir(len(texto) <= LIMITE_GANCHO, "o gancho passa do tamanho máximo")
    editado = bool(texto) and texto != gancho.get("texto")
    escolhido = {k: v for k, v in gancho.items() if k not in ("leitura_fria", "criterios", "ajustes_do_sistema")}
    escolhido.update(texto=texto if editado else gancho.get("texto"), editado_pelo_usuario=editado,
                     texto_proposto=gancho.get("texto") if editado else None, angulo=doc.get("angulo"),
                     escolhido_em=date.today().isoformat(), escolhido_por="interface web")
    espaco.gravar_texto(f"projetos/{slug}/04-gancho-escolhido.json", json.dumps(escolhido, ensure_ascii=False, indent=2) + "\n")
    etapas.registrar(slug, "ganchos")
    enviar_projeto(slug)
    if "roteiro" in ids_da_trilha(p):
        repositorios.atualizar_projeto(slug, etapa="roteiro")
    return {"ok": True}


# ---------------------------------------------------------------- roteiro


def resumo_avaliacao(a: dict | None) -> dict | None:
    if not isinstance(a, dict):
        return None
    criterios = [{k: c.get(k) for k in ("id", "nota", "peso", "bloco", "problema", "instrucao")}
                 for c in a.get("criterios", []) if isinstance(c, dict) and c.get("problema")]
    return {"aprovado": a.get("aprovado"), "nota": a.get("nota_percentual"), "bloqueantes": a.get("bloqueantes_reprovados") or [],
            "resumo": a.get("resumo") or a.get("veredito") or a.get("onde_cumpre"), "criterios": criterios,
            "pontos_de_abandono": a.get("pontos_de_abandono"),
            "obrigacoes": [{k: o.get(k) for k in ("id", "obrigacao", "cumprida", "onde")}
                           for o in a.get("obrigacoes") or [] if isinstance(o, dict)]}


def resumo_medicao(m: dict | None) -> dict | None:
    if not isinstance(m, dict):
        return None
    return {k: m.get(k) for k in ("palavras", "duracao_estimada", "duracao_alvo", "alertas", "erros", "erro") if m.get(k) is not None}


def nomes_do_roteiro(b: dict | None, docs: list[dict]) -> list[str]:
    nomes = [roteiro.NARRADOR] + [x["nome"] for x in (b or {}).get("personagens") or [] if isinstance(x, dict) and x.get("nome")]
    for doc in docs:
        for bloco in doc["blocos"]:
            for item in bloco["itens"]:
                if item["tipo"] == "fala" and item["quem"].lower() not in {n.lower() for n in nomes}:
                    nomes.append(item["quem"])
    return nomes


@roteador.get("/api/projetos/{slug}/roteiros")
def roteiros(slug: str):
    validar_slug(slug)
    pasta = sincronizar_projeto(slug)
    final = ler_texto(pasta / "06-roteiro-final.md")
    saida = []
    for versao, caminho in roteiro.versoes(pasta):
        doc = roteiro.ler(caminho)
        origem = re.search(r'(?m)^origem:\s*"?([^"\n]*)"?', doc["frontmatter"])
        saida.append({"versao": versao, "arquivo": f"projetos/{slug}/{Path(caminho).name}", "doc": doc,
                      "origem": origem.group(1) if origem else "roteirista",
                      "final": final is not None and final == ler_texto(Path(caminho)),
                      "avaliacao": resumo_avaliacao(ler_json(pasta / f"05-avaliacao-v{versao}.json")),
                      "medicao": resumo_medicao(ler_json(pasta / f"05-medicao-v{versao}.json"))})
    return {"versoes": saida, "tem_final": final is not None,
            "nomes": nomes_do_roteiro(ler_json(pasta / "00-brief.json"), [v["doc"] for v in saida])}


@roteador.post("/api/projetos/{slug}/roteiros")
def salvar_roteiro(slug: str, dados: dict = Body(...)):
    """A edição do usuário vira uma versão nova; a anterior fica como estava. A medição sai junto."""
    carregar_projeto(slug)
    exigir_sem_execucao(slug)
    pasta = sincronizar_projeto(slug)
    try:
        base = int(dados.get("base") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "versão de base inválida") from None
    exigir((pasta / f"05-roteiro-v{base}.md").exists(), f"não existe a versão {base}")
    doc = dados.get("doc") or {}
    exigir(isinstance(doc, dict) and isinstance(doc.get("blocos"), list) and doc["blocos"], "o roteiro precisa ter blocos")
    with trava(f"roteiro:{slug}"):  # dois salvamentos ao mesmo tempo não pegam o mesmo número de versão
        espaco.baixar(f"projetos/{slug}/05-roteiro-v")
        nova = max((v for v, _ in roteiro.versoes(pasta)), default=0) + 1
        try:
            texto = roteiro.montar(doc, nova, f"editada pelo usuário a partir da v{base}")
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise HTTPException(400, f"roteiro em formato inesperado: {e}") from None
        caminho = espaco.gravar_texto(f"projetos/{slug}/05-roteiro-v{nova}.md", texto)
    medicao = etapas.medir(caminho)
    enviar_projeto(slug)
    return {"versao": nova, "arquivo": f"projetos/{slug}/05-roteiro-v{nova}.md", "medicao": resumo_medicao(medicao)}


@roteador.post("/api/projetos/{slug}/roteiros/{versao}/final")
def definir_final(slug: str, versao: int):
    p = carregar_projeto(slug)
    exigir_sem_execucao(slug)
    pasta = sincronizar_projeto(slug)
    origem = pasta / f"05-roteiro-v{versao}.md"
    exigir(origem.exists(), f"não existe a versão {versao}")
    espaco.gravar_bytes(f"projetos/{slug}/06-roteiro-final.md", origem.read_bytes())
    etapas.registrar(slug, "roteiro")  # a escolha do usuário vale como roteiro feito com o ângulo atual
    enviar_projeto(slug)
    proxima = etapas.proxima(slug, "roteiro")
    if proxima is not None and proxima not in ids_da_trilha(p):
        proxima = None
    repositorios.atualizar_projeto(slug, etapa=proxima or "roteiro")
    return {"ok": True, "proxima": proxima}


# ---------------------------------------------------------------- direção visual


@roteador.get("/api/projetos/{slug}/visual")
def visual(slug: str):
    validar_slug(slug)
    pasta = sincronizar_projeto(slug)
    biblia = ler_json(pasta / "08-biblia-visual.json") or {}
    cenas = (ler_json(pasta / "08-cenas.json") or {}).get("cenas", [])
    return {"storyboard": ler_texto(pasta / "08-storyboard.md"), "estilo": biblia.get("estilo"),
            "personagens": biblia.get("personagens", []), "locais": biblia.get("locais", []), "cenas": len(cenas)}


# ---------------------------------------------------------------- produção


def imagem_da_cena(pasta: Path, id_: str, registro: dict) -> tuple[str | None, str | None]:
    """(arquivo relativo à pasta do projeto, situação): a imagem real que a montagem usaria (a mesma busca do
    montar_video.py), senão o quadro provisório."""
    arquivo = registro.get("arquivo")
    if isinstance(arquivo, str) and (pasta / arquivo).is_file() and Path(arquivo).parent.as_posix() == "09-imagens":
        return arquivo, None
    for ext in EXTENSOES_CENA:
        if (pasta / "09-imagens" / f"{id_}.{ext}").is_file():
            return f"09-imagens/{id_}.{ext}", None
    if (pasta / "09-imagens" / "provisorio" / f"{id_}.png").is_file():
        return f"09-imagens/provisorio/{id_}.png", "provisorio"
    return None, "faltando"


def cenas_da_producao(pasta: Path) -> list[dict]:
    """Cada cena com a imagem dela, de onde veio (internet, gerada) e a situação (ok, bloqueada pelo filtro, erro,
    provisória, sem imagem), para a grade da tela de produção."""
    cenas = (ler_json(pasta / "08-cenas.json") or {}).get("cenas") or []
    manifesto = ler_json(pasta / "09-imagens" / "manifesto.json") or {}
    saida = []
    for c in cenas:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        id_ = str(c["id"])
        registro = manifesto.get(id_) if isinstance(manifesto.get(id_), dict) else {}
        imagem, situacao = imagem_da_cena(pasta, id_, registro)
        status = registro.get("status")
        if status in (None, "ok") and situacao:
            status = situacao
        saida.append({"id": id_, "bloco": c.get("bloco") or id_.split("-")[0], "imagem": imagem,
                      "origem": registro.get("origem") or ("provisorio" if situacao == "provisorio" else c.get("fonte")),
                      "status": status or "ok", "descricao": c.get("descricao")})
    return saida


def revisao_visual(pasta: Path) -> list | dict | None:
    """A revisão visual que o fluxo da produção gravou em 09-imagens/revisao.json, como a lista de rodadas que a tela
    mostra: as rodadas das imagens (`imagens`) e, por último, a conferência do vídeo montado (`video`)."""
    doc = next((d for nome in REVISAO if (d := ler_json(pasta / nome)) is not None), None)
    if isinstance(doc, list) or doc is None:
        return doc
    if not isinstance(doc, dict) or not ({"imagens", "video"} & set(doc)):
        return doc  # uma rodada só, no formato da tela
    rodadas = [{**r, "rodada": r.get("rodada") or i} for i, r in enumerate(doc.get("imagens") or [], 1) if isinstance(r, dict)]
    if isinstance(doc.get("video"), dict):
        rodadas.append({**doc["video"], "rodada": "final (vídeo montado)"})
    return rodadas or None


def ultima_terminada(slug: str, nomes: tuple[str, ...]) -> dict | None:
    finais = [e for nome in nomes if (e := repositorios.ultima_execucao(slug, nome)) and e["status"] not in ("pendente", "rodando")]
    return max(finais, key=lambda e: e["id"], default=None)


def custo_do_projeto(slug: str) -> dict:
    """O que o projeto gastou, do banco: cada chamada paga (agentes, busca, imagens, narração) com o preço da tabela."""
    c = repositorios.custos_do_projeto(slug)
    tabela = custos.tabela()
    por_etapa = [{**x, "etapa": None if x["etapa"] == "avulso" else x["etapa"]} for x in c.get("por_etapa", [])]
    return {**c, "por_etapa": por_etapa, "tabela": {"conferido_em": tabela.get("conferido_em"), "moeda": tabela.get("moeda", "USD")}}


@roteador.get("/api/projetos/{slug}/producao")
def producao(slug: str):
    validar_slug(slug)
    pasta = sincronizar_projeto(slug)
    videos = [str(p.relative_to(pasta)) for p in sorted((pasta / "10-video").glob("*.mp4"))]
    contato = pasta / "09-imagens" / "folha-de-contato.jpg"
    thumb = pasta / "10-video" / "thumbnail.jpg"
    ultima = ultima_terminada(slug, ("producao", "refazer-cenas"))
    return {"videos": videos, "folha_de_contato": "09-imagens/folha-de-contato.jpg" if contato.exists() else None,
            # o endereço inteiro, com a versão: o navegador busca a thumbnail nova depois de uma montagem (um caminho
            # relativo com "?v=" teria o "?" codificado pela tela, que monta a URL parte a parte)
            "thumbnail": f"/api/projetos/{slug}/arquivos/10-video/thumbnail.jpg?v={thumb.stat().st_mtime_ns}"
            if thumb.exists() else None,
            "creditos": ler_texto(pasta / "09-imagens" / "creditos.md"),
            "narracao": (pasta / "09-narracao" / "narracao.wav").exists(),
            "publicacao": ler_json(pasta / "11-publicacao.json"),
            "cenas": cenas_da_producao(pasta), "revisao": revisao_visual(pasta),
            "avisos": (ultima or {}).get("avisos") or [], "custo": custo_do_projeto(slug)}
