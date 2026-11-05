"""Execução das etapas: a fila no banco, o contexto de uma execução e o trabalhador que as roda.

O servidor web só enfileira (`enfileirar`): grava uma execução `pendente` no banco e responde. Quem roda é o
trabalhador (`python -m nucleo.trabalhador`, ou uma thread do servidor com TRABALHADOR_EMBUTIDO=1): pega a próxima
execução da fila (FOR UPDATE SKIP LOCKED no Postgres), baixa do armazenamento o que ela precisa (o projeto, as
partes sincronizadas da biblioteca, o pedido e o contexto usado), roda o fluxo da etapa (nucleo/pipeline/fluxos.py),
manda batimento a cada 5 s (é por ele que chega o pedido de cancelamento) e envia os arquivos de volta depois de
cada agente ou script e no fim, mesmo com erro. Nada fica só na memória do processo: dá para ter vários
trabalhadores, e um que morre deixa a execução como `interrompida` (pela falta de batimento).

A execução guarda a etapa da trilha (`avaliar` conta como `roteiro`, `publicacao` e `refazer-cenas` como
`producao`, `dublagem` e `exportar` como `edicao`), para a tela mostrar a última execução de cada etapa como antes;
o fluxo que de fato roda vai em `parametros["fluxo"]`.

O `Contexto` é o que os fluxos e o motor de agentes recebem (o protocolo está em nucleo/agentes/motor.py): log no
banco, cancelamento, pasta temporária, processos abertos, custo de cada chamada, avisos, a rota de cada agente e a
sincronização com o armazenamento.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from nucleo import RAIZ as CODIGO
from nucleo.agentes.erros import Cancelado
from nucleo.banco import repositorios
from nucleo.pipeline import etapas

log = logging.getLogger(__name__)

BATIMENTO_S = 5.0
ESPERA_FILA_S = 2.0
MARCAR_INTERROMPIDAS_S = 60.0
SEM_PROJETO = ("personagem", "referencia")
# execuções que contam, na trilha da tela, como outra etapa
TRILHA = {"avaliar": "roteiro", "publicacao": "producao", "refazer-cenas": "producao", "dublagem": "edicao",
          "exportar": "edicao"}
NOMES = {"sugestoes": "sugestões do pedido", "pesquisa": "pesquisa", "angulos": "ângulos", "ganchos": "ganchos",
         "roteiro": "roteiro", "avaliar": "avaliação do roteiro", "visual": "direção visual",
         "producao": "produção",
         "publicacao": "textos de publicação", "importar": "importação do roteiro", "refazer-cenas": "refazer cenas",
         "personagem": "personagem", "referencia": "análise de referência", "dublagem": "dublagem",
         "exportar": "exportação da edição"}

_ativos: dict[int, "Contexto"] = {}
_ativos_trava = threading.Lock()


# ---------------------------------------------------------------- contexto de uma execução


def _custos():
    """ferramentas/custos.py (registro de custo no banco, com o preço de biblioteca/precos.json)."""
    pasta = str(CODIGO / "ferramentas")
    if pasta not in sys.path:
        sys.path.insert(0, pasta)
    import custos
    return custos


class Contexto:
    """O que um fluxo (e cada agente que ele roda) sabe da execução. Seguro entre threads: a pesquisa roda agentes
    em paralelo."""

    def __init__(self, execucao: dict):
        self.execucao_id: int = execucao["id"]
        self.slug: str | None = execucao.get("projeto_slug")
        self.etapa: str = execucao.get("etapa") or ""
        parametros = dict(execucao.get("parametros") or {})
        self.fluxo: str = parametros.pop("fluxo", None) or self.etapa
        self.parametros: dict = parametros
        self.cancelado = threading.Event()
        self.desligando = False  # cancelado porque o trabalhador está saindo, não pelo usuário
        self.tmp = Path(tempfile.mkdtemp(prefix=f"execucao-{self.execucao_id}-"))
        self.processos: set = set()
        self.trava = threading.Lock()
        self.custo_usd = 0.0
        self.sem_preco: set[str] = set()
        self.avisos: list[str] = []
        self.rotas: dict[str, dict] = {}
        self.prefixos: list[str] = []  # o que vai para o armazenamento a cada sincronizar()

    # ------------------------------------------------ log, avisos, rota

    def log(self, texto: str) -> None:
        texto = str(texto or "").rstrip("\n")
        if not texto:
            return
        try:
            repositorios.anexar_log(self.execucao_id, texto + "\n")
        except Exception as e:  # noqa: BLE001 - o log nunca derruba a execução
            print(f"[execução {self.execucao_id}] {texto} (log no banco falhou: {e})", file=sys.stderr)

    def avisar(self, texto: str) -> None:
        """Um aviso para o usuário: vai para o log e para a lista de avisos da execução (a tela mostra)."""
        texto = " ".join(str(texto).split())
        with self.trava:
            if texto in self.avisos:
                return
            self.avisos.append(texto)
        self.log(f"! {texto}")
        try:
            repositorios.acrescentar_aviso(self.execucao_id, texto)
        except Exception as e:  # noqa: BLE001
            print(f"[execução {self.execucao_id}] aviso não gravado: {e}", file=sys.stderr)

    def registrar_rota(self, nome: str, valor: dict) -> None:
        """O provedor e o modelo de um agente (ou da imagem, da narração, do whisper) usados nesta execução."""
        with self.trava:
            if self.rotas.get(nome) == valor:
                return
            self.rotas[nome] = dict(valor)
            rotas = dict(self.rotas)
        try:
            repositorios.atualizar_execucao(self.execucao_id, rota=rotas)
        except Exception as e:  # noqa: BLE001
            print(f"[execução {self.execucao_id}] rota não gravada: {e}", file=sys.stderr)

    def usar(self, agente: str, rota) -> None:
        self.registrar_rota(agente, {"provedor": rota.provedor, "modelo": rota.modelo})

    # ------------------------------------------------ custo

    def somar(self, provedor: str, modelo: str, uso: dict, agente: str | None = None, servico: str = "llm") -> None:
        """Uma chamada paga de LLM ou de busca: grava em `custos` (que também soma no custo_usd da execução) e
        acumula aqui para o resumo."""
        try:
            valor = _custos().registrar_llm(provedor, modelo, uso or {}, agente=agente, servico=servico,
                                            execucao_id=self.execucao_id, projeto_slug=self.slug)
        except Exception as e:  # noqa: BLE001 - custo nunca derruba a execução
            print(f"[execução {self.execucao_id}] custo não registrado: {e}", file=sys.stderr)
            valor = None
        with self.trava:
            if valor is None:
                self.sem_preco.add(modelo)
            else:
                self.custo_usd += valor

    def custo_total(self) -> float:
        """O custo da execução no banco: os agentes e também as imagens e a narração dos scripts."""
        try:
            return float((repositorios.execucao(self.execucao_id) or {}).get("custo_usd") or 0.0)
        except Exception:  # noqa: BLE001
            return self.custo_usd

    # ------------------------------------------------ armazenamento

    def acompanhar(self, prefixo: str) -> None:
        """Passa a enviar este prefixo ao armazenamento em cada sincronizar()."""
        with self.trava:
            if prefixo not in self.prefixos:
                self.prefixos.append(prefixo)

    def sincronizar(self, levantar: bool = False) -> int:
        """Envia ao armazenamento o que mudou nos prefixos da execução (o projeto, e o que o fluxo acompanha)."""
        from nucleo import espaco
        total = 0
        for prefixo in list(self.prefixos):
            try:
                total += espaco.enviar(prefixo)
            except Exception as e:  # noqa: BLE001
                self.log(f"✗ não consegui enviar {prefixo} ao armazenamento: {type(e).__name__}: {e}")
                if levantar:
                    raise
        return total

    # ------------------------------------------------ cancelamento

    def cancelar(self) -> None:
        """Para no próximo passo do agente e mata o grupo de cada processo aberto (ffmpeg, whisper...)."""
        self.cancelado.set()
        for proc in list(self.processos):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    def conferir_cancelamento(self) -> None:
        if self.cancelado.is_set():
            raise Cancelado()


# ---------------------------------------------------------------- fila


def _fluxos():
    from nucleo.pipeline import fluxos
    return fluxos


def chave_padrao(etapa: str, parametros: dict) -> str | None:
    """A chave exclusiva de uma execução sem projeto: uma por personagem, uma por vídeo de referência."""
    if etapa == "personagem" and parametros.get("slug"):
        return f"personagem:{parametros['slug']}"
    if etapa == "referencia":
        alvo = _fluxos().video_id(parametros.get("url") or "") or (parametros.get("url") or "").strip()
        return f"referencia:{alvo}" if alvo else None
    return None


def enfileirar(slug: str | None, etapa: str, parametros: dict | None = None, chave_exclusiva: str | None = None) -> int:
    """Põe a execução na fila e devolve o id. ValueError com etapa desconhecida, projeto que falta ou que não existe;
    repositorios.ErroOcupado se já há uma execução ativa no projeto (ou na chave exclusiva)."""
    fluxos = _fluxos()
    if etapa not in fluxos.FLUXOS:
        raise ValueError(f"etapa desconhecida: {etapa}")
    parametros = {k: v for k, v in dict(parametros or {}).items() if k != "fluxo"}
    if etapa in SEM_PROJETO:
        if etapa == "personagem" and not parametros.get("slug"):
            parametros["slug"] = fluxos.slug_livre_de_personagem(parametros.get("nome") or parametros.get("descricao") or "")
        chave_exclusiva = chave_exclusiva or chave_padrao(etapa, parametros)
    elif not slug:
        raise ValueError(f"a etapa {etapa} precisa de um projeto")
    registro = TRILHA.get(etapa, etapa)
    id_ = repositorios.criar_execucao(slug, registro, {**parametros, "fluxo": etapa}, chave_exclusiva)
    if slug and registro in etapas.POR_ID:
        try:
            repositorios.atualizar_projeto(slug, etapa=registro)
        except Exception as e:  # noqa: BLE001 - a etapa da tela não pode impedir a execução
            log.warning("não consegui atualizar a etapa do projeto %s: %s", slug, e)
    return id_


def cancelar(id_: int) -> bool:
    """Pede o cancelamento (o trabalhador obedece no próximo batimento; pendente cancela na hora). Se a execução
    roda neste mesmo processo, o cancelamento é imediato. False se ela já tinha terminado."""
    ok = repositorios.pedir_cancelamento(id_)
    with _ativos_trava:
        ctx = _ativos.get(id_)
    if ok and ctx is not None:
        ctx.cancelar()
    return ok


def ativos() -> list[int]:
    """As execuções que rodam neste processo."""
    with _ativos_trava:
        return sorted(_ativos)


# ---------------------------------------------------------------- preparação (armazenamento -> espaço local)


def contexto_usado(slug: str) -> str | None:
    """O contexto do pedido (pedidos/contextos/<id>/contexto.md, ou um .md solto do formato antigo), como prefixo
    para baixar: o formulário no banco diz; senão, o brief."""
    caminho = None
    try:
        caminho = (((repositorios.projeto(slug) or {}).get("formulario") or {}).get("valores") or {}).get("contexto")
    except Exception:  # noqa: BLE001
        caminho = None
    if not caminho or "\n" in str(caminho):
        caminho = (etapas.brief(slug) or {}).get("contexto_arquivo")
    caminho = str(caminho or "").strip().lstrip("./")
    if not caminho.startswith("pedidos/contextos/") or ".." in caminho.split("/"):
        return None
    partes = caminho.split("/")
    return f"pedidos/contextos/{partes[2]}/" if len(partes) > 3 else caminho


def prefixos_para_baixar(ctx: Contexto) -> list[str]:
    from nucleo import espaco
    prefixos = [p for p in espaco.SINCRONIZADOS if p.startswith("biblioteca/")]
    if ctx.slug:
        prefixos += [f"projetos/{ctx.slug}/", f"pedidos/web/{ctx.slug}.yaml"]
        if c := contexto_usado(ctx.slug):
            prefixos.append(c)
    if ctx.fluxo == "personagem" and ctx.parametros.get("slug"):
        prefixos.append(f"rascunhos/personagens/{ctx.parametros['slug']}/")
    return list(dict.fromkeys(prefixos))


def preparar(ctx: Contexto) -> int:
    """Baixa o que a execução lê e marca o que ela envia de volta."""
    from nucleo import espaco
    if ctx.slug:
        ctx.acompanhar(f"projetos/{ctx.slug}/")
    if ctx.fluxo == "personagem" and ctx.parametros.get("slug"):
        ctx.acompanhar(f"rascunhos/personagens/{ctx.parametros['slug']}/")
    if ctx.fluxo in ("pesquisa", "referencia"):
        ctx.acompanhar("biblioteca/esqueletos/")
    baixados = 0
    for prefixo in prefixos_para_baixar(ctx):
        baixados += espaco.baixar(prefixo)
    if baixados:
        ctx.log(f"· {baixados} arquivo(s) baixado(s) do armazenamento")
    if ctx.slug:
        etapas.pasta(ctx.slug).mkdir(parents=True, exist_ok=True)
    return baixados


# ---------------------------------------------------------------- trabalhador


class Trabalhador:
    """Roda as execuções da fila, `concorrencia` de cada vez, cada uma numa thread."""

    def __init__(self, concorrencia: int | None = None, nome: str | None = None, *, batimento_s: float = BATIMENTO_S,
                 espera_s: float = ESPERA_FILA_S):
        if concorrencia is None:
            from nucleo.config import config
            concorrencia = config().concorrencia
        self.concorrencia = max(1, int(concorrencia))
        self.nome = nome or f"{socket.gethostname()}:{os.getpid()}"
        self.batimento_s = batimento_s
        self.espera_s = espera_s
        self._parar = threading.Event()
        self._vagas = threading.BoundedSemaphore(self.concorrencia)
        self._threads: set[threading.Thread] = set()
        self._contextos: dict[int, Contexto] = {}
        self._trava = threading.Lock()

    # ------------------------------------------------ laço

    def rodar_para_sempre(self) -> None:
        """Pega e roda execuções até parar() ser chamado."""
        self._parar.clear()
        ultima_marcacao = 0.0
        log.info("trabalhador %s: %d execução(ões) de cada vez", self.nome, self.concorrencia)
        while not self._parar.is_set():
            if time.monotonic() - ultima_marcacao > MARCAR_INTERROMPIDAS_S:
                ultima_marcacao = time.monotonic()
                try:
                    if n := repositorios.marcar_interrompidas():
                        log.warning("%d execução(ões) sem batimento marcadas como interrompidas", n)
                except Exception as e:  # noqa: BLE001
                    log.warning("não consegui conferir as execuções interrompidas: %s", e)
            if not self._vagas.acquire(timeout=self.espera_s):
                continue
            try:
                execucao = repositorios.pegar_proxima(self.nome)
            except Exception as e:  # noqa: BLE001 - banco fora do ar: espera e tenta de novo
                self._vagas.release()
                log.warning("não consegui ler a fila: %s", e)
                self._parar.wait(self.espera_s)
                continue
            if execucao is None:
                self._vagas.release()
                self._parar.wait(self.espera_s)
                continue
            t = threading.Thread(target=self._rodar_e_liberar, args=(execucao,), daemon=True,
                                 name=f"execucao-{execucao['id']}")
            with self._trava:
                self._threads.add(t)
            t.start()
        for t in list(self._threads):
            t.join(timeout=30)

    def parar(self) -> None:
        """Para de pegar execuções e interrompe as que estão rodando (ficam como `interrompida`)."""
        self._parar.set()
        with self._trava:
            contextos = list(self._contextos.values())
        for ctx in contextos:
            ctx.desligando = True
            ctx.cancelar()

    def processar_fila(self) -> int:
        """Roda, uma a uma e nesta thread, as execuções pendentes até a fila esvaziar. Devolve quantas rodou."""
        n = 0
        while (execucao := repositorios.pegar_proxima(self.nome)) is not None:
            self.rodar_uma(execucao)
            n += 1
        return n

    def _rodar_e_liberar(self, execucao: dict) -> None:
        try:
            self.rodar_uma(execucao)
        finally:
            with self._trava:
                self._threads.discard(threading.current_thread())
            self._vagas.release()

    # ------------------------------------------------ uma execução

    def _bater(self, ctx: Contexto, parar: threading.Event) -> None:
        avisado = False
        while not parar.wait(self.batimento_s):
            try:
                if repositorios.bater(ctx.execucao_id) and not avisado:
                    avisado = True
                    ctx.cancelar()
            except Exception as e:  # noqa: BLE001 - um batimento perdido não para a execução
                log.warning("batimento da execução %s falhou: %s", ctx.execucao_id, e)

    def rodar_uma(self, execucao: dict) -> str:
        """Roda uma execução já marcada como `rodando` (pegar_proxima) e grava o fim dela. Devolve o status."""
        ctx = Contexto(execucao)
        with _ativos_trava:
            _ativos[ctx.execucao_id] = ctx
        with self._trava:
            self._contextos[ctx.execucao_id] = ctx
        parar_batimento = threading.Event()
        batimento = threading.Thread(target=self._bater, args=(ctx, parar_batimento), daemon=True,
                                     name=f"batimento-{ctx.execucao_id}")
        batimento.start()
        status, resumo, estruturado = "erro", "", None
        inicio = time.time()
        fluxos = _fluxos()
        try:
            ctx.log(f"iniciado: {NOMES.get(ctx.fluxo, ctx.fluxo)}" + (f" do projeto {ctx.slug}" if ctx.slug else "")
                    + f" (trabalhador {self.nome})")
            if ctx.fluxo not in fluxos.FLUXOS:
                raise ValueError(f"etapa desconhecida: {ctx.fluxo}")
            preparar(ctx)
            ctx.conferir_cancelamento()
            saida = fluxos.FLUXOS[ctx.fluxo](ctx, ctx.slug, ctx.parametros)
            estruturado = saida if isinstance(saida, (dict, list)) else None
            resumo = json.dumps(saida, ensure_ascii=False) if estruturado is not None else str(saida or "")
            registro = TRILHA.get(ctx.fluxo, ctx.fluxo)
            if ctx.slug and registro in etapas.ENTRADAS and registro not in ("angulos", "ganchos"):
                # o ângulo e o gancho valem quando o usuário escolhe, não quando sai a lista
                etapas.registrar(ctx.slug, registro, desde=inicio)
            status = "ok"
        except Cancelado:
            status = "interrompida" if ctx.desligando else "cancelada"
            ctx.log("■ interrompida: o trabalhador foi desligado" if ctx.desligando else "■ cancelado pelo usuário")
        except Exception as e:  # noqa: BLE001 - qualquer falha vira erro da execução, com o motivo no log
            resumo = f"{type(e).__name__}: {e}"
            ctx.log(f"✗ terminou com erro: {resumo}\n{traceback.format_exc(limit=6)}")
        finally:
            parar_batimento.set()
            try:
                ctx.sincronizar(levantar=True)
            except Exception as e:  # noqa: BLE001
                if status == "ok":
                    status, resumo = "erro", f"os arquivos não foram enviados ao armazenamento: {type(e).__name__}: {e}"
            if status == "ok":
                custo = ctx.custo_total()
                ctx.log(f"✓ concluído · US$ {custo:.2f}"
                        + (f" (sem preço para {', '.join(sorted(ctx.sem_preco))})" if ctx.sem_preco else ""))
            self._terminar(ctx, status, resumo, estruturado)
            shutil.rmtree(ctx.tmp, ignore_errors=True)
            with _ativos_trava:
                _ativos.pop(ctx.execucao_id, None)
            with self._trava:
                self._contextos.pop(ctx.execucao_id, None)
        return status

    @staticmethod
    def _terminar(ctx: Contexto, status: str, resumo: str, estruturado: Any) -> None:
        try:
            atual = repositorios.execucao(ctx.execucao_id) or {}
            if atual.get("status") not in ("pendente", "rodando"):
                # outro processo já deu a execução por encerrada (interrompida por falta de batimento): fica
                ctx.log(f"· a execução já estava como {atual.get('status')}; o resultado desta rodada não foi gravado")
                return
            repositorios.terminar_execucao(ctx.execucao_id, status, resumo, estruturado, ctx.avisos)
        except Exception as e:  # noqa: BLE001
            print(f"[execução {ctx.execucao_id}] não consegui gravar o fim ({status}): {e}", file=sys.stderr)


__all__ = ["BATIMENTO_S", "Contexto", "SEM_PROJETO", "TRILHA", "Trabalhador", "ativos", "cancelar", "chave_padrao",
           "contexto_usado", "enfileirar", "preparar", "prefixos_para_baixar"]
