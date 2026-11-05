"""Motor de agentes: roda os agentes de agentes/*.md com qualquer LLM (Claude, GPT ou Gemini) pela API.

O motor lê a definição do agente (o frontmatter diz as ferramentas, o nível e se ele vê imagens; o corpo é a
instrução), monta o prompt de sistema (instruções + ambiente + agentes/_contexto.md), oferece ao modelo as
ferramentas dele (nucleo/agentes/ferramentas.py), executa aqui cada chamada e devolve o resultado, até o
modelo terminar. Quando o fluxo pede saída estruturada (`esquema`), o agente ganha a ferramenta Entregar,
cujos parâmetros são o próprio esquema: o motor valida a entrega com jsonschema e devolve os erros ao modelo
(até 3 vezes); terminar sem entregar faz o motor pedir a entrega.

O contexto (`ctx`) é o da execução (nucleo/pipeline/execucao.py): o motor só usa o protocolo abaixo, então
ContextoSimples serve para testes e para uso avulso.

    log(texto)                          uma linha do log da execução
    cancelado: threading.Event          o usuário pediu para parar
    tmp: Path                           pasta temporária da execução
    processos: set                      subprocessos abertos (o cancelamento mata o grupo de cada um)
    somar(provedor, modelo, uso, agente, servico="llm")   custo de cada chamada
    execucao_id, slug                   (slug limita a escrita a projetos/<slug>/)
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from nucleo import RAIZ as CODIGO
from nucleo.agentes import registro
from nucleo.agentes.erros import Cancelado, ErroMotor
from nucleo.agentes.ferramentas import DEFINICOES, Ferramentas, declaracoes, linha_de_ferramenta, raiz
from nucleo.agentes.registro import Agente
from nucleo.provedores.erros import ErroProvedor
from nucleo.provedores.llm import MAX_SAIDA, Conversa, Resultado

__all__ = ["Agente", "Cancelado", "ContextoSimples", "ErroMotor", "Rota", "agente_avulso", "chamada_unica",
           "json_do_texto", "rodar_agente", "sistema_de"]

MAX_ERROS_ENTREGA = 3
MAX_LEMBRETES = 2
MAX_CONTINUACOES = 4

REGRAS = """Você está rodando sem ninguém no terminal, chamado pelo servidor do projeto. Não pergunte nada nem espere
resposta: as decisões do usuário já foram tomadas e estão nos arquivos do projeto e na mensagem que você recebeu.
Faça só a tarefa pedida, salve as saídas nos arquivos de sempre e termine com um resumo curto, em português, do que
foi feito e do que o usuário precisa olhar. Para trabalhar, chame as ferramentas: um texto sem chamada de ferramenta
encerra a tarefa. Nunca procure chaves de API, variáveis de ambiente ou configuração do servidor: você não precisa
delas, e o orquestrador diz na mensagem o que estiver disponível (a chave do YouTube, por exemplo)."""
REGRA_ENTREGA = """Esta tarefa termina com uma saída estruturada: quando acabar, chame a ferramenta Entregar com o
resultado (os parâmetros dela são o formato pedido). Só a chamada de Entregar encerra a tarefa; se a entrega tiver
erro de formato, a resposta diz qual e você chama de novo, corrigida."""
CONTINUE = ("Sua resposta foi cortada no limite de tamanho. Continue de onde parou; para um arquivo longo, grave em "
            "partes (Write e depois Edit).")
DESCRICAO_ENTREGAR = ("Entrega o resultado final da tarefa, no formato pedido (os parâmetros são o formato). Chame uma "
                      "vez, no fim. Se a entrega tiver erro de formato, a resposta diz qual e você chama de novo.")


@dataclass
class Rota:
    provedor: str  # anthropic | openai | gemini
    modelo: str


@dataclass
class ContextoSimples:
    """O contexto mínimo de uma execução, para testes e para rodar um agente fora do trabalhador."""
    log: Callable[[str], None] = field(default=lambda texto: print(texto, file=sys.stderr))
    cancelado: threading.Event = field(default_factory=threading.Event)
    tmp: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="agente-")))
    processos: set = field(default_factory=set)
    execucao_id: int | None = None
    slug: str | None = None
    custo_usd: float = 0.0
    sem_preco: set = field(default_factory=set)
    usos: list = field(default_factory=list)
    avisos: list = field(default_factory=list)
    trava: threading.Lock = field(default_factory=threading.Lock)

    def somar(self, provedor: str, modelo: str, uso: dict, agente: str | None = None, servico: str = "llm") -> None:
        valor = custo_llm(modelo, uso)
        with self.trava:
            self.usos.append({"provedor": provedor, "modelo": modelo, "uso": dict(uso), "agente": agente,
                              "servico": servico, "custo_usd": valor})
            if valor is None:
                self.sem_preco.add(modelo)
            else:
                self.custo_usd += valor

    def avisar(self, texto: str) -> None:
        with self.trava:
            self.avisos.append(texto)
        self.log(f"! {texto}")

    def sincronizar(self) -> None:
        pass

    def cancelar(self) -> None:
        self.cancelado.set()
        for proc in list(self.processos):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


def custo_llm(modelo: str, uso: dict) -> float | None:
    """O custo pela tabela de biblioteca/precos.json (ferramentas/custos.py); None quando não há preço."""
    try:
        if str(CODIGO / "ferramentas") not in sys.path:
            sys.path.insert(0, str(CODIGO / "ferramentas"))
        import custos
        return custos.custo_llm(modelo, uso)
    except Exception:  # noqa: BLE001 - custo nunca derruba um agente
        return None


# ---------------------------------------------------------------- prompt de sistema


def hoje() -> str:
    try:
        from zoneinfo import ZoneInfo
        agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    except Exception:  # noqa: BLE001 - imagem sem tzdata: o horário de Brasília sem horário de verão
        agora = datetime.now(timezone(timedelta(hours=-3)))
    return agora.date().isoformat()


def sistema_de(agente: Agente, rota: Rota, ctx, esquema: dict | None = None) -> str:
    ferramentas = [f for f in agente.ferramentas if f in DEFINICOES] + (["Entregar"] if esquema else [])
    regras = REGRAS + ("\n\n" + REGRA_ENTREGA if esquema else "")
    contexto = registro.contexto_comum()
    return f"""{agente.instrucoes}

