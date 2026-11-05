"""Custos (ferramentas/custos.py): o registro de cada chamada paga no custos.jsonl do projeto e na tabela custos do
banco (com o projeto, a execução de EXECUCAO_ID e o provedor), o cálculo pelo preço de cada serviço e a regra de
nunca derrubar a produção por causa do custo. Os preços daqui são inventados (TABELA), para as contas não
dependerem de biblioteca/precos.json."""

from __future__ import annotations

import json
import sys

import pytest

from nucleo import RAIZ

if str(RAIZ / "ferramentas") not in sys.path:
    sys.path.insert(0, str(RAIZ / "ferramentas"))

import custos  # noqa: E402 - ferramentas/custos.py, como os scripts e o motor importam

QUANDO = "2026-10-23T10:00:00-03:00"
TABELA = {"modelos": {
    "gemini-imagem-teste": {"entrada": 0.5, "saida_texto": 3.0, "saida_imagem": 60.0,
                            "por_imagem": {"1K": 0.067, "2K": 0.101}},
    "gpt-image-teste": {"entrada": 5.0, "entrada_imagem": 10.0, "saida_imagem": 40.0},
    "gemini-tts-teste": [{"desde": "2000-01-01", "entrada": 0.5, "saida_audio": 9.0},
                         {"desde": "2027-01-01", "entrada": 1.0, "saida_audio": 18.0}],
    "gpt-tts-teste": {"por_minuto": 0.015},
    "eleven-teste": {"por_mil_caracteres": 0.3},
    "claude-teste": {"entrada": 4, "saida": 20, "cache_leitura": 0.2, "cache_escrita": 5, "busca": 0.01},
    "gpt-teste": {"entrada": 1, "saida": 2, "busca": 0.01},
}}
USO_IMAGEM = {"promptTokenCount": 1000, "thoughtsTokenCount": 90,
              "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1120},
                                          {"modality": "TEXT", "tokenCount": 10}]}


@pytest.fixture
def tabela(monkeypatch):
    monkeypatch.setattr(custos, "tabela", lambda: TABELA)
    return TABELA


@pytest.fixture
def projeto(ambiente):
    """Um projeto com uma execução rodando; devolve (pasta, id da execução)."""
    from nucleo.banco import repositorios as repo
    repo.criar_projeto("meu-video", "Meu vídeo", {})
    id_ = repo.criar_execucao("meu-video", "producao", {})
    pasta = ambiente / "projetos" / "meu-video"
    pasta.mkdir(parents=True)
    return pasta, id_


def item(servico: str, modelo: str, uso: dict | None = None, **extra) -> dict:
    return {"servico": servico, "modelo": modelo, "quando": QUANDO, "uso": uso or {}, **extra}


# --------------------------------------------------------------------------------------------------- cálculo

def test_custo_de_llm_com_cache_e_busca():
    uso = {"entrada": 1_000_000, "saida": 500_000, "cache_leitura": 2_000_000, "cache_escrita": 100_000, "buscas": 3}
    assert custos.custo_llm("claude-teste", uso, QUANDO, TABELA) == pytest.approx(4 + 10 + 0.4 + 0.5 + 0.03)
    # sem preço de cache na tabela, o cache sai pelo preço de entrada
    assert custos.custo_llm("gpt-teste", {"entrada": 1_000_000, "cache_leitura": 1_000_000}, QUANDO,
                            TABELA) == pytest.approx(2.0)
    assert custos.custo_llm("modelo-sem-preco", uso, QUANDO, TABELA) is None
    assert custos.custo_llm("gemini-imagem-teste", uso, QUANDO, TABELA) is None  # não é um modelo de texto
    assert custos.custo(item("llm", "claude-teste", uso), TABELA) == custos.custo(item("busca", "claude-teste", uso),
                                                                                   TABELA)


def test_custo_de_imagem_por_tokens():
    # entrada + saída de imagem + texto e raciocínio pelo preço de texto
    assert custos.custo(item("imagem", "gemini-imagem-teste", USO_IMAGEM), TABELA) == pytest.approx(
        (1000 * 0.5 + 1120 * 60 + (10 + 90) * 3) / 1_000_000)
    # OpenAI: as imagens de referência têm preço próprio na entrada
    uso = {"promptTokenCount": 600, "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 100},
                                                            {"modality": "IMAGE", "tokenCount": 500}],
           "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 4160}]}
    assert custos.custo(item("imagem", "gpt-image-teste", uso), TABELA) == pytest.approx(
        (100 * 5 + 500 * 10 + 4160 * 40) / 1_000_000)


def test_custo_de_imagem_por_imagem_quando_a_api_nao_devolve_o_uso():
    assert custos.custo(item("imagem", "gemini-imagem-teste", tamanho="2K"), TABELA) == pytest.approx(0.101)
    assert custos.custo(item("imagem", "gemini-imagem-teste"), TABELA) == pytest.approx(0.067)  # 1K é o padrão
    assert custos.custo(item("imagem", "gpt-image-teste"), TABELA) is None  # sem preço por imagem
    assert custos.custo(item("narracao", "gemini-tts-teste"), TABELA) is None  # sem uso e sem tempo: sem preço


