"""Operações do banco em funções simples que recebem e devolvem dicts (a camada web e os fluxos não veem o ORM).

Datas saem como texto ISO 8601 em UTC com fuso (`2026-10-23T12:00:00.123456+00:00`), que o navegador e o
`json.dumps` entendem sem conversão. Cada função abre e fecha a própria sessão: o log e os custos são gravados
de várias threads e processos ao mesmo tempo.

Execuções: `pendente` (na fila) → `rodando` (um trabalhador pegou) → `ok` | `erro` | `cancelada` |
`interrompida` (o trabalhador sumiu sem terminar). Só uma execução ativa (pendente ou rodando) por projeto, e
uma por `chave_exclusiva` nas execuções sem projeto; o banco garante com um índice único parcial, e quem tenta
criar a segunda recebe ErroOcupado.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from nucleo.banco.modelos import (STATUS_ATIVOS, STATUS_FINAIS, Arquivo, ChaveApi, Configuracao, Contexto, Custo,
                                  Desempenho, Execucao, ExecucaoLog, LicaoRoteiro, Projeto)
from nucleo.banco.sessao import agora, e_sqlite, sessao

LOG_POR_CHAMADA = 2000
ORIGENS = ("pipeline", "roteiro-importado")
SERVICOS = ("llm", "busca", "imagem", "narracao")
ROTULOS_SERVICO = {"llm": "Agentes (LLM)", "busca": "Busca na web", "imagem": "Imagens", "narracao": "Narração"}
NOMES_PROVEDOR = {"anthropic": "Claude", "openai": "OpenAI", "gemini": "Gemini", "elevenlabs": "ElevenLabs",
                  "espeak": "espeak"}
JANELAS = ("48h", "7d", "28d")
METRICAS = ("ctr", "retencao_30s", "retencao_media", "viu_vs_passou", "views", "inscritos_ganhos", "inscritos_por_mil")
ESCOPOS = ("projeto", "biblioteca", "pedido", "contexto", "rascunho")


class ErroOcupado(Exception):
    """Já há uma execução ativa para o projeto (ou para a chave exclusiva). `execucao_id` é a que está ativa."""

    def __init__(self, mensagem: str, execucao_id: int | None = None):
        super().__init__(mensagem)
        self.execucao_id = execucao_id


def _iso(valor: datetime | None) -> str | None:
    return valor.isoformat() if valor else None


def _data(valor: datetime | str | float | int | None) -> datetime | None:
    """Aceita datetime, texto ISO ou timestamp (segundos) e devolve datetime com fuso."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, datetime):
        return valor
    if isinstance(valor, (int, float)):
        from datetime import timezone
        return datetime.fromtimestamp(valor, timezone.utc)
    return datetime.fromisoformat(str(valor))


def _dict(obj, *, sem: tuple[str, ...] = ()) -> dict:
    saida = {}
    for coluna in obj.__table__.columns:
        if coluna.key in sem:
            continue
        valor = getattr(obj, coluna.key)
        saida[coluna.key] = _iso(valor) if isinstance(valor, datetime) else valor
    return saida


# ------------------------------------------------------------------------------------------------ projetos

def _projeto(p: Projeto | None) -> dict | None:
    if p is None:
        return None
    d = _dict(p)
    d["formulario"] = d["formulario"] or {}
    return d


def projeto(slug: str) -> dict | None:
    with sessao() as s:
        return _projeto(s.get(Projeto, slug))


def projetos() -> list[dict]:
    with sessao() as s:
        return [_projeto(p) for p in s.scalars(select(Projeto).order_by(Projeto.atualizado_em.desc()))]


def criar_projeto(slug: str, titulo: str, formulario: dict, etapa: str = "configurar",
                  origem: str = "pipeline") -> dict:
    """Levanta ValueError se o slug já existe ou a origem é desconhecida."""
    if origem not in ORIGENS:
        raise ValueError(f"origem desconhecida: {origem}")
    try:
        with sessao() as s:
            s.add(Projeto(slug=slug, titulo=titulo, etapa=etapa, formulario=formulario or {}, origem=origem,
                          criado_em=agora(), atualizado_em=agora()))
    except IntegrityError:
        raise ValueError(f"já existe um projeto {slug}") from None
    return projeto(slug)


