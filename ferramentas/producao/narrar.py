#!/usr/bin/env python3
"""Gera a narração de um projeto, bloco a bloco, com um fornecedor de voz sintética.

Lê projetos/<slug>/06-roteiro-final.md e 00-brief.json (vozes, estilo e tom do pedido) e grava:

  09-narracao/blocos/<bloco>.wav     a narração de cada bloco do roteiro
  09-narracao/falas/<bloco>-Fnn.wav  cada fala dos blocos com diálogo, na voz de quem fala
  09-narracao/narracao.wav           os blocos emendados, com um intervalo curto entre eles
  09-narracao/blocos.json            vozes, modelo, início e duração de cada bloco e fala (usado por alinhar.py)

Diálogo: no roteiro, a linha que começa com [FALA: Nome] (ou [FALA: Nome, como dizer]) é do personagem; o
resto é do narrador. A voz e o jeito de falar de cada personagem vêm de personagens, no pedido. O bloco com
diálogo é gerado fala a fala, cada uma na sua voz, e as falas são emendadas no áudio do bloco; um [PAUSA]
entre duas falas vira silêncio. O bloco só com narrador sai numa chamada só.

Gerar por bloco permite refazer só um trecho e respeita o limite de áudio por chamada. Blocos e falas já
gerados são pulados, a não ser que o texto, a voz ou o estilo tenham mudado, ou que se use --refazer.

Velocidade: a voz fala no ritmo dela. Na emenda, a fala inteira é acelerada ou desacelerada pelo mesmo fator
(atempo do ffmpeg, sem mudar o tom) até narracao.palavras_por_minuto do pedido (ou --ppm), entre 0,8x e 1,35x.
Mudar a velocidade não gera nada de novo: só refaz a emenda.

Fornecedores (o padrão vem de narracao.fornecedor no pedido; os de nucleo/provedores/voz/). As chaves vêm de
nucleo.chaves (ambiente, depois as cadastradas em Configurações > Chaves de API):
  gemini      Gemini TTS: vozes naturais, pago. O perfil e o estilo vão num prompt estruturado.
  openai      TTS da OpenAI (gpt-4o-mini-tts): o perfil e o estilo vão como instruções.
  elevenlabs  ElevenLabs: as vozes da conta (nome ou voice_id), sem instrução de estilo.
  espeak      espeak-ng, local e grátis: voz robótica, para testar ritmo, falas, alinhamento e montagem antes de
              pagar. Cada voz do Gemini do pedido vira uma variante do espeak do mesmo gênero, sempre a mesma.
Uma voz que não existe no fornecedor escolhido (a voz do canal é do Gemini) vira uma voz dele do mesmo gênero,
sempre a mesma: dá para trocar de fornecedor sem mexer no pedido.

Uso:
    python3 ferramentas/producao/narrar.py projetos/<slug> --testar-vozes Charon,Kore,Algenib
    python3 ferramentas/producao/narrar.py projetos/<slug> --testar-vozes Leda,Kore,Aoede --personagem Moranguete
    python3 ferramentas/producao/narrar.py projetos/<slug>
    python3 ferramentas/producao/narrar.py projetos/<slug> --fornecedor espeak
    python3 ferramentas/producao/narrar.py projetos/<slug> --blocos B03 --refazer
    python3 ferramentas/producao/narrar.py --listar-vozes
    python3 ferramentas/producao/narrar.py --amostras            # ~5 s de cada voz em biblioteca/vozes/
    python3 ferramentas/producao/narrar.py --amostras Kore,Leda --refazer

Se o pedido desligou a narração (narracao.ativa: não), não gera nada, a não ser com --forcar.

Cada trecho gerado é conferido por transcrição (alinhar.py --verificar, com o faster-whisper): se a voz
repetiu o texto, leu a instrução de estilo ou pulou um trecho, ele é gerado de novo, até 3 rodadas. Se a
verificação não puder rodar (sem o faster-whisper, por exemplo), a narração é gravada mesmo assim, com um aviso,
e o script sai com código 1: quem chamou decide. Depois de narrar, rode alinhar.py para medir o tempo real de
cada palavra.

Códigos de saída: 0 ok; 1 erro de síntese, trechos ainda com defeito ou verificação indisponível; 2 sem roteiro,
fornecedor desconhecido, sem chave ou personagem sem voz; 3 sem cota no fornecedor.
"""

from __future__ import annotations

import argparse
from array import array
import hashlib
import io
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "ferramentas"))
from brief import config, ler_brief, narracao_ativa, ppm_do_brief  # noqa: E402
import custos  # noqa: E402
from medir_roteiro import PPM_PADRAO, SEGUNDOS_POR_PAUSA, ler_roteiro, sem_acento  # noqa: E402
import nucleo.provedores.voz as tts  # noqa: E402
from nucleo.provedores.erros import ErroProvedor, SemCota  # noqa: E402
from nucleo.provedores.uso import Uso  # noqa: E402
from nucleo.provedores.voz.base import duracao_bytes  # noqa: E402

# Os dados (biblioteca/, projetos/) ficam no espaço de trabalho: a raiz do repositório, ou APP_RAIZ.
ESPACO = Path(os.environ.get("APP_RAIZ") or RAIZ).resolve()
PPM_REFERENCIA = 150
INTERVALO_PADRAO_S = 0.35
INTERVALO_FALAS_S = 0.25
MAX_RODADAS = 3
# Ajuste do ritmo da fala até narracao.palavras_por_minuto: abaixo de 3% de diferença não mexe; fora de 0,8x a 1,35x
# o atempo começa a soar artificial.
TOLERANCIA_RITMO = 0.03
VELOCIDADE_MIN, VELOCIDADE_MAX = 0.8, 1.35  # gerar e verificar; refazer até duas vezes os trechos com defeito
ALINHADOR = RAIZ / "ferramentas" / "producao" / "alinhar.py"
FORNECEDORES = tts.FORNECEDORES
ErroSintese = ErroProvedor  # erros de síntese e de emenda: o mesmo tratamento

