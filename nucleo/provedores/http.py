"""HTTP de ida e volta para as APIs dos provedores, só com a biblioteca padrão.

Novas tentativas em limite de taxa e erro do servidor, com a espera que a API sugere ("retry in 12s" do
Gemini, cabeçalho Retry-After) ou espera dobrada até 90 s. "limit: 0" (cota zero no plano gratuito do Gemini),
insufficient_quota e quota_exceeded viram SemCota na hora, sem esperar: tentar de novo não resolve.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from nucleo.provedores.erros import ErroProvedor, SemCota

TIMEOUT_S = 600
TENTATIVAS = 6
REPETIR = (408, 429, 500, 502, 503, 504, 529)
SEM_COTA = re.compile(r"limit: 0\b|insufficient_quota|quota_exceeded|billing", re.I)


def _mensagem(texto: str) -> str:
    try:
        dados = json.loads(texto)
    except json.JSONDecodeError:
        return texto[:400]
    erro = dados.get("error") if isinstance(dados, dict) else None
    erro = erro or (dados.get("detail") if isinstance(dados, dict) else None) or dados
    if isinstance(erro, dict):
        return str(erro.get("message") or erro.get("detail") or erro)[:400]
    return str(erro)[:400]


def _espera(e: urllib.error.HTTPError, msg: str, padrao: float) -> float:
    if m := re.search(r"retry in (\d+(?:\.\d+)?)s", msg or ""):
        return min(90.0, float(m.group(1)) + 1)
    if (depois := e.headers.get("Retry-After") if e.headers else None) and str(depois).replace(".", "").isdigit():
        return min(90.0, float(depois))
    return padrao


def pedir(url: str, corpo: bytes | None, cabecalhos: dict, *, metodo: str = "POST", timeout: int = TIMEOUT_S,
          provedor: str | None = None, espera_inicial: float = 5) -> tuple[bytes, dict]:
    """Faz a requisição e devolve (corpo da resposta, cabeçalhos). Levanta SemCota ou ErroProvedor."""
    req = urllib.request.Request(url, data=corpo, method=metodo, headers=cabecalhos)
    espera = espera_inicial
    for tentativa in range(TENTATIVAS):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            texto = e.read().decode("utf-8", "replace")
            msg = _mensagem(texto)
            if SEM_COTA.search(texto):
                raise SemCota(msg, provedor, e.code) from None
            if e.code in REPETIR and tentativa < TENTATIVAS - 1:
                time.sleep(_espera(e, msg, espera))
                espera = min(90, espera * 2)
                continue
            raise ErroProvedor(f"HTTP {e.code}: {msg}", provedor, e.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if tentativa < TENTATIVAS - 1:
                time.sleep(espera)
                espera = min(90, espera * 2)
                continue
            raise ErroProvedor(f"rede: {e}", provedor) from None
    raise ErroProvedor("sem resposta", provedor)


def pedir_json(url: str, corpo: dict | None, cabecalhos: dict, *, metodo: str = "POST", timeout: int = TIMEOUT_S,
               provedor: str | None = None) -> dict:
    dados = json.dumps(corpo).encode() if corpo is not None else None
    bruto, _ = pedir(url, dados, {"Content-Type": "application/json", **cabecalhos}, metodo=metodo, timeout=timeout,
                     provedor=provedor)
    try:
        return json.loads(bruto) if bruto else {}
    except json.JSONDecodeError:
        raise ErroProvedor(f"resposta que não é JSON: {bruto[:200]!r}", provedor) from None


def pedir_binario(url: str, corpo: dict, cabecalhos: dict, *, timeout: int = 300, provedor: str | None = None) -> bytes:
    """POST com corpo JSON que devolve bytes (áudio). Espera inicial maior, como a narração sempre usou."""
    bruto, _ = pedir(url, json.dumps(corpo).encode(), {"Content-Type": "application/json", **cabecalhos},
                     timeout=timeout, provedor=provedor, espera_inicial=10)
    return bruto


def multipart(campos: list[tuple[str, str | tuple[str, str, bytes]]]) -> tuple[bytes, str]:
    """Corpo multipart/form-data: cada campo é (nome, valor) ou (nome, (arquivo, mime, bytes)).
    Devolve (corpo, content-type). Usado pelo images/edits da OpenAI."""
    fronteira = f"----fronteira{time.time_ns():x}"
    partes: list[bytes] = []
    for nome, valor in campos:
        partes.append(f"--{fronteira}\r\n".encode())
        if isinstance(valor, tuple):
            arquivo, mime, dados = valor
            partes.append(f'Content-Disposition: form-data; name="{nome}"; filename="{arquivo}"\r\n'
                          f"Content-Type: {mime}\r\n\r\n".encode())
            partes.append(dados)
        else:
            partes.append(f'Content-Disposition: form-data; name="{nome}"\r\n\r\n{valor}'.encode())
        partes.append(b"\r\n")
    partes.append(f"--{fronteira}--\r\n".encode())
    return b"".join(partes), f"multipart/form-data; boundary={fronteira}"
