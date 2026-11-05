#!/usr/bin/env python3
"""Dubla a narração de um projeto em outra língua, com cada frase no tempo do vídeo.

A dublagem acontece em três passos; este script faz o primeiro e o último, e o fluxo da etapa Edição
(nucleo/pipeline/fluxos.py) faz a tradução no meio, com o agente tradutor:

  segmentar   divide a narração alinhada (09-narracao/tempos.json) em trechos de uma frase, cada um com uma voz só
              (o narrador ou um personagem, pelas marcas [FALA] do roteiro final), o tempo que a frase original
              ocupa no vídeo e o limite de caracteres para a tradução caber nele;
  (traduzir)  o tradutor grava a tradução de cada trecho em traducao.json;
  sintetizar  fala cada trecho na língua nova, confere com o whisper, encaixa cada um no tempo da frase original e
              grava a faixa inteira, do tamanho exato do vídeo, com as legendas na língua nova.

Tudo fica em projetos/<slug>/12-dublagens/<idioma>/:

  segmentos.json   os trechos, com o tempo e o limite de cada um
  traducao.json    a tradução de cada trecho (gravada pelo fluxo)
  trechos/<id>.wav cada trecho como a voz gerou (cache: só é gerado de novo quando o texto, a voz ou a direção mudam)
  dublagem.wav     a faixa de voz, sem música, com a duração exata do vídeo (a música entra na exportação)
  dublagem.json    vozes, ritmo e lugar de cada trecho; os que pedem tradução mais curta (encurtar)
  legendas.srt     as legendas na língua nova, para subir no YouTube
  legendas.ass     as mesmas, no estilo das legendas do vídeo (para queimar na imagem na exportação)

Encaixe: cada trecho começa onde a frase original começa, para a fala acompanhar as imagens. A voz fala no ritmo
dela; o trecho é desacelerado até 0,9x ou acelerado até 1,12x para durar o mesmo que a frase original; se ainda
sobrar fala, ele avança sobre a pausa até a próxima frase, acelerando até 1,3x. O trecho que nem assim cabe vai para
`encurtar`, com o limite de caracteres que o faria caber, e o fluxo pede ao tradutor uma versão mais curta. Até ela
chegar (ou se não chegar), o trecho é acelerado até 1,6x e, se ainda passar, empurra o seguinte um pouco para a
frente. O último nunca passa do fim do vídeo. O atempo do ffmpeg muda o ritmo sem mudar o tom.

Vozes: as do pedido (narrador e personagens), no fornecedor de narracao.fornecedor, ou as da linha de comando. As
vozes do Gemini, da OpenAI e da ElevenLabs (modelo multilíngue) falam outras línguas; o perfil de cada uma diz em que
língua e com que pronúncia falar. O espeak (--fornecedor espeak) dubla de graça, com voz robótica, para testar.

Cada trecho gerado é conferido pelo whisper na língua nova (como na narração): se a voz pulou texto ou disse
texto a mais, ele é gerado de novo, até 3 rodadas. A mesma transcrição dá o tempo de cada palavra para as legendas;
sem ela (espeak, --sem-verificar ou sem o faster-whisper), o tempo de cada palavra é estimado pelo tamanho.

Uso:
    python3 ferramentas/producao/dublar.py segmentar projetos/<slug> --idioma en
    python3 ferramentas/producao/dublar.py sintetizar projetos/<slug> --idioma en
    python3 ferramentas/producao/dublar.py sintetizar projetos/<slug> --idioma es --fornecedor espeak
    python3 ferramentas/producao/dublar.py idiomas

Códigos de saída: 0 ok; 1 a faixa foi gravada, mas há trechos que pedem tradução mais curta, que a verificação não
aprovou ou que não foram conferidos; 2 falta arquivo ou tradução, língua ou fornecedor desconhecido, ou sem chave;
3 sem cota no fornecedor de voz.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from array import array
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "ferramentas"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import alinhar  # noqa: E402
from brief import config, ler_brief  # noqa: E402
import custos  # noqa: E402
from medir_roteiro import ler_roteiro  # noqa: E402
import montar_video  # noqa: E402
import narrar  # noqa: E402
from nucleo import edicao, idiomas  # noqa: E402
import nucleo.provedores.voz as tts  # noqa: E402
from nucleo.provedores.erros import ErroProvedor, SemCota  # noqa: E402
from nucleo.provedores.uso import Uso  # noqa: E402

TAXA = 24000
FPS = 30                   # o da montagem: a duração do vídeo é um número inteiro de quadros
MIN_TRECHO_S = 1.6         # frase mais curta que isso se junta à seguinte (mesma voz, mesmo bloco)
MAX_TRECHO_S = 12.0        # frase mais longa que isso se divide numa vírgula
PAUSA_QUE_SEPARA_S = 0.7   # uma pausa maior que isso na fala original também separa trechos
MIN_ALVO_S = 0.3
FOLGA_S = 0.12             # silêncio mínimo entre dois trechos
FIM_S = 0.05               # o último trecho termina antes do fim do vídeo
VEL_MIN = 0.9              # desacelera até aqui para a fala curta ocupar o tempo da original
VEL_SUAVE = 1.12           # acelera até aqui para terminar junto com a original
VEL_MAX = 1.3              # até aqui, avançando sobre a pausa; passou disso, o trecho pede tradução mais curta
VEL_LIMITE = 1.6           # o encaixe forçado; acima disso, empurra o trecho seguinte
VEL_ABSOLUTA = 4.0         # o último trecho, que não pode passar do fim do vídeo
MARGEM_ENCURTAR = 0.95     # o limite de caracteres pedido ao tradutor, com uma margem
MAX_RODADAS = 3
FIM_DE_FRASE = re.compile(r"[.!?…]+[\"'”»)]*$")
FIM_DE_ORACAO = re.compile(r"[,;:—–][\"'”»)]*$")
ErroSintese = ErroProvedor


def pasta_da_dublagem(pasta: Path, codigo: str) -> Path:
    return pasta / edicao.DUBLAGENS / codigo


def ler_json(caminho: Path):
    return edicao.ler_json(caminho)


def gravar_json(caminho: Path, dados) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def duracao_do_video(tempos: dict) -> float:
    """A duração do vídeo montado: a narração com a cauda, em quadros inteiros (como montar_video.py conta)."""
    return round(round(float(tempos["duracao_video_s"]) * FPS) / FPS, 3)


# ---------------------------------------------------------------- segmentar


def palavras_narradas(tempos: dict, cenas: list[dict], blocos: list) -> list[dict]:
    """Cada palavra da narração, na ordem do vídeo: o tempo dela (tempos.json), o bloco, a posição no bloco e a fala
    (a voz) a que pertence. A palavra k de uma cena é a palavra palavras[0] + k do bloco dela."""
    por_id = {c["id"]: c for c in cenas}
    por_bloco = {}
    for b in blocos:
        fala = [None] * len(b.tokens)
        for n, f in enumerate(b.falas):
            fala[f.inicio:f.fim] = [n] * (f.fim - f.inicio)
        por_bloco[b.id] = (b, fala)
    saida = []
    for ct in tempos.get("cenas") or []:
        c = por_id.get(ct.get("id"))
        if not c or c.get("palavras") is None or c.get("bloco") not in por_bloco:
            continue
        b, fala = por_bloco[c["bloco"]]
        for k, p in enumerate(ct.get("palavras") or []):
            pos = c["palavras"][0] + k
            if pos >= len(b.tokens):
                break
            n = fala[pos]
            f = b.falas[n] if n is not None else None
            saida.append({"bloco": b.id, "pos": pos, "texto": p["texto"], "inicio": float(p["inicio"]),
                          "fim": float(p["fim"]), "fala": n, "personagem": f.personagem if f else None,
                          "direcao": f.direcao if f else ""})
    return saida


def agrupar(palavras: list[dict]) -> list[list[dict]]:
    """Os trechos: uma frase (ou mais, se curtas), sempre de uma voz só e de um bloco só. Frase longa demais se divide
    numa vírgula; uma pausa longa na fala original também separa."""
    grupos: list[list[dict]] = []
    atual: list[dict] = []

    def duracao(g: list[dict]) -> float:
        return g[-1]["fim"] - g[0]["inicio"] if g else 0.0

    for p in palavras:
        if atual and (p["bloco"], p["fala"]) != (atual[-1]["bloco"], atual[-1]["fala"]):
            grupos.append(atual)
            atual = []
        elif atual and p["inicio"] - atual[-1]["fim"] > PAUSA_QUE_SEPARA_S and duracao(atual) >= MIN_TRECHO_S:
            grupos.append(atual)
            atual = []
        atual.append(p)
        if (FIM_DE_FRASE.search(p["texto"]) and duracao(atual) >= MIN_TRECHO_S) or \
                (FIM_DE_ORACAO.search(p["texto"]) and duracao(atual) >= MAX_TRECHO_S):
            grupos.append(atual)
            atual = []
    if atual:
        grupos.append(atual)
    # um trecho curto que sobrou no fim de uma fala se junta ao anterior da mesma fala
    juntos: list[list[dict]] = []
    for g in grupos:
        anterior = juntos[-1] if juntos else None
        if anterior and duracao(g) < MIN_TRECHO_S and (g[0]["bloco"], g[0]["fala"]) == (anterior[-1]["bloco"], anterior[-1]["fala"]):
            anterior.extend(g)
        else:
            juntos.append(g)
    return juntos


def segmentar(tempos: dict, cenas: list[dict], blocos: list, idioma: idiomas.Idioma) -> list[dict]:
    """Os trechos da narração, com o tempo de cada um no vídeo e o limite de caracteres da tradução."""
    por_id = {b.id: b for b in blocos}
    grupos = agrupar(palavras_narradas(tempos, cenas, blocos))
    fim_video = duracao_do_video(tempos)
    saida = []
    for n, g in enumerate(grupos):
        b = por_id[g[0]["bloco"]]
        k0, k1 = g[0]["pos"], g[-1]["pos"] + 1
        inicio = round(g[0]["inicio"], 3)
        proximo = round(grupos[n + 1][0]["inicio"], 3) if n + 1 < len(grupos) else fim_video
        fim_fala = round(min(g[-1]["fim"], proximo), 3)
        alvo = max(MIN_ALVO_S, fim_fala - inicio)
        janela = max(MIN_ALVO_S, proximo - (FIM_S if n + 1 == len(grupos) else FOLGA_S) - inicio)
        saida.append({"id": f"S{n + 1:03d}", "bloco": b.id, "palavras_no_bloco": [k0, k1],
                      "personagem": g[0]["personagem"], "direcao": g[0]["direcao"] or "",
                      "texto": narrar.texto_de(b, k0, k1), "palavras": k1 - k0,
                      "inicio_s": inicio, "fim_fala_s": fim_fala, "proximo_s": proximo,
                      "segundos": round(alvo, 2), "janela_s": round(janela, 2),
                      "max_caracteres": max(4, int(min(alvo * VEL_SUAVE, janela) * idioma.cps))})
    return saida


def cmd_segmentar(args) -> int:
    idioma = idiomas.achar(args.idioma)
    if idioma is None:
        print(f"erro: língua desconhecida: {args.idioma} (veja: dublar.py idiomas)", file=sys.stderr)
        return 2
    pasta = args.pasta
    for nome in ("06-roteiro-final.md", "08-cenas.json", "09-narracao/tempos.json"):
        if not (pasta / nome).exists():
            print(f"erro: {pasta / nome} não existe; a dublagem parte da narração alinhada do vídeo produzido",
                  file=sys.stderr)
            return 2
    brief = ler_brief(pasta)
    if idiomas.mesmo(idioma.codigo, edicao.idioma_original(brief)):
        print(f"erro: o vídeo já é em {idioma.nome}", file=sys.stderr)
        return 2
    tempos = json.loads((pasta / "09-narracao" / "tempos.json").read_text(encoding="utf-8"))
    doc_cenas = json.loads((pasta / "08-cenas.json").read_text(encoding="utf-8"))
    _, blocos, _ = ler_roteiro(pasta / "06-roteiro-final.md")
    cenas = doc_cenas.get("cenas") or []
    if [c["id"] for c in cenas] != [c["id"] for c in tempos.get("cenas") or []]:
        print("erro: as cenas mudaram depois do alinhamento; produza o vídeo de novo antes de dublar", file=sys.stderr)
        return 2
    segmentos = segmentar(tempos, cenas, blocos, idioma)
    if not segmentos:
        print("erro: a narração alinhada não tem palavras para dublar", file=sys.stderr)
        return 2
    doc = {"idioma": idioma.codigo, "nome": idioma.nome, "original": edicao.idioma_original(brief),
           "duracao_video_s": duracao_do_video(tempos), "proporcao": doc_cenas.get("proporcao", "16:9"),
           "entradas": edicao.entradas(pasta), "segmentos": segmentos}
    destino = pasta_da_dublagem(pasta, idioma.codigo) / "segmentos.json"
    gravar_json(destino, doc)
    falas = sum(1 for s in segmentos if s["personagem"])
    duracoes = [s["segundos"] for s in segmentos]
    print(f"segmentos: {destino} · {len(segmentos)} trechos ({falas} de personagens) · {doc['duracao_video_s']:.1f} s de "
          f"vídeo · trecho médio {statistics.mean(duracoes):.1f} s (mín {min(duracoes):.1f}, máx {max(duracoes):.1f})")
    return 0


# ---------------------------------------------------------------- encaixe


def encaixar(segmentos: list[dict], naturais: dict[str, float], duracao_video: float) -> list[dict]:
    """Onde e em que ritmo cada trecho entra na faixa (veja o começo do arquivo). `naturais` é a duração de cada
    trecho no ritmo da voz, sem o silêncio das pontas. `cabe` diz se o trecho caberia no tempo da própria frase
    original até VEL_MAX (o que foi empurrado pelo anterior não conta contra ele)."""
    lugares: list[dict] = []
    fim_anterior: float | None = None
    for i, s in enumerate(segmentos):
        ultimo = i + 1 == len(segmentos)
        d = max(0.01, naturais[s["id"]])
        proximo = duracao_video if ultimo else segmentos[i + 1]["inicio_s"]
        teto = proximo - (FIM_S if ultimo else FOLGA_S)
        alvo = max(MIN_ALVO_S, min(s["fim_fala_s"], proximo) - s["inicio_s"])
        limite_proprio = max(MIN_ALVO_S, teto - s["inicio_s"])
        inicio = s["inicio_s"] if fim_anterior is None else max(s["inicio_s"], fim_anterior + FOLGA_S)
        limite = max(MIN_ALVO_S, teto - inicio)
        fator = min(VEL_SUAVE, max(VEL_MIN, d / alvo))
        if d / fator > limite:
            fator = min(d / limite, VEL_ABSOLUTA if ultimo else VEL_LIMITE)
        lugares.append({"id": s["id"], "inicio_s": round(inicio, 3), "duracao_s": round(d / fator, 3),
                        "velocidade": round(fator, 3), "natural_s": round(d, 3), "alvo_s": round(alvo, 3),
                        "atraso_s": round(inicio - s["inicio_s"], 3), "cabe": d / limite_proprio <= VEL_MAX,
                        "limite_s": round(limite_proprio, 3)})
        fim_anterior = inicio + d / fator
    return lugares


def pedido_de_encurtar(s: dict, texto: str, lugar: dict) -> dict:
    """O que o tradutor precisa para encurtar um trecho: o limite de caracteres que, no ritmo que a voz falou, faria o
    trecho caber no tempo dele acelerando no máximo até VEL_SUAVE."""
    razao = lugar["limite_s"] * VEL_SUAVE / max(0.01, lugar["natural_s"])
    return {"id": s["id"], "original": s["texto"], "atual": texto, "caracteres": len(texto),
            "max_caracteres": max(4, int(len(texto) * razao * MARGEM_ENCURTAR)), "segundos": lugar["limite_s"],
            "falado_em_s": lugar["natural_s"]}


# ---------------------------------------------------------------- áudio


def pcm_de(wav: bytes) -> tuple[wave._wave_params, bytes]:
    with wave.open(io.BytesIO(wav), "rb") as w:
        return w.getparams(), w.readframes(w.getnframes())


def gravar_wav(destino: Path, quadros: bytes, taxa: int = TAXA, canais: int = 1) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destino), "wb") as w:
        w.setnchannels(canais)
        w.setsampwidth(2)
        w.setframerate(taxa)
        w.writeframes(quadros)


def cadeia_atempo(fator: float) -> list[str]:
    """O atempo aceita de 0,5 a 2 por filtro: fatores maiores viram uma cadeia."""
    filtros = []
    while fator > 2.0:
        filtros.append("atempo=2.0")
        fator /= 2.0
    if abs(fator - 1.0) > 0.002:
        filtros.append(f"atempo={fator:.4f}")
    return filtros


def no_formato(quadros: bytes, params, fator: float) -> bytes:
    """O trecho no ritmo do encaixe (atempo: muda a velocidade sem mudar o tom), em PCM de 16 bits, mono, 24 kHz."""
    filtros = cadeia_atempo(fator)
    if not filtros and (params.framerate, params.nchannels, params.sampwidth) == (TAXA, 1, 2):
        return quadros
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", str(params.framerate),
                        "-ac", str(params.nchannels), "-i", "pipe:0", *(["-filter:a", ",".join(filtros)] if filtros else []),
                        "-ar", str(TAXA), "-ac", "1", "-f", "s16le", "pipe:1"], input=quadros, capture_output=True)
    if r.returncode:
        raise ErroSintese(f"ffmpeg falhou ao ajustar o trecho: {r.stderr.decode(errors='replace').strip()[-200:]}")
    return r.stdout


# ---------------------------------------------------------------- vozes


@dataclass
class Trecho:
    """Um trecho da dublagem, pronto para pedir à voz."""
    id: str
    texto: str
    personagem: str | None
    voz: str
    perfil: str
    estilo: str
    arquivo: Path
    pedido: str = ""

    @property
    def tokens(self) -> list[str]:
        """As palavras faladas, sem a pontuação solta ("—"): as que o whisper confere e as legendas mostram."""
        return [p for p in self.texto.split() if any(ch.isalnum() for ch in p)]

    @property
    def palavras(self) -> int:
        return len(self.tokens)


def perfil_do_narrador(idioma: idiomas.Idioma) -> str:
    return (f"Narrador de vídeos para o YouTube. Fala {idioma.lingua} com a pronúncia de um falante nativo, "
            "sem sotaque estrangeiro, dicção clara.")


def planejar(segmentos: list[dict], traducao: dict, idioma: idiomas.Idioma, narrador: tuple[str, str],
             voz_narrador: str, personagens: dict[str, dict], brief: dict, dub: Path,
             fornecedor: str, modelo: str) -> tuple[list[Trecho], list[str]]:
    trechos, erros = [], []
    for s in segmentos:
        texto = " ".join(str((traducao.get(s["id"]) or {}).get("texto") or "").split())
        if s["personagem"] is None:
            voz, (perfil, estilo) = voz_narrador, narrador
            if s["direcao"]:
                estilo += f" Nesta fala: {s['direcao'].rstrip('.')}."
        else:
            p = personagens.get(narrar.chave(s["personagem"]))
            if p is None or not p.get("voz"):
                erros.append(f"{s['personagem']} fala no roteiro, mas não está com voz em personagens no pedido")
                continue
            voz = p["voz"]
            perfil, estilo = narrar.config_personagem(p, s["direcao"], brief, f"{idioma.lingua}, como falante nativo")
        t = Trecho(s["id"], texto, s["personagem"], voz, perfil, estilo, dub / "trechos" / f"{s['id']}.wav")
        t.pedido = narrar.hash_pedido(fornecedor, modelo, voz, perfil, estilo, texto)
        trechos.append(t)
    return trechos, sorted(set(erros))


# ---------------------------------------------------------------- verificação (whisper)


def carregar_whisper(nome: str | None):
    """O modelo do whisper, ou None se o faster-whisper não está instalado."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    nome = nome or alinhar.modelo_padrao()
    print(f"carregando o whisper {nome}…", file=sys.stderr, flush=True)
    return WhisperModel(nome, device="cpu", compute_type="int8")