def test_custo_de_narracao_por_tokens_minuto_e_caracteres():
    uso = {"promptTokenCount": 100, "candidatesTokensDetails": [{"modality": "AUDIO", "tokenCount": 2500}]}
    assert custos.custo(item("narracao", "gemini-tts-teste", uso), TABELA) == pytest.approx(
        (100 * 0.5 + 2500 * 9) / 1e6)
    depois = {**item("narracao", "gemini-tts-teste", uso), "quando": "2027-02-01T10:00:00-03:00"}  # o preço mudou
    assert custos.custo(depois, TABELA) == pytest.approx((100 * 1 + 2500 * 18) / 1e6)
    assert custos.custo(item("narracao", "gpt-tts-teste", segundos=90), TABELA) == pytest.approx(90 / 60 * 0.015)
    assert custos.custo(item("narracao", "eleven-teste", caracteres=2500), TABELA) == pytest.approx(0.75)
    assert custos.custo(item("narracao", "eleven-teste"), TABELA) is None  # sem os caracteres


def test_provedor_deduzido_pelo_modelo():
    casos = {"gemini-3.1-flash-image": "gemini", "imagen-4": "gemini", "gpt-image-1": "openai",
             "gpt-4o-mini-tts": "openai", "tts-1": "openai", "eleven_multilingual_v2": "elevenlabs",
             "claude-opus-5-5": "anthropic", "espeak-ng": "espeak", "modelo-novo": "desconhecido", "": "desconhecido", None: "desconhecido"}
    for modelo, provedor in casos.items():
        assert custos.provedor_do_modelo(modelo) == provedor, modelo


# --------------------------------------------------------------------------------------------------- registro

def test_registrar_grava_jsonl_e_banco(projeto, tabela, monkeypatch):
    from nucleo.banco import repositorios as repo
    pasta, execucao = projeto
    monkeypatch.setenv("EXECUCAO_ID", str(execucao))
    custos.registrar(pasta, "imagem", "gemini-imagem-teste", USO_IMAGEM, item="B01-01", tamanho="1K", vazio=None)
    custos.registrar(pasta, "narracao", "eleven_multilingual_v2", None, provedor="elevenlabs", caracteres=1000)

    linhas = [json.loads(l) for l in (pasta / "custos.jsonl").read_text(encoding="utf-8").splitlines()]
    assert linhas[0]["servico"] == "imagem" and linhas[0]["modelo"] == "gemini-imagem-teste"
    assert linhas[0]["provedor"] == "gemini"  # deduzido pelo nome do modelo
    assert linhas[0]["uso"] == USO_IMAGEM and linhas[0]["item"] == "B01-01" and linhas[0]["tamanho"] == "1K"
    assert "vazio" not in linhas[0] and linhas[0]["quando"]
    assert linhas[1]["provedor"] == "elevenlabs" and linhas[1]["uso"] == {} and linhas[1]["caracteres"] == 1000

    no_banco = repo.custos(projeto_slug="meu-video")
    assert len(no_banco) == 2
    imagem = no_banco[0]
    assert imagem["execucao_id"] == execucao and imagem["projeto_slug"] == "meu-video"
    assert imagem["servico"] == "imagem" and imagem["provedor"] == "gemini" and imagem["item"] == "B01-01"
    assert imagem["extra"] == {"tamanho": "1K"} and imagem["uso"] == USO_IMAGEM and imagem["estimado"] is False
    assert imagem["custo_usd"] == pytest.approx(custos.custo(linhas[0], TABELA))
    assert no_banco[1]["custo_usd"] is None  # o modelo real da ElevenLabs não está na TABELA de teste
    assert repo.execucao(execucao)["custo_usd"] == pytest.approx(imagem["custo_usd"])  # soma na execução
    assert repo.custos_do_projeto("meu-video")["sem_preco"] == ["eleven_multilingual_v2"]


def test_registrar_fora_de_um_projeto_e_sem_execucao(ambiente, tabela, monkeypatch):
    """Um rascunho de personagem não é projeto; EXECUCAO_ID inválido vale como nenhum."""
    from nucleo.banco import repositorios as repo
    monkeypatch.setenv("EXECUCAO_ID", "não-é-número")
    pasta = ambiente / "rascunhos" / "personagens" / "moranguete"
    pasta.mkdir(parents=True)
    custos.registrar(pasta, "imagem", "gemini-imagem-teste", None, tamanho="2K", estimado=True)
    assert (pasta / "custos.jsonl").is_file()
    [linha] = repo.custos()
    assert linha["projeto_slug"] is None and linha["execucao_id"] is None and linha["estimado"] is True
    assert linha["custo_usd"] == pytest.approx(0.101)


