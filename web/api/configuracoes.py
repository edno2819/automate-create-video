"""Configurações: as chaves dos provedores (cifradas no banco), o modelo de cada etapa e agente, as mídias (imagem,
animação, narração, alinhamento) e o estado do servidor.

Tudo sai de um `GET /api/configuracoes` e a configuração de modelos grava com `PUT /api/configuracoes/modelos`
(parcial: o servidor mescla, e `null` numa chave volta ao padrão). Salvar uma chave já a testa: nos LLMs, o teste
lista os modelos que a chave acessa (a lista vai para configuracoes["modelos_disponiveis"] e alimenta a escolha de
modelo); na ElevenLabs, atualiza as vozes da conta; no YouTube, faz uma busca de uma unidade de cota.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.engine import make_url

from nucleo import banco, chaves, provedores, rotas
from nucleo.agentes import registro
from nucleo.armazenamento import armazenamento
from nucleo.banco import repositorios
from nucleo.config import config
from nucleo.provedores import imagem as provedores_imagem
from nucleo.provedores import llm as provedores_llm
from nucleo.provedores import video as provedores_video
from nucleo.provedores import voz as provedores_voz
from nucleo.provedores.erros import ErroProvedor
from web.api.comum import exigir

import custos  # noqa: E402 - ferramentas/custos.py: o comum põe ferramentas/ no caminho

roteador = APIRouter()

TESTES = "testes"
MODELOS_DISPONIVEIS = "modelos_disponiveis"
YOUTUBE_TESTE = ("https://www.googleapis.com/youtube/v3/videos?part=id&chart=mostPopular&maxResults=1&regionCode=BR"
                 "&key={chave}")
TIMEOUT_TESTE_S = 30


def agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def provedor_conhecido(provedor: str) -> str:
    if provedor not in chaves.PROVEDORES:
        raise HTTPException(404, "provedor desconhecido")
    return provedor


# ---------------------------------------------------------------- leitura


def mascara(provedor: str, origem: str | None) -> str | None:
    if origem is None:
        return None
    if origem == "banco" and (m := repositorios.mascara_chave(provedor)):
        return m
    return chaves.mascarar(chaves.ler(provedor))


def lista_de_provedores() -> list[dict]:
    testes = repositorios.ler_config(TESTES, {}) or {}
    disponiveis = repositorios.ler_config(MODELOS_DISPONIVEIS, {}) or {}
    saida = []
    for id_, p in chaves.PROVEDORES.items():
        origem = chaves.origem(id_)
        saida.append({"id": id_, "nome": p["nome"], "variavel": p["variavel"], "usos": list(p.get("usos") or []),
                      "painel": p.get("painel"), "origem": origem, "mascara": mascara(id_, origem),
                      "teste": testes.get(id_) if origem else None,
                      "modelos": list((disponiveis.get(id_) or {}).get("lista") or [])})
    return saida


def agente_da_tela(nome: str) -> dict:
    try:
        a = registro.carregar(nome)
        return {"nome": nome, "nivel": a.nivel, "descricao": a.descricao}
    except (LookupError, ValueError, OSError):
        return {"nome": nome, "nivel": rotas.nivel_do_agente(nome), "descricao": ""}


def etapas_da_tela() -> list[dict]:
    saida = []
    for id_, info in rotas.ETAPAS.items():
        try:
            problemas = rotas.problemas(id_)
        except Exception as e:  # noqa: BLE001 - a tela mostra o motivo em vez de cair inteira
            problemas = [f"não consegui conferir esta etapa: {e}"]
        saida.append({"id": id_, "nome": info["nome"], "agentes": [agente_da_tela(a) for a in info["agentes"]],
                      "problemas": problemas})
    return saida


def descricao_do_banco() -> str:
    """O tipo e o endereço do banco, sem usuário nem senha."""
    try:
        url = make_url(config().banco_url)
    except Exception:  # noqa: BLE001
        return "desconhecido"
    if url.get_backend_name() == "sqlite":
        return f"SQLite ({url.database})"
    lugar = f"{url.host or ''}{f':{url.port}' if url.port else ''}/{url.database or ''}"
    return f"PostgreSQL ({lugar})" if url.get_backend_name() == "postgresql" else f"{url.get_backend_name()} ({lugar})"


def sistema() -> dict:
    c = config()
    saida = {"armazenamento": c.armazenamento, "bucket": c.bucket if c.armazenamento != "local" else None,
             "banco": descricao_do_banco(), "endereco_do_armazenamento": c.s3_endpoint if c.armazenamento != "local" else None,
             "pasta_dos_objetos": str(c.pasta_objetos_local) if c.armazenamento == "local" else None,
             "prefixo": c.prefixo or None,
             "trabalhador": f"embutido na web ({c.concorrencia} por vez)" if c.trabalhador_embutido else "processo separado",
             "autenticacao": "ligada" if c.usuario and c.senha else "desligada"}
    return {k: v for k, v in saida.items() if v is not None}


@roteador.get("/api/configuracoes")
def ver_configuracoes():
    tabela = custos.tabela()
    return {
        "provedores": lista_de_provedores(),
        "modelos": rotas.ler(),
        "etapas": etapas_da_tela(),
        "niveis": list(rotas.NIVEIS),
        "llm": list(provedores.LLM),
        "imagem": {"provedores": provedores_imagem.info()},
        "animacao": {"provedores": provedores_video.info()},
        "narracao": {"fornecedores": provedores_voz.info()},
        "whisper": list(rotas.WHISPER),
        "precos": sorted((tabela.get("modelos") or {}).keys()),
        "precos_conferidos_em": tabela.get("conferido_em"),
        "sistema": sistema(),
    }


@roteador.put("/api/configuracoes/modelos")
def gravar_modelos(parcial: dict = Body(...)):
    """Mescla a configuração parcial na gravada (null apaga a escolha) e devolve a configuração inteira."""
    try:
        return rotas.gravar(parcial)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


# ---------------------------------------------------------------- chaves


def sem_chave_no_texto(texto: str, chave: str | None) -> str:
    texto = str(texto)
    if chave:
        texto = texto.replace(chave, "•••").replace(urllib.parse.quote(chave, safe=""), "•••")
    return " ".join(texto.split())[:300]


def testar_youtube(chave: str) -> dict:
    """Uma busca de uma unidade de cota, sem novas tentativas: o teste responde rápido."""
    try:
        with urllib.request.urlopen(YOUTUBE_TESTE.format(chave=urllib.parse.quote(chave, safe="")),
                                    timeout=TIMEOUT_TESTE_S) as r:
            dados = json.load(r)
    except urllib.error.HTTPError as e:
        mensagem = "chave recusada ou sem permissão" if e.code in (400, 401, 403) else "erro do serviço"
        return {"ok": False, "mensagem": f"HTTP {e.code}: {mensagem}"}
    ok = isinstance(dados, dict) and dados.get("items") is not None
    return {"ok": ok, "mensagem": "a busca de trends usa a API (cota diária de 10.000 unidades)" if ok
            else "resposta inesperada da API do YouTube"}


def testar_provedor(provedor: str) -> dict:
    """Uma chamada barata que só funciona com a chave válida. Nunca levanta: a falha vira {ok: False, mensagem}."""
    chave = chaves.ler(provedor)
    if not chave:
        return {"ok": False, "mensagem": "sem chave cadastrada"}
    try:
        if provedor in provedores.LLM:
            cliente = provedores_llm.provedor(provedor, chave)
            mensagem = cliente.testar()
            modelos = cliente.modelos if cliente.modelos is not None else cliente.listar_modelos()
            with banco.trava(f"config:{MODELOS_DISPONIVEIS}"):
                disponiveis = repositorios.ler_config(MODELOS_DISPONIVEIS, {}) or {}
                disponiveis[provedor] = {"lista": list(modelos), "atualizado_em": agora_iso()}
                repositorios.gravar_config(MODELOS_DISPONIVEIS, disponiveis)
            return {"ok": True, "mensagem": mensagem}
        if provedor == "elevenlabs":
            vozes = provedores_voz.atualizar_vozes_elevenlabs(chave)
            return {"ok": True, "mensagem": f"{len(vozes)} vozes na conta"}
        if provedor == "youtube":
            return testar_youtube(chave)
        return {"ok": False, "mensagem": "provedor sem teste"}
    except ErroProvedor as e:
        return {"ok": False, "mensagem": sem_chave_no_texto(e, chave)}
    except Exception as e:  # noqa: BLE001 - rede, JSON, resposta inesperada: o teste falhou, e a tela diz por quê
        return {"ok": False, "mensagem": sem_chave_no_texto(f"{type(e).__name__}: {e}", chave)}


def testar_e_guardar(provedor: str) -> dict:
    resultado = {**testar_provedor(provedor), "quando": agora_iso()}
    with banco.trava(f"config:{TESTES}"):
        testes = repositorios.ler_config(TESTES, {}) or {}
        testes[provedor] = resultado
        repositorios.gravar_config(TESTES, testes)
    return resultado


def recusar_se_do_ambiente(provedor: str) -> None:
    if chaves.origem(provedor) == "ambiente":
        raise HTTPException(400, f"a chave de {chaves.PROVEDORES[provedor]['nome']} vem da variável de ambiente "
                                 f"{chaves.PROVEDORES[provedor]['variavel']} do servidor: troque-a onde ela é definida")


@roteador.put("/api/configuracoes/chaves/{provedor}")
def gravar_chave(provedor: str, dados: dict = Body(...)):
    """Grava a chave (cifrada, no banco) e já testa: {teste, mascara}."""
    provedor_conhecido(provedor)
    recusar_se_do_ambiente(provedor)
    valor = dados.get("valor")
    exigir(isinstance(valor, str) and valor.strip(), "cole a chave")
    try:
        chaves.gravar(provedor, valor.strip())
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {"teste": testar_e_guardar(provedor), "mascara": chaves.mascarar(valor.strip())}


@roteador.delete("/api/configuracoes/chaves/{provedor}")
def apagar_chave(provedor: str):
    provedor_conhecido(provedor)
    recusar_se_do_ambiente(provedor)
    chaves.gravar(provedor, None)
    with banco.trava(f"config:{TESTES}"):
        testes = repositorios.ler_config(TESTES, {}) or {}
        if testes.pop(provedor, None) is not None:
            repositorios.gravar_config(TESTES, testes)
    return {"ok": True, "origem": chaves.origem(provedor)}


@roteador.post("/api/configuracoes/chaves/{provedor}/testar")
def testar_chave(provedor: str):
    provedor_conhecido(provedor)
    return testar_e_guardar(provedor)


# ---------------------------------------------------------------- saúde


def saude_do_banco() -> dict:
    try:
        with banco.sessao() as s:
            s.execute(text("SELECT 1"))
        return {"ok": True, "mensagem": descricao_do_banco().split(" (")[0]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "mensagem": f"sem conexão ({type(e).__name__})"}


def saude_do_armazenamento() -> dict:
    try:
        a = armazenamento()
        a.existe(f"{config().prefixo}.saude")  # um HEAD: responde (falso) quando o armazenamento está de pé
        return {"ok": True, "mensagem": a.descricao()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "mensagem": f"sem acesso ({type(e).__name__})"}


@roteador.get("/api/saude")
def saude():
    """Sem autenticação (o healthcheck do contêiner usa). 503 quando o banco ou o armazenamento não respondem, com o
    motivo em `detail` para a tela de sistema."""
    b, a = saude_do_banco(), saude_do_armazenamento()
    corpo = {"ok": b["ok"] and a["ok"], "banco": b, "armazenamento": a}
    if corpo["ok"]:
        return corpo
    falhas = [f"{nome}: {x['mensagem']}" for nome, x in (("banco", b), ("armazenamento", a)) if not x["ok"]]
    return JSONResponse({**corpo, "detail": "; ".join(falhas)}, status_code=503)
