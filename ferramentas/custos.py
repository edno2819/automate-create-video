#!/usr/bin/env python3
"""Custo de produção de um projeto: quanto cada chamada paga gastou, e o total.

Cada chamada paga é registrada em dois lugares:
- projetos/<slug>/custos.jsonl, com o uso que a própria API devolve (usageMetadata do Gemini, usage da OpenAI,
  caracteres da ElevenLabs): é um artefato do projeto, que vai junto num fork ou numa cópia;
- a tabela `custos` do banco, quando ele está acessível (o servidor e os scripts que ele chama), com o projeto,
  a execução (variável EXECUCAO_ID, que o trabalhador passa aos scripts), o provedor e o custo calculado. É de lá
  que a interface soma o custo do projeto, por etapa e por serviço.

O preço vem de biblioteca/precos.json. No jsonl ele é aplicado só na hora de somar (corrigir a tabela recalcula
tudo); no banco, o custo é calculado na hora do registro. Os agentes (LLM e busca na web) são registrados pelo
motor com `registrar_llm`.

Uso:
    python3 ferramentas/custos.py projetos/<slug>
    python3 ferramentas/custos.py projetos/<slug> --json
    python3 ferramentas/custos.py projetos/<slug> --estimar   # projeto produzido antes do registro existir

A estimativa reconstrói o mínimo a partir do que ficou no projeto: uma imagem por cena gerada no manifesto de
09-imagens (pelo preço fixo por imagem) e a duração da narração (o Gemini cobra 25 tokens por segundo de
áudio). Tentativas que falharam e imagens refeitas não deixam rastro, então o custo real é maior. As linhas
estimadas levam "estimado": true, e só entram para o serviço que ainda não tem nenhum registro.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

PRECOS = RAIZ / "biblioteca" / "precos.json"
REGISTRO = "custos.jsonl"
SERVICOS = {"imagem": "Imagens", "animacao": "Animação", "narracao": "Narração", "llm": "Agentes (LLM)",
            "busca": "Busca na web"}
NOMES_PROVEDOR = {"anthropic": "Claude", "openai": "OpenAI", "gemini": "Gemini", "elevenlabs": "ElevenLabs",
                  "espeak": "espeak"}
MILHAO = 1_000_000
TOKENS_POR_SEGUNDO_DE_AUDIO = 25
# campos de uma linha do jsonl que viram colunas no banco; o resto vai para `extra`
_COLUNAS = {"quando", "servico", "modelo", "uso", "provedor", "item", "estimado", "agente"}


def provedor_do_modelo(modelo: str | None) -> str:
    """Quem cobra por um modelo, pelo nome dele (para registros antigos e scripts que não dizem)."""
    m = (modelo or "").lower()
    if m.startswith(("gemini", "imagen", "veo")):
        return "gemini"
    if m.startswith(("gpt", "tts-1", "chatgpt", "o1", "o3", "o4", "dall-e", "whisper")):
        return "openai"
    if m.startswith("eleven"):
        return "elevenlabs"
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("espeak"):
        return "espeak"
    return "desconhecido"


def _projeto_da_pasta(pasta: Path) -> str | None:
    """O slug, quando a pasta é projetos/<slug> (um rascunho de personagem, por exemplo, não é projeto)."""
    try:
        resolvida = Path(pasta).resolve()
    except OSError:
        return None
    return resolvida.name if resolvida.parent.name == "projetos" else None


def _execucao_do_ambiente() -> int | None:
    try:
        return int(os.environ["EXECUCAO_ID"])
    except (KeyError, ValueError):
        return None


def _gravar_no_banco(linha: dict, custo_usd: float | None, *, projeto_slug: str | None,
                     execucao_id: int | None) -> None:
    """Grava a linha na tabela custos, se o banco está acessível. Nunca levanta erro."""
    try:
        from nucleo import banco
        if not banco.disponivel():
            return
        from nucleo.banco import repositorios
        repositorios.registrar_custo(
            linha["servico"], linha.get("provedor") or provedor_do_modelo(linha.get("modelo")), linha.get("modelo") or "",
            linha.get("uso") or {}, custo_usd, execucao_id=execucao_id, projeto_slug=projeto_slug,
            agente=linha.get("agente"), item=linha.get("item"), estimado=bool(linha.get("estimado")),
            extra={k: v for k, v in linha.items() if k not in _COLUNAS}, quando=linha.get("quando"))
    except Exception as e:  # noqa: BLE001 - o custo não pode derrubar a produção
        print(f"aviso: não consegui registrar o custo no banco: {type(e).__name__}: {e}", file=sys.stderr)


def registrar(pasta: Path, servico: str, modelo: str, uso: dict | None, provedor: str | None = None,
              **extra) -> None:
    """Acrescenta uma chamada ao registro do projeto (custos.jsonl) e ao banco. Nunca levanta erro: o custo não
    pode derrubar a produção."""
    linha = {"quando": datetime.now().astimezone().isoformat(timespec="seconds"), "servico": servico,
             "modelo": modelo, "provedor": provedor or provedor_do_modelo(modelo), "uso": uso or {},
             **{k: v for k, v in extra.items() if v is not None}}
    try:
        with (Path(pasta) / REGISTRO).open("a", encoding="utf-8") as f:
            f.write(json.dumps(linha, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"aviso: não consegui registrar o custo em {Path(pasta) / REGISTRO}: {e}", file=sys.stderr)
    try:
        valor = custo(linha, tabela())
    except Exception:  # noqa: BLE001 - tabela de preços quebrada: fica sem preço
        valor = None
    _gravar_no_banco(linha, valor, projeto_slug=_projeto_da_pasta(pasta), execucao_id=_execucao_do_ambiente())


def registrar_llm(provedor: str, modelo: str, uso: dict, *, agente: str | None = None, servico: str = "llm",
                  execucao_id: int | None = None, projeto_slug: str | None = None,
                  pasta: Path | None = None) -> float | None:
    """Uma chamada de agente (servico "llm") ou de busca na web ("busca"), com o uso normalizado (entrada,
    saida, cache_leitura, cache_escrita, buscas). Grava no banco (e no custos.jsonl, se `pasta` vier) e devolve o
    custo em dólares (None quando o modelo não tem preço). Nunca levanta erro."""
    linha = {"quando": datetime.now().astimezone().isoformat(timespec="seconds"), "servico": servico,
             "modelo": modelo, "provedor": provedor, "uso": uso or {}}
    if agente:
        linha["agente"] = agente
    try:
        valor = custo_llm(modelo, uso or {})
    except Exception:  # noqa: BLE001
        valor = None
    if pasta is not None:
        try:
            with (Path(pasta) / REGISTRO).open("a", encoding="utf-8") as f:
                f.write(json.dumps(linha, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"aviso: não consegui registrar o custo em {Path(pasta) / REGISTRO}: {e}", file=sys.stderr)
        projeto_slug = projeto_slug or _projeto_da_pasta(pasta)
    _gravar_no_banco(linha, valor, projeto_slug=projeto_slug,
                     execucao_id=execucao_id if execucao_id is not None else _execucao_do_ambiente())
    return valor


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
    caminho = Path(pasta) / REGISTRO
    if not caminho.exists():
        return []
    linhas = []
    for l in caminho.read_text(encoding="utf-8").splitlines():
        if not l.strip():
            continue
        try:
            linhas.append(json.loads(l))
        except json.JSONDecodeError:
            continue  # uma linha cortada (processo morto no meio da escrita) não estraga o resto
    return linhas


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
    """Custo de um uso já normalizado pelos provedores de LLM (entrada sem cache, saida, cache, buscas)."""
    p = preco_em(modelo, quando or datetime.now().isoformat(), precos or tabela())
    if p is None or "saida" not in p:
        return None
    total = (uso.get("entrada", 0) * p.get("entrada", 0) + uso.get("saida", 0) * p["saida"]
             + uso.get("cache_leitura", 0) * p.get("cache_leitura", p.get("entrada", 0))
             + uso.get("cache_escrita", 0) * p.get("cache_escrita", p.get("entrada", 0))) / MILHAO
    return total + uso.get("buscas", 0) * p.get("busca", 0)


def _custo_entrada(uso: dict, p: dict) -> float:
    """Tokens de entrada × preço. Quando a tabela separa as imagens de referência (`entrada_imagem`, nos modelos
    de imagem da OpenAI), cada modalidade tem o seu preço; senão, tudo pelo preço de entrada."""
    detalhes = uso.get("promptTokensDetails") or []
    if "entrada_imagem" in p and detalhes:
        colunas = {"IMAGE": "entrada_imagem", "AUDIO": "entrada_audio"}
        return sum((d.get("tokenCount") or 0) * p.get(colunas.get(d.get("modality", "TEXT"), "entrada"), p.get("entrada", 0))
                   for d in detalhes)
    return (uso.get("promptTokenCount") or 0) * p.get("entrada", 0)


def preco_por_segundo(modelo: str, resolucao: str | None, quando: str | None = None,
                      precos: dict | None = None) -> float | None:
    """Dólares por segundo de vídeo gerado: os tokens por segundo da resolução × o preço do token de vídeo. None
    quando a tabela não tem a conta dessa resolução (o Gemini publica só a de 720p)."""
    p = preco_em(modelo, quando or datetime.now().isoformat(), precos or tabela())
    tokens = ((p or {}).get("tokens_por_segundo") or {}).get(resolucao or "720p")
    return tokens * p["saida_video"] / MILHAO if tokens and "saida_video" in p else None


def custo(item: dict, precos: dict) -> float | None:
    if item.get("servico") in ("llm", "busca"):
        return custo_llm(item["modelo"], item.get("uso") or {}, item.get("quando"), precos)
    if item.get("servico") == "animacao":  # pelo segundo de vídeo; a entrada (a imagem e o texto) fica em centavos
        por_segundo = preco_por_segundo(item["modelo"], item.get("tamanho"), item.get("quando"), precos)
        return por_segundo * item["segundos"] if por_segundo is not None and item.get("segundos") is not None else None
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
    total = _custo_entrada(uso, p)
    total += sum(n * p.get(colunas.get(tipo, "saida_texto"), 0) for tipo, n in saida.items())
    return total / MILHAO


def rotulo(servico: str, provedor: str | None) -> str:
    base = SERVICOS.get(servico, servico)
    nome = NOMES_PROVEDOR.get(provedor or "", provedor or "")
    return f"{base} ({nome})" if nome and nome != "desconhecido" else base


def resumo(pasta: Path) -> dict:
    """O custo do projeto pelo custos.jsonl (o artefato), agrupado por serviço e modelo."""
    precos = tabela()
    grupos: dict[tuple[str, str], dict] = {}
    sem_preco = set()
    for item in ler(pasta):
        if not item.get("servico") or not item.get("modelo") or not item.get("quando"):
            continue
        provedor = item.get("provedor") or provedor_do_modelo(item["modelo"])
        g = grupos.setdefault((item["servico"], item["modelo"]), {
            "servico": item["servico"], "rotulo": rotulo(item["servico"], provedor), "provedor": provedor,
            "modelo": item["modelo"], "chamadas": 0, "tokens_entrada": 0, "tokens_saida": 0, "usd": 0.0,
            "estimado": False})
        g["chamadas"] += 1
        g["estimado"] = g["estimado"] or bool(item.get("estimado"))
        uso = item.get("uso") or {}
        if item["servico"] in ("llm", "busca"):
            g["tokens_entrada"] += (uso.get("entrada") or 0) + (uso.get("cache_leitura") or 0) + (uso.get("cache_escrita") or 0)
            g["tokens_saida"] += uso.get("saida") or 0
        else:
            g["tokens_entrada"] += uso.get("promptTokenCount") or 0
            g["tokens_saida"] += sum(tokens_saida(uso).values())
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
        print(f"{g['rotulo']:28} {g['modelo']:30} {g['chamadas']:4} chamadas  US$ {g['usd']:.4f}"
              + (" (estimativa mínima)" if g["estimado"] else ""))
    print(f"{'total das APIs':28} {'':30} {'':14} US$ {r['total_usd']:.4f}")
    if r["sem_preco"]:
        print(f"aviso: sem preço em biblioteca/precos.json para {', '.join(r['sem_preco'])} (fora do total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