CAMPOS_DO_PROJETO = ("titulo", "etapa", "formulario", "sugestoes", "origem")


def atualizar_projeto(slug: str, **campos) -> None:
    if not campos:
        return
    desconhecidos = set(campos) - set(CAMPOS_DO_PROJETO)
    if desconhecidos:
        raise ValueError(f"campos desconhecidos do projeto: {', '.join(sorted(desconhecidos))}")
    if "origem" in campos and campos["origem"] not in ORIGENS:
        raise ValueError(f"origem desconhecida: {campos['origem']}")
    with sessao() as s:
        s.execute(update(Projeto).where(Projeto.slug == slug).values(**campos, atualizado_em=agora()))


def apagar_projeto(slug: str) -> None:
    """Apaga o projeto, as execuções (com o log) e os custos dele. Os arquivos são do nucleo.espaco.apagar."""
    with sessao() as s:
        ids = select(Execucao.id).where(Execucao.projeto_slug == slug)
        s.execute(delete(ExecucaoLog).where(ExecucaoLog.execucao_id.in_(ids)))
        s.execute(delete(Custo).where(or_(Custo.projeto_slug == slug, Custo.execucao_id.in_(ids))))
        s.execute(delete(Execucao).where(Execucao.projeto_slug == slug))
        s.execute(delete(Projeto).where(Projeto.slug == slug))


# ----------------------------------------------------------------------------------------------- execuções

def _execucao(e: Execucao | None, ultimo_log: int | None = None) -> dict | None:
    if e is None:
        return None
    d = _dict(e)
    d["slug"] = d["projeto_slug"]  # o nome antigo, que a camada web usava
    d["parametros"] = d["parametros"] or {}
    d["rota"] = d["rota"] or {}
    d["avisos"] = d["avisos"] or []
    d["custo_usd"] = d["custo_usd"] or 0.0
    if ultimo_log is not None:
        d["ultimo_log"] = ultimo_log
    return d


def _ativa(s, slug: str | None, chave_exclusiva: str | None) -> Execucao | None:
    filtros = []
    if slug is not None:
        filtros.append(Execucao.projeto_slug == slug)
    if chave_exclusiva is not None:
        filtros.append(Execucao.chave_exclusiva == chave_exclusiva)
    if not filtros:
        return None
    return s.scalars(select(Execucao).where(Execucao.status.in_(STATUS_ATIVOS), or_(*filtros)).limit(1)).first()


def criar_execucao(slug: str | None, etapa: str, parametros: dict, chave_exclusiva: str | None = None) -> int:
    """Põe uma execução na fila (pendente). ErroOcupado se já há uma ativa no projeto ou na chave exclusiva;
    ValueError se o projeto não existe."""
    with sessao() as s:
        if slug is not None and s.get(Projeto, slug) is None:
            raise ValueError(f"projeto desconhecido: {slug}")
        if ativa := _ativa(s, slug, chave_exclusiva):
            raise ErroOcupado(f"já há uma execução ativa ({ativa.etapa}, #{ativa.id})", ativa.id)
    try:
        with sessao() as s:
            e = Execucao(projeto_slug=slug, etapa=etapa, status="pendente", parametros=parametros or {},
                         chave_exclusiva=chave_exclusiva, criada_em=agora())
            s.add(e)
            s.flush()
            return e.id
    except IntegrityError:
        # outra requisição criou no mesmo instante: o índice único parcial recusou esta
        with sessao() as s:
            ativa = _ativa(s, slug, chave_exclusiva)
        raise ErroOcupado("já há uma execução ativa", ativa.id if ativa else None) from None


