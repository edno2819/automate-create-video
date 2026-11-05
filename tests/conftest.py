"""Fixtures comuns dos testes.

`ambiente` (não é autouse: peça-a no teste que precisa de banco, armazenamento ou espaço de trabalho) monta uma
instalação inteira e isolada numa pasta temporária, sem rede e sem chaves reais:

- RAIZ temporária em `APP_RAIZ` (o espaço de trabalho: `projetos/`, `biblioteca/`... ficam lá; o código continua
  no repositório, em nucleo.RAIZ). O `.env` do repositório nunca é lido;
- banco SQLite em `<raiz>/dados/teste.sqlite3` (`DATABASE_URL`), já criado (`banco.iniciar()`);
- armazenamento `local` numa pasta irmã da raiz (`ARMAZENAMENTO_PASTA`);
- `APP_SEGREDO` gerado na hora (as chaves de API ficam cifradas no banco como em produção);
- as variáveis de chave dos provedores, de MinIO/S3, `EXECUCAO_ID` e autenticação tiradas do ambiente;
- os singletons (configuração, motor do banco, armazenamento, cifra, índices do espaço) recriados antes e
  depois do teste.

Devolve a RAIZ temporária (Path):

    def test_algo(ambiente):
        from nucleo.banco import repositorios as repo
        repo.criar_projeto("x", "X", {})
        (ambiente / "projetos" / "x").mkdir(parents=True)

`objetos_locais` devolve a pasta do armazenamento local, para conferir o que foi enviado.
"""

from __future__ import annotations

import pytest

VARIAVEIS_LIMPAS = (
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ELEVENLABS_API_KEY", "YOUTUBE_API_KEY",
    "MINIO_ENDPOINT", "MINIO_BUCKET", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD",
    "AWS_S3_BUCKET", "AWS_S3_ENDPOINT", "ARMAZENAMENTO_BUCKET", "ARMAZENAMENTO_PREFIXO", "EXECUCAO_ID",
    "APP_USUARIO", "APP_SENHA", "TRABALHADOR_EMBUTIDO", "TRABALHADOR_CONCORRENCIA", "WHISPER_MODELO",
)


def _redefinir_tudo() -> None:
    from nucleo import armazenamento, banco, espaco, seguranca
    from nucleo.config import config
    banco.redefinir()
    armazenamento.redefinir()
    seguranca.redefinir()
    espaco.redefinir()
    config.cache_clear()


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    raiz = tmp_path / "raiz"
    (raiz / "dados").mkdir(parents=True)
    for variavel in VARIAVEIS_LIMPAS:
        monkeypatch.delenv(variavel, raising=False)
    monkeypatch.setenv("APP_RAIZ", str(raiz))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{raiz / 'dados' / 'teste.sqlite3'}")
    monkeypatch.setenv("ARMAZENAMENTO", "local")
    monkeypatch.setenv("ARMAZENAMENTO_PASTA", str(tmp_path / "objetos"))
    monkeypatch.setenv("APP_SEGREDO", Fernet.generate_key().decode())
    _redefinir_tudo()

    from nucleo import banco
    banco.iniciar()
    try:
        yield raiz
    finally:
        _redefinir_tudo()


@pytest.fixture
def objetos_locais(ambiente, tmp_path):
    return tmp_path / "objetos"
