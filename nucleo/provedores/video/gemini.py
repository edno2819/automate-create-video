"""Gemini Omni Flash pela Interactions API: anima a imagem (o primeiro quadro) a partir do texto do movimento.

    POST /v1beta/interactions
    {"model": "gemini-omni-1.1-flash",
     "input": [{"type": "image", "data": <base64>, "mime_type": "image/png"}, {"type": "text", "text": <movimento>}],
     "generation_config": {"video_config": {"task": "image_to_video"}},
     "response_format": {"type": "video", "aspect_ratio": "9:16", "resolution": "720p", "delivery": "uri"},
     "background": false, "store": true, "stream": false}

Com o último quadro (a transição para a cena seguinte), as duas imagens vão em ordem, primeiro e último quadro, e
sem `video_config.task`, como no exemplo de interpolação da documentação. A chamada é síncrona (a resposta vem com o
vídeo pronto, depois de um ou dois minutos). Com "delivery": "uri" (a documentação a indica acima de 4 MB, e um
clipe de 10 s passa disso), o vídeo fica na Files API e a API exige "store": true ("store=true is required when
response format has video delivery set to URI", HTTP 400): o adaptador espera o arquivo ficar ACTIVE, baixa, apaga o
arquivo e apaga a interação guardada (senão ela fica 55 dias no plano pago). A API não tem campo de duração, de
prompt negativo nem de áudio desligado: isso vai no texto, que quem pede monta. Só 16:9 e 9:16; 1080p e 4K são
ampliados a partir de 720p. Os Veo 3.1 em prévia saíram do ar em 22/10/2026: o Omni Flash é o substituto indicado.
"""

from __future__ import annotations

import base64
import json
import re
import time

from nucleo.provedores import http
from nucleo.provedores.erros import Bloqueado, ErroProvedor
from nucleo.provedores.video.base import PedidoVideo, ProvedorVideo, VideoGerado

BASE = "https://generativelanguage.googleapis.com/v1beta/"
TIMEOUT_S = 900
ESPERA_ARQUIVO_S = 2.0
MAX_ESPERA_ARQUIVO_S = 300.0
FILTRO = re.compile(r"\b(safety|blocked|prohibited|policy|responsible ai)\b", re.I)


def achar_video(dados) -> dict | None:
    """A parte com o vídeo da resposta ({"mime_type": "video/mp4", "data" | "uri"}): em `output_video` ou no
    model_output dos `steps`, onde estiver."""
    if isinstance(dados, dict):
        if str(dados.get("mime_type") or dados.get("mimeType") or "").startswith("video/") and (dados.get("data") or dados.get("uri")):
            return dados
        if achado := achar_video(dados.get("output_video")):
            return achado
        valores = dados.values()
    elif isinstance(dados, list):
        valores = dados
    else:
        return None
    return next((v for v in map(achar_video, valores) if v), None)


def id_do_arquivo(uri: str) -> str:
    """O id na Files API de "files/abc", ".../v1beta/files/abc" ou ".../files/abc:download?alt=media"."""
    return uri.split("files/", 1)[-1].split(":", 1)[0].split("?", 1)[0].strip("/")


class GeminiVideo(ProvedorVideo):
    id = "gemini"
    nome = "Gemini (Omni Flash)"
    modelo_padrao = "gemini-omni-1.1-flash"
    modelos = ("gemini-omni-1.1-flash",)
    resolucoes = ("720p", "1080p", "4k")
    proporcoes = ("16:9", "9:16")

    def _cabecalhos(self) -> dict:
        return {"x-goog-api-key": self.chave}

    def corpo(self, pedido: PedidoVideo) -> dict:
        imagens = [pedido.imagem] + ([pedido.ultimo_quadro] if pedido.ultimo_quadro else [])
        corpo = {"model": self.modelo,
                 "input": [*({"type": "image", "data": base64.b64encode(r.dados).decode(), "mime_type": r.mime} for r in imagens),
                           {"type": "text", "text": pedido.texto}],
                 "response_format": {"type": "video", "aspect_ratio": pedido.proporcao, "resolution": pedido.resolucao,
                                     "delivery": "uri"},
                 "background": False, "store": True, "stream": False}  # a entrega por URI exige store
        if not pedido.ultimo_quadro:  # a interpolação entre dois quadros não leva a tarefa image_to_video
            corpo["generation_config"] = {"video_config": {"task": "image_to_video"}}
        return corpo

    def animar(self, pedido: PedidoVideo) -> VideoGerado:
        if pedido.proporcao not in self.proporcoes:
            raise ErroProvedor(f"o {self.nome} só gera vídeo em {' e '.join(self.proporcoes)}", self.id)
        try:
            bruto, _ = http.pedir(BASE + "interactions", json.dumps(self.corpo(pedido)).encode(),
                                  {"Content-Type": "application/json", **self._cabecalhos()}, timeout=TIMEOUT_S,
                                  provedor=self.id, espera_inicial=15)
        except ErroProvedor as e:
            if e.status == 400 and FILTRO.search(str(e)):
                raise Bloqueado(f"pedido recusado pelo filtro do Gemini: {e}", self.id) from None
            raise
        try:
            dados = json.loads(bruto)
        except json.JSONDecodeError:
            raise ErroProvedor(f"resposta que não é JSON: {bruto[:200]!r}", self.id) from None
        video = achar_video(dados)
        if video is None:
            texto = json.dumps(dados, ensure_ascii=False)
            if FILTRO.search(texto):
                raise Bloqueado(f"vídeo recusado pelo filtro do Gemini (status {dados.get('status')})", self.id)
            raise ErroProvedor(f"nenhum vídeo na resposta (status {dados.get('status')}): {texto[:200]}", self.id)
        try:
            conteudo = base64.b64decode(video["data"]) if video.get("data") else self.baixar(video["uri"])
        finally:
            self.apagar_interacao(dados.get("id"))
        uso = dados.get("usage") or dados.get("usageMetadata") or {}
        self._usou(uso, pedido)
        return VideoGerado(conteudo, uso)

    def apagar_interacao(self, id_: str | None) -> None:
        """A interação guardada (store) com as imagens e o texto do pedido: apagada logo depois do download."""
        if not id_:
            return
        try:
            http.pedir(f"{BASE}interactions/{str(id_).split('interactions/', 1)[-1]}", None, self._cabecalhos(),
                       metodo="DELETE", provedor=self.id)
        except ErroProvedor:
            pass  # ela expira sozinha

    def baixar(self, uri: str) -> bytes:
        """O vídeo entregue na Files API: espera ficar ACTIVE, baixa e apaga (ele expiraria sozinho em 48 h)."""
        arquivo = id_do_arquivo(uri)
        limite = time.monotonic() + MAX_ESPERA_ARQUIVO_S
        while True:
            estado = http.pedir_json(f"{BASE}files/{arquivo}", None, self._cabecalhos(), metodo="GET", provedor=self.id)
            if estado.get("state") in (None, "ACTIVE"):
                break
            if estado.get("state") == "FAILED" or time.monotonic() > limite:
                raise ErroProvedor(f"o vídeo não ficou pronto na Files API (estado {estado.get('state')})", self.id)
            time.sleep(ESPERA_ARQUIVO_S)
        dados, _ = http.pedir(f"{BASE}files/{arquivo}:download?alt=media", None, self._cabecalhos(), metodo="GET",
                              provedor=self.id)
        try:
            http.pedir(f"{BASE}files/{arquivo}", None, self._cabecalhos(), metodo="DELETE", provedor=self.id)
        except ErroProvedor:
            pass  # o arquivo expira sozinho
        return dados
