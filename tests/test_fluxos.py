"""Os fluxos (nucleo/pipeline/fluxos.py) com o motor de agentes e os scripts de produção falsos: os agentes falsos
gravam os arquivos que os de verdade gravariam, e os scripts falsos simulam as saídas e os códigos. Cobre os laços de
qualidade: aprovação recalculada pela rubrica, nova tentativa do crítico, leitura fria gravada e entregue ao crítico,
medição antes do crítico, orientação e edição do usuário, lições no banco, revisão visual que refaz cenas, cenas
bloqueadas suavizadas, blocos refeitos no alinhamento, importação, personagem, referência e cancelamento."""

from __future__ import annotations

import json
import re
import shutil

import pytest

from nucleo import RAIZ
from nucleo.agentes import motor
from nucleo.agentes.erros import Cancelado
from nucleo.agentes.motor import ContextoSimples
from nucleo.banco import repositorios
from nucleo.pipeline import fluxos, verificacoes


# ---------------------------------------------------------------- apoio


class Agentes:
    """O motor falso: cada agente é uma função (mensagem, n-ésima chamada dele) que grava o que precisar."""

    def __init__(self, monkeypatch, **comportamento):
        self.chamadas: list[tuple[str, str]] = []
        self.comportamento = {k.replace("_", "-"): f for k, f in comportamento.items()}
        monkeypatch.setattr(motor, "rodar_agente", self.rodar)
        monkeypatch.setattr(motor, "chamada_unica", self.unica)

    def rodar(self, agente, mensagem, rota, ctx, *, esquema=None, imagens=(), **_):
        nome = agente if isinstance(agente, str) else agente.nome
        n = self.vezes(nome)
        self.chamadas.append((nome, mensagem))
        f = self.comportamento.get(nome)
        return f(mensagem, n) if f else "ok"

    def unica(self, sistema, mensagem, rota, ctx, *, esquema=None, imagens=(), **_):
        self.chamadas.append(("leitor-frio", mensagem))
        return {"leitura_fria": ["B01: um rato fugiu"],
                "confusoes": [{"bloco": "B01", "trecho": "o rato fugiu", "problema": "fugiu de quê?"}]}

    def vezes(self, nome: str) -> int:
        return sum(1 for c in self.chamadas if c[0] == nome)

    def mensagens(self, nome: str) -> list[str]:
        return [m for n, m in self.chamadas if n == nome]


def gravar(caminho, conteudo) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(conteudo if isinstance(conteudo, str) else json.dumps(conteudo, ensure_ascii=False),
                       encoding="utf-8")


def roteiro(visual: bool = True, origem: str | None = None, palavras: int = 37) -> str:
    """Um short de ~75 palavras (o orçamento de 30 s a 150 palavras/min), sem alerta de erro na medição."""
    corpo = " ".join(["rato"] * palavras)
    return ("---\nprojeto: rato\nversao: 1\nformato: short\nduracao_alvo: \"00:30\"\nppm: 150\ntitulo: \"O rato\"\n"
            "tecnicas: [loop-aberto]\ntipo_gancho: pergunta-direta\n" + (f'origem: "{origem}"\n' if origem else "")
            + "---\n\n## [B01] Gancho\n> função: gancho · abre: L1 · fecha: — · conector: — · cortável: não\n\n"
            + ("[VISUAL: um rato na lama] " if visual else "") + f"O rato fugiu. {corpo}.\n\n"
            "## [B02] Fecho\n> função: payoff · abre: — · fecha: L1 · conector: então · cortável: não\n\n"
            f"Então ele voltou. {corpo}.\n\n## Loops\n\n- L1: por que o rato fugiu\n")


def avaliacao(formato: str = "short", notas: dict | None = None, aprovado: bool = True, nota: float = 100) -> dict:
    criterios = []
    for c in verificacoes.criterios_aplicaveis(formato):
        n = (notas or {}).get(c["id"], 2)
        item = {"id": c["id"], "nota": n, "peso": c["peso"]}
        if n < 2:
            item.update(bloco="B01", evidencia="o rato fugiu", problema="ambíguo", instrucao="diga de quê")
        criterios.append(item)
    return {"aprovado": aprovado, "nota_percentual": nota, "criterios": criterios, "leitura_fria": ["B01: ok"],
            "confusoes": [], "prioridades": ["1. clareza"], "manter": ["B02"]}


def saida_da_avaliacao(mensagem: str) -> str:
    return re.search(r"Caminho de saída da avaliação: (\S+)", mensagem).group(1)


@pytest.fixture
def ctx():
    linhas: list[str] = []
    c = ContextoSimples(log=linhas.append)
    c.linhas = linhas
    return c