def conferir(modelo, t: Trecho, quadros: bytes, params, idioma: idiomas.Idioma) -> dict:
    """Transcreve o trecho (sem o silêncio das pontas) na língua nova: diz se a voz disse o texto e o tempo de cada
    palavra, a partir do começo do trecho aparado."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "trecho.wav"
        gravar_wav(wav, quadros, params.framerate, params.nchannels)
        r = alinhar.avaliar(modelo, wav, t.tokens, idioma.whisper)
    return {"ok": r["ok"], "cobertura_pct": round(r["cobertura"] * 100, 1), "palavras_extras": r["n_extras"],
            "alertas": alinhar.descrever(t.id, r), "tempos": [[round(a, 3), round(b, 3)] for a, b in r["tempos"]]}


# ---------------------------------------------------------------- legendas


def palavras_no_tempo(t: Trecho, lugar: dict, verificacao: dict | None) -> list[dict]:
    """As palavras do trecho no tempo do vídeo: pelo whisper (tempo no trecho aparado, dividido pela velocidade do
    encaixe), ou distribuídas pelo tamanho de cada uma."""
    tokens = t.tokens
    inicio, fator, dur = lugar["inicio_s"], lugar["velocidade"], lugar["duracao_s"]
    tempos = (verificacao or {}).get("tempos") or []
    if len(tempos) == len(tokens) and tokens:
        saida = [{"texto": p, "inicio": inicio + a / fator, "fim": inicio + b / fator} for p, (a, b) in zip(tokens, tempos)]
    else:
        pesos = [len(p) + 1 for p in tokens]
        saida, agora = [], inicio
        for p, peso in zip(tokens, pesos):
            d = dur * peso / sum(pesos)
            saida.append({"texto": p, "inicio": agora, "fim": agora + d})
            agora += d
    return [{**p, "fala": t.personagem} for p in saida]


def gravar_legendas(trechos: list[Trecho], lugares: list[dict], verificacoes: dict[str, dict], brief: dict,
                    proporcao: str, dub: Path) -> int:
    """legendas.srt e legendas.ass da dublagem, no estilo das legendas do vídeo (legendas.* no pedido). Todo trecho é
    legendado, mesmo o das cartelas: o texto na tela delas está na língua original."""
    opcoes = SimpleNamespace(sem_destaque=False, maiusculas=False, palavras_por_legenda=None, legendar_cartelas=True)
    montar_video.opcoes_de_legenda(opcoes, brief)
    largura, altura = montar_video.RESOLUCOES.get(proporcao, (1920, 1080))
    cenas = [{"id": t.id, "tipo": "fala"} for t in trechos]
    palavras = [palavras_no_tempo(t, l, verificacoes.get(t.id)) for t, l in zip(trechos, lugares)]
    eventos = montar_video.gerar_legendas(cenas, palavras, opcoes, largura, altura)
    montar_video.escrever_ass(eventos, dub / "legendas.ass", largura, altura, opcoes)
    montar_video.escrever_srt(eventos, dub / "legendas.srt", opcoes.maiusculas)
    return len(eventos)


# ---------------------------------------------------------------- sintetizar


def cmd_sintetizar(args) -> int:
    idioma = idiomas.achar(args.idioma)
    if idioma is None:
        print(f"erro: língua desconhecida: {args.idioma}", file=sys.stderr)
        return 2
    pasta = args.pasta
    dub = pasta_da_dublagem(pasta, idioma.codigo)
    seg_doc = ler_json(dub / "segmentos.json")
    traducao = (ler_json(dub / "traducao.json") or {}).get("trechos") or {}
    if not seg_doc:
        print(f"erro: {dub / 'segmentos.json'} não existe; rode dublar.py segmentar antes", file=sys.stderr)
        return 2
    segmentos = seg_doc["segmentos"]
    sem_traducao = [s["id"] for s in segmentos if not str((traducao.get(s["id"]) or {}).get("texto") or "").strip()]
    velhas = [s["id"] for s in segmentos if s["id"] not in sem_traducao and traducao[s["id"]].get("original") != s["texto"]]
    if sem_traducao or velhas:
        if sem_traducao:
            print(f"erro: falta a tradução de {len(sem_traducao)} trecho(s): {', '.join(sem_traducao[:12])}", file=sys.stderr)
        if velhas:
            print(f"erro: a tradução de {len(velhas)} trecho(s) é de outra versão da narração: {', '.join(velhas[:12])}",
                  file=sys.stderr)
        return 2

    brief = ler_brief(pasta)
    nome_fornecedor = args.fornecedor or config(brief, "narracao.fornecedor", "gemini")
    if nome_fornecedor not in tts.FORNECEDORES:
        print(f"erro: fornecedor '{nome_fornecedor}' desconhecido (use {', '.join(sorted(tts.FORNECEDORES))})", file=sys.stderr)
        return 2
    classe = tts.classe(nome_fornecedor)

    def registrar_custo(uso: Uso) -> None:
        custos.registrar(pasta, "narracao", uso.modelo, uso.bruto, provedor=uso.provedor, caracteres=uso.caracteres,
                         segundos=uso.segundos, item=f"dublagem {idioma.codigo}")

    try:
        fornecedor = tts.criar(nome_fornecedor, args.modelo, ao_usar=registrar_custo)
    except ErroSintese as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2
    fornecedor.idioma = idioma.codigo
    modelo = fornecedor.modelo
    canal_path = narrar.ESPACO / "biblioteca" / "canal.json"
    canal = (ler_json(canal_path) or {}).get("narracao", {}) if canal_path.exists() else {}
    conf = {**canal, **{k: v for k, v in (brief.get("narracao") or {}).items() if v is not None}}
    voz_narrador = args.voz or conf.get("voz") or classe.voz_padrao
    estilo = args.estilo or conf.get("estilo") or narrar.config_padrao(brief)[1]
    narrador = (perfil_do_narrador(idioma), estilo)
    personagens = {narrar.chave(p["nome"]): p for p in brief.get("personagens") or [] if p.get("nome")}
    trechos, erros = planejar(segmentos, traducao, idioma, narrador, voz_narrador, personagens, brief, dub,
                              nome_fornecedor, modelo)
    if erros:
        for e in erros:
            print(f"erro: {e}", file=sys.stderr)
        return 2

    anterior = {t["id"]: t for t in (ler_json(dub / "dublagem.json") or {}).get("trechos") or []}
    a_gerar = [t for t in trechos if args.refazer or not t.arquivo.exists() or anterior.get(t.id, {}).get("pedido") != t.pedido]
    elenco = ", ".join(f"{p['nome']} ({p.get('voz')})" for p in personagens.values())
    print(f"dublagem em {idioma.nome} · {nome_fornecedor} · {modelo} · narrador {voz_narrador}"
          + (f" · personagens: {elenco}" if elenco else "") + f" · {len(a_gerar)} de {len(trechos)} trecho(s) a gerar",
          flush=True)

    tentativas = 1 if classe.deterministico else 3
    verificar = not args.sem_verificar and not classe.deterministico
    whisper, verificacao_indisponivel = None, False
    verificacoes: dict[str, dict] = {}

    def gerar(t: Trecho) -> tuple[Trecho, bytes | Exception, float]:
        inicio = time.time()
        try:
            wav, _, _ = narrar.sintetizar_verificado(fornecedor, t.texto, t.palavras, t.voz, t.perfil, t.estilo, tentativas)
            return t, wav, time.time() - inicio
        except ErroSintese as e:
            return t, e, time.time() - inicio

    pendentes, reprovados = a_gerar, []
    for rodada in range(1, MAX_RODADAS + 1):
        with ThreadPoolExecutor(max_workers=max(1, args.paralelo)) as pool:
            for t, wav, gasto in pool.map(gerar, pendentes):
                if isinstance(wav, SemCota):
                    print(f"erro: {narrar.mensagem_sem_cota(nome_fornecedor, wav)}", file=sys.stderr)
                    return 3
                if isinstance(wav, Exception):
                    print(f"erro em {t.id}: {wav}", file=sys.stderr)
                    return 1
                t.arquivo.parent.mkdir(parents=True, exist_ok=True)
                t.arquivo.write_bytes(wav)
                anterior.pop(t.id, None)  # a verificação antiga era de outro áudio
                print(f"  ok {t.id} · {t.personagem or 'narrador'} ({t.voz}) · {gasto:.0f} s", flush=True)
        if not verificar:
            break
        a_conferir = [t for t in trechos if t.id not in verificacoes and not (anterior.get(t.id) or {}).get("verificacao")]
        for t in trechos:
            if t.id not in verificacoes and (anterior.get(t.id) or {}).get("verificacao"):
                verificacoes[t.id] = anterior[t.id]["verificacao"]
        if a_conferir and whisper is None:
            whisper = carregar_whisper(args.modelo_whisper)
            if whisper is None:
                verificacao_indisponivel = True
                print("erro: a verificação por transcrição não rodou (instale o faster-whisper)", file=sys.stderr)
                break
        for t in a_conferir:
            params, quadros = narrar.ler_wav(t.arquivo)
            verificacoes[t.id] = conferir(whisper, t, narrar.aparar(quadros, params), params, idioma)
            r = verificacoes[t.id]
            print(f"  verificação {t.id}: cobertura {r['cobertura_pct']}% · {r['palavras_extras']} extra(s) · "
                  f"{'ok' if r['ok'] else 'com defeito'}", flush=True)
        reprovados = [t for t in trechos if not verificacoes.get(t.id, {}).get("ok", True)]
        if not reprovados or rodada == MAX_RODADAS:
            break
        print(f"  refazendo {', '.join(t.id for t in reprovados)} (rodada {rodada + 1} de {MAX_RODADAS})", flush=True)
        for t in reprovados:
            verificacoes.pop(t.id, None)
        pendentes = reprovados

    # Encaixe: cada trecho, sem o silêncio das pontas, no tempo da frase original.
    aparados = {}
    for t in trechos:
        params, quadros = narrar.ler_wav(t.arquivo)
        aparados[t.id] = (params, narrar.aparar(quadros, params))
    naturais = {i: len(q) / (p.sampwidth * p.nchannels) / p.framerate for i, (p, q) in aparados.items()}
    duracao = float(seg_doc["duracao_video_s"])
    lugares = encaixar(segmentos, naturais, duracao)
    total = round(duracao * TAXA)
    pista = array("h", bytes(2 * total))
    try:
        for t, lugar in zip(trechos, lugares):
            params, quadros = aparados[t.id]
            amostras = array("h")
            amostras.frombytes(no_formato(quadros, params, lugar["velocidade"]))
            ini = min(total, round(lugar["inicio_s"] * TAXA))
            fim = min(total, ini + len(amostras))
            pista[ini:fim] = amostras[:fim - ini]
            lugar["duracao_s"] = round((fim - ini) / TAXA, 3)
    except ErroSintese as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    gravar_wav(dub / "dublagem.wav", pista.tobytes())
    n_legendas = gravar_legendas(trechos, lugares, verificacoes, brief, seg_doc.get("proporcao", "16:9"), dub)

    por_id = {s["id"]: s for s in segmentos}
    encurtar = [pedido_de_encurtar(por_id[l["id"]], t.texto, l) for t, l in zip(trechos, lugares) if not l["cabe"]]
    medidas = [len(t.texto) / naturais[t.id] for t in trechos if naturais[t.id] >= 0.8]
    cps = round(statistics.median(medidas), 2) if medidas else None
    avisos = [a for t in reprovados for a in verificacoes.get(t.id, {}).get("alertas", [])]
    forcados = [l for l in lugares if l["velocidade"] > VEL_MAX or l["atraso_s"] > 0.3]
    if forcados:
        pior = max(forcados, key=lambda l: l["velocidade"])
        avisos.append(f"{len(forcados)} trecho(s) acima de {VEL_MAX}x ou atrasados em relação à fala original "
                      f"(o mais rápido: {pior['id']} a {pior['velocidade']}x)")
    if verificacao_indisponivel:
        avisos.append("os trechos não foram conferidos por transcrição (a verificação não rodou)")
    registros = [{**l, "pedido": t.pedido, "personagem": t.personagem, "voz": t.voz, "texto": t.texto,
                  "verificacao": verificacoes.get(t.id)} for t, l in zip(trechos, lugares)]
    estado = {"idioma": idioma.codigo, "nome": idioma.nome, "fornecedor": nome_fornecedor, "modelo": modelo,
              "voz": voz_narrador, "perfil": narrador[0], "estilo": narrador[1],
              "personagens": {p["nome"]: p.get("voz") for p in personagens.values()},
              "arquivo": f"{edicao.DUBLAGENS}/{idioma.codigo}/dublagem.wav", "duracao_s": duracao, "taxa": TAXA,
              "cps": cps, "entradas": seg_doc.get("entradas") or {}, "verificada": verificar and not verificacao_indisponivel,
              "trechos": registros, "encurtar": encurtar, "reprovados": [t.id for t in reprovados], "avisos": avisos,
              "gerada_em": time.strftime("%Y-%m-%dT%H:%M:%S")}
    gravar_json(dub / "dublagem.json", estado)

    velocidades = [l["velocidade"] for l in lugares]
    print(f"dublagem: {dub / 'dublagem.wav'} · {duracao:.1f} s (a duração do vídeo) · {len(trechos)} trechos · "
          f"velocidade de {min(velocidades):.2f}x a {max(velocidades):.2f}x" + (f" · voz a {cps:.1f} caracteres/s" if cps else ""))
    print(f"legendas: {n_legendas} ({dub / 'legendas.srt'} e legendas.ass)")
    if encurtar:
        print(f"encurtar: {len(encurtar)} trecho(s) não cabem no tempo da frase original nem a {VEL_MAX}x: "
              f"{', '.join(e['id'] for e in encurtar[:15])} (dublagem.json, encurtar)")
    for a in avisos:
        print(f"aviso: {a}")
    return 1 if encurtar or reprovados or verificacao_indisponivel else 0


# ---------------------------------------------------------------- principal


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="comando", required=True)
    seg = sub.add_parser("segmentar", help="divide a narração alinhada em trechos com tempo e limite de caracteres")
    seg.add_argument("pasta", type=Path)
    seg.add_argument("--idioma", required=True, help="código da língua (veja: dublar.py idiomas)")
    sin = sub.add_parser("sintetizar", help="fala a tradução, encaixa no tempo do vídeo e grava a faixa e as legendas")
    sin.add_argument("pasta", type=Path)
    sin.add_argument("--idioma", required=True)
    sin.add_argument("--fornecedor", choices=sorted(tts.FORNECEDORES), help="padrão: narracao.fornecedor no pedido")
    sin.add_argument("--modelo", help="modelo de voz (padrão: o do fornecedor)")
    sin.add_argument("--voz", help="voz do narrador (padrão: a da narração original)")
    sin.add_argument("--estilo", help="como o narrador fala (padrão: o do pedido)")
    sin.add_argument("--refazer", action="store_true", help="gera de novo todos os trechos")
    sin.add_argument("--paralelo", type=int, default=4, help="trechos gerados ao mesmo tempo")
    sin.add_argument("--sem-verificar", action="store_true", help="não transcreve os trechos para conferir")
    sin.add_argument("--modelo-whisper", help="modelo do whisper (padrão: o configurado, senão small)")
    sub.add_parser("idiomas", help="lista as línguas")
    args = ap.parse_args()
    if args.comando == "idiomas":
        for i in idiomas.IDIOMAS.values():
            print(f"{i.codigo:6} {i.nome:24} {i.lingua}")
        return 0
    if args.comando == "segmentar":
        return cmd_segmentar(args)
    return cmd_sintetizar(args)


if __name__ == "__main__":
    sys.exit(main())
