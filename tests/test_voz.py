"""Fornecedores de voz: o corpo enviado a cada API (igual ao de antes, byte a byte nos campos de texto), a troca de
voz entre fornecedores, o cache das vozes da ElevenLabs e o espeak de verdade. Sem rede: o HTTP é simulado
trocando nucleo.provedores.http.pedir."""

from __future__ import annotations

import base64
import json
import shutil
import sys
import types
import wave
import io

import pytest

from nucleo import provedores
from nucleo.provedores import http
from nucleo.provedores.erros import ErroProvedor, SemCota
import nucleo.provedores.voz as tts
from nucleo.provedores.voz import base, elevenlabs, espeak
from nucleo.provedores.voz.base import Voz

PCM = b"\x01\x00" * 2400  # 0,1 s de PCM 16 bits a 24 kHz


class HttpFalso:
    """Guarda cada requisição e devolve as respostas na ordem."""

    def __init__(self, *respostas):
        self.respostas = list(respostas)
        self.pedidos = []

    def __call__(self, url, corpo, cabecalhos, *, metodo="POST", timeout=0, provedor=None, espera_inicial=5):
        self.pedidos.append({"url": url, "corpo": corpo, "cabecalhos": cabecalhos, "metodo": metodo})
        resposta = self.respostas.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        return (json.dumps(resposta).encode() if isinstance(resposta, dict) else resposta), {}


@pytest.fixture
def http_falso(monkeypatch):
    def instalar(*respostas):
        falso = HttpFalso(*respostas)
        monkeypatch.setattr(http, "pedir", falso)
        return falso
    return instalar


def duracao(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav), "rb") as w:
        return w.getnframes() / w.getframerate()


# ---------------------------------------------------------------- troca de voz


def test_voz_equivalente_e_deterministica_e_respeita_o_genero():
    openai = tts.catalogo("openai")
    a = tts.voz_equivalente("openai", "Sadaltager")
    assert a == tts.voz_equivalente(openai, "Sadaltager") == tts.voz_equivalente("openai", "Sadaltager")
    assert base.GENERO_OPENAI[a] == "m"
    assert base.GENERO_OPENAI[tts.voz_equivalente("openai", "Leda")] == "f"
    # a regra é a de sempre: sha1 do nome sobre as vozes do gênero, em ordem
    import hashlib
    masculinas = sorted(n for n, g in base.GENERO_OPENAI.items() if g == "m")
    assert a == masculinas[int(hashlib.sha1(b"Sadaltager").hexdigest(), 16) % len(masculinas)]


def test_voz_equivalente_mantem_a_voz_existente_e_ignora_maiusculas():
    assert tts.voz_equivalente("gemini", "Kore") == "Kore"
    assert tts.voz_equivalente("gemini", "kore") == "Kore"
    assert tts.voz_equivalente("openai", "CEDAR") == "cedar"


def test_voz_equivalente_vale_para_o_gemini():
    # antes, uma voz da OpenAI ia crua para o Gemini e dava HTTP 400
    nova = tts.voz_equivalente("gemini", "cedar")
    assert nova in base.VOZES_GEMINI and base.GENERO_VOZES[nova] == "m"
    assert base.GENERO_VOZES[tts.voz_equivalente("gemini", "marin")] == "f"


def test_voz_equivalente_sem_vozes_falha():
    with pytest.raises(ErroProvedor):
        base.voz_equivalente([], "Kore")


def test_para_wav_embrulha_pcm_e_respeita_wav_pronto():
    wav = base.para_wav(PCM, "audio/L16;rate=24000")
    assert wav[:4] == b"RIFF" and duracao(wav) == pytest.approx(0.1)
    assert base.para_wav(wav, "audio/wav") is wav
    assert duracao(base.para_wav(PCM, "audio/L16;rate=12000")) == pytest.approx(0.2)


# ---------------------------------------------------------------- Gemini


