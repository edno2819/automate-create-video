"""As etapas do pipeline (nucleo/pipeline/etapas.py): a trilha do projeto de roteiro importado, as entradas com
partes do brief (o que desatualiza o visual e a produção), o registro de versões, o fork pelo espaço de trabalho e
o pedido.py rodando no espaço de trabalho (o brief com caminhos relativos e a marca do roteiro importado)."""

from __future__ import annotations

import json
import os
import time


from nucleo.pipeline import etapas


def gravar(caminho, conteudo) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(conteudo if isinstance(conteudo, str) else json.dumps(conteudo, ensure_ascii=False),
                       encoding="utf-8")


def envelhecer(caminho, segundos: int = 100) -> None:
    t = time.time() - segundos
    os.utime(caminho, (t, t))


BRIEF = {"tema": "gatos", "formato": "longo", "tom": "leve", "estilo_artistico": "aquarela",
         "visual": {"proporcao": "16:9", "qualidade": "padrao", "imagens_da_internet": False},
         "narracao": {"voz": "Charon"}, "legendas": {"posicao": "baixo"}, "musica": {"ativa": False},
         "producao": {"ate": "video"}, "personagens": []}


def test_caminhos_seguem_o_espaco_de_trabalho(ambiente):
    assert etapas.PROJETOS == ambiente / "projetos"
    assert etapas.PEDIDOS == ambiente / "pedidos" / "web"
    assert etapas.pasta("x") == ambiente / "projetos" / "x"


def test_trilha_do_roteiro_importado(ambiente):
    p = etapas.pasta("imp")
    gravar(p / "00-roteiro-original.md", "Era uma vez um gato.")
    gravar(p / "00-brief.json", BRIEF)
    b = etapas.brief("imp")
    assert b.slug == "imp" and etapas.e_importado(b)
    aplicaveis = [e.id for e in etapas.ETAPAS if etapas.aplicavel(e.id, b)]
    assert aplicaveis == ["configurar", "sugestoes", "importar", "visual", "producao", "edicao"]
    assert etapas.aplicavel("importar", dict(BRIEF)) is False          # sem slug e sem marca: projeto do pipeline
    assert etapas.aplicavel("importar", {**BRIEF, "origem": "roteiro-importado"}) is True
    assert etapas.aplicavel("pesquisa", None, "imp") is False
    assert etapas.proxima("imp", "sugestoes") == "importar"
    assert not etapas.pronta("imp", "importar")
    gravar(p / "06-roteiro-final.md", "## [B01] A\n\nEra uma vez um gato.\n")
    assert etapas.pronta("imp", "importar") and etapas.proxima("imp", "importar") == "visual"


def test_projeto_do_pipeline_nao_mostra_importar(ambiente):
    gravar(etapas.pasta("pip") / "00-brief.json", BRIEF)
    b = etapas.brief("pip")
    assert not etapas.aplicavel("importar", b)
    assert etapas.aplicavel("pesquisa", b)
    assert "cortes" not in etapas.POR_ID  # a etapa dos cortes saiu da trilha


def test_importado_pelo_banco(ambiente):
    from nucleo.banco import repositorios
    repositorios.criar_projeto("banco", "B", {}, origem="roteiro-importado")
    assert etapas.importado("banco")
    gravar(etapas.pasta("banco") / "04-angulo-escolhido.json", {})
    assert not etapas.importado("banco")


def preparar_projeto_produzido(slug: str):
    p = etapas.pasta(slug)
    gravar(p / "00-pedido.yaml", "tema: gatos\n")
    gravar(p / "00-brief.json", BRIEF)
    gravar(p / "02-dossie.md", "# dossiê")
    gravar(p / "02-trends.md", "# trends")
    gravar(p / "04-angulo-escolhido.json", {"id": "A1"})
    gravar(p / "06-roteiro-final.md", "## [B01] A\n\nTexto.\n")
    gravar(p / "08-cenas.json", {"cenas": [{"id": "B01-01"}]})
    gravar(p / "08-storyboard.md", "# sb")
    gravar(p / "10-video" / f"{slug}.mp4", "video")
    for e in ("pesquisa", "roteiro", "visual", "producao"):
        assert etapas.registrar(slug, e)
    return p