# As funções de voz moram em nucleo/provedores/voz; ficam visíveis aqui para quem importava daqui.
VOZES_GEMINI, GENERO_VOZES, para_wav, voz_equivalente = tts.VOZES_GEMINI, tts.GENERO_VOZES, tts.para_wav, tts.voz_equivalente


def mensagem_sem_cota(fornecedor: str, e: Exception) -> str:
    if fornecedor == "gemini":
        return f"o projeto do Google não tem cota de TTS ({e}). Ative o faturamento."
    nome = tts.classe(fornecedor).nome if fornecedor in FORNECEDORES else fornecedor
    return f"a conta de {nome} está sem cota ou sem crédito ({e})"


def vozes_do(fornecedor: str, instancia=None) -> dict[str, tts.Voz]:
    """As vozes do fornecedor por nome: as da instância (ElevenLabs: as da conta) ou as do catálogo."""
    lista = instancia.vozes() if instancia is not None else tts.catalogo(fornecedor)
    return {v.nome: v for v in lista}


# ---------------------------------------------------------------- áudio


def ler_wav(caminho: Path) -> tuple[wave._wave_params, bytes]:
    with wave.open(str(caminho), "rb") as w:
        return w.getparams(), w.readframes(w.getnframes())


def duracao_wav(caminho: Path) -> float:
    params, _ = ler_wav(caminho)
    return params.nframes / params.framerate


def aparar(quadros: bytes, params, limiar: float = 0.006, margem_s: float = 0.06) -> bytes:
    """Tira o silêncio do começo e do fim de um áudio PCM de 16 bits. As falas curtas vêm com ~0,3 s de cada
    lado, que somados ao intervalo entre falas deixam o diálogo arrastado."""
    if params.sampwidth != 2:
        return quadros
    amostras, teto, canais = array("h", quadros), int(limiar * 32768), params.nchannels
    inicio = next((i for i in range(len(amostras)) if abs(amostras[i]) > teto), None)
    if inicio is None:
        return quadros
    fim = next(i for i in range(len(amostras) - 1, -1, -1) if abs(amostras[i]) > teto) + 1
    margem = int(margem_s * params.framerate) * canais
    inicio = max(0, inicio - margem) // canais * canais
    fim = min(len(amostras), -(-(fim + margem) // canais) * canais)
    return amostras[inicio:fim].tobytes()


def encurtar_pausas(quadros: bytes, params, maximo_s: float, limiar: float = 0.006, janela_s: float = 0.01) -> bytes:
    """Encurta para maximo_s as pausas internas mais longas que isso (as reticências que a voz estica), guardando
    metade do silêncio de cada lado para não cortar o fim nem o começo das palavras."""
    if params.sampwidth != 2:
        return quadros
    amostras, canais = array("h", quadros), params.nchannels
    passo = max(1, int(janela_s * params.framerate)) * canais
    teto, manter = int(limiar * 32768), int(maximo_s * params.framerate) // 2 * canais
    saida, inicio_pausa = array("h"), None
    for i in range(0, len(amostras), passo):
        janela = amostras[i:i + passo]
        if max(janela) <= teto and min(janela) >= -teto:
            if inicio_pausa is None:
                inicio_pausa = i
            continue
        if inicio_pausa is not None:
            pausa = amostras[inicio_pausa:i]
            saida.extend(pausa if len(pausa) <= 2 * manter else pausa[:manter] + pausa[len(pausa) - manter:])
            inicio_pausa = None
        saida.extend(janela)
    if inicio_pausa is not None:  # o silêncio do fim fica com aparar()
        saida.extend(amostras[inicio_pausa:])
    return saida.tobytes()


def no_ritmo(quadros: bytes, params, velocidade: float) -> bytes:
    """A fala mais rápida (ou mais lenta) sem mudar o tom da voz, com o atempo do ffmpeg."""
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", str(params.framerate), "-ac", str(params.nchannels),
                        "-i", "pipe:0", "-filter:a", f"atempo={velocidade}", "-f", "s16le", "pipe:1"],
                       input=quadros, capture_output=True)
    if r.returncode:
        raise ErroSintese(f"ffmpeg falhou ao mudar o ritmo: {r.stderr.decode(errors='replace').strip()[-200:]}")
    return r.stdout


def ritmo_natural(plano: list) -> float | None:
    """Palavras por minuto em que a voz falou, medidas como cada trecho entra na narração (as falas dos diálogos
    sem o silêncio das pontas, os blocos só de narrador inteiros). Um trecho muito fora da mediana (a voz repetiu
    ou acrescentou texto) fica fora da conta."""
    medidas = []
    for b, trechos in plano:
        inteiro = len(trechos) == 1 and trechos[0].id == b.id
        for t in trechos:
            params, quadros = ler_wav(t.arquivo)
            if not inteiro:
                quadros = aparar(quadros, params)
            segundos = len(quadros) / (params.sampwidth * params.nchannels) / params.framerate
            if segundos and t.palavras:
                medidas.append((t.palavras, segundos))
    if not medidas:
        return None
    mediana = statistics.median(p / s for p, s in medidas)
    validas = [(p, s) for p, s in medidas if 0.7 * mediana <= p / s <= 1.4 * mediana] or medidas
    return sum(p for p, _ in validas) / sum(s for _, s in validas) * 60