def test_gemini_manda_o_prompt_estruturado(http_falso):
    resposta = {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000",
                                                                    "data": base64.b64encode(PCM).decode()}}]}}],
                "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 100}}
    falso = http_falso(resposta)
    usos = []
    voz = provedores.voz("gemini", None, ao_usar=usos.append, chave="chave-falsa")
    wav = voz.sintetizar("Olá, mundo.", "Sadaltager", "Narrador brasileiro.", "Tom: calmo.")
    assert wav[:4] == b"RIFF"
    pedido = falso.pedidos[0]
    assert pedido["url"] == ("https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash-tts:"
                             "generateContent")
    assert pedido["cabecalhos"]["x-goog-api-key"] == "chave-falsa"
    corpo = json.loads(pedido["corpo"])
    assert corpo == {
        "contents": [{"role": "user", "parts": [{"text": "# PERFIL DE ÁUDIO\nNarrador brasileiro.\n\n## NOTAS DE DIREÇÃO\n"
                                                         "Tom: calmo.\n\n## TRANSCRIÇÃO\nOlá, mundo."}]}],
        "generationConfig": {"responseModalities": ["AUDIO"],
                             "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Sadaltager"}}}},
    }
    assert usos[0].servico == "narracao" and usos[0].provedor == "gemini" and usos[0].caracteres == len("Olá, mundo.")
    assert usos[0].bruto == {"promptTokenCount": 30, "candidatesTokenCount": 100}


def test_gemini_sem_audio_e_erro(http_falso):
    http_falso({"candidates": [{"finishReason": "OTHER", "content": {"parts": []}}]})
    with pytest.raises(ErroProvedor, match="finishReason=OTHER"):
        provedores.voz("gemini", chave="x").sintetizar("a", "Kore", "p", "e")


def test_sem_cota_do_gemini_vem_do_http(monkeypatch):
    import urllib.error

    def urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 429, "x", {}, io.BytesIO(
            json.dumps({"error": {"message": "Quota exceeded, limit: 0"}}).encode()))
    monkeypatch.setattr(http.urllib.request, "urlopen", urlopen)
    with pytest.raises(SemCota):
        provedores.voz("gemini", chave="x").sintetizar("a", "Kore", "p", "e")


# ---------------------------------------------------------------- OpenAI


def test_openai_manda_instrucoes_e_troca_a_voz(http_falso):
    falso = http_falso(PCM)
    usos = []
    voz = provedores.voz("openai", ao_usar=usos.append, chave="sk-falsa")
    wav = voz.sintetizar("Olá.", "Sadaltager", "Perfil.", "Estilo.")
    corpo = json.loads(falso.pedidos[0]["corpo"])
    assert falso.pedidos[0]["url"] == "https://api.openai.com/v1/audio/speech"
    assert falso.pedidos[0]["cabecalhos"]["Authorization"] == "Bearer sk-falsa"
    assert corpo == {"model": "gpt-4o-mini-tts", "voice": tts.voz_equivalente("openai", "Sadaltager"), "input": "Olá.",
                     "response_format": "pcm", "instructions": "Perfil.\n\nEstilo."}
    assert duracao(wav) == pytest.approx(0.1)
    assert usos[0].bruto == {} and usos[0].segundos == 0.1 and usos[0].caracteres == 4


def test_openai_tts1_nao_aceita_instrucoes(http_falso):
    falso = http_falso(PCM)
    provedores.voz("openai", "tts-1", chave="x").sintetizar("Olá.", "cedar", "Perfil.", "Estilo.")
    assert "instructions" not in json.loads(falso.pedidos[0]["corpo"])


# ---------------------------------------------------------------- ElevenLabs


@pytest.fixture
def cache_elevenlabs(tmp_path, monkeypatch):
    """O cache da ElevenLabs num arquivo temporário e num banco falso (nunca o banco de verdade)."""
    arquivo = tmp_path / "elevenlabs.json"
    monkeypatch.setattr(elevenlabs, "arquivo_cache", lambda: arquivo)
    guardado = {}
    repositorios = types.SimpleNamespace(ler_config=lambda chave, padrao=None: guardado.get(chave, padrao),
                                         gravar_config=lambda chave, valor: guardado.__setitem__(chave, valor))
    monkeypatch.setitem(sys.modules, "nucleo.banco", types.SimpleNamespace(repositorios=repositorios))
    monkeypatch.setitem(sys.modules, "nucleo.banco.repositorios", repositorios)
    return arquivo, guardado


CONTA = {"voices": [
    {"voice_id": "id-ana", "name": "Ana", "labels": {"gender": "female", "accent": "brazilian"}, "preview_url": "u1"},
    {"voice_id": "id-beto", "name": "Beto", "labels": {"gender": "male", "description": "deep"}, "category": "premade"},
]}


