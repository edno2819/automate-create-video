"""Testes da API HTTP (web/servidor.py e web/api/) com o TestClient do FastAPI e a fixture `ambiente` (banco SQLite,
armazenamento local e espaço de trabalho numa pasta temporária). Sem rede e sem chaves reais: o teste de chave usa
um provedor falso, e as chaves que as etapas exigem vêm de variáveis de ambiente falsas.

O que é do trabalhador (rodar os fluxos) não roda aqui: os testes conferem que a web enfileira, acompanha pelo log
no banco e cancela.
"""

from __future__ import annotations

import base64
import json

import pytest

pytest.importorskip("nucleo.pipeline.execucao")
pytest.importorskip("nucleo.pipeline.fluxos")
pytest.importorskip("nucleo.roteiro")

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
ROTEIRO = """---
projeto: {slug}
versao: 1
formato: short
duracao_alvo: "00:30"
ppm: 150
titulo: "Teste"
tecnicas: [tese-contraria, final-em-loop]
tipo_gancho: tese-contraria
gatilhos: [curiosidade, surpresa]
---

## [B01] Gancho
> função: gancho · abre: L1 · fecha: — · conector: — · cortável: não

[VISUAL: um navio pequeno no mar]

Esquece o cinema: o navio era pequeno, e o capitão podia ser demitido no voto.

## [B02] Fim
> função: payoff · abre: — · fecha: L1 · conector: então · cortável: não

[FALA: Moranguete, rindo] E quem decide sou eu!
"""


@pytest.fixture
def cliente(ambiente):
    from fastapi.testclient import TestClient

    from web import servidor
    with TestClient(servidor.app) as c:
        yield c


