"""Interface web do pipeline de vídeos: o app FastAPI (API em web/api/ e a página em web/estatico/).

O usuário preenche o pedido num formulário (só título e descrição são obrigatórios), o sistema sugere o resto, ele
aprova, e cada etapa seguinte vira uma execução na fila do banco. A web só confere e enfileira: quem roda é o
trabalhador (`python -m nucleo.trabalhador`, ou uma thread deste processo com TRABALHADOR_EMBUTIDO=1), que pega as
execuções do banco, roda os agentes pela API dos provedores e os scripts de ferramentas/, e grava o log no banco.
Os arquivos de cada projeto ficam no armazenamento (MinIO ou S3), com a cópia local sincronizada por
nucleo.espaco; o estado (projetos, execuções, custos, configuração, chaves cifradas) fica no banco.

No arranque: o esquema do banco (`banco.iniciar`), o bucket, a semente e a baixa do espaço de trabalho
(`espaco.inicializar`) e, se pedido, o trabalhador embutido. Autenticação básica opcional (APP_USUARIO e
APP_SENHA) em tudo, menos em /api/saude (o healthcheck do contêiner).

Uso:
    python web/servidor.py                        # http://0.0.0.0:8765 (APP_HOST e PORTA mudam o padrão)
    python web/servidor.py --host 127.0.0.1 --porta 8765
    uvicorn web.servidor:app --host 0.0.0.0 --port 8765 --workers 2
"""

from __future__ import annotations

import argparse
import base64
import binascii
import logging
import os
import secrets
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
if str(RAIZ) not in sys.path:  # rodando como script (python web/servidor.py): o código importa a partir da raiz
    sys.path.insert(0, str(RAIZ))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from nucleo import banco, espaco, tecnicas  # noqa: E402
from nucleo.armazenamento import armazenamento  # noqa: E402
from nucleo.config import config  # noqa: E402
from nucleo.pipeline.execucao import Trabalhador  # noqa: E402
from web.api import ROTEADORES  # noqa: E402

log = logging.getLogger("web")

LIVRES = ("/api/saude",)  # sem autenticação
REINO = 'Basic realm="Estudio de videos", charset="UTF-8"'
LOCAIS = ("127.0.0.1", "localhost", "::1")


# ---------------------------------------------------------------- autenticação


def credenciais_validas(cabecalho: bytes | None, usuario: str, senha: str) -> bool:
    if not cabecalho:
        return False
    tipo, _, valor = cabecalho.decode("latin-1").partition(" ")
    if tipo.lower() != "basic" or not valor:
        return False
    try:
        bruto = base64.b64decode(valor.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    try:
        texto = bruto.decode("utf-8")
    except UnicodeDecodeError:
        texto = bruto.decode("latin-1")
    quem, separador, qual = texto.partition(":")
    if not separador:
        return False
    # as duas comparações sempre rodam: o tempo da resposta não diz qual das duas errou
    certo_usuario = secrets.compare_digest(quem.encode(), usuario.encode())
    certa_senha = secrets.compare_digest(qual.encode(), senha.encode())
    return certo_usuario and certa_senha


class AutenticacaoBasica:
    """Autenticação básica (ASGI puro: não atrapalha as respostas em pedaços do vídeo). Ligada quando APP_USUARIO ou
    APP_SENHA está definido; com só um dos dois, o outro vale vazio (a configuração pela metade fecha, não abre).
    A configuração é lida a cada requisição (um lru_cache): os testes ligam e desligam pelo ambiente."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or scope.get("path") in LIVRES:
            return await self.app(scope, receive, send)
        c = config()
        if not (c.usuario or c.senha):
            return await self.app(scope, receive, send)
        cabecalho = dict(scope.get("headers") or []).get(b"authorization")
        if credenciais_validas(cabecalho, c.usuario or "", c.senha or ""):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        resposta = JSONResponse({"detail": "autenticação necessária"}, status_code=401,
                                headers={"WWW-Authenticate": REINO})
        await resposta(scope, receive, send)


# ---------------------------------------------------------------- arranque


def rodar_trabalhador(trabalhador: Trabalhador) -> None:
    try:
        trabalhador.rodar_para_sempre()
    except Exception:  # noqa: BLE001 - a web continua no ar; as execuções esperam um trabalhador separado
        log.exception("o trabalhador embutido parou com erro")


@asynccontextmanager
async def ciclo_de_vida(app_: FastAPI):
    banco.iniciar()
    armazenamento().garantir_bucket()
    tecnicas_da_imagem = tecnicas.da_imagem()  # antes de sincronizar: depois, o arquivo é o do armazenamento
    espaco.inicializar()
    try:
        tecnicas.completar(tecnicas_da_imagem)
    except Exception:  # noqa: BLE001 - a biblioteca antiga continua valendo; o servidor sobe mesmo assim
        log.exception("os ids novos de biblioteca/tecnicas.json não foram acrescentados")
    c = config()
    trabalhador = thread = None
    if c.trabalhador_embutido:
        trabalhador = Trabalhador(c.concorrencia)
        thread = threading.Thread(target=rodar_trabalhador, args=(trabalhador,), name="trabalhador", daemon=True)
        thread.start()
        log.info("trabalhador embutido: %d execução(ões) por vez", c.concorrencia)
    try:
        yield
    finally:
        if trabalhador is not None:
            trabalhador.parar()
            thread.join(timeout=60)


app = FastAPI(title="Estúdio de vídeos", lifespan=ciclo_de_vida)
app.add_middleware(AutenticacaoBasica)
for roteador in ROTEADORES:
    app.include_router(roteador)


@app.exception_handler(Exception)
async def erro_inesperado(request: Request, erro: Exception):
    """Erro não previsto: o motivo completo vai para o log do servidor; a tela recebe só o tipo (a mensagem pode
    trazer caminhos e endereços internos)."""
    log.error("erro em %s %s", request.method, request.url.path, exc_info=erro)
    return JSONResponse({"detail": f"erro interno no servidor ({type(erro).__name__}); veja o log do servidor"},
                        status_code=500)


class SemCache(StaticFiles):
    """O navegador revalida o app.js e o CSS a cada carga: sem isso, uma versão nova só aparece com Ctrl+F5."""

    async def get_response(self, path, scope):
        resposta = await super().get_response(path, scope)
        resposta.headers["Cache-Control"] = "no-cache"
        return resposta


app.mount("/estatico", SemCache(directory=AQUI / "estatico"), name="estatico")


@app.get("/")
def inicio():
    return FileResponse(AQUI / "estatico" / "index.html", headers={"Cache-Control": "no-cache"})


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("APP_HOST") or "0.0.0.0",
                    help="endereço de escuta (padrão: APP_HOST, ou 0.0.0.0)")
    ap.add_argument("--porta", type=int, default=int(os.environ.get("PORTA") or 8765), help="porta (padrão: PORTA, ou 8765)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    c = config()
    if not (c.usuario or c.senha) and args.host not in LOCAIS:
        print(f"aviso: a interface fica aberta em {args.host} sem senha; defina APP_USUARIO e APP_SENHA para expor o "
              "servidor fora desta máquina", file=sys.stderr, flush=True)
    print(f"interface: http://{args.host}:{args.porta}", flush=True)
    uvicorn.run(app, host=args.host, port=args.porta, log_level="warning")


if __name__ == "__main__":
    main()