@pytest.fixture
def projeto(ambiente):
    p = ambiente / "projetos" / "rato"
    gravar(p / "00-brief.json", {"tema": "ratos", "formato": "short", "tom": "leve", "estilo_artistico": "aquarela",
                                 "narracao": {"ativa": True, "fornecedor": "gemini", "palavras_por_minuto": 150},
                                 "visual": {"qualidade": "padrao", "imagens_da_internet": False},
                                 "legendas": {"posicao": "baixo"}, "musica": {"ativa": False}, "personagens": []})
    gravar(p / "04-angulo-escolhido.json", {"id": "A1", "titulo": "O rato", "duracao_recomendada": "00:30",
                                            "estrutura": ["B01: gancho", "B02: fecho"], "riscos": []})
    gravar(p / "02-dossie.md", "# dossiê\n")
    return p


# ---------------------------------------------------------------- roteiro


def test_roteiro_aprovacao_recalculada_leitura_fria_e_nova_tentativa(ctx, projeto, monkeypatch):
    def roteirista(msg, n):
        versao = re.search(r"Versão a escrever: (\d+)", msg).group(1)
        gravar(projeto / f"05-roteiro-v{versao}.md", roteiro())

    def critico(msg, n):
        if n == 0:
            return "esqueci de gravar"  # a primeira tentativa não grava: o fluxo pede de novo
        saida = projeto.parent.parent / saida_da_avaliacao(msg)
        if "v1" in saida.name:  # o crítico diz aprovado, mas zerou um bloqueante: a rubrica reprova
            gravar(saida, avaliacao(notas={"clareza": 0}, aprovado=True, nota=95))
        else:
            gravar(saida, avaliacao())

    agentes = Agentes(monkeypatch, roteirista=roteirista, critico=critico)
    resumo = fluxos.roteiro(ctx, "rato", {"rodadas": 2})
    assert "aprovado na v2" in resumo
    v1 = json.loads((projeto / "05-avaliacao-v1.json").read_text())
    assert v1["aprovado"] is False and v1["bloqueantes_reprovados"] == ["clareza"]
    assert v1["aprovacao_do_critico"]["aprovado"] is True
    # a confusão do leitor frio que o crítico deixou de fora entrou na avaliação
    assert any(c.get("origem") == "leitura fria isolada" for c in v1["confusoes"])
    fria = json.loads((projeto / "05-leitura-fria-v1.json").read_text())
    assert fria["confusoes"][0]["trecho"] == "o rato fugiu"
    msgs = agentes.mensagens("critico")
    assert "05-leitura-fria-v1.json" in msgs[0] and "Na tentativa anterior" in msgs[1]
    assert "[B01]" in agentes.mensagens("leitor-frio")[0]
    assert "Avaliação anterior: projetos/rato/05-avaliacao-v1.json" in agentes.mensagens("roteirista")[1]
    assert (projeto / "06-roteiro-final.md").read_text() == (projeto / "05-roteiro-v2.md").read_text()


def test_roteiro_edicao_do_usuario_orientacao_e_licoes(ctx, projeto, ambiente, monkeypatch):
    (ambiente / "biblioteca").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RAIZ / "biblioteca" / "licoes-roteiro.md", ambiente / "biblioteca" / "licoes-roteiro.md")
    gravar(projeto / "05-roteiro-v1.md", roteiro(origem="editada pelo usuário a partir da v0"))

    def roteirista(msg, n):
        versao = re.search(r"Versão a escrever: (\d+)", msg).group(1)
        gravar(projeto / f"05-roteiro-v{versao}.md", roteiro())
        if versao == "2":
            gravar(projeto / "05-licoes-v2.json", [{"secao": "Final", "regra": "Fecho com virada vale mais",
                                                    "erro": "terminar num fato", "correcao": "terminar numa ironia"},
                                                   {"secao": "Clareza", "regra": "Diga a ação, não só o efeito."}])

    def critico(msg, n):
        saida = projeto.parent.parent / saida_da_avaliacao(msg)
        aprova = "v3" in saida.name
        gravar(saida, avaliacao() if aprova else avaliacao(notas={"clareza": 0}, aprovado=False, nota=70))

    agentes = Agentes(monkeypatch, roteirista=roteirista, critico=critico)
    resumo = fluxos.roteiro(ctx, "rato", {"rodadas": 2, "orientacao": "deixe o fecho mais irônico"})
    assert "aprovado na v3" in resumo
    primeira = agentes.mensagens("roteirista")[0]
    assert "A v1 foi editada pelo usuário" in primeira and "05-orientacao-v2.md" in primeira
    assert (projeto / "05-orientacao-v2.md").exists() and (projeto / "05-orientacao-v3.md").exists()
    assert "05-orientacao-v3.md" in agentes.mensagens("roteirista")[1]
    # lições: a nova entra no banco; a repetida ganha mais uma origem; o arquivo de antes virou a semente
    regras = [l["regra"] for l in repositorios.licoes()]
    assert "Fecho com virada vale mais" in regras and regras.count("Diga a ação, não só o efeito.") == 1
    texto = (ambiente / "biblioteca" / "licoes-roteiro.md").read_text()
    assert "## Final" in texto and "**Fecho com virada vale mais.** Erro: terminar num fato." in texto
    linha = re.search(r"- \*\*Diga a ação.*?(?=\n- |\n\n)", texto, re.S).group(0)
    assert "como-era-higiene-europeus-idade-media, 2026-10-21; rato," in " ".join(linha.split())


