"""Configuração do servidor, lida das variáveis de ambiente (e do .env da raiz, em desenvolvimento).

Nada aqui guarda chave de provedor de LLM, voz ou imagem: essas ficam em nucleo/chaves.py (ambiente > banco).
Aqui ficam só a infraestrutura (banco, armazenamento, trabalhador) e a autenticação da interface.

    from nucleo.config import config
    c = config()
    c.armazenamento   # "minio" | "s3" | "local"
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from nucleo import RAIZ

ARMAZENAMENTOS = ("minio", "s3", "local")
_LINHA = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


@dataclass(frozen=True)
class Configuracao:
    raiz: Path
    banco_url: str
    armazenamento: str
    bucket: str
    s3_endpoint: str | None
    s3_regiao: str
    s3_chave: str | None
    s3_segredo: str | None
    prefixo: str
    pasta_objetos_local: Path
    segredo_app: str | None
    usuario: str | None
    senha: str | None
    concorrencia: int
    trabalhador_embutido: bool
    whisper_modelo: str

    @property
    def banco_e_sqlite(self) -> bool:
        return self.banco_url.startswith("sqlite")


def _env_arquivo(raiz: Path) -> dict[str, str]:
    """O .env da raiz, só como reserva para desenvolvimento: no contêiner tudo chega pelo ambiente."""
    arquivo = raiz / ".env"
    if not arquivo.exists():
        return {}
    valores = {}
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        if linha.lstrip().startswith("#"):
            continue
        if m := _LINHA.match(linha):
            valores[m.group(1)] = m.group(2).strip("\"'")
    return valores


def _bool(valor: str | None) -> bool:
    return (valor or "").strip().lower() in ("1", "true", "sim", "yes", "on")


@lru_cache(maxsize=1)
def config() -> Configuracao:
    raiz = Path(os.environ.get("APP_RAIZ") or RAIZ).resolve()
    arquivo = _env_arquivo(raiz)

    def ler(nome: str, padrao: str | None = None) -> str | None:
        valor = os.environ.get(nome)
        if valor is None or valor == "":
            valor = arquivo.get(nome)
        return valor if valor not in (None, "") else padrao

    escolhido = (ler("ARMAZENAMENTO") or "").lower()
    if escolhido and escolhido not in ARMAZENAMENTOS:
        raise ValueError(f"ARMAZENAMENTO={escolhido}: use um de {', '.join(ARMAZENAMENTOS)}")
    if not escolhido:
        # AWS quando há bucket da AWS configurado; MinIO quando há endpoint do MinIO; senão, pasta local
        escolhido = "s3" if ler("AWS_S3_BUCKET") else "minio" if ler("MINIO_ENDPOINT") else "local"

    if escolhido == "s3":
        bucket = ler("AWS_S3_BUCKET") or ler("ARMAZENAMENTO_BUCKET") or "videos"
        endpoint = ler("AWS_S3_ENDPOINT")
        regiao = ler("AWS_REGION") or ler("AWS_DEFAULT_REGION") or "us-east-1"
        chave, segredo = ler("AWS_ACCESS_KEY_ID"), ler("AWS_SECRET_ACCESS_KEY")
    else:
        bucket = ler("MINIO_BUCKET") or ler("ARMAZENAMENTO_BUCKET") or "videos"
        endpoint = ler("MINIO_ENDPOINT", "http://localhost:9000")
        regiao = ler("MINIO_REGION", "us-east-1")
        chave, segredo = ler("MINIO_ACCESS_KEY") or ler("MINIO_ROOT_USER"), ler("MINIO_SECRET_KEY") or ler("MINIO_ROOT_PASSWORD")

    prefixo = (ler("ARMAZENAMENTO_PREFIXO") or "").strip("/")
    return Configuracao(
        raiz=raiz,
        banco_url=ler("DATABASE_URL") or f"sqlite:///{raiz / 'dados' / 'app.sqlite3'}",
        armazenamento=escolhido,
        bucket=bucket,
        s3_endpoint=endpoint,
        s3_regiao=regiao,
        s3_chave=chave,
        s3_segredo=segredo,
        prefixo=f"{prefixo}/" if prefixo else "",
        pasta_objetos_local=Path(ler("ARMAZENAMENTO_PASTA") or raiz / "dados" / "objetos"),
        segredo_app=ler("APP_SEGREDO"),
        usuario=ler("APP_USUARIO"),
        senha=ler("APP_SENHA"),
        concorrencia=max(1, int(ler("TRABALHADOR_CONCORRENCIA", "2"))),
        trabalhador_embutido=_bool(ler("TRABALHADOR_EMBUTIDO")),
        whisper_modelo=ler("WHISPER_MODELO", "small"),
    )
