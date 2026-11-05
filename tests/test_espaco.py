"""Espaço de trabalho (nucleo/espaco.py): a cópia local dos arquivos sincronizada com o armazenamento e o índice
do banco. Dois processos são simulados por duas raízes (APP_RAIZ) que dividem o mesmo banco e o mesmo
armazenamento, como a web e o trabalhador num servidor.

Cada teste roda sobre SQLite e uma pasta local e, com PG_TESTE_URL e MINIO_TESTE_ENDPOINT definidos, de novo
sobre um Postgres e um MinIO de verdade (o Postgres apontado é apagado a cada teste: use um descartável):

    PG_TESTE_URL=postgresql+psycopg://postgres:teste@localhost:15432/postgres \\
    MINIO_TESTE_ENDPOINT=http://localhost:19000 MINIO_TESTE_USUARIO=teste MINIO_TESTE_SENHA=testeteste \\
        pytest tests/test_espaco.py
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from nucleo import armazenamento as pacote_armazenamento
from nucleo import banco, espaco
from nucleo.armazenamento import armazenamento
from nucleo.banco import repositorios as repo
from nucleo.config import config

# uma data com microssegundos (o que o banco guarda), para conferir o mtime restaurado sem arredondamento
MTIME_NS = 1_700_000_000_123_456_000


def escrever(raiz: Path, caminho: str, conteudo: str | bytes) -> Path:
    arquivo = raiz / caminho
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(conteudo, bytes):
        arquivo.write_bytes(conteudo)
    else:
        arquivo.write_text(conteudo, encoding="utf-8")
    return arquivo


def ler(raiz: Path, caminho: str) -> str:
    return (raiz / caminho).read_text(encoding="utf-8")


def sha(texto: str | bytes) -> str:
    return hashlib.sha256(texto.encode() if isinstance(texto, str) else texto).hexdigest()


def objeto(caminho: str) -> bytes | None:
    """O conteúdo guardado no armazenamento para um caminho lógico (None se não há objeto)."""
    try:
        return armazenamento().ler_bytes(espaco.chave_objeto(caminho))
    except FileNotFoundError:
        return None


@pytest.fixture(params=["sqlite-pasta", "postgres-minio"])
def ambiente(ambiente, request, monkeypatch):
    """O `ambiente` do conftest (SQLite e pasta local) e, com os serviços de teste no ar, o mesmo sobre um Postgres
    (do zero, pela migração do Alembic) e um bucket novo no MinIO."""
    if request.param == "sqlite-pasta":
        yield ambiente
        return
    url, endpoint = os.environ.get("PG_TESTE_URL"), os.environ.get("MINIO_TESTE_ENDPOINT")
    if not (url and endpoint):
        pytest.skip("defina PG_TESTE_URL e MINIO_TESTE_ENDPOINT para testar contra Postgres e MinIO de verdade")
    from sqlalchemy import create_engine, text
    banco.redefinir()
    motor = create_engine(url)
    with motor.begin() as con:  # banco descartável: começa do zero
        con.execute(text("DROP SCHEMA public CASCADE"))
        con.execute(text("CREATE SCHEMA public"))
    motor.dispose()
    bucket = f"teste-espaco-{uuid.uuid4().hex[:12]}"
    for nome, valor in {"DATABASE_URL": url, "ARMAZENAMENTO": "minio", "MINIO_ENDPOINT": endpoint, "MINIO_BUCKET": bucket,
                        "MINIO_ACCESS_KEY": os.environ.get("MINIO_TESTE_USUARIO", "teste"),
                        "MINIO_SECRET_KEY": os.environ.get("MINIO_TESTE_SENHA", "testeteste")}.items():
        monkeypatch.setenv(nome, valor)
    config.cache_clear()
    pacote_armazenamento.redefinir()
    banco.iniciar()
    minio = armazenamento()
    minio.garantir_bucket()
    try:
        yield ambiente
    finally:
        for o in minio.listar(""):
            minio.apagar(o.chave)
        minio.cliente.delete_bucket(Bucket=bucket)
        banco.redefinir()


@pytest.fixture
def raizes(ambiente, tmp_path, monkeypatch):
    """Duas cópias locais (a web e o trabalhador, digamos) do mesmo banco e do mesmo armazenamento.
    `usar(raiz)` troca o processo da vez; cada raiz tem o próprio índice local."""
    outra = tmp_path / "raiz-b"
    outra.mkdir()

    def usar(raiz: Path) -> Path:
        monkeypatch.setenv("APP_RAIZ", str(raiz))
        config.cache_clear()
        return raiz

    return SimpleNamespace(a=ambiente, b=outra, usar=usar)


# ------------------------------------------------------------------------------------------- regras de caminho

@pytest.mark.parametrize("caminho", ["", "../fora.md", "/etc/passwd", "a//b", "a/./b", "a/../b", "a\\b", "a\x00b"])
def test_validar_recusa_caminhos_perigosos(caminho):
    with pytest.raises(ValueError):
        espaco.validar(caminho)


def test_escopo_sincronizados_e_ignorados():
    assert espaco.escopo_de("projetos/x/05-roteiro-v1.md") == ("projeto", "x")
    assert espaco.escopo_de("biblioteca/esqueletos/e.json") == ("biblioteca", None)
    assert espaco.escopo_de("pedidos/contextos/novela/contexto.md") == ("contexto", None)
    assert espaco.escopo_de("pedidos/web/x.yaml") == ("pedido", None)
    assert espaco.escopo_de("rascunhos/personagens/p/ficha.json") == ("rascunho", None)

    assert espaco.sincronizado("biblioteca/tecnicas.json") and espaco.sincronizado("biblioteca/musicas/a.mp3")
    assert not espaco.sincronizado("biblioteca/rubrica.json")  # vem na imagem, com o código
    assert not espaco.sincronizado("biblioteca/tecnicas.json.bak")
    assert not espaco.sincronizado("dados/web.sqlite3")

    for caminho in ("projetos/x/montagem-3/quadro.png", "projetos/x/__pycache__/m.pyc", "projetos/x/a.tmp",
                    "projetos/x/.capa.jpg", "projetos/x/09-imagens/.DS_Store", "projetos/x/.r.md.abc.gravando",
                    "projetos/x/.r.md.abc.parcial"):
        assert espaco.ignorado(caminho), caminho
    assert not espaco.ignorado("projetos/x/montagem.md")
    assert not espaco.ignorado("projetos/x/10-video/x.mp4")


# --------------------------------------------------------------------------------------------- enviar e baixar

def test_enviar_so_o_que_mudou_pelo_sha(ambiente, monkeypatch):
    escrever(ambiente, "projetos/x/a.md", "um")
    escrever(ambiente, "projetos/x/b.json", "{}")
    assert espaco.enviar("projetos/x/") == 2
    assert objeto("projetos/x/a.md") == b"um"
    assert repo.arquivo("projetos/x/a.md")["versao"] == 1

    # nada mudou: nem o sha256 é recalculado (tamanho e mtime iguais aos do índice local)
    calculos = []
    original = espaco._sha256
    monkeypatch.setattr(espaco, "_sha256", lambda arquivo: calculos.append(arquivo) or original(arquivo))
    assert espaco.enviar("projetos/x/") == 0
    assert calculos == []

    # só a data mudou: o conteúdo é o mesmo, então não sobe nem muda a versão
    os.utime(ambiente / "projetos/x/a.md", (1_600_000_000, 1_600_000_000))
    assert espaco.enviar("projetos/x/") == 0
    assert repo.arquivo("projetos/x/a.md")["versao"] == 1

    escrever(ambiente, "projetos/x/a.md", "um dois")
    assert espaco.enviar("projetos/x/") == 1
    remoto = repo.arquivo("projetos/x/a.md")
    assert remoto["versao"] == 2 and remoto["sha256"] == sha("um dois")
    assert objeto("projetos/x/a.md") == "um dois".encode()

    indice = json.loads((ambiente / ".espaco" / "indice.json").read_text(encoding="utf-8"))["arquivos"]
    entrada = indice["projetos/x/a.md"]
    assert entrada["sha256"] == sha("um dois") and entrada["versao"] == 2 and entrada["tamanho"] == len("um dois")
    assert entrada["mtime_ns"] == (ambiente / "projetos/x/a.md").stat().st_mtime_ns


def test_enviar_um_arquivo_so_e_o_prefixo_do_armazenamento(ambiente, monkeypatch):
    monkeypatch.setenv("ARMAZENAMENTO_PREFIXO", "instalacao-1")
    config.cache_clear()
    escrever(ambiente, "biblioteca/tecnicas.json", "[]")
    escrever(ambiente, "biblioteca/esqueletos/e.json", "{}")
    assert espaco.enviar("biblioteca/tecnicas.json") == 1  # o prefixo é um arquivo: só ele
    assert armazenamento().existe("instalacao-1/biblioteca/tecnicas.json")
    assert set(repo.indice("biblioteca/")) == {"biblioteca/tecnicas.json"}  # o índice guarda o caminho lógico


def test_baixar_traz_o_que_falta_e_a_versao_remota_mais_nova(raizes, caplog):
    a = raizes.usar(raizes.a)
    escrever(a, "projetos/x/r.md", "v1")
    escrever(a, "projetos/x/09-imagens/B01.png", b"\x89PNG um")
    espaco.enviar("projetos/x/")

    b = raizes.usar(raizes.b)
    assert espaco.baixar("projetos/x/") == 2
    assert ler(b, "projetos/x/r.md") == "v1"
    assert (b / "projetos/x/09-imagens/B01.png").read_bytes() == b"\x89PNG um"
    assert espaco.baixar("projetos/x/") == 0  # nada novo

    raizes.usar(a)
    escrever(a, "projetos/x/r.md", "versão 2")
    espaco.enviar("projetos/x/")
    raizes.usar(b)
    assert espaco.baixar("projetos/x/") == 1
    assert ler(b, "projetos/x/r.md") == "versão 2"

    # mudou aqui e no armazenamento: o remoto ganha, com aviso no log
    escrever(b, "projetos/x/r.md", "mudança local em b")
    raizes.usar(a)
    escrever(a, "projetos/x/r.md", "versão 3, de a")
    espaco.enviar("projetos/x/")
    raizes.usar(b)
    with caplog.at_level(logging.WARNING, logger="nucleo.espaco"):
        assert espaco.baixar("projetos/x/") == 1
    assert ler(b, "projetos/x/r.md") == "versão 3, de a"
    assert "fica a versão do armazenamento" in caplog.text

    # o mesmo conteúdo que o remoto (outro caminho até ele): só o índice local muda, sem baixar
    raizes.usar(a)
    escrever(a, "projetos/x/s.md", "igual")
    espaco.enviar("projetos/x/")
    raizes.usar(b)
    escrever(b, "projetos/x/s.md", "igual")
    assert espaco.baixar("projetos/x/") == 0
    assert espaco.enviar("projetos/x/") == 0


def test_apagado_no_remoto_apaga_aqui(raizes, caplog):
    a = raizes.usar(raizes.a)
    escrever(a, "projetos/x/sub/r.md", "r")
    escrever(a, "projetos/x/s.md", "s")
    espaco.enviar("projetos/x/")
    b = raizes.usar(raizes.b)
    espaco.baixar("projetos/x/")

    raizes.usar(a)
    (a / "projetos/x/sub/r.md").unlink()
    (a / "projetos/x/s.md").unlink()
    assert espaco.enviar("projetos/x/") == 2  # sumiu do disco depois de sincronizado: apagado no remoto
    for caminho in ("projetos/x/sub/r.md", "projetos/x/s.md"):
        remoto = repo.arquivo(caminho)
        assert remoto["apagado_em"] and remoto["versao"] == 2
        assert repo.texto_do_arquivo(caminho) is None
        assert objeto(caminho) is None

    raizes.usar(b)
    escrever(b, "projetos/x/s.md", "s mudado em b")  # mudou aqui: a cópia local fica
    with caplog.at_level(logging.WARNING, logger="nucleo.espaco"):
        assert espaco.baixar("projetos/x/") == 0
    assert not (b / "projetos/x/sub").exists()  # a pasta que ficou vazia sai junto
    assert ler(b, "projetos/x/s.md") == "s mudado em b"
    assert "mudou aqui" in caplog.text
    indice = json.loads((b / ".espaco" / "indice.json").read_text(encoding="utf-8"))["arquivos"]
    assert "projetos/x/sub/r.md" not in indice and "projetos/x/s.md" not in indice

    # a cópia que ficou é uma escrita nova deste processo: o próximo envio a põe de volta
    assert espaco.enviar("projetos/x/") == 1
    assert repo.arquivo("projetos/x/s.md")["apagado_em"] is None
    assert repo.texto_do_arquivo("projetos/x/s.md") == "s mudado em b"


def test_copia_velha_de_um_arquivo_apagado_nao_volta(raizes):
    """Um contêiner novo traz na imagem (ou num espaço antigo) um arquivo que já foi apagado no armazenamento:
    a cópia velha sai daqui em vez de voltar para o armazenamento no próximo envio."""
    a = raizes.usar(raizes.a)
    escrever(a, "biblioteca/musicas/velha.mp3", b"ID3 velha")
    escrever(a, "biblioteca/musicas/fica.mp3", b"ID3 fica")
    espaco.enviar("biblioteca/musicas/")
    assert espaco.apagar("biblioteca/musicas/velha.mp3") == 1

    b = raizes.usar(raizes.b)
    escrever(b, "biblioteca/musicas/velha.mp3", b"ID3 velha")  # a mesma que foi apagada, sem índice local
    espaco.inicializar()
    assert not (b / "biblioteca/musicas/velha.mp3").exists()
    assert (b / "biblioteca/musicas/fica.mp3").read_bytes() == b"ID3 fica"
    assert espaco.enviar("biblioteca/musicas/") == 0
    assert repo.arquivo("biblioteca/musicas/velha.mp3")["apagado_em"]

    # outro conteúdo no mesmo caminho é um arquivo novo: fica e sobe
    escrever(b, "biblioteca/musicas/velha.mp3", b"ID3 outra musica")
    assert espaco.baixar("biblioteca/musicas/") == 0
    assert (b / "biblioteca/musicas/velha.mp3").exists()
    assert espaco.enviar("biblioteca/musicas/") == 1
    assert repo.arquivo("biblioteca/musicas/velha.mp3")["apagado_em"] is None


def test_ignorados_e_fora_dos_sincronizados_nao_sobem(ambiente):
    for caminho in ("projetos/x/montagem-3/quadro.png", "projetos/x/__pycache__/m.pyc", "projetos/x/09-imagens/a.tmp",
                    "projetos/x/.capa.jpg", "projetos/x/.DS_Store", "projetos/x/.r.md.abc.gravando",
                    "projetos/x/.r.md.abc.parcial", "biblioteca/rubrica.json", "biblioteca/formatos.md",
                    "biblioteca/precos.json", "skills/x/SKILL.md"):
        escrever(ambiente, caminho, "x")
    escrever(ambiente, "projetos/x/ok.md", "ok")
    escrever(ambiente, "biblioteca/esqueletos/e.json", "{}")
    escrever(ambiente, "biblioteca/tecnicas.json", "[]")
    assert espaco.enviar("projetos/") == 1
    assert espaco.enviar("biblioteca/") == 2
    assert set(repo.indice("")) == {"projetos/x/ok.md", "biblioteca/esqueletos/e.json", "biblioteca/tecnicas.json"}
    assert {o.chave for o in armazenamento().listar("")} == set(repo.indice(""))


def test_texto_dos_arquivos_de_texto_vai_para_o_banco(ambiente):
    escrever(ambiente, "projetos/x/a.md", "# Título\nolá, mundo")
    escrever(ambiente, "projetos/x/b.json", '{"a": 1}')
    escrever(ambiente, "projetos/x/custos.jsonl", '{"servico": "imagem"}\n')
    escrever(ambiente, "projetos/x/00-pedido.yaml", "tema: polvos\n")
    escrever(ambiente, "projetos/x/10-video/legendas.srt", "1\n00:00:00,000 --> 00:00:01,000\nOi\n")
    escrever(ambiente, "projetos/x/10-video/legendas.ass", "[Script Info]\n")
    escrever(ambiente, "projetos/x/09-imagens/creditos.txt", "créditos")
    escrever(ambiente, "projetos/x/09-imagens/B01.png", b"\x89PNG\r\n\x1a\n")
    escrever(ambiente, "projetos/x/limite.txt", b"a" * espaco.LIMITE_TEXTO)
    escrever(ambiente, "projetos/x/grande.txt", b"a" * (espaco.LIMITE_TEXTO + 1))
    escrever(ambiente, "projetos/x/binario.json", b"\xff\xfe\x00{")
    escrever(ambiente, "projetos/x/nulo.txt", "a\x00b")
    espaco.enviar("projetos/x/")

    assert repo.texto_do_arquivo("projetos/x/a.md") == "# Título\nolá, mundo"
    assert repo.texto_do_arquivo("projetos/x/b.json") == '{"a": 1}'
    for caminho in ("custos.jsonl", "00-pedido.yaml", "10-video/legendas.srt", "10-video/legendas.ass",
                    "09-imagens/creditos.txt"):
        assert repo.texto_do_arquivo(f"projetos/x/{caminho}") == ler(ambiente, f"projetos/x/{caminho}")
    assert repo.texto_do_arquivo("projetos/x/limite.txt") == "a" * espaco.LIMITE_TEXTO  # até 2 MB, inclusive
    for caminho in ("09-imagens/B01.png", "grande.txt", "binario.json", "nulo.txt"):
        assert repo.texto_do_arquivo(f"projetos/x/{caminho}") is None, caminho

    a = repo.arquivo("projetos/x/a.md")
    assert a["mime"] == "text/markdown" and a["escopo"] == "projeto" and a["projeto_slug"] == "x"
    assert a["tamanho"] == len("# Título\nolá, mundo".encode()) and a["sha256"] == sha("# Título\nolá, mundo")
    assert repo.arquivo("projetos/x/09-imagens/B01.png")["mime"] == "image/png"


def test_mtime_original_restaurado_na_baixa(raizes):
    a = raizes.usar(raizes.a)
    os.utime(escrever(a, "projetos/x/05-roteiro-v1.md", "roteiro"), ns=(MTIME_NS, MTIME_NS))
    espaco.enviar("projetos/x/")
    assert repo.arquivo("projetos/x/05-roteiro-v1.md")["modificado_em"] == "2023-11-14T22:13:20.123456+00:00"

    b = raizes.usar(raizes.b)
    espaco.baixar("projetos/x/")
    assert (b / "projetos/x/05-roteiro-v1.md").stat().st_mtime_ns == MTIME_NS
    assert espaco.enviar("projetos/x/") == 0  # a data restaurada não conta como mudança


# ------------------------------------------------------------------------------------- garantir e gravar

def test_garantir(raizes):
    a = raizes.usar(raizes.a)
    escrever(a, "projetos/x/r.md", "r1")
    espaco.enviar("projetos/x/")

    b = raizes.usar(raizes.b)
    caminho = espaco.garantir("projetos/x/r.md")
    assert caminho == b / "projetos/x/r.md" and caminho.read_text(encoding="utf-8") == "r1"

    raizes.usar(a)
    escrever(a, "projetos/x/r.md", "r2, mais novo")
    espaco.enviar("projetos/x/")
    raizes.usar(b)
    assert espaco.garantir("projetos/x/r.md").read_text(encoding="utf-8") == "r2, mais novo"  # estava velho

    with pytest.raises(FileNotFoundError):
        espaco.garantir("projetos/x/nao-existe.md")
    with pytest.raises(ValueError):
        espaco.garantir("../fora.md")

    # fora dos sincronizados (vem na imagem): o arquivo local, sem consultar o banco
    escrever(b, "biblioteca/rubrica.json", "{}")
    assert espaco.garantir("biblioteca/rubrica.json") == b / "biblioteca/rubrica.json"
    with pytest.raises(FileNotFoundError):
        espaco.garantir("biblioteca/formatos.md")

    # o objeto sumiu do armazenamento: o texto guardado no banco põe o arquivo de volta
    raizes.usar(a)
    escrever(a, "projetos/x/t.md", "texto guardado no banco")
    espaco.enviar("projetos/x/")
    armazenamento().apagar(espaco.chave_objeto("projetos/x/t.md"))
    raizes.usar(b)
    assert espaco.garantir("projetos/x/t.md").read_text(encoding="utf-8") == "texto guardado no banco"

    # apagado no armazenamento: a cópia local sai e o arquivo não existe mais
    raizes.usar(a)
    (a / "projetos/x/r.md").unlink()
    espaco.enviar("projetos/x/")
    raizes.usar(b)
    with pytest.raises(FileNotFoundError):
        espaco.garantir("projetos/x/r.md")
    assert not (b / "projetos/x/r.md").exists()


def test_gravar_texto_e_bytes_gravam_e_enviam(ambiente):
    caminho = espaco.gravar_texto("pedidos/web/x.yaml", "tema: polvos\n")
    assert caminho == ambiente / "pedidos/web/x.yaml" and caminho.read_text(encoding="utf-8") == "tema: polvos\n"
    assert objeto("pedidos/web/x.yaml") == b"tema: polvos\n"
    r = repo.arquivo("pedidos/web/x.yaml")
    assert r["versao"] == 1 and r["escopo"] == "pedido" and r["projeto_slug"] is None
    assert repo.texto_do_arquivo("pedidos/web/x.yaml") == "tema: polvos\n"

    espaco.gravar_texto("pedidos/web/x.yaml", "tema: lulas\n")
    assert repo.arquivo("pedidos/web/x.yaml")["versao"] == 2
    espaco.gravar_texto("pedidos/web/x.yaml", "tema: lulas\n")  # o mesmo conteúdo: nenhuma versão nova
    assert repo.arquivo("pedidos/web/x.yaml")["versao"] == 2
    assert not list((ambiente / "pedidos/web").glob(".*"))  # sem temporários largados

    espaco.gravar_bytes("pedidos/contextos/novela/anexo.png", b"\x89PNG")
    anexo = repo.arquivo("pedidos/contextos/novela/anexo.png")
    assert anexo["escopo"] == "contexto" and anexo["mime"] == "image/png"
    assert repo.texto_do_arquivo("pedidos/contextos/novela/anexo.png") is None

    # fora dos sincronizados: só o arquivo local
    espaco.gravar_texto("biblioteca/rubrica.json", "{}")
    assert (ambiente / "biblioteca/rubrica.json").is_file() and repo.arquivo("biblioteca/rubrica.json") is None
    with pytest.raises(ValueError):
        espaco.gravar_texto("../fora.md", "x")


def test_apagar_arquivo_e_pasta(ambiente):
    escrever(ambiente, "projetos/x/a.md", "a")
    escrever(ambiente, "projetos/x/sub/b.md", "b")
    escrever(ambiente, "projetos/xy/c.md", "c")
    espaco.enviar("projetos/")
    escrever(ambiente, "projetos/x/so-local.md", "nunca enviado")

    assert espaco.apagar("projetos/x/a.md") == 1
    assert not (ambiente / "projetos/x/a.md").exists() and objeto("projetos/x/a.md") is None
    assert repo.arquivo("projetos/x/a.md")["apagado_em"]

    assert espaco.apagar("projetos/x") == 2  # sem a barra também é a pasta (e não projetos/xy)
    assert not (ambiente / "projetos/x").exists()
    assert repo.arquivo("projetos/x/sub/b.md")["apagado_em"] and repo.arquivo("projetos/x/so-local.md") is None
    assert (ambiente / "projetos/xy/c.md").exists() and repo.arquivo("projetos/xy/c.md")["apagado_em"] is None
    assert (ambiente / "projetos").is_dir()
    assert espaco.enviar("projetos/") == 0  # o índice local esqueceu o que foi apagado
    with pytest.raises(ValueError):
        espaco.apagar("biblioteca/rubrica.json")


def test_copiar_transforma_o_texto_e_preserva_a_data(ambiente):
    original = "projeto: a\nveja projetos/a/02-dossie.md\n"
    os.utime(escrever(ambiente, "projetos/a/05-roteiro-v1.md", original), ns=(MTIME_NS, MTIME_NS))
    escrever(ambiente, "projetos/a/09-imagens/B01.png", b"\x89PNG projetos/a/")
    escrever(ambiente, "projetos/a/montagem-1/quadro.txt", "ignorado")
    espaco.enviar("projetos/a/")

    def trocar_slug(texto: str) -> str:
        return texto.replace("projetos/a/", "projetos/a-fork/").replace("projeto: a\n", "projeto: a-fork\n")

    assert espaco.copiar("projetos/a/", "projetos/a-fork/", trocar_slug) == 2
    novo = ambiente / "projetos/a-fork/05-roteiro-v1.md"
    assert novo.read_text(encoding="utf-8") == "projeto: a-fork\nveja projetos/a-fork/02-dossie.md\n"
    assert novo.stat().st_mtime_ns == MTIME_NS
    assert (ambiente / "projetos/a-fork/09-imagens/B01.png").read_bytes() == b"\x89PNG projetos/a/"  # binário intacto
    assert not (ambiente / "projetos/a-fork/montagem-1").exists()
    assert ler(ambiente, "projetos/a/05-roteiro-v1.md") == original

    copia = repo.indice("projetos/a-fork/")
    assert set(copia) == {"projetos/a-fork/05-roteiro-v1.md", "projetos/a-fork/09-imagens/B01.png"}
    assert {r["projeto_slug"] for r in copia.values()} == {"a-fork"}
    assert copia["projetos/a-fork/05-roteiro-v1.md"]["modificado_em"].startswith("2023-11-14T22:13:20.123456")
    assert repo.texto_do_arquivo("projetos/a-fork/05-roteiro-v1.md") == novo.read_text(encoding="utf-8")
    assert objeto("projetos/a-fork/05-roteiro-v1.md") == novo.read_bytes()

    # sem transformação, uma cópia byte a byte; o prefixo é de texto (só os roteiros)
    assert espaco.copiar("projetos/a/05-roteiro-", "projetos/b/05-roteiro-") == 1
    assert ler(ambiente, "projetos/b/05-roteiro-v1.md") == original
    with pytest.raises(ValueError):
        espaco.copiar("projetos/a/", "projetos/c")


# ------------------------------------------------------------------------------------ arranque e migração

def test_inicializar_semeia_o_que_falta_e_baixa_o_resto(raizes):
    a = raizes.usar(raizes.a)
    escrever(a, "projetos/x/05-roteiro-v1.md", "roteiro")
    escrever(a, "biblioteca/tecnicas.json", '[{"id": "escada"}]')
    escrever(a, "biblioteca/rubrica.json", "{}")  # vem na imagem: não circula
    escrever(a, "pedidos/meu.yaml", "tema: x\n")
    espaco.inicializar()
    assert set(repo.indice("")) == {"projetos/x/05-roteiro-v1.md", "biblioteca/tecnicas.json", "pedidos/meu.yaml"}
    assert objeto("biblioteca/tecnicas.json") == b'[{"id": "escada"}]'

    # outro processo, com um arquivo só dele e uma cópia velha da biblioteca (a da imagem)
    b = raizes.usar(raizes.b)
    escrever(b, "rascunhos/personagens/p/ficha.json", "{}")
    escrever(b, "biblioteca/tecnicas.json", "[]")
    espaco.inicializar()
    assert "rascunhos/personagens/p/ficha.json" in repo.indice("rascunhos/")  # a semente sobe o que falta lá
    assert ler(b, "projetos/x/05-roteiro-v1.md") == "roteiro"  # e baixa o que falta aqui
    assert ler(b, "biblioteca/tecnicas.json") == '[{"id": "escada"}]'  # o armazenamento ganha da cópia velha
    assert repo.arquivo("biblioteca/tecnicas.json")["versao"] == 1
    assert not (b / "biblioteca/rubrica.json").exists()

    raizes.usar(a)
    espaco.inicializar()
    assert ler(a, "rascunhos/personagens/p/ficha.json") == "{}"


def test_importar_tudo_envia_as_partes_sincronizadas(raizes):
    a = raizes.usar(raizes.a)
    arquivos = {
        "projetos/x/05-roteiro-v1.md": "r", "projetos/y/00-brief.json": "{}", "pedidos/p.yaml": "tema: p",
        "pedidos/contextos/c/contexto.md": "contexto", "rascunhos/personagens/p/v1.png": b"\x89PNG",
        "biblioteca/esqueletos/e.json": "{}", "biblioteca/personagens/p/personagem.json": "{}",
        "biblioteca/musicas/m.mp3": b"ID3", "biblioteca/vozes/Kore.wav": b"RIFF", "biblioteca/tecnicas.json": "[]",
        "biblioteca/canal.json": "{}", "biblioteca/licoes-roteiro.md": "# Lições",
        "biblioteca/aprendizados.md": "# Aprendizados",
    }
    for caminho, conteudo in arquivos.items():
        escrever(a, caminho, conteudo)
    for caminho in ("biblioteca/rubrica.json", "biblioteca/formatos.md", "biblioteca/precos.json"):
        escrever(a, caminho, "{}")

    resultado = espaco.importar_tudo()
    assert set(resultado) == set(espaco.SINCRONIZADOS)
    assert resultado["projetos/"] == 2 and resultado["pedidos/"] == 2 and resultado["rascunhos/"] == 1
    assert resultado["biblioteca/tecnicas.json"] == 1 and sum(resultado.values()) == len(arquivos)
    assert set(repo.indice("")) == set(arquivos)
    assert repo.arquivo("pedidos/contextos/c/contexto.md")["escopo"] == "contexto"
    assert repo.arquivo("rascunhos/personagens/p/v1.png")["escopo"] == "rascunho"
    assert repo.arquivo("biblioteca/vozes/Kore.wav")["escopo"] == "biblioteca"

    assert espaco.importar_tudo() == {p: 0 for p in espaco.SINCRONIZADOS}  # de novo: nada sobe

    # outra instalação com os mesmos arquivos (o mesmo sha256): nada sobe de novo, nem muda de versão
    b = raizes.usar(raizes.b)
    for caminho in arquivos:
        (b / caminho).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(a / caminho, b / caminho)
    assert espaco.importar_tudo() == {p: 0 for p in espaco.SINCRONIZADOS}
    assert {r["versao"] for r in repo.indice("").values()} == {1}
