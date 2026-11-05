"""Provedores de imagem: o corpo enviado ao Gemini (o de sempre) e à OpenAI (generations sem referências, edits
multipart com elas), os erros (bloqueio, sem imagem, sem cota) e o recorte para a proporção do vídeo. Sem rede:
o HTTP é simulado trocando nucleo.provedores.http.pedir."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess

import pytest

from nucleo import provedores
from nucleo.provedores import http
from nucleo.provedores.erros import Bloqueado, ErroProvedor
import nucleo.provedores.imagem as provedores_imagem
from nucleo.provedores.imagem import PedidoImagem, Referencia
from nucleo.provedores.imagem import openai as img_openai

PNG_1X1 = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
sem_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg não instalado")


class HttpFalso:
    def __init__(self, *respostas):
        self.respostas = list(respostas)
        self.pedidos = []

    def __call__(self, url, corpo, cabecalhos, *, metodo="POST", timeout=0, provedor=None, espera_inicial=5):
        self.pedidos.append({"url": url, "corpo": corpo, "cabecalhos": cabecalhos})
        resposta = self.respostas.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        return json.dumps(resposta).encode(), {}


@pytest.fixture
def http_falso(monkeypatch):
    def instalar(*respostas):
        falso = HttpFalso(*respostas)
        monkeypatch.setattr(http, "pedir", falso)
        return falso
    return instalar


def png(largura: int, altura: int) -> bytes:
    return subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=red:s={largura}x{altura}",
                           "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
                          capture_output=True, check=True).stdout


def dimensoes(dados: bytes) -> tuple[int, int]:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "json", "-i", "pipe:0"],
                       input=dados, capture_output=True, check=True)
    s = json.loads(r.stdout)["streams"][0]
    return s["width"], s["height"]


# ---------------------------------------------------------------- Gemini


def resposta_gemini(mime="image/jpeg"):
    return {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": mime, "data": base64.b64encode(b"img").decode()}}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1290}]}}


def test_gemini_manda_texto_e_referencias_numa_mensagem(http_falso):
    falso = http_falso(resposta_gemini())
    usos = []
    gerador = provedores.imagem("gemini", "gemini-3-pro-image", ao_usar=usos.append, chave="g-falsa")
    refs = (Referencia(b"ficha1", "image/png"), Referencia(b"ficha2", "image/webp"))
    imagem = gerador.gerar(PedidoImagem("Reference images...\n\nA cat.\n\nAvoid: text.", refs, "9:16", "2K"))
    assert imagem.dados == b"img" and imagem.extensao == "jpg"
    assert imagem.uso["promptTokenCount"] == 10 and usos[0].servico == "imagem" and usos[0].tamanho == "2K"
    pedido = falso.pedidos[0]
    assert pedido["url"].endswith("/models/gemini-3-pro-image:generateContent")
    assert pedido["cabecalhos"]["x-goog-api-key"] == "g-falsa"
    assert json.loads(pedido["corpo"]) == {
        "contents": [{"role": "user", "parts": [
            {"text": "Reference images...\n\nA cat.\n\nAvoid: text."},
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(b"ficha1").decode()}},
            {"inlineData": {"mimeType": "image/webp", "data": base64.b64encode(b"ficha2").decode()}}]}],
        "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "9:16", "imageSize": "2K"}},
    }


def test_gemini_sem_tamanho_nao_manda_image_size(http_falso):
    falso = http_falso(resposta_gemini("image/png"))
    assert provedores.imagem("gemini", chave="x").gerar(PedidoImagem("a")).extensao == "png"
    assert json.loads(falso.pedidos[0]["corpo"])["generationConfig"]["imageConfig"] == {"aspectRatio": "16:9"}


def test_gemini_bloqueio_e_resposta_sem_imagem(http_falso):
    http_falso({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}},
               {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "não posso"}]}}]})
    gerador = provedores.imagem("gemini", chave="x")
    with pytest.raises(Bloqueado, match="PROHIBITED_CONTENT"):
        gerador.gerar(PedidoImagem("a"))
    with pytest.raises(ErroProvedor, match=r"finishReason=STOP\) não posso") as e:
        gerador.gerar(PedidoImagem("a"))
    assert not isinstance(e.value, Bloqueado)


def test_modelos_por_qualidade():
    assert provedores_imagem.modelo_para("gemini", "rascunho") == "gemini-3.1-flash-lite-image"
    assert provedores_imagem.modelo_para("gemini", "padrao") == "gemini-3.1-flash-image"
    assert provedores_imagem.modelo_para("gemini", "maxima") == "gemini-3-pro-image"
    assert provedores_imagem.modelo_para("gemini", None) == "gemini-3.1-flash-image"
    assert provedores_imagem.modelo_para("openai", "rascunho") == "gpt-image-1-mini"
    assert provedores_imagem.modelo_para("openai", "maxima") == "gpt-image-1"
    info = {i["id"]: i for i in provedores_imagem.info()}
    assert info["gemini"]["max_referencias"] == 6 and info["openai"]["modelo_padrao"] == "gpt-image-1"
    assert callable(provedores.imagem) and provedores.imagem is provedores_imagem


# ---------------------------------------------------------------- OpenAI


def test_tamanho_pela_proporcao():
    assert img_openai.tamanho_para("16:9") == "1536x1024"
    assert img_openai.tamanho_para("9:16") == "1024x1536"
    assert img_openai.tamanho_para("4:5") == "1024x1536"
    assert img_openai.tamanho_para("1:1") == "1024x1024"


@sem_ffmpeg
def test_openai_generations_sem_referencias_e_recorte(http_falso):
    falso = http_falso({"data": [{"b64_json": base64.b64encode(png(1536, 1024)).decode()}],
                        "usage": {"input_tokens": 50, "output_tokens": 4160,
                                  "input_tokens_details": {"text_tokens": 50, "image_tokens": 0}}})
    usos = []
    imagem = provedores.imagem("openai", ao_usar=usos.append, chave="sk").gerar(
        PedidoImagem("A cat.\n\nAvoid: text.", (), "16:9", "1K", "maxima"))
    pedido = falso.pedidos[0]
    assert pedido["url"] == "https://api.openai.com/v1/images/generations"
    corpo = json.loads(pedido["corpo"])
    assert corpo["model"] == "gpt-image-1" and corpo["size"] == "1536x1024" and corpo["quality"] == "high"
    assert corpo["prompt"].startswith("A cat.\n\nAvoid: text.\n\nCompose for a 16:9 frame")
    assert "response_format" not in corpo
    assert imagem.extensao == "png" and dimensoes(imagem.dados) == (1536, 864)
    assert imagem.uso["output_tokens"] == 4160 and imagem.uso["candidatesTokensDetails"] == [{"modality": "IMAGE", "tokenCount": 4160}]
    assert imagem.uso["promptTokenCount"] == 50 and usos[0].provedor == "openai"


@sem_ffmpeg
def test_openai_edits_com_referencias_em_multipart(http_falso):
    falso = http_falso({"data": [{"b64_json": base64.b64encode(png(1024, 1024)).decode()}], "usage": {}})
    refs = (Referencia(PNG_1X1, "image/png"), Referencia(b"jpeg", "image/jpeg"))
    imagem = provedores.imagem("openai", "gpt-image-1-mini", chave="sk").gerar(
        PedidoImagem("Reference images are attached: ...\n\nScene.", refs, "1:1", qualidade="rascunho"))
    pedido = falso.pedidos[0]
    assert pedido["url"] == "https://api.openai.com/v1/images/edits"
    assert pedido["cabecalhos"]["Content-Type"].startswith("multipart/form-data; boundary=")
    corpo = pedido["corpo"]
    assert corpo.count(b'name="image[]"') == 2 and PNG_1X1 in corpo and b"Content-Type: image/jpeg" in corpo
    assert b'name="prompt"\r\n\r\nReference images are attached: ...\n\nScene.\r\n' in corpo  # 1:1: sem linha de composição
    assert b'name="quality"\r\n\r\nlow\r\n' in corpo and b'name="model"\r\n\r\ngpt-image-1-mini\r\n' in corpo
    assert dimensoes(imagem.dados) == (1024, 1024)


def test_openai_moderacao_vira_bloqueado(http_falso):
    http_falso(ErroProvedor("HTTP 400: Your request was rejected as a result of our safety system.", "openai", 400),
               ErroProvedor("HTTP 400: Invalid size", "openai", 400))
    gerador = provedores.imagem("openai", chave="sk")
    with pytest.raises(Bloqueado):
        gerador.gerar(PedidoImagem("a"))
    with pytest.raises(ErroProvedor) as e:
        gerador.gerar(PedidoImagem("a"))
    assert not isinstance(e.value, Bloqueado)


def test_openai_sem_imagem(http_falso):
    http_falso({"data": []})
    with pytest.raises(ErroProvedor, match="nenhuma imagem"):
        provedores.imagem("openai", chave="sk").gerar(PedidoImagem("a", proporcao="1:1"))
