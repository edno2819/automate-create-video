"""Chaves de API (nucleo/chaves.py): ordem de leitura (ambiente > banco > .env legado), cifra no banco, recusa
de gravar por cima do ambiente e as variáveis entregues aos scripts de produção. Todas as chaves aqui são
inventadas; o .env lido é o da raiz temporária da fixture `ambiente`, nunca o do repositório."""

from __future__ import annotations

import sys

import pytest
from sqlalchemy import select

from nucleo import RAIZ, banco, chaves, seguranca
from nucleo.config import config

CHAVE_ARQUIVO = "sk-do-arquivo-0001"
CHAVE_BANCO = "sk-do-banco-0002"
CHAVE_AMBIENTE = "sk-do-ambiente-0003"


def env_legado(raiz, **variaveis) -> None:
    (raiz / ".env").write_text("".join(f"{k}={v}\n" for k, v in variaveis.items()), encoding="utf-8")


def test_ordem_ambiente_banco_arquivo(ambiente, monkeypatch):
    assert chaves.ler("openai") is None and chaves.origem("openai") is None

    env_legado(ambiente, OPENAI_API_KEY=CHAVE_ARQUIVO)
    assert chaves.ler("openai") == CHAVE_ARQUIVO and chaves.origem("openai") == "arquivo"

    chaves.gravar("openai", CHAVE_BANCO)
    assert chaves.ler("openai") == CHAVE_BANCO and chaves.origem("openai") == "banco"

    monkeypatch.setenv("OPENAI_API_KEY", CHAVE_AMBIENTE)
    assert chaves.ler("openai") == CHAVE_AMBIENTE and chaves.origem("openai") == "ambiente"

    monkeypatch.delenv("OPENAI_API_KEY")
    assert chaves.ler("openai") == CHAVE_BANCO
    chaves.gravar("openai", None)  # apagar tira só a do banco: a do .env volta a valer
    assert chaves.ler("openai") == CHAVE_ARQUIVO and chaves.origem("openai") == "arquivo"
    (ambiente / ".env").unlink()
    assert chaves.ler("openai") is None


def test_nomes_antigos_aceitos_e_comentarios_do_env(ambiente, monkeypatch):
    (ambiente / ".env").write_text("# GEMINI_API_KEY=comentada\nexport YOUTUBE_API_KEY='yt-entre-aspas-0004'\n"
                                   "ELEVENLABS_API_KEY=\n", encoding="utf-8")
    assert chaves.ler("gemini") is None  # a linha comentada não vale
    assert chaves.ler("youtube") == "yt-entre-aspas-0004"
    assert chaves.ler("elevenlabs") is None  # vazia é o mesmo que não ter
    monkeypatch.setenv("GOOGLE_API_KEY", "nome-antigo-do-gemini-0005")
    assert chaves.ler("gemini") == "nome-antigo-do-gemini-0005" and chaves.origem("gemini") == "ambiente"
    assert chaves.variaveis("gemini") == ["GEMINI_API_KEY", "GOOGLE_API_KEY"]


def test_no_banco_a_chave_fica_cifrada(ambiente, monkeypatch, capsys):
    from nucleo.banco.modelos import ChaveApi

    chaves.gravar("anthropic", f"  {CHAVE_BANCO}  ")  # espaços em volta saem
    with banco.sessao() as s:
        linha = s.scalars(select(ChaveApi).where(ChaveApi.provedor == "anthropic")).one()
    assert CHAVE_BANCO not in linha.valor_cifrado and "0002" not in linha.valor_cifrado
    assert seguranca.decifrar(linha.valor_cifrado) == CHAVE_BANCO
    assert linha.mascara == "••••••••0002"
    assert chaves.ler("anthropic") == CHAVE_BANCO

    # outro APP_SEGREDO não abre a chave guardada: ela não vale (cadastre de novo), sem quebrar nada
    from cryptography.fernet import Fernet
    monkeypatch.setenv("APP_SEGREDO", Fernet.generate_key().decode())
    config.cache_clear()
    seguranca.redefinir()
    assert chaves.ler("anthropic") is None and chaves.origem("anthropic") is None
    assert "não abre com o segredo atual" in capsys.readouterr().err


def test_segredo_de_texto_qualquer_e_arquivo_gerado(ambiente, monkeypatch, capsys):
    monkeypatch.setenv("APP_SEGREDO", "uma frase qualquer como segredo")
    config.cache_clear()
    seguranca.redefinir()
    guardado = seguranca.cifrar("valor")
    assert seguranca.decifrar(guardado) == "valor"

    # sem APP_SEGREDO: dados/segredo.key é gerado (chmod 600) e reaproveitado
    monkeypatch.delenv("APP_SEGREDO")
    config.cache_clear()
    seguranca.redefinir()
    guardado = seguranca.cifrar("valor")
    arquivo = ambiente / "dados" / "segredo.key"
    assert arquivo.is_file() and (arquivo.stat().st_mode & 0o777) == 0o600
    seguranca.redefinir()
    assert seguranca.decifrar(guardado) == "valor"
    with pytest.raises(ValueError):
        seguranca.decifrar("nao-e-um-token")


