#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["fastapi>=0.110", "uvicorn>=0.29", "python-multipart>=0.0.9", "ruamel.yaml>=0.18"]
# ///
"""Interface web do pipeline de vídeos: do pedido ao roteiro editável, e daí à produção.

O usuário preenche o pedido num formulário (só título e descrição são obrigatórios), o sistema sugere o
resto, ele aprova, e cada etapa seguinte roda o Claude Code em modo headless, que segue a skill novo-video
como numa conversa. Tudo continua sendo gravado em projetos/<slug>/; o banco SQLite (dados/web.sqlite3)
guarda só o estado da interface e o log das execuções.

Uso (o uv instala as dependências na primeira vez; o 2>&1 | cat é exigido pelo uv do snap):
    uv run --script web/servidor.py 2>&1 | cat
    uv run --script web/servidor.py --porta 8765 --host 127.0.0.1 2>&1 | cat

Variáveis: WEB_PERMISSAO (padrão acceptEdits; bypassPermissions libera todas as ferramentas do Claude Code),
WEB_MODELO (modelo da sessão principal) e WEB_CLAUDE (caminho do executável claude).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(RAIZ / "ferramentas"))

import banco  # noqa: E402
import custos  # noqa: E402
import chaves  # noqa: E402
import claude_code  # noqa: E402
import conexoes  # noqa: E402
import contextos  # noqa: E402
import orquestrador  # noqa: E402
import etapas  # noqa: E402
import pedido  # noqa: E402
import roteiro  # noqa: E402

PERSONAGENS = RAIZ / "biblioteca" / "personagens"
MUSICAS = RAIZ / "biblioteca" / "musicas"
VOZES = RAIZ / "biblioteca" / "vozes"  # amostras de ~5 s de cada voz (narrar.py --amostras)
EXTENSOES_IMAGEM = contextos.EXTENSOES_IMAGEM
EXTENSOES_AUDIO = contextos.EXTENSOES_AUDIO
FORA_DAS_SUGESTOES = {"titulo", "slug", "musica.nome", "musica.arquivo", "musica.ativa", "musica.inicio", "contexto",
                      "imagens_referencia", "trends.links", "trends.ativa"}

app = FastAPI(title="Vídeos: pedido ao roteiro")


# ---------------------------------------------------------------- opções do formulário


def texto_de(v) -> str:
    if isinstance(v, bool):
        return "sim" if v else "não"
    if isinstance(v, list):
        return ", ".join(map(str, v)) or "(nenhum)"
    return "" if v is None else f"{v:g}" if isinstance(v, float) else str(v)


def padrao_texto(c: pedido.Campo) -> str:
    if isinstance(c.padrao, pedido.PorFormato):
        return f"{texto_de(c.padrao.longo)} no longo, {texto_de(c.padrao.short)} no short"
    if c.padrao is pedido.voz_do_canal:
        return texto_de(pedido.voz_do_canal({}))
    if c.padrao is pedido.musica_preenchida:
        return "sim se houver música"
    if c.padrao in (None, [], ""):
        return c.vazio
    return texto_de(c.padrao)


def campos() -> list[dict]:
    saida, secao = [], ""
    for c in pedido.CAMPOS:
        if c.secao:
            secao = c.secao[0]
        saida.append({"chave": c.chave, "tipo": c.tipo, "ajuda": c.ajuda, "quem": c.quem, "opcoes": list(c.opcoes),
                      "faixa": list(c.faixa) if c.faixa else None, "padrao": padrao_texto(c), "secao": secao})
    return saida


def personagens_da_biblioteca() -> list[dict]:
    saida = []
    for arquivo in sorted(PERSONAGENS.glob("*/personagem.json")):
        d = etapas.ler_json(arquivo) or {}
        voz = d.get("voz") or {}
        imagem = next((p for p in sorted(arquivo.parent.glob("imagem.*")) if p.suffix.lower() in EXTENSOES_IMAGEM), None)
        saida.append({"slug": d.get("slug") or arquivo.parent.name, "nome": d.get("nome") or arquivo.parent.name,
                      "resumo": d.get("resumo", ""), "voz": voz.get("voz"), "estilo": voz.get("estilo"), "cor": voz.get("cor"),
                      "descricao": d.get("descricao_en"), "estilo_visual": (d.get("estilo") or {}).get("nome"),
                      "imagem": f"/api/biblioteca/personagens/{arquivo.parent.name}/imagem" if imagem else None})
    return saida


def vozes_por_fornecedor() -> dict[str, list[dict]]:
    """As vozes de cada fornecedor de narração, com a amostra para ouvir: as gravadas em biblioteca/vozes/
    (narrar.py --amostras) ou, na ElevenLabs, a prévia que a própria conta oferece."""
    narrar = pedido.modulo_narrar()
    saida = {}
    for nome, classe in narrar.FORNECEDORES.items():
        previas = {v["nome"]: v.get("amostra") for v in narrar.vozes_elevenlabs_em_cache()} if nome == "elevenlabs" else {}
        saida[nome] = [{"nome": n, "descricao": d, "genero": classe.GENEROS.get(n, "?"),
                        "amostra": previas.get(n) or (f"/api/biblioteca/vozes/{n}" if (VOZES / f"{n}.mp3").exists() else None)}
                       for n, d in classe.VOZES.items()]
    return saida


@app.get("/api/opcoes")
def opcoes():
    por_fornecedor = vozes_por_fornecedor()
    musicas = [p.name for p in sorted(MUSICAS.iterdir()) if p.suffix.lower() in EXTENSOES_AUDIO] if MUSICAS.is_dir() else []
    return {"campos": campos(), "vozes": por_fornecedor["gemini"], "vozes_por_fornecedor": por_fornecedor,
            "voz_do_canal": pedido.voz_do_canal({}), "musicas": musicas,
            "contextos": contextos.listar(), "personagens": personagens_da_biblioteca(), "cores": pedido.CORES,
            "modos_voz": etapas.MODOS_VOZ, "etapas": [e.__dict__ for e in etapas.ETAPAS]}


@app.get("/api/biblioteca/personagens/{slug}/imagem")
def imagem_personagem(slug: str):
    pasta = (PERSONAGENS / slug).resolve()
    if pasta.parent != PERSONAGENS.resolve():
        raise HTTPException(404)
    imagem = next((p for p in sorted(pasta.glob("imagem.*")) if p.suffix.lower() in EXTENSOES_IMAGEM), None)
    if not imagem:
        raise HTTPException(404)
    return FileResponse(imagem)


@app.get("/api/biblioteca/vozes/{nome}")
def amostra_voz(nome: str):
    arquivo = VOZES / f"{nome}.mp3"
    if not re.fullmatch(r"[A-Za-z]+", nome) or not arquivo.exists():
        raise HTTPException(404)
    return FileResponse(arquivo, media_type="audio/mpeg")


# ---------------------------------------------------------------- conexões com os provedores


@app.get("/api/conexoes")
def ver_conexoes():
    dados = conexoes.ler()
    nomes = {e.id: e.nome for e in etapas.ETAPAS}
    return {"provedores": conexoes.provedores(), "modos": conexoes.MODOS, "sugeridos": conexoes.SUGERIDOS,
            "modelos_claude_code": conexoes.MODELOS_CLAUDE_CODE,
            "etapas": [{"id": e, "nome": nomes.get(e, e), "agentes": agentes, **conexoes.rota(e)}
                       for e, agentes in conexoes.ETAPAS_LLM.items()],
            "precos": sorted((custos.tabela().get("modelos") or {}).keys()), "testes": dados["testes"]}


@app.put("/api/conexoes/chaves/{provedor}")
def gravar_chave(provedor: str, dados: dict = Body(...)):
    exigir(provedor in chaves.PROVEDORES, "provedor desconhecido")
    exigir(chaves.origem(provedor) != "ambiente",
           f"a chave vem da variável de ambiente {chaves.PROVEDORES[provedor]['variavel']}: mude-a onde ela é exportada")
    valor = (dados.get("valor") or "").strip()
    exigir(bool(valor), "cole a chave")
    try:
        chaves.gravar(provedor, valor)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"teste": conexoes.testar(provedor), "mascara": chaves.mascarar(valor)}


@app.delete("/api/conexoes/chaves/{provedor}")
def apagar_chave(provedor: str):
    exigir(provedor in chaves.PROVEDORES, "provedor desconhecido")
    exigir(chaves.origem(provedor) != "ambiente", "a chave vem de uma variável de ambiente: apague-a onde ela é exportada")
    chaves.gravar(provedor, None)
    return {"ok": True}


@app.post("/api/conexoes/{provedor}/testar")
def testar_conexao(provedor: str):
    exigir(provedor in chaves.PROVEDORES, "provedor desconhecido")
    return conexoes.testar(provedor)


@app.put("/api/conexoes/etapas")
def definir_etapas(dados: dict = Body(...)):
    try:
        return conexoes.definir_rotas(dados)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- contextos salvos


def contexto_ou_erro(acao, *args):
    """Traduz os erros de contextos.py para HTTP: id ou anexo desconhecido é 404, operação recusada é 400."""
    try:
        return acao(*args)
    except KeyError:
        raise HTTPException(404, "contexto ou anexo não encontrado")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/contextos")
def listar_contextos():
    return contextos.listar()


@app.post("/api/contextos")
def criar_contexto(dados: dict = Body(...)):
    nome = (dados.get("nome") or "").strip()
    exigir(bool(nome), "o nome é obrigatório")
    c = contextos.criar(nome)
    if texto := (dados.get("texto") or "").strip():
        c = contextos.salvar(c["id"], nome, texto, {})
    return c


@app.get("/api/contextos/{id_}")
def ver_contexto(id_: str):
    c = contexto_ou_erro(contextos.ler, id_)
    if c is None:
        raise HTTPException(404, "contexto não encontrado")
    return c


@app.put("/api/contextos/{id_}")
def salvar_contexto(id_: str, dados: dict = Body(...)):
    return contexto_ou_erro(contextos.salvar, id_, dados.get("nome") or "", dados.get("texto") or "",
                            dados.get("descricoes") or {})


@app.delete("/api/contextos/{id_}")
def apagar_contexto(id_: str):
    contexto_ou_erro(contextos.apagar, id_)
    return {"ok": True}


@app.post("/api/contextos/{id_}/anexos")
def anexar_ao_contexto(id_: str, arquivo: UploadFile = File(...), descricao: str = Form("")):
    return contexto_ou_erro(contextos.anexar, id_, arquivo.filename or "arquivo", arquivo.file, descricao)


@app.delete("/api/contextos/{id_}/anexos/{nome}")
def remover_anexo_do_contexto(id_: str, nome: str):
    return contexto_ou_erro(contextos.remover_anexo, id_, nome)


@app.get("/api/contextos/{id_}/arquivos/{nome}")
def anexo_do_contexto(id_: str, nome: str):
    caminho = contexto_ou_erro(contextos.arquivo, id_, nome)
    if caminho is None or not caminho.is_file():
        raise HTTPException(404)
    return FileResponse(caminho)


# ---------------------------------------------------------------- projetos


def importar(slug: str) -> dict | None:
    """Projeto que existe em projetos/ mas não no banco (feito pela conversa): entra com o pedido original."""
    pasta = etapas.pasta(slug)
    if not pasta.is_dir():
        return None
    formulario = {"valores": {}, "personagens": []}
    for nome in ("00-pedido-original.yaml", "00-pedido.yaml"):
        if (pasta / nome).exists():
            p = pedido.carregar(pasta / nome)
            formulario["valores"] = {k: v for k, v in p.valores.items() if v not in (None, [], "") and k != "personagens"}
            formulario["personagens"] = p.valores.get("personagens") or []
            break
    b = etapas.brief(slug) or {}
    titulo = b.get("titulo") or b.get("tema") or formulario["valores"].get("titulo") or slug
    return banco.criar_projeto(slug, titulo, formulario, etapas.primeira_pendente(slug))


def carregar_projeto(slug: str) -> dict:
    p = banco.projeto(slug) or importar(slug)
    if p is None:
        raise HTTPException(404, f"projeto {slug} não existe")
    return p


def endereco_capa(slug: str) -> str | None:
    origem = etapas.capa(slug)
    # a versão no endereço troca quando a imagem muda, e o navegador busca a capa nova
    return f"/api/projetos/{slug}/capa?v={origem.stat().st_mtime_ns}" if origem else None


@app.get("/api/projetos")
def listar():
    no_banco = {p["slug"]: p for p in banco.projetos()}
    saida = [{"slug": p["slug"], "titulo": p["titulo"], "etapa": p["etapa"], "atualizado_em": p["atualizado_em"], "no_banco": True,
              "capa": endereco_capa(p["slug"])} for p in no_banco.values()]
    for pasta in sorted(etapas.PROJETOS.iterdir()) if etapas.PROJETOS.is_dir() else []:
        if pasta.is_dir() and pasta.name not in no_banco:
            b = etapas.brief(pasta.name) or {}
            saida.append({"slug": pasta.name, "titulo": b.get("titulo") or b.get("tema") or pasta.name,
                          "etapa": None, "atualizado_em": None, "no_banco": False, "capa": endereco_capa(pasta.name)})
    return saida


@app.get("/api/projetos/{slug}/capa")
def capa_do_projeto(slug: str):
    """A capa reduzida a 640 px, guardada em .capa.jpg e refeita quando a imagem de origem muda: a lista não baixa a
    thumbnail inteira (até 2 MB) de cada projeto."""
    origem = etapas.capa(slug)
    if origem is None:
        raise HTTPException(404)
    destino = etapas.pasta(slug) / ".capa.jpg"
    if not destino.exists() or destino.stat().st_mtime < origem.stat().st_mtime:
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(origem), "-vf",
                            "scale=640:640:force_original_aspect_ratio=decrease", "-frames:v", "1", "-q:v", "4", str(destino)],
                           capture_output=True)
        if r.returncode:
            return FileResponse(origem)
    return FileResponse(destino, headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/projetos")
def criar(dados: dict = Body(...)):
    titulo = (dados.get("titulo") or "").strip()
    if not titulo:
        raise HTTPException(400, "o título é obrigatório")
    base = etapas.slug_de(dados.get("slug") or titulo)
    slug, n = base, 2
    while banco.projeto(slug) or etapas.pasta(slug).exists() or (etapas.PEDIDOS / f"{slug}.yaml").exists():
        slug, n = f"{base}-{n}", n + 1
    formulario = {"valores": {"titulo": titulo, "slug": slug, "trends.ativa": True}, "personagens": [], "modo_voz": ""}
    return banco.criar_projeto(slug, titulo, formulario)


@app.get("/api/projetos/{slug}")
def estado(slug: str):
    p = carregar_projeto(slug)
    b = etapas.brief(slug)
    defasadas = etapas.defasagens(slug)
    lista = []
    for e in etapas.ETAPAS:
        lista.append({**e.__dict__, "aplicavel": etapas.aplicavel(e.id, b), "pronta": etapas.pronta(slug, e.id),
                      "desatualizada": defasadas.get(e.id), "execucao": banco.ultima_execucao(slug, e.id)})
    ids = rodando(slug)
    em_andamento = banco.execucao(ids[0]) if ids else None
    if em_andamento:
        em_andamento.pop("log")
    return {"projeto": p, "etapas": lista, "brief": b, "rodando": em_andamento}


@app.post("/api/projetos/{slug}/fork")
def criar_fork(slug: str, dados: dict = Body(...)):
    """Um projeto novo com tudo do original até a etapa escolhida, inclusive, para refazer dali em diante sem mexer
    no original. O custo do fork começa do zero: só conta o que ele mesmo rodar."""
    p = carregar_projeto(slug)
    ate = dados.get("ate")
    exigir(ate in etapas.POR_ID, "etapa desconhecida")
    exigir(not rodando(slug), "espere a execução em andamento terminar antes de criar o fork")
    novo = etapas.slug_livre(slug, {x["slug"] for x in banco.projetos()})
    copiadas = etapas.fork(slug, novo, ate)
    formulario = json.loads(json.dumps(p["formulario"]).replace(slug, novo))
    formulario.setdefault("valores", {})["slug"] = novo
    titulo = (dados.get("titulo") or "").strip() or f"{p['titulo']} (fork)"
    banco.criar_projeto(novo, titulo, formulario, ate)
    if p.get("sugestoes"):
        banco.atualizar_projeto(novo, sugestoes=p["sugestoes"])
    return {"slug": novo, "etapa": ate, "copiadas": copiadas}


@app.put("/api/projetos/{slug}/formulario")
def salvar_formulario(slug: str, formulario: dict = Body(...)):
    carregar_projeto(slug)
    formulario.setdefault("valores", {})["slug"] = slug
    titulo = (formulario["valores"].get("titulo") or "").strip()
    banco.atualizar_projeto(slug, formulario=formulario, **({"titulo": titulo} if titulo else {}))
    return {"ok": True}


@app.put("/api/projetos/{slug}/etapa")
def mudar_etapa(slug: str, dados: dict = Body(...)):
    carregar_projeto(slug)
    if dados.get("etapa") not in etapas.POR_ID:
        raise HTTPException(400, "etapa desconhecida")
    banco.atualizar_projeto(slug, etapa=dados["etapa"])
    return {"ok": True}


@app.post("/api/projetos/{slug}/anexos")
async def anexar(slug: str, tipo: str = Form(...), arquivo: UploadFile = File(...)):
    carregar_projeto(slug)
    nome = Path(arquivo.filename or "arquivo")
    ext = nome.suffix.lower()
    if tipo == "imagem":
        if ext not in EXTENSOES_IMAGEM:
            raise HTTPException(400, f"imagem precisa ser {', '.join(sorted(EXTENSOES_IMAGEM))}")
        destino_pasta = etapas.pasta(slug) / "00-anexos"
    elif tipo == "musica":
        if ext not in EXTENSOES_AUDIO:
            raise HTTPException(400, f"música precisa ser {', '.join(sorted(EXTENSOES_AUDIO))}")
        destino_pasta = MUSICAS
    else:
        raise HTTPException(400, "tipo é imagem ou musica")
    destino_pasta.mkdir(parents=True, exist_ok=True)
    destino = destino_pasta / f"{etapas.slug_de(nome.stem, 10)}{ext}"
    n = 2
    while destino.exists():
        destino, n = destino_pasta / f"{etapas.slug_de(nome.stem, 10)}-{n}{ext}", n + 1
    with destino.open("wb") as f:
        shutil.copyfileobj(arquivo.file, f)
    return {"caminho": str(destino.relative_to(RAIZ)), "nome": destino.name}


@app.get("/api/projetos/{slug}/arquivos/{caminho:path}")
def arquivo_do_projeto(slug: str, caminho: str):
    base = etapas.pasta(slug).resolve()
    alvo = (base / caminho).resolve()
    if not alvo.is_relative_to(base) or not alvo.is_file():
        raise HTTPException(404)
    return FileResponse(alvo)


# ---------------------------------------------------------------- pedido: formulário -> YAML -> completar -> brief


def vazio(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or v == []


def aplicar_modo_voz(valores: dict, modo: str | None) -> dict:
    """Traduz o modo de voz da interface para os campos do pedido."""
    saida = {}
    if modo == "narrador":
        saida["personagens"] = []
    elif modo == "sem-voz":
        saida["narracao.ativa"] = False
    elif modo == "so-personagens":
        restricoes = list(valores.get("restricoes") or [])
        if etapas.RESTRICAO_SO_PERSONAGENS not in restricoes:
            restricoes.append(etapas.RESTRICAO_SO_PERSONAGENS)
        saida["restricoes"] = restricoes
    return saida


def personagens_limpos(lista) -> list[dict]:
    saida = []
    for x in lista or []:
        item = {k: v for k, v in (x or {}).items() if k in pedido.SUBCAMPOS_PERSONAGEM and not vazio(v)}
        if item.get("nome"):
            saida.append(item)
    return saida


def gravar_pedido_do_usuario(slug: str, brutos: dict) -> tuple[Path, list[str]]:
    """Grava pedidos/web/<slug>.yaml só com o que o usuário escreveu, no mesmo formato comentado do modelo."""
    valores, erros = {c.chave: None for c in pedido.CAMPOS}, []
    for c in pedido.CAMPOS:
        v = brutos.get(c.chave)
        if c.tipo == "personagens":
            if v is None:
                continue
            v = personagens_limpos(v)
        elif vazio(v):
            continue
        valores[c.chave], erro = pedido.normalizar(c, v)
        if erro:
            erros.append(f"{c.chave}: {erro}")
    cabecalho = [f"# Pedido de vídeo: {valores.get('titulo') or slug}", "#",
                 f"# Gravado pela interface web (web/) em {date.today().isoformat()}, só com o que o usuário preencheu.",
                 "# O resto vem das sugestões que ele aprovou na tela, em projetos/" + slug + "/00-pedido.yaml."]
    caminho = etapas.PEDIDOS / f"{slug}.yaml"
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(pedido.renderizar(valores, {}, cabecalho), encoding="utf-8")
    return caminho, erros


def brutos_do_formulario(formulario: dict) -> dict:
    brutos = {k: v for k, v in (formulario.get("valores") or {}).items() if not vazio(v)}
    if formulario.get("personagens"):
        brutos["personagens"] = formulario["personagens"]
    brutos.update(aplicar_modo_voz(brutos, formulario.get("modo_voz")))
    return brutos


def para_definir(chave: str, v) -> str:
    if isinstance(v, (list, dict)):
        return f"{chave}={json.dumps(v, ensure_ascii=False)}"
    if isinstance(v, bool):
        return f"{chave}={'sim' if v else 'não'}"
    return f"{chave}={v}"


@app.post("/api/projetos/{slug}/confirmar")
def confirmar(slug: str, dados: dict = Body(...)):
    """Aprovação do pedido: o que o usuário escreveu ou mudou vai para pedidos/web/<slug>.yaml; as sugestões que
    ele aceitou como vieram entram no completar como decisões do sistema (--definir). Depois, o brief."""
    carregar_projeto(slug)
    if rodando(slug):
        raise HTTPException(409, "há uma execução rodando neste projeto")
    valores, origens = dados.get("valores") or {}, dados.get("origens") or {}
    do_usuario = {k: v for k, v in valores.items() if origens.get(k, "usuario") == "usuario" and not vazio(v)}
    sugeridos = {k: v for k, v in valores.items() if origens.get(k) == "sugestao" and not vazio(v)}
    do_usuario["slug"] = slug
    destino = do_usuario if dados.get("origem_personagens", "usuario") == "usuario" else sugeridos
    destino["personagens"] = personagens_limpos(dados.get("personagens"))
    origem_modo = dados.get("origem_modo", "usuario")
    for chave, v in aplicar_modo_voz({**sugeridos, **do_usuario}, dados.get("modo_voz")).items():
        # o que o modo de voz muda num campo do usuário continua dele; nos outros, segue a origem do modo
        if chave in do_usuario or origem_modo == "usuario":
            sugeridos.pop(chave, None)
            do_usuario[chave] = v
        else:
            do_usuario.pop(chave, None)
            sugeridos[chave] = v
    if vazio(do_usuario.get("tema")) and vazio(sugeridos.get("tema")):
        sugeridos["tema"] = do_usuario.get("titulo") or slug

    caminho, erros = gravar_pedido_do_usuario(slug, do_usuario)
    if erros:
        raise HTTPException(400, "\n".join(erros))
    args = ["completar", str(caminho)]
    for chave, v in sugeridos.items():
        if chave in pedido.POR_CHAVE:
            args += ["--definir", para_definir(chave, v)]
    pasta = etapas.pasta(slug)
    if (pasta / "00-pedido.yaml").exists() or (pasta / "00-brief.json").exists():
        args.append("--sobrescrever")
    codigo, saida = etapas.rodar_pedido(*args)
    if codigo != 0:
        raise HTTPException(400, saida)
    codigo_b, saida_b = etapas.rodar_pedido("brief", str(pasta))
    if codigo_b != 0:
        raise HTTPException(400, saida + "\n\n" + saida_b)
    banco.atualizar_projeto(slug, etapa="pesquisa", titulo=do_usuario.get("titulo") or slug)
    return {"completar": saida, "brief": saida_b}


# ---------------------------------------------------------------- execuções


def exigir(condicao: bool, mensagem: str) -> None:
    if not condicao:
        raise HTTPException(400, mensagem)


@app.post("/api/projetos/{slug}/executar")
def executar(slug: str, dados: dict = Body(...)):
    """Cada etapa tem o prompt para o Claude Code e o fluxo equivalente do motor de agentes; Configurações > Modelos das etapas
    diz qual dos dois roda a etapa, e com que modelo."""
    p = carregar_projeto(slug)
    if rodando(slug):
        raise HTTPException(409, "já há uma execução rodando neste projeto")
    etapa = dados.get("etapa")
    orientacao = (dados.get("orientacao") or "").strip()
    pasta = etapas.pasta(slug)
    ao_terminar = None
    esquema = None
    escolha = conexoes.rota(etapa)
    exigir(not (problema := conexoes.problema_da_rota(escolha)), problema or "")
    rota = orquestrador.Rota(escolha["modo"], escolha["modelo"])

    if etapa == "sugestoes":
        form = p["formulario"]
        v = form.get("valores") or {}
        exigir(not vazio(v.get("titulo")) and not vazio(v.get("descricao")), "título e descrição são obrigatórios")
        brutos = brutos_do_formulario(form)
        _, erros = gravar_pedido_do_usuario(slug, brutos)
        exigir(not erros, "\n".join(erros))
        vazios = [c.chave for c in pedido.CAMPOS if c.chave not in FORA_DAS_SUGESTOES and vazio(brutos.get(c.chave))
                  and not (c.chave == "personagens" and "personagens" in brutos)]
        prompt = etapas.prompt_sugestoes(slug, vazios, form.get("modo_voz") or None)
        fluxo = orquestrador.sugestoes(slug, rota, vazios, form.get("modo_voz") or None)
        esquema = etapas.ESQUEMA_SUGESTOES

        def ao_terminar(execucao, resultado):
            sugestoes = resultado.get("structured_output")
            if not sugestoes:
                texto = resultado.get("result") or ""
                m = re.search(r"\{.*\}", texto, re.S)
                sugestoes = json.loads(m.group(0)) if m else None
            if not isinstance(sugestoes, dict):
                raise ValueError("o modelo não devolveu as sugestões em JSON")
            banco.atualizar_projeto(slug, sugestoes=sugestoes)
    elif etapa == "pesquisa":
        exigir(etapas.pronta(slug, "sugestoes"), "aprove o pedido antes da pesquisa")
        prompt = etapas.prompt_pesquisa(slug)
        fluxo = orquestrador.pesquisa(slug, rota)

        def ao_terminar(execucao, resultado):
            etapas.rodar_pedido("brief", str(pasta))  # acha os esqueletos novos das referências
    elif etapa == "angulos":
        exigir((pasta / "02-dossie.md").exists(), "a pesquisa ainda não gerou o dossiê (02-dossie.md)")
        prompt = etapas.prompt_angulos(slug, orientacao)
        fluxo = orquestrador.angulos(slug, rota, orientacao)
    elif etapa == "roteiro":
        exigir((pasta / "04-angulo-escolhido.json").exists(), "escolha o ângulo antes do roteiro")
        rodadas = max(1, min(3, int(dados.get("rodadas") or 3)))
        prompt = etapas.prompt_roteiro(slug, orientacao, rodadas)
        fluxo = orquestrador.roteiro(slug, rota, orientacao, rodadas)
    elif etapa == "avaliar":
        versao = int(dados.get("versao") or 0)
        exigir((pasta / f"05-roteiro-v{versao}.md").exists(), f"não existe a versão {versao}")
        prompt = etapas.prompt_avaliar(slug, versao)
        fluxo = orquestrador.avaliar_versao(slug, rota, versao)
    elif etapa == "cortes":
        exigir((pasta / "06-roteiro-final.md").exists(), "defina o roteiro final antes dos cortes")
        prompt = etapas.prompt_cortes(slug)
        fluxo = orquestrador.cortes(slug, rota)
    elif etapa == "visual":
        exigir((pasta / "06-roteiro-final.md").exists(), "defina o roteiro final antes da direção visual")
        prompt = etapas.prompt_visual(slug)
        fluxo = orquestrador.visual(slug, rota)
    elif etapa == "producao":
        exigir((pasta / "08-cenas.json").exists(), "a direção visual ainda não gerou 08-cenas.json")
        prompt = etapas.prompt_producao(slug, bool(dados.get("teste")))
        fluxo = orquestrador.producao(slug, rota, bool(dados.get("teste")))
    elif etapa == "publicacao":
        exigir((pasta / "06-roteiro-final.md").exists(), "defina o roteiro final antes dos textos de publicação")
        prompt = etapas.prompt_publicacao(slug)
        fluxo = orquestrador.publicacao(slug, rota)
    else:
        raise HTTPException(400, f"etapa desconhecida: {etapa}")

    registro = {"avaliar": "roteiro", "publicacao": "producao"}.get(etapa, etapa)
    if registro in etapas.POR_ID:
        banco.atualizar_projeto(slug, etapa=registro)
    if etapa in etapas.ENTRADAS and etapa != "angulos":  # o ângulo vale quando o usuário escolhe, não quando sai a lista
        inicio, depois = time.time(), ao_terminar

        def ao_terminar(execucao, resultado):
            if depois:
                depois(execucao, resultado)
            etapas.registrar(slug, etapa, desde=inicio)
    if rota.provedor in orquestrador.CLAUDE_CODE and etapa not in orquestrador.AGENTE_A_AGENTE:
        id_ = claude_code.iniciar(slug, registro, prompt, esquema, ao_terminar, modelo=rota.modelo or None,
                                  chave_api=rota.provedor == "claude-code-api")
    else:
        id_ = orquestrador.iniciar(slug, registro, rota, fluxo, ao_terminar)
    return {"execucao": id_}


def rodando(slug: str) -> list[int]:
    return claude_code.rodando(slug) + orquestrador.rodando(slug)


@app.get("/api/execucoes/{id_}")
def ver_execucao(id_: int, desde: int = 0):
    e = banco.execucao(id_)
    if not e:
        raise HTTPException(404)
    log = e.pop("log")
    return {**e, "log": log[desde:], "tamanho": len(log)}


@app.post("/api/execucoes/{id_}/cancelar")
def cancelar(id_: int):
    return {"cancelada": claude_code.cancelar(id_) or orquestrador.cancelar(id_)}


# ---------------------------------------------------------------- dados de cada etapa


def ler_texto(caminho: Path) -> str | None:
    return caminho.read_text(encoding="utf-8") if caminho.exists() else None


@app.get("/api/projetos/{slug}/pesquisa")
def pesquisa(slug: str):
    pasta = etapas.pasta(slug)
    b = etapas.brief(slug) or {}
    esqueletos = [r for r in b.get("referencias", []) if isinstance(r, dict)]
    return {"trends_ativa": etapas.trends_ativa(b), "links": (b.get("trends") or {}).get("links") or [],
            "trends": ler_texto(pasta / "02-trends.md"), "dossie": ler_texto(pasta / "02-dossie.md"), "referencias": esqueletos}


@app.get("/api/projetos/{slug}/angulos")
def angulos(slug: str):
    pasta = etapas.pasta(slug)
    return {"angulos": etapas.ler_json(pasta / "03-angulos.json"), "resumo": ler_texto(pasta / "03-angulos.md"),
            "escolhido": etapas.ler_json(pasta / "04-angulo-escolhido.json")}


@app.post("/api/projetos/{slug}/angulos/escolher")
def escolher_angulo(slug: str, dados: dict = Body(...)):
    pasta = etapas.pasta(slug)
    doc = etapas.ler_json(pasta / "03-angulos.json") or {}
    angulo = next((a for a in doc.get("angulos", []) if a.get("id") == dados.get("id")), None)
    exigir(angulo is not None, "ângulo não encontrado")
    escolhido = {**angulo, "ajustes_do_usuario": (dados.get("ajustes") or "").strip() or None,
                 "escolhido_em": date.today().isoformat(), "escolhido_por": "interface web"}
    (pasta / "04-angulo-escolhido.json").write_text(json.dumps(escolhido, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    etapas.registrar(slug, "angulos")
    banco.atualizar_projeto(slug, etapa="roteiro")
    return {"ok": True}


def resumo_avaliacao(a: dict | None) -> dict | None:
    if not a:
        return None
    criterios = [{k: c.get(k) for k in ("id", "nota", "peso", "bloco", "problema", "instrucao")}
                 for c in a.get("criterios", []) if c.get("problema")]
    return {"aprovado": a.get("aprovado"), "nota": a.get("nota_percentual"), "bloqueantes": a.get("bloqueantes_reprovados") or [],
            "resumo": a.get("resumo") or a.get("veredito") or a.get("onde_cumpre"), "criterios": criterios,
            "pontos_de_abandono": a.get("pontos_de_abandono")}


def resumo_medicao(m: dict | None) -> dict | None:
    if not m:
        return None
    return {k: m.get(k) for k in ("palavras", "duracao_estimada", "duracao_alvo", "alertas", "erros", "erro") if m.get(k) is not None}


def nomes_do_roteiro(b: dict | None, docs: list[dict]) -> list[str]:
    nomes = [roteiro.NARRADOR] + [x["nome"] for x in (b or {}).get("personagens") or []]
    for doc in docs:
        for bloco in doc["blocos"]:
            for item in bloco["itens"]:
                if item["tipo"] == "fala" and item["quem"].lower() not in {n.lower() for n in nomes}:
                    nomes.append(item["quem"])
    return nomes


@app.get("/api/projetos/{slug}/roteiros")
def roteiros(slug: str):
    pasta = etapas.pasta(slug)
    final = ler_texto(pasta / "06-roteiro-final.md")
    saida = []
    for versao, caminho in roteiro.versoes(pasta):
        doc = roteiro.ler(caminho)
        origem = re.search(r'(?m)^origem:\s*"?([^"\n]*)"?', doc["frontmatter"])
        medicao = etapas.ler_json(pasta / f"05-medicao-v{versao}.json")
        saida.append({"versao": versao, "arquivo": str(caminho.relative_to(RAIZ)), "doc": doc,
                      "origem": origem.group(1) if origem else "roteirista",
                      "final": final is not None and final == caminho.read_text(encoding="utf-8"),
                      "avaliacao": resumo_avaliacao(etapas.ler_json(pasta / f"05-avaliacao-v{versao}.json")),
                      "medicao": resumo_medicao(medicao)})
    return {"versoes": saida, "tem_final": final is not None,
            "nomes": nomes_do_roteiro(etapas.brief(slug), [v["doc"] for v in saida])}


@app.post("/api/projetos/{slug}/roteiros")
def salvar_roteiro(slug: str, dados: dict = Body(...)):
    """A edição do usuário vira uma versão nova; a anterior fica como estava."""
    pasta = etapas.pasta(slug)
    base = int(dados.get("base") or 0)
    exigir((pasta / f"05-roteiro-v{base}.md").exists(), f"não existe a versão {base}")
    doc = dados.get("doc") or {}
    exigir(isinstance(doc.get("blocos"), list) and doc["blocos"], "o roteiro precisa ter blocos")
    nova = max(v for v, _ in roteiro.versoes(pasta)) + 1
    caminho = pasta / f"05-roteiro-v{nova}.md"
    caminho.write_text(roteiro.montar(doc, nova, f"editada pelo usuário a partir da v{base}"), encoding="utf-8")
    return {"versao": nova, "arquivo": str(caminho.relative_to(RAIZ)), "medicao": resumo_medicao(etapas.medir(caminho))}


@app.post("/api/projetos/{slug}/roteiros/{versao}/final")
def definir_final(slug: str, versao: int):
    pasta = etapas.pasta(slug)
    origem = pasta / f"05-roteiro-v{versao}.md"
    exigir(origem.exists(), f"não existe a versão {versao}")
    shutil.copyfile(origem, pasta / "06-roteiro-final.md")
    etapas.registrar(slug, "roteiro")  # a escolha do usuário vale como roteiro feito com o ângulo atual
    proxima = etapas.proxima(slug, "roteiro")
    banco.atualizar_projeto(slug, etapa=proxima or "roteiro")
    return {"ok": True, "proxima": proxima}


@app.get("/api/projetos/{slug}/cortes")
def cortes(slug: str):
    pasta = etapas.pasta(slug) / "07-cortes"
    itens = []
    for caminho in sorted(pasta.glob("corte-*.md")):
        avaliacao = etapas.ler_json(pasta / f"avaliacao-{caminho.stem}.json")
        itens.append({"arquivo": caminho.name, "doc": roteiro.ler(caminho), "avaliacao": resumo_avaliacao(avaliacao)})
    return {"indice": ler_texto(pasta / "indice.md"), "cortes": itens}


@app.get("/api/projetos/{slug}/visual")
def visual(slug: str):
    pasta = etapas.pasta(slug)
    biblia = etapas.ler_json(pasta / "08-biblia-visual.json") or {}
    cenas = (etapas.ler_json(pasta / "08-cenas.json") or {}).get("cenas", [])
    return {"storyboard": ler_texto(pasta / "08-storyboard.md"), "estilo": biblia.get("estilo"),
            "personagens": biblia.get("personagens", []), "locais": biblia.get("locais", []), "cenas": len(cenas)}


@app.get("/api/projetos/{slug}/producao")
def producao(slug: str):
    pasta = etapas.pasta(slug)
    videos = [str(p.relative_to(pasta)) for p in sorted((pasta / "10-video").glob("*.mp4"))]
    contato = pasta / "09-imagens" / "folha-de-contato.jpg"
    thumb = pasta / "10-video" / "thumbnail.jpg"
    return {"videos": videos, "folha_de_contato": "09-imagens/folha-de-contato.jpg" if contato.exists() else None,
            # a versão no endereço faz o navegador buscar a thumbnail nova depois de uma montagem
            "thumbnail": f"10-video/thumbnail.jpg?v={thumb.stat().st_mtime_ns}" if thumb.exists() else None,
            "creditos": ler_texto(pasta / "09-imagens" / "creditos.md"),
            "narracao": (pasta / "09-narracao" / "narracao.wav").exists(),
            "publicacao": etapas.ler_json(pasta / "11-publicacao.json"), "custo": custo_total(slug)}


def custo_total(slug: str) -> dict:
    """Claude Code (os agentes, por etapa, do banco) mais as APIs pagas da produção (custos.jsonl do projeto)."""
    nomes = {e.id: e.nome for e in etapas.ETAPAS}
    claude = [{"rotulo": f"Claude Code · {nomes.get(c['etapa'], c['etapa'])}", "detalhe": f"{c['execucoes']} execução(ões)",
               "usd": round(c["usd"], 4)} for c in banco.custos_por_etapa(slug)]
    apis = custos.resumo(etapas.pasta(slug))
    producao = [{"rotulo": i["rotulo"], "usd": i["usd"],
                 "detalhe": f"{i['modelo']} · {i['chamadas']} chamada(s)" + (" · estimativa mínima" if i["estimado"] else "")}
                for i in apis["itens"]]
    itens = claude + producao
    return {"total_usd": round(sum(i["usd"] for i in itens), 2), "itens": itens, "sem_preco": apis["sem_preco"],
            "tabela": apis["tabela"], "registro_apis": bool(apis["itens"]),
            "fora_do_banco": not banco.projeto(slug)}


class SemCache(StaticFiles):
    """O navegador revalida o app.js e o CSS a cada carga: sem isso, uma versão nova só aparece com Ctrl+F5."""

    async def get_response(self, path, scope):
        resposta = await super().get_response(path, scope)
        resposta.headers["Cache-Control"] = "no-cache"
        return resposta


app.mount("/estatico", SemCache(directory=AQUI / "estatico"), name="estatico")


@app.get("/")
def inicio():
    return FileResponse(AQUI / "estatico" / "index.html", headers={"Cache-Control": "no-cache"})


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--porta", type=int, default=8765)
    args = ap.parse_args()
    banco.iniciar()
    print(f"interface: http://{args.host}:{args.porta}", flush=True)
    uvicorn.run(app, host=args.host, port=args.porta, log_level="warning")


if __name__ == "__main__":
    main()
