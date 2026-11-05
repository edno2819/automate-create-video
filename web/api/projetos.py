"""Projetos: lista, criação (do zero ou com um roteiro pronto), estado da trilha, capa, fork, formulário,
anexos, arquivos e a aprovação do pedido.

O estado de cada projeto (etapa da tela, formulário, sugestões, origem) fica no banco; os arquivos de
`projetos/<slug>/`, no armazenamento, com a cópia local sincronizada antes de cada leitura. A trilha e o que está
pronto ou desatualizado vêm de nucleo.pipeline.etapas, que lê a cópia local.
"""

from __future__ import annotations

import dataclasses
import json
import os
import secrets
import subprocess
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from nucleo import espaco
from nucleo.banco import repositorios
from nucleo.pipeline import etapas
from web.api import formulario
from web.api.comum import (EXTENSOES_AUDIO, EXTENSOES_IMAGEM, SLUG, caminho_dentro, em_andamento, enviar_projeto,
                           exigir, exigir_sem_execucao, ffmpeg_disponivel, garantir_se_existe, gravar_upload, ler_json,
                           nome_livre, pedido_web, prefixo, raiz, rodar_script, servir, sincronizar_projeto,
                           validar_slug, vivos)

import pedido  # noqa: E402 - ferramentas/pedido.py: o comum põe ferramentas/ no caminho

roteador = APIRouter()

IMPORTADO = "roteiro-importado"
# as execuções que aparecem na tela de cada etapa da trilha (a última delas é a que a tela mostra)
EXECUCOES_DA_TELA = {"roteiro": ("roteiro", "avaliar"), "producao": ("producao", "publicacao", "refazer-cenas")}
LIMITE_TITULO = 300
LIMITE_ROTEIRO = 1_000_000  # caracteres: um roteiro de 15 minutos tem uns 15 mil
LIMITE_IMAGEM_MB = 25
LIMITE_MUSICA_MB = 200
EXTENSOES_CAPA = (".png", ".jpg", ".jpeg", ".webp")


# ---------------------------------------------------------------- projeto e trilha


def etapa_dict(e) -> dict:
    if isinstance(e, dict):
        return dict(e)
    if dataclasses.is_dataclass(e):
        return dataclasses.asdict(e)
    return {k: v for k, v in vars(e).items() if not k.startswith("_")}


def importar_pasta(slug: str) -> dict | None:
    """Projeto que está no armazenamento mas não no banco (feito pela conversa, numa instalação antiga): entra com o
    pedido original, e a tela começa na primeira etapa pendente."""
    if not vivos(prefixo(slug)):
        return None
    p = sincronizar_projeto(slug)
    formulario_ = {"valores": {}, "personagens": []}
    for nome in ("00-pedido-original.yaml", "00-pedido.yaml"):
        if (p / nome).exists():
            try:
                lido = pedido.carregar(p / nome)
            except Exception:  # noqa: BLE001 - um pedido ilegível não impede abrir o projeto
                continue
            formulario_["valores"] = {k: v for k, v in lido.valores.items() if v not in (None, [], "") and k != "personagens"}
            formulario_["personagens"] = lido.valores.get("personagens") or []
            break
    b = ler_json(p / "00-brief.json") or {}
    titulo = b.get("titulo") or b.get("tema") or formulario_["valores"].get("titulo") or slug
    origem = IMPORTADO if (p / "00-roteiro-original.md").exists() and not (p / "02-dossie.md").exists() else "pipeline"
    try:
        etapa = etapas.primeira_pendente(slug)
    except Exception:  # noqa: BLE001
        etapa = "configurar"
    try:
        return repositorios.criar_projeto(slug, str(titulo), formulario_, etapa, origem=origem)
    except ValueError:  # outra requisição importou no mesmo instante
        return repositorios.projeto(slug)


def carregar_projeto(slug: str) -> dict:
    validar_slug(slug)
    p = repositorios.projeto(slug) or importar_pasta(slug)
    if p is None:
        raise HTTPException(404, f"projeto {slug} não existe")
    return p


def importado(p: dict) -> bool:
    """O projeto nasceu de um roteiro pronto do usuário (o banco diz, ou os arquivos dizem)."""
    return p.get("origem") == IMPORTADO or etapas.importado(p["slug"])