def _ultimo_log(s, id_: int) -> int:
    return s.scalar(select(func.max(ExecucaoLog.id)).where(ExecucaoLog.execucao_id == id_)) or 0


def execucao(id_: int) -> dict | None:
    with sessao() as s:
        e = s.get(Execucao, id_)
        return _execucao(e, _ultimo_log(s, id_) if e else None)


def ultima_execucao(slug: str, etapa: str) -> dict | None:
    with sessao() as s:
        e = s.scalars(select(Execucao).where(Execucao.projeto_slug == slug, Execucao.etapa == etapa)
                      .order_by(Execucao.id.desc()).limit(1)).first()
        return _execucao(e, _ultimo_log(s, e.id) if e else None)


def execucoes(slug: str | None = None, limite: int = 50) -> list[dict]:
    """As últimas execuções (de um projeto, ou de todos), da mais nova para a mais velha."""
    with sessao() as s:
        consulta = select(Execucao).order_by(Execucao.id.desc()).limit(limite)
        if slug is not None:
            consulta = consulta.where(Execucao.projeto_slug == slug)
        return [_execucao(e) for e in s.scalars(consulta)]


def execucoes_ativas(slug: str | None = None) -> list[dict]:
    with sessao() as s:
        consulta = select(Execucao).where(Execucao.status.in_(STATUS_ATIVOS)).order_by(Execucao.id)
        if slug is not None:
            consulta = consulta.where(Execucao.projeto_slug == slug)
        return [_execucao(e) for e in s.scalars(consulta)]


_pegar_sqlite = threading.Lock()


def pegar_proxima(trabalhador: str) -> dict | None:
    """A execução pendente mais antiga passa a `rodando` com este trabalhador. None se a fila está vazia.

    Postgres: SELECT ... FOR UPDATE SKIP LOCKED (vários trabalhadores não pegam a mesma). SQLite: troca
    condicional (só muda se ainda estiver pendente), que também é segura entre processos.
    """
    if e_sqlite():
        with _pegar_sqlite:
            for _ in range(20):
                with sessao() as s:
                    candidato = s.scalar(select(Execucao.id).where(Execucao.status == "pendente")
                                         .order_by(Execucao.id).limit(1))
                    if candidato is None:
                        return None
                    agora_ = agora()
                    mudou = s.execute(update(Execucao).where(Execucao.id == candidato, Execucao.status == "pendente")
                                      .values(status="rodando", trabalhador=trabalhador, iniciada_em=agora_,
                                              batimento_em=agora_)).rowcount
                if mudou:
                    return execucao(candidato)
            return None
    with sessao() as s:
        e = s.scalars(select(Execucao).where(Execucao.status == "pendente").order_by(Execucao.id).limit(1)
                      .with_for_update(skip_locked=True)).first()
        if e is None:
            return None
        e.status, e.trabalhador = "rodando", trabalhador
        e.iniciada_em = e.batimento_em = agora()
        id_ = e.id
    return execucao(id_)


def bater(id_: int) -> bool:
    """Batimento do trabalhador. Devolve True quando ele deve parar: cancelamento pedido, ou a execução já não
    está rodando (foi dada como interrompida por falta de batimento)."""
    with sessao() as s:
        s.execute(update(Execucao).where(Execucao.id == id_, Execucao.status == "rodando")
                  .values(batimento_em=agora()))
        e = s.get(Execucao, id_)
        return e is None or bool(e.cancelar_solicitado) or e.status != "rodando"


def pedir_cancelamento(id_: int) -> bool:
    """Pendente: cancela na hora. Rodando: marca o pedido, que o trabalhador obedece no próximo batimento.
    Devolve False se a execução já tinha terminado (ou não existe)."""
    with sessao() as s:
        e = s.get(Execucao, id_)
        if e is None or e.status not in STATUS_ATIVOS:
            return False
        if e.status == "pendente":
            e.status, e.terminada_em = "cancelada", agora()
            s.add(ExecucaoLog(execucao_id=id_, texto="■ cancelado antes de começar\n", criado_em=agora()))
        else:
            e.cancelar_solicitado = True
        return True