def velocidade_para(alvo: int, natural: float | None) -> tuple[float, str | None]:
    """O fator do atempo que leva a fala do ritmo natural ao pedido, dentro da faixa em que ela ainda soa natural."""
    if not natural:
        return 1.0, None
    fator = alvo / natural
    if abs(fator - 1) < TOLERANCIA_RITMO:
        return 1.0, None
    limitado = min(VELOCIDADE_MAX, max(VELOCIDADE_MIN, fator))
    aviso = None
    if limitado != fator:
        sentido = "mais rápido" if fator > 1 else "mais devagar"
        aviso = (f"a voz fala a {natural:.0f} ppm; chegar a {alvo} pedia {fator:.2f}x, além da faixa em que a fala soa natural "
                 f"({VELOCIDADE_MIN}x a {VELOCIDADE_MAX}x). Ficou em {limitado}x ({natural * limitado:.0f} ppm); para chegar lá, "
                 f"peça um ritmo {sentido} em narracao.estilo e narre de novo com --refazer")
    return round(limitado, 3), aviso


def emendar(arquivos: list[Path], destino: Path, intervalos: float | list[float], aparar_silencio: bool = False,
            pausa_max: float | None = None, velocidade: float = 1.0) -> list[tuple[float, float]]:
    """Junta os WAVs com silêncio entre eles (um intervalo só, ou um por emenda), tirando antes o silêncio das
    pontas se pedido, encurtando as pausas internas longas (pausa_max) e mudando o ritmo da fala (velocidade).
    Devolve (início, duração) de cada um no arquivo final."""
    params0, _ = ler_wav(arquivos[0])
    if not isinstance(intervalos, list):
        intervalos = [intervalos] * (len(arquivos) - 1)
    posicoes, t = [], 0.0
    destino.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destino), "wb") as saida:
        saida.setnchannels(params0.nchannels)
        saida.setsampwidth(params0.sampwidth)
        saida.setframerate(params0.framerate)
        for i, arq in enumerate(arquivos):
            params, quadros = ler_wav(arq)
            if aparar_silencio:
                quadros = aparar(quadros, params)
            if (params.nchannels, params.sampwidth, params.framerate) != (params0.nchannels, params0.sampwidth, params0.framerate):
                raise ErroSintese(f"{arq.name} tem formato diferente dos outros trechos (gerados por outro fornecedor?); "
                                  "narre de novo com --refazer")
            if pausa_max:
                quadros = encurtar_pausas(quadros, params, pausa_max)
            if velocidade != 1.0:
                quadros = no_ritmo(quadros, params, velocidade)
            if i:
                silencio = int(params0.framerate * intervalos[i - 1])
                saida.writeframes(b"\x00" * (silencio * params0.sampwidth * params0.nchannels))
                t += silencio / params0.framerate
            saida.writeframes(quadros)
            dur = len(quadros) / (params.sampwidth * params.nchannels) / params.framerate
            posicoes.append((round(t, 3), round(dur, 3)))
            t += dur
    return posicoes


# ---------------------------------------------------------------- roteiro e vozes


@dataclass
class Trecho:
    """O que se pede à voz numa chamada: o bloco inteiro, quando só o narrador fala nele, ou uma fala."""
    id: str                 # "B03" (o bloco inteiro) ou "B03-F02" (a fala 2 do bloco B03)
    bloco: str
    arquivo: Path
    texto: str
    palavras: int
    inicio: int             # índice da primeira palavra no bloco
    personagem: str | None  # None: o narrador
    voz: str
    perfil: str
    estilo: str
    pedido: str = ""


def chave(nome: str | None) -> str:
    return sem_acento(nome or "").strip()


def texto_de(bloco, inicio: int, fim: int) -> str:
    """As palavras [inicio, fim) do bloco com a pontuação original. Cada [PAUSA] no meio vira reticências; a de
    uma emenda entre falas vira silêncio (veja intervalos_das_falas)."""
    palavras = list(bloco.tokens[inicio:fim])
    for p in sorted(bloco.pausas, reverse=True):
        if inicio < p < fim or p == fim == len(bloco.tokens):
            palavras[p - inicio - 1] = palavras[p - inicio - 1].rstrip(".,;:") + "…"
    return " ".join(palavras)


def texto_do_bloco(bloco) -> str:
    return texto_de(bloco, 0, len(bloco.tokens))


def intervalos_das_falas(bloco, trechos: list[Trecho], base: float) -> list[float]:
    return [base + (SEGUNDOS_POR_PAUSA * 0.75 if t.inicio in bloco.pausas else 0.0) for t in trechos[1:]]


def config_padrao(brief: dict) -> tuple[str, str]:
    perfil = "Narrador brasileiro de vídeos para o YouTube. Português do Brasil, sotaque neutro, dicção clara."
    estilo = (f"Tom: {brief.get('tom') or 'envolvente'}. Fale como quem conta algo surpreendente para uma pessoa só. "
              "Respeite a pontuação: pausa curta nas vírgulas, pausa maior nos pontos e nas reticências. "
              "Leia os números como se fala.")
    return perfil, estilo