@pytest.fixture
def com_chaves(monkeypatch):
    """Chaves falsas no ambiente: as etapas conferem só que existem (nada chama a API nestes testes)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-falsa-para-teste")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-falsa-para-teste")


def gravar(caminho: str, conteudo: str | bytes):
    from nucleo import espaco
    if isinstance(conteudo, bytes):
        return espaco.gravar_bytes(caminho, conteudo)
    return espaco.gravar_texto(caminho, conteudo)


def criar_projeto(cliente, titulo="Como era cada cargo de um navio pirata", **extra) -> str:
    r = cliente.post("/api/projetos", json={"titulo": titulo, **extra})
    assert r.status_code == 200, r.text
    return r.json()["slug"]


def com_brief(slug: str, **brief):
    gravar(f"projetos/{slug}/00-brief.json", json.dumps({"titulo": "Teste", "formato": "short", **brief}))


# ---------------------------------------------------------------- saúde e autenticação


def test_saude(cliente):
    r = cliente.get("/api/saude")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["banco"]["ok"] and d["armazenamento"]["ok"]


def test_autenticacao_basica(cliente, monkeypatch):
    from nucleo.config import config
    monkeypatch.setenv("APP_USUARIO", "edno")
    monkeypatch.setenv("APP_SENHA", "s3nha-forte")
    config.cache_clear()
    try:
        r = cliente.get("/api/projetos")
        assert r.status_code == 401
        assert r.headers["www-authenticate"].lower().startswith("basic")
        assert cliente.get("/").status_code == 401
        assert cliente.get("/estatico/app.js").status_code == 401
        assert cliente.get("/api/projetos", auth=("edno", "errada")).status_code == 401
        assert cliente.get("/api/projetos", auth=("outro", "s3nha-forte")).status_code == 401
        assert cliente.get("/api/projetos", headers={"Authorization": "Basic nao-e-base64!"}).status_code == 401
        assert cliente.get("/api/projetos", auth=("edno", "s3nha-forte")).status_code == 200
        assert cliente.get("/api/saude").status_code == 200  # o healthcheck não pede senha
    finally:
        monkeypatch.delenv("APP_USUARIO")
        monkeypatch.delenv("APP_SENHA")
        config.cache_clear()
    assert cliente.get("/api/projetos").status_code == 200


def test_pagina_e_estaticos(cliente):
    r = cliente.get("/")
    assert r.status_code == 200 and "app.js" in r.text
    assert cliente.get("/estatico/app.js").headers["cache-control"] == "no-cache"


# ---------------------------------------------------------------- projetos


def test_criar_projeto_e_estado(cliente):
    slug = criar_projeto(cliente)
    assert slug == "como-era-cada-cargo-navio-pirata"
    assert criar_projeto(cliente) == f"{slug}-2"  # o mesmo título não pisa no projeto anterior

    lista = cliente.get("/api/projetos").json()
    item = next(p for p in lista if p["slug"] == slug)
    assert item["origem"] == "pipeline" and item["no_banco"] and item["capa"] is None

    e = cliente.get(f"/api/projetos/{slug}").json()
    assert e["projeto"]["slug"] == slug and e["projeto"]["formulario"]["valores"]["titulo"].startswith("Como era")
    assert e["projeto"]["formulario"]["valores"]["producao.ate"] == "video"  # até onde ir começa no vídeo completo
    ids = [x["id"] for x in e["etapas"]]
    assert ids[:3] == ["configurar", "sugestoes", "pesquisa"] and "importar" not in ids and "cortes" not in ids
    assert all({"aplicavel", "pronta", "desatualizada", "execucao", "nome", "descricao"} <= set(x) for x in e["etapas"])
    assert e["rodando"] is None and e["brief"] is None and e["roteiro_importado"] is None

    assert cliente.get("/api/projetos/nao-existe").status_code == 404
    assert cliente.get("/api/projetos/..").status_code == 404
    assert cliente.get("/api/projetos/Maiusculas").status_code == 404
    assert cliente.post("/api/projetos", json={"titulo": "  "}).status_code == 400


def test_projeto_com_roteiro_importado(cliente):
    from nucleo.banco import repositorios
    slug = criar_projeto(cliente, "Meu roteiro", roteiro="Era uma vez um navio.\r\nE um capitão.")
    p = repositorios.projeto(slug)
    assert p["origem"] == "roteiro-importado"
    assert repositorios.arquivo(f"projetos/{slug}/00-roteiro-original.md")["versao"] == 1  # já está no armazenamento

    e = cliente.get(f"/api/projetos/{slug}").json()
    ids = [x["id"] for x in e["etapas"]]
    assert ids.index("importar") == ids.index("sugestoes") + 1
    aplicavel = {x["id"]: x["aplicavel"] for x in e["etapas"]}
    assert aplicavel["importar"] and not aplicavel["pesquisa"] and not aplicavel["angulos"] and not aplicavel["roteiro"]
    assert e["roteiro_importado"] == {"palavras": 8}  # a tela calcula a duração com a velocidade escolhida

    r = cliente.get(f"/api/projetos/{slug}/arquivos/00-roteiro-original.md")
    assert r.status_code == 200 and r.text == "Era uma vez um navio.\nE um capitão.\n"
    assert cliente.get(f"/api/projetos/{slug}/arquivos/06-roteiro-final.md").status_code == 404
    lista = cliente.get("/api/projetos").json()
    assert next(x for x in lista if x["slug"] == slug)["origem"] == "roteiro-importado"


def test_formulario_e_etapa(cliente):
    from nucleo.banco import repositorios
    slug = criar_projeto(cliente)
    r = cliente.put(f"/api/projetos/{slug}/formulario",
                    json={"valores": {"titulo": "Outro título", "descricao": "piratas"}, "personagens": [], "modo_voz": ""})
    assert r.status_code == 200
    p = repositorios.projeto(slug)
    assert p["titulo"] == "Outro título" and p["formulario"]["valores"]["slug"] == slug
    assert cliente.put(f"/api/projetos/{slug}/etapa", json={"etapa": "roteiro"}).status_code == 200
    assert repositorios.projeto(slug)["etapa"] == "roteiro"
    assert cliente.put(f"/api/projetos/{slug}/etapa", json={"etapa": "inventada"}).status_code == 400
    assert cliente.put(f"/api/projetos/{slug}/formulario", json={"valores": []}).status_code == 400


def test_confirmar_pedido(cliente):
    from nucleo import RAIZ
    from nucleo.banco import repositorios
    if "skills/produzir-video/scripts" in (RAIZ / "ferramentas" / "pedido.py").read_text(encoding="utf-8").replace('" / "', "/"):
        pytest.skip("ferramentas/pedido.py ainda carrega o narrar.py antigo (escopo D)")
    slug = criar_projeto(cliente, "Higiene na Idade Média")
    corpo = {
        "valores": {"titulo": "Higiene na Idade Média", "descricao": "como as pessoas se lavavam", "tema": "higiene medieval",
                    "formato": "short", "tom": "irônico", "natureza": "factual", "publico": "curiosos de história",
                    "nicho": "história", "estilo_artistico": "pintura digital", "visual.imagens_da_internet": False,
                    "referencias": [], "canais_referencia": []},
        "origens": {"titulo": "usuario", "descricao": "usuario", "tema": "usuario", "formato": "sugestao", "tom": "sugestao",
                    "natureza": "sugestao", "publico": "sugestao", "nicho": "sugestao", "estilo_artistico": "sugestao",
                    "visual.imagens_da_internet": "sugestao", "referencias": "sugestao", "canais_referencia": "sugestao"},
        "personagens": [], "origem_personagens": "sugestao", "modo_voz": "narrador", "origem_modo": "sugestao",
    }
    r = cliente.post(f"/api/projetos/{slug}/confirmar", json=corpo)
    assert r.status_code == 200, r.text
    assert "completar" in r.json() and "brief" in r.json()
    for nome in ("00-pedido.yaml", "00-pedido-original.yaml", "00-brief.json"):
        assert repositorios.arquivo(f"projetos/{slug}/{nome}"), nome  # subiram para o armazenamento
    assert repositorios.arquivo(f"pedidos/web/{slug}.yaml")
    assert repositorios.projeto(slug)["etapa"] == "pesquisa"
    brief = json.loads(repositorios.texto_do_arquivo(f"projetos/{slug}/00-brief.json"))
    assert brief["formato"] == "short"
    # a lista vazia sugerida tira as referências do canal: não volta o padrão (o vídeo do Dor Pintada)
    assert brief["referencias"] == [] and brief["canais_referencia"] == []

    corpo["valores"]["formato"] = "comprido"  # valor fora das opções: o pedido.py recusa e nada muda
    assert cliente.post(f"/api/projetos/{slug}/confirmar", json=corpo).status_code == 400


# ---------------------------------------------------------------- configurações e chaves


def test_configuracoes(cliente):
    d = cliente.get("/api/configuracoes").json()
    assert {p["id"] for p in d["provedores"]} == {"anthropic", "openai", "gemini", "elevenlabs", "youtube"}
    assert all(p["origem"] is None and p["mascara"] is None for p in d["provedores"])
    assert set(d["modelos"]) >= {"padroes", "etapas", "agentes", "busca", "imagem", "narracao", "alinhamento"}
    etapas = {e["id"]: e for e in d["etapas"]}
    assert set(etapas) >= {"sugestoes", "pesquisa", "roteiro", "producao", "importar", "personagem", "referencia"}
    critico = next(a for a in etapas["roteiro"]["agentes"] if a["nome"] == "critico")
    assert critico["nivel"] == "julgamento" and critico["descricao"]
    assert etapas["roteiro"]["problemas"]  # sem nenhuma chave de LLM
    assert d["niveis"] == ["julgamento", "coleta"] and d["llm"] == ["anthropic", "openai", "gemini"]
    assert {p["id"] for p in d["imagem"]["provedores"]} == {"gemini", "openai"}
    assert {f["id"] for f in d["narracao"]["fornecedores"]} >= {"gemini", "espeak"}
    assert "small" in d["whisper"] and d["precos"]
    assert d["sistema"]["armazenamento"] == "local" and d["sistema"]["banco"].startswith("SQLite")


def test_gravar_modelos(cliente):
    r = cliente.put("/api/configuracoes/modelos", json={"etapas": {"roteiro": {"provedor": "openai", "modelo": ""}},
                                                        "agentes": {"critico": {"provedor": "gemini", "modelo": "gemini-x"}}})
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg["padroes"]["anthropic"]["julgamento"] and cfg["etapas"]["roteiro"]["provedor"] == "openai"
    assert cfg["agentes"]["critico"] == {"provedor": "gemini", "modelo": "gemini-x"}
    cfg = cliente.put("/api/configuracoes/modelos", json={"agentes": {"critico": None}}).json()  # null: volta a herdar
    assert cfg["agentes"]["critico"]["provedor"] == "" and cfg["etapas"]["roteiro"]["provedor"] == "openai"
    assert cliente.get("/api/configuracoes").json()["modelos"]["etapas"]["roteiro"]["provedor"] == "openai"
    assert cliente.put("/api/configuracoes/modelos", json={"etapas": {"roteiro": {"provedor": "inventado"}}}).status_code == 400
    assert cliente.put("/api/configuracoes/modelos", json={"secao": {}}).status_code == 400
    assert cliente.put("/api/configuracoes/modelos", json={"etapas": {"cortes": {"provedor": "openai"}}}).status_code == 400


def test_modelos_gravados_com_etapa_que_saiu(cliente):
    from nucleo import rotas
    from nucleo.banco import repositorios
    # gravado antes de os cortes saírem da trilha: não aparece e não impede gravar o resto
    repositorios.gravar_config(rotas.CHAVE_CONFIG, {"etapas": {"cortes": {"provedor": "openai", "modelo": ""}}})
    assert "cortes" not in cliente.get("/api/configuracoes").json()["modelos"]["etapas"]
    r = cliente.put("/api/configuracoes/modelos", json={"etapas": {"roteiro": {"provedor": "openai", "modelo": ""}}})
    assert r.status_code == 200, r.text
    assert "cortes" not in repositorios.ler_config(rotas.CHAVE_CONFIG, {})["etapas"]


def test_chaves(cliente, monkeypatch):
    from nucleo.provedores.erros import ErroProvedor
    from nucleo.provedores.llm.anthropic import ProvedorAnthropic

    chave = "sk-ant-api03-teste-1234567890abcd"
    monkeypatch.setattr(ProvedorAnthropic, "listar_modelos", lambda self: ["claude-opus-5-5", "claude-sonnet-5-5"])
    r = cliente.put("/api/configuracoes/chaves/anthropic", json={"valor": chave})
    assert r.status_code == 200, r.text
    assert r.json()["teste"]["ok"] is True and r.json()["mascara"].endswith("abcd")
    assert chave not in r.text

    d = cliente.get("/api/configuracoes")
    assert chave not in d.text
    anthropic = next(p for p in d.json()["provedores"] if p["id"] == "anthropic")
    assert anthropic["origem"] == "banco" and anthropic["mascara"].endswith("abcd")
    assert anthropic["modelos"] == ["claude-opus-5-5", "claude-sonnet-5-5"] and anthropic["teste"]["ok"]
    roteiro = next(e for e in d.json()["etapas"] if e["id"] == "roteiro")
    assert roteiro["problemas"] == []  # agora há chave de LLM

    def falha(self):
        raise ErroProvedor(f"HTTP 401: invalid x-api-key {chave}", "anthropic", 401)
    monkeypatch.setattr(ProvedorAnthropic, "listar_modelos", falha)
    t = cliente.post("/api/configuracoes/chaves/anthropic/testar")
    assert t.status_code == 200 and t.json()["ok"] is False and chave not in t.text and "401" in t.json()["mensagem"]

    assert cliente.delete("/api/configuracoes/chaves/anthropic").status_code == 200
    anthropic = next(p for p in cliente.get("/api/configuracoes").json()["provedores"] if p["id"] == "anthropic")
    assert anthropic["origem"] is None and anthropic["teste"] is None

    monkeypatch.setenv("OPENAI_API_KEY", "sk-do-ambiente")
    assert cliente.put("/api/configuracoes/chaves/openai", json={"valor": "sk-outra"}).status_code == 400
    assert cliente.delete("/api/configuracoes/chaves/openai").status_code == 400
    assert cliente.put("/api/configuracoes/chaves/inventado", json={"valor": "x"}).status_code == 404
    assert cliente.put("/api/configuracoes/chaves/gemini", json={"valor": ""}).status_code == 400


# ---------------------------------------------------------------- execuções


def test_executar_enfileira_acompanha_e_cancela(cliente, com_chaves):
    from nucleo.banco import repositorios
    slug = criar_projeto(cliente)
    com_brief(slug)
    gravar(f"projetos/{slug}/02-dossie.md", "# Dossiê\n\nfatos")

    r = cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "angulos", "orientacao": "mais humor"})
    assert r.status_code == 200, r.text
    id_ = r.json()["id"]
    e = repositorios.execucao(id_)
    assert e["status"] == "pendente" and e["parametros"]["orientacao"] == "mais humor"
    assert repositorios.projeto(slug)["etapa"] == "angulos"

    estado = cliente.get(f"/api/projetos/{slug}").json()
    assert estado["rodando"]["id"] == id_ and estado["rodando"]["status"] == "pendente"
    assert next(x for x in estado["etapas"] if x["id"] == "angulos")["execucao"]["id"] == id_

    # uma execução ativa por projeto, e nada que escreva no projeto enquanto ela existe
    assert cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "angulos"}).status_code == 409
    assert cliente.post(f"/api/projetos/{slug}/confirmar", json={}).status_code == 409
    assert cliente.post(f"/api/projetos/{slug}/fork", json={"ate": "sugestoes"}).status_code == 409

    # o log chega pelo cursor (o id da última linha lida)
    v = cliente.get(f"/api/execucoes/{id_}?desde=0").json()
    assert v["log"] == "" and v["tamanho"] == 0 and v["status"] == "pendente"
    repositorios.anexar_log(id_, "▶ agente estrategista")
    repositorios.anexar_log(id_, "✎ projetos/x/03-angulos.json")
    v = cliente.get(f"/api/execucoes/{id_}?desde=0").json()
    assert v["log"] == "▶ agente estrategista\n✎ projetos/x/03-angulos.json\n" and v["tamanho"] > 0
    cursor = v["tamanho"]
    assert cliente.get(f"/api/execucoes/{id_}?desde={cursor}").json()["log"] == ""
    repositorios.anexar_log(id_, "› pensando")
    novo = cliente.get(f"/api/execucoes/{id_}?desde={cursor}").json()
    assert novo["log"] == "› pensando\n" and novo["tamanho"] > cursor
    assert cliente.get(f"/api/execucoes/{id_}?desde=9007199254740991").json()["log"] == ""

    c = cliente.post(f"/api/execucoes/{id_}/cancelar")
    assert c.status_code == 200 and c.json()["cancelada"] is True
    assert repositorios.execucao(id_)["status"] == "cancelada"
    assert cliente.post(f"/api/execucoes/{id_}/cancelar").json()["cancelada"] is False
    assert cliente.get("/api/execucoes/999999").status_code == 404

    # sem execução ativa, dá para enfileirar de novo
    assert cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "angulos"}).status_code == 200


def test_executar_cancelar_rodando(cliente, com_chaves):
    from nucleo.banco import repositorios
    slug = criar_projeto(cliente)
    com_brief(slug)
    id_ = cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "pesquisa"}).json()["id"]
    assert repositorios.pegar_proxima("teste")["id"] == id_  # um trabalhador pegou
    assert cliente.post(f"/api/execucoes/{id_}/cancelar").json()["cancelada"] is True
    assert repositorios.execucao(id_)["cancelar_solicitado"] is True
    assert repositorios.bater(id_) is True  # o trabalhador para no próximo batimento


def test_executar_condicoes(cliente, com_chaves, monkeypatch):
    slug = criar_projeto(cliente)
    r = cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "pesquisa"})
    assert r.status_code == 400 and "aprove o pedido" in r.json()["detail"]
    assert cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "roteiro"}).status_code == 400
    assert cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "inventada"}).status_code == 400
    assert cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "avaliar", "versao": 3}).status_code == 400
    r = cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "sugestoes"})
    assert r.status_code == 400 and "descrição" in r.json()["detail"]
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("GEMINI_API_KEY")
    com_brief(slug)
    r = cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "pesquisa"})
    assert r.status_code == 400 and "chave" in r.json()["detail"]


def test_executar_sugestoes_grava_o_pedido(cliente, com_chaves):
    from nucleo.banco import repositorios
    slug = criar_projeto(cliente)
    cliente.put(f"/api/projetos/{slug}/formulario",
                json={"valores": {"titulo": "Piratas", "descricao": "a vida num navio", "formato": "short"},
                      "personagens": [], "modo_voz": "narrador"})
    r = cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "sugestoes"})
    assert r.status_code == 200, r.text
    p = repositorios.execucao(r.json()["id"])["parametros"]
    assert "tom" in p["vazios"] and "formato" not in p["vazios"] and "titulo" not in p["vazios"]
    assert "producao.ate" not in p["vazios"]  # até onde ir é do usuário: o orquestrador não sugere, nem vazio
    assert p["modo_voz"] == "narrador"
    texto = repositorios.texto_do_arquivo(f"pedidos/web/{slug}.yaml")
    assert "a vida num navio" in texto


def test_duracao_do_roteiro_importado_vem_do_roteiro(cliente, com_chaves):
    from nucleo import RAIZ
    from nucleo.banco import repositorios
    if "skills/produzir-video/scripts" in (RAIZ / "ferramentas" / "pedido.py").read_text(encoding="utf-8").replace('" / "', "/"):
        pytest.skip("ferramentas/pedido.py ainda carrega o narrar.py antigo (escopo D)")
    # 170 palavras narradas: a 170 palavras por minuto, um minuto (o cabeçalho não é narrado)
    slug = criar_projeto(cliente, "Peixe raro", roteiro="# O peixe\n\n" + " ".join(["peixe"] * 170))
    valores = {"titulo": "Peixe raro", "descricao": "o peixe mais raro", "duracao_alvo": "01:00",
               "narracao.palavras_por_minuto": 150}
    cliente.put(f"/api/projetos/{slug}/formulario", json={"valores": valores, "personagens": [], "modo_voz": "narrador"})
    r = cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "sugestoes"})
    assert r.status_code == 200, r.text
    assert "duracao_alvo" not in repositorios.execucao(r.json()["id"])["parametros"]["vazios"]  # o orquestrador não sugere
    assert "01:00" not in repositorios.texto_do_arquivo(f"pedidos/web/{slug}.yaml")  # nem vale a duração escrita
    repositorios.terminar_execucao(r.json()["id"], "cancelada")  # sem trabalhador nos testes: a sugestão sai da fila

    corpo = {
        "valores": {**valores, "narracao.palavras_por_minuto": 170, "formato": "short", "tom": "curioso",
                    "natureza": "factual", "publico": "curiosos", "nicho": "natureza", "estilo_artistico": "colagem",
                    "visual.imagens_da_internet": False},
        "origens": {"titulo": "usuario", "descricao": "usuario", "duracao_alvo": "usuario",
                    "narracao.palavras_por_minuto": "usuario", "formato": "sugestao", "tom": "sugestao",
                    "natureza": "sugestao", "publico": "sugestao", "nicho": "sugestao", "estilo_artistico": "sugestao",
                    "visual.imagens_da_internet": "sugestao"},
        "personagens": [], "origem_personagens": "sugestao", "modo_voz": "narrador", "origem_modo": "usuario",
    }
    r = cliente.post(f"/api/projetos/{slug}/confirmar", json=corpo)
    assert r.status_code == 200, r.text
    assert "170 palavras a 170 palavras por minuto = 01:00" in r.json()["completar"]
    brief = json.loads(repositorios.texto_do_arquivo(f"projetos/{slug}/00-brief.json"))
    assert brief["duracao_alvo"] == "01:00" and brief["origem"] == "roteiro-importado"


def test_confirmacoes_antes_de_gastar(cliente, com_chaves):
    slug = criar_projeto(cliente)
    com_brief(slug, visual={"qualidade": "padrao"})
    gravar(f"projetos/{slug}/06-roteiro-final.md", ROTEIRO.format(slug=slug).replace(
        "Esquece o cinema", "[VERIFICAR: o voto do capitão] Esquece o cinema"))
    r = cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "visual"})
    assert r.status_code == 409
    assert r.json()["detail"]["precisa"] == "confirmar_verificar" and "VERIFICAR" in r.json()["detail"]["detail"]
    r = cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "visual", "confirmar_verificar": True})
    assert r.status_code == 200, r.text
    cliente.post(f"/api/execucoes/{r.json()['id']}/cancelar")

    cenas = [{"id": f"B01-{i:02d}", "bloco": "B01", "descricao": f"cena {i}", "prompt_final": f"a ship number {i}",
              "negativo_final": "text", "referencias": [], "fonte": "ia"} for i in range(1, 33)]
    gravar(f"projetos/{slug}/08-cenas.json", json.dumps({"proporcao": "16:9", "cenas": cenas}))
    r = cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "producao", "confirmar_verificar": True})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["precisa"] == "confirmar_imagens" and r.json()["detail"]["imagens"] == 32
    r = cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "producao", "teste": True, "confirmar_verificar": True})
    assert r.status_code == 200, r.text  # o modo teste não gasta com imagens
    cliente.post(f"/api/execucoes/{r.json()['id']}/cancelar")
    r = cliente.post(f"/api/projetos/{slug}/executar",
                     json={"etapa": "producao", "confirmar_verificar": True, "confirmar_imagens": True})
    assert r.status_code == 200, r.text
    cliente.post(f"/api/execucoes/{r.json()['id']}/cancelar")

    r = cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "refazer-cenas", "cenas": ["B01-02"], "ajuste": "menos gente"})
    assert r.status_code == 200, r.text
    from nucleo.banco import repositorios
    assert repositorios.execucao(r.json()["id"])["parametros"]["cenas"] == ["B01-02"]
    cliente.post(f"/api/execucoes/{r.json()['id']}/cancelar")
    assert cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "refazer-cenas", "cenas": ["X9"]}).status_code == 400


# ---------------------------------------------------------------- arquivos


def test_arquivos_e_caminhos_maliciosos(cliente, ambiente):
    slug = criar_projeto(cliente)
    (ambiente / ".env").write_text("SEGREDO=nao-pode-sair\n")
    gravar(f"projetos/{slug}/10-video/{slug}.mp4", bytes(range(256)) * 4)

    r = cliente.get(f"/api/projetos/{slug}/arquivos/10-video/{slug}.mp4", headers={"Range": "bytes=0-3"})
    assert r.status_code == 206 and r.content == bytes([0, 1, 2, 3])

    for url in (f"/api/projetos/{slug}/arquivos/%2E%2E/%2E%2E/.env", f"/api/projetos/{slug}/arquivos/..%2F..%2F.env",
                f"/api/projetos/{slug}/arquivos/10-video/%2E%2E/%2E%2E/%2E%2E/.env", "/api/projetos/%2E%2E/arquivos/.env",
                "/api/arquivos/biblioteca/%2E%2E/.env", "/api/arquivos/rascunhos/..%2F.env", "/api/arquivos/.env",
                "/api/arquivos/dados/teste.sqlite3", "/api/arquivos/projetos/x/y.txt", "/api/biblioteca/vozes/..%2F.env",
                "/api/contextos/x/arquivos/..%2F..%2F.env"):
        r = cliente.get(url)
        assert r.status_code in (400, 404), url
        assert "nao-pode-sair" not in r.text, url

    gravar("biblioteca/esqueletos/x.json", '{"a": 1}')
    assert cliente.get("/api/arquivos/biblioteca/esqueletos/x.json").json() == {"a": 1}


def test_anexos_e_musica(cliente):
    from nucleo.banco import repositorios
    slug = criar_projeto(cliente)
    r = cliente.post(f"/api/projetos/{slug}/anexos", data={"tipo": "imagem"}, files={"arquivo": ("Referência Um.PNG", PNG, "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["caminho"] == f"projetos/{slug}/00-anexos/referencia.png"
    assert repositorios.arquivo(r.json()["caminho"])
    assert cliente.get(f"/api/projetos/{slug}/arquivos/00-anexos/referencia.png").content == PNG
    r2 = cliente.post(f"/api/projetos/{slug}/anexos", data={"tipo": "imagem"}, files={"arquivo": ("referencia.png", PNG, "image/png")})
    assert r2.json()["caminho"].endswith("referencia-2.png")

    r = cliente.post(f"/api/projetos/{slug}/anexos", data={"tipo": "musica"}, files={"arquivo": ("Trilha Épica.mp3", b"ID3fake", "audio/mpeg")})
    assert r.status_code == 200 and r.json() == {"caminho": "biblioteca/musicas/trilha-epica.mp3", "nome": "trilha-epica.mp3"}
    assert "trilha-epica.mp3" in cliente.get("/api/opcoes").json()["musicas"]

    assert cliente.post(f"/api/projetos/{slug}/anexos", data={"tipo": "imagem"},
                        files={"arquivo": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 400
    assert cliente.post(f"/api/projetos/{slug}/anexos", data={"tipo": "outro"},
                        files={"arquivo": ("x.png", PNG, "image/png")}).status_code == 400


def test_capa(cliente):
    slug = criar_projeto(cliente)
    gravar(f"projetos/{slug}/10-video/thumbnail.jpg", PNG)  # o ffmpeg reconhece pelo conteúdo
    capa = next(p for p in cliente.get("/api/projetos").json() if p["slug"] == slug)["capa"]
    assert capa.startswith(f"/api/projetos/{slug}/capa?v=")
    r = cliente.get(capa)
    assert r.status_code == 200 and r.content


# ---------------------------------------------------------------- dados das etapas


def test_roteiros_editar_e_final(cliente, com_chaves):
    from nucleo.banco import repositorios
    slug = criar_projeto(cliente)
    com_brief(slug, personagens=[{"nome": "Moranguete"}])
    gravar(f"projetos/{slug}/04-angulo-escolhido.json", json.dumps({"id": "A1"}))
    gravar(f"projetos/{slug}/05-roteiro-v1.md", ROTEIRO.format(slug=slug))
    d = cliente.get(f"/api/projetos/{slug}/roteiros").json()
    assert [v["versao"] for v in d["versoes"]] == [1] and not d["tem_final"]
    assert "Moranguete" in d["nomes"] and d["versoes"][0]["origem"] == "roteirista"
    doc = d["versoes"][0]["doc"]
    fala = next(i for i in doc["blocos"][0]["itens"] if i["tipo"] == "fala")
    fala["texto"] = "Esquece o cinema: o navio era minúsculo."

    r = cliente.post(f"/api/projetos/{slug}/roteiros", json={"base": 1, "doc": doc})
    assert r.status_code == 200, r.text
    assert r.json()["versao"] == 2 and r.json()["arquivo"] == f"projetos/{slug}/05-roteiro-v2.md"
    texto = repositorios.texto_do_arquivo(f"projetos/{slug}/05-roteiro-v2.md")
    assert "minúsculo" in texto and "[VISUAL: um navio pequeno no mar]" in texto  # a indicação de imagem continua
    assert cliente.post(f"/api/projetos/{slug}/roteiros", json={"base": 9, "doc": doc}).status_code == 400

    r = cliente.post(f"/api/projetos/{slug}/roteiros/2/final")
    assert r.status_code == 200 and "proxima" in r.json()
    assert repositorios.texto_do_arquivo(f"projetos/{slug}/06-roteiro-final.md") == texto
    assert next(v for v in cliente.get(f"/api/projetos/{slug}/roteiros").json()["versoes"] if v["versao"] == 2)["final"]

    id_ = cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "avaliar", "versao": 2}).json()["id"]
    assert cliente.post(f"/api/projetos/{slug}/roteiros/1/final").status_code == 409
    estado = cliente.get(f"/api/projetos/{slug}").json()
    assert estado["rodando"]["etapa"] == "avaliar"  # a tela mostra o que roda; a trilha conta como roteiro
    assert repositorios.execucao(id_)["parametros"]["versao"] == 2


def test_angulos_escolher(cliente):
    from nucleo.banco import repositorios
    slug = criar_projeto(cliente)
    gravar(f"projetos/{slug}/03-angulos.json", json.dumps({"angulos": [{"id": "A1", "titulo": "Um"}, {"id": "A2", "titulo": "Dois"}],
                                                          "recomendacao": {"angulo": "A2"}}))
    d = cliente.get(f"/api/projetos/{slug}/angulos").json()
    assert d["angulos"]["recomendacao"]["angulo"] == "A2" and d["escolhido"] is None
    assert cliente.post(f"/api/projetos/{slug}/angulos/escolher", json={"id": "A9"}).status_code == 400
    r = cliente.post(f"/api/projetos/{slug}/angulos/escolher", json={"id": "A2", "ajustes": "título mais curto"})
    assert r.status_code == 200
    escolhido = json.loads(repositorios.texto_do_arquivo(f"projetos/{slug}/04-angulo-escolhido.json"))
    assert escolhido["id"] == "A2" and escolhido["ajustes_do_usuario"] == "título mais curto"
    assert repositorios.projeto(slug)["etapa"] == "ganchos"


def test_ganchos_escolher(cliente):
    from nucleo.banco import repositorios
    from nucleo.pipeline import etapas
    slug = criar_projeto(cliente)
    gravar(f"projetos/{slug}/04-angulo-escolhido.json", json.dumps({"id": "A1", "obrigacoes": ["os oito cargos"]}))
    cliente.get(f"/api/projetos/{slug}/ganchos")  # baixa o projeto para a cópia local
    angulo = etapas.hash_de(etapas.pasta(slug) / "04-angulo-escolhido.json")
    lista = {"angulo": angulo, "ganchos": [
        {"id": "G1", "texto": "Você tem 19 anos.", "tipo_gancho": "pov-segunda-pessoa", "gatilhos": ["identificacao"],
         "obrigacoes": ["mostrar o primeiro cargo"], "leitura_fria": ["B01: ok"], "criterios": [], "nota_percentual": 90},
        {"id": "G2", "texto": "Não é a arma.", "tipo_gancho": "negacao-revelacao", "gatilhos": ["curiosidade"]}],
        "melhores": ["G1", "G2"], "recomendacao": {"gancho": "G1", "motivo": "maior nota"}}
    gravar(f"projetos/{slug}/04-ganchos.json", json.dumps(lista))
    d = cliente.get(f"/api/projetos/{slug}/ganchos").json()
    assert d["lista_do_angulo"] and d["escolhido"] is None
    assert cliente.post(f"/api/projetos/{slug}/ganchos/escolher", json={"id": "G9"}).status_code == 400
    r = cliente.post(f"/api/projetos/{slug}/ganchos/escolher", json={"id": "G1", "texto": "Você tem 19 anos e um rádio."})
    assert r.status_code == 200
    escolhido = json.loads(repositorios.texto_do_arquivo(f"projetos/{slug}/04-gancho-escolhido.json"))
    assert escolhido["texto"] == "Você tem 19 anos e um rádio." and escolhido["editado_pelo_usuario"]
    assert escolhido["texto_proposto"] == "Você tem 19 anos." and escolhido["angulo"] == angulo
    assert "leitura_fria" not in escolhido and escolhido["obrigacoes"] == ["mostrar o primeiro cargo"]
    assert repositorios.projeto(slug)["etapa"] == "roteiro"
    # outro ângulo: a lista antiga não vale mais
    gravar(f"projetos/{slug}/04-angulo-escolhido.json", json.dumps({"id": "A2"}))
    assert cliente.get(f"/api/projetos/{slug}/ganchos").json()["lista_do_angulo"] is False
    assert cliente.post(f"/api/projetos/{slug}/ganchos/escolher", json={"id": "G2"}).status_code == 400


def test_pesquisa_e_visual(cliente):
    slug = criar_projeto(cliente)
    com_brief(slug, trends={"ativa": False, "links": ["https://youtu.be/x"]}, referencias=[{"url": "https://y", "esqueleto": None}])
    gravar(f"projetos/{slug}/02-dossie.md", "# Dossiê")
    d = cliente.get(f"/api/projetos/{slug}/pesquisa").json()
    assert d == {"trends_ativa": False, "links": ["https://youtu.be/x"], "trends": None, "dossie": "# Dossiê",
                 "referencias": [{"url": "https://y", "esqueleto": None}]}
    gravar(f"projetos/{slug}/08-biblia-visual.json", json.dumps({"estilo": {"nome": "3D"}, "personagens": [{"id": "P01"}], "locais": []}))
    gravar(f"projetos/{slug}/08-storyboard.md", "# Storyboard")
    v = cliente.get(f"/api/projetos/{slug}/visual").json()
    assert v["storyboard"] == "# Storyboard" and v["estilo"]["nome"] == "3D" and v["cenas"] == 0


def test_producao(cliente):
    from nucleo.banco import repositorios
    slug = criar_projeto(cliente)
    cenas = [{"id": "B01-01", "bloco": "B01", "descricao": "um navio", "fonte": "ia"},
             {"id": "B01-02", "bloco": "B01", "descricao": "o capitão", "fonte": "ia"},
             {"id": "B02-01", "bloco": "B02", "descricao": "o porto", "fonte": "internet"}]
    gravar(f"projetos/{slug}/08-cenas.json", json.dumps({"cenas": cenas}))
    gravar(f"projetos/{slug}/09-imagens/B01-01.png", PNG)
    gravar(f"projetos/{slug}/09-imagens/provisorio/B01-02.png", PNG)
    gravar(f"projetos/{slug}/09-imagens/manifesto.json", json.dumps({
        "B01-01": {"status": "ok", "origem": "ia", "arquivo": "09-imagens/B01-01.png"},
        "B01-02": {"status": "bloqueado", "origem": "ia"}}))
    gravar(f"projetos/{slug}/09-imagens/revisao.json", json.dumps({  # o formato do fluxo da produção
        "imagens": [{"resumo": "uma cena refeita", "cenas_refazer": [{"id": "B01-02", "motivo": "rosto"}]}],
        "video": {"resumo": "ok", "quadros": {"ok": True, "problemas": []}, "thumbnail": {"ok": True}}}))
    gravar(f"projetos/{slug}/10-video/{slug}.mp4", b"\x00" * 10)
    gravar(f"projetos/{slug}/10-video/thumbnail.jpg", PNG)
    gravar(f"projetos/{slug}/11-publicacao.json", json.dumps({"titulo": "Piratas"}))
    repositorios.registrar_custo("imagem", "gemini", "gemini-3.1-flash-image", {"saida": 1}, 0.04, projeto_slug=slug)

    d = cliente.get(f"/api/projetos/{slug}/producao").json()
    assert d["videos"] == [f"10-video/{slug}.mp4"] and d["publicacao"]["titulo"] == "Piratas"
    assert d["thumbnail"].startswith(f"/api/projetos/{slug}/arquivos/10-video/thumbnail.jpg?v=")
    por_id = {c["id"]: c for c in d["cenas"]}
    assert por_id["B01-01"] == {"id": "B01-01", "bloco": "B01", "imagem": "09-imagens/B01-01.png", "origem": "ia",
                                "status": "ok", "descricao": "um navio"}
    assert por_id["B01-02"]["status"] == "bloqueado" and por_id["B01-02"]["imagem"] == "09-imagens/provisorio/B01-02.png"
    assert por_id["B02-01"]["status"] == "faltando" and por_id["B02-01"]["imagem"] is None
    assert [r["rodada"] for r in d["revisao"]] == [1, "final (vídeo montado)"] and d["avisos"] == []
    assert d["revisao"][0]["cenas_refazer"][0]["id"] == "B01-02"
    assert d["custo"]["total_usd"] == pytest.approx(0.04) and d["custo"]["por_etapa"][0]["etapa"] is None
    assert d["custo"]["tabela"]["conferido_em"]
    assert cliente.get(d["thumbnail"]).status_code == 200


def test_desempenho(cliente):
    slug = criar_projeto(cliente)
    gravar(f"projetos/{slug}/06-roteiro-final.md", ROTEIRO.format(slug=slug))
    r = cliente.post(f"/api/projetos/{slug}/desempenho",
                     json={"video_id": "DYAYD0UYvvE", "janela": "7d", "metricas": {"ctr": 6.5, "views": 1200}, "obs": None})
    assert r.status_code == 200, r.text
    reg = r.json()["registro"]
    assert reg["tecnicas"] == ["tese-contraria", "final-em-loop"] and reg["tipo_gancho"] == "tese-contraria"
    assert reg["formato"] == "short" and reg["gatilhos"] == ["curiosidade", "surpresa"]
    lista = cliente.get(f"/api/projetos/{slug}/desempenho").json()
    assert len(lista) == 1 and lista[0]["metricas"]["ctr"] == 6.5 and lista[0]["janela"] == "7d"
    cliente.post(f"/api/projetos/{slug}/desempenho", json={"video_id": "DYAYD0UYvvE", "janela": "7d", "metricas": {"ctr": 7}})
    assert len(cliente.get(f"/api/projetos/{slug}/desempenho").json()) == 1  # a mesma janela substitui
    canal = cliente.get("/api/desempenho").json()
    assert canal["registros"] and "### Por gatilho emocional" in canal["aprendizados"]
    assert cliente.post(f"/api/projetos/{slug}/desempenho", json={"video_id": "x", "metricas": {}}).status_code == 400
    assert cliente.post(f"/api/projetos/{slug}/desempenho",
                        json={"video_id": "DYAYD0UYvvE", "janela": "1ano", "metricas": {"ctr": 1}}).status_code == 400


def test_fork(cliente):
    from nucleo.banco import repositorios
    slug = criar_projeto(cliente)
    gravar(f"projetos/{slug}/00-pedido.yaml", f"slug: {slug}\n")
    com_brief(slug)
    gravar(f"projetos/{slug}/02-dossie.md", "# Dossiê")
    r = cliente.post(f"/api/projetos/{slug}/fork", json={"ate": "sugestoes", "titulo": "Outra versão"})
    assert r.status_code == 200, r.text
    novo = r.json()["slug"]
    assert novo == f"{slug}-fork" and r.json()["etapa"] == "sugestoes"
    assert repositorios.projeto(novo)["titulo"] == "Outra versão"
    assert repositorios.texto_do_arquivo(f"projetos/{novo}/00-pedido.yaml") == f"slug: {novo}\n"
    assert repositorios.arquivo(f"projetos/{novo}/02-dossie.md") is None  # a pesquisa fica para o fork refazer
    assert cliente.post(f"/api/projetos/{slug}/fork", json={"ate": "inventada"}).status_code == 400


# ---------------------------------------------------------------- opções, contextos, biblioteca


def test_opcoes(cliente):
    gravar("biblioteca/vozes/Kore.mp3", b"ID3amostra")
    d = cliente.get("/api/opcoes").json()
    assert {"campos", "vozes", "vozes_por_fornecedor", "voz_do_canal", "musicas", "contextos", "personagens", "cores",
            "modos_voz", "etapas"} <= set(d)
    kore = next(v for v in d["vozes"] if v["nome"] == "Kore")
    assert kore["amostra"] == "/api/biblioteca/vozes/Kore" and kore["genero"] == "f"
    assert next(v for v in d["vozes"] if v["nome"] == "Puck")["amostra"] is None
    assert set(d["vozes_por_fornecedor"]) >= {"gemini", "openai", "elevenlabs", "espeak"}
    assert any(c["chave"] == "tema" for c in d["campos"]) and "narrador" in d["modos_voz"]
    r = cliente.get(kore["amostra"])
    assert r.status_code == 200 and r.content == b"ID3amostra" and r.headers["content-type"] == "audio/mpeg"


def test_contextos(cliente):
    from nucleo.banco import repositorios
    c = cliente.post("/api/contextos", json={"nome": "Novela das frutas"}).json()
    id_ = c["id"]
    assert id_ == "novela-frutas" and c["caminho"] == f"pedidos/contextos/{id_}/contexto.md" and not c["legado"]
    a = cliente.post(f"/api/contextos/{id_}/anexos", files={"arquivo": ("Moranguete.png", PNG, "image/png")},
                     data={"descricao": "a vilã"}).json()
    assert a["anexos"] == [{"arquivo": "moranguete.png", "tipo": "imagem", "descricao": "a vilã"}]
    s = cliente.put(f"/api/contextos/{id_}", json={"nome": "Novela", "texto": "Frutas que falam.",
                                                   "descricoes": {"moranguete.png": "a vilã charmosa"}}).json()
    assert s["nome"] == "Novela" and s["anexos"][0]["descricao"] == "a vilã charmosa"
    md = repositorios.texto_do_arquivo(f"pedidos/contextos/{id_}/contexto.md")
    assert "Frutas que falam." in md and f"`pedidos/contextos/{id_}/moranguete.png`: a vilã charmosa" in md
    manifesto = json.loads(repositorios.texto_do_arquivo(f"pedidos/contextos/{id_}/contexto.json"))
    assert manifesto["anexos"][0]["arquivo"] == "moranguete.png"  # o pedido.py brief lê os anexos daqui
    assert cliente.get(f"/api/contextos/{id_}/arquivos/moranguete.png").content == PNG
    assert cliente.get(f"/api/contextos/{id_}/arquivos/contexto.json").status_code == 404  # só os anexos
    lista = cliente.get("/api/contextos").json()
    assert lista[0]["imagens"] == ["moranguete.png"] and lista[0]["resumo"] == "Frutas que falam."

    gravar("pedidos/contextos/antigo.md", "# Contexto: Série antiga\n\nTexto velho.")
    antigo = cliente.get("/api/contextos/antigo").json()
    assert antigo["legado"] and antigo["nome"] == "Série antiga"
    assert any(x["id"] == "antigo" and x["legado"] for x in cliente.get("/api/contextos").json())
    assert cliente.put("/api/contextos/antigo", json={"nome": "x", "texto": "y"}).status_code == 400

    assert cliente.delete(f"/api/contextos/{id_}/anexos/moranguete.png").json()["anexos"] == []
    assert repositorios.arquivo(f"pedidos/contextos/{id_}/moranguete.png")["apagado_em"]
    assert cliente.delete(f"/api/contextos/{id_}").status_code == 200
    assert cliente.get(f"/api/contextos/{id_}").status_code == 404
    assert cliente.get("/api/contextos/..").status_code == 404


FICHA = {
    "slug": "bananildo", "nome": "Bananildo", "resumo": "O beta da novela.", "referencia_en": "Bananildo the banana",
    "descricao_en": "Bananildo the banana, an anthropomorphic banana adult man with round glasses",
    "estilo": {"nome": "3D", "prompt_base": "3D animated film still", "negativo_base": "text"},
    "fisico": {"tipo": "Banana humanizada."}, "comportamento": {"personalidade": "Gentil."},
    "voz": {"voz": "Enceladus", "estilo": "cansado", "cor": "roxo"},
}


def test_personagens(cliente, com_chaves):
    from nucleo.banco import repositorios
    gravar("biblioteca/personagens/moranguete/personagem.json",
           json.dumps({"slug": "moranguete", "nome": "Moranguete", "resumo": "A vilã", "voz": {"voz": "Leda"}}))
    gravar("biblioteca/personagens/moranguete/imagem.jpg", PNG)
    gravar("rascunhos/personagens/bananildo/ficha.json", json.dumps(FICHA))
    gravar("rascunhos/personagens/bananildo/v1.png", PNG)
    gravar("rascunhos/personagens/bananildo/v1.json", json.dumps({
        "versao": "v1", "arquivo": "v1.png", "modelo": "gemini-3.1-flash-image", "provedor": "gemini", "proporcao": "1:1",
        "tamanho": None, "prompt": "Character reference sheet. Bananildo the banana...", "negativo": "text", "base": None,
        "ajuste": None, "gerado_em": "2026-10-21T08:51:55", "segundos": 9.1, "ficha": FICHA}))

    d = cliente.get("/api/personagens").json()
    assert [p["slug"] for p in d["biblioteca"]] == ["moranguete"]
    assert d["biblioteca"][0]["imagem"].startswith("/api/biblioteca/personagens/moranguete/imagem?v=")
    assert cliente.get(d["biblioteca"][0]["imagem"]).content == PNG
    assert d["rascunhos"][0]["slug"] == "bananildo" and d["rascunhos"][0]["versoes"] == 1
    assert cliente.get("/api/opcoes").json()["personagens"][0]["slug"] == "moranguete"

    r = cliente.get("/api/personagens/rascunhos/bananildo").json()
    assert r["nome"] == "Bananildo" and r["versoes"][0]["versao"] == "v1"
    assert r["versoes"][0]["imagem"] == "rascunhos/personagens/bananildo/v1.png"
    assert not r["na_biblioteca"] and r["aprovada"] is None
    assert cliente.get("/api/arquivos/rascunhos/personagens/bananildo/v1.png").content == PNG
    assert cliente.get("/api/personagens/rascunhos/nao-existe").status_code == 404

    novo = cliente.post("/api/personagens", json={"descricao": "Um abacaxi rabugento que fala alto.", "nome": "Abacaxildo"})
    assert novo.status_code == 200, novo.text
    slug, id_ = novo.json()["slug"], novo.json()["id"]
    assert slug == "abacaxildo"
    e = repositorios.execucao(id_)
    assert e["chave_exclusiva"] == "personagem:abacaxildo" and e["parametros"]["descricao"].startswith("Um abacaxi")
    assert "abacaxi rabugento" in repositorios.texto_do_arquivo("rascunhos/personagens/abacaxildo/descricao.md")
    rascunho = cliente.get(f"/api/personagens/rascunhos/{slug}").json()  # sem ficha ainda, mas com a execução
    assert rascunho["nome"] == "Abacaxildo" and rascunho["rodando"]["id"] == id_

    g = cliente.post("/api/personagens/rascunhos/bananildo/gerar", json={"base": "v1", "ajuste": "óculos maiores"})
    assert g.status_code == 200, g.text
    assert repositorios.execucao(g.json()["id"])["parametros"]["base"] == "v1"
    assert cliente.post("/api/personagens/rascunhos/bananildo/gerar", json={}).status_code == 409  # já há uma
    assert cliente.post("/api/personagens/rascunhos/bananildo/aprovar", json={"versao": "v1"}).status_code == 409
    cliente.post(f"/api/execucoes/{g.json()['id']}/cancelar")
    assert cliente.post("/api/personagens/rascunhos/bananildo/gerar", json={"base": "v7"}).status_code == 400

    a = cliente.post("/api/personagens/rascunhos/bananildo/aprovar", json={"versao": "v1", "sobrescrever": False})
    assert a.status_code == 200, a.text
    assert repositorios.arquivo("biblioteca/personagens/bananildo/personagem.json")
    assert repositorios.arquivo("biblioteca/personagens/bananildo/imagem.png")
    r = cliente.get("/api/personagens/rascunhos/bananildo").json()
    assert r["na_biblioteca"] and r["aprovada"] == "v1"
    assert cliente.post("/api/personagens/rascunhos/bananildo/aprovar", json={"versao": "v1"}).status_code == 400  # já existe


def tecnica_de_exemplo() -> dict:
    """Uma técnica no formato da biblioteca (a aprovação confere o formato), tirada do tecnicas.json do repositório."""
    from nucleo import RAIZ
    return json.loads((RAIZ / "biblioteca" / "tecnicas.json").read_text(encoding="utf-8"))["tecnicas"][0]


def test_referencias(cliente, com_chaves):
    from nucleo.banco import repositorios
    gravar("biblioteca/tecnicas.json", json.dumps({"tecnicas": [{"id": "loop-aberto", "nome": "Loop aberto"}]}))
    gravar("biblioteca/esqueletos/canal-DYAYD0UYvvE.json", json.dumps({
        "fonte": {"titulo": "Como é ser", "canal": "Canal", "url": "https://www.youtube.com/watch?v=DYAYD0UYvvE",
                  "analisado_em": "2026-10-19"},
        "template_abstrato": "Escada.", "tecnicas_detectadas": ["loop-aberto"],
        "tecnicas_novas_sugeridas": [{**tecnica_de_exemplo(), "id": "escada-de-cargos", "nome": "Escada de cargos"}]}))
    d = cliente.get("/api/referencias").json()
    e = d["esqueletos"][0]
    assert e["arquivo"] == "canal-DYAYD0UYvvE.json" and e["tecnicas_aprovadas"] == []
    assert e["tecnicas_novas_sugeridas"][0]["id"] == "escada-de-cargos" and d["rodando"] is None

    assert cliente.post("/api/referencias", json={"url": "https://youtu.be/DYAYD0UYvvE"}).status_code == 409  # já analisado
    assert cliente.post("/api/referencias", json={"url": "não é link"}).status_code == 400
    r = cliente.post("/api/referencias", json={"url": "https://www.youtube.com/watch?v=abcdefghijk"})
    assert r.status_code == 200, r.text
    ex = repositorios.execucao(r.json()["id"])
    assert ex["projeto_slug"] is None and ex["parametros"]["url"].endswith("abcdefghijk")
    assert cliente.get("/api/referencias").json()["rodando"]["id"] == r.json()["id"]

    t = cliente.post("/api/referencias/canal-DYAYD0UYvvE.json/tecnicas", json={"aprovar": ["escada-de-cargos"]})
    assert t.status_code == 200, t.text
    ids = {x["id"] for x in json.loads(repositorios.texto_do_arquivo("biblioteca/tecnicas.json"))["tecnicas"]}
    assert ids == {"loop-aberto", "escada-de-cargos"}
    e = cliente.get("/api/referencias").json()["esqueletos"][0]
    assert e["tecnicas_aprovadas"] == ["escada-de-cargos"]
    assert cliente.post("/api/referencias/..%2Ftecnicas.json/tecnicas", json={"aprovar": ["x"]}).status_code == 404
    assert cliente.post("/api/referencias/nao-existe.json/tecnicas", json={"aprovar": ["x"]}).status_code == 404