def test_medicao_antes_do_critico_pede_uma_correcao(ctx, projeto, monkeypatch):
    def roteirista(msg, n):
        if "medição objetiva" in msg:
            gravar(projeto / "05-roteiro-v1.md", roteiro())
        else:
            gravar(projeto / "05-roteiro-v1.md", roteiro(visual=False))  # short sem [VISUAL] no B01: erro

    agentes = Agentes(monkeypatch, roteirista=roteirista,
                      critico=lambda msg, n: gravar(projeto.parent.parent / saida_da_avaliacao(msg), avaliacao()))
    fluxos.roteiro(ctx, "rato", {"rodadas": 1})
    correcao = agentes.mensagens("roteirista")[1]
    assert "medição objetiva" in correcao and "[visual]" in correcao
    assert (projeto / "05-medicao-v1.json").exists()


# ---------------------------------------------------------------- ganchos e contrato


def test_ganchos_leitura_fria_notas_e_melhores(ctx, projeto, monkeypatch):
    criterios = [c["id"] for c in verificacoes.rubrica_ganchos()["criterios"]]
    textos = ["O rato fugiu.", "Não foi o gato. Não foi a fome.", "Por que o rato fugiu?", "Você é o rato.",
              "Três da manhã, a ratoeira estala e o rato, que já devia estar morto, ainda corre pela cozinha inteira.",
              "Esse rato tem dono."]

    def redator(msg, n):
        assert "Formato: short" in msg
        return {"ganchos": [{"texto": t, "tipo_gancho": "pergunta-direta", "gatilhos": ["curiosidade"], "padrao": "p",
                             "referencia": "sem referência: estrutura da biblioteca", "obrigacoes": [f"entregar {i}"],
                             "visual": "um rato na lama"} for i, t in enumerate(textos, 1)]}

    def critico(msg, n):
        assert "Avaliação de ganchos" in msg and "projetos/rato/04-ganchos.json" in msg
        doc = json.loads((projeto / "04-ganchos.json").read_text())
        assert all(g["leitura_fria"] and g["palavras"] for g in doc["ganchos"])  # o crítico já recebe a leitura fria
        # G3 inventa um fato: credibilidade 0 (bloqueante); G4 é mais fraco que os outros
        notas = {"G3": {"credibilidade-da-promessa": 0}, "G4": {"curiosidade": 0}}
        return {"ganchos": [{"id": g["id"], "resumo": "ok", "criterios": [
            {"id": c, "nota": notas.get(g["id"], {}).get(c, 2)} for c in criterios]} for g in doc["ganchos"]],
            "recomendacao": {"gancho": "G2", "motivo": "o mais claro"}}

    agentes = Agentes(monkeypatch, redator_ganchos=redator, critico=critico)
    resumo = fluxos.ganchos(ctx, "rato", {})
    doc = json.loads((projeto / "04-ganchos.json").read_text())
    assert [g["id"] for g in doc["ganchos"]] == ["G1", "G2", "G3", "G4", "G5", "G6"]
    assert len(agentes.mensagens("leitor-frio")) == 6  # uma leitura isolada por gancho
    assert all("[B01]" in m and "espero:" in m for m in agentes.mensagens("leitor-frio"))
    g3, g5 = doc["ganchos"][2], doc["ganchos"][4]
    assert not g3["elegivel"] and g3["bloqueantes"] == ["credibilidade-da-promessa"]
    # a leitura fria falsa sempre acha uma confusão: clareza no máximo 1; o G5 passa do dobro da janela de 2 s
    assert all(next(c["nota"] for c in g["criterios"] if c["id"] == "clareza-imediata") == 1 for g in doc["ganchos"])
    assert any("fluidez-e-duracao" in a for a in g5["ajustes_do_sistema"])
    assert doc["melhores"] == ["G1", "G2", "G6"] and doc["recomendacao"]["gancho"] == "G1"
    assert "o crítico preferia G2" in doc["recomendacao"]["motivo"]
    assert doc["angulo"] == fluxos.etapas.hash_de(projeto / "04-angulo-escolhido.json")
    assert "G1" in resumo and "Recomendado: G1" in resumo