---

## Ambiente

Você é o agente `{agente.nome}`, rodando no motor de agentes do servidor com o modelo {rota.modelo} ({rota.provedor}).
O diretório de trabalho é a raiz do repositório ({raiz()}): caminhos relativos partem dela. Hoje é {hoje()}
(horário de Brasília). Pasta temporária para arquivos de trabalho: {ctx.tmp}. Ferramentas disponíveis:
{", ".join(ferramentas) or "nenhuma"}.

{regras}""" + (f"\n\n## Contexto do projeto\n\n{contexto}" if contexto else "")


# ---------------------------------------------------------------- saída estruturada


def declaracao_entregar(esquema: dict) -> tuple[dict, bool]:
    """A ferramenta Entregar com o esquema como parâmetros. Um esquema que não é objeto vai dentro de
    {"resultado": ...}; o segundo valor diz se é preciso desembrulhar."""
    esquema = {k: v for k, v in esquema.items() if k != "$schema"}
    embrulhado = esquema.get("type") != "object"
    parametros = ({"type": "object", "properties": {"resultado": esquema}, "required": ["resultado"]}
                  if embrulhado else esquema)
    return {"nome": "Entregar", "descricao": DESCRICAO_ENTREGAR, "parametros": parametros}, embrulhado


def erros_do_esquema(dados: Any, esquema: dict) -> list[str]:
    from jsonschema import Draft202012Validator
    erros = sorted(Draft202012Validator(esquema).iter_errors(dados), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(raiz)'}: {e.message}"[:300] for e in erros[:20]]


def json_do_texto(texto: str) -> Any:
    """O JSON de uma resposta em texto (com ou sem cerca de código). ErroMotor se não houver."""
    texto = (texto or "").strip()
    if m := re.search(r"```(?:json)?\s*(.*?)```", texto, re.S):
        texto = m.group(1).strip()
    for abre, fecha in (("{", "}"), ("[", "]")):
        inicio, fim = texto.find(abre), texto.rfind(fecha)
        if inicio != -1 and fim > inicio:
            try:
                return json.loads(texto[inicio:fim + 1])
            except json.JSONDecodeError:
                continue
    raise ErroMotor("a resposta não trouxe o JSON pedido")


# ---------------------------------------------------------------- o laço


def _laco(conversa: Conversa, ctx, rota: Rota, nome: str, ferramentas: Ferramentas | None, disponiveis: list[str],
          esquema: dict | None, embrulhado: bool, max_turnos: int, recuo: str) -> str | dict:
    final = ""
    erros_entrega = lembretes = 0
    for turno in range(1, max_turnos + 1):
        if ctx.cancelado.is_set():
            raise Cancelado()
        try:
            passo = conversa.passo()
        except ErroProvedor as e:
            ctx.log(f"{recuo}✗ {nome}: {str(e)[:300]}")
            raise
        ctx.somar(rota.provedor, rota.modelo, passo.uso, nome)
        if passo.texto:
            final = passo.texto
            ctx.log(recuo + "› " + " ".join(passo.texto.split())[:400])
        if not passo.chamadas:
            if passo.cortado:
                conversa.usuario(CONTINUE)
                continue
            if esquema is None:
                ctx.log(f"{recuo}✓ {nome} terminou em {turno} turno(s)")
                return final
            # sem Entregar: um JSON válido no texto também serve; senão, pede a entrega
            try:
                dados = json_do_texto(final)
                if embrulhado and isinstance(dados, dict) and set(dados) == {"resultado"}:
                    dados = dados["resultado"]
                if not erros_do_esquema(dados, esquema):
                    ctx.log(f"{recuo}✓ {nome} entregou (no texto) em {turno} turno(s)")
                    return dados
            except (ErroMotor, ValueError):
                pass
            lembretes += 1
            if lembretes > MAX_LEMBRETES:
                raise ErroMotor(f"{nome} terminou sem entregar o resultado no formato pedido")
            conversa.usuario("Você terminou sem chamar a ferramenta Entregar. Chame Entregar agora com o resultado, "
                             "no formato dos parâmetros dela.")
            continue
        resultados: list[Resultado] = []
        entregue: Any = None
        for ch in passo.chamadas:
            if ch.nome == "Entregar" and esquema is not None:
                dados = ch.args.get("resultado") if embrulhado and isinstance(ch.args, dict) else ch.args
                erros = ["mais de uma entrega no mesmo passo: só a primeira vale"] if entregue is not None else \
                    erros_do_esquema(dados, esquema)
                if not erros:
                    entregue = dados
                    ctx.log(recuo + linha_de_ferramenta("Entregar", ch.args))
                    resultados.append(Resultado(ch.id, ch.nome, "entregue"))
                    continue
                erros_entrega += 1
                ctx.log(f"{recuo}✗ entrega fora do formato ({len(erros)} erro(s)): {erros[0][:160]}")
                if erros_entrega > MAX_ERROS_ENTREGA:
                    raise ErroMotor(f"{nome} não entregou no formato pedido depois de {MAX_ERROS_ENTREGA} correções:\n"
                                    + "\n".join(erros))
                resultados.append(Resultado(ch.id, ch.nome, "erro: a entrega não segue o formato pedido. Corrija e "
                                                            "chame Entregar de novo:\n- " + "\n- ".join(erros)))
                continue
            ctx.log(recuo + linha_de_ferramenta(ch.nome, ch.args if isinstance(ch.args, dict) else {}))
            try:
                if ferramentas is None:
                    raise ErroMotor(f"a ferramenta {ch.nome} não está disponível")
                texto, imagens = ferramentas.executar(ch.nome, ch.args, disponiveis)
            except Cancelado:
                raise
            except (ErroMotor, ErroProvedor, OSError, TypeError, ValueError, UnicodeError) as e:
                texto, imagens = f"erro: {e}", []
                ctx.log(f"{recuo}  ✗ {str(e)[:200]}")
            resultados.append(Resultado(ch.id, ch.nome, texto, imagens))
        if entregue is not None:
            ctx.log(f"{recuo}✓ {nome} entregou em {turno} turno(s)")
            return entregue
        if ctx.cancelado.is_set():
            raise Cancelado()
        conversa.resultados(resultados)
    raise ErroMotor(f"o agente {nome} passou de {max_turnos} turnos sem terminar")


def _conversa(rota: Rota, sistema: str, decls: list[dict], max_saida: int) -> Conversa:
    from nucleo import provedores
    return provedores.llm(rota.provedor).conversa(rota.modelo, sistema, decls, max_saida)


def rodar_agente(agente: str | Agente, mensagem: str, rota: Rota, ctx, *, esquema: dict | None = None,
                 imagens: Sequence[tuple[str, bytes]] = (), recuo: str = "    ", max_saida: int = MAX_SAIDA) -> str | dict:
    """Roda o agente até ele terminar. Devolve o texto final ou, com `esquema`, o objeto entregue (validado).
    O uso de cada passo vai para ctx.somar. Levanta Cancelado, ErroMotor ou ErroProvedor (SemChave, SemCota...)."""
    if isinstance(agente, str):
        agente = registro.carregar(agente)
    cabeca = recuo[:-4] if len(recuo) >= 4 else ""
    ctx.log(f"{cabeca}▶ agente {agente.nome} ({rota.provedor} · {rota.modelo})")
    disponiveis = [f for f in agente.ferramentas if f in DEFINICOES]
    if desconhecidas := [f for f in agente.ferramentas if f not in DEFINICOES]:
        ctx.log(f"{recuo}· ferramentas que o motor não tem, ignoradas: {', '.join(desconhecidas)}")
    decls = declaracoes(disponiveis)
    embrulhado = False
    if esquema is not None:
        entregar, embrulhado = declaracao_entregar(esquema)
        decls.append(entregar)
    if imagens and not agente.visao:
        ctx.log(f"{recuo}· {agente.nome} não vê imagens: {len(imagens)} imagem(ns) ignorada(s)")
        imagens = ()
    conversa = _conversa(rota, sistema_de(agente, rota, ctx, esquema), decls, max_saida)
    conversa.usuario(mensagem, imagens)
    return _laco(conversa, ctx, rota, agente.nome, Ferramentas(ctx, rota, agente), disponiveis, esquema, embrulhado,
                 agente.max_turnos, recuo)


def chamada_unica(sistema: str | Agente, mensagem: str, rota: Rota, ctx, *, esquema: dict | None = None,
                  imagens: Sequence[tuple[str, bytes]] = (), nome: str | None = None,
                  max_saida: int = MAX_SAIDA) -> str | dict:
    """Uma conversa sem ferramentas e sem o contexto do projeto (o leitor frio não pode saber nada além do texto).
    `sistema` é o texto do prompt ou um Agente (vale só a instrução dele). Com `esquema`, a única ferramenta é
    Entregar e volta o objeto validado; sem, volta o texto (as continuações de uma resposta cortada emendadas)."""
    if isinstance(sistema, Agente):
        nome = nome or sistema.nome
        sistema = sistema.instrucoes
    nome = nome or "chamada"
    ctx.log(f"▶ {nome} ({rota.provedor} · {rota.modelo})")
    decls, embrulhado = [], False
    if esquema is not None:
        entregar, embrulhado = declaracao_entregar(esquema)
        decls.append(entregar)
        sistema = f"{sistema}\n\n{REGRA_ENTREGA}"
    conversa = _conversa(rota, sistema, decls, max_saida)
    conversa.usuario(mensagem, imagens)
    if esquema is not None:
        return _laco(conversa, ctx, rota, nome, None, [], esquema, embrulhado, MAX_ERROS_ENTREGA + MAX_LEMBRETES + 2,
                     "    ")
    partes: list[str] = []
    for _ in range(MAX_CONTINUACOES + 1):
        if ctx.cancelado.is_set():
            raise Cancelado()
        try:
            passo = conversa.passo()
        except ErroProvedor as e:
            ctx.log(f"    ✗ {nome}: {str(e)[:300]}")
            raise
        ctx.somar(rota.provedor, rota.modelo, passo.uso, nome)
        partes.append(passo.texto)
        if not passo.cortado:
            break
        conversa.usuario("Sua resposta foi cortada no limite de tamanho. Continue exatamente de onde parou, sem repetir.")
    texto = "".join(partes).strip()
    ctx.log("    ✓ " + (" ".join(texto.split())[:200] or "(resposta vazia)"))
    return texto


def agente_avulso(nome: str, instrucoes: str, ferramentas: list[str], nivel: str = "julgamento",
                  visao: bool = False, max_turnos: int = registro.MAX_TURNOS_PADRAO) -> Agente:
    """Um papel sem arquivo em agentes/ (um teste, uma tarefa pontual de um fluxo)."""
    return Agente(nome=nome, descricao="", nivel=nivel, ferramentas=list(ferramentas), instrucoes=instrucoes,
                  visao=visao, max_turnos=max_turnos)
