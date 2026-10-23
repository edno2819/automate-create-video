"""Execuções do Claude Code em modo headless (claude -p), uma por etapa do pipeline.

A interface não fala com a API do Claude: ela roda o próprio Claude Code na raiz do projeto, que carrega o
CLAUDE.md, as skills e os subagentes de .claude/agents/, exatamente como na conversa. Cada execução roda
numa thread, lê o stream-json do processo e grava no banco um log legível (agentes disparados, comandos,
arquivos gravados), que a tela acompanha.

Permissões: sem ninguém no terminal para aprovar, as ferramentas precisam estar liberadas. O padrão é
acceptEdits com a lista FERRAMENTAS; WEB_PERMISSAO=bypassPermissions libera tudo (use só se confiar nos
dados que entram no pedido). WEB_MODELO troca o modelo da sessão principal.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

import banco

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ferramentas"))
import chaves  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
CLAUDE = os.environ.get("WEB_CLAUDE") or shutil.which("claude") or "claude"
PERMISSAO = os.environ.get("WEB_PERMISSAO", "acceptEdits")
MODELO = os.environ.get("WEB_MODELO")
FERRAMENTAS = [
    "Read", "Write", "Edit", "Glob", "Grep", "Agent", "Skill", "WebSearch", "WebFetch", "TodoWrite",
    "Bash(uv run:*)", "Bash(python3:*)", "Bash(cat:*)", "Bash(cat)", "Bash(ls:*)", "Bash(cp:*)", "Bash(mkdir:*)",
    "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(grep:*)", "Bash(find:*)", "Bash(jq:*)", "Bash(sed -n:*)",
    "Bash(ffmpeg:*)", "Bash(ffprobe:*)", "Bash(espeak-ng:*)", "Bash(date:*)", "Bash(echo:*)", "Bash(sort:*)",
    "Bash(uniq:*)", "Bash(tr:*)", "Bash(cut:*)", "Bash(basename:*)", "Bash(dirname:*)", "Bash(test:*)", "Bash(pwd)",
    "Bash(which:*)", "Bash(du:*)", "Bash(file:*)",
    "Bash(rtk:*)",  # o hook RTK, se instalado, reescreve cat/ls/grep como rtk ...
]
REGRAS = """Você está rodando sem ninguém no terminal, chamado pela interface web do projeto (web/).
Não use AskUserQuestion nem espere resposta: as decisões do usuário já foram tomadas na interface e estão
nos arquivos do projeto. Faça só a etapa pedida, salve as saídas nos arquivos de sempre e termine com um
resumo curto, em português, do que foi feito e do que o usuário precisa olhar. Se faltar algo que só o
usuário decide, pare e diga o que falta no resumo."""

_processos: dict[int, subprocess.Popen] = {}
_trava = threading.Lock()


def _relativo(caminho: str) -> str:
    try:
        return str(Path(caminho).resolve().relative_to(RAIZ))
    except (ValueError, OSError):
        return caminho


def _curto(texto: str, limite: int) -> str:
    texto = " ".join(str(texto).split())
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


def linha_de_ferramenta(nome: str, entrada: dict) -> str | None:
    if nome in ("Agent", "Task"):
        return f"▶ agente {entrada.get('subagent_type') or 'geral'}: {_curto(entrada.get('description', ''), 120)}"
    if nome == "Skill":
        return f"▶ skill {entrada.get('skill')}"
    if nome == "Bash":
        return f"$ {_curto(entrada.get('command', ''), 220)}"
    if nome in ("Write", "Edit", "NotebookEdit"):
        return f"✎ {_relativo(entrada.get('file_path', ''))}"
    if nome == "Read":
        return f"· lê {_relativo(entrada.get('file_path', ''))}"
    if nome == "WebSearch":
        return f"⌕ busca: {_curto(entrada.get('query', ''), 160)}"
    if nome == "WebFetch":
        return f"⌕ abre: {_curto(entrada.get('url', ''), 160)}"
    if nome in ("Glob", "Grep", "TodoWrite", "ToolSearch", "StructuredOutput"):
        return None
    return f"· {nome}"


def linhas_do_evento(ev: dict) -> list[str]:
    tipo = ev.get("type")
    recuo = "    " if ev.get("parent_tool_use_id") else ""
    if tipo == "assistant":
        linhas = []
        for parte in (ev.get("message") or {}).get("content") or []:
            if parte.get("type") == "text" and parte.get("text", "").strip():
                linhas.append(recuo + "› " + _curto(parte["text"], 160 if recuo else 700))
            elif parte.get("type") == "tool_use":
                if linha := linha_de_ferramenta(parte.get("name", ""), parte.get("input") or {}):
                    linhas.append(recuo + linha)
        return linhas
    if tipo == "result":
        custo = ev.get("total_cost_usd")
        minutos = (ev.get("duration_ms") or 0) / 60000
        negadas = [d.get("tool_name", "?") + (f" ({_curto(json.dumps(d.get('tool_input', {}), ensure_ascii=False), 120)})"
                                              if d.get("tool_input") else "") for d in ev.get("permission_denials") or []]
        linhas = [f"✗ ferramenta bloqueada pela permissão: {n}" for n in negadas[:10]]
        if ev.get("is_error") or ev.get("subtype") != "success":
            return linhas + [f"✗ terminou com erro ({ev.get('subtype')}): {_curto(ev.get('result') or '', 400)}"]
        return linhas + [f"✓ concluído em {minutos:.1f} min" + (f" · US$ {custo:.2f}" if custo else "")]
    return []


def iniciar(slug: str, etapa: str, prompt: str, esquema: dict | None = None,
            ao_terminar: Callable[[dict, dict], None] | None = None, modelo: str | None = None,
            chave_api: bool = False) -> int:
    """Dispara o Claude Code com o prompt da etapa. ao_terminar(execucao, evento_result) roda quando ele termina
    bem, antes de a execução ser marcada como ok; uma exceção ali marca a execução como erro. modelo vem da aba
    Conexões (senão WEB_MODELO); com chave_api, o Claude Code cobra pela chave da Anthropic cadastrada, e não
    pelo login. As outras chaves do .env vão no ambiente, para os scripts que o Claude Code rodar."""
    id_ = banco.criar_execucao(slug, etapa)
    cmd = [CLAUDE, "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--permission-mode", PERMISSAO, "--allowedTools", ",".join(FERRAMENTAS),
           "--append-system-prompt", REGRAS]
    if modelo or MODELO:
        cmd += ["--model", modelo or MODELO]
    if esquema:
        cmd += ["--json-schema", json.dumps(esquema, ensure_ascii=False)]
    threading.Thread(target=_rodar, args=(id_, cmd, ao_terminar, ambiente(chave_api)), daemon=True,
                     name=f"claude-{slug}-{etapa}").start()
    return id_


def ambiente(chave_api: bool) -> dict:
    env = {**os.environ, **chaves.ambiente(excluir=("anthropic",)), "CLAUDE_CODE_ENTRYPOINT": "web"}
    if chave_api:
        env["ANTHROPIC_API_KEY"] = chaves.ler("anthropic") or ""
    return env


def rodar_agente(nome: str, mensagem: str, log: Callable[[str], None], processos: set,
                 modelo: str | None = None, chave_api: bool = False) -> tuple[str, float]:
    """Roda um agente de .claude/agents/ direto (claude -p --agent), sem sessão principal: quem encadeia os
    agentes é o orquestrador em Python (web/orquestrador.py). O agente usa o modelo do próprio arquivo, a não ser
    que a aba Configurações escolha outro. Bloqueia até ele terminar; devolve o texto final e o custo."""
    cmd = [CLAUDE, "-p", mensagem, "--agent", nome, "--output-format", "stream-json", "--verbose",
           "--permission-mode", PERMISSAO, "--allowedTools", ",".join(FERRAMENTAS), "--append-system-prompt", REGRAS]
    if modelo:
        cmd += ["--model", modelo]
    proc = subprocess.Popen(cmd, cwd=RAIZ, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, bufsize=1, start_new_session=True, env=ambiente(chave_api))
    processos.add(proc)
    resultado: dict = {}
    try:
        for bruta in proc.stdout:
            try:
                ev = json.loads(bruta)
            except json.JSONDecodeError:
                if bruta.strip():
                    log("    " + _curto(bruta, 300))
                continue
            if ev.get("type") == "result":
                resultado = ev
            for linha in linhas_do_evento(ev):
                log("    " + linha)
        proc.wait()
    finally:
        processos.discard(proc)
    custo = resultado.get("total_cost_usd") or 0.0
    if proc.returncode != 0 or not resultado or resultado.get("is_error"):
        raise RuntimeError(f"o agente {nome} terminou com erro (código {proc.returncode}): "
                           f"{_curto(resultado.get('result') or 'sem resultado', 300)}")
    return resultado.get("result") or "", custo


def _rodar(id_: int, cmd: list[str], ao_terminar, env: dict) -> None:
    inicio = time.time()
    banco.anexar_log(id_, f"iniciado: claude -p (etapa {banco.execucao(id_)['etapa']})\n")
    resultado: dict = {}
    try:
        proc = subprocess.Popen(cmd, cwd=RAIZ, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, bufsize=1, start_new_session=True, env=env)
    except OSError as e:
        banco.anexar_log(id_, f"✗ não consegui rodar o Claude Code ({CLAUDE}): {e}\n")
        banco.atualizar_execucao(id_, status="erro", terminada_em=banco.agora(), resultado=str(e))
        return
    with _trava:
        _processos[id_] = proc
    for bruta in proc.stdout:
        bruta = bruta.strip()
        if not bruta:
            continue
        try:
            ev = json.loads(bruta)
        except json.JSONDecodeError:
            banco.anexar_log(id_, f"  {_curto(bruta, 300)}\n")
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            banco.atualizar_execucao(id_, sessao=ev.get("session_id"))
        if ev.get("type") == "result":
            resultado = ev
        if linhas := linhas_do_evento(ev):
            banco.anexar_log(id_, "\n".join(linhas) + "\n")
    proc.wait()
    with _trava:
        _processos.pop(id_, None)
    atual = banco.execucao(id_)
    if atual["status"] == "cancelada":
        return
    ok = proc.returncode == 0 and resultado and not resultado.get("is_error")
    campos = {"terminada_em": banco.agora(), "custo_usd": resultado.get("total_cost_usd"),
              "resultado": resultado.get("result") or ""}
    if ok and ao_terminar:
        try:
            ao_terminar(atual, resultado)
        except Exception as e:  # noqa: BLE001 - qualquer falha do pós-processamento vai para o log
            banco.anexar_log(id_, f"✗ pós-processamento: {e}\n{traceback.format_exc(limit=3)}")
            ok = False
    if not resultado:
        banco.anexar_log(id_, f"✗ o Claude Code saiu com código {proc.returncode} sem resultado "
                              f"({(time.time() - inicio) / 60:.1f} min)\n")
    banco.atualizar_execucao(id_, status="ok" if ok else "erro", **campos)


def cancelar(id_: int) -> bool:
    with _trava:
        proc = _processos.get(id_)
    if proc is None:
        return False
    banco.atualizar_execucao(id_, status="cancelada", terminada_em=banco.agora())
    banco.anexar_log(id_, "■ cancelado pelo usuário\n")
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return True


def rodando(slug: str) -> list[int]:
    with _trava:
        ids = list(_processos)
    return [i for i in ids if (e := banco.execucao(i)) and e["slug"] == slug]