def test_ganchos_recomendam_o_que_o_usuario_pediu(ctx, projeto, monkeypatch):
    # o caso do vídeo dos monstros: o crítico preferia o pedido, mas o da referência tinha a nota maior
    b = json.loads((projeto / "00-brief.json").read_text())
    gravar(projeto / "00-brief.json", {**b, "descricao": 'Inicie o video com um hook como "Vc conhece os ratos mais '
                                                         'terriveis? O ultimo é o pior de todos"'})
    criterios = [c["id"] for c in verificacoes.rubrica_ganchos()["criterios"]]
    textos = ["Nível 1: o rato do porão.", "Você conhece os ratos mais terríveis? O último é o pior de todos.",
              "Esse rato tem dono.", "Por que o rato fugiu?", "Você é o rato.", "Não foi o gato."]

    def redator(msg, n):
        assert "Gancho pedido pelo usuário" in msg and "“Vc conhece os ratos mais terriveis?" in msg
        return {"ganchos": [{"texto": t, "tipo_gancho": "pergunta-direta", "gatilhos": ["curiosidade"], "padrao": "p",
                             "referencia": "r", "obrigacoes": [], "visual": "um rato"} for t in textos]}

    def critico(msg, n):
        assert "G2 é o gancho que o usuário pediu" in msg
        doc = json.loads((projeto / "04-ganchos.json").read_text())
        assert next(g for g in doc["ganchos"] if g["id"] == "G2")["pedido_pelo_usuario"] is True
        return {"ganchos": [{"id": g["id"], "resumo": "ok", "criterios": [
            {"id": c, "nota": 1 if (c == "padroes-observados" and g["id"] != "G1") else 2} for c in criterios]}
            for g in doc["ganchos"]], "recomendacao": {"gancho": "G2", "motivo": "é o pedido"}}

    Agentes(monkeypatch, redator_ganchos=redator, critico=critico)
    resumo = fluxos.ganchos(ctx, "rato", {})
    doc = json.loads((projeto / "04-ganchos.json").read_text())
    assert doc["melhores"][0] == "G2" and doc["recomendacao"]["gancho"] == "G2"
    assert doc["recomendacao"]["motivo"].startswith("é o gancho que você pediu")
    assert "Recomendado: G2 (o que você pediu)" in resumo

def test_roteiro_com_gancho_escolhido_e_obrigacoes(ctx, projeto, monkeypatch):
    angulo = {"id": "A1", "titulo": "O rato", "duracao_recomendada": "00:30", "estrutura": ["B01: gancho", "B02: fecho"],
              "riscos": [], "obrigacoes": ["dizer por que o rato fugiu"]}
    gravar(projeto / "04-angulo-escolhido.json", angulo)
    gravar(projeto / "04-gancho-escolhido.json", {
        "id": "G2", "texto": "O rato fugiu.", "tipo_gancho": "negacao-revelacao", "gatilhos": ["curiosidade", "surpresa"],
        "obrigacoes": ["mostrar para onde ele foi", "Dizer por que o rato fugiu"],
        "angulo": fluxos.etapas.hash_de(projeto / "04-angulo-escolhido.json")})

    def roteirista(msg, n):
        if "corrija o próprio" in msg:  # a rodada de correção antes do crítico
            assert "[gancho]" in msg and "\"O rato fugiu.\"" in msg
            gravar(projeto / "05-roteiro-v1.md", roteiro())
            return
        versao = re.search(r"Versão a escrever: (\d+)", msg).group(1)
        texto = roteiro()
        gravar(projeto / f"05-roteiro-v{versao}.md", texto.replace("O rato fugiu.", "Um gato miou.") if versao == "1" else texto)

    def critico(msg, n):
        saida = projeto.parent.parent / saida_da_avaliacao(msg)
        a = avaliacao()
        if n == 0:
            gravar(saida, a)  # sem as obrigações: o fluxo pede de novo
            return
        cumprida = "nao" if "v1" in saida.name else "sim"
        a["obrigacoes"] = [{"id": "O1", "obrigacao": "dizer por que o rato fugiu", "cumprida": "sim"},
                           {"id": "O2", "obrigacao": "mostrar para onde ele foi", "cumprida": cumprida, "onde": "B02"}]
        gravar(saida, a)

    agentes = Agentes(monkeypatch, roteirista=roteirista, critico=critico)
    resumo = fluxos.roteiro(ctx, "rato", {"rodadas": 2})
    assert "aprovado na v2" in resumo
    primeira = agentes.mensagens("roteirista")[0]
    assert "Gancho escolhido pelo usuário" in primeira and "\"O rato fugiu.\"" in primeira
    # as obrigações do ângulo e do gancho, sem a repetida, numeradas para o roteirista e para o crítico
    assert "- O1 (ângulo): dizer por que o rato fugiu" in primeira and "- O2 (gancho G2): mostrar para onde" in primeira
    assert "O3" not in primeira
    msgs = agentes.mensagens("critico")
    assert "- O2 (gancho G2)" in msgs[0] and "Na tentativa anterior" in msgs[1] and "obrigacoes: faltam O1, O2" in msgs[1]
    v1 = json.loads((projeto / "05-avaliacao-v1.json").read_text())
    promessas = next(c for c in v1["criterios"] if c["id"] == "promessas-cumpridas")
    assert promessas["nota"] == 0 and "O2" in promessas["problema"] and promessas["bloco"] == "B02"
    assert v1["aprovado"] is False and v1["bloqueantes_reprovados"] == ["promessas-cumpridas"]
    assert v1["prioridades"][0].startswith("Cumpra as obrigações do contrato")
    # o frontmatter da versão que abre com o gancho leva o tipo e os gatilhos dele (o desempenho agrupa por eles)
    texto = (projeto / "05-roteiro-v1.md").read_text()
    assert "tipo_gancho: negacao-revelacao" in texto and "gatilhos: [curiosidade, surpresa]" in texto
    assert "tipo_gancho: pergunta-direta" not in texto