def test_partes_do_brief_desatualizam_so_quem_usa(ambiente):
    p = preparar_projeto_produzido("prod")
    assert etapas.defasagens("prod") == {}
    b = json.loads((p / "00-brief.json").read_text())
    b["visual"]["qualidade"] = "maxima"  # só a produção usa a qualidade das imagens
    gravar(p / "00-brief.json", b)
    defasadas = etapas.defasagens("prod")
    assert set(defasadas) == {"producao"} and "o pedido mudou" in defasadas["producao"]
    b["estilo_artistico"] = "nanquim"  # o estilo muda a direção visual, e a produção depende dela
    gravar(p / "00-brief.json", b)
    defasadas = etapas.defasagens("prod")
    assert set(defasadas) == {"visual", "producao"}
    assert "Direção visual" in defasadas["producao"] or "o pedido mudou" in defasadas["producao"]
    b["tom"] = "sombrio"
    gravar(p / "00-brief.json", b)
    assert "roteiro" in etapas.defasagens("prod")
    assert etapas.nota_de_refazer("prod", "visual").startswith("\n\nAtenção: o pedido mudou")


def test_registro_antigo_sem_as_partes_do_brief_nao_desatualiza(ambiente):
    p = preparar_projeto_produzido("velho")
    doc = json.loads((p / etapas.CONTROLE).read_text())
    for etapa in doc.values():  # como era antes das partes do brief entrarem nas entradas
        etapa["entradas"] = {k: v for k, v in etapa["entradas"].items() if "#" not in k}
    gravar(p / etapas.CONTROLE, doc)
    b = json.loads((p / "00-brief.json").read_text())
    b["estilo_artistico"] = "nanquim"
    gravar(p / "00-brief.json", b)
    assert etapas.defasagens("velho") == {}


def test_sem_registro_compara_as_datas(ambiente):
    p = preparar_projeto_produzido("datas")
    (p / etapas.CONTROLE).unlink()
    for arq in p.rglob("*"):
        if arq.is_file():
            envelhecer(arq, 100)
    envelhecer(p / "08-cenas.json", 50)
    assert etapas.mudancas("datas", "producao") == ["visual"]


def test_registrar_so_quando_a_saida_foi_reescrita(ambiente):
    p = preparar_projeto_produzido("desde")
    envelhecer(p / "06-roteiro-final.md", 100)
    assert not etapas.registrar("desde", "roteiro", desde=time.time())
    assert etapas.registrar("desde", "roteiro", desde=time.time() - 200)
    assert not etapas.registrar("desde", "angulos-que-nao-existe")


def test_fork_pelo_espaco_de_trabalho(ambiente):
    from nucleo import espaco
    from nucleo.banco import repositorios
    p = preparar_projeto_produzido("orig")
    gravar(ambiente / "pedidos" / "web" / "orig.yaml", "slug: orig\n")
    gravar(p / "05-roteiro-v1.md", "projeto: orig\n")
    espaco.enviar("projetos/orig/")
    espaco.enviar("pedidos/web/orig.yaml")
    copiadas = etapas.fork("orig", "orig-fork", "roteiro")
    assert copiadas == ["sugestoes", "importar", "pesquisa", "angulos", "ganchos", "roteiro"]
    novo = etapas.pasta("orig-fork")
    assert (novo / "05-roteiro-v1.md").read_text() == "projeto: orig-fork\n"
    assert not (novo / "08-cenas.json").exists() and not (novo / "10-video").exists()
    assert (ambiente / "pedidos" / "web" / "orig-fork.yaml").read_text() == "slug: orig-fork\n"
    assert set(json.loads((novo / etapas.CONTROLE).read_text())) == {"pesquisa", "angulos", "roteiro"}
    assert "projetos/orig-fork/05-roteiro-v1.md" in repositorios.indice("projetos/orig-fork/")
    # o vídeo leva o slug no nome
    etapas.fork("orig", "orig-fork-2", "producao")
    assert (etapas.pasta("orig-fork-2") / "10-video" / "orig-fork-2.mp4").exists()
    assert not (etapas.pasta("orig-fork-2") / "10-video" / "orig.mp4").exists()
    assert etapas.slug_livre("orig", set()) == "orig-fork-3"


def test_capa_e_slug(ambiente):
    p = etapas.pasta("capa")
    assert etapas.capa("capa") is None
    gravar(p / "08-cenas.json", {"cenas": [{"id": "B01-01"}]})
    gravar(p / "09-imagens" / "B01-01.png", "png")
    assert etapas.capa("capa") == p / "09-imagens" / "B01-01.png"
    gravar(p / "10-video" / "thumbnail.jpg", "jpg")
    assert etapas.capa("capa") == p / "10-video" / "thumbnail.jpg"
    assert etapas.slug_de("Como era a higiene na Idade Média?") == "como-era-higiene-idade-media"