def test_elevenlabs_atualiza_o_cache_no_banco_e_no_arquivo(cache_elevenlabs, http_falso):
    arquivo, guardado = cache_elevenlabs
    falso = http_falso(CONTA)
    vozes = tts.atualizar_vozes_elevenlabs("xi-falsa")
    assert falso.pedidos[0]["metodo"] == "GET" and falso.pedidos[0]["cabecalhos"]["xi-api-key"] == "xi-falsa"
    assert vozes == [Voz("Ana", "f", "brazilian", "id-ana", "u1"), Voz("Beto", "m", "deep", "id-beto", None)]
    assert [v["nome"] for v in guardado["vozes_elevenlabs"]["vozes"]] == ["Ana", "Beto"]
    assert json.loads(arquivo.read_text())["vozes"][0] == {"nome": "Ana", "id": "id-ana", "genero": "f",
                                                          "descricao": "brazilian", "amostra": "u1"}
    assert tts.catalogo("elevenlabs") == vozes


def test_elevenlabs_le_o_arquivo_sem_banco(cache_elevenlabs, monkeypatch):
    arquivo, _ = cache_elevenlabs
    monkeypatch.setitem(sys.modules, "nucleo.banco", None)  # o import falha: sem banco
    monkeypatch.setitem(sys.modules, "nucleo.banco.repositorios", None)
    arquivo.write_text(json.dumps({"vozes": [{"nome": "Ana", "id": "id-ana", "genero": "f", "descricao": ""}]}))
    assert [v.nome for v in tts.catalogo("elevenlabs")] == ["Ana"]


def test_elevenlabs_sintetiza_pelo_id_sem_mudar_a_classe(cache_elevenlabs, http_falso):
    falso = http_falso(CONTA, PCM, PCM)
    usos = []
    voz = provedores.voz("elevenlabs", ao_usar=usos.append, chave="xi")
    voz.sintetizar("Oi.", "Sadaltager", "ignorado", "ignorado")  # voz do Gemini: vira a masculina da conta
    assert falso.pedidos[1]["url"] == "https://api.elevenlabs.io/v1/text-to-speech/id-beto?output_format=pcm_24000"
    assert json.loads(falso.pedidos[1]["corpo"]) == {"text": "Oi.", "model_id": "eleven_multilingual_v2"}
    voz.sintetizar("Oi.", "id-ana", "", "")  # o voice_id direto
    assert "id-ana" in falso.pedidos[2]["url"]
    assert len(falso.pedidos) == 3  # a conta foi listada uma vez só
    assert usos[0].caracteres == 3 and usos[0].provedor == "elevenlabs"
    assert not hasattr(elevenlabs.ElevenLabsTTS, "VOZES") and elevenlabs.ElevenLabsTTS.voz_padrao is None


# ---------------------------------------------------------------- espeak e registro


@pytest.mark.skipif(not shutil.which("espeak-ng"), reason="espeak-ng não instalado")
def test_espeak_de_verdade():
    voz = provedores.voz("espeak")
    wav = voz.sintetizar("Teste de voz.", "Kore", "", "")
    assert wav[:4] == b"RIFF" and duracao(wav) > 0.3
    assert espeak.variante("Kore") == espeak.variante("Kore")
    assert espeak.variante("Kore") in espeak.VARIANTES["f"] and espeak.variante("Charon") in espeak.VARIANTES["m"]


def test_modulos_chamaveis_e_info():
    assert callable(provedores.voz) and provedores.voz is tts
    ids = [i["id"] for i in tts.info()]
    assert ids == list(provedores.VOZ)
    gemini = tts.info()[0]
    assert gemini["modelo_padrao"] == "gemini-3.8-flash-tts" and gemini["precisa_chave"] and gemini["aceita_instrucoes"]
    espeak_info = next(i for i in tts.info() if i["id"] == "espeak")
    assert not espeak_info["precisa_chave"] and not espeak_info["aceita_instrucoes"]
    assert len(tts.catalogo("gemini")) == 30 and len(tts.catalogo("openai")) == 13


def test_fornecedor_sem_chave(monkeypatch):
    from nucleo.provedores.erros import SemChave
    falso = types.SimpleNamespace(ler=lambda p: None, PROVEDORES={"openai": {"nome": "GPT (OpenAI)"}})
    monkeypatch.setitem(sys.modules, "nucleo.chaves", falso)
    monkeypatch.setattr(sys.modules["nucleo"], "chaves", falso, raising=False)
    with pytest.raises(SemChave):
        provedores.voz("openai")