def test_gancho_de_outro_angulo_nao_vale(projeto):
    gravar(projeto / "04-gancho-escolhido.json", {"id": "G1", "texto": "O rato fugiu.", "angulo": "de-outro-angulo",
                                                  "obrigacoes": ["x"]})
    assert fluxos.gancho_escolhido("rato") is None and fluxos.obrigacoes_do_contrato("rato") == []
    gravar(projeto / "05-roteiro-v1.md", roteiro())
    assert fluxos.abre_com_gancho(projeto / "05-roteiro-v1.md", "o rato, fugiu!")
    assert not fluxos.abre_com_gancho(projeto / "05-roteiro-v1.md", "Um gato miou.")


def test_cancelamento_atravessa_o_fluxo(ctx, projeto, monkeypatch):
    def roteirista(msg, n):
        raise Cancelado()
    Agentes(monkeypatch, roteirista=roteirista)
    with pytest.raises(Cancelado):
        fluxos.roteiro(ctx, "rato", {})
    ctx.cancelado.set()
    with pytest.raises(Cancelado):
        fluxos.rodar_script(ctx, "ferramentas/cenas.py", "montar", "projetos/rato")


# ---------------------------------------------------------------- importação


def test_importar_devolve_a_diferenca_ao_formatador(ctx, projeto, monkeypatch):
    gravar(projeto / "00-roteiro-original.md", "O rato fugiu da cidade.\n\nEntão ele voltou para casa.\n")

    def formatador(msg, n):
        texto = "O rato fugiu da vila." if n == 0 else "O rato fugiu da cidade."
        gravar(projeto / "06-roteiro-final.md", f"---\nformato: short\n---\n\n## [B01] A\n> função: gancho\n\n{texto}\n\n"
                                                "## [B02] B\n> função: payoff\n\nEntão ele voltou para casa.\n")

    agentes = Agentes(monkeypatch, formatador=formatador)
    resumo = fluxos.importar(ctx, "rato", {})
    assert "Roteiro importado" in resumo and agentes.vezes("formatador") == 2
    assert "achou diferença" in agentes.mensagens("formatador")[1]


# ---------------------------------------------------------------- produção