def test_pedido_completar_e_brief_no_espaco_de_trabalho(ambiente):
    # producao.cortes saiu do modelo: o pedido antigo que ainda o traz passa sem aviso, e o brief não leva o campo
    gravar(ambiente / "pedidos" / "web" / "gatos.yaml", "tema: gatos no telhado\nslug: gatos\nproducao:\n  cortes: sim\n")
    gravar(ambiente / "biblioteca" / "esqueletos" / "canal-DYAYD0UYvvE.json", {"fonte": {}})
    gravar(etapas.pasta("gatos") / "00-roteiro-original.md", "Era uma vez um gato.")
    definir = ["descricao=um gato", "formato=short", "tom=leve", "natureza=ficcional", "publico=todos", "nicho=gatos",
               "estilo_artistico=aquarela", "visual.imagens_da_internet=não", "personagens=[]"]
    args = ["completar", "pedidos/web/gatos.yaml"] + [x for d in definir for x in ("--definir", d)]
    codigo, saida = etapas.rodar_pedido(*args)
    assert codigo == 0, saida
    assert "fora do modelo" not in saida
    assert (etapas.pasta("gatos") / "00-pedido.yaml").exists()
    codigo, saida = etapas.rodar_pedido("brief", "projetos/gatos")
    assert codigo == 0, saida
    b = etapas.brief("gatos")
    assert b["origem"] == "roteiro-importado"
    assert b["producao"] == {"ate": "video"}  # o roteiro importado também vai até o vídeo, se o usuário não mudar
    assert b["referencias"][0]["esqueleto"] == "biblioteca/esqueletos/canal-DYAYD0UYvvE.json"
    assert "uv run" not in (etapas.pasta("gatos") / "00-pedido.yaml").read_text()


DECISOES = ["descricao=um peixe", "formato=short", "tom=leve", "natureza=factual", "publico=todos", "nicho=peixes",
            "estilo_artistico=aquarela", "visual.imagens_da_internet=não", "personagens=[]"]


def completar(slug: str, pedido: str, *definir: str) -> str:
    gravar(etapas.PEDIDOS / f"{slug}.yaml", pedido)
    args = ["completar", f"pedidos/web/{slug}.yaml"] + [x for d in [*DECISOES, *definir] for x in ("--definir", d)]
    codigo, saida = etapas.rodar_pedido(*args)
    assert codigo == 0, saida
    return saida


def test_duracao_do_roteiro_importado(ambiente):
    # 170 palavras narradas (o cabeçalho não conta): a 170 palavras por minuto, um minuto
    roteiro = "# O peixe\n\n" + " ".join(["peixe"] * 170) + "\n"
    gravar(etapas.pasta("peixe") / "00-roteiro-original.md", roteiro)
    pedido = "tema: o peixe mais raro\nslug: peixe\nnarracao:\n  palavras_por_minuto: 170\n"
    saida = completar("peixe", pedido, "duracao_alvo=00:30")  # a sugestão perde para o roteiro
    assert "170 palavras a 170 palavras por minuto = 01:00" in saida and "não cabe" not in saida
    codigo, saida = etapas.rodar_pedido("brief", "projetos/peixe")
    assert codigo == 0, saida
    assert etapas.brief("peixe")["duracao_alvo"] == "01:00"

    # sem velocidade, vale a padrão (150): 170 palavras dão 01:08
    gravar(etapas.pasta("peixe-150") / "00-roteiro-original.md", roteiro)
    assert "= 01:08" in completar("peixe-150", "tema: o peixe mais raro\nslug: peixe-150\n")

    # a duração que o usuário escreveu fica, com o aviso de que não cabe no roteiro
    gravar(etapas.pasta("peixe-30") / "00-roteiro-original.md", roteiro)
    saida = completar("peixe-30", pedido.replace("slug: peixe", "slug: peixe-30") + 'duracao_alvo: "00:30"\n')
    assert "duração calculada" not in saida and "duracao_alvo 00:30 não cabe no roteiro" in saida and "dão 01:00" in saida

    # projeto sem roteiro pronto: a duração continua do formato
    assert "duração calculada" not in completar("sem-roteiro", "tema: o peixe mais raro\nslug: sem-roteiro\n")


def test_medir_grava_ao_lado(ambiente):
    p = etapas.pasta("med")
    gravar(p / "05-roteiro-v1.md", "---\nformato: short\n---\n\n## [B01] A\n> função: gancho · abre: L1\n\n"
                                   "[VISUAL: x] Um gato voou.\n\n## Loops\n\n- L1: voa?\n")
    m = etapas.medir(p / "05-roteiro-v1.md")
    assert m["palavras"] == 3 and (p / "05-medicao-v1.json").exists()
    assert m["arquivo"] == "projetos/med/05-roteiro-v1.md"
    assert "erro" in etapas.medir(p / "nao-existe.md")