def terminar_execucao(id_: int, status: str, resultado: str = "", estruturado: Any = None,
                      avisos: list[str] | None = None) -> None:
    if status not in STATUS_FINAIS:
        raise ValueError(f"status final desconhecido: {status}")
    valores: dict[str, Any] = {"status": status, "resultado": resultado or "", "terminada_em": agora()}
    if estruturado is not None:
        valores["estruturado"] = estruturado
    if avisos is not None:
        valores["avisos"] = list(avisos)
    with sessao() as s:
        s.execute(update(Execucao).where(Execucao.id == id_).values(**valores))


CAMPOS_DA_EXECUCAO = ("rota", "avisos", "resultado", "estruturado", "parametros", "custo_usd")


def atualizar_execucao(id_: int, **campos) -> None:
    """Muda campos de uma execução em andamento (rota usada, avisos...). Status só por terminar_execucao."""
    desconhecidos = set(campos) - set(CAMPOS_DA_EXECUCAO)
    if desconhecidos:
        raise ValueError(f"campos desconhecidos da execução: {', '.join(sorted(desconhecidos))}")
    if campos:
        with sessao() as s:
            s.execute(update(Execucao).where(Execucao.id == id_).values(**campos))


def acrescentar_aviso(id_: int, texto: str) -> None:
    with sessao() as s:
        e = s.get(Execucao, id_)
        if e is not None:
            e.avisos = [*(e.avisos or []), texto]


def marcar_interrompidas(sem_batimento_ha_s: int = 120) -> int:
    """Execuções `rodando` sem batimento há mais que o limite viram `interrompida` (o trabalhador morreu)."""
    limite = agora() - timedelta(seconds=sem_batimento_ha_s)
    with sessao() as s:
        perdidas = s.scalars(select(Execucao).where(
            Execucao.status == "rodando",
            or_(Execucao.batimento_em < limite,
                Execucao.batimento_em.is_(None) & (func.coalesce(Execucao.iniciada_em, Execucao.criada_em) < limite))
        )).all()
        for e in perdidas:
            e.status, e.terminada_em = "interrompida", agora()
            s.add(ExecucaoLog(execucao_id=e.id, texto="✗ interrompida: o trabalhador parou de responder\n",
                              criado_em=agora()))
        return len(perdidas)


def anexar_log(id_: int, texto: str) -> None:
    """Uma linha (ou um bloco de linhas) do log; termina sempre em quebra de linha."""
    if not texto:
        return
    if not texto.endswith("\n"):
        texto += "\n"
    with sessao() as s:
        s.add(ExecucaoLog(execucao_id=id_, texto=texto, criado_em=agora()))


def log_desde(id_: int, cursor: int) -> tuple[str, int]:
    """O log depois do cursor (o id da última linha já lida; 0 no começo): (texto novo, novo cursor).
    Traz no máximo LOG_POR_CHAMADA linhas por vez; o resto vem na próxima chamada."""
    with sessao() as s:
        linhas = s.execute(select(ExecucaoLog.id, ExecucaoLog.texto)
                           .where(ExecucaoLog.execucao_id == id_, ExecucaoLog.id > (cursor or 0))
                           .order_by(ExecucaoLog.id).limit(LOG_POR_CHAMADA)).all()
    if not linhas:
        return "", cursor or 0
    return "".join(l.texto for l in linhas), linhas[-1].id


# -------------------------------------------------------------------------------------------------- custos