def test_registrar_llm_do_motor(projeto, tabela):
    from nucleo.banco import repositorios as repo
    pasta, execucao = projeto
    uso = {"entrada": 1000, "saida": 200, "cache_leitura": 0, "cache_escrita": 0, "buscas": 0}
    valor = custos.registrar_llm("anthropic", "claude-teste", uso, agente="roteirista", execucao_id=execucao)
    assert valor == pytest.approx((1000 * 4 + 200 * 20) / 1e6)
    assert not (pasta / "custos.jsonl").exists()  # sem pasta, só o banco
    busca = custos.registrar_llm("openai", "gpt-teste", {**uso, "buscas": 2}, agente="pesquisador-conteudo",
                                 servico="busca", pasta=pasta)
    assert busca == pytest.approx((1000 * 1 + 200 * 2) / 1e6 + 2 * 0.01)
    assert json.loads((pasta / "custos.jsonl").read_text(encoding="utf-8"))["agente"] == "pesquisador-conteudo"
    assert custos.registrar_llm("gemini", "sem-preco", uso) is None

    linhas = repo.custos(projeto_slug="meu-video")
    assert [(l["servico"], l["agente"], l["provedor"]) for l in linhas] == [
        ("llm", "roteirista", "anthropic"), ("busca", "pesquisador-conteudo", "openai")]
    assert linhas[0]["execucao_id"] == execucao  # o projeto veio da execução
    assert repo.execucao(execucao)["custo_usd"] == pytest.approx(valor)


def test_nunca_levanta_erro(ambiente, monkeypatch, capsys):
    from nucleo.banco import repositorios as repo

    # a pasta não existe: o jsonl não grava, mas o banco sim
    custos.registrar(ambiente / "projetos" / "nao-existe", "imagem", "gemini-imagem-teste", None)
    assert "não consegui registrar o custo" in capsys.readouterr().err
    assert len(repo.custos()) == 1

    # tabela de preços quebrada: o custo fica sem preço
    def quebrada():
        raise ValueError("json inválido")
    monkeypatch.setattr(custos, "tabela", quebrada)
    pasta = ambiente / "projetos" / "x"
    pasta.mkdir(parents=True)
    custos.registrar(pasta, "imagem", "gemini-imagem-teste", None)
    assert repo.custos(projeto_slug="x")[0]["custo_usd"] is None
    assert custos.registrar_llm("anthropic", "claude-opus-5-5", {"entrada": 1}) is None

    # o banco falha no meio: só um aviso
    def falha(*a, **k):
        raise RuntimeError("banco fora do ar")
    monkeypatch.setattr(repo, "registrar_custo", falha)
    custos.registrar(pasta, "imagem", "gemini-imagem-teste", None)
    custos.registrar_llm("anthropic", "claude-opus-5-5", {"entrada": 1}, pasta=pasta)
    assert "banco fora do ar" in capsys.readouterr().err
    assert len((pasta / "custos.jsonl").read_text(encoding="utf-8").splitlines()) == 3  # o artefato continua


def test_registrar_sem_banco_so_grava_o_jsonl(ambiente, monkeypatch, tmp_path):
    from nucleo import banco
    from nucleo.config import config
    inexistente = tmp_path / "sem" / "banco.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{inexistente}")
    banco.redefinir()
    config.cache_clear()
    pasta = ambiente / "projetos" / "x"
    pasta.mkdir(parents=True)
    custos.registrar(pasta, "narracao", "gpt-tts-teste", None, segundos=30)
    assert len(custos.ler(pasta)) == 1 and not inexistente.exists()


def test_resumo_pelo_jsonl(ambiente, tabela):
    pasta = ambiente / "projetos" / "x"
    pasta.mkdir(parents=True)
    custos.registrar(pasta, "imagem", "gemini-imagem-teste", USO_IMAGEM)
    custos.registrar(pasta, "imagem", "gemini-imagem-teste", None, tamanho="2K")
    custos.registrar_llm("anthropic", "claude-teste", {"entrada": 1000, "saida": 100}, agente="critico", pasta=pasta)
    custos.registrar(pasta, "imagem", "modelo-sem-preco", None)
    with (pasta / custos.REGISTRO).open("a", encoding="utf-8") as f:
        f.write('{"servico": "imagem", "modelo": "cortad')  # processo morto no meio da linha

    r = custos.resumo(pasta)
    grupos = {(g["servico"], g["modelo"]): g for g in r["itens"]}
    imagens = grupos[("imagem", "gemini-imagem-teste")]
    assert imagens["chamadas"] == 2 and imagens["rotulo"] == "Imagens (Gemini)"
    primeira = custos.custo(item("imagem", "gemini-imagem-teste", USO_IMAGEM), TABELA)
    assert imagens["usd"] == pytest.approx(round(primeira + 0.101, 4))
    assert grupos[("llm", "claude-teste")]["rotulo"] == "Agentes (LLM) (Claude)"
    agentes = grupos[("llm", "claude-teste")]
    assert agentes["tokens_entrada"] == 1000 and agentes["tokens_saida"] == 100
    assert r["sem_preco"] == ["modelo-sem-preco"]
    assert r["total_usd"] == pytest.approx(sum(g["usd"] for g in r["itens"]))
    assert custos.rotulo("busca", "openai") == "Busca na web (OpenAI)"
    assert custos.rotulo("imagem", "desconhecido") == "Imagens"
