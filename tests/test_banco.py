"""Banco: esquema, repositórios (projetos, execuções e fila, log, custos, configurações, chaves, arquivos,
contextos, lições, desempenho) e travas. Em SQLite (fixture `ambiente`); com PG_TESTE_URL definido, também
contra um Postgres de verdade (migração do Alembic, índice parcial, SKIP LOCKED e pg_advisory_lock).

    PG_TESTE_URL=postgresql+psycopg://postgres:teste@localhost:15432/postgres pytest tests/test_banco.py

Cuidado: o teste do Postgres apaga as tabelas do banco apontado. Use um banco descartável.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from nucleo import RAIZ


@pytest.fixture
def repo(ambiente):
    from nucleo.banco import repositorios
    return repositorios


# ------------------------------------------------------------------------------------------------ esquema

def test_iniciar_e_idempotente_e_carimba_o_alembic(ambiente):
    from sqlalchemy import inspect, text

    from nucleo import banco
    banco.iniciar()
    banco.iniciar()
    with banco.motor().connect() as con:
        tabelas = set(inspect(con).get_table_names())
        versao = con.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert {"projetos", "execucoes", "execucao_logs", "custos", "arquivos", "configuracoes", "chaves_api",
            "contextos", "licoes_roteiro", "desempenho"} <= tabelas
    assert versao == "0001_inicial"
    assert banco.disponivel()


def test_migracao_do_alembic_cria_o_mesmo_esquema_dos_modelos(tmp_path, monkeypatch):
    """A migração escrita à mão bate com nucleo/banco/modelos.py (o `alembic check` do SQLite)."""
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    from nucleo.banco.modelos import Base

    sessao = importlib.import_module("nucleo.banco.sessao")  # o módulo (nucleo.banco.sessao é a função)
    url = f"sqlite:///{tmp_path / 'migrado.sqlite3'}"
    cfg = sessao._config_alembic(url)
    command.upgrade(cfg, "head")
    motor = create_engine(url)
    with motor.connect() as con:
        diferencas = compare_metadata(MigrationContext.configure(con, opts={"compare_type": True}), Base.metadata)
    motor.dispose()
    assert diferencas == []


# ----------------------------------------------------------------------------------------------- projetos

def test_projetos(repo):
    p = repo.criar_projeto("meu-video", "Meu vídeo", {"tema": "polvos", "lista": [1, 2]})
    assert p["formulario"] == {"tema": "polvos", "lista": [1, 2]}
    assert p["etapa"] == "configurar" and p["origem"] == "pipeline" and p["sugestoes"] is None
    assert p["criado_em"].endswith("+00:00")
    with pytest.raises(ValueError):
        repo.criar_projeto("meu-video", "outro", {})
    with pytest.raises(ValueError):
        repo.criar_projeto("x", "X", {}, origem="inventada")

    time.sleep(0.01)
    repo.criar_projeto("outro", "Outro", {}, etapa="pesquisa", origem="roteiro-importado")
    assert [p["slug"] for p in repo.projetos()] == ["outro", "meu-video"]

    repo.atualizar_projeto("meu-video", sugestoes={"titulo": "T"}, etapa="roteiro")
    p = repo.projeto("meu-video")
    assert p["sugestoes"] == {"titulo": "T"} and p["etapa"] == "roteiro"
    assert repo.projetos()[0]["slug"] == "meu-video"  # mais recente primeiro
    with pytest.raises(ValueError):
        repo.atualizar_projeto("meu-video", inventado=1)

    i = repo.criar_execucao("meu-video", "roteiro", {})
    repo.anexar_log(i, "linha")
    repo.registrar_custo("llm", "anthropic", "claude-opus-5-5", {}, 0.5, execucao_id=i)
    repo.apagar_projeto("meu-video")
    assert repo.projeto("meu-video") is None
    assert repo.execucao(i) is None
    assert repo.custos(projeto_slug="meu-video") == []


# ----------------------------------------------------------------------------------------------- execuções

def test_uma_execucao_ativa_por_projeto(repo):
    repo.criar_projeto("a", "A", {})
    repo.criar_projeto("b", "B", {})
    i = repo.criar_execucao("a", "pesquisa", {"x": 1})
    with pytest.raises(repo.ErroOcupado) as erro:
        repo.criar_execucao("a", "roteiro", {})
    assert erro.value.execucao_id == i
    j = repo.criar_execucao("b", "pesquisa", {})  # outro projeto: pode
    assert repo.execucao(i)["parametros"] == {"x": 1}
    assert {e["id"] for e in repo.execucoes_ativas()} == {i, j}
    assert [e["id"] for e in repo.execucoes_ativas("a")] == [i]

    repo.terminar_execucao(i, "ok", "feito")
    k = repo.criar_execucao("a", "roteiro", {})  # a anterior terminou: pode de novo
    assert repo.ultima_execucao("a", "roteiro")["id"] == k
    assert repo.ultima_execucao("a", "pesquisa")["resultado"] == "feito"
    with pytest.raises(ValueError):
        repo.criar_execucao("nao-existe", "pesquisa", {})
    with pytest.raises(ValueError):
        repo.terminar_execucao(k, "rodando")


def test_indice_unico_parcial_no_banco(repo):
    """Mesmo sem a conferência do repositório, o banco recusa a segunda execução ativa."""
    from nucleo import banco
    from nucleo.banco.modelos import Execucao
    repo.criar_projeto("a", "A", {})
    repo.criar_execucao("a", "pesquisa", {})
    with pytest.raises(IntegrityError):
        with banco.sessao() as s:
            s.add(Execucao(projeto_slug="a", etapa="roteiro", status="rodando"))
    with banco.sessao() as s:  # terminada não conta
        s.add(Execucao(projeto_slug="a", etapa="roteiro", status="ok"))


def test_execucoes_sem_projeto_usam_a_chave_exclusiva(repo):
    i = repo.criar_execucao(None, "personagem", {"descricao": "x"}, chave_exclusiva="personagem:moranguete")
    with pytest.raises(repo.ErroOcupado):
        repo.criar_execucao(None, "personagem", {}, chave_exclusiva="personagem:moranguete")
    repo.criar_execucao(None, "personagem", {}, chave_exclusiva="personagem:outro")
    repo.criar_execucao(None, "referencia", {})  # sem chave: não há exclusividade
    repo.criar_execucao(None, "referencia", {})
    assert repo.execucao(i)["slug"] is None


def test_criar_execucao_concorrente_so_uma_passa(repo):
    repo.criar_projeto("a", "A", {})
    resultados: list = []

    def tentar():
        try:
            resultados.append(repo.criar_execucao("a", "pesquisa", {}))
        except repo.ErroOcupado:
            resultados.append("ocupado")

    threads = [threading.Thread(target=tentar) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in resultados if r != "ocupado") == 1


def test_fila_pegar_proxima(repo):
    for slug in ("a", "b", "c"):
        repo.criar_projeto(slug, slug, {})
    ids = [repo.criar_execucao(s, "pesquisa", {}) for s in ("a", "b", "c")]
    e = repo.pegar_proxima("trabalhador-1")
    assert e["id"] == ids[0] and e["status"] == "rodando" and e["trabalhador"] == "trabalhador-1"
    assert e["iniciada_em"] and e["batimento_em"]
    assert repo.pegar_proxima("t")["id"] == ids[1]
    assert repo.pegar_proxima("t")["id"] == ids[2]
    assert repo.pegar_proxima("t") is None


def test_fila_concorrente_cada_execucao_uma_vez(repo):
    slugs = [f"p{i}" for i in range(30)]
    for s in slugs:
        repo.criar_projeto(s, s, {})
        repo.criar_execucao(s, "pesquisa", {})
    pegas: list[int] = []
    trava = threading.Lock()

    def trabalhar(nome):
        while (e := repo.pegar_proxima(nome)) is not None:
            with trava:
                pegas.append(e["id"])

    threads = [threading.Thread(target=trabalhar, args=(f"t{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(pegas) == sorted(set(pegas)) and len(pegas) == 30


def test_cancelamento_e_batimento(repo):
    repo.criar_projeto("a", "A", {})
    repo.criar_projeto("b", "B", {})
    pendente = repo.criar_execucao("a", "pesquisa", {})
    assert repo.pedir_cancelamento(pendente) is True
    e = repo.execucao(pendente)
    assert e["status"] == "cancelada" and e["terminada_em"]
    assert "cancelado" in repo.log_desde(pendente, 0)[0]
    assert repo.pegar_proxima("t") is None  # cancelada não entra na fila

    rodando = repo.criar_execucao("b", "pesquisa", {})
    repo.pegar_proxima("t")
    assert repo.bater(rodando) is False
    assert repo.pedir_cancelamento(rodando) is True
    assert repo.execucao(rodando)["status"] == "rodando"  # quem cancela é o trabalhador
    assert repo.bater(rodando) is True
    repo.terminar_execucao(rodando, "cancelada", "cancelado", avisos=["a"])
    assert repo.pedir_cancelamento(rodando) is False
    assert repo.bater(rodando) is True  # já não está rodando: pare
    assert repo.pedir_cancelamento(999) is False


def test_marcar_interrompidas(repo):
    from nucleo import banco
    from nucleo.banco.modelos import Execucao
    repo.criar_projeto("a", "A", {})
    repo.criar_projeto("b", "B", {})
    velha = repo.criar_execucao("a", "pesquisa", {})
    nova = repo.criar_execucao("b", "pesquisa", {})
    repo.pegar_proxima("t")
    repo.pegar_proxima("t")
    with banco.sessao() as s:
        s.execute(update(Execucao).where(Execucao.id == velha)
                  .values(batimento_em=banco.agora() - timedelta(seconds=300)))
    assert repo.marcar_interrompidas(120) == 1
    assert repo.execucao(velha)["status"] == "interrompida"
    assert repo.execucao(nova)["status"] == "rodando"
    assert "interrompida" in repo.log_desde(velha, 0)[0]
    assert repo.criar_execucao("a", "roteiro", {})  # o projeto ficou livre


def test_campos_da_execucao(repo):
    repo.criar_projeto("a", "A", {})
    i = repo.criar_execucao("a", "roteiro", {})
    repo.atualizar_execucao(i, rota={"roteirista": {"provedor": "openai", "modelo": "gpt-6.1-sol"}})
    repo.acrescentar_aviso(i, "primeiro")
    repo.acrescentar_aviso(i, "segundo")
    repo.terminar_execucao(i, "ok", "resumo", estruturado={"a": [1]})
    e = repo.execucao(i)
    assert e["rota"]["roteirista"]["provedor"] == "openai"
    assert e["avisos"] == ["primeiro", "segundo"]
    assert e["estruturado"] == {"a": [1]} and e["resultado"] == "resumo"
    with pytest.raises(ValueError):
        repo.atualizar_execucao(i, status="ok")
    assert [x["id"] for x in repo.execucoes("a")] == [i]


def test_log_com_cursor(repo):
    repo.criar_projeto("a", "A", {})
    i = repo.criar_execucao("a", "pesquisa", {})
    assert repo.log_desde(i, 0) == ("", 0)
    repo.anexar_log(i, "▶ roteirista")
    repo.anexar_log(i, "  $ ls\n")
    texto, cursor = repo.log_desde(i, 0)
    assert texto == "▶ roteirista\n  $ ls\n"
    assert repo.log_desde(i, cursor) == ("", cursor)
    repo.anexar_log(i, "✓ fim")
    assert repo.log_desde(i, cursor)[0] == "✓ fim\n"
    assert repo.execucao(i)["ultimo_log"] == repo.log_desde(i, cursor)[1]


def test_log_limita_linhas_por_chamada(repo):
    from nucleo import banco
    from nucleo.banco.modelos import ExecucaoLog
    repo.criar_projeto("a", "A", {})
    i = repo.criar_execucao("a", "pesquisa", {})
    with banco.sessao() as s:
        s.add_all(ExecucaoLog(execucao_id=i, texto=f"{n}\n") for n in range(2500))
    texto, cursor = repo.log_desde(i, 0)
    assert texto.count("\n") == repo.LOG_POR_CHAMADA
    resto, cursor2 = repo.log_desde(i, cursor)
    assert resto.count("\n") == 500 and resto.endswith("2499\n") and cursor2 > cursor


# -------------------------------------------------------------------------------------------------- custos

def test_custos_somam_na_execucao_e_no_projeto(repo):
    repo.criar_projeto("a", "A", {})
    i = repo.criar_execucao("a", "roteiro", {})
    repo.registrar_custo("llm", "anthropic", "claude-opus-5-5", {"entrada": 10}, 0.25, execucao_id=i, agente="roteirista")
    repo.registrar_custo("llm", "openai", "gpt-6.1-sol", {"entrada": 10}, 0.5, execucao_id=i, agente="critico")
    repo.registrar_custo("llm", "openai", "modelo-sem-preco", {}, None, execucao_id=i, agente="critico")
    repo.terminar_execucao(i, "ok")
    j = repo.criar_execucao("a", "producao", {})
    repo.registrar_custo("imagem", "gemini", "gemini-3.1-flash-image", {}, 0.067, execucao_id=j, item="B01-01")
    repo.registrar_custo("narracao", "gemini", "gemini-3.8-flash-tts", {}, 0.1, projeto_slug="a")  # script avulso
    repo.registrar_custo("imagem", "gemini", "gemini-3.1-flash-image", {}, 1.0)  # sem projeto: não entra

    assert repo.execucao(i)["custo_usd"] == pytest.approx(0.75)
    assert repo.custos(execucao_id=j)[0]["projeto_slug"] == "a"  # o projeto vem da execução
    c = repo.custos_do_projeto("a")
    assert c["total_usd"] == pytest.approx(0.917)
    etapas = {e["etapa"]: e for e in c["por_etapa"]}
    assert etapas["roteiro"] == {"etapa": "roteiro", "execucoes": 1, "usd": 0.75}
    assert etapas["producao"]["usd"] == pytest.approx(0.067)
    assert etapas["avulso"]["usd"] == pytest.approx(0.1) and etapas["avulso"]["execucoes"] == 0
    assert c["sem_preco"] == ["modelo-sem-preco"]
    servicos = {(s["servico"], s["modelo"]): s for s in c["por_servico"]}
    assert servicos[("llm", "gpt-6.1-sol")]["rotulo"] == "Agentes (LLM) (OpenAI)"
    assert servicos[("imagem", "gemini-3.1-flash-image")]["chamadas"] == 1
    assert "Gemini" not in servicos[("llm", "claude-opus-5-5")]["rotulo"]


def test_custos_de_execucoes_antigas_sem_linhas(repo):
    """Execuções migradas da instalação antiga trazem só o total (custo do Claude Code)."""
    repo.criar_projeto("a", "A", {})
    i = repo.criar_execucao("a", "pesquisa", {})
    repo.atualizar_execucao(i, custo_usd=1.5)
    repo.terminar_execucao(i, "ok")
    c = repo.custos_do_projeto("a")
    assert c["total_usd"] == 1.5
    assert c["por_etapa"] == [{"etapa": "pesquisa", "execucoes": 1, "usd": 1.5}]


def test_custo_com_execucao_apagada_fica_sem_vinculo(repo):
    repo.registrar_custo("imagem", "gemini", "x", {}, 0.1, execucao_id=12345, projeto_slug="p")
    assert repo.custos(projeto_slug="p")[0]["execucao_id"] is None


# ------------------------------------------------------------------------------------ configurações e chaves

def test_configuracoes(repo):
    assert repo.ler_config("modelos") is None
    assert repo.ler_config("modelos", {"padrao": 1}) == {"padrao": 1}
    repo.gravar_config("modelos", {"etapas": {"roteiro": {"provedor": "openai"}}})
    repo.gravar_config("modelos", {"etapas": {"roteiro": {"provedor": "gemini"}}})
    assert repo.ler_config("modelos")["etapas"]["roteiro"]["provedor"] == "gemini"
    repo.gravar_config("lista", ["a", "b"])
    assert repo.ler_config("lista") == ["a", "b"]


def test_chaves_cifradas(repo):
    from nucleo import banco
    from nucleo.banco.modelos import ChaveApi
    repo.gravar_chave("openai", "sk-segredo-muito-secreto-1234")
    with banco.sessao() as s:
        guardada = s.scalar(select(ChaveApi.valor_cifrado).where(ChaveApi.provedor == "openai"))
    assert "segredo" not in guardada and "1234" not in guardada
    assert repo.ler_chave("openai") == "sk-segredo-muito-secreto-1234"
    assert repo.mascara_chave("openai") == "••••••••1234"
    repo.gravar_chave("openai", None)
    assert repo.ler_chave("openai") is None and repo.mascara_chave("openai") is None


# ------------------------------------------------------------------------------------------------ arquivos

def test_indice_de_arquivos(repo):
    a = repo.registrar_arquivo("projetos/a_b/05-roteiro-v1.md", escopo="projeto", projeto_slug="a_b", tamanho=3,
                               sha256="x" * 64, mime="text/markdown", texto="abc", modificado_em=1_700_000_000.5)
    assert a["versao"] == 1 and "texto" not in a
    assert a["modificado_em"].startswith("2023-11-14T22:13:20.5")
    assert repo.registrar_arquivo("projetos/a_b/05-roteiro-v1.md", tamanho=4, texto="abcd")["versao"] == 2
    repo.registrar_arquivo("projetos/axb/x.md", escopo="projeto", projeto_slug="axb", tamanho=1)
    repo.registrar_arquivo("projetos/a%/x.md", escopo="projeto", tamanho=1)
    assert list(repo.indice("projetos/a_b/")) == ["projetos/a_b/05-roteiro-v1.md"]  # o _ não é curinga
    assert list(repo.indice("projetos/a%/")) == ["projetos/a%/x.md"]
    assert len(repo.indice("projetos/")) == 3
    assert repo.texto_do_arquivo("projetos/a_b/05-roteiro-v1.md") == "abcd"

    assert repo.marcar_apagado("projetos/a_b/05-roteiro-v1.md") == 3
    assert repo.marcar_apagado("projetos/a_b/05-roteiro-v1.md") == 3  # de novo: nada muda
    assert repo.marcar_apagado("nao/existe") is None
    entrada = repo.indice("projetos/a_b/")["projetos/a_b/05-roteiro-v1.md"]
    assert entrada["apagado_em"] and entrada["versao"] == 3
    assert repo.texto_do_arquivo("projetos/a_b/05-roteiro-v1.md") is None
    volta = repo.registrar_arquivo("projetos/a_b/05-roteiro-v1.md", tamanho=1)
    assert volta["apagado_em"] is None and volta["versao"] == 4
    with pytest.raises(ValueError):
        repo.registrar_arquivo("x", inventado=1)


# ---------------------------------------------------------------------------------- contextos, lições, desempenho

def test_contextos(repo):
    c = repo.salvar_contexto("novela", nome="Novela das frutas", texto="Série...", anexos=[{"arquivo": "a.png"}])
    assert c["nome"] == "Novela das frutas" and c["anexos"] == [{"arquivo": "a.png"}]
    repo.salvar_contexto("novela", texto="Outro texto")
    c = repo.contexto("novela")
    assert c["texto"] == "Outro texto" and c["nome"] == "Novela das frutas"
    assert [x["id"] for x in repo.contextos()] == ["novela"]
    repo.apagar_contexto("novela")
    assert repo.contexto("novela") is None


def test_licoes_sem_repeticao(repo):
    n = repo.acrescentar_licoes([
        {"secao": "Gancho", "regra": "Abra com a cena, não com a tese.", "erro": "e", "correcao": "c",
         "projeto_slug": "a", "versao": 2},
        {"secao": "Gancho", "regra": "abra com a cena,  não com a tese."},  # a mesma regra
        {"secao": "Ritmo", "regra": "Frases curtas no clímax."},
        {"secao": "Ritmo", "regra": ""},
    ])
    assert n == 2
    assert repo.acrescentar_licoes([{"secao": "Ritmo", "regra": "Frases curtas no clímax."}]) == 0
    licoes = repo.licoes()
    assert [l["secao"] for l in licoes] == ["Gancho", "Ritmo"]
    assert licoes[0]["projeto_slug"] == "a" and licoes[0]["versao"] == 2


def test_desempenho(repo):
    d = repo.registrar_desempenho({"projeto": "a", "video_id": "abc", "janela": "7d", "ctr": 6.2, "views": 1000,
                                   "inscritos_ganhos": 10, "tecnicas": ["escada"], "tipo_gancho": "pergunta",
                                   "titulo": "T", "observacao": "obs"})
    assert d["metricas"] == {"ctr": 6.2, "views": 1000, "inscritos_ganhos": 10, "inscritos_por_mil": 10.0}
    assert d["ctr"] == 6.2 and d["retencao_30s"] is None and d["projeto"] == "a" and d["titulo"] == "T"
    assert d["observacao"] == "obs"
    repo.registrar_desempenho({"projeto": "a", "video_id": "abc", "janela": "7d", "ctr": 7.0})  # substitui
    repo.registrar_desempenho({"projeto": "a", "video_id": "abc", "janela": "28d", "ctr": 5.0})
    assert len(repo.desempenhos()) == 2
    assert repo.desempenhos("7d")[0]["ctr"] == 7.0
    with pytest.raises(ValueError):
        repo.registrar_desempenho({"video_id": "abc", "janela": "1ano"})
    with pytest.raises(ValueError):
        repo.registrar_desempenho({"janela": "7d"})


# -------------------------------------------------------------------------------------------------- travas

def test_trava_entre_threads_e_reentrante(ambiente):
    from nucleo import banco
    dentro, maximo = [0], [0]
    guarda = threading.Lock()

    def trabalho():
        with banco.trava("licoes"):
            with banco.trava("licoes"):  # reentrante na mesma thread
                with guarda:
                    dentro[0] += 1
                    maximo[0] = max(maximo[0], dentro[0])
                time.sleep(0.02)
                with guarda:
                    dentro[0] -= 1

    threads = [threading.Thread(target=trabalho) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert maximo[0] == 1
    assert list((ambiente / "dados" / "travas").glob("licoes-*.lock"))


def test_trava_entre_processos(ambiente, tmp_path):
    from nucleo import banco
    marca = tmp_path / "pegou"
    filho = subprocess.Popen(
        [sys.executable, "-c",
         "import pathlib, time, sys\n"
         "from nucleo import banco\n"
         "with banco.trava('tecnicas'):\n"
         f"    pathlib.Path({str(marca)!r}).write_text('1')\n"
         "    time.sleep(1.0)\n"],
        env={**os.environ, "PYTHONPATH": str(RAIZ)}, cwd=RAIZ)
    try:
        limite = time.time() + 15
        while not marca.exists() and time.time() < limite:
            time.sleep(0.02)
        assert marca.exists()
        inicio = time.time()
        with banco.trava("tecnicas"):
            esperou = time.time() - inicio
    finally:
        filho.wait(timeout=15)
    assert esperou > 0.5


# ------------------------------------------------------------------------------------------------ Postgres

@pytest.fixture
def postgres(ambiente, monkeypatch):
    url = os.environ.get("PG_TESTE_URL")
    if not url:
        pytest.skip("defina PG_TESTE_URL para testar contra um Postgres de verdade")
    from sqlalchemy import create_engine, text

    from nucleo import banco
    from nucleo.config import config
    motor = create_engine(url)
    with motor.begin() as con:  # banco descartável: começa do zero
        con.execute(text("DROP SCHEMA public CASCADE"))
        con.execute(text("CREATE SCHEMA public"))
    motor.dispose()
    monkeypatch.setenv("DATABASE_URL", url)
    banco.redefinir()
    config.cache_clear()
    banco.iniciar()
    yield url
    banco.redefinir()


def test_postgres_migracao_indices_fila_e_trava(postgres):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import text

    from nucleo import banco
    from nucleo.banco import repositorios as repo
    from nucleo.banco.modelos import Base, Execucao

    banco.iniciar()  # de novo: idempotente
    with banco.motor().connect() as con:
        assert con.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0001_inicial"
        diferencas = compare_metadata(MigrationContext.configure(con, opts={"compare_type": True}), Base.metadata)
        assert diferencas == []
        tipo = con.execute(text("SELECT data_type FROM information_schema.columns "
                                "WHERE table_name='execucoes' AND column_name='parametros'")).scalar()
        assert tipo == "jsonb"
        indice = con.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname='execucoes_ativa_por_projeto'")).scalar()
        assert "UNIQUE" in indice and "WHERE" in indice

    repo.criar_projeto("a", "A", {"tema": "x"})
    i = repo.criar_execucao("a", "pesquisa", {})
    with pytest.raises(repo.ErroOcupado):
        repo.criar_execucao("a", "roteiro", {})
    with pytest.raises(IntegrityError):
        with banco.sessao() as s:
            s.add(Execucao(projeto_slug="a", etapa="roteiro", status="pendente"))

    # SKIP LOCKED: com a primeira pendente travada por outra transação, a fila entrega a seguinte
    for slug in ("b", "c"):
        repo.criar_projeto(slug, slug, {})
    j = repo.criar_execucao("b", "pesquisa", {})
    with banco.motor().connect() as outra:
        outra.execute(text("SELECT id FROM execucoes WHERE id = :i FOR UPDATE"), {"i": i})
        assert repo.pegar_proxima("t1")["id"] == j
        outra.rollback()
    assert repo.pegar_proxima("t2")["id"] == i
    assert repo.pegar_proxima("t3") is None

    # fila concorrente
    for n in range(20):
        repo.criar_projeto(f"p{n}", "p", {})
        repo.criar_execucao(f"p{n}", "pesquisa", {})
    pegas: list[int] = []

    def trabalhar(nome):
        while (e := repo.pegar_proxima(nome)) is not None:
            pegas.append(e["id"])

    threads = [threading.Thread(target=trabalhar, args=(f"t{n}",)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(pegas) == 20 == len(set(pegas))

    # log, custo e datas com fuso
    repo.anexar_log(i, "olá")
    assert repo.log_desde(i, 0)[0] == "olá\n"
    repo.registrar_custo("llm", "anthropic", "claude-opus-5-5", {"entrada": 1}, 0.1, execucao_id=i)
    assert repo.execucao(i)["custo_usd"] == pytest.approx(0.1)
    assert repo.execucao(i)["criada_em"].endswith("+00:00")
    repo.registrar_arquivo("projetos/a_b/x.md", escopo="projeto", tamanho=1)
    repo.registrar_arquivo("projetos/axb/x.md", escopo="projeto", tamanho=1)
    assert list(repo.indice("projetos/a_b/")) == ["projetos/a_b/x.md"]

    # pg_advisory_lock: exclusão entre threads (conexões diferentes)
    dentro, maximo = [0], [0]
    guarda = threading.Lock()

    def trabalho():
        with banco.trava("licoes"):
            with guarda:
                dentro[0] += 1
                maximo[0] = max(maximo[0], dentro[0])
            time.sleep(0.05)
            with guarda:
                dentro[0] -= 1

    threads = [threading.Thread(target=trabalho) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert maximo[0] == 1
    with banco.motor().connect() as con:  # nenhuma trava ficou presa
        assert con.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")).scalar() == 0