class Scripts:
    """Os scripts de produção falsos: gravam o que os de verdade gravariam e devolvem o código combinado."""

    def __init__(self, monkeypatch, projeto):
        self.p = projeto
        self.chamadas: list[tuple[str, list[str]]] = []
        monkeypatch.setattr(fluxos, "rodar_script", self.rodar)

    def rodar(self, ctx, script, *args, **_):
        args = [str(a) for a in args]
        nome = script.rsplit("/", 1)[-1]
        self.chamadas.append((nome, args))
        return getattr(self, nome.replace(".py", ""))(args)

    def de(self, nome):
        return [a for n, a in self.chamadas if n == nome]

    def cenas(self, args):
        return 0, "cenas: 2\nalertas: 0 erro(s), 0 aviso(s)"

    def gerar_imagens(self, args):
        imagens = self.p / "09-imagens"
        imagens.mkdir(parents=True, exist_ok=True)
        (imagens / "folha-de-contato.jpg").write_bytes(b"jpg")
        if "--so-folha" in args:
            return 0, "folha"
        manifesto = json.loads((imagens / "manifesto.json").read_text()) if (imagens / "manifesto.json").exists() else {}
        ids = args[args.index("--cenas") + 1].split(",") if "--cenas" in args else ["B01-01", "B02-01"]
        primeira = len(self.de("gerar_imagens.py")) == 1
        codigo = 0
        for i in ids:
            if primeira and i == "B02-01":  # o filtro recusa na primeira geração
                manifesto[i] = {"status": "bloqueado", "origem": "ia", "erro": "SAFETY", "pedido": None}
                codigo = 1
                continue
            (imagens / f"{i}.png").write_bytes(b"png")
            manifesto[i] = {"status": "ok", "origem": "ia", "arquivo": f"09-imagens/{i}.png"}
        (imagens / "manifesto.json").write_text(json.dumps(manifesto))
        return codigo, "geradas"

    def narrar(self, args):
        gravar(self.p / "09-narracao" / "blocos.json", {"duracao_s": 30.0, "blocos": []})
        (self.p / "09-narracao" / "narracao.wav").write_bytes(b"wav")
        return 0, "narração: 30 s"

    def alinhar(self, args):
        if len(self.de("alinhar.py")) == 1:  # a voz pulou texto no B02
            gravar(self.p / "09-narracao" / "tempos.json", {"qualidade": [{"bloco": "B02", "cobertura_pct": 70}],
                                                            "alertas": ["B02: só 70% das palavras"]})
            return 1, "alerta: B02"
        gravar(self.p / "09-narracao" / "tempos.json", {"qualidade": [], "alertas": []})
        return 0, "tempos ok"

    def animar_imagens(self, args):
        gravar(self.p / "09-animacoes" / "manifesto.json", {
            "B01-01": {"status": "ok", "arquivo": "09-animacoes/B01-01.mp4"},
            "B02-01": {"status": "bloqueado", "erro": "filtro", "pedido": "abc"}})
        return 1, "a animar: 2 cena(s) · 8 s de vídeo · estimado: US$ 0.81 (já prontas: 0)\nanimadas: 1 · bloqueadas: 1"

    def montar_video(self, args):
        gravar(self.p / "10-video" / "rato.mp4", "mp4")
        gravar(self.p / "10-video" / "thumbnail.jpg", "jpg")
        return 0, "duração: 30.0 s · tamanho: 1 MB\nimagens: 2 reais, 0 provisórias, 0 cartelas sobre fundo liso"


def test_producao_com_os_lacos_de_qualidade(ctx, projeto, monkeypatch, tmp_path):
    gravar(projeto / "06-roteiro-final.md", roteiro())
    gravar(projeto / "08-cenas.json", {"proporcao": "9:16", "cenas": [
        {"id": "B01-01", "bloco": "B01", "tipo": "ilustracao", "referencias": ["P01"]},
        {"id": "B02-01", "bloco": "B02", "tipo": "ilustracao", "referencias": ["P01"]}]})
    gravar(projeto / "08-referencias.jsonl", json.dumps({"id": "P01", "tipo": "personagem"}) + "\n")
    scripts = Scripts(monkeypatch, projeto)
    quadro = tmp_path / "quadro-1.jpg"
    quadro.write_bytes(b"\xff\xd8\xff")
    monkeypatch.setattr(fluxos, "extrair_quadros", lambda video, destino: [quadro])

    def revisor(msg, n):
        if msg.startswith("Modo: imagens"):
            cenas = [{"id": "B01-01", "motivo": "rosto diferente", "ajuste_prompt": "keep the same face"}] if n == 0 else []
            return {"resumo": "ok", "cenas_refazer": cenas, "fichas_refazer": [], "internet_trocar": [],
                    "thumbnail": {"ok": True, "motivo": ""}}
        return {"resumo": "ok", "quadros": {"ok": False, "problemas": ["legenda transborda no quadro 1"]},
                "thumbnail": {"ok": True, "motivo": ""}, "ajustes_montagem": {"palavras_por_legenda": 3}}

    def publicador(msg, n):
        gravar(projeto / "11-publicacao.json", {"titulo": "O rato", "plataformas": [{"id": "youtube", "descricao": "d"}],
                                                "tags": {"em_alta": [], "tema": ["ratos"]}, "tags_youtube": "ratos"})
        return "publicação pronta"

    agentes = Agentes(monkeypatch, revisor_visual=revisor, publicador=publicador)
    resumo = fluxos.producao(ctx, "rato", {})
    assert "Vídeo pronto: projetos/rato/10-video/rato.mp4" in resumo
    diretor = agentes.mensagens("diretor-visual")
    assert "recusadas pelo filtro" in diretor[0] and "B02-01" in diretor[0]
    assert "keep the same face" in diretor[1]
    refeita = [a for a in scripts.de("gerar_imagens.py") if "--cenas" in a]
    assert refeita and refeita[0][refeita[0].index("--cenas") + 1] == "B01-01" and "--refazer" not in refeita[0]
    narrar = scripts.de("narrar.py")
    assert narrar[1][-3:] == ["--blocos", "B02", "--refazer"] and "--modelo-whisper" in narrar[0]
    assert len(scripts.de("alinhar.py")) == 2
    montagens = scripts.de("montar_video.py")
    assert "--sem-provisorio" in montagens[0] and "--corte-seco" in montagens[0]
    assert montagens[1][-2:] == ["--palavras-por-legenda", "3"]
    revisao = json.loads((projeto / "09-imagens" / "revisao.json").read_text())
    assert len(revisao["imagens"]) == 2 and revisao["video"]["ajustes_montagem"] == {"palavras_por_legenda": 3}
    assert any("quadros do vídeo" in a for a in ctx.avisos)