def registrar_custo(servico: str, provedor: str, modelo: str, uso: dict | None, custo_usd: float | None, *,
                    execucao_id: int | None = None, projeto_slug: str | None = None, agente: str | None = None,
                    item: str | None = None, extra: dict | None = None, estimado: bool = False,
                    quando: datetime | str | None = None) -> None:
    """Uma chamada paga. Com execução, o projeto vem dela (se não foi dado) e o custo soma em execucoes.custo_usd
    na mesma transação: quem registra aqui não soma de novo na execução."""
    with sessao() as s:
        if execucao_id is not None:
            e = s.get(Execucao, execucao_id)
            if e is None:
                execucao_id = None  # execução apagada: o custo fica, sem o vínculo
            else:
                projeto_slug = projeto_slug or e.projeto_slug
                if custo_usd:
                    s.execute(update(Execucao).where(Execucao.id == execucao_id)
                              .values(custo_usd=func.coalesce(Execucao.custo_usd, 0) + custo_usd))
        s.add(Custo(execucao_id=execucao_id, projeto_slug=projeto_slug, servico=servico, provedor=provedor or "",
                    modelo=modelo or "", agente=agente, item=item, uso=uso or {}, extra=extra or {},
                    custo_usd=custo_usd, estimado=bool(estimado), quando=_data(quando) or agora()))


def custos_do_projeto(slug: str) -> dict:
    """O que o projeto gastou: total, por etapa (das execuções; `avulso` para o que rodou fora delas) e por
    serviço e modelo. `sem_preco` lista os modelos sem preço em biblioteca/precos.json (fora do total).
    Execuções antigas sem nenhuma linha de custo (o custo veio só no total da execução) também entram."""
    with sessao() as s:
        linhas = s.execute(
            select(Custo.servico, Custo.provedor, Custo.modelo, Custo.custo_usd, Custo.estimado, Custo.execucao_id,
                   Execucao.etapa)
            .outerjoin(Execucao, Execucao.id == Custo.execucao_id)
            .where(or_(Custo.projeto_slug == slug, Execucao.projeto_slug == slug))).all()
        com_custo = {l.execucao_id for l in linhas if l.execucao_id is not None}
        antigas = s.execute(select(Execucao.id, Execucao.etapa, Execucao.custo_usd)
                            .where(Execucao.projeto_slug == slug, Execucao.custo_usd > 0)).all()

    por_etapa: dict[str, dict] = {}
    por_servico: dict[tuple[str, str, str], dict] = {}
    sem_preco: set[str] = set()
    total = 0.0
    for l in linhas:
        etapa = l.etapa or "avulso"
        g = por_etapa.setdefault(etapa, {"etapa": etapa, "execucoes": set(), "usd": 0.0})
        if l.execucao_id is not None:
            g["execucoes"].add(l.execucao_id)
        chave = (l.servico, l.provedor, l.modelo)
        sv = por_servico.setdefault(chave, {
            "servico": l.servico, "provedor": l.provedor, "modelo": l.modelo,
            "rotulo": _rotulo(l.servico, l.provedor), "chamadas": 0, "usd": 0.0, "estimado": False})
        sv["chamadas"] += 1
        sv["estimado"] = sv["estimado"] or bool(l.estimado)
        if l.custo_usd is None:
            sem_preco.add(l.modelo)
            continue
        g["usd"] += l.custo_usd
        sv["usd"] += l.custo_usd
        total += l.custo_usd
    for a in antigas:
        if a.id in com_custo:
            continue
        g = por_etapa.setdefault(a.etapa, {"etapa": a.etapa, "execucoes": set(), "usd": 0.0})
        g["execucoes"].add(a.id)
        g["usd"] += a.custo_usd
        total += a.custo_usd
        sv = por_servico.setdefault(("llm", "", "execucao"), {
            "servico": "llm", "provedor": "", "modelo": "", "rotulo": "Agentes (total da execução)",
            "chamadas": 0, "usd": 0.0, "estimado": False})
        sv["chamadas"] += 1
        sv["usd"] += a.custo_usd
    etapas = [{"etapa": g["etapa"], "execucoes": len(g["execucoes"]), "usd": round(g["usd"], 4)}
              for g in por_etapa.values()]
    servicos = [{**sv, "usd": round(sv["usd"], 4)} for sv in por_servico.values()]
    return {"total_usd": round(total, 4), "por_etapa": etapas,
            "por_servico": sorted(servicos, key=lambda x: -x["usd"]), "sem_preco": sorted(sem_preco)}


