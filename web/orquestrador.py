"""Orquestrador próprio: as fases da skill novo-video em Python, para as etapas que rodam com GPT, Gemini ou
Claude pela API (escolha em Configurações > Modelos das etapas).

No Claude Code, uma sessão segue a skill e dispara os subagentes. Aqui a sequência é fixa, a mesma da skill:
cada fase chama os agentes de .claude/agents/ pelo motor (web/motor.py), com o modelo escolhido para a etapa,
passa os caminhos que eles esperam e roda os scripts determinísticos entre eles. As saídas são os mesmos
arquivos de projetos/<slug>/, então dá para alternar entre o Claude Code e o motor de uma etapa para outra.

Cada execução roda numa thread, grava o log no banco no mesmo formato das execuções do Claude Code e soma o
custo pelo uso de tokens de cada chamada (tabela de biblioteca/precos.json).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import banco
import claude_code
import etapas
import motor
from motor import Contexto, Rota

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "ferramentas"))
import chaves  # noqa: E402

SCRIPTS = RAIZ / "skills" / "produzir-video" / "scripts"
ORQUESTRADOR = """Você é o orquestrador do pipeline de vídeos deste repositório. Você completa e confere o pedido de
vídeo, seguindo a skill skills/novo-video/SKILL.md (leia-a). Leia os arquivos que a tarefa citar antes de decidir."""

# Etapas que o orquestrador em Python conduz também no Claude Code, agente a agente (claude -p --agent), sem
# sessão principal: o ciclo é fixo, então a sessão que só disparava os agentes era gasto sem ganho. As outras
# etapas, no Claude Code, continuam com uma sessão que segue a skill.
AGENTE_A_AGENTE = {"roteiro", "avaliar"}
CLAUDE_CODE = ("claude-code", "claude-code-api")

_ativos: dict[int, tuple[str, Contexto]] = {}
_trava = threading.Lock()


# ---------------------------------------------------------------- execução


def iniciar(slug: str, etapa: str, rota: Rota, fluxo: Callable[[Contexto], object],
            ao_terminar: Callable[[dict, dict], None] | None = None) -> int:
    """Roda o fluxo numa thread. O fluxo devolve o resumo (texto) ou, nas sugestões, o dicionário pedido."""
    id_ = banco.criar_execucao(slug, etapa)
    ctx = Contexto(log=lambda linha: banco.anexar_log(id_, linha + "\n"))
    with _trava:
        _ativos[id_] = (slug, ctx)
    threading.Thread(target=_rodar, args=(id_, etapa, rota, ctx, fluxo, ao_terminar), daemon=True,
                     name=f"motor-{slug}-{etapa}").start()
    return id_


def _rodar(id_, etapa, rota, ctx, fluxo, ao_terminar):
    ctx.log(f"iniciado: orquestrador (etapa {etapa}, agentes no Claude Code"
            + (f", modelo {rota.modelo})" if rota.modelo else ", modelo de cada agente)")
            if rota.provedor in CLAUDE_CODE else f"iniciado: motor de agentes (etapa {etapa}, {rota.provedor} · {rota.modelo})")
    status, resumo, estruturado = "erro", "", None
    try:
        saida = fluxo(ctx)
        estruturado = saida if isinstance(saida, dict) else None
        resumo = json.dumps(saida, ensure_ascii=False) if estruturado else str(saida or "")
        if ao_terminar:
            ao_terminar(banco.execucao(id_), {"result": resumo, "structured_output": estruturado})
        status = "ok"
        ctx.log(f"✓ concluído · US$ {ctx.uso_usd:.2f}" + (f" (sem preço para {', '.join(sorted(ctx.sem_preco))})" if ctx.sem_preco else ""))
    except motor.Cancelado:
        status = "cancelada"
        ctx.log("■ cancelado pelo usuário")
    except Exception as e:  # noqa: BLE001 - qualquer falha vira erro da execução, com o motivo no log
        resumo = f"{type(e).__name__}: {e}"
        ctx.log(f"✗ terminou com erro: {resumo}\n{traceback.format_exc(limit=4)}")
    finally:
        with _trava:
            _ativos.pop(id_, None)
        shutil.rmtree(ctx.tmp, ignore_errors=True)
        if banco.execucao(id_)["status"] != "cancelada" or status == "cancelada":
            banco.atualizar_execucao(id_, status=status, terminada_em=banco.agora(), resultado=resumo,
                                     custo_usd=round(ctx.uso_usd, 6))


def cancelar(id_: int) -> bool:
    with _trava:
        ativo = _ativos.get(id_)
    if not ativo:
        return False
    banco.atualizar_execucao(id_, status="cancelada", terminada_em=banco.agora())
    ativo[1].cancelar()
    return True


def rodando(slug: str) -> list[int]:
    with _trava:
        return [i for i, (s, _) in _ativos.items() if s == slug]


# ---------------------------------------------------------------- utilidades dos fluxos


def agente(ctx: Contexto, rota: Rota, nome: str, mensagem: str) -> str:
    if rota.provedor in CLAUDE_CODE:
        ctx.log(f"▶ agente {nome} (Claude Code{', ' + rota.modelo if rota.modelo else ''})")
        try:
            texto, custo = claude_code.rodar_agente(nome, mensagem, ctx.log, ctx.processos, rota.modelo or None,
                                                    chave_api=rota.provedor == "claude-code-api")
        except RuntimeError:
            if ctx.cancelado.is_set():
                raise motor.Cancelado()
            raise
        with ctx.trava:
            ctx.uso_usd += custo
        return texto
    ctx.log(f"▶ agente {nome} ({rota.modelo})")
    return motor.rodar_agente(nome, mensagem, rota, ctx)


def em_paralelo(ctx: Contexto, tarefas: dict[str, Callable[[], str]]) -> dict[str, tuple[bool, str]]:
    """Roda os agentes ao mesmo tempo, como a skill pede na pesquisa e nos cortes. Uma falha não derruba os outros."""
    def um(nome, f):
        try:
            return nome, (True, f())
        except motor.Cancelado:
            raise
        except Exception as e:  # noqa: BLE001
            ctx.log(f"✗ {nome}: {e}")
            return nome, (False, str(e))
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(lambda kv: um(*kv), tarefas.items()))


def rodar_script(ctx: Contexto, cmd: list[str], rotulo: str | None = None) -> tuple[int, str]:
    """Um script do pipeline, com a saída no log, linha a linha."""
    ctx.log(f"$ {rotulo or ' '.join(str(c) for c in cmd)}")
    proc = subprocess.Popen([str(c) for c in cmd], cwd=RAIZ, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True,
                            env={**os.environ, **chaves.ambiente(excluir=("anthropic",))})
    ctx.processos.add(proc)
    linhas = []
    try:
        for linha in proc.stdout:
            linhas.append(linha.rstrip())
            if linha.strip():
                ctx.log("    " + linha.rstrip()[:300])
        proc.wait()
    finally:
        ctx.processos.discard(proc)
    if ctx.cancelado.is_set():
        raise motor.Cancelado()
    return proc.returncode, "\n".join(linhas)


def ler_json(caminho: Path):
    return etapas.ler_json(caminho)


# ---------------------------------------------------------------- fluxos (as fases da skill novo-video)


def sugestoes(slug: str, rota: Rota, vazios: list[str], modo_voz: str | None) -> Callable[[Contexto], dict]:
    def fluxo(ctx):
        prompt = (etapas.prompt_sugestoes(slug, vazios, modo_voz)
                  + "\n\nTermine respondendo só com o JSON, sem texto antes nem depois, neste formato (JSON Schema):\n"
                  + json.dumps(etapas.ESQUEMA_SUGESTOES, ensure_ascii=False))
        papel = motor.agente_avulso("orquestrador", ORQUESTRADOR, ["Read", "Glob", "Grep", "Bash"])
        ctx.log(f"▶ orquestrador ({rota.modelo})")
        return motor.json_do_texto(motor.rodar_agente(papel, prompt, rota, ctx))
    return fluxo


def pesquisa(slug: str, rota: Rota) -> Callable[[Contexto], str]:
    def fluxo(ctx):
        b = etapas.brief(slug) or {}
        pasta = f"projetos/{slug}"
        tarefas: dict[str, Callable[[], str]] = {}
        sem_esqueleto = [r for r in b.get("referencias") or [] if isinstance(r, dict) and r.get("url") and not r.get("esqueleto")]
        for i, r in enumerate(sem_esqueleto[:3], 1):
            tarefas[f"analista-referencias {i}"] = lambda r=r: agente(ctx, rota, "analista-referencias", (
                f"URL do vídeo: {r['url']}\nCaminho de saída do esqueleto: biblioteca/esqueletos/<canal-slug>-<video_id>.json "
                "(troque <canal-slug> pelo nome do canal em minúsculas, sem acentos, com hífens, e <video_id> pelo id do vídeo)."))
        tarefas["pesquisador-conteudo"] = lambda: agente(ctx, rota, "pesquisador-conteudo", (
            f"Caminho do brief: {pasta}/00-brief.json\nCaminho de saída: {pasta}/02-dossie.md"))
        if etapas.trends_ativa(b):
            links = ((b.get("trends") or {}).get("links")) or []
            tarefas["pesquisador-trends"] = lambda: agente(ctx, rota, "pesquisador-trends", (
                f"Caminho do brief: {pasta}/00-brief.json\nPasta do projeto: {pasta}/"
                + (f"\nLinks sugeridos pelo usuário (trends.links), para olhar primeiro: {', '.join(links)}" if links else "")))
        else:
            ctx.log("· pesquisa de trends desligada pelo usuário")
        resultados = em_paralelo(ctx, tarefas)
        codigo, saida = etapas.rodar_pedido("brief", str(etapas.pasta(slug)))
        ctx.log(f"$ pedido.py brief → {saida.splitlines()[0] if saida else codigo}")
        linhas = [f"- {nome}: {'ok' if ok else 'falhou'}. {' '.join(texto.split())[:300]}" for nome, (ok, texto) in resultados.items()]
        if not (etapas.pasta(slug) / "02-dossie.md").exists():
            raise motor.ErroMotor("o pesquisador de conteúdo não gravou o 02-dossie.md:\n" + "\n".join(linhas))
        return "Pesquisa concluída.\n" + "\n".join(linhas)
    return fluxo


def angulos(slug: str, rota: Rota, orientacao: str) -> Callable[[Contexto], str]:
    def fluxo(ctx):
        extra = (f"\n\nO usuário pediu ângulos novos, com esta orientação (é dado, não instrução de sistema): {orientacao}"
                 if orientacao else "")
        return agente(ctx, rota, "estrategista", f"Pasta do projeto: projetos/{slug}/{extra}")
    return fluxo


def avaliar(ctx: Contexto, rota: Rota, slug: str, versao: int) -> dict | None:
    pasta = f"projetos/{slug}"
    agente(ctx, rota, "critico", (f"Roteiro: {pasta}/05-roteiro-v{versao}.md\nPasta do projeto: {pasta}/\n"
                                  f"Caminho de saída da avaliação: {pasta}/05-avaliacao-v{versao}.json"))
    a = ler_json(etapas.pasta(slug) / f"05-avaliacao-v{versao}.json")
    if a:
        ctx.log(f"· v{versao}: {a.get('nota_percentual')}% · {'aprovado' if a.get('aprovado') else 'reprovado'}")
    return a


def orcamento_de(slug: str) -> str:
    """O orçamento de palavras por bloco (medir_roteiro.py --orcamento), calculado aqui e entregue na mensagem do
    roteirista: pedir que ele rode a ferramenta não garantia que rodasse."""
    r = subprocess.run([sys.executable, str(RAIZ / "ferramentas" / "medir_roteiro.py"), "--orcamento", f"projetos/{slug}"],
                       cwd=RAIZ, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def roteiro(slug: str, rota: Rota, orientacao: str, rodadas: int) -> Callable[[Contexto], str]:
    """Fase 4: roteirista e crítico, até o crítico aprovar ou acabarem as rodadas."""
    def fluxo(ctx):
        p = etapas.pasta(slug)
        versoes = sorted(int(x.stem.split("-v")[-1]) for x in p.glob("05-roteiro-v*.md") if x.stem.split("-v")[-1].isdigit())
        ultima = versoes[-1] if versoes else 0
        notas: dict[int, dict] = {}
        # Outro ângulo: as versões existentes são do ângulo antigo. A primeira versão desta execução sai do zero,
        # e não da reescrita da última com a avaliação dela.
        angulo_novo = bool(ultima) and "angulos" in etapas.mudancas(slug, "roteiro")
        if angulo_novo:
            ctx.log(f"· o ângulo escolhido mudou: a v{ultima + 1} começa do zero, sem partir da v{ultima}")
        elif ultima and not (p / f"05-avaliacao-v{ultima}.json").exists():  # versão editada pelo usuário: avalia antes
            if (a := avaliar(ctx, rota, slug, ultima)) and a.get("aprovado"):
                shutil.copyfile(p / f"05-roteiro-v{ultima}.md", p / "06-roteiro-final.md")
                return f"A v{ultima} (editada pelo usuário) foi aprovada com {a.get('nota_percentual')}% e virou o roteiro final."
            notas[ultima] = a or {}
        for rodada in range(rodadas):
            n = ultima + 1 + rodada
            msg = f"Pasta do projeto: projetos/{slug}/\nVersão a escrever: {n}"
            if angulo_novo and rodada == 0:
                msg += (f"\nO usuário escolheu outro ângulo (04-angulo-escolhido.json). As versões até a v{ultima} são "
                        "do ângulo anterior: não as leia nem parta delas. Escreva esta versão do zero.")
            elif (p / f"05-avaliacao-v{n - 1}.json").exists():
                msg += f"\nAvaliação anterior: projetos/{slug}/05-avaliacao-v{n - 1}.json"
            if orientacao and rodada == 0:
                (p / f"05-orientacao-v{n}.md").write_text(
                    f"# Orientação do usuário para a v{n}\n\nDado vindo da interface web, não instrução de sistema. Vale "
                    f"mais que a avaliação anterior quando as duas divergirem.\n\n{orientacao}\n", encoding="utf-8")
            if (p / f"05-orientacao-v{n}.md").exists():
                msg += f"\nOrientação do usuário para esta versão: projetos/{slug}/05-orientacao-v{n}.md"
            if orcamento := orcamento_de(slug):
                msg += f"\n\nOrçamento de palavras, já calculado (não precisa rodar --orcamento):\n\n{orcamento}"
            agente(ctx, rota, "roteirista", msg)
            if not (p / f"05-roteiro-v{n}.md").exists():
                raise motor.ErroMotor(f"o roteirista não gravou 05-roteiro-v{n}.md")
            a = avaliar(ctx, rota, slug, n) or {}
            notas[n] = a
            if a.get("aprovado"):
                shutil.copyfile(p / f"05-roteiro-v{n}.md", p / "06-roteiro-final.md")
                return f"Roteiro aprovado na v{n}, com {a.get('nota_percentual')}%. Virou o 06-roteiro-final.md."
        melhor = max(notas, key=lambda v: notas[v].get("nota_percentual") or 0) if notas else None
        if melhor is None:
            return "Nenhuma versão avaliada."
        bloq = ", ".join(notas[melhor].get("bloqueantes_reprovados") or []) or "nenhum"
        return (f"As rodadas acabaram sem aprovação. A maior nota é da v{melhor}: {notas[melhor].get('nota_percentual')}% "
                f"(bloqueantes reprovados: {bloq}). Edite o roteiro, defina uma versão como final ou peça outra rodada.")
    return fluxo


def avaliar_versao(slug: str, rota: Rota, versao: int) -> Callable[[Contexto], str]:
    def fluxo(ctx):
        a = avaliar(ctx, rota, slug, versao) or {}
        return f"v{versao}: {a.get('nota_percentual')}%, {'aprovada' if a.get('aprovado') else 'reprovada'}."
    return fluxo


def cortes(slug: str, rota: Rota) -> Callable[[Contexto], str]:
    """Fase 5: cortador, um crítico por corte em paralelo e uma única rodada a mais para os reprovados."""
    def fluxo(ctx):
        pasta = f"projetos/{slug}"
        agente(ctx, rota, "cortador", f"Pasta do projeto: {pasta}/" + etapas.nota_de_refazer(slug, "cortes"))
        arquivos = sorted((etapas.pasta(slug) / "07-cortes").glob("corte-*.md"))

        def criticar(arq: Path):
            saida = f"{pasta}/07-cortes/avaliacao-{arq.stem}.json"
            return lambda: agente(ctx, rota, "critico", f"Roteiro: {pasta}/07-cortes/{arq.name}\nPasta do projeto: {pasta}/\n"
                                                        f"Caminho de saída da avaliação: {saida}")
        em_paralelo(ctx, {arq.stem: criticar(arq) for arq in arquivos})
        reprovados = [f"{pasta}/07-cortes/avaliacao-{a.stem}.json" for a in arquivos
                      if not (ler_json(a.with_name(f"avaliacao-{a.stem}.json")) or {}).get("aprovado")]
        if reprovados:
            agente(ctx, rota, "cortador", f"Pasta do projeto: {pasta}/\nReescreva os cortes reprovados a partir destas "
                                          f"avaliações: {', '.join(reprovados)}")
        return f"{len(arquivos)} cortes; {len(reprovados)} reprovado(s) na primeira avaliação" + (
            " e reescrito(s) uma vez (sem nova avaliação, como pede a skill)." if reprovados else ".")
    return fluxo


def visual(slug: str, rota: Rota) -> Callable[[Contexto], str]:
    """Fase 6: diretor visual e a montagem das cenas, devolvendo os erros ao diretor até três vezes."""
    def fluxo(ctx):
        pasta = f"projetos/{slug}"
        agente(ctx, rota, "diretor-visual", f"Pasta do projeto: {pasta}/" + etapas.nota_de_refazer(slug, "visual"))
        for tentativa in range(3):
            codigo, saida = rodar_script(ctx, [sys.executable, RAIZ / "ferramentas" / "cenas.py", "montar", pasta],
                                         f"python3 ferramentas/cenas.py montar {pasta}")
            if codigo == 0:
                return f"Direção visual pronta: {pasta}/08-storyboard.md.\n" + "\n".join(saida.splitlines()[-6:])
            if tentativa < 2:
                agente(ctx, rota, "diretor-visual", f"Pasta do projeto: {pasta}/\nA montagem das cenas acusou estes erros. "
                                                    f"Corrija a bíblia, os arquivos de 08-cenas/ e o 08-thumbnail.json e não mexa no resto:\n{motor.encurtar(saida, 8000)}")
        raise motor.ErroMotor("a montagem das cenas continuou com erro depois de três tentativas:\n" + saida[-2000:])
    return fluxo


def publicacao(slug: str, rota: Rota) -> Callable[[Contexto], str]:
    def fluxo(ctx):
        return agente(ctx, rota, "publicador", f"Pasta do projeto: projetos/{slug}/")
    return fluxo


def producao(slug: str, rota: Rota, teste: bool) -> Callable[[Contexto], str]:
    """Fase 7, pelos scripts da skill produzir-video, na ordem dela, e o publicador no fim. Sem LLM no meio: a
    revisão das imagens e a conferência do vídeo ficam com o usuário, na tela."""
    def fluxo(ctx):
        pasta = etapas.pasta(slug)
        rel = f"projetos/{slug}"
        b = etapas.brief(slug) or {}
        avisos = []
        cenas = (ler_json(pasta / "08-cenas.json") or {}).get("cenas", [])
        if not teste and (b.get("visual") or {}).get("imagens_da_internet") and any(c.get("fonte") == "internet" for c in cenas):
            rodar_script(ctx, [sys.executable, SCRIPTS / "buscar_imagens.py", rel], f"python3 buscar_imagens.py {rel}")
        gerar = [sys.executable, SCRIPTS / "gerar_imagens.py", rel] + (["--provisorio"] if teste else [])
        codigo, _ = rodar_script(ctx, gerar, f"python3 gerar_imagens.py {rel}" + (" --provisorio" if teste else ""))
        if codigo == 3:
            avisos.append("sem cota de imagem no Google (faturamento): o vídeo saiu com quadros provisórios")
            rodar_script(ctx, [sys.executable, SCRIPTS / "gerar_imagens.py", rel, "--provisorio"])
        elif codigo == 1:
            avisos.append("algumas imagens falharam (veja o log): o vídeo usa quadros provisórios no lugar delas")
        if (b.get("narracao") or {}).get("ativa", True) is not False:
            narrar = [sys.executable, SCRIPTS / "narrar.py", rel] + (["--fornecedor", "espeak"] if teste else [])
            codigo, _ = rodar_script(ctx, narrar, f"python3 narrar.py {rel}" + (" --fornecedor espeak" if teste else ""))
            if codigo != 0:
                raise motor.ErroMotor("a narração falhou (veja o log)")
            uv = shutil.which("uv") or "uv"
            codigo, _ = rodar_script(ctx, [uv, "run", "--script", SCRIPTS / "alinhar.py", rel], f"uv run --script alinhar.py {rel}")
            if codigo == 1:
                avisos.append("o alinhamento achou trechos em que a voz não disse o roteiro (veja o log); refaça esses blocos")
            elif codigo != 0:
                avisos.append("o alinhamento falhou: a montagem usa os tempos estimados")
        codigo, _ = rodar_script(ctx, [sys.executable, SCRIPTS / "montar_video.py", rel], f"python3 montar_video.py {rel}")
        if codigo != 0:
            raise motor.ErroMotor("a montagem do vídeo falhou (veja o log)")
        publicacao(slug, rota)(ctx)
        videos = sorted((pasta / "10-video").glob("*.mp4"))
        return (f"Vídeo pronto: {videos[0].relative_to(RAIZ) if videos else '(não achei o mp4)'}."
                + ("\nAvisos:\n" + "\n".join(f"- {a}" for a in avisos) if avisos else ""))
    return fluxo