def test_producao_anima_depois_do_alinhamento(ctx, projeto, monkeypatch, tmp_path):
    b = json.loads((projeto / "00-brief.json").read_text())
    gravar(projeto / "00-brief.json", {**b, "visual": {**b["visual"], "animacao": "destaques"}})
    gravar(projeto / "06-roteiro-final.md", roteiro())
    gravar(projeto / "08-cenas.json", {"proporcao": "9:16", "cenas": [
        {"id": "B01-01", "bloco": "B01", "tipo": "ilustracao", "referencias": []},
        {"id": "B02-01", "bloco": "B02", "tipo": "ilustracao", "referencias": []}]})
    scripts = Scripts(monkeypatch, projeto)
    quadro = tmp_path / "quadro-1.jpg"
    quadro.write_bytes(b"\xff\xd8\xff")
    monkeypatch.setattr(fluxos, "extrair_quadros", lambda video, destino: [quadro])

    def revisor(msg, n):
        if msg.startswith("Modo: imagens"):
            return {"resumo": "ok", "cenas_refazer": [], "fichas_refazer": [], "internet_trocar": [],
                    "thumbnail": {"ok": True, "motivo": ""}}
        return {"resumo": "ok", "quadros": {"ok": True, "problemas": []}, "thumbnail": {"ok": True, "motivo": ""},
                "ajustes_montagem": {}}

    Agentes(monkeypatch, revisor_visual=revisor, publicador=lambda msg, n: "ok")
    fluxos.producao(ctx, "rato", {})
    ordem = [n for n, _ in scripts.chamadas]
    assert ordem.index("alinhar.py") < ordem.index("animar_imagens.py") < ordem.index("montar_video.py")
    args = scripts.de("animar_imagens.py")[0]
    assert args[args.index("--modelo") + 1] == "gemini-omni-1.1-flash" and args[args.index("--resolucao") + 1] == "720p"
    assert args[args.index("--paralelo") + 1] == "4" and "--cenas" not in args
    imagens = scripts.de("gerar_imagens.py")[0]
    assert imagens[imagens.index("--paralelo") + 1] == "6"
    assert any("B02-01" in a and "recusada(s) pelo filtro" in a for a in ctx.avisos)

    # no modo teste, nada de animação (ela é paga)
    scripts.chamadas.clear()
    fluxos.producao(ctx, "rato", {"teste": True})
    assert scripts.de("animar_imagens.py") == []


def test_producao_em_teste_nao_gasta(ctx, projeto, monkeypatch):
    gravar(projeto / "06-roteiro-final.md", roteiro())
    gravar(projeto / "08-cenas.json", {"cenas": [{"id": "B01-01", "bloco": "B01", "tipo": "ilustracao"}]})
    scripts = Scripts(monkeypatch, projeto)
    agentes = Agentes(monkeypatch, publicador=lambda msg, n: "ok")
    fluxos.producao(ctx, "rato", {"teste": True})
    assert scripts.de("gerar_imagens.py") == [["projetos/rato", "--provisorio"]]
    assert scripts.de("narrar.py")[0][1:3] == ["--fornecedor", "espeak"]
    assert "--sem-provisorio" not in scripts.de("montar_video.py")[0]
    assert agentes.vezes("revisor-visual") == 0


# ---------------------------------------------------------------- personagem e referência

FICHA = {"slug": "moranguete", "nome": "Moranguete", "referencia_en": "Moranguete the strawberry",
         "descricao_en": "Moranguete the strawberry, a cute anthropomorphic strawberry young adult woman, glossy bright-red "
                         "heart-shaped body dotted with tiny golden seeds, a crown of green leaves with a curly stem, huge "
                         "sparkling dark-brown eyes with long curled lashes, a small dark beauty mark under her left eye, "
                         "glossy pink lips, slim little arms and legs, wearing a plain navy-blue cashier vest",
         "estilo": {"prompt_base": "3d animation"}, "fisico": {"traco_marcante": "a pinta", "paleta": "vermelho, verde e rosa"},
         "comportamento": {"personalidade": "doce no caixa"}, "voz": {"voz": "Leda"}}