def test_recusa_gravar_quando_a_chave_vem_do_ambiente(ambiente, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", CHAVE_AMBIENTE)
    with pytest.raises(ValueError, match="variável de ambiente"):
        chaves.gravar("gemini", CHAVE_BANCO)
    with pytest.raises(ValueError):
        chaves.gravar("gemini", None)  # nem apagar: quem definiu no ambiente é quem troca
    from nucleo.banco import repositorios
    assert repositorios.ler_chave("gemini") is None

    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.setenv("GOOGLE_API_KEY", CHAVE_AMBIENTE)  # o nome antigo também é ambiente
    with pytest.raises(ValueError):
        chaves.gravar("gemini", CHAVE_BANCO)


def test_gravar_valida_provedor_e_valor(ambiente):
    with pytest.raises(ValueError):
        chaves.gravar("inventado", "x")
    with pytest.raises(ValueError):
        chaves.gravar("openai", "sk-linha\nOUTRA=1")
    with pytest.raises(ValueError):
        chaves.gravar("openai", "x" * (chaves.TAMANHO_MAXIMO + 1))
    chaves.gravar("openai", "")  # vazio apaga (e não havia nada): não é erro
    assert chaves.ler("openai") is None


def test_ambiente_para_os_scripts(ambiente, monkeypatch):
    env_legado(ambiente, GEMINI_API_KEY=CHAVE_ARQUIVO, YOUTUBE_API_KEY="yt-do-arquivo-0006",
               OPENAI_API_KEY="sk-velha-do-arquivo-0007")
    chaves.gravar("openai", CHAVE_BANCO)  # o banco vale mais que o .env
    monkeypatch.setenv("ANTHROPIC_API_KEY", CHAVE_AMBIENTE)  # já no ambiente: o subprocesso herda

    assert chaves.ambiente() == {"OPENAI_API_KEY": CHAVE_BANCO, "GEMINI_API_KEY": CHAVE_ARQUIVO,
                                 "YOUTUBE_API_KEY": "yt-do-arquivo-0006"}
    assert chaves.ambiente(excluir=("youtube", "openai")) == {"GEMINI_API_KEY": CHAVE_ARQUIVO}


def test_sem_banco_acessivel_le_o_ambiente_e_o_env(ambiente, monkeypatch, tmp_path):
    """Um script rodando sem o servidor (sem banco) continua com o ambiente e o .env, sem criar um banco vazio."""
    env_legado(ambiente, GEMINI_API_KEY=CHAVE_ARQUIVO)
    inexistente = tmp_path / "nao-existe" / "banco.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{inexistente}")
    banco.redefinir()
    config.cache_clear()
    assert chaves.ler("gemini") == CHAVE_ARQUIVO and chaves.origem("gemini") == "arquivo"
    assert chaves.ambiente() == {"GEMINI_API_KEY": CHAVE_ARQUIVO}
    assert not inexistente.exists() and not inexistente.parent.exists()


def test_banco_fora_do_ar_nao_quebra_a_leitura(ambiente, monkeypatch, capsys):
    pytest.importorskip("psycopg")
    env_legado(ambiente, OPENAI_API_KEY=CHAVE_ARQUIVO)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://ninguem:senha@127.0.0.1:1/nada")
    banco.redefinir()
    config.cache_clear()
    assert chaves.ler("openai") == CHAVE_ARQUIVO
    aviso = capsys.readouterr().err
    assert "banco indisponível" in aviso and "senha" not in aviso


def test_mascarar():
    assert chaves.mascarar(None) is None and chaves.mascarar("") is None
    assert chaves.mascarar("abc") == "•••"
    assert chaves.mascarar("sk-1234567890") == "••••••••7890"


def test_compatibilidade_de_ferramentas_chaves():
    sys.path.insert(0, str(RAIZ / "ferramentas"))
    try:
        import chaves as antigo
    finally:
        sys.path.remove(str(RAIZ / "ferramentas"))
    assert antigo.ler is chaves.ler and antigo.gravar is chaves.gravar and antigo.PROVEDORES is chaves.PROVEDORES
    assert set(antigo.PROVEDORES) == {"anthropic", "openai", "gemini", "elevenlabs", "youtube"}
    for provedor in antigo.PROVEDORES.values():
        assert {"nome", "variavel", "usos", "painel"} <= set(provedor)