def _rotulo(servico: str, provedor: str) -> str:
    base = ROTULOS_SERVICO.get(servico, servico)
    nome = NOMES_PROVEDOR.get(provedor, provedor)
    return f"{base} ({nome})" if nome else base


def custos(projeto_slug: str | None = None, execucao_id: int | None = None) -> list[dict]:
    """As linhas de custo, para relatórios e testes."""
    with sessao() as s:
        consulta = select(Custo).order_by(Custo.id)
        if projeto_slug is not None:
            consulta = consulta.where(Custo.projeto_slug == projeto_slug)
        if execucao_id is not None:
            consulta = consulta.where(Custo.execucao_id == execucao_id)
        return [_dict(c) for c in s.scalars(consulta)]


# ------------------------------------------------------------------------------------ configurações e chaves

def ler_config(chave: str, padrao: Any = None) -> Any:
    with sessao() as s:
        c = s.get(Configuracao, chave)
        return padrao if c is None or c.valor is None else c.valor


def gravar_config(chave: str, valor: Any) -> None:
    with sessao() as s:
        c = s.get(Configuracao, chave)
        if c is None:
            s.add(Configuracao(chave=chave, valor=valor, atualizado_em=agora()))
        else:
            c.valor, c.atualizado_em = valor, agora()


def ler_chave(provedor: str) -> str | None:
    """A chave decifrada, ou None (sem chave, ou o segredo atual não abre a guardada)."""
    from nucleo import seguranca
    with sessao() as s:
        c = s.get(ChaveApi, provedor)
        cifrado = c.valor_cifrado if c else None
    if not cifrado:
        return None
    try:
        return seguranca.decifrar(cifrado)
    except ValueError:
        import sys
        print(f"aviso: a chave de {provedor} guardada no banco não abre com o segredo atual; cadastre de novo",
              file=sys.stderr)
        return None


def gravar_chave(provedor: str, valor: str | None) -> None:
    """Grava cifrada (ou apaga, com None ou vazio)."""
    from nucleo import seguranca
    with sessao() as s:
        c = s.get(ChaveApi, provedor)
        if not valor:
            if c is not None:
                s.delete(c)
            return
        cifrado, mascara = seguranca.cifrar(valor), seguranca.mascarar(valor)
        if c is None:
            s.add(ChaveApi(provedor=provedor, valor_cifrado=cifrado, mascara=mascara, atualizado_em=agora()))
        else:
            c.valor_cifrado, c.mascara, c.atualizado_em = cifrado, mascara, agora()


def mascara_chave(provedor: str) -> str | None:
    with sessao() as s:
        c = s.get(ChaveApi, provedor)
        return c.mascara if c else None


# ------------------------------------------------------------------------------------------------ arquivos

def _arquivo(a: Arquivo) -> dict:
    return _dict(a, sem=("texto", "id"))


def _prefixo_like(prefixo: str) -> str:
    return prefixo.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def indice(prefixo: str) -> dict[str, dict]:
    """Os arquivos cujo caminho começa com o prefixo (os apagados também, com `apagado_em`), sem o texto."""
    with sessao() as s:
        consulta = select(Arquivo).order_by(Arquivo.caminho)
        if prefixo:
            consulta = consulta.where(Arquivo.caminho.like(_prefixo_like(prefixo), escape="\\"))
        return {a.caminho: _arquivo(a) for a in s.scalars(consulta)}


CAMPOS_DO_ARQUIVO = ("escopo", "projeto_slug", "tamanho", "sha256", "mime", "texto", "modificado_em", "versao",
                     "apagado_em")


