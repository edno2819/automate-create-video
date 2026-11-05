"""Execuções: a web só confere e enfileira; quem roda é o trabalhador (nucleo.pipeline.execucao).

`POST /api/projetos/{slug}/executar` confere o que a etapa precisa (arquivos das etapas anteriores, chaves dos
provedores) e põe a execução na fila; o banco garante uma execução ativa por projeto (409 na segunda). Antes de
gastar, pede confirmação (409 com `precisa`): a direção visual e a produção com o roteiro final ainda marcado
com [VERIFICAR], a produção que vai gerar mais de 30 imagens pagas e a que vai animar imagens (visual.animacao no
pedido), com a estimativa do custo. A dublagem e a exportação (etapa Edição) são
conferidas em web/api/edicao.py.

O log de cada execução fica no banco, uma linha por evento, e a tela acompanha pelo cursor (o id da última linha
lida): `GET /api/execucoes/{id}?desde=<cursor>` devolve o que veio depois e o cursor novo em `tamanho`.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

from nucleo import chaves, rotas
from nucleo.banco import repositorios
from nucleo.pipeline import execucao
from web.api import edicao, formulario
from web.api.comum import exigir, exigir_sem_execucao, ler_json, ler_texto, rodar_script, sincronizar_projeto
from web.api.projetos import carregar_projeto, importado, para_tela, preparar_biblioteca

roteador = APIRouter()

ETAPAS = ("sugestoes", "pesquisa", "angulos", "ganchos", "roteiro", "avaliar", "visual", "producao", "publicacao",
          "importar", "refazer-cenas", "dublagem", "exportar")
LIMITE_IMAGENS = 30          # acima disso a produção pede confirmação
LIMITE_ORIENTACAO = 5000     # caracteres
LIMITE_CENAS = 500
VERIFICAR = re.compile(r"\[VERIFICAR\b:?\s*([^\]]*)\]", re.I)
A_GERAR = re.compile(r"a gerar:\s*(\d+)\s*ficha\(s\)[^+]*\+\s*(\d+)\s*cena\(s\)")
A_ANIMAR = re.compile(r"a animar:\s*(\d+)\s*cena\(s\)\s*·\s*(\d+)\s*s de vídeo\s*·\s*([^(\n]+?)\s*\(")


def texto_curto(dados: dict, chave: str, limite: int = LIMITE_ORIENTACAO) -> str:
    valor = dados.get(chave)
    if valor is None:
        return ""
    exigir(isinstance(valor, str), f"{chave} precisa ser texto")
    exigir(len(valor) <= limite, f"{chave} passa de {limite} caracteres")
    return valor.strip()


def precisa_confirmar(mensagem: str, precisa: str, **extras) -> HTTPException:
    """409 que a tela transforma numa pergunta; confirmada, ela reenvia com `precisa: true`."""
    return HTTPException(409, {"detail": mensagem, "precisa": precisa, **extras})


def conferir_verificar(pasta: Path, dados: dict) -> None:
    """O roteiro final com [VERIFICAR] (fatos que o roteirista pediu para conferir) vai para as imagens e a voz só
    com a confirmação do usuário."""
    if dados.get("confirmar_verificar"):
        return
    marcas = VERIFICAR.findall(ler_texto(pasta / "06-roteiro-final.md") or "")
    if not marcas:
        return
    exemplos = "; ".join(m.strip()[:120] or "(sem texto)" for m in marcas[:3]) + ("; ..." if len(marcas) > 3 else "")
    raise precisa_confirmar(
        f"O roteiro final ainda tem {len(marcas)} marcação(ões) [VERIFICAR], fatos que o roteirista pediu para conferir: "
        f"{exemplos}. Confira (e edite o roteiro, se preciso) antes de gastar com imagens e narração.",
        "confirmar_verificar", marcas=len(marcas))


def imagens_a_gerar(slug: str, brief: dict) -> tuple[int, int, str, str] | None:
    """(fichas, cenas, provedor, modelo) que a produção geraria agora, pelo gerar_imagens.py --simular com o
    provedor e o modelo que o fluxo vai usar (o hash do que já existe depende do modelo). None se não deu para saber."""
    qualidade = ((brief or {}).get("visual") or {}).get("qualidade")
    provedor, modelo = rotas.imagem(qualidade)
    codigo, saida = rodar_script("ferramentas/producao/gerar_imagens.py", f"projetos/{slug}", "--simular",
                                 "--provedor", provedor, "--modelo", modelo)
    if m := A_GERAR.search(saida):
        return int(m.group(1)), int(m.group(2)), provedor, modelo
    if codigo == 1 and "nenhuma cena" in saida:
        return 0, 0, provedor, modelo
    return None


def conferir_imagens(slug: str, pasta: Path, dados: dict) -> None:
    if dados.get("confirmar_imagens"):
        return
    conta = imagens_a_gerar(slug, ler_json(pasta / "00-brief.json") or {})
    if conta is None:
        return  # a simulação falhou: o fluxo vai dizer o motivo no log
    fichas, cenas, provedor, modelo = conta
    if fichas + cenas > LIMITE_IMAGENS:
        raise precisa_confirmar(
            f"A produção vai gerar {fichas + cenas} imagens pagas ({fichas} ficha(s) de referência e {cenas} cena(s)) "
            f"com {provedor} ({modelo}).", "confirmar_imagens", imagens=fichas + cenas)


def conferir_animacao(slug: str, pasta: Path, dados: dict) -> None:
    """Com a animação ligada no pedido: a chave do provedor de vídeo tem de existir (400) e, antes de gastar, o
    usuário confirma quantas cenas e quantos segundos vão ser animados e a estimativa do custo (409)."""
    modo = (((ler_json(pasta / "00-brief.json") or {}).get("visual") or {}).get("animacao")) or "nenhuma"
    if modo == "nenhuma":
        return
    cfg = rotas.animacao()
    exigir(chaves.ler(cfg["provedor"]), f"o pedido anima as imagens (visual.animacao: {modo}) com "
                                        f"{chaves.PROVEDORES[cfg['provedor']]['nome']}, sem chave cadastrada: cadastre em "
                                        "Configurações > Chaves de API, ou desligue a animação no pedido")
    if dados.get("confirmar_animacao"):
        return
    _, saida = rodar_script("ferramentas/producao/animar_imagens.py", f"projetos/{slug}", "--simular", "--provedor",
                            cfg["provedor"], "--modelo", cfg["modelo"], "--resolucao", cfg["resolucao"])
    if (m := A_ANIMAR.search(saida)) and int(m.group(1)):
        raise precisa_confirmar(
            f"A produção vai animar {m.group(1)} cena(s), cerca de {m.group(2)} s de vídeo, com {cfg['modelo']} "
            f"({cfg['resolucao']}): {m.group(3)}.", "confirmar_animacao", cenas=int(m.group(1)), segundos=int(m.group(2)))


# ---------------------------------------------------------------- o que cada etapa precisa


def parametros_da_etapa(etapa: str, slug: str, p: dict, pasta: Path, dados: dict) -> dict:
    """Confere as condições da etapa e devolve os parâmetros que vão para o fluxo. HTTPException se faltar algo."""
    if etapa == "sugestoes":
        form = p.get("formulario") or {}
        v = form.get("valores") or {}
        exigir(not formulario.vazio(v.get("titulo")) and not formulario.vazio(v.get("descricao")),
               "título e descrição são obrigatórios")
        brutos = formulario.brutos_do_formulario(form)
        preparar_biblioteca(brutos)
        _, erros = formulario.gravar_pedido_do_usuario(slug, brutos)
        exigir(not erros, "\n".join(erros))
        return {"vazios": formulario.vazios_para_sugerir(slug, brutos), "modo_voz": form.get("modo_voz") or None}
    if etapa == "pesquisa":
        exigir((pasta / "00-brief.json").exists(), "aprove o pedido antes da pesquisa")
        return {}
    if etapa == "angulos":
        exigir((pasta / "02-dossie.md").exists(), "a pesquisa ainda não gerou o dossiê (02-dossie.md)")
        return {"orientacao": texto_curto(dados, "orientacao")}
    if etapa == "ganchos":
        exigir((pasta / "04-angulo-escolhido.json").exists(), "escolha o ângulo antes dos ganchos")
        return {"orientacao": texto_curto(dados, "orientacao")}
    if etapa == "roteiro":
        exigir((pasta / "04-angulo-escolhido.json").exists(), "escolha o ângulo antes do roteiro")
        try:
            rodadas = max(1, min(3, int(dados.get("rodadas") or 3)))
        except (TypeError, ValueError):
            raise HTTPException(400, "rodadas precisa ser um número de 1 a 3") from None
        return {"orientacao": texto_curto(dados, "orientacao"), "rodadas": rodadas}
    if etapa == "avaliar":
        try:
            versao = int(dados.get("versao") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "versão inválida") from None
        exigir(versao > 0 and (pasta / f"05-roteiro-v{versao}.md").exists(), f"não existe a versão {versao}")
        return {"versao": versao}
    if etapa == "importar":
        exigir((pasta / "00-roteiro-original.md").exists(), "este projeto não tem roteiro importado (00-roteiro-original.md)")
        exigir((pasta / "00-brief.json").exists(), "aprove o pedido antes de formatar o roteiro")
        return {}
    exigir((pasta / "06-roteiro-final.md").exists(),
           "defina o roteiro final antes" if not importado(p) else "formate o roteiro antes")
    if etapa == "publicacao":
        return {}
    if etapa in ("dublagem", "exportar"):
        return edicao.parametros(etapa, slug, pasta, dados)
    if etapa == "visual":
        conferir_verificar(pasta, dados)
        return {"confirmar_verificar": True} if dados.get("confirmar_verificar") else {}
    exigir((pasta / "08-cenas.json").exists(), "a direção visual ainda não gerou 08-cenas.json")
    if etapa == "producao":
        teste = bool(dados.get("teste"))
        conferir_verificar(pasta, dados)
        if not teste:
            conferir_imagens(slug, pasta, dados)
            conferir_animacao(slug, pasta, dados)
        return {"teste": teste, **{k: True for k in ("confirmar_verificar", "confirmar_imagens", "confirmar_animacao")
                                   if dados.get(k)}}
    # refazer-cenas
    cenas = dados.get("cenas")
    exigir(isinstance(cenas, list) and cenas and all(isinstance(c, str) and c for c in cenas),
           "marque as cenas que quer refazer")
    exigir(len(cenas) <= LIMITE_CENAS, "cenas demais de uma vez")
    existentes = {str(c.get("id")) for c in (ler_json(pasta / "08-cenas.json") or {}).get("cenas") or [] if isinstance(c, dict)}
    desconhecidas = [c for c in cenas if c not in existentes and c != "thumbnail"]
    exigir(not desconhecidas, f"cena(s) desconhecida(s): {', '.join(desconhecidas[:10])}")
    return {"cenas": list(dict.fromkeys(cenas)), "ajuste": texto_curto(dados, "ajuste")}


@roteador.post("/api/projetos/{slug}/executar")
def executar(slug: str, dados: dict = Body(...)):
    """Enfileira uma etapa do projeto: `{etapa, ...parametros}` → `{id}` da execução."""
    p = carregar_projeto(slug)
    etapa = dados.get("etapa")
    exigir(etapa in ETAPAS, f"etapa desconhecida: {etapa}")
    exigir_sem_execucao(slug)
    pasta = sincronizar_projeto(slug)
    problemas = rotas.problemas(etapa, teste=bool(dados.get("teste")) if etapa == "producao" else False)
    exigir(not problemas, "\n".join(problemas))
    parametros = parametros_da_etapa(etapa, slug, p, pasta, dados)
    try:
        id_ = execucao.enfileirar(slug, etapa, parametros)
    except repositorios.ErroOcupado as e:
        raise HTTPException(409, f"já há uma execução ativa neste projeto: {e}") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"id": id_}


# ---------------------------------------------------------------- acompanhar e cancelar


def execucao_ou_404(id_: int) -> dict:
    e = repositorios.execucao(id_)
    if e is None:
        raise HTTPException(404, "execução não encontrada")
    return e


@roteador.get("/api/execucoes/{id_}")
def ver_execucao(id_: int, desde: int = 0):
    """O estado da execução e o log depois do cursor (`desde`, o id da última linha já lida; 0 no começo). O cursor
    novo vem em `tamanho` (o nome antigo, de quando era o tamanho do texto)."""
    e = execucao_ou_404(id_)
    texto, cursor = repositorios.log_desde(id_, max(0, desde))
    return {**para_tela(e), "log": texto, "tamanho": cursor}


@roteador.post("/api/execucoes/{id_}/cancelar")
def cancelar(id_: int):
    """Pendente: cancela na hora. Rodando: o trabalhador para no próximo batimento (o que já foi gravado fica)."""
    e = execucao_ou_404(id_)
    if e["status"] not in ("pendente", "rodando"):
        return {"cancelada": False, "status": e["status"]}
    resposta = execucao.cancelar(id_)
    return {"cancelada": resposta is not False, "status": (repositorios.execucao(id_) or e)["status"]}

