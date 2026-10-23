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

Fornecedores (o padrão vem de narracao.fornecedor no pedido). As chaves vêm do .env da raiz ou do ambiente,
cadastradas em Configurações > Chaves de API, na interface web (ferramentas/chaves.py):
  gemini      Gemini TTS: vozes naturais, pago. Chave: GEMINI_API_KEY (ou GOOGLE_API_KEY).
  openai      TTS da OpenAI (gpt-4o-mini-tts): o perfil e o estilo vão como instruções. Chave: OPENAI_API_KEY.
  elevenlabs  ElevenLabs: as vozes da conta (nome ou voice_id), sem instrução de estilo. Chave: ELEVENLABS_API_KEY.
  espeak      espeak-ng, local e grátis: voz robótica, para testar ritmo, falas, alinhamento e montagem antes de
              pagar. Cada voz do Gemini do pedido vira uma variante do espeak do mesmo gênero, sempre a mesma.
Uma voz que não existe no fornecedor escolhido (a voz do canal é do Gemini) vira uma voz dele do mesmo gênero,
sempre a mesma: dá para trocar de fornecedor sem mexer no pedido. Para acrescentar outro, implemente uma
classe com o método sintetizar(texto, voz, perfil, estilo) -> bytes de WAV e registre-a em FORNECEDORES.

Uso:
    python3 skills/produzir-video/scripts/narrar.py projetos/<slug> --testar-vozes Charon,Kore,Algenib
    python3 skills/produzir-video/scripts/narrar.py projetos/<slug> --testar-vozes Leda,Kore,Aoede --personagem Moranguete
    python3 skills/produzir-video/scripts/narrar.py projetos/<slug>
    python3 skills/produzir-video/scripts/narrar.py projetos/<slug> --fornecedor espeak
    python3 skills/produzir-video/scripts/narrar.py projetos/<slug> --blocos B03 --refazer
    python3 skills/produzir-video/scripts/narrar.py --listar-vozes
    python3 skills/produzir-video/scripts/narrar.py --amostras            # ~5 s de cada voz em biblioteca/vozes/
    python3 skills/produzir-video/scripts/narrar.py --amostras Kore,Leda --refazer

Se o pedido desligou a narração (narracao.ativa: não), não gera nada, a não ser com --forcar.

Cada trecho gerado pelo Gemini é conferido por transcrição (alinhar.py --verificar): se a voz repetiu o
texto, leu a instrução de estilo ou pulou um trecho, ele é gerado de novo, até 3 rodadas. Depois de narrar,
rode alinhar.py para medir o tempo real de cada palavra.