def registrar_arquivo(caminho: str, **campos) -> dict:
    """Cria ou atualiza a linha do arquivo. Sem `versao`, a versão sobe um (atomicamente). Um arquivo que volta
    a existir perde o `apagado_em`. Devolve a linha gravada (sem o texto)."""
    desconhecidos = set(campos) - set(CAMPOS_DO_ARQUIVO)
    if desconhecidos:
        raise ValueError(f"campos desconhecidos do arquivo: {', '.join(sorted(desconhecidos))}")
    for nome in ("modificado_em", "apagado_em"):
        if nome in campos:
            campos[nome] = _data(campos[nome])
    campos.setdefault("apagado_em", None)
    for tentativa in range(3):
        try:
            with sessao() as s:
                a = s.scalars(select(Arquivo).where(Arquivo.caminho == caminho).with_for_update()).first()
                if a is None:
                    a = Arquivo(caminho=caminho, escopo=campos.get("escopo") or "projeto", versao=campos.get("versao") or 1)
                    s.add(a)
                elif "versao" not in campos:
                    a.versao = (a.versao or 0) + 1
                for nome, valor in campos.items():
                    setattr(a, nome, valor)
                a.atualizado_em = agora()
                s.flush()
                return _arquivo(a)
        except IntegrityError:
            if tentativa == 2:
                raise  # outro processo criou a mesma linha ao mesmo tempo: na próxima volta ela já existe
    raise AssertionError("inalcançável")


def marcar_apagado(caminho: str) -> int | None:
    """Marca o arquivo como apagado (a versão sobe e o texto sai). Devolve a nova versão, ou None se não existia."""
    with sessao() as s:
        a = s.scalars(select(Arquivo).where(Arquivo.caminho == caminho).with_for_update()).first()
        if a is None:
            return None
        if a.apagado_em is None:
            a.apagado_em = agora()
            a.versao = (a.versao or 0) + 1
            a.texto = None
            a.atualizado_em = agora()
        return a.versao


def texto_do_arquivo(caminho: str) -> str | None:
    with sessao() as s:
        return s.scalar(select(Arquivo.texto).where(Arquivo.caminho == caminho, Arquivo.apagado_em.is_(None)))


def arquivo(caminho: str) -> dict | None:
    with sessao() as s:
        a = s.scalars(select(Arquivo).where(Arquivo.caminho == caminho)).first()
        return _arquivo(a) if a else None


# ----------------------------------------------------------------------------------------------- contextos

def contextos() -> list[dict]:
    with sessao() as s:
        return [_dict(c) for c in s.scalars(select(Contexto).order_by(Contexto.atualizado_em.desc()))]


def contexto(id_: str) -> dict | None:
    with sessao() as s:
        c = s.get(Contexto, id_)
        return _dict(c) if c else None


def salvar_contexto(id_: str, **campos) -> dict:
    """Cria ou atualiza (nome, texto, anexos). Devolve o contexto gravado."""
    desconhecidos = set(campos) - {"nome", "texto", "anexos"}
    if desconhecidos:
        raise ValueError(f"campos desconhecidos do contexto: {', '.join(sorted(desconhecidos))}")
    with sessao() as s:
        c = s.get(Contexto, id_)
        if c is None:
            c = Contexto(id=id_, nome=campos.get("nome") or id_, texto="", anexos=[], criado_em=agora())
            s.add(c)
        for nome, valor in campos.items():
            setattr(c, nome, valor if valor is not None else ([] if nome == "anexos" else ""))
        c.atualizado_em = agora()
        s.flush()
        return _dict(c)


def apagar_contexto(id_: str) -> None:
    with sessao() as s:
        s.execute(delete(Contexto).where(Contexto.id == id_))


# ------------------------------------------------------------------------------------------------- lições

