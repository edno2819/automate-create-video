"""A fila e o trabalhador (nucleo/pipeline/execucao.py) num SQLite de teste: enfileirar → pegar → rodar → terminar,
uma execução ativa por projeto, o cancelamento que chega pelo banco (batimento), o erro com os arquivos enviados
assim mesmo, o custo somado na execução e o ambiente dos scripts."""

from __future__ import annotations

import threading
import time

import pytest

from nucleo.agentes.motor import Rota
from nucleo.banco import repositorios
from nucleo.pipeline import execucao, fluxos


@pytest.fixture
def projeto(ambiente):
    repositorios.criar_projeto("gatos", "Gatos", {})
    return ambiente / "projetos" / "gatos"


def esperar(condicao, limite: float = 10.0) -> None:
    fim = time.time() + limite
    while time.time() < fim:
        if condicao():
            return
        time.sleep(0.02)
    raise AssertionError("a condição não aconteceu a tempo")


def test_enfileirar_valida_e_guarda_a_etapa_da_trilha(projeto):
    with pytest.raises(ValueError):
        execucao.enfileirar("gatos", "etapa-que-nao-existe", {})
    with pytest.raises(ValueError):
        execucao.enfileirar(None, "roteiro", {})
    id_ = execucao.enfileirar("gatos", "avaliar", {"versao": 2})
    e = repositorios.execucao(id_)
    assert e["etapa"] == "roteiro" and e["parametros"] == {"versao": 2, "fluxo": "avaliar"}
    assert repositorios.projeto("gatos")["etapa"] == "roteiro"
    with pytest.raises(repositorios.ErroOcupado):
        execucao.enfileirar("gatos", "visual", {})  # uma ativa por projeto


def test_personagem_tem_chave_exclusiva(ambiente):
    id_ = execucao.enfileirar(None, "personagem", {"descricao": "um rato de óculos", "nome": "Rato Sábio"})
    e = repositorios.execucao(id_)
    assert e["projeto_slug"] is None and e["parametros"]["slug"] == "rato-sabio"
    with pytest.raises(repositorios.ErroOcupado):
        execucao.enfileirar(None, "personagem", {"slug": "rato-sabio"})
    outro = execucao.enfileirar(None, "personagem", {"slug": "outro"})
    assert outro != id_


def test_rodar_uma_com_sucesso_envia_os_arquivos(projeto, monkeypatch):
    def fluxo(ctx, slug, parametros):
        assert ctx.fluxo == "angulos" and parametros == {"orientacao": "mais humor"}
        (ctx.tmp / "x").write_text("temporário")
        (projeto / "03-angulos.md").parent.mkdir(parents=True, exist_ok=True)
        (projeto / "03-angulos.md").write_text("# ângulos", encoding="utf-8")
        ctx.usar("estrategista", Rota("anthropic", "claude-opus-5-5"))
        ctx.somar("anthropic", "claude-opus-5-5", {"entrada": 1000, "saida": 1000}, "estrategista")
        ctx.avisar("um aviso")
        ctx.log("› pronto")
        return "três ângulos"
    monkeypatch.setitem(fluxos.FLUXOS, "angulos", fluxo)
    id_ = execucao.enfileirar("gatos", "angulos", {"orientacao": "mais humor"})
    t = execucao.Trabalhador(1, "teste")
    assert t.processar_fila() == 1
    e = repositorios.execucao(id_)
    assert e["status"] == "ok" and e["resultado"] == "três ângulos" and e["avisos"] == ["um aviso"]
    assert e["rota"] == {"estrategista": {"provedor": "anthropic", "modelo": "claude-opus-5-5"}}
    assert e["custo_usd"] > 0
    assert repositorios.custos(execucao_id=id_)[0]["agente"] == "estrategista"
    log, _ = repositorios.log_desde(id_, 0)
    assert "iniciado: ângulos" in log and "› pronto" in log and "✓ concluído" in log
    assert "projetos/gatos/03-angulos.md" in repositorios.indice("projetos/gatos/")


def test_erro_vira_status_e_envia_mesmo_assim(projeto, monkeypatch):
    def fluxo(ctx, slug, parametros):
        (projeto / "02-dossie.md").parent.mkdir(parents=True, exist_ok=True)
        (projeto / "02-dossie.md").write_text("parcial", encoding="utf-8")
        raise RuntimeError("deu ruim")
    monkeypatch.setitem(fluxos.FLUXOS, "pesquisa", fluxo)
    id_ = execucao.enfileirar("gatos", "pesquisa", {})
    execucao.Trabalhador(1, "teste").processar_fila()
    e = repositorios.execucao(id_)
    assert e["status"] == "erro" and "deu ruim" in e["resultado"]
    assert "projetos/gatos/02-dossie.md" in repositorios.indice("projetos/gatos/")
    assert "✗ terminou com erro" in repositorios.log_desde(id_, 0)[0]