def config_personagem(p: dict, direcao: str, brief: dict,
                      lingua: str = "português do Brasil, sotaque neutro") -> tuple[str, str]:
    """Perfil e notas de direção da voz de um personagem, a partir de personagens no pedido. `lingua` muda na
    dublagem (dublar.py); o texto de sempre não muda, porque o cache da narração depende dele."""
    perfil = f"{p['nome']}, personagem de uma história contada em vídeo para o YouTube."
    if p.get("descricao"):
        perfil += f" {p['descricao'].rstrip('.')}."
    perfil += f" Fala em {lingua}."
    notas = [f"Jeito de falar: {p['estilo'].rstrip('.')}." if p.get("estilo") else "",
             f"Nesta fala: {direcao.rstrip('.')}." if direcao else "",
             f"Tom do vídeo: {brief['tom'].rstrip('.')}." if brief.get("tom") else "",
             "Interprete a fala com emoção, no ritmo de um diálogo, sem arrastar. Respeite a pontuação."]
    return perfil, " ".join(n for n in notas if n)


def planejar(blocos, pasta: Path, narrador: tuple[str, str, str], personagens: dict[str, dict], brief: dict,
             fornecedor: str, modelo: str) -> tuple[list[tuple[object, list[Trecho]]], list[str]]:
    """Os trechos de cada bloco: um só, se o bloco é todo do narrador; senão, um por fala."""
    plano, erros = [], []
    for b in blocos:
        if len(b.falas) == 1 and b.falas[0].personagem is None and not b.falas[0].direcao:
            t = Trecho(b.id, b.id, pasta / "blocos" / f"{b.id}.wav", texto_do_bloco(b), len(b.tokens), 0, None, *narrador)
            t.pedido = hash_pedido(fornecedor, modelo, t.voz, t.perfil, t.estilo, t.texto)
            plano.append((b, [t]))
            continue
        trechos = []
        for n, f in enumerate(b.falas, 1):
            if f.personagem is None:
                voz, perfil, estilo = narrador
                if f.direcao:
                    estilo += f" Nesta fala: {f.direcao.rstrip('.')}."
            else:
                p = personagens.get(chave(f.personagem))
                if p is None:
                    erros.append(f"{f.personagem} fala no roteiro ({b.id}) mas não está em personagens no pedido")
                    continue
                if not p.get("voz"):
                    erros.append(f"{p['nome']} está em personagens no pedido, mas sem voz")
                    continue
                voz = p["voz"]
                perfil, estilo = config_personagem(p, f.direcao, brief)
            fid = f"{b.id}-F{n:02d}"
            t = Trecho(fid, b.id, pasta / "falas" / f"{fid}.wav", texto_de(b, f.inicio, f.fim), f.fim - f.inicio,
                       f.inicio, f.personagem, voz, perfil, estilo)
            t.pedido = hash_pedido(fornecedor, modelo, voz, perfil, estilo, t.texto)
            trechos.append(t)
        plano.append((b, trechos))
    return plano, sorted(set(erros))


def hash_pedido(*partes: object) -> str:
    return hashlib.sha1(json.dumps(partes, ensure_ascii=False).encode()).hexdigest()[:12]


def sintetizar_verificado(fornecedor, texto: str, palavras: int, voz: str, perfil: str, estilo: str,
                          tentativas: int = 3) -> tuple[bytes, str | None, int]:
    """Sintetiza e confere a duração. A voz às vezes repete o texto ou lê a instrução (áudio longo demais) ou
    corta o fim (curto demais); nesses casos, tenta de novo e fica com o resultado mais próximo do esperado."""
    esperado = palavras / PPM_REFERENCIA * 60
    melhor = None
    for n in range(1, tentativas + 1):
        wav = fornecedor.sintetizar(texto, voz, perfil, estilo)
        aviso = aviso_de_duracao(palavras, duracao_bytes(wav))
        if aviso is None:
            return wav, None, n
        distancia = abs(duracao_bytes(wav) - esperado)
        if melhor is None or distancia < melhor[0]:
            melhor = (distancia, wav, aviso)
    return melhor[1], melhor[2], tentativas


def verificar(pasta: Path, itens: list[str], modelo_whisper: str | None = None) -> list[dict] | None:
    """Confere os áudios transcrevendo-os (alinhar.py --verificar, no mesmo Python, com o whisper carregado uma
    vez por chamada). Devolve um resultado por item, na ordem, ou None se a verificação não puder rodar (sem o
    faster-whisper, por exemplo): quem chama avisa e sai com código 1, nunca pula em silêncio."""
    comando = [sys.executable, str(ALINHADOR), str(pasta), "--verificar", ",".join(itens)]
    if modelo_whisper:
        comando += ["--modelo", modelo_whisper]
    r = subprocess.run(comando, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(RAIZ), os.environ.get("PYTHONPATH")]))})
    linhas = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
    if len(linhas) != len(itens):
        print(f"erro: a verificação por transcrição não rodou ({(r.stderr or '').strip().splitlines()[-1:] or ['sem saída']})",
              file=sys.stderr)
        return None
    return linhas


def aviso_de_duracao(palavras: int, duracao: float) -> str | None:
    esperado = palavras / PPM_REFERENCIA * 60
    if duracao > esperado * 1.8 + 2:
        return f"áudio de {duracao:.1f} s para {palavras} palavras (esperado ~{esperado:.0f} s): a voz pode ter lido texto a mais. Confira com alinhar.py"
    if duracao < esperado * 0.45:
        return f"áudio de {duracao:.1f} s para {palavras} palavras (esperado ~{esperado:.0f} s): a voz pode ter pulado texto"
    return None


def quem(t: Trecho) -> str:
    return f"{t.personagem or 'narrador'} ({t.voz})"


# ---------------------------------------------------------------- amostras das vozes

