"""Modelos de imagem da OpenAI (gpt-image), pela Images API.

Sem referências, o pedido vai para /v1/images/generations; com as fichas de referência, para /v1/images/edits
(multipart, uma parte `image[]` por ficha), que é o que mantém o personagem igual entre as cenas.

Esses modelos só geram 1024x1024, 1536x1024 e 1024x1536. O adaptador escolhe o tamanho mais perto da proporção
do vídeo, acrescenta ao texto uma linha pedindo a composição dentro da área segura dessa proporção e recorta o
centro com o ffmpeg até a proporção pedida (sem ffmpeg, a imagem fica como veio e a montagem a encaixa sobre o
fundo desfocado). A qualidade do pedido (rascunho, padrao, maxima) vira `quality` low, medium e high.

O uso devolvido é o `usage` da API, acrescido das chaves no formato do Gemini (promptTokenCount,
candidatesTokensDetails) para o cálculo de custo de ferramentas/custos.py.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess

from nucleo.provedores import http
from nucleo.provedores.erros import Bloqueado, ErroProvedor
from nucleo.provedores.imagem.base import EXTENSOES, ImagemGerada, PedidoImagem, ProvedorImagem

API = "https://api.openai.com/v1/images/{acao}"
TAMANHOS = {"1024x1024": 1.0, "1536x1024": 1.5, "1024x1536": 1024 / 1536}
QUALIDADE_API = {"rascunho": "low", "padrao": "medium", "maxima": "high"}
MODERACAO = re.compile(r"moderation|safety system|content[_ ]policy|safety_violations", re.I)


def razao(proporcao: str) -> float:
    try:
        largura, altura = (float(x) for x in proporcao.split(":"))
        return largura / altura
    except ValueError:
        return 16 / 9


def tamanho_para(proporcao: str) -> str:
    """O tamanho suportado com a proporção mais próxima da pedida."""
    alvo = razao(proporcao)
    return min(TAMANHOS, key=lambda t: abs(TAMANHOS[t] - alvo))


def linha_de_composicao(proporcao: str) -> str:
    return (f"\n\nCompose for a {proporcao} frame: the image will be center-cropped to {proporcao}, so keep every "
            f"character, face and important element inside the central {proporcao} safe area, away from the edges "
            "that will be cut.")


def recortar(dados: bytes, tamanho: str, proporcao: str) -> bytes:
    """Recorta o centro da imagem até a proporção pedida (PNG). Sem ffmpeg, ou se ele falhar, devolve como veio."""
    largura, altura = (int(x) for x in tamanho.split("x"))
    alvo = razao(proporcao)
    if abs(largura / altura - alvo) < 0.01 or not shutil.which("ffmpeg"):
        return dados
    if largura / altura > alvo:
        novo_l, novo_a = int(altura * alvo) // 2 * 2, altura
    else:
        novo_l, novo_a = largura, int(largura / alvo) // 2 * 2
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-vf", f"crop={novo_l}:{novo_a}",
                        "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
                       input=dados, capture_output=True)
    return r.stdout if r.returncode == 0 and r.stdout else dados


def uso_compativel(usage: dict) -> dict:
    """O usage da OpenAI com as chaves que ferramentas/custos.py já sabe somar (formato usageMetadata do Gemini)."""
    detalhes = usage.get("input_tokens_details") or {}
    saida = usage.get("output_tokens") or 0
    return {**usage,
            "promptTokenCount": usage.get("input_tokens") or 0,
            "promptTokensDetails": [{"modality": "TEXT", "tokenCount": detalhes.get("text_tokens") or 0},
                                    {"modality": "IMAGE", "tokenCount": detalhes.get("image_tokens") or 0}],
            "candidatesTokenCount": saida,
            "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": saida}]}


class OpenAIImagem(ProvedorImagem):
    id = "openai"
    nome = "OpenAI (gpt-image)"
    modelo_padrao = "gpt-image-1"
    modelos = ("gpt-image-1", "gpt-image-1-mini", "gpt-image-1.5", "gpt-image-2")
    modelos_por_qualidade = {"rascunho": "gpt-image-1-mini", "padrao": "gpt-image-1", "maxima": "gpt-image-1"}
    max_referencias = 6  # a API aceita mais; o limite é o mesmo do Gemini para o prompt ser o mesmo
    tamanhos = tuple(TAMANHOS)

    def campos(self, pedido: PedidoImagem) -> tuple[str, dict]:
        """(tamanho, campos do pedido) sem as imagens: o mesmo texto do Gemini mais a linha da área segura."""
        tamanho = tamanho_para(pedido.proporcao)
        texto = pedido.texto
        if abs(TAMANHOS[tamanho] - razao(pedido.proporcao)) >= 0.01:
            texto += linha_de_composicao(pedido.proporcao)
        return tamanho, {"model": self.modelo, "prompt": texto, "n": 1, "size": tamanho,
                         "quality": QUALIDADE_API.get(pedido.qualidade or "padrao", "medium")}

    def _pedir(self, pedido: PedidoImagem) -> dict:
        tamanho, campos = self.campos(pedido)
        cabecalhos = {"Authorization": f"Bearer {self.chave}"}
        if not pedido.referencias:
            return http.pedir_json(API.format(acao="generations"), campos, cabecalhos, timeout=300, provedor=self.id)
        partes: list = [(k, str(v)) for k, v in campos.items()]
        for n, ref in enumerate(pedido.referencias, 1):
            partes.append(("image[]", (f"referencia-{n}.{EXTENSOES.get(ref.mime, 'png')}", ref.mime, ref.dados)))
        corpo, tipo = http.multipart(partes)
        bruto, _ = http.pedir(API.format(acao="edits"), corpo, {**cabecalhos, "Content-Type": tipo}, timeout=300,
                              provedor=self.id, espera_inicial=10)
        try:
            return json.loads(bruto)
        except json.JSONDecodeError:
            raise ErroProvedor(f"resposta que não é JSON: {bruto[:200]!r}", self.id) from None

    def gerar(self, pedido: PedidoImagem) -> ImagemGerada:
        try:
            dados = self._pedir(pedido)
        except Bloqueado:
            raise
        except ErroProvedor as e:
            if e.status == 400 and MODERACAO.search(str(e)):
                raise Bloqueado(f"pedido bloqueado pelo filtro da OpenAI ({str(e)[:200]})", self.id, e.status) from None
            raise
        itens = dados.get("data") or []
        if not itens or not itens[0].get("b64_json"):
            raise ErroProvedor(f"nenhuma imagem na resposta {json.dumps(dados)[:200]}", self.id)
        tamanho = tamanho_para(pedido.proporcao)
        imagem = recortar(base64.b64decode(itens[0]["b64_json"]), tamanho, pedido.proporcao)
        uso = uso_compativel(dados.get("usage") or {})
        self._usou(uso, tamanho)
        return ImagemGerada(imagem, "png", uso)
