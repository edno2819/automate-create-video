#!/usr/bin/env python3
"""Custo de produção de um projeto: quanto cada chamada paga gastou, e o total.

Os scripts que chamam APIs pagas (imagens e narração no Gemini) registram cada chamada em
projetos/<slug>/custos.jsonl, com os tokens que a própria API devolve (usageMetadata). O preço é aplicado só na
hora de somar, a partir de biblioteca/precos.json: corrigir a tabela recalcula tudo. O custo do Claude Code (os
agentes) não passa por aqui: vem do total_cost_usd de cada execução, que a interface web guarda no banco.

Uso:
    python3 ferramentas/custos.py projetos/<slug>
    python3 ferramentas/custos.py projetos/<slug> --json
    python3 ferramentas/custos.py projetos/<slug> --estimar   # projeto produzido antes do registro existir

A estimativa reconstrói o mínimo a partir do que ficou no projeto: uma imagem por cena gerada no manifesto de
09-imagens (pelo preço fixo por imagem) e a duração da narração (o Gemini cobra 25 tokens por segundo de
áudio). Tentativas que falharam e imagens refeitas não deixam rastro, então o custo real é maior. As linhas
estimadas levam "estimado": true, e só entram para o serviço que ainda não tem nenhum registro.

Só usa a biblioteca padrão.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PRECOS = RAIZ / "biblioteca" / "precos.json"
REGISTRO = "custos.jsonl"
SERVICOS = {"imagem": "Imagens (Gemini)", "narracao": "Narração (Gemini TTS)"}
MILHAO = 1_000_000
TOKENS_POR_SEGUNDO_DE_AUDIO = 25


def registrar(pasta: Path, servico: str, modelo: str, uso: dict | None, **extra) -> None:
    """Acrescenta uma chamada ao registro do projeto. Nunca levanta erro: o custo não pode derrubar a produção."""
    linha = {"quando": datetime.now().isoformat(timespec="seconds"), "servico": servico, "modelo": modelo,
             "uso": uso or {}, **{k: v for k, v in extra.items() if v is not None}}
    try:
        with (pasta / REGISTRO).open("a", encoding="utf-8") as f:
            f.write(json.dumps(linha, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"aviso: não consegui registrar o custo em {pasta / REGISTRO}: {e}", file=sys.stderr)


def duracao_wav(caminho: Path) -> float:
    import wave
    with wave.open(str(caminho), "rb") as w:
        return w.getnframes() / w.getframerate()


def estimar(pasta: Path) -> list[str]:
    """Registra o custo mínimo de imagens e narração feitas antes do registro existir. Devolve o que acrescentou."""
    ja = {item["servico"] for item in ler(pasta)}
    feito = []
    manifesto = pasta / "09-imagens" / "manifesto.json"
    if "imagem" not in ja and manifesto.exists():
        geradas = [(id_, r) for id_, r in json.loads(manifesto.read_text(encoding="utf-8")).items()
                   if r.get("origem") == "ia" and r.get("status") == "ok" and r.get("modelo")]
        for id_, r in geradas:
            registrar(pasta, "imagem", r["modelo"], None, item=id_, estimado=True)
        if geradas:
            feito.append(f"{len(geradas)} imagem(ns) do manifesto")
    blocos, audio = pasta / "09-narracao" / "blocos.json", pasta / "09-narracao" / "narracao.wav"
    if "narracao" not in ja and blocos.exists() and audio.exists():
        conf = json.loads(blocos.read_text(encoding="utf-8"))
        if conf.get("fornecedor", "gemini") == "gemini" and conf.get("modelo"):
            segundos = duracao_wav(audio)
            tokens = round(segundos * TOKENS_POR_SEGUNDO_DE_AUDIO)
            registrar(pasta, "narracao", conf["modelo"], {
                "candidatesTokenCount": tokens, "candidatesTokensDetails": [{"modality": "AUDIO", "tokenCount": tokens}]},
                estimado=True, segundos=round(segundos, 1))
            feito.append(f"narração de {segundos:.0f} s")
    return feito


def ler(pasta: Path) -> list[dict]:
    caminho = pasta / REGISTRO
    if not caminho.exists():
        return []
    return [json.loads(l) for l in caminho.read_text(encoding="utf-8").splitlines() if l.strip()]


def tabela() -> dict:
    return json.loads(PRECOS.read_text(encoding="utf-8")) if PRECOS.exists() else {"modelos": {}}


def preco_em(modelo: str, quando: str, precos: dict) -> dict | None:
    """O preço do modelo na data da chamada: um período só, ou uma lista de períodos com 'desde'."""
    valor = precos.get("modelos", {}).get(modelo)
    if isinstance(valor, list):
        vigentes = [p for p in valor if (p.get("desde") or "") <= quando[:10]]
        return vigentes[-1] if vigentes else None
    return valor


def tokens_saida(uso: dict) -> dict[str, int]:
    """Tokens de saída por modalidade (IMAGE, AUDIO, TEXT); o raciocínio do modelo é cobrado como texto."""
    por_tipo: dict[str, int] = {}
    for d in uso.get("candidatesTokensDetails") or []:
        por_tipo[d.get("modality", "TEXT")] = por_tipo.get(d.get("modality", "TEXT"), 0) + (d.get("tokenCount") or 0)
    if not por_tipo and uso.get("candidatesTokenCount"):
        por_tipo["TEXT"] = uso["candidatesTokenCount"]
    if uso.get("thoughtsTokenCount"):
        por_tipo["TEXT"] = por_tipo.get("TEXT", 0) + uso["thoughtsTokenCount"]
    return por_tipo


def custo_llm(modelo: str, uso: dict, quando: str | None = None, precos: dict | None = None) -> float | None:
    """Custo de um uso já normalizado por ferramentas/llm.py (entrada sem cache, saida, cache, buscas)."""
    p = preco_em(modelo, quando or datetime.now().isoformat(), precos or tabela())
    if p is None or "saida" not in p:
        return None
    total = (uso.get("entrada", 0) * p.get("entrada", 0) + uso.get("saida", 0) * p["saida"]
             + uso.get("cache_leitura", 0) * p.get("cache_leitura", p.get("entrada", 0))
             + uso.get("cache_escrita", 0) * p.get("cache_escrita", p.get("entrada", 0))) / MILHAO
    return total + uso.get("buscas", 0) * p.get("busca", 0)


def custo(item: dict, precos: dict) -> float | None:
    p = preco_em(item["modelo"], item["quando"], precos)
    if p is None:
        return None
    uso = item.get("uso") or {}
    saida = tokens_saida(uso)
    if "por_mil_caracteres" in p and item.get("caracteres") is not None:  # ElevenLabs
        return item["caracteres"] / 1000 * p["por_mil_caracteres"]
    if "por_minuto" in p and item.get("segundos") is not None and not uso:  # TTS da OpenAI: a API não devolve o uso
        return item["segundos"] / 60 * p["por_minuto"]
    if not uso:  # a API não devolveu o uso: vale o preço fixo por imagem, se houver
        return (p.get("por_imagem") or {}).get(item.get("tamanho") or "1K") if item["servico"] == "imagem" else None
    colunas = {"IMAGE": "saida_imagem", "AUDIO": "saida_audio", "TEXT": "saida_texto"}
    total = (uso.get("promptTokenCount") or 0) * p.get("entrada", 0)
    total += sum(n * p.get(colunas.get(tipo, "saida_texto"), 0) for tipo, n in saida.items())
    return total / MILHAO


def resumo(pasta: Path) -> dict:
    precos = tabela()
    grupos: dict[tuple[str, str], dict] = {}
    sem_preco = set()
    for item in ler(pasta):
        g = grupos.setdefault((item["servico"], item["modelo"]), {
            "servico": item["servico"], "rotulo": SERVICOS.get(item["servico"], item["servico"]), "modelo": item["modelo"],
            "chamadas": 0, "tokens_entrada": 0, "tokens_saida": 0, "usd": 0.0, "estimado": False})
        g["chamadas"] += 1
        g["estimado"] = g["estimado"] or bool(item.get("estimado"))
        g["tokens_entrada"] += (item.get("uso") or {}).get("promptTokenCount") or 0
        g["tokens_saida"] += sum(tokens_saida(item.get("uso") or {}).values())
        valor = custo(item, precos)
        if valor is None:
            sem_preco.add(item["modelo"])
        else:
            g["usd"] += valor
    itens = [{**g, "usd": round(g["usd"], 4)} for g in grupos.values()]
    return {"total_usd": round(sum(g["usd"] for g in itens), 4), "itens": itens, "sem_preco": sorted(sem_preco),
            "tabela": {k: precos.get(k) for k in ("fontes", "conferido_em")}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pasta", type=Path, help="pasta do projeto (projetos/<slug>)")
    ap.add_argument("--json", action="store_true", help="imprime o resumo em JSON")
    ap.add_argument("--estimar", action="store_true", help="registra o custo mínimo do que foi feito antes do registro")
    args = ap.parse_args()
    if args.estimar:
        feito = estimar(args.pasta)
        print(f"estimado: {', '.join(feito)}" if feito else "nada a estimar: os serviços já têm registro ou não há manifesto",
              file=sys.stderr)
    r = resumo(args.pasta)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    for g in r["itens"]:
        print(f"{g['rotulo']:24} {g['modelo']:30} {g['chamadas']:4} chamadas  US$ {g['usd']:.4f}"
              + (" (estimativa mínima)" if g["estimado"] else ""))
    print(f"{'total das APIs':24} {'':30} {'':14} US$ {r['total_usd']:.4f}")
    if r["sem_preco"]:
        print(f"aviso: sem preço em biblioteca/precos.json para {', '.join(r['sem_preco'])} (fora do total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