def test_personagem_ficha_imagem_e_autorrevisao(ctx, ambiente, monkeypatch):
    rascunho = ambiente / "rascunhos" / "personagens" / "moranguete"
    chamadas = []

    def rodar(ctx_, script, *args, **_):
        args = [str(a) for a in args]
        chamadas.append(args)
        n = len(list(rascunho.glob("v*.json"))) + 1
        (rascunho / f"v{n}.png").write_bytes(b"png")
        gravar(rascunho / f"v{n}.json", {"versao": f"v{n}"})
        return 0, f"ok: v{n}"
    monkeypatch.setattr(fluxos, "rodar_script", rodar)

    def designer(msg, n):
        gravar(rascunho / "ficha.json", FICHA)
        return {"slug": "moranguete", "resumo": "uma morango", "mudancas": []}

    def revisor(msg, n):
        return {"ok": n > 0, "defeitos": [] if n else ["sem a pinta"], "ajuste_en": "" if n else "add the beauty mark"}

    Agentes(monkeypatch, designer_personagem=designer, revisor_visual=revisor)
    resumo = fluxos.personagem(ctx, None, {"slug": "moranguete", "subetapa": "ficha", "descricao": "uma morango vilã"})
    assert "2 imagem(ns): v1, v2" in resumo
    assert chamadas[1][-4:] == ["--base", "v1", "--ajuste", "add the beauty mark"]
    assert json.loads((rascunho / "v1.json").read_text())["revisao"]["ok"] is False


def test_referencia_e_aprovar_tecnicas(ctx, ambiente, monkeypatch):
    esqueleto = ambiente / "biblioteca" / "esqueletos" / "canal-abcdefghijk.json"
    nova = {"id": "pergunta-espelho", "nome": "Pergunta espelho", "categoria": "gancho", "formatos": ["short"],
            "descricao": "Abre e fecha com a mesma pergunta.", "beats": ["abre", "fecha"], "origem": "ignorada"}

    def analista(msg, n):
        gravar(esqueleto, {"fonte": {"url": "https://youtu.be/abcdefghijk", "video_id": "abcdefghijk"}, "formato": "short",
                           "gancho": {}, "blocos": [{"id": "B01", "funcao": "gancho"}], "tecnicas_detectadas": [],
                           "tecnicas_novas_sugeridas": [nova], "template_abstrato": {}, "limitacoes": []})
        return "esqueleto gravado"

    Agentes(monkeypatch, analista_referencias=analista)
    resumo = fluxos.referencia(ctx, None, {"url": "https://youtu.be/abcdefghijk"})
    assert "pergunta-espelho" in resumo
    antes = (RAIZ / "biblioteca" / "tecnicas.json").read_text(encoding="utf-8")
    r = fluxos.aprovar_tecnicas("canal-abcdefghijk.json", ["pergunta-espelho", "loop-aberto", "inventada"])
    assert r == {"aprovadas": ["pergunta-espelho"], "ja_existiam": [], "desconhecidas": ["loop-aberto", "inventada"]}
    depois = (ambiente / "biblioteca" / "tecnicas.json").read_text(encoding="utf-8")
    tecnicas = json.loads(depois)
    assert tecnicas["tecnicas"][-1]["origem"] == "esqueleto:canal-abcdefghijk"
    assert depois.startswith(antes.rstrip()[:-len("\n  ]\n}")])  # o resto do arquivo ficou como estava
    assert fluxos.aprovar_tecnicas("canal-abcdefghijk", ["pergunta-espelho"])["ja_existiam"] == ["pergunta-espelho"]
    assert json.loads(esqueleto.read_text())["tecnicas_aprovadas"] == ["pergunta-espelho"]
    assert "biblioteca/tecnicas.json" in repositorios.indice("biblioteca/tecnicas.json")


def test_pesquisa_repete_o_agente_que_nao_gravou(ctx, projeto, monkeypatch):
    b = json.loads((projeto / "00-brief.json").read_text())
    gravar(projeto / "00-brief.json", {**b, "referencias": [], "trends": {"ativa": True}})
    (projeto / "02-dossie.md").unlink()
    monkeypatch.setattr(fluxos.etapas, "rodar_pedido", lambda *a: (0, "brief: ok"))

    def trends(msg, n):
        if n == 1:
            gravar(projeto / "02-trends.md", "# trends")

    agentes = Agentes(monkeypatch, pesquisador_trends=trends,
                      pesquisador_conteudo=lambda msg, n: gravar(projeto / "02-dossie.md", "# dossiê"))
    fluxos.pesquisa(ctx, "rato", {})
    assert agentes.vezes("pesquisador-trends") == 2 and agentes.vezes("pesquisador-conteudo") == 1
    assert "sem gravar projetos/rato/02-trends.md" in agentes.mensagens("pesquisador-trends")[1]
    assert "Chave da YouTube Data API: não há" in agentes.mensagens("pesquisador-trends")[0]
