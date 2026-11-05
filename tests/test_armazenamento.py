"""Armazenamento de objetos: o mesmo contrato na pasta local e no MinIO/S3.

O MinIO só é testado com MINIO_TESTE_ENDPOINT definido (um MinIO de verdade, descartável):

    docker run -d --rm --name minio-teste -p 19000:9000 -e MINIO_ROOT_USER=teste \\
        -e MINIO_ROOT_PASSWORD=testeteste minio/minio server /data
    MINIO_TESTE_ENDPOINT=http://localhost:19000 MINIO_TESTE_USUARIO=teste MINIO_TESTE_SENHA=testeteste \\
        pytest tests/test_armazenamento.py
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from nucleo.armazenamento.base import Armazenamento, mime_de
from nucleo.armazenamento.local import LocalArmazenamento


def _minio(bucket: str) -> Armazenamento:
    from nucleo.armazenamento.s3 import S3Armazenamento
    return S3Armazenamento(bucket, tipo="minio", endpoint=os.environ["MINIO_TESTE_ENDPOINT"],
                           chave=os.environ.get("MINIO_TESTE_USUARIO", "teste"),
                           segredo=os.environ.get("MINIO_TESTE_SENHA", "testeteste"))


@pytest.fixture(params=["local", "minio"])
def arm(request, tmp_path) -> Armazenamento:
    if request.param == "local":
        a = LocalArmazenamento(tmp_path / "objetos")
    else:
        if not os.environ.get("MINIO_TESTE_ENDPOINT"):
            pytest.skip("defina MINIO_TESTE_ENDPOINT para testar contra um MinIO de verdade")
        a = _minio(f"teste-{uuid.uuid4().hex[:12]}")
    a.garantir_bucket()
    a.garantir_bucket()  # idempotente
    yield a
    if request.param == "minio":
        for o in a.listar(""):
            a.apagar(o.chave)
        a.cliente.delete_bucket(Bucket=a.bucket)


def test_enviar_baixar_ler_listar_apagar(arm, tmp_path):
    origem = tmp_path / "roteiro.md"
    origem.write_text("# Roteiro\nolá\n", encoding="utf-8")
    arm.enviar("projetos/x/05-roteiro-v1.md", origem)
    arm.enviar_bytes("projetos/x/09-imagens/B01-01.png", b"\x89PNG fake")
    arm.enviar_bytes("projetos/xy/a.json", b"{}")

    assert arm.existe("projetos/x/05-roteiro-v1.md")
    assert not arm.existe("projetos/x/nao-existe.md")
    assert arm.ler_bytes("projetos/x/09-imagens/B01-01.png") == b"\x89PNG fake"

    destino = tmp_path / "baixado" / "sub" / "r.md"
    arm.baixar("projetos/x/05-roteiro-v1.md", destino)
    assert destino.read_text(encoding="utf-8") == "# Roteiro\nolá\n"
    assert [p.name for p in destino.parent.iterdir()] == ["r.md"]  # sem temporários largados

    objetos = {o.chave: o for o in arm.listar("projetos/x/")}
    assert set(objetos) == {"projetos/x/05-roteiro-v1.md", "projetos/x/09-imagens/B01-01.png"}
    assert objetos["projetos/x/09-imagens/B01-01.png"].tamanho == len(b"\x89PNG fake")
    assert objetos["projetos/x/05-roteiro-v1.md"].modificado is not None
    assert len(list(arm.listar("projetos/"))) == 3

    arm.apagar("projetos/x/05-roteiro-v1.md")
    arm.apagar("projetos/x/05-roteiro-v1.md")  # apagar o que não existe não é erro
    assert not arm.existe("projetos/x/05-roteiro-v1.md")
    with pytest.raises(FileNotFoundError):
        arm.ler_bytes("projetos/x/05-roteiro-v1.md")
    with pytest.raises(FileNotFoundError):
        arm.baixar("projetos/x/05-roteiro-v1.md", tmp_path / "nada.md")
    assert not (tmp_path / "nada.md").exists()
    assert not list((tmp_path).glob(".nada.md*"))


def test_sobrescrever(arm, tmp_path):
    arm.enviar_bytes("a/b.txt", b"um")
    arm.enviar_bytes("a/b.txt", b"dois")
    assert arm.ler_bytes("a/b.txt") == b"dois"
    destino = tmp_path / "b.txt"
    destino.write_bytes(b"velho")
    arm.baixar("a/b.txt", destino)
    assert destino.read_bytes() == b"dois"


def test_url_assinada(arm):
    arm.enviar_bytes("a/b.txt", b"um")
    url = arm.url_assinada("a/b.txt", 60)
    if arm.tipo == "local":
        assert url is None
    else:
        import urllib.request
        with urllib.request.urlopen(url, timeout=10) as r:
            assert r.read() == b"um"


def test_local_recusa_chave_fora_da_pasta(tmp_path):
    a = LocalArmazenamento(tmp_path / "objetos")
    for chave in ("../fora.txt", "/abs.txt", "a/../../b", "", "a//b"):
        with pytest.raises(ValueError):
            a.enviar_bytes(chave, b"x")


def test_mime():
    assert mime_de("projetos/x/a.md") == "text/markdown"
    assert mime_de("x.jsonl") == "application/x-ndjson"
    assert mime_de("x.mp4") == "video/mp4"
    assert mime_de("x.wav") in ("audio/wav", "audio/x-wav")
    assert mime_de("x.desconhecido") == "application/octet-stream"


def test_singleton_pela_configuracao(ambiente, monkeypatch):
    from nucleo import armazenamento as pacote
    from nucleo.config import config
    a = pacote.armazenamento()
    assert a is pacote.armazenamento() and a.tipo == "local"

    monkeypatch.setenv("ARMAZENAMENTO", "minio")
    monkeypatch.setenv("MINIO_ENDPOINT", "http://minio:9000")
    monkeypatch.setenv("MINIO_BUCKET", "videos-teste")
    config.cache_clear()
    pacote.redefinir()
    m = pacote.armazenamento()  # criar o cliente não conecta
    assert m.tipo == "minio" and m.bucket == "videos-teste" and m.endpoint == "http://minio:9000"
    assert m.cliente.meta.config.s3["addressing_style"] == "path"
    assert "minio:9000" in m.descricao()

    monkeypatch.setenv("ARMAZENAMENTO", "s3")
    monkeypatch.setenv("AWS_S3_BUCKET", "meu-bucket")
    monkeypatch.setenv("AWS_REGION", "sa-east-1")
    config.cache_clear()
    pacote.redefinir()
    s = pacote.armazenamento()
    assert s.tipo == "s3" and s.bucket == "meu-bucket" and s.endpoint is None
    assert s.cliente.meta.region_name == "sa-east-1"


def test_minio_arquivo_grande_em_partes(tmp_path):
    if not os.environ.get("MINIO_TESTE_ENDPOINT"):
        pytest.skip("defina MINIO_TESTE_ENDPOINT para testar contra um MinIO de verdade")
    a = _minio(f"teste-{uuid.uuid4().hex[:12]}")
    a.garantir_bucket()
    try:
        grande = tmp_path / "video.mp4"
        with open(grande, "wb") as f:
            for _ in range(40):  # 40 MB: passa do limiar de 16 MB e sobe em partes
                f.write(os.urandom(1024 * 1024))
        a.enviar("projetos/x/10-video/x.mp4", grande)
        objeto = next(iter(a.listar("projetos/x/10-video/")))
        assert objeto.tamanho == grande.stat().st_size
        assert "-" in (objeto.etag or "")  # etag de multipart: <md5>-<partes>
        destino = tmp_path / "volta.mp4"
        a.baixar("projetos/x/10-video/x.mp4", destino)
        assert destino.read_bytes() == grande.read_bytes()
        head = a.cliente.head_object(Bucket=a.bucket, Key="projetos/x/10-video/x.mp4")
        assert head["ContentType"] == "video/mp4"
    finally:
        for o in a.listar(""):
            a.apagar(o.chave)
        a.cliente.delete_bucket(Bucket=a.bucket)


def test_minio_pelo_espaco(ambiente, monkeypatch):
    """O espaço de trabalho inteiro sobre o MinIO: enviar de uma raiz e baixar em outra."""
    if not os.environ.get("MINIO_TESTE_ENDPOINT"):
        pytest.skip("defina MINIO_TESTE_ENDPOINT para testar contra um MinIO de verdade")
    from nucleo import armazenamento as pacote
    from nucleo import espaco
    from nucleo.config import config
    bucket = f"teste-{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("ARMAZENAMENTO", "minio")
    monkeypatch.setenv("MINIO_ENDPOINT", os.environ["MINIO_TESTE_ENDPOINT"])
    monkeypatch.setenv("MINIO_ACCESS_KEY", os.environ.get("MINIO_TESTE_USUARIO", "teste"))
    monkeypatch.setenv("MINIO_SECRET_KEY", os.environ.get("MINIO_TESTE_SENHA", "testeteste"))
    monkeypatch.setenv("MINIO_BUCKET", bucket)
    monkeypatch.setenv("ARMAZENAMENTO_PREFIXO", "instalacao-1")
    config.cache_clear()
    pacote.redefinir()
    a = pacote.armazenamento()
    a.garantir_bucket()
    try:
        pasta = ambiente / "projetos" / "x"
        pasta.mkdir(parents=True)
        (pasta / "05-roteiro-v1.md").write_text("roteiro", encoding="utf-8")
        assert espaco.enviar("projetos/x/") == 1
        assert a.existe("instalacao-1/projetos/x/05-roteiro-v1.md")

        outra = ambiente.parent / "outra-raiz"
        outra.mkdir()
        monkeypatch.setenv("APP_RAIZ", str(outra))
        config.cache_clear()
        espaco.redefinir()
        assert espaco.baixar("projetos/x/") == 1
        assert (outra / "projetos" / "x" / "05-roteiro-v1.md").read_text(encoding="utf-8") == "roteiro"
    finally:
        for o in a.listar(""):
            a.apagar(o.chave)
        a.cliente.delete_bucket(Bucket=a.bucket)
