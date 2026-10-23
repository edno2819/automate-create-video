#!/usr/bin/env python3
"""Mede um roteiro no formato de biblioteca/formato-roteiro.md.

Conta palavras por bloco, estima os tempos pela taxa de palavras por minuto e aponta problemas
objetivos: trechos longos sem re-hook, trechos sem loop aberto, frases proibidas no início, conectores
"e depois", fatos a verificar e a posição da virada. Também separa as falas dos personagens ([FALA: Nome])
das do narrador e confere se cada personagem está no pedido. A avaliação de qualidade fica com o crítico;
este script só fornece os números.

Uso:
    python3 ferramentas/medir_roteiro.py projetos/<slug>/05-roteiro-v1.md
    python3 ferramentas/medir_roteiro.py <roteiro.md> --json saida.json --ppm 150
    python3 ferramentas/medir_roteiro.py <roteiro.md> --narracao   # só o texto ouvido, para a leitura fria
    python3 ferramentas/medir_roteiro.py --orcamento projetos/<slug>   # palavras por bloco, antes de escrever

Só usa a biblioteca padrão.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from brief import ppm_do_brief

PPM_PADRAO = 150
MAX_INTERVALO_REHOOK_S = 120
MIN_INTERVALO_REHOOK_S = 60
SEGUNDOS_POR_PAUSA = 1.0

FRASES_PROIBIDAS_INICIO = [
    "fala galera", "fala pessoal", "e ai pessoal", "e ai galera", "ola pessoal", "oi pessoal",
    "sejam bem-vindos", "bem-vindos ao canal", "bem vindos ao canal", "nesse video", "neste video",
    "no video de hoje", "hoje eu vou", "hoje vamos", "antes de comecar", "se inscreve", "se inscreva",
    "inscreva-se", "deixa o like", "deixe o like", "ativa o sininho", "ative o sininho",
]

MARCADOR = re.compile(r"\[(RE-HOOK|PAUSA|VERIFICAR|VISUAL|FALA)(?::\s*([^\]]*))?\]", re.IGNORECASE)
CABECALHO_BLOCO = re.compile(r"^##\s*\[(B\d+)\]\s*(.*)$")
CABECALHO_SECAO = re.compile(r"^##\s+(.*)$")
LOOP_DECLARADO = re.compile(r"^\s*[-*]\s*(L\d+)\s*:\s*(.+)$")
ID_LOOP = re.compile(r"L\d+")
MAX_PALAVRAS_FALA = 40  # fala de personagem mais longa que isso pesa para uma voz só e para o ouvinte


def sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn").lower()


def eh_palavra(token: str) -> bool:
    return any(c.isalnum() for c in token)


def fmt_tempo(segundos: float) -> str:
    segundos = max(0, int(round(segundos)))
    return f"{segundos // 60:02d}:{segundos % 60:02d}"


def parse_tempo(valor: str) -> float | None:
    m = re.fullmatch(r"\s*(\d+):(\d{1,2})\s*", valor or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


@dataclass
class Fala:
    """Um trecho contínuo do bloco dito por uma voz: um personagem ou, sem [FALA], o narrador."""
    personagem: str | None  # None: o narrador
    direcao: str            # como dizer, de [FALA: Nome, direção] ("chorando"); pode ser vazia
    inicio: int             # índice da primeira palavra no bloco
    fim: int                # índice depois da última


@dataclass
class Bloco:
    id: str
    titulo: str
    meta: dict[str, str] = field(default_factory=dict)
    tokens: list[str] = field(default_factory=list)  # palavras narradas, em ordem
    rehooks: list[int] = field(default_factory=list)  # índice da palavra (relativo ao bloco) de cada [RE-HOOK]
    pausas: list[int] = field(default_factory=list)  # índice da palavra de cada [PAUSA]
    verificar: list[str] = field(default_factory=list)
    visuais: list[str] = field(default_factory=list)
    mudancas: list[tuple[int, str | None, str]] = field(default_factory=list)  # (palavra, quem fala, direção)
    falas: list[Fala] = field(default_factory=list)  # o bloco inteiro, dividido por quem fala
    ouvido: list[str] = field(default_factory=list)  # cada parágrafo como o espectador ouve, sem marcadores
    inicio_s: float = 0.0
    fim_s: float = 0.0

    def loops(self, campo: str) -> list[str]:
        return ID_LOOP.findall(self.meta.get(campo, ""))


def ler_frontmatter(linhas: list[str]) -> tuple[dict[str, object], int]:
    if not linhas or linhas[0].strip() != "---":
        return {}, 0
    dados: dict[str, object] = {}
    for i in range(1, len(linhas)):
        linha = linhas[i]
        if linha.strip() == "---":
            return dados, i + 1
        linha = re.sub(r"\s+#.*$", "", linha)  # comentário no fim da linha
        if ":" not in linha:
            continue
        chave, valor = linha.split(":", 1)
        valor = valor.strip()
        if valor.startswith("[") and valor.endswith("]"):
            dados[chave.strip()] = [v.strip().strip("\"'") for v in valor[1:-1].split(",") if v.strip()]
        else:
            dados[chave.strip()] = valor.strip("\"'")
    return dados, 0


def ler_meta(linha: str) -> dict[str, str]:
    meta = {}
    for parte in re.split(r"\s*[·|]\s*", linha.lstrip(">").strip()):
        if ":" in parte:
            chave, valor = parte.split(":", 1)
            meta[sem_acento(chave.strip())] = valor.strip()
    return meta


def tokenizar_narracao(texto: str, bloco: Bloco) -> None:
    """Acrescenta ao bloco as palavras do texto, registrando a posição dos marcadores."""
    pos = 0
    for m in MARCADOR.finditer(texto):
        bloco.tokens.extend(t for t in texto[pos:m.start()].split() if eh_palavra(t))
        tipo, conteudo = m.group(1).upper(), (m.group(2) or "").strip()
        if tipo == "RE-HOOK":
            bloco.rehooks.append(len(bloco.tokens))
        elif tipo == "PAUSA":
            bloco.pausas.append(len(bloco.tokens))
        elif tipo == "VERIFICAR":
            bloco.verificar.append(conteudo)
        elif tipo == "VISUAL":
            bloco.visuais.append(conteudo)
        elif tipo == "FALA":
            nome, _, direcao = conteudo.partition(",")
            nome = nome.strip()
            bloco.mudancas.append((len(bloco.tokens), None if sem_acento(nome) in ("", "narrador") else nome, direcao.strip()))
        pos = m.end()
    bloco.tokens.extend(t for t in texto[pos:].split() if eh_palavra(t))


def montar_falas(b: Bloco) -> list[Fala]:
    """Divide o bloco por quem fala. Falas seguidas da mesma voz, com a mesma direção, viram uma só."""
    falas = [Fala(None, "", 0, 0)]
    for pos, personagem, direcao in b.mudancas:
        falas[-1].fim = pos
        falas.append(Fala(personagem, direcao, pos, pos))
    falas[-1].fim = len(b.tokens)
    unidas: list[Fala] = []
    for f in falas:
        if f.inicio == f.fim:
            continue
        if unidas and (unidas[-1].personagem, unidas[-1].direcao, unidas[-1].fim) == (f.personagem, f.direcao, f.inicio):
            unidas[-1].fim = f.fim
        else:
            unidas.append(f)
    return unidas or [Fala(None, "", 0, len(b.tokens))]


def ler_roteiro(caminho: Path) -> tuple[dict[str, object], list[Bloco], dict[str, str]]:
    linhas = caminho.read_text(encoding="utf-8").splitlines()
    front, inicio = ler_frontmatter(linhas)
    blocos: list[Bloco] = []
    loops_declarados: dict[str, str] = {}
    atual: Bloco | None = None
    secao = ""
    lendo_meta = False
    for linha in linhas[inicio:]:
        if m := CABECALHO_BLOCO.match(linha):
            atual = Bloco(id=m.group(1), titulo=m.group(2).strip())
            blocos.append(atual)
            secao, lendo_meta = "bloco", True
            continue
        if m := CABECALHO_SECAO.match(linha):
            atual, secao, lendo_meta = None, sem_acento(m.group(1).strip()), False
            continue
        if linha.startswith("# "):  # título do documento
            continue
        if secao == "loops":
            if m := LOOP_DECLARADO.match(linha):
                loops_declarados[m.group(1)] = m.group(2).strip()
            continue
        if atual is None:
            continue
        if linha.startswith(">"):
            if lendo_meta:
                atual.meta.update(ler_meta(linha))
            continue
        if linha.strip():
            lendo_meta = False
            antes, n_mudancas = len(atual.tokens), len(atual.mudancas)
            tokenizar_narracao(linha, atual)
            if falado := " ".join(atual.tokens[antes:]):
                quem = next((p for _, p, _ in atual.mudancas[n_mudancas:] if p), None)
                atual.ouvido.append(f"{quem}: {falado}" if quem else falado)
            if atual.mudancas and atual.mudancas[-1][1] is not None:
                atual.mudancas.append((len(atual.tokens), None, ""))  # a [FALA] vale até o fim da linha
    for b in blocos:
        b.falas = montar_falas(b)
    return front, blocos, loops_declarados


def personagens_do_brief(roteiro: Path) -> set[str] | None:
    """Nomes (sem acento, minúsculos) dos personagens do pedido, procurando 00-brief.json na pasta do
    roteiro e na de cima (os cortes ficam em 07-cortes/). None se não houver brief."""
    for pasta in (roteiro.parent, roteiro.parent.parent):
        if (pasta / "00-brief.json").exists():
            brief = json.loads((pasta / "00-brief.json").read_text(encoding="utf-8"))
            return {sem_acento(p.get("nome", "")).strip() for p in brief.get("personagens") or []}
    return None


def ppm_do_projeto(roteiro: Path) -> int | None:
    """narracao.palavras_por_minuto do brief do projeto do roteiro (os cortes ficam em 07-cortes/)."""
    for pasta in (roteiro.parent, roteiro.parent.parent):
        if (pasta / "00-brief.json").exists():
            return ppm_do_brief(json.loads((pasta / "00-brief.json").read_text(encoding="utf-8")))
    return None


def medir(caminho: Path, ppm_cli: int | None) -> dict[str, object]:
    front, blocos, loops_declarados = ler_roteiro(caminho)
    if not blocos:
        raise ValueError("nenhum bloco encontrado (cabeçalhos devem ser '## [B01] Título')")

    formato = str(front.get("formato", "longo")).lower()
    ppm = ppm_cli or ppm_do_projeto(caminho) or int(front.get("ppm") or PPM_PADRAO)
    seg_por_palavra = 60.0 / ppm
    alertas: list[dict[str, str]] = []

    def alertar(nivel: str, codigo: str, msg: str) -> None:
        alertas.append({"nivel": nivel, "codigo": codigo, "mensagem": msg})

    # Tempos: cada palavra dura 60/ppm s e cada [PAUSA] soma 1 s.
    t = 0.0
    todas: list[str] = []
    pontos_rehook: list[tuple[float, str]] = []
    for b in blocos:
        b.inicio_s = t
        for idx in b.rehooks:
            pontos_rehook.append((t + idx * seg_por_palavra, b.id))
        t += len(b.tokens) * seg_por_palavra + len(b.pausas) * SEGUNDOS_POR_PAUSA
        b.fim_s = t
        todas.extend(b.tokens)
    total_s = t

    # Duração contra a meta.
    alvo_s = parse_tempo(str(front.get("duracao_alvo", "")))
    if alvo_s:
        desvio = (total_s - alvo_s) / alvo_s * 100
        if abs(desvio) > 15:
            alertar("aviso", "duracao", f"duração estimada {fmt_tempo(total_s)} está {desvio:+.0f}% da meta {fmt_tempo(alvo_s)}")
    if formato == "short":
        if total_s > 180:
            alertar("erro", "duracao", f"short com {fmt_tempo(total_s)}: o limite do YouTube Shorts é 3 min")
        elif total_s > 60:
            alertar("aviso", "duracao", f"short com {fmt_tempo(total_s)}: o ideal é entre 30 e 60 s")

    # Trechos iniciais.
    n_gancho = round(ppm * (2 if formato == "short" else 5) / 60)
    n_30s = round(ppm * 30 / 60)
    trecho_gancho = " ".join(todas[:n_gancho])
    trecho_30s = " ".join(todas[:n_30s])
    inicio_norm = sem_acento(trecho_30s)
    for frase in FRASES_PROIBIDAS_INICIO:
        if frase in inicio_norm:
            alertar("erro", "inicio", f"frase proibida nos primeiros 30 s: '{frase}'")
    if formato == "short" and not blocos[0].visuais:
        alertar("erro", "visual", "short sem [VISUAL: ...] no B01: o gancho precisa ser verbal e visual")

    # Intervalos entre re-hooks (só no longo). O início do vídeo conta como o gancho.
    intervalos = []
    if formato == "longo":
        marcos = [(0.0, blocos[0].id)] + pontos_rehook + [(total_s, blocos[-1].id)]
        for (ta, ba), (tb, bb) in zip(marcos, marcos[1:]):
            gap = tb - ta
            intervalos.append({"de": fmt_tempo(ta), "ate": fmt_tempo(tb), "segundos": round(gap), "blocos": f"{ba}–{bb}"})
            if gap > MAX_INTERVALO_REHOOK_S:
                alertar("erro", "re-hook", f"{round(gap)} s sem re-hook entre {fmt_tempo(ta)} e {fmt_tempo(tb)} ({ba}–{bb}); máximo {MAX_INTERVALO_REHOOK_S} s")
        if not pontos_rehook:
            alertar("erro", "re-hook", "nenhum [RE-HOOK] marcado no roteiro")

    # Mapa de loops por bloco.
    abertos: dict[str, str] = {}  # loop -> bloco onde abriu
    fechados: dict[str, str] = {}
    mapa = []
    for i, b in enumerate(blocos):
        ativos_inicio = sorted(abertos)
        for loop in b.loops("abre"):
            if loop in abertos or loop in fechados:
                alertar("aviso", "loops", f"{loop} aberto de novo em {b.id}")
            abertos[loop] = b.id
        for loop in b.loops("fecha"):
            if loop not in abertos:
                alertar("erro", "loops", f"{b.id} fecha {loop}, que não foi aberto antes")
            else:
                fechados[loop] = b.id
                del abertos[loop]
        mapa.append({"bloco": b.id, "inicio": fmt_tempo(b.inicio_s), "ativos_no_inicio": ativos_inicio,
                     "abre": b.loops("abre"), "fecha": b.loops("fecha"), "ativos_no_fim": sorted(abertos)})
        if not ativos_inicio and not b.loops("abre"):
            alertar("erro", "abandono", f"{b.id} ({fmt_tempo(b.inicio_s)}–{fmt_tempo(b.fim_s)}) não tem nenhum loop ativo: ponto de abandono")
        elif not abertos and i < len(blocos) - 1:
            prox = blocos[i + 1]
            if prox.loops("abre"):
                alertar("aviso", "abandono", f"{b.id} fecha todos os loops; {prox.id} precisa abrir o próximo logo nas primeiras frases")
    for loop, origem in abertos.items():
        alertar("erro", "loops", f"{loop} aberto em {origem} e nunca fechado")
    for loop in sorted(set(abertos) | set(fechados)):
        if loops_declarados and loop not in loops_declarados:
            alertar("aviso", "loops", f"{loop} usado nos blocos mas não descrito na seção '## Loops'")

    # Conectores e virada.
    for i, b in enumerate(blocos):
        conector = sem_acento(b.meta.get("conector", ""))
        if i > 0 and conector in ("", "—", "-"):
            alertar("aviso", "conector", f"{b.id} sem conector declarado")
        elif "depois" in conector:
            alertar("aviso", "conector", f"{b.id} liga ao anterior por 'e depois': troque por causa (então) ou conflito (mas)")
    if formato == "longo" and total_s > 0:
        viradas = [b for b in blocos if "virada" in sem_acento(b.meta.get("funcao", ""))]
        if not viradas:
            alertar("aviso", "virada", "nenhum bloco com função 'virada'")
        elif not any(b.inicio_s / total_s <= 0.65 and b.fim_s / total_s >= 0.40 for b in viradas):
            posicoes = ", ".join(f"{b.id} em {b.inicio_s / total_s:.0%}" for b in viradas)
            alertar("aviso", "virada", f"virada fora da faixa de 40–65% da duração ({posicoes})")

    verificar = [{"bloco": b.id, "item": v} for b in blocos for v in b.verificar]
    if verificar:
        alertar("aviso", "factualidade", f"{len(verificar)} item(ns) marcados com [VERIFICAR]")

    # Falas dos personagens.
    falas: dict[str, dict[str, int]] = {}
    do_pedido = personagens_do_brief(caminho)
    for b in blocos:
        for i, (pos, personagem, _) in enumerate(b.mudancas):
            fim = b.mudancas[i + 1][0] if i + 1 < len(b.mudancas) else len(b.tokens)
            if personagem and pos == fim:
                alertar("aviso", "falas", f"{b.id}: [FALA: {personagem}] sem texto depois")
        for f in b.falas:
            if f.personagem is None:
                continue
            n = f.fim - f.inicio
            stats = falas.setdefault(f.personagem, {"falas": 0, "palavras": 0})
            stats["falas"] += 1
            stats["palavras"] += n
            if n > MAX_PALAVRAS_FALA:
                alertar("aviso", "falas", f"{b.id}: fala de {f.personagem} com {n} palavras; divida (até {MAX_PALAVRAS_FALA})")
    for nome in falas:
        if do_pedido is not None and sem_acento(nome).strip() not in do_pedido:
            alertar("erro", "falas", f"{nome} fala no roteiro mas não está em personagens no pedido (sem voz para ele)")

    ultima_frase = re.split(r"(?<=[.!?…])\s+", " ".join(blocos[-1].tokens))[-1] if blocos[-1].tokens else ""
    primeira_frase = re.split(r"(?<=[.!?…])\s+", " ".join(blocos[0].tokens))[0] if blocos[0].tokens else ""

    return {
        "arquivo": str(caminho),
        "formato": formato,
        "ppm": ppm,
        "titulo": front.get("titulo", ""),
        "palavras": len(todas),
        "duracao_estimada": fmt_tempo(total_s),
        "duracao_segundos": round(total_s),
        "duracao_alvo": fmt_tempo(alvo_s) if alvo_s else None,
        "trecho_gancho": trecho_gancho,
        "trecho_30s": trecho_30s,
        "primeira_frase": primeira_frase,
        "ultima_frase": ultima_frase,
        "blocos": [
            {"id": b.id, "titulo": b.titulo, "funcao": b.meta.get("funcao", ""), "palavras": len(b.tokens),
             "inicio": fmt_tempo(b.inicio_s), "fim": fmt_tempo(b.fim_s), "duracao_s": round(b.fim_s - b.inicio_s),
             "rehooks": len(b.rehooks), "conector": b.meta.get("conector", ""), "cortavel": b.meta.get("cortavel", ""),
             "abre_com": " ".join(b.tokens[:8])}
            for b in blocos
        ],
        "intervalos_rehook": intervalos,
        "mapa_loops": mapa,
        "loops_declarados": loops_declarados,
        "verificar": verificar,
        "falas": falas,
        "alertas": alertas,
    }


def relatorio_md(r: dict[str, object]) -> str:
    alvo = f" (meta {r['duracao_alvo']})" if r["duracao_alvo"] else ""
    rotulo_gancho = "0–2 s" if r["formato"] == "short" else "0–5 s"
    linhas = [
        f"# Medição: {r['titulo'] or r['arquivo']}",
        "",
        f"- formato: {r['formato']} · {r['palavras']} palavras · {r['ppm']} ppm · duração estimada **{r['duracao_estimada']}**{alvo}",
        f"- {rotulo_gancho}: \"{r['trecho_gancho']}\"",
        f"- 0–30 s: \"{r['trecho_30s']}\"",
    ]
    if r["formato"] == "short":
        linhas.append(f"- teste de loop (última → primeira): \"{r['ultima_frase']}\" → \"{r['primeira_frase']}\"")
    linhas += ["", "## Blocos", "", "| Bloco | Função | Início | Fim | Dur. | Palavras | Re-hooks | Conector | Cortável | Abre com |",
               "|---|---|---|---|---|---|---|---|---|---|"]
    for b in r["blocos"]:
        linhas.append(f"| {b['id']} {b['titulo']} | {b['funcao']} | {b['inicio']} | {b['fim']} | {b['duracao_s']} s | {b['palavras']} | "
                      f"{b['rehooks']} | {b['conector']} | {b['cortavel']} | {b['abre_com']}… |")
    linhas += ["", "## Loops", ""]
    for m in r["mapa_loops"]:
        linhas.append(f"- {m['bloco']} ({m['inicio']}): ativos {m['ativos_no_inicio'] or '—'} · abre {m['abre'] or '—'} · "
                      f"fecha {m['fecha'] or '—'} · ativos no fim {m['ativos_no_fim'] or '—'}")
    for loop, desc in r["loops_declarados"].items():
        linhas.append(f"  - {loop}: {desc}")
    if r["intervalos_rehook"]:
        linhas += ["", "## Intervalos entre re-hooks", ""]
        linhas += [f"- {i['de']} → {i['ate']}: {i['segundos']} s ({i['blocos']})" for i in r["intervalos_rehook"]]
    if r["verificar"]:
        linhas += ["", "## A verificar", ""] + [f"- {v['bloco']}: {v['item']}" for v in r["verificar"]]
    if r["falas"]:
        linhas += ["", "## Falas", ""]
        linhas += [f"- {nome}: {n['falas']} fala(s), {n['palavras']} palavras" for nome, n in r["falas"].items()]
    linhas += ["", "## Alertas", ""]
    if r["alertas"]:
        linhas += [f"- **{a['nivel']}** [{a['codigo']}] {a['mensagem']}" for a in r["alertas"]]
    else:
        linhas.append("- nenhum")
    return "\n".join(linhas)


# ---------------------------------------------------------------- orçamento de palavras (antes de escrever)

FAIXA_SEGUNDOS = re.compile(r"(\d+:\d{2}|\d+)\s*[–-]\s*(\d+:\d{2}|\d+)\s*s\b|(\d+:\d{2})\s*[–-]\s*(\d+:\d{2})")
PALAVRAS_DECLARADAS = re.compile(r"~\s*(\d+)\s*palavras")
TOLERANCIA = 0.10


def segundos_de(valor: str) -> float:
    if ":" in valor:
        minutos, seg = valor.split(":")
        return int(minutos) * 60 + int(seg)
    return float(valor)


def orcamento(pasta: Path, ppm_cli: int | None = None) -> dict:
    """Quantas palavras cabem no vídeo e em cada bloco da estrutura do ângulo escolhido. A duração vem do ângulo
    (o contrato), senão do brief. Cada bloco usa as palavras declaradas (~N palavras) ou a faixa de tempo (0-5 s,
    0:45-2:30); o que não tem nenhuma das duas divide o tempo que sobrar."""
    angulo = json.loads((pasta / "04-angulo-escolhido.json").read_text(encoding="utf-8"))
    brief_path = pasta / "00-brief.json"
    brief = json.loads(brief_path.read_text(encoding="utf-8")) if brief_path.exists() else {}
    ppm = ppm_cli or ppm_do_brief(brief) or PPM_PADRAO
    duracao_txt = angulo.get("duracao_recomendada") or brief.get("duracao_alvo")
    total_s = parse_tempo(str(duracao_txt or ""))
    if not total_s:
        raise ValueError("sem duração no ângulo (duracao_recomendada) nem no brief (duracao_alvo)")
    total = round(total_s * ppm / 60)
    blocos = []
    for item in angulo.get("estrutura") or []:
        texto = str(item)
        m_id = re.match(r"\s*(B\d+)", texto)
        b = {"id": m_id.group(1) if m_id else f"B{len(blocos) + 1:02d}", "resumo": re.sub(r"^\s*B\d+\s*(\([^)]*\))?\s*:?\s*", "", texto)[:90],
             "faixa": None, "segundos": None, "palavras": None}
        if m := PALAVRAS_DECLARADAS.search(texto):
            b["palavras"] = int(m.group(1))
        if m := FAIXA_SEGUNDOS.search(texto):
            ini, fim = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            b["faixa"] = f"{ini}–{fim} s" if ":" not in ini else f"{ini}–{fim}"
            b["segundos"] = max(0.0, segundos_de(fim) - segundos_de(ini))
        if b["palavras"] is None and b["segundos"] is not None:
            b["palavras"] = round(b["segundos"] * ppm / 60)
        blocos.append(b)
    sem = [b for b in blocos if b["palavras"] is None]
    if sem:
        sobra = max(0, total - sum(b["palavras"] for b in blocos if b["palavras"] is not None))
        for b in sem:
            b["palavras"] = round(sobra / len(sem))
            b["estimado"] = True
    return {"titulo": angulo.get("titulo", ""), "formato": angulo.get("formato") or brief.get("formato"),
            "duracao": fmt_tempo(total_s), "fonte_duracao": "ângulo" if angulo.get("duracao_recomendada") else "brief",
            "ppm": ppm, "palavras": total, "minimo": round(total * (1 - TOLERANCIA)), "maximo": round(total * (1 + TOLERANCIA)),
            "blocos": blocos, "soma_blocos": sum(b["palavras"] for b in blocos)}


def orcamento_md(o: dict) -> str:
    linhas = [f"# Orçamento de palavras: {o['titulo']}", "",
              f"- duração-alvo {o['duracao']} (do {o['fonte_duracao']}) a {o['ppm']} palavras/min: **{o['palavras']} palavras**; "
              f"dentro da faixa de {o['minimo']} a {o['maximo']} está certo, não persiga o número exato",
              f"- cada [PAUSA] consome 1 s (cerca de {round(o['ppm'] / 60)} palavras)",
              "- se a orientação do usuário pedir outro tamanho, ela vale mais que este orçamento", ""]
    if o["blocos"]:
        linhas += ["| Bloco | Faixa | Palavras | O que o ângulo pede |", "|---|---|---|---|"]
        for b in o["blocos"]:
            linhas.append(f"| {b['id']} | {b['faixa'] or '—'} | {b['palavras']}{' (divisão do resto)' if b.get('estimado') else ''} | "
                          f"{b['resumo'].replace('|', '/')} |")
        if abs(o["soma_blocos"] - o["palavras"]) > o["palavras"] * TOLERANCIA:
            linhas += ["", f"aviso: os blocos do ângulo somam {o['soma_blocos']} palavras, fora da faixa do total; "
                           "reparta a diferença entre os blocos de desenvolvimento"]
    return "\n".join(linhas)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roteiro", type=Path, nargs="?")
    ap.add_argument("--ppm", type=int, help=f"palavras por minuto (padrão: narracao.palavras_por_minuto do brief, o ppm do frontmatter ou {PPM_PADRAO})")
    ap.add_argument("--json", type=Path, help="também salva o resultado em JSON neste caminho")
    ap.add_argument("--narracao", action="store_true",
                    help="só imprime o texto como o espectador ouve, sem títulos, metadados, marcadores nem notas "
                         "(para a leitura fria)")
    ap.add_argument("--orcamento", type=Path, metavar="PASTA",
                    help="antes de escrever: quantas palavras cabem no vídeo e em cada bloco do ângulo escolhido")
    args = ap.parse_args()
    if args.orcamento:
        try:
            o = orcamento(args.orcamento, args.ppm)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"erro: {e}", file=sys.stderr)
            return 2
        print(orcamento_md(o))
        return 0
    if not args.roteiro:
        ap.error("informe o roteiro (ou --orcamento PASTA)")
    if args.narracao:
        try:
            _, blocos, _ = ler_roteiro(args.roteiro)
        except OSError as e:
            print(f"erro: {e}", file=sys.stderr)
            return 2
        print("\n\n".join("\n".join(b.ouvido) for b in blocos if b.ouvido))
        return 0
    try:
        resultado = medir(args.roteiro, args.ppm)
    except (OSError, ValueError) as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2
    print(relatorio_md(resultado))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