AMOSTRAS = ESPACO / "biblioteca" / "vozes"
TEXTO_AMOSTRA = "Oi, eu sou a voz {voz}. É assim que eu vou contar a sua próxima história."
PERFIL_AMOSTRA = "Narrador brasileiro de vídeos para o YouTube. Português do Brasil, sotaque neutro, dicção clara."
ESTILO_AMOSTRA = "Tom natural e envolvente, como quem começa a contar uma história para uma pessoa só."


def gravar_amostras(nome_fornecedor: str, modelo: str | None, vozes: list[str], refazer: bool) -> int:
    """Uma frase curta (~5 s) em cada voz, com o mesmo texto e a mesma direção, para comparar pelo ouvido na
    hora de escolher. Grava biblioteca/vozes/<Voz>.mp3 e amostras.json; pula o que já foi gravado igual."""
    fornecedor = tts.criar(nome_fornecedor, modelo, ao_usar=None)
    conhecidas = vozes_do(nome_fornecedor, fornecedor)
    AMOSTRAS.mkdir(parents=True, exist_ok=True)
    indice_path = AMOSTRAS / "amostras.json"
    indice = json.loads(indice_path.read_text(encoding="utf-8")) if indice_path.exists() else {}
    falhas = []
    for v in vozes:
        texto = TEXTO_AMOSTRA.format(voz=v)
        assinatura = hash_pedido(fornecedor.modelo, v, PERFIL_AMOSTRA, ESTILO_AMOSTRA, texto)
        destino = AMOSTRAS / f"{v}.mp3"
        if not refazer and destino.exists() and indice.get(v, {}).get("pedido") == assinatura:
            print(f"  {v}: já gravada")
            continue
        try:
            wav, aviso, _ = sintetizar_verificado(fornecedor, texto, len(texto.split()), v, PERFIL_AMOSTRA, ESTILO_AMOSTRA)
        except SemCota as e:
            print(f"erro: {mensagem_sem_cota(nome_fornecedor, e)}", file=sys.stderr)
            return 3
        except ErroSintese as e:
            print(f"  erro {v}: {e}")
            falhas.append(v)
            continue
        with wave.open(io.BytesIO(wav), "rb") as w:
            params, quadros = w.getparams(), w.readframes(w.getnframes())
        quadros = aparar(quadros, params)
        with tempfile.TemporaryDirectory() as tmp:
            bruto = Path(tmp) / "amostra.wav"
            with wave.open(str(bruto), "wb") as w:
                w.setparams(params)
                w.writeframes(quadros)
            r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(bruto), "-af", "loudnorm=I=-16:TP=-1.5",
                                "-ar", "24000", "-ac", "1", "-b:a", "64k", str(destino)], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  erro {v}: ffmpeg: {(r.stderr or '').strip()[:200]}")
            falhas.append(v)
            continue
        duracao = len(quadros) / (params.sampwidth * params.nchannels * params.framerate)
        indice[v] = {"arquivo": destino.name, "texto": texto, "modelo": fornecedor.modelo, "duracao_s": round(duracao, 2),
                     "pedido": assinatura}
        indice_path.write_text(json.dumps(indice, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        info = conhecidas.get(v)
        print(f"  {v} ({info.genero if info else '?'}, {info.descricao if info else '?'}): {duracao:.1f} s"
              f"{' · ' + aviso if aviso else ''} → {destino.relative_to(ESPACO)}")
    return 1 if falhas else 0


# ---------------------------------------------------------------- principal


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pasta", type=Path, nargs="?", help="pasta do projeto (projetos/<slug>)")
    ap.add_argument("--fornecedor", choices=sorted(FORNECEDORES), help="padrão: narracao.fornecedor no pedido, senão gemini")
    ap.add_argument("--modelo", help="modelo de voz (padrão do Gemini: gemini-3.8-flash-tts)")
    ap.add_argument("--voz", help="voz do narrador (padrão: a do brief, depois a de biblioteca/canal.json, depois Charon)")
    ap.add_argument("--perfil", help="quem narra (vai para o prompt de direção)")
    ap.add_argument("--estilo", help="como o narrador fala: tom, ritmo, emoção (padrão: derivado do tom do brief)")
    ap.add_argument("--blocos", help="só estes blocos, separados por vírgula (ex.: B01,B03)")
    ap.add_argument("--refazer", action="store_true", help="gera de novo mesmo o que já existe")
    ap.add_argument("--intervalo", type=float, default=INTERVALO_PADRAO_S, help="silêncio entre blocos, em segundos")
    ap.add_argument("--intervalo-falas", type=float, default=INTERVALO_FALAS_S, help="silêncio entre falas, em segundos")
    ap.add_argument("--pausa-max", type=float,
                    help="encurta para N segundos as pausas dentro de cada fala (ex.: 0.3); vale na emenda, sem gerar de novo")
    ap.add_argument("--ppm", type=int,
                    help="palavras por minuto da fala (padrão: narracao.palavras_por_minuto do pedido, ou 150): a fala é "
                         "acelerada ou desacelerada até chegar nele, sem mudar o tom; vale na emenda, sem gerar de novo")
    ap.add_argument("--velocidade", type=float,
                    help="fator fixo do ritmo da fala (1.1 = 10%% mais rápido), no lugar do --ppm")
    ap.add_argument("--testar-vozes", help="gera a primeira fala do narrador (ou do --personagem) com cada voz da lista")
    ap.add_argument("--personagem", help="com --testar-vozes: testa as vozes na primeira fala deste personagem")
    ap.add_argument("--listar-vozes", action="store_true")
    ap.add_argument("--amostras", nargs="?", const="", metavar="VOZES",
                    help="grava uma frase de ~5 s em cada voz (ou só nas da lista) em biblioteca/vozes/, para a escolha da voz")
    ap.add_argument("--simular", action="store_true", help="mostra o que seria gerado, sem chamar a API")
    ap.add_argument("--sem-verificar", action="store_true", help="não transcreve os áudios para conferir o texto")
    ap.add_argument("--modelo-whisper", help="modelo do whisper da verificação (padrão: o configurado, senão small)")
    ap.add_argument("--forcar", action="store_true", help="narra mesmo que o pedido tenha desligado a narração")
    args = ap.parse_args()

    if args.listar_vozes:
        nome = args.fornecedor or "gemini"
        lista = tts.catalogo(nome)
        if nome == "elevenlabs":  # a lista vem da conta: atualiza antes de mostrar, se houver chave
            try:
                lista = tts.criar(nome).atualizar()
            except ErroSintese as e:
                print(f"aviso: {e}; usando a lista guardada", file=sys.stderr)
        for v in lista:
            print(f"{v.nome:15} {v.genero}  {v.descricao}")
        return 0
    if args.amostras is not None:
        nome = args.fornecedor or "gemini"
        vozes = [x.strip() for x in args.amostras.split(",") if x.strip()] or [v.nome for v in tts.catalogo(nome)]
        try:
            return gravar_amostras(nome, args.modelo, vozes, args.refazer)
        except ErroSintese as e:
            print(f"erro: {e}", file=sys.stderr)
            return 2
    if not args.pasta:
        ap.error("informe a pasta do projeto")

    roteiro = args.pasta / "06-roteiro-final.md"
    if not roteiro.exists():
        print(f"erro: {roteiro} não existe", file=sys.stderr)
        return 2
    _, blocos, _ = ler_roteiro(roteiro)
    brief = ler_brief(args.pasta)
    if not narracao_ativa(brief) and not (args.forcar or args.testar_vozes):
        print("o pedido desligou a narração (narracao.ativa: não); nada a narrar. Use --forcar para narrar mesmo assim.")
        return 0
    nome_fornecedor = args.fornecedor or config(brief, "narracao.fornecedor", "gemini")
    if nome_fornecedor not in FORNECEDORES:
        print(f"erro: fornecedor '{nome_fornecedor}' desconhecido (use {', '.join(sorted(FORNECEDORES))})", file=sys.stderr)
        return 2
    classe = tts.classe(nome_fornecedor)
    try:
        fornecedor = None if args.simular else tts.criar(nome_fornecedor, args.modelo, ao_usar=None)
    except ErroSintese as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2
    conhecidas = vozes_do(nome_fornecedor, fornecedor)
    canal_path = ESPACO / "biblioteca" / "canal.json"
    canal = json.loads(canal_path.read_text(encoding="utf-8")).get("narracao", {}) if canal_path.exists() else {}
    do_brief = {k: v for k, v in (brief.get("narracao") or {}).items() if v is not None}
    conf = {**canal, **do_brief}  # o brief do projeto tem prioridade sobre o padrão do canal
    perfil_padrao, estilo_padrao = config_padrao(brief)
    voz = args.voz or conf.get("voz") or classe.voz_padrao or next(iter(conhecidas), None)
    if not voz:
        print(f"erro: {nome_fornecedor} não tem nenhuma voz disponível; escolha uma com --voz", file=sys.stderr)
        return 2
    perfil = args.perfil or conf.get("perfil") or perfil_padrao
    estilo = args.estilo or conf.get("estilo") or estilo_padrao
    personagens = {chave(p["nome"]): p for p in brief.get("personagens") or [] if p.get("nome")}
    for v in [voz] + [p["voz"] for p in personagens.values() if p.get("voz")]:
        if v not in conhecidas and conhecidas:
            print(f"aviso: '{v}' não é uma voz de {nome_fornecedor}; vai uma voz dele do mesmo gênero, sempre a mesma")

    saida = args.pasta / "09-narracao"
    saida.mkdir(parents=True, exist_ok=True)

    def registrar_custo(uso: Uso) -> None:
        custos.registrar(args.pasta, "narracao", uso.modelo, uso.bruto, provedor=uso.provedor,
                         caracteres=uso.caracteres, segundos=uso.segundos)

    if fornecedor is not None:
        fornecedor.ao_usar = registrar_custo  # cada tentativa paga, inclusive as refeitas, vai para o custo
    modelo = args.modelo or classe.modelo_padrao
    tentativas = 1 if classe.deterministico else 3
    conferir = not args.sem_verificar and not classe.deterministico
    verificacao_indisponivel = False

    def sintetizar(t: Trecho, v: str | None = None) -> tuple[bytes, int]:
        wav, _, n = sintetizar_verificado(fornecedor, t.texto, t.palavras, v or t.voz, t.perfil, t.estilo, tentativas)
        return wav, n

    # Teste de vozes: a primeira fala do narrador (ou do personagem) com cada voz, para escolher pelo ouvido.
    if args.testar_vozes:
        alvo = chave(args.personagem)
        achado = next(((b, n, f) for b in blocos for n, f in enumerate(b.falas, 1) if chave(f.personagem) == alvo), None)
        if achado is None:
            print(f"erro: nenhuma fala de {args.personagem or 'narrador'} no roteiro", file=sys.stderr)
            return 2
        b, n, f = achado
        if len(b.falas) == 1 and f.personagem is None:
            teste = Trecho(b.id, b.id, Path(), texto_do_bloco(b), len(b.tokens), 0, None, voz, perfil, estilo)
        else:
            p_perfil, p_estilo = ((perfil, estilo) if f.personagem is None else
                                  config_personagem(personagens.get(alvo) or {"nome": f.personagem}, f.direcao, brief))
            teste = Trecho(f"{b.id}-F{n:02d}", b.id, Path(), texto_de(b, f.inicio, f.fim), f.fim - f.inicio, f.inicio,
                           f.personagem, voz, p_perfil, p_estilo)
        pasta_teste = saida / "teste-vozes"
        pasta_teste.mkdir(parents=True, exist_ok=True)
        prefixo = re.sub(r"[^a-z0-9]+", "-", alvo).strip("-") + "-" if alvo else ""
        vozes = [x.strip() for x in args.testar_vozes.split(",") if x.strip()]
        if args.simular:
            print("\n".join(f"  {v}: {teste.palavras} palavras de {teste.personagem or 'narrador'} ({teste.id})" for v in vozes))
            return 0
        pendentes = vozes
        for rodada in range(1, MAX_RODADAS + 1):
            for v in pendentes:
                try:
                    wav, _ = sintetizar(teste, v)
                except SemCota as e:
                    print(f"erro: {mensagem_sem_cota(nome_fornecedor, e)}", file=sys.stderr)
                    return 3
                except ErroSintese as e:
                    print(f"  erro {v}: {e}")
                    continue
                (pasta_teste / f"{prefixo}{v}.wav").write_bytes(wav)
            if not conferir:
                break
            resultado = verificar(args.pasta, [f"{teste.id}={pasta_teste / f'{prefixo}{v}.wav'}" for v in pendentes],
                                  args.modelo_whisper)
            if resultado is None:
                verificacao_indisponivel = True
                break
            pendentes = [v for v, r in zip(pendentes, resultado) if not r.get("ok")]
            if not pendentes:
                break
            if rodada < MAX_RODADAS:
                print(f"  voz com defeito ({', '.join(pendentes)}); gerando de novo")
        print(f"texto testado ({teste.personagem or 'narrador'}, {teste.id}): \"{teste.texto}\"")
        for v in vozes:
            destino = pasta_teste / f"{prefixo}{v}.wav"
            if destino.exists():
                marca = " · NÃO PASSOU NA VERIFICAÇÃO" if v in pendentes and conferir else ""
                info = conhecidas.get(v)
                print(f"  {v} ({info.genero if info else '?'}, {info.descricao if info else '?'}): "
                      f"{duracao_wav(destino):.1f} s{marca} → {destino}")
        if verificacao_indisponivel:
            print("erro: as vozes não foram conferidas por transcrição (a verificação não rodou)", file=sys.stderr)
        return 1 if (pendentes or verificacao_indisponivel) and conferir else 0

    plano, erros = planejar(blocos, saida, (voz, perfil, estilo), personagens, brief, nome_fornecedor, modelo)
    if erros:
        for e in erros:
            print(f"erro: {e}", file=sys.stderr)
        print("  acrescente o personagem (com voz) em personagens no 00-pedido.yaml e gere o brief de novo,"
              " ou corrija a marca [FALA: Nome] no roteiro", file=sys.stderr)
        return 2

    estado_path = saida / "blocos.json"
    estado = json.loads(estado_path.read_text(encoding="utf-8")) if estado_path.exists() else {}
    anteriores = {}
    for registro in estado.get("blocos", []):
        anteriores[registro["id"]] = registro
        anteriores.update({f["id"]: f for f in registro.get("falas", [])})
    escolhidos = set(args.blocos.split(",")) if args.blocos else None

    elenco = ", ".join(f"{p['nome']} ({p.get('voz') or 'sem voz'})" for p in personagens.values())
    print(f"fornecedor: {nome_fornecedor} · modelo: {modelo} · narrador: {voz} ({conhecidas[voz].descricao if voz in conhecidas else '?'})"
          + (f" · personagens: {elenco}" if elenco else ""))
    a_gerar = []
    for b, trechos in plano:
        alvo = escolhidos is None or b.id in escolhidos
        for t in trechos:
            precisa = alvo and (args.refazer or not t.arquivo.exists() or anteriores.get(t.id, {}).get("pedido") != t.pedido)
            if args.simular:
                print(f"  {t.id}: {t.palavras} palavras · {quem(t)} · {'gerar' if precisa else 'pronto'}")
            elif precisa:
                a_gerar.append(t)
            elif not t.arquivo.exists():
                print(f"erro: {t.id} ainda não foi narrado; rode sem --blocos", file=sys.stderr)
                return 1
    if args.simular:
        return 0

    # Gera; depois confere cada trecho transcrevendo o áudio e refaz os que saíram com defeito.
    avisos, gerados, reprovados = [], 0, []
    pendentes = a_gerar
    for rodada in range(1, MAX_RODADAS + 1):
        for t in pendentes:
            inicio = time.time()
            try:
                wav, n = sintetizar(t)
            except SemCota as e:
                print(f"erro: {mensagem_sem_cota(nome_fornecedor, e)}", file=sys.stderr)
                return 3
            except ErroSintese as e:
                print(f"erro em {t.id}: {e}", file=sys.stderr)
                return 1
            t.arquivo.parent.mkdir(parents=True, exist_ok=True)
            t.arquivo.write_bytes(wav)
            gerados += 1
            print(f"  ok {t.id} · {quem(t)} · {time.time() - inicio:.0f} s{f' · {n} tentativas' if n > 1 else ''}", flush=True)
        if not pendentes or not conferir:
            break
        resultado = verificar(args.pasta, [f"{t.id}={t.arquivo}" for t in pendentes], args.modelo_whisper)
        if resultado is None:
            verificacao_indisponivel = True
            avisos.append("verificação por transcrição indisponível: os trechos gerados não foram conferidos; "
                          "confira com alinhar.py")
            break
        por_id = {r["bloco"]: r for r in resultado}
        reprovados = [t for t in pendentes if not por_id.get(t.id, {}).get("ok")]
        for t in pendentes:
            r = por_id.get(t.id, {})
            print(f"  verificação {t.id}: cobertura {r.get('cobertura_pct', '?')}% · {r.get('palavras_extras', '?')} extra(s) · "
                  f"{'ok' if r.get('ok') else 'com defeito'}", flush=True)
        if not reprovados:
            break
        if rodada < MAX_RODADAS:
            print(f"  refazendo {', '.join(t.id for t in reprovados)} (rodada {rodada + 1} de {MAX_RODADAS})", flush=True)
        else:
            for t in reprovados:
                avisos += por_id.get(t.id, {}).get("alertas", [])
        pendentes = reprovados

    # Os blocos com diálogo são as falas emendadas; os outros já são o próprio trecho.
    gerados_agora = {t.id for t in a_gerar}
    registros = []
    # Velocidade: a voz fala no ritmo dela; a emenda acelera ou desacelera tudo pelo mesmo fator até o ppm pedido.
    ppm_alvo = args.ppm or ppm_do_brief(brief) or PPM_PADRAO
    natural = ritmo_natural(plano)
    if args.velocidade:
        velocidade = args.velocidade
    else:
        velocidade, aviso = velocidade_para(ppm_alvo, natural)
        if aviso:
            avisos.append(aviso)
    if natural:
        print(f"ritmo: a voz fala a {natural:.0f} ppm · pedido {ppm_alvo} ppm · velocidade {velocidade}x")
    ritmo = {k: v for k, v in (("pausa_max", args.pausa_max), ("velocidade", velocidade)) if v and v != 1.0}
    try:
        for b, trechos in plano:
            for t in trechos:
                if aviso := aviso_de_duracao(t.palavras, duracao_wav(t.arquivo)):
                    avisos.append(f"{t.id}: {aviso}")
                if t.id not in gerados_agora:  # guarda o pedido com que o arquivo foi gerado
                    t.pedido = anteriores.get(t.id, {}).get("pedido", t.pedido)
            if len(trechos) == 1 and trechos[0].id == b.id:
                arquivo, falas = trechos[0].arquivo, None
                if ritmo:  # o bloco gerado fica como está; a versão no ritmo pedido vai para ritmo/
                    arquivo = saida / "ritmo" / f"{b.id}.wav"
                    emendar([trechos[0].arquivo], arquivo, [], **ritmo)
            else:
                arquivo = saida / "blocos" / f"{b.id}.wav"
                posicoes = emendar([t.arquivo for t in trechos], arquivo, intervalos_das_falas(b, trechos, args.intervalo_falas),
                                   aparar_silencio=True, **ritmo)
                falas = [{"id": t.id, "personagem": t.personagem, "voz": t.voz, "arquivo": str(t.arquivo.relative_to(args.pasta)),
                          "palavras": t.palavras, "inicio_no_bloco_s": ini, "duracao_s": dur, "pedido": t.pedido}
                         for t, (ini, dur) in zip(trechos, posicoes)]
            dur = duracao_wav(arquivo)
            registro = {"id": b.id, "arquivo": str(arquivo.relative_to(args.pasta)), "palavras": len(b.tokens),
                        "duracao_s": round(dur, 3), "ppm": round(len(b.tokens) / dur * 60) if dur else None}
            registro.update({"falas": falas} if falas else {"pedido": trechos[0].pedido})
            registros.append(registro)
        posicoes = emendar([args.pasta / r["arquivo"] for r in registros], saida / "narracao.wav", args.intervalo)
    except ErroSintese as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    for r, (ini, _) in zip(registros, posicoes):
        r["inicio_s"] = ini
    total = posicoes[-1][0] + posicoes[-1][1]
    estado = {"fornecedor": nome_fornecedor, "modelo": modelo, "voz": voz, "perfil": perfil, "estilo": estilo,
              "personagens": {p["nome"]: p.get("voz") for p in personagens.values()},
              "intervalo_s": args.intervalo, "intervalo_falas_s": args.intervalo_falas, "pausa_max_s": args.pausa_max,
              "velocidade": velocidade, "ppm_pedido": ppm_alvo, "ppm_natural": round(natural) if natural else None,
              "arquivo": "09-narracao/narracao.wav",
              "duracao_s": round(total, 3), "blocos": registros}
    estado_path.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")

    palavras = sum(r["palavras"] for r in registros)
    n_falas = sum(1 for r in registros for f in r.get("falas", []) if f["personagem"])
    print(f"narração: {saida / 'narracao.wav'} · {total:.1f} s · {palavras} palavras · {palavras / total * 60:.0f} ppm · "
          f"{gerados} geração(ões) nesta rodada" + (f" · {n_falas} fala(s) de personagens" if n_falas else ""))
    for a in avisos:
        print(f"aviso: {a}")
    print(f"próximo passo: python3 ferramentas/producao/alinhar.py {args.pasta}")
    if verificacao_indisponivel:
        print("erro: a narração foi gravada, mas sem a verificação por transcrição (instale o faster-whisper)",
              file=sys.stderr)
    return 1 if (reprovados or verificacao_indisponivel) and conferir else 0


if __name__ == "__main__":
    sys.exit(main())
