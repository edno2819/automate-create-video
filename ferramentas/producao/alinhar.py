#!/usr/bin/env python3
"""Mede o tempo real de cada palavra da narração e confere se a voz disse exatamente o roteiro.

Para cada bloco narrado (09-narracao/blocos.json), transcreve o áudio com o faster-whisper, com tempo por
palavra, e casa as palavras reconhecidas com as palavras do roteiro. O resultado é:

  09-narracao/tempos.json   início e fim de cada cena e de cada palavra, no tempo da narração

A montagem (montar_video.py) usa esse arquivo no lugar da estimativa de 150 palavras por minuto: cada
imagem troca quando a voz chega ao trecho dela, e as legendas aparecem junto com a fala.

O casamento também é um controle de qualidade da voz:
  - cobertura: parte das palavras do roteiro que aparece no áudio. Grafia diferente com o mesmo número de
    palavras ("13" e "XIII", "bacia" ouvido como "vacia") conta como coberta;
  - extras: palavras faladas que não estão no roteiro (a voz repetiu o texto ou leu a instrução de estilo).
Cobertura abaixo de 85% ou mais de 2 palavras extras num bloco viram alerta, e o script sai com código 1.

Com --verificar, só confere os blocos (ou as falas, B03-F02, nos blocos com diálogo) indicados, sem gravar
tempos.json, e imprime uma linha JSON por item. É o modo que narrar.py usa para refazer sozinho o que saiu
com defeito.

Uso (as dependências, faster-whisper e numpy, estão no pyproject.toml):
    python3 ferramentas/producao/alinhar.py projetos/<slug>
    python3 ferramentas/producao/alinhar.py projetos/<slug> --verificar B01,B03
    python3 ferramentas/producao/alinhar.py projetos/<slug> --verificar B01=caminho/teste.wav
    python3 ferramentas/producao/alinhar.py projetos/<slug> --modelo medium

O modelo do whisper vem de --modelo; sem ele, o configurado (Configurações > Imagens e voz, ou WHISPER_MODELO),
senão small. Ele é baixado na primeira execução (small: ~500 MB; na imagem Docker, já vem baixado) e fica em
cache.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "ferramentas"))
from medir_roteiro import ler_roteiro  # noqa: E402

TAXA = 16000
CAUDA_S = 0.6          # a última imagem fica na tela um pouco depois da última palavra
MIN_COBERTURA = 0.85   # abaixo disso, o áudio provavelmente pulou texto
MAX_EXTRAS = 2         # palavras a mais toleradas por bloco (eco do reconhecimento)


def modelo_padrao() -> str:
    """O modelo do whisper configurado: o do banco (nucleo.rotas), senão WHISPER_MODELO, senão small."""
    try:
        from nucleo import rotas
        return rotas.modelo_whisper()
    except Exception:  # sem banco ou sem a configuração: vale o ambiente
        return os.environ.get("WHISPER_MODELO") or "small"


def norm(palavra: str) -> str:
    sem_acento = "".join(c for c in unicodedata.normalize("NFD", palavra.lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[\W_]+", "", sem_acento)


def carregar_audio(caminho: Path):
    """Decodifica com o ffmpeg para 16 kHz mono (o decode_audio do faster-whisper quebra com o PyAV atual)."""
    import numpy as np
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(caminho), "-ar", str(TAXA), "-ac", "1", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(pcm, np.int16).astype(np.float32) / 32768


def reconhecer(modelo, audio, contexto: str | None, idioma: str = "pt") -> list[tuple[str, float, float]]:
    segmentos, _ = modelo.transcribe(audio, language=idioma, word_timestamps=True, beam_size=5, vad_filter=False,
                                     condition_on_previous_text=False, initial_prompt=contexto)
    return [(w.word.strip(), float(w.start), float(w.end)) for s in segmentos for w in (s.words or []) if norm(w.word)]


def casar(roteiro: list[str], reconhecidas: list[tuple[str, float, float]], duracao: float):
    """Atribui (início, fim) a cada palavra do roteiro. Devolve tempos, cobertura e trechos extras."""
    a = [norm(w) for w in roteiro]
    b = [norm(w) for w, _, _ in reconhecidas]
    ini: list[float | None] = [None] * len(a)
    fim: list[float | None] = [None] * len(a)
    cobertas, extras = 0, []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal" or (op == "replace" and i2 - i1 == j2 - j1):
            # Mesma quantidade de palavras: iguais, ou grafia diferente ("13" e "XIII", "bacia" e "vacia").
            for k in range(i2 - i1):
                ini[i1 + k], fim[i1 + k] = reconhecidas[j1 + k][1], reconhecidas[j1 + k][2]
            cobertas += i2 - i1
        elif op == "replace":
            # Quantidades diferentes: distribui o trecho reconhecido pelas palavras do roteiro, pelo tamanho.
            t0, t1 = reconhecidas[j1][1], reconhecidas[j2 - 1][2]
            pesos = [len(a[k]) + 1 for k in range(i1, i2)]
            t = t0
            for k, p in zip(range(i1, i2), pesos):
                d = (t1 - t0) * p / sum(pesos)
                ini[k], fim[k] = t, t + d
                t += d
            cobertas += min(i2 - i1, j2 - j1)
            if j2 - j1 > i2 - i1:
                extras.append((" ".join(w for w, _, _ in reconhecidas[j1:j2]), reconhecidas[j1][1], (j2 - j1) - (i2 - i1)))
        elif op == "insert":
            extras.append((" ".join(w for w, _, _ in reconhecidas[j1:j2]), reconhecidas[j1][1], j2 - j1))
    # Palavras do roteiro que não foram reconhecidas: interpola entre as vizinhas.
    k = 0
    while k < len(a):
        if ini[k] is not None:
            k += 1
            continue
        j = k
        while j < len(a) and ini[j] is None:
            j += 1
        t0 = fim[k - 1] if k > 0 else 0.0
        t1 = ini[j] if j < len(a) else duracao
        pesos = [len(a[m]) + 1 for m in range(k, j)]
        t = t0
        for m, p in zip(range(k, j), pesos):
            d = max(0.0, t1 - t0) * p / sum(pesos)
            ini[m], fim[m] = t, t + d
            t += d
        k = j
    for m in range(1, len(a)):  # garante tempos crescentes
        ini[m] = max(ini[m], ini[m - 1])
        fim[m] = max(fim[m], ini[m])
    return list(zip(ini, fim)), (cobertas / len(a) if a else 1.0), extras


def avaliar(modelo, wav: Path, tokens: list[str], idioma: str = "pt") -> dict:
    """Transcreve sem contexto e, se a cobertura ficar baixa, de novo com o roteiro como contexto; fica com a
    melhor leitura. (Com o roteiro como contexto desde o início, o whisper às vezes pula o trecho que coincide
    com o contexto.) `idioma` é o código do whisper (a dublagem confere outras línguas)."""
    audio = carregar_audio(wav)
    duracao = len(audio) / TAXA
    melhor = None
    for contexto in (None, " ".join(tokens[-120:])):
        tempos, cobertura, extras = casar(tokens, reconhecer(modelo, audio, contexto, idioma), duracao)
        n_extras = sum(n for _, _, n in extras)
        nota = cobertura - 0.02 * n_extras
        if melhor is None or nota > melhor["nota"]:
            melhor = {"nota": nota, "tempos": tempos, "cobertura": cobertura, "extras": extras, "n_extras": n_extras}
        if cobertura >= MIN_COBERTURA:
            break
    melhor["duracao"] = duracao
    melhor["ok"] = melhor["cobertura"] >= MIN_COBERTURA and melhor["n_extras"] <= MAX_EXTRAS
    melhor["ppm"] = round(len(tokens) / duracao * 60) if duracao else None
    return melhor


def descrever(bid: str, r: dict) -> list[str]:
    alertas = []
    if r["cobertura"] < MIN_COBERTURA:
        alertas.append(f"{bid}: só {r['cobertura']:.0%} das palavras do roteiro aparecem no áudio; narre o bloco de novo")
    if r["n_extras"] > MAX_EXTRAS:
        trechos = "; ".join(f"'{t}' em {s:.1f} s" for t, s, _ in r["extras"][:3])
        alertas.append(f"{bid}: a voz disse {r['n_extras']} palavra(s) fora do roteiro ({trechos}); narre o bloco de novo")
    return alertas


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pasta", type=Path, help="pasta do projeto (projetos/<slug>)")
    ap.add_argument("--modelo", help="modelo do whisper: base, small, medium, large-v3 (padrão: o configurado, senão small)")
    ap.add_argument("--verificar", help="só confere estes blocos: B01,B02 ou B01=arquivo.wav (não grava tempos.json)")
    ap.add_argument("--cauda", type=float, default=CAUDA_S,
                    help=f"segundos que a última imagem fica na tela depois da última palavra (padrão {CAUDA_S})")
    args = ap.parse_args()

    roteiro = args.pasta / "06-roteiro-final.md"
    if not roteiro.exists():
        print(f"erro: {roteiro} não existe", file=sys.stderr)
        return 2
    _, blocos, _ = ler_roteiro(roteiro)
    tokens = {b.id: b.tokens for b in blocos}
    for b in blocos:  # nos blocos com diálogo, narrar.py gera e confere fala por fala
        if len(b.falas) > 1 or b.falas[0].personagem or b.falas[0].direcao:
            tokens.update({f"{b.id}-F{n:02d}": b.tokens[f.inicio:f.fim] for n, f in enumerate(b.falas, 1)})

    args.modelo = args.modelo or modelo_padrao()
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("erro: faster-whisper não está instalado (pip install faster-whisper numpy)", file=sys.stderr)
        return 2
    print(f"carregando o whisper {args.modelo}…", file=sys.stderr, flush=True)
    modelo = WhisperModel(args.modelo, device="cpu", compute_type="int8")

    # ------------------------------------------------------------ modo verificação
    if args.verificar:
        todos_ok = True
        for item in [x.strip() for x in args.verificar.split(",") if x.strip()]:
            bid, _, arquivo = item.partition("=")
            pasta_audio = "falas" if "-F" in bid else "blocos"
            wav = Path(arquivo) if arquivo else args.pasta / "09-narracao" / pasta_audio / f"{bid}.wav"
            if bid not in tokens or not wav.exists():
                print(json.dumps({"bloco": bid, "arquivo": str(wav), "ok": False, "erro": "bloco ou arquivo inexistente"}, ensure_ascii=False))
                todos_ok = False
                continue
            r = avaliar(modelo, wav, tokens[bid])
            todos_ok &= r["ok"]
            print(json.dumps({"bloco": bid, "arquivo": str(wav), "ok": r["ok"], "cobertura_pct": round(r["cobertura"] * 100, 1),
                              "palavras_extras": r["n_extras"], "ppm": r["ppm"], "alertas": descrever(bid, r)},
                             ensure_ascii=False), flush=True)
        return 0 if todos_ok else 1

    # ------------------------------------------------------------ alinhamento completo
    estado_path = args.pasta / "09-narracao" / "blocos.json"
    cenas_path = args.pasta / "08-cenas.json"
    for p in (estado_path, cenas_path):
        if not p.exists():
            print(f"erro: {p} não existe", file=sys.stderr)
            return 2
    estado = json.loads(estado_path.read_text(encoding="utf-8"))
    cenas = json.loads(cenas_path.read_text(encoding="utf-8"))["cenas"]
    if any(c.get("palavras") is None for c in cenas):
        print("erro: 08-cenas.json sem o intervalo de palavras das cenas; rode ferramentas/cenas.py montar", file=sys.stderr)
        return 2

    tempos_globais: dict[str, list[tuple[float, float]]] = {}
    qualidade, alertas = [], []
    for reg in estado["blocos"]:
        bid = reg["id"]
        if bid not in tokens:
            alertas.append(f"{bid}: o bloco narrado não existe mais no roteiro; narre de novo")
            continue
        r = avaliar(modelo, args.pasta / reg["arquivo"], tokens[bid])
        tempos_globais[bid] = [(reg["inicio_s"] + i, reg["inicio_s"] + f) for i, f in r["tempos"]]
        qualidade.append({"bloco": bid, "palavras": len(tokens[bid]), "cobertura_pct": round(r["cobertura"] * 100, 1),
                          "palavras_extras": r["n_extras"], "extras": [{"texto": t, "em_s": round(s, 2)} for t, s, _ in r["extras"]],
                          "ppm_real": r["ppm"]})
        alertas += descrever(bid, r)
        print(f"  {bid}: cobertura {r['cobertura']:.0%} · {r['n_extras']} extra(s) · {r['ppm']} ppm · {'ok' if r['ok'] else 'REVISAR'}", flush=True)

    # Cenas: cada uma começa na primeira palavra do seu trecho e vai até o início da próxima.
    saida_cenas = []
    for c in cenas:
        if c["bloco"] not in tempos_globais:
            continue
        a, b = c["palavras"]
        tb = tempos_globais[c["bloco"]]
        palavras = [{"texto": tokens[c["bloco"]][k], "inicio": round(tb[k][0], 3), "fim": round(tb[k][1], 3)}
                    for k in range(a, min(b, len(tb)))]
        inicio = palavras[0]["inicio"] if palavras else (saida_cenas[-1]["fim_s"] if saida_cenas else 0.0)
        saida_cenas.append({"id": c["id"], "inicio_s": round(inicio, 3), "palavras": palavras})
    if saida_cenas:
        saida_cenas[0]["inicio_s"] = 0.0
    duracao_video = round(estado["duracao_s"] + args.cauda, 3)
    for atual, prox in zip(saida_cenas, saida_cenas[1:] + [None]):
        atual["fim_s"] = prox["inicio_s"] if prox else duracao_video

    resultado = {
        "audio": estado["arquivo"], "duracao_audio_s": estado["duracao_s"], "duracao_video_s": duracao_video,
        "fonte": f"faster-whisper {args.modelo}", "voz": estado.get("voz"), "personagens": estado.get("personagens") or {},
        "modelo_voz": estado.get("modelo"),
        "cenas": saida_cenas, "qualidade": qualidade, "alertas": alertas,
    }
    destino = args.pasta / "09-narracao" / "tempos.json"
    destino.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    duracoes = [c["fim_s"] - c["inicio_s"] for c in saida_cenas]
    print(f"tempos: {destino} · {len(saida_cenas)} cenas · vídeo de {duracao_video:.1f} s")
    if duracoes:
        print(f"duração por cena (real): média {sum(duracoes) / len(duracoes):.1f} s · mín {min(duracoes):.1f} s · máx {max(duracoes):.1f} s")
    for a in alertas:
        print(f"alerta: {a}")
    if alertas and estado.get("fornecedor") == "espeak":
        print("nota: a narração é do espeak; o reconhecimento erra mais nessa voz robótica e os tempos ficam aproximados. "
              "Para o vídeo final, narre com uma voz de verdade (Gemini, OpenAI ou ElevenLabs).")
    return 1 if alertas else 0


if __name__ == "__main__":
    sys.exit(main())