def trilha(p: dict) -> list[dict]:
    """As etapas que a tela do projeto mostra, na ordem. A importação do roteiro só aparece no projeto que nasceu de
    um roteiro pronto (lá, pesquisa, ângulo e roteiro continuam na trilha, como etapas que não valem para ele): a
    tela decide pelo que vem depois das sugestões olhando se a importação está na lista."""
    proprio = importado(p)
    return [etapa_dict(e) for e in etapas.ETAPAS if proprio or etapa_dict(e)["id"] != "importar"]


def para_tela(e: dict | None) -> dict | None:
    """A execução como a tela a mostra: `etapa` é o fluxo que roda (avaliar, publicacao, refazer-cenas), e a etapa
    da trilha em que ela conta fica em `etapa_da_trilha`."""
    if not e:
        return e
    fluxo = (e.get("parametros") or {}).get("fluxo")
    return {**e, "etapa": fluxo or e["etapa"], "etapa_da_trilha": e["etapa"]}


def ultima_execucao(slug: str, etapa: str) -> dict | None:
    candidatas = [e for nome in EXECUCOES_DA_TELA.get(etapa, (etapa,))
                  if (e := repositorios.ultima_execucao(slug, nome))]
    return para_tela(max(candidatas, key=lambda e: e["id"], default=None))


def ids_da_trilha(p: dict) -> list[str]:
    return [e["id"] for e in trilha(p)]


def slugs_no_armazenamento() -> set[str]:
    return {c.split("/")[1] for c in vivos("projetos/") if c.count("/") >= 2}


def slug_livre(base: str) -> str:
    """O primeiro de base, base-2, base-3... que não é projeto no banco, nem pasta no armazenamento ou na cópia
    local, nem pedido gravado pela interface."""
    ocupados = {p["slug"] for p in repositorios.projetos()} | slugs_no_armazenamento()
    slug, n = base, 2
    while (slug in ocupados or (raiz() / "projetos" / slug).exists() or (raiz() / pedido_web(slug)).exists()
           or repositorios.arquivo(pedido_web(slug))):
        slug, n = f"{base}-{n}", n + 1
    return slug


# ---------------------------------------------------------------- capa


def capa_do_indice(slug: str, arquivos: dict[str, dict]) -> tuple[str, dict] | None:
    """A imagem que representa o projeto na lista, pelo índice do armazenamento (sem baixar nada): a thumbnail com o
    texto, a imagem gerada para ela ou, nos projetos produzidos antes da thumbnail, a primeira cena. A mesma ordem de
    nucleo.pipeline.etapas.capa, que olha a cópia local."""
    base = prefixo(slug)
    candidatas = [f"{base}10-video/thumbnail.jpg"] + [f"{base}09-imagens/thumbnail.{ext}" for ext in ("png", "jpg", "webp")]
    if (doc := json_do_indice(f"{base}08-cenas.json", arquivos)) and isinstance(doc.get("cenas"), list) and doc["cenas"]:
        primeira = doc["cenas"][0].get("id") if isinstance(doc["cenas"][0], dict) else None
        if primeira:
            candidatas += [f"{base}09-imagens/{primeira}.{ext}" for ext in ("png", "jpg", "webp")]
    pasta_imagens = f"{base}09-imagens/"
    candidatas += sorted(c for c in arquivos if c.startswith(pasta_imagens + "B") and "/" not in c[len(pasta_imagens):]
                         and Path(c).suffix.lower() in EXTENSOES_CAPA)
    return next(((c, arquivos[c]) for c in candidatas if c in arquivos), None)


def json_do_indice(caminho: str, arquivos: dict[str, dict]):
    if caminho not in arquivos:
        return None
    texto = repositorios.texto_do_arquivo(caminho)
    try:
        return json.loads(texto) if texto else None
    except ValueError:
        return None


def versao_de(registro: dict) -> str:
    return (registro.get("sha256") or str(registro.get("versao") or ""))[:12]


def endereco_capa(slug: str, arquivos: dict[str, dict]) -> str | None:
    achada = capa_do_indice(slug, arquivos)
    # a versão no endereço troca quando a imagem muda, e o navegador busca a capa nova
    return f"/api/projetos/{slug}/capa?v={versao_de(achada[1])}" if achada else None