Só usa a biblioteca padrão (a verificação chama alinhar.py pelo uv; o fornecedor espeak usa o espeak-ng; o
ajuste de velocidade usa o ffmpeg).
"""

from __future__ import annotations

import argparse
import base64
from array import array
import hashlib
import io
import json
import re
import statistics
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(RAIZ / "ferramentas"))
from brief import config, ler_brief, narracao_ativa, ppm_do_brief  # noqa: E402
import custos  # noqa: E402
from medir_roteiro import PPM_PADRAO, SEGUNDOS_POR_PAUSA, ler_roteiro, sem_acento  # noqa: E402
import chaves  # noqa: E402

PPM_REFERENCIA = 150
INTERVALO_PADRAO_S = 0.35
INTERVALO_FALAS_S = 0.25
MAX_RODADAS = 3
# Ajuste do ritmo da fala até narracao.palavras_por_minuto: abaixo de 3% de diferença não mexe; fora de 0,8x a 1,35x
# o atempo começa a soar artificial.
TOLERANCIA_RITMO = 0.03
VELOCIDADE_MIN, VELOCIDADE_MAX = 0.8, 1.35  # gerar e verificar; refazer até duas vezes os trechos com defeito
ALINHADOR = RAIZ / "skills" / "produzir-video" / "scripts" / "alinhar.py"

# Vozes prontas do Gemini TTS e a característica que o Google atribui a cada uma.
VOZES_GEMINI = {
    "Zephyr": "clara", "Puck": "animada", "Charon": "informativa", "Kore": "firme", "Fenrir": "empolgada",
    "Leda": "jovem", "Orus": "firme", "Aoede": "leve", "Callirrhoe": "tranquila", "Autonoe": "clara",
    "Enceladus": "sussurrada", "Iapetus": "nítida", "Umbriel": "tranquila", "Algieba": "suave", "Despina": "suave",
    "Erinome": "nítida", "Algenib": "rouca", "Rasalgethi": "informativa", "Laomedeia": "animada", "Achernar": "macia",
    "Alnilam": "firme", "Schedar": "uniforme", "Gacrux": "madura", "Pulcherrima": "direta", "Achird": "amigável",
    "Zubenelgenubi": "casual", "Vindemiatrix": "gentil", "Sadachbia": "viva", "Sadaltager": "conhecedora",
    "Sulafat": "calorosa",
}
# Gênero de cada voz, como aparece no seletor do AI Studio (f: feminina, m: masculina). Confira ouvindo.
GENERO_VOZES = {
    "Zephyr": "f", "Puck": "m", "Charon": "m", "Kore": "f", "Fenrir": "m", "Leda": "f", "Orus": "m", "Aoede": "f",
    "Callirrhoe": "f", "Autonoe": "f", "Enceladus": "m", "Iapetus": "m", "Umbriel": "m", "Algieba": "m",
    "Despina": "f", "Erinome": "f", "Algenib": "m", "Rasalgethi": "m", "Laomedeia": "f", "Achernar": "f",
    "Alnilam": "m", "Schedar": "m", "Gacrux": "f", "Pulcherrima": "f", "Achird": "m", "Zubenelgenubi": "m",
    "Vindemiatrix": "f", "Sadachbia": "m", "Sadaltager": "m", "Sulafat": "f",
}


class SemCota(Exception):
    pass


class ErroSintese(Exception):
    pass


def carregar_chave() -> str | None:
    return chaves.ler("gemini")


def voz_equivalente(fornecedor, voz: str) -> str:
    """A voz do pedido, se o fornecedor a tem; senão uma dele do mesmo gênero, sempre a mesma para o mesmo nome."""
    if voz in fornecedor.VOZES:
        return voz
    por_nome = {k.lower(): k for k in fornecedor.VOZES}
    if voz.lower() in por_nome:
        return por_nome[voz.lower()]
    genero = GENERO_VOZES.get(voz) or fornecedor.GENEROS.get(voz, "m")
    mesmas = sorted(v for v in fornecedor.VOZES if fornecedor.GENEROS.get(v) == genero) or sorted(fornecedor.VOZES)
    if not mesmas:
        raise ErroSintese(f"o fornecedor não tem vozes disponíveis para usar no lugar de '{voz}'")
    return mesmas[int(hashlib.sha1(voz.encode()).hexdigest(), 16) % len(mesmas)]


def chamar_audio(url: str, corpo: dict, cabecalhos: dict) -> bytes:
    """POST que devolve áudio, com novas tentativas em limite de taxa e erro do servidor."""
    req = urllib.request.Request(url, data=json.dumps(corpo).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **cabecalhos})
    espera = 10
    for tentativa in range(6):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            texto = e.read().decode("utf-8", "replace")
            try:
                detalhe = json.loads(texto)
                detalhe = detalhe.get("error") or detalhe.get("detail") or detalhe
                msg = detalhe.get("message", str(detalhe)) if isinstance(detalhe, dict) else str(detalhe)
            except json.JSONDecodeError:
                msg = texto[:300]
            if re.search(r"insufficient_quota|quota_exceeded", texto):
                raise SemCota(msg[:300]) from None
            if e.code in (429, 500, 502, 503, 504) and tentativa < 5:
                time.sleep(espera)
                espera = min(90, espera * 2)
                continue
            raise ErroSintese(f"HTTP {e.code}: {msg[:300]}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if tentativa < 5:
                time.sleep(espera)
                continue
            raise ErroSintese(f"rede: {e}") from None
    raise ErroSintese("sem resposta")


# ---------------------------------------------------------------- fornecedores


class GeminiTTS:
    """Gemini TTS pela API REST. O estilo vai num prompt estruturado (perfil, notas de direção, transcrição):
    em teste, foi o único formato em que o modelo não leu a instrução em voz alta."""

    API = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
    MODELO_PADRAO = "gemini-3.8-flash-tts"
    VOZ_PADRAO = "Charon"
    VOZES = VOZES_GEMINI
    GENEROS = GENERO_VOZES
    DETERMINISTICO = False

    def __init__(self, modelo: str | None):
        self.modelo = modelo or self.MODELO_PADRAO
        self.ao_usar = None  # chamado com o usageMetadata de cada resposta (registro de custos do projeto)
        self.chave = carregar_chave()
        if not self.chave:
            raise ErroSintese("defina GEMINI_API_KEY ou crie .env na raiz do projeto")

    def sintetizar(self, texto: str, voz: str, perfil: str, estilo: str) -> bytes:
        prompt = f"# PERFIL DE ÁUDIO\n{perfil}\n\n## NOTAS DE DIREÇÃO\n{estilo}\n\n## TRANSCRIÇÃO\n{texto}"
        corpo = json.dumps({
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voz}}}},
        }).encode()
        req = urllib.request.Request(self.API.format(modelo=self.modelo), data=corpo, method="POST",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": self.chave})
        espera = 10
        for tentativa in range(6):
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    dados = json.load(r)
                break
            except urllib.error.HTTPError as e:
                try:
                    msg = json.load(e).get("error", {}).get("message", "")
                except (json.JSONDecodeError, AttributeError):
                    msg = ""
                if e.code == 429 and "limit: 0" in msg:
                    raise SemCota(msg.splitlines()[0]) from None
                if e.code in (429, 500, 502, 503, 504) and tentativa < 5:
                    m = re.search(r"retry in (\d+(?:\.\d+)?)s", msg)
                    time.sleep(min(90, float(m.group(1)) + 1) if m else espera)
                    espera = min(90, espera * 2)
                    continue
                raise ErroSintese(f"HTTP {e.code}: {msg[:300]}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                if tentativa < 5:
                    time.sleep(espera)
                    continue
                raise ErroSintese(f"rede: {e}") from None
        if self.ao_usar:
            self.ao_usar(self.modelo, dados.get("usageMetadata") or {}, caracteres=len(texto))
        candidato = (dados.get("candidates") or [{}])[0]
        for parte in (candidato.get("content") or {}).get("parts", []):
            if "inlineData" in parte:
                return para_wav(base64.b64decode(parte["inlineData"]["data"]), parte["inlineData"].get("mimeType", ""))
        raise ErroSintese(f"nenhum áudio na resposta (finishReason={candidato.get('finishReason', '?')})")


class EspeakTTS:
    """espeak-ng, local e grátis. A voz é robótica: serve para testar o vídeo inteiro (ritmo, falas, alinhamento,
    montagem) antes de pagar pelo Gemini. Cada voz do Gemini vira uma variante do espeak do mesmo gênero; vozes
    diferentes do mesmo gênero caem em variantes diferentes (até dez de cada)."""

    MODELO_PADRAO = "espeak-ng"
    VOZ_PADRAO = "Charon"
    VOZES = VOZES_GEMINI  # aceita os nomes do pedido, para trocar de fornecedor sem mexer nele
    GENEROS = GENERO_VOZES
    DETERMINISTICO = True  # o mesmo texto dá sempre o mesmo áudio: não adianta conferir nem repetir
    VARIANTES = {"f": ["f3", "Andrea", "f1", "linda", "f4", "steph", "f2", "Annie", "f5", "belinda"],
                 "m": ["m3", "Henrique", "m1", "Diogo", "m7", "antonio", "m2", "marcelo", "m6", "pedro"]}

    def __init__(self, modelo: str | None):
        if not shutil.which("espeak-ng"):
            raise ErroSintese("espeak-ng não encontrado (instale com: sudo apt install espeak-ng)")
        self.modelo = self.MODELO_PADRAO

    def variante(self, voz: str) -> str:
        genero = GENERO_VOZES.get(voz, "m")
        mesmas = sorted(v for v, g in GENERO_VOZES.items() if g == genero)
        posicao = mesmas.index(voz) if voz in mesmas else int(hashlib.sha1(voz.encode()).hexdigest(), 16)
        return self.VARIANTES[genero][posicao % len(self.VARIANTES[genero])]

    def sintetizar(self, texto: str, voz: str, perfil: str, estilo: str) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp) / "fala.wav"
            r = subprocess.run(["espeak-ng", "-v", f"pt-br+{self.variante(voz)}", "-s", "160", "-w", str(destino), texto],
                               capture_output=True, text=True)
            if r.returncode != 0 or not destino.exists():
                raise ErroSintese(f"espeak-ng: {(r.stderr or '').strip()[:200]}")
            return destino.read_bytes()


VOZES_OPENAI = {
    "marin": "natural, a melhor", "cedar": "natural, a melhor", "alloy": "neutra", "ash": "firme", "ballad": "suave",
    "coral": "calorosa", "echo": "clara", "fable": "expressiva", "nova": "animada", "onyx": "grave", "sage": "serena",
    "shimmer": "leve", "verse": "versátil",
}
GENERO_OPENAI = {"marin": "f", "cedar": "m", "alloy": "f", "ash": "m", "ballad": "m", "coral": "f", "echo": "m",
                 "fable": "m", "nova": "f", "onyx": "m", "sage": "f", "shimmer": "f", "verse": "m"}


class OpenAITTS:
    """TTS da OpenAI (/v1/audio/speech). O perfil e o estilo vão em instructions, que o gpt-4o-mini-tts segue; os
    modelos tts-1 ignoram. A API não devolve o uso: o custo é estimado pela duração do áudio."""

    API = "https://api.openai.com/v1/audio/speech"
    MODELO_PADRAO = "gpt-4o-mini-tts"
    VOZ_PADRAO = "cedar"
    VOZES = VOZES_OPENAI
    GENEROS = GENERO_OPENAI
    DETERMINISTICO = False

    def __init__(self, modelo: str | None):
        self.modelo = modelo or self.MODELO_PADRAO
        self.ao_usar = None
        self.chave = chaves.ler("openai")
        if not self.chave:
            raise ErroSintese("sem chave da OpenAI: cadastre em Configurações > Chaves de API, na interface, ou defina OPENAI_API_KEY no .env")

    def sintetizar(self, texto: str, voz: str, perfil: str, estilo: str) -> bytes:
        corpo = {"model": self.modelo, "voice": voz_equivalente(self, voz), "input": texto, "response_format": "pcm"}
        if not self.modelo.startswith("tts-1"):
            corpo["instructions"] = f"{perfil}\n\n{estilo}".strip()
        wav = para_wav(chamar_audio(self.API, corpo, {"Authorization": f"Bearer {self.chave}"}), "audio/L16;rate=24000")
        if self.ao_usar:
            self.ao_usar(self.modelo, {}, segundos=round(duracao_bytes(wav), 2), caracteres=len(texto))
        return wav


VOZES_ELEVENLABS_CACHE = RAIZ / "biblioteca" / "vozes" / "elevenlabs.json"


def vozes_elevenlabs_em_cache() -> list[dict]:
    try:
        return json.loads(VOZES_ELEVENLABS_CACHE.read_text(encoding="utf-8")).get("vozes", [])
    except (OSError, json.JSONDecodeError):
        return []


def atualizar_vozes_elevenlabs(chave: str) -> list[dict]:
    """As vozes da conta na ElevenLabs (as prontas e as clonadas), guardadas para validar o pedido sem rede."""
    req = urllib.request.Request("https://api.elevenlabs.io/v2/voices?page_size=100", headers={"xi-api-key": chave})
    with urllib.request.urlopen(req, timeout=30) as r:
        dados = json.load(r)
    vozes = []
    for v in dados.get("voices") or []:
        rotulos = v.get("labels") or {}
        genero = {"female": "f", "male": "m"}.get(str(rotulos.get("gender", "")).lower(), "?")
        descricao = ", ".join(str(x) for x in (rotulos.get("description") or rotulos.get("descriptive"), rotulos.get("accent"),
                                                 rotulos.get("use_case")) if x)
        vozes.append({"nome": v.get("name", v["voice_id"]), "id": v["voice_id"], "genero": genero,
                      "descricao": descricao or v.get("category", ""), "amostra": v.get("preview_url")})
    VOZES_ELEVENLABS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    VOZES_ELEVENLABS_CACHE.write_text(json.dumps({"atualizado_em": time.strftime("%Y-%m-%d %H:%M"), "vozes": vozes},
                                                 ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return vozes


class ElevenLabsTTS:
    """ElevenLabs (/v1/text-to-speech). A voz é o nome de uma voz da conta (ou o voice_id). Não há instrução de
    estilo: o jeito de falar vem da própria voz escolhida. Cobrança por caractere."""

    API = "https://api.elevenlabs.io/v1/text-to-speech/{voz}?output_format=pcm_24000"
    MODELO_PADRAO = "eleven_multilingual_v2"
    VOZ_PADRAO = None
    VOZES = {v["nome"]: v["descricao"] for v in vozes_elevenlabs_em_cache()}
    GENEROS = {v["nome"]: v["genero"] for v in vozes_elevenlabs_em_cache()}
    DETERMINISTICO = False

    def __init__(self, modelo: str | None):
        self.modelo = modelo or self.MODELO_PADRAO
        self.ao_usar = None
        self.chave = chaves.ler("elevenlabs")
        if not self.chave:
            raise ErroSintese("sem chave da ElevenLabs: cadastre em Configurações > Chaves de API, na interface, ou defina ELEVENLABS_API_KEY no .env")
        try:
            vozes = atualizar_vozes_elevenlabs(self.chave)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            vozes = vozes_elevenlabs_em_cache()
            print(f"aviso: não consegui listar as vozes da ElevenLabs ({e}); usando a lista guardada", file=sys.stderr)
        self.ids = {v["nome"]: v["id"] for v in vozes}
        type(self).VOZES = {v["nome"]: v["descricao"] for v in vozes}
        type(self).GENEROS = {v["nome"]: v["genero"] for v in vozes}
        type(self).VOZ_PADRAO = next(iter(self.VOZES), None)

    def sintetizar(self, texto: str, voz: str, perfil: str, estilo: str) -> bytes:
        id_ = voz if voz in self.ids.values() else self.ids[voz_equivalente(self, voz)]
        dados = chamar_audio(self.API.format(voz=id_), {"text": texto, "model_id": self.modelo}, {"xi-api-key": self.chave})
        if self.ao_usar:
            self.ao_usar(self.modelo, {}, caracteres=len(texto))
        return para_wav(dados, "audio/L16;rate=24000")


FORNECEDORES = {"gemini": GeminiTTS, "openai": OpenAITTS, "elevenlabs": ElevenLabsTTS, "espeak": EspeakTTS}


# ---------------------------------------------------------------- áudio


def para_wav(dados: bytes, mime: str) -> bytes:
    """O Gemini devolve WAV pronto ou PCM cru (audio/L16;rate=24000), conforme o modelo."""
    if dados[:4] == b"RIFF":
        return dados
    taxa = int(m.group(1)) if (m := re.search(r"rate=(\d+)", mime)) else 24000
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(taxa)
        w.writeframes(dados)
    return buf.getvalue()


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


def config_personagem(p: dict, direcao: str, brief: dict) -> tuple[str, str]:
    """Perfil e notas de direção da voz de um personagem, a partir de personagens no pedido."""
    perfil = f"{p['nome']}, personagem de uma história contada em vídeo para o YouTube."
    if p.get("descricao"):
        perfil += f" {p['descricao'].rstrip('.')}."
    perfil += " Fala em português do Brasil, sotaque neutro."
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


def duracao_bytes(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav), "rb") as w:
        return w.getnframes() / w.getframerate()


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


def verificar(pasta: Path, itens: list[str]) -> list[dict] | None:
    """Confere os áudios transcrevendo-os (alinhar.py --verificar). Devolve um resultado por item, na ordem,
    ou None se a verificação não puder rodar (sem uv, por exemplo)."""
    uv = shutil.which("uv")
    if not uv:
        print("aviso: uv não encontrado; pulando a verificação por transcrição", file=sys.stderr)
        return None
    r = subprocess.run([uv, "run", "--script", str(ALINHADOR), str(pasta), "--verificar", ",".join(itens)],
                       capture_output=True, text=True)
    linhas = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
    if len(linhas) != len(itens):
        print(f"aviso: a verificação falhou ({(r.stderr or '').strip().splitlines()[-1:] or ['sem saída']})", file=sys.stderr)
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

AMOSTRAS = RAIZ / "biblioteca" / "vozes"
TEXTO_AMOSTRA = "Oi, eu sou a voz {voz}. É assim que eu vou contar a sua próxima história."
PERFIL_AMOSTRA = "Narrador brasileiro de vídeos para o YouTube. Português do Brasil, sotaque neutro, dicção clara."
ESTILO_AMOSTRA = "Tom natural e envolvente, como quem começa a contar uma história para uma pessoa só."


def gravar_amostras(classe, modelo: str | None, vozes: list[str], refazer: bool) -> int:
    """Uma frase curta (~5 s) em cada voz, com o mesmo texto e a mesma direção, para comparar pelo ouvido na
    hora de escolher. Grava biblioteca/vozes/<Voz>.mp3 e amostras.json; pula o que já foi gravado igual."""
    fornecedor = classe(modelo)
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
            print(f"erro: sem cota de TTS no projeto do Google ({e})", file=sys.stderr)
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
        print(f"  {v} ({classe.GENEROS.get(v, '?')}, {classe.VOZES.get(v, '?')}): {duracao:.1f} s"
              f"{' · ' + aviso if aviso else ''} → {destino.relative_to(RAIZ)}")
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
    ap.add_argument("--forcar", action="store_true", help="narra mesmo que o pedido tenha desligado a narração")
    args = ap.parse_args()

    if args.listar_vozes:
        classe = FORNECEDORES[args.fornecedor or "gemini"]
        if classe is ElevenLabsTTS:  # a lista vem da conta: atualiza antes de mostrar
            try:
                classe(None)
            except ErroSintese as e:
                print(f"aviso: {e}", file=sys.stderr)
        for nome, desc in classe.VOZES.items():
            print(f"{nome:15} {classe.GENEROS.get(nome, '?')}  {desc}")
        return 0
    if args.amostras is not None:
        classe = FORNECEDORES[args.fornecedor or "gemini"]
        vozes = [x.strip() for x in args.amostras.split(",") if x.strip()] or list(classe.VOZES)
        try:
            return gravar_amostras(classe, args.modelo, vozes, args.refazer)
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
    classe = FORNECEDORES[nome_fornecedor]
    canal_path = RAIZ / "biblioteca" / "canal.json"
    canal = json.loads(canal_path.read_text(encoding="utf-8")).get("narracao", {}) if canal_path.exists() else {}
    do_brief = {k: v for k, v in (brief.get("narracao") or {}).items() if v is not None}
    conf = {**canal, **do_brief}  # o brief do projeto tem prioridade sobre o padrão do canal
    perfil_padrao, estilo_padrao = config_padrao(brief)
    voz = args.voz or conf.get("voz") or classe.VOZ_PADRAO
    perfil = args.perfil or conf.get("perfil") or perfil_padrao
    estilo = args.estilo or conf.get("estilo") or estilo_padrao
    personagens = {chave(p["nome"]): p for p in brief.get("personagens") or [] if p.get("nome")}
    for v in [voz] + [p["voz"] for p in personagens.values() if p.get("voz")]:
        if v not in classe.VOZES and classe.VOZES:
            print(f"aviso: '{v}' não é uma voz de {nome_fornecedor}; vai uma voz dele do mesmo gênero, sempre a mesma")

    saida = args.pasta / "09-narracao"
    saida.mkdir(parents=True, exist_ok=True)
    try:
        fornecedor = None if args.simular else classe(args.modelo)
    except ErroSintese as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2
    if hasattr(fornecedor, "ao_usar"):
        fornecedor.ao_usar = lambda m, uso, **extra: custos.registrar(args.pasta, "narracao", m, uso, **extra)
    modelo = args.modelo or classe.MODELO_PADRAO
    tentativas = 1 if classe.DETERMINISTICO else 3
    conferir = not args.sem_verificar and not classe.DETERMINISTICO

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
                    print(f"erro: sem cota de TTS no projeto do Google ({e})", file=sys.stderr)
                    return 3
                except ErroSintese as e:
                    print(f"  erro {v}: {e}")
                    continue
                (pasta_teste / f"{prefixo}{v}.wav").write_bytes(wav)
            if not conferir:
                break
            resultado = verificar(args.pasta, [f"{teste.id}={pasta_teste / f'{prefixo}{v}.wav'}" for v in pendentes])
            if resultado is None:
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
                print(f"  {v} ({classe.GENEROS.get(v, '?')}, {classe.VOZES.get(v, '?')}): {duracao_wav(destino):.1f} s{marca} → {destino}")
        return 1 if pendentes and conferir else 0

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
    print(f"fornecedor: {nome_fornecedor} · modelo: {modelo} · narrador: {voz} ({classe.VOZES.get(voz, '?')})"
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
                print(f"erro: o projeto do Google não tem cota de TTS ({e}). Ative o faturamento.", file=sys.stderr)
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
        resultado = verificar(args.pasta, [f"{t.id}={t.arquivo}" for t in pendentes])
        if resultado is None:
            avisos.append("verificação por transcrição indisponível; confira com alinhar.py")
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
    print("próximo passo: uv run --script skills/produzir-video/scripts/alinhar.py " + str(args.pasta) + " 2>&1 | cat")
    return 1 if reprovados and conferir else 0


if __name__ == "__main__":
    sys.exit(main())