def acrescentar_licoes(lista: list[dict]) -> int:
    """Acrescenta lições de roteiro ({secao, regra, erro, correcao, projeto_slug?, versao?}). Uma regra que já
    existe na mesma seção (mesmo texto, sem diferença de espaços e caixa) não entra de novo. Devolve quantas
    entraram."""
    def chave(secao: str, regra: str) -> tuple[str, str]:
        return " ".join((secao or "").lower().split()), " ".join((regra or "").lower().split())

    with sessao() as s:
        existentes = {chave(l.secao, l.regra) for l in s.scalars(select(LicaoRoteiro))}
        novas = 0
        for item in lista:
            regra = (item.get("regra") or "").strip()
            if not regra or chave(item.get("secao", ""), regra) in existentes:
                continue
            existentes.add(chave(item.get("secao", ""), regra))
            s.add(LicaoRoteiro(secao=(item.get("secao") or "").strip(), regra=regra,
                               erro=(item.get("erro") or "").strip(), correcao=(item.get("correcao") or "").strip(),
                               projeto_slug=item.get("projeto_slug") or item.get("projeto"),
                               versao=item.get("versao"), criada_em=agora()))
            novas += 1
        return novas


def licoes() -> list[dict]:
    with sessao() as s:
        return [_dict(l) for l in s.scalars(select(LicaoRoteiro).order_by(LicaoRoteiro.id))]


# --------------------------------------------------------------------------------------------- desempenho

def _desempenho(d: Desempenho) -> dict:
    saida = _dict(d)
    # achatado também, como no antigo dados/desempenho.jsonl: o resumo lê r["ctr"], r["projeto"]...
    return {**(d.extra or {}), **{m: None for m in METRICAS}, **(d.metricas or {}), **saida,
            "projeto": d.projeto_slug, "observacao": d.obs}


def registrar_desempenho(dados: dict) -> dict:
    """Grava (ou substitui) as métricas de um vídeo numa janela. Aceita as métricas soltas (como o antigo
    registro) ou em `metricas`; `projeto`/`projeto_slug` e `obs`/`observacao` valem igual."""
    if not dados.get("video_id"):
        raise ValueError("falta o video_id")
    janela = dados.get("janela") or "7d"
    if janela not in JANELAS:
        raise ValueError(f"janela desconhecida: {janela} (use {', '.join(JANELAS)})")
    metricas = {m: dados[m] for m in METRICAS if dados.get(m) is not None}
    metricas.update({k: v for k, v in (dados.get("metricas") or {}).items() if v is not None})
    if metricas.get("inscritos_ganhos") is not None and metricas.get("views") and "inscritos_por_mil" not in metricas:
        metricas["inscritos_por_mil"] = round(metricas["inscritos_ganhos"] / metricas["views"] * 1000, 2)
    conhecidos = {"video_id", "janela", "projeto", "projeto_slug", "formato", "tecnicas", "tipo_gancho", "metricas",
                  "obs", "observacao", "registrado_em", "id", *METRICAS}
    extra = {k: v for k, v in dados.items() if k not in conhecidos and v is not None}
    valores = {
        "projeto_slug": dados.get("projeto_slug") or dados.get("projeto"),
        "formato": dados.get("formato") or "longo",
        "tecnicas": [t for t in (dados.get("tecnicas") or []) if t],
        "tipo_gancho": dados.get("tipo_gancho") or "",
        "metricas": metricas,
        "extra": extra,
        "obs": dados.get("obs") or dados.get("observacao") or "",
        "registrado_em": _data(dados.get("registrado_em")) or agora(),
    }
    with sessao() as s:
        d = s.scalars(select(Desempenho).where(Desempenho.video_id == dados["video_id"],
                                               Desempenho.janela == janela)).first()
        if d is None:
            d = Desempenho(video_id=dados["video_id"], janela=janela)
            s.add(d)
        for nome, valor in valores.items():
            setattr(d, nome, valor)
        s.flush()
        return _desempenho(d)


def desempenhos(janela: str | None = None) -> list[dict]:
    with sessao() as s:
        consulta = select(Desempenho).order_by(Desempenho.registrado_em, Desempenho.id)
        if janela:
            consulta = consulta.where(Desempenho.janela == janela)
        return [_desempenho(d) for d in s.scalars(consulta)]