@roteador.get("/api/projetos/{slug}/capa")
def capa_do_projeto(slug: str):
    """A capa reduzida a 640 px, guardada fora do projeto (.espaco/capas, só deste processo) e refeita quando a
    imagem de origem muda: a lista não baixa a thumbnail inteira (até 2 MB) de cada projeto."""
    validar_slug(slug)
    achada = capa_do_indice(slug, vivos(prefixo(slug)))
    if achada is None:
        raise HTTPException(404, "sem capa")
    caminho, registro = achada
    origem = garantir_se_existe(caminho)
    if origem is None:
        raise HTTPException(404, "sem capa")
    pasta_cache = raiz() / espaco.PASTA_INDICE / "capas"  # fora do que sincroniza
    destino = pasta_cache / f"{slug}-{versao_de(registro) or 'x'}.jpg"
    if not destino.exists():
        pasta_cache.mkdir(parents=True, exist_ok=True)
        temporario = destino.with_name(f".{destino.stem}.{secrets.token_hex(4)}.jpg")
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(origem), "-vf",
                            "scale=640:640:force_original_aspect_ratio=decrease", "-frames:v", "1", "-q:v", "4",
                            str(temporario)], capture_output=True) if ffmpeg_disponivel() else None
        if r is None or r.returncode or not temporario.exists():
            temporario.unlink(missing_ok=True)
            return FileResponse(origem)
        os.replace(temporario, destino)
        for velha in pasta_cache.glob(f"{slug}-*.jpg"):  # as capas das versões anteriores
            if velha != destino and velha.name[len(slug) + 1:-4].isalnum():
                velha.unlink(missing_ok=True)
    return FileResponse(destino, headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------- lista e criação


@roteador.get("/api/projetos")
def listar():
    por_projeto: dict[str, dict[str, dict]] = defaultdict(dict)
    for caminho, r in vivos("projetos/").items():
        partes = caminho.split("/")
        if len(partes) >= 3 and SLUG.fullmatch(partes[1]):
            por_projeto[partes[1]][caminho] = r
    saida, no_banco = [], set()
    for p in repositorios.projetos():
        no_banco.add(p["slug"])
        saida.append({"slug": p["slug"], "titulo": p["titulo"], "etapa": p["etapa"], "atualizado_em": p["atualizado_em"],
                      "origem": p.get("origem") or "pipeline", "no_banco": True,
                      "capa": endereco_capa(p["slug"], por_projeto.get(p["slug"], {}))})
    for slug in sorted(set(por_projeto) - no_banco):
        b = json_do_indice(f"projetos/{slug}/00-brief.json", por_projeto[slug]) or {}
        saida.append({"slug": slug, "titulo": b.get("titulo") or b.get("tema") or slug, "etapa": None, "atualizado_em": None,
                      "origem": "pipeline", "no_banco": False, "capa": endereco_capa(slug, por_projeto[slug])})
    return saida


@roteador.post("/api/projetos")
def criar(dados: dict = Body(...)):
    """Um projeto novo. Com `roteiro`, o texto colado vira 00-roteiro-original.md e o projeto pula pesquisa, ângulo
    e roteiro: depois do pedido aprovado, o formatador o põe no formato do sistema."""
    titulo = (dados.get("titulo") or "").strip() if isinstance(dados.get("titulo"), str) else ""
    exigir(titulo, "o título é obrigatório")
    exigir(len(titulo) <= LIMITE_TITULO, f"o título passa de {LIMITE_TITULO} caracteres")
    roteiro = dados.get("roteiro")
    if roteiro is not None:
        exigir(isinstance(roteiro, str) and roteiro.strip(), "cole o roteiro")
        exigir(len(roteiro) <= LIMITE_ROTEIRO, "o roteiro é grande demais")
    base = etapas.slug_de(dados.get("slug") if isinstance(dados.get("slug"), str) and dados.get("slug") else titulo)
    # até onde ir é do usuário (obrigatório na configuração, nunca sugerido): começa no vídeo completo
    valores = {"titulo": titulo, "trends.ativa": roteiro is None, "producao.ate": "video"}
    for _ in range(20):
        slug = slug_livre(base)
        formulario_ = {"valores": {**valores, "slug": slug}, "personagens": [], "modo_voz": ""}
        try:
            p = repositorios.criar_projeto(slug, titulo, formulario_, origem=IMPORTADO if roteiro is not None else "pipeline")
            break
        except ValueError:  # outra requisição pegou o mesmo slug no mesmo instante
            continue
    else:
        raise HTTPException(409, "não achei um nome livre para o projeto; tente de novo")
    if roteiro is not None:
        texto = roteiro.replace("\r\n", "\n")
        espaco.gravar_texto(f"projetos/{slug}/00-roteiro-original.md", texto if texto.endswith("\n") else texto + "\n")
    return p


# ---------------------------------------------------------------- estado


@roteador.get("/api/projetos/{slug}")
def estado(slug: str):
    p = carregar_projeto(slug)
    pasta = sincronizar_projeto(slug)
    b = etapas.brief(slug)
    defasadas = etapas.defasagens(slug) or {}
    lista = []
    for e in trilha(p):
        lista.append({**e, "aplicavel": bool(etapas.aplicavel(e["id"], b, slug)), "pronta": bool(etapas.pronta(slug, e["id"])),
                      "desatualizada": defasadas.get(e["id"]), "execucao": ultima_execucao(slug, e["id"])})
    # as palavras narradas do roteiro importado: a tela calcula a duração com a velocidade escolhida
    roteiro = pedido.roteiro_importado(pasta) if importado(p) else None
    return {"projeto": p, "etapas": lista, "brief": ler_json(pasta / "00-brief.json"), "rodando": para_tela(em_andamento(slug)),
            "roteiro_importado": {"palavras": pedido.palavras_do_roteiro(roteiro)} if roteiro else None}


# ---------------------------------------------------------------- fork, formulário, etapa


@roteador.post("/api/projetos/{slug}/fork")
def criar_fork(slug: str, dados: dict = Body(...)):
    """Um projeto novo com tudo do original até a etapa escolhida, inclusive, para refazer dali em diante sem mexer
    no original. O custo do fork começa do zero: só conta o que ele mesmo rodar."""
    p = carregar_projeto(slug)
    ate = dados.get("ate")
    exigir(ate in ids_da_trilha(p), "etapa desconhecida")
    if em_andamento(slug):
        raise HTTPException(409, "espere a execução em andamento terminar antes de criar o fork")
    sincronizar_projeto(slug)
    ocupados = {x["slug"] for x in repositorios.projetos()} | slugs_no_armazenamento()
    novo = etapas.slug_livre(slug, ocupados)
    try:
        copiadas = etapas.fork(slug, novo, ate)  # copia pelo espaço de trabalho: os arquivos já sobem
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    formulario_ = json.loads(json.dumps(p["formulario"] or {}).replace(slug, novo))
    formulario_.setdefault("valores", {})["slug"] = novo
    titulo = (dados.get("titulo") or "").strip() if isinstance(dados.get("titulo"), str) else ""
    repositorios.criar_projeto(novo, (titulo or f"{p['titulo']} (fork)")[:LIMITE_TITULO], formulario_, ate,
                               origem=p.get("origem") or "pipeline")
    if p.get("sugestoes"):
        repositorios.atualizar_projeto(novo, sugestoes=p["sugestoes"])
    return {"slug": novo, "etapa": ate, "copiadas": copiadas}


@roteador.put("/api/projetos/{slug}/formulario")
def salvar_formulario(slug: str, formulario_: dict = Body(...)):
    """O formulário fica só no banco (a tela salva enquanto o usuário digita): nada aqui toca nos arquivos."""
    carregar_projeto(slug)
    exigir(isinstance(formulario_.get("valores", {}), dict), "valores precisa ser um objeto")
    exigir(isinstance(formulario_.get("personagens", []), list), "personagens precisa ser uma lista")
    formulario_.setdefault("valores", {})["slug"] = slug
    titulo = formulario_["valores"].get("titulo")
    titulo = titulo.strip()[:LIMITE_TITULO] if isinstance(titulo, str) else ""
    repositorios.atualizar_projeto(slug, formulario=formulario_, **({"titulo": titulo} if titulo else {}))
    return {"ok": True}


@roteador.put("/api/projetos/{slug}/etapa")
def mudar_etapa(slug: str, dados: dict = Body(...)):
    p = carregar_projeto(slug)
    exigir(dados.get("etapa") in ids_da_trilha(p), "etapa desconhecida")
    repositorios.atualizar_projeto(slug, etapa=dados["etapa"])
    return {"ok": True}


# ---------------------------------------------------------------- anexos e arquivos


@roteador.post("/api/projetos/{slug}/anexos")
def anexar(slug: str, tipo: str = Form(...), arquivo: UploadFile = File(...)):
    """Imagem de referência (fica com o projeto, em 00-anexos/) ou música (vai para a biblioteca de músicas, para
    os outros vídeos também). Devolve o caminho a partir da raiz, que é o que o pedido guarda."""
    carregar_projeto(slug)
    nome = Path(arquivo.filename or "arquivo")
    ext = nome.suffix.lower()
    if tipo == "imagem":
        exigir(ext in EXTENSOES_IMAGEM, f"imagem precisa ser {', '.join(sorted(EXTENSOES_IMAGEM))}")
        exigir_sem_execucao(slug)
        pasta_destino, limite = f"projetos/{slug}/00-anexos/", LIMITE_IMAGEM_MB
    elif tipo == "musica":
        exigir(ext in EXTENSOES_AUDIO, f"música precisa ser {', '.join(sorted(EXTENSOES_AUDIO))}")
        pasta_destino, limite = "biblioteca/musicas/", LIMITE_MUSICA_MB
    else:
        raise HTTPException(400, "tipo é imagem ou musica")
    caminho = nome_livre(pasta_destino, etapas.slug_de(nome.stem, 10), ext)
    gravar_upload(caminho, arquivo.file, limite)
    return {"caminho": caminho, "nome": caminho.rsplit("/", 1)[1]}


@roteador.get("/api/projetos/{slug}/arquivos/{caminho:path}")
def arquivo_do_projeto(slug: str, caminho: str):
    """Qualquer arquivo do projeto (imagens, áudio, vídeo, textos), com Range: o player de vídeo pede aos pedaços."""
    validar_slug(slug)
    return servir(caminho_dentro(prefixo(slug), caminho))


# ---------------------------------------------------------------- aprovação do pedido


def preparar_biblioteca(valores: dict) -> None:
    """O que o pedido.py lê além do pedido: os personagens da biblioteca, os esqueletos das referências, o canal e o
    contexto salvo escolhido. Tudo na cópia local antes de rodar."""
    espaco.baixar("biblioteca/personagens/")
    espaco.baixar("biblioteca/esqueletos/")
    garantir_se_existe("biblioteca/canal.json")
    contexto = valores.get("contexto")
    if isinstance(contexto, str) and contexto.startswith("pedidos/contextos/"):
        try:
            pasta_contexto = espaco.validar(contexto).rsplit("/", 1)[0] + "/"
        except ValueError:
            return
        if pasta_contexto != "pedidos/contextos/":
            espaco.baixar(pasta_contexto)
        else:
            garantir_se_existe(contexto)  # contexto antigo: um .md solto


@roteador.post("/api/projetos/{slug}/confirmar")
def confirmar(slug: str, dados: dict = Body(...)):
    """Aprovação do pedido: o que o usuário escreveu ou mudou vai para pedidos/web/<slug>.yaml; as sugestões que
    ele aceitou como vieram entram no completar como decisões do sistema (--definir). Depois, o brief."""
    p = carregar_projeto(slug)
    exigir_sem_execucao(slug)
    pasta = sincronizar_projeto(slug)
    do_usuario, sugeridos = formulario.separar_aprovacao(slug, dados)
    preparar_biblioteca({**sugeridos, **do_usuario})
    caminho, erros = formulario.gravar_pedido_do_usuario(slug, do_usuario)
    exigir(not erros, "\n".join(erros))
    args = ["completar", caminho, "--projetos", "projetos", *formulario.argumentos_de_definir(sugeridos)]
    if (pasta / "00-pedido.yaml").exists() or (pasta / "00-brief.json").exists():
        args.append("--sobrescrever")
    codigo, saida = rodar_script("ferramentas/pedido.py", *args)
    if codigo != 0:
        raise HTTPException(400, saida or f"pedido.py completar saiu com código {codigo}")
    codigo_b, saida_b = rodar_script("ferramentas/pedido.py", "brief", f"projetos/{slug}")
    enviar_projeto(slug)
    if codigo_b != 0:
        raise HTTPException(400, f"{saida}\n\n{saida_b}".strip())
    titulo = do_usuario.get("titulo") if isinstance(do_usuario.get("titulo"), str) else None
    repositorios.atualizar_projeto(slug, etapa="importar" if importado(p) else "pesquisa",
                                   titulo=(titulo or p["titulo"]).strip()[:LIMITE_TITULO])
    return {"completar": saida, "brief": saida_b}