def test_cancelamento_pelo_banco(projeto, monkeypatch):
    comecou = threading.Event()

    def fluxo(ctx, slug, parametros):
        comecou.set()
        while not ctx.cancelado.wait(0.01):
            pass
        ctx.conferir_cancelamento()
    monkeypatch.setitem(fluxos.FLUXOS, "visual", fluxo)
    id_ = execucao.enfileirar("gatos", "visual", {})
    t = execucao.Trabalhador(1, "teste", batimento_s=0.05)
    execucao_ = repositorios.pegar_proxima("teste")
    rodando = threading.Thread(target=t.rodar_uma, args=(execucao_,))
    rodando.start()
    assert comecou.wait(5)
    assert repositorios.pedir_cancelamento(id_)  # como outro processo faria: só marca no banco
    rodando.join(10)
    assert repositorios.execucao(id_)["status"] == "cancelada"
    assert "■ cancelado pelo usuário" in repositorios.log_desde(id_, 0)[0]


def test_trabalhador_em_laco_e_parar(projeto, monkeypatch):
    monkeypatch.setitem(fluxos.FLUXOS, "visual", lambda ctx, slug, p: "direção visual pronta")
    t = execucao.Trabalhador(2, "laco", espera_s=0.05)
    laco = threading.Thread(target=t.rodar_para_sempre)
    laco.start()
    try:
        id_ = execucao.enfileirar("gatos", "visual", {})
        esperar(lambda: repositorios.execucao(id_)["status"] == "ok")
    finally:
        t.parar()
        laco.join(10)
    assert not laco.is_alive()


def test_parar_interrompe_a_execucao_em_andamento(projeto, monkeypatch):
    comecou = threading.Event()

    def fluxo(ctx, slug, parametros):
        comecou.set()
        ctx.cancelado.wait(10)
        ctx.conferir_cancelamento()
    monkeypatch.setitem(fluxos.FLUXOS, "producao", fluxo)
    t = execucao.Trabalhador(1, "desligando", espera_s=0.05)
    laco = threading.Thread(target=t.rodar_para_sempre)
    laco.start()
    id_ = execucao.enfileirar("gatos", "producao", {"teste": True})
    assert comecou.wait(5)
    t.parar()
    laco.join(10)
    assert repositorios.execucao(id_)["status"] == "interrompida"


def test_ambiente_dos_scripts(projeto):
    id_ = execucao.enfileirar("gatos", "roteiro", {})
    ctx = execucao.Contexto(repositorios.execucao(id_))
    env = fluxos.ambiente(ctx)
    assert env["EXECUCAO_ID"] == str(id_) and env["APP_RAIZ"] == str(projeto.parent.parent)
    from nucleo import RAIZ
    assert env["PYTHONPATH"].split(":")[0] == str(RAIZ)


def test_rodar_script_com_o_python_do_servidor(projeto):
    id_ = execucao.enfileirar("gatos", "roteiro", {})
    ctx = execucao.Contexto(repositorios.execucao(id_))
    codigo, saida = fluxos.rodar_script(ctx, "ferramentas/medir_roteiro.py", "--orcamento", "projetos/nao-existe",
                                        sincronizar=False)
    assert codigo == 2 and "erro" in saida
    assert "$ python3 ferramentas/medir_roteiro.py --orcamento" in repositorios.log_desde(id_, 0)[0]


def test_resumo_do_desempenho_le_o_banco(ambiente):
    """ferramentas/desempenho.py resumo (o que a web roda depois de cada registro) lê a tabela desempenho."""
    import subprocess
    import sys

    from nucleo import RAIZ
    from nucleo.pipeline import etapas
    repositorios.registrar_desempenho({"projeto": "gatos", "video_id": "abc123xyz00", "janela": "7d", "formato": "short",
                                       "tecnicas": ["loop-aberto"], "tipo_gancho": "pergunta-direta",
                                       "retencao_media": 80, "views": 1000})
    r = subprocess.run([sys.executable, str(RAIZ / "ferramentas" / "desempenho.py"), "resumo"], cwd=ambiente,
                       env=etapas.ambiente_dos_scripts(), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    texto = (ambiente / "biblioteca" / "aprendizados.md").read_text(encoding="utf-8")
    assert "Base: 1 registro(s)" in texto and "loop-aberto" in texto
    assert "biblioteca/aprendizados.md" in repositorios.indice("biblioteca/aprendizados.md")
