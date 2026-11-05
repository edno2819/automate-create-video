"""Verificações determinísticas dos artefatos (nucleo/pipeline/verificacoes.py): a aprovação recalculada pela
rubrica, a validação das avaliações, dos ângulos e da ficha de personagem, os [VERIFICAR] que sobraram, a leitura
das saídas dos scripts e o loudness medido com o ffmpeg de verdade."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from nucleo import RAIZ
from nucleo.pipeline import verificacoes as v

precisa_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg não instalado")


def avaliacao(formato: str, notas: dict[str, int] | None = None, **extra) -> dict:
    """Uma avaliação com todos os critérios do formato com nota 2, menos os de `notas`."""
    criterios = []
    for c in v.criterios_aplicaveis(formato):
        nota = (notas or {}).get(c["id"], 2)
        item = {"id": c["id"], "nota": nota, "peso": c["peso"]}
        if nota < 2:
            item.update(bloco="B01", evidencia="citação", problema="problema", instrucao="faça assim")
        criterios.append(item)
    return {"aprovado": True, "nota_percentual": 100, "criterios": criterios, "leitura_fria": ["B01: entendi"],
            "confusoes": [], "prioridades": [], "manter": ["B01"], **extra}


def test_aprovacao_tudo_dois_aprova():
    aprovado, nota, bloqueantes = v.aprovacao_pela_rubrica(avaliacao("longo"), "longo")
    assert (aprovado, nota, bloqueantes) == (True, 100, [])


def test_bloqueante_com_zero_reprova_mesmo_com_nota_alta():
    aprovado, nota, bloqueantes = v.aprovacao_pela_rubrica(avaliacao("longo", {"clareza": 0}), "longo")
    assert not aprovado and nota > 80 and bloqueantes == ["clareza"]


def test_nota_ponderada_abaixo_do_minimo_reprova():
    nao_bloqueantes = [c["id"] for c in v.criterios_aplicaveis("longo") if not c.get("bloqueante")]
    aprovado, nota, bloqueantes = v.aprovacao_pela_rubrica(avaliacao("longo", {i: 0 for i in nao_bloqueantes}), "longo")
    assert not aprovado and nota < 80 and bloqueantes == []


def test_peso_vem_da_rubrica_e_nao_do_critico():
    a = avaliacao("short", {"tom": 0})
    for c in a["criterios"]:
        c["peso"] = 100 if c["id"] == "tom" else 1  # o crítico errou o peso: não pode afundar a nota
    _, nota, _ = v.aprovacao_pela_rubrica(a, "short")
    soma = sum(2 * c["peso"] for c in v.criterios_aplicaveis("short"))
    assert nota == pytest.approx(round((soma - 2) / soma * 100, 1))


def test_corte_conta_como_short_e_so_criterios_do_formato():
    so_longo = {c["id"] for c in v.criterios_aplicaveis("longo")} - {c["id"] for c in v.criterios_aplicaveis("short")}
    assert "gancho-5s" in so_longo and v.formato_da_rubrica("corte") == "short"
    a = avaliacao("short")
    a["criterios"].append({"id": "gancho-5s", "nota": 0, "peso": 3, "bloco": "B01", "evidencia": "x", "instrucao": "y"})
    assert v.aprovacao_pela_rubrica(a, "corte")[0] is True  # o critério do longo não conta no corte


def test_bloqueante_nao_avaliado_impede_a_aprovacao():
    a = avaliacao("short")
    a["criterios"] = [c for c in a["criterios"] if c["id"] != "factualidade"]
    aprovado, _, bloqueantes = v.aprovacao_pela_rubrica(a, "short")
    assert not aprovado and bloqueantes == ["factualidade"]
    assert any("faltam factualidade" in e for e in v.erros_da_avaliacao(a, "short"))


def test_nota_abaixo_de_dois_exige_evidencia_e_instrucao():
    a = avaliacao("short", {"clareza": 1})
    for c in a["criterios"]:
        if c["id"] == "clareza":
            del c["evidencia"]
    erros = v.erros_da_avaliacao(a, "short")
    assert any("evidencia" in e for e in erros)
    assert v.erros_da_avaliacao(avaliacao("short", {"clareza": 1}), "short") == []


def test_validar_arquivo_faltando_e_json_quebrado(tmp_path):
    assert v.validar(tmp_path / "x.json", v.ESQUEMA_PUBLICACAO) == ["x.json não existe"]
    (tmp_path / "y.json").write_text("{quebrado", encoding="utf-8")
    assert "não é um JSON válido" in v.validar(tmp_path / "y.json", v.ESQUEMA_PUBLICACAO)[0]


def test_validar_angulos_confere_a_recomendacao_os_ids_e_a_quantidade(tmp_path):
    def angulo(id_, **extra):
        return {"id": id_, "titulo": "T", "thumbnail": {}, "promessa": "p", "duracao_recomendada": "12:00",
                "tecnicas": [], "tipo_gancho": "negacao-revelacao", "gatilhos": ["curiosidade"], "gancho_proposto": "g",
                "obrigacoes": ["os oito cargos aparecem"], "estrutura": ["B01: x"], **extra}

    def validar(angulos, recomendado="A1"):
        (tmp_path / "a.json").write_text(json.dumps({"recomendacao": {"angulo": recomendado}, "angulos": angulos}),
                                         encoding="utf-8")
        return v.validar_angulos(tmp_path / "a.json")

    assert any("A9" in e for e in validar([angulo("A1"), angulo("A2")], "A9"))
    assert validar([angulo("A1"), angulo("A2")]) == []
    assert validar([angulo("A1")]) and validar([angulo(f"A{i}") for i in range(1, 5)])  # de 2 a 3 ângulos
    assert any("obrigacoes" in e for e in validar([angulo("A1"), angulo("A2", obrigacoes=[])]))
    erros = validar([angulo("A1", tipo_gancho="inventado"), angulo("A2", gatilhos=["curiosidade", "raiva"])])
    assert any("tipo_gancho" in e and "inventado" in e for e in erros) and any("raiva" in e for e in erros)


def test_obrigacoes_na_avaliacao():
    avaliacao = {"criterios": [{"id": "promessas-cumpridas", "nota": 2, "peso": 3}],
                 "obrigacoes": [{"id": "O1", "obrigacao": "os oito cargos", "cumprida": "sim"},
                                {"id": "O2", "obrigacao": "a melhor parte", "cumprida": "parcial", "onde": "B04"}]}
    assert any("O3" in e for e in v.erros_da_avaliacao(avaliacao, "longo", ["O1", "O2", "O3"]))
    assert not any("obrigacoes" in e for e in v.erros_da_avaliacao(avaliacao, "longo", ["O1", "O2"]))
    assert "de 2 para 1" in v.aplicar_obrigacoes(avaliacao)
    item = avaliacao["criterios"][0]
    assert item["nota"] == 1 and item["bloco"] == "B04" and "O2" in item["problema"] and item["instrucao"]
    assert v.aplicar_obrigacoes(avaliacao) is None  # já está no teto
    avaliacao["obrigacoes"][0]["cumprida"] = "nao"
    assert "para 0" in v.aplicar_obrigacoes(avaliacao) and item["nota"] == 0
    # sem o critério, ele entra com a nota da regra; sem obrigação descumprida, nada muda
    sem = {"criterios": [], "obrigacoes": [{"id": "R1", "obrigacao": "três motivos", "cumprida": "nao"}]}
    v.aplicar_obrigacoes(sem)
    assert sem["criterios"][0]["id"] == "promessas-cumpridas" and sem["criterios"][0]["nota"] == 0
    assert v.aplicar_obrigacoes({"criterios": [], "obrigacoes": [{"id": "O1", "cumprida": "sim"}]}) is None


def test_pontuacao_e_melhores_ganchos():
    criterios = [c["id"] for c in v.rubrica_ganchos()["criterios"]]
    tudo_2 = {"criterios": [{"id": c, "nota": 2} for c in criterios]}
    limpo = v.pontuar_gancho({"id": "G1", "segundos": 4}, tudo_2, "longo")
    assert limpo["nota_percentual"] == 100 and limpo["elegivel"] and not limpo["ajustes"]
    # uma confusão da leitura fria: clareza no máximo 1; mais que o dobro da janela: fluidez no máximo 1
    cortado = v.pontuar_gancho({"id": "G2", "segundos": 11, "confusoes": [{"trecho": "x"}]}, tudo_2, "longo")
    notas = {c["id"]: c["nota"] for c in cortado["criterios"]}
    assert notas["clareza-imediata"] == 1 and notas["fluidez-e-duracao"] == 1 and len(cortado["ajustes"]) == 2
    assert cortado["nota_percentual"] < 100 and cortado["elegivel"]
    # duas confusões zeram a clareza, que é bloqueante; no short a janela é de 2 s
    zerado = v.pontuar_gancho({"id": "G3", "confusoes": [{}, {}]}, tudo_2, "longo")
    assert not zerado["elegivel"] and zerado["bloqueantes"] == ["clareza-imediata"]
    assert v.pontuar_gancho({"id": "G4", "segundos": 5}, tudo_2, "short")["ajustes"]
    sem_nota = v.pontuar_gancho({"id": "G5"}, {"criterios": [{"id": "relevancia", "nota": 2}]}, "longo")
    assert not sem_nota["elegivel"]  # bloqueante sem nota não passa
    ganchos = [{"id": "G1", **cortado}, {"id": "G2", **limpo, "confusoes": [{}]}, {"id": "G3", **zerado},
               {"id": "G4", **limpo}, {"id": "G5", **limpo, "nota_percentual": 70}]
    assert v.melhores_ganchos(ganchos) == ["G4", "G2", "G1"]  # empate na nota: menos confusões primeiro



def test_gancho_pedido_pelo_usuario():
    # o caso real: a frase entre aspas depois de "hook", com "Vc" e sem acento
    descricao = ('Conte um pouco sobre cada um. Inicie o video com um hook como "Vc conhece os mais terriveis '
                 'monstros da biblia? O ultimo é o mais assustador de todos"')
    pedidos = v.ganchos_pedidos(descricao, ["sem palavrão"], "", None)
    assert pedidos == ["Vc conhece os mais terriveis monstros da biblia? O ultimo é o mais assustador de todos"]
    assert v.ganchos_pedidos('O vídeo fala de "monstros" antigos.') == []  # aspas sem pedido de abertura não contam
    textos = ["Nível 1: Beemote. Você está diante dos três maiores monstros da Bíblia.",
              "Você conhece os mais terríveis monstros da Bíblia? O último é o mais assustador de todos.",
              "Três monstros colossais da Bíblia: o último arranca estrelas do céu."]
    ganchos = [{"id": f"G{i}", "texto": t} for i, t in enumerate(textos, 1)]
    ganchos[2]["pedido_pelo_usuario"] = True  # o redator marcou o errado: vale o texto
    assert v.marcar_gancho_pedido(ganchos, pedidos) == ("G2", None)
    assert [g.get("pedido_pelo_usuario") for g in ganchos] == [None, True, None]
    # sem frase entre aspas, vale a marca do redator; com frase e sem nenhum parecido, aviso
    outros = [{"id": "G1", "texto": "Esse rato tem dono."}, {"id": "G2", "texto": "x", "pedido_pelo_usuario": True}]
    assert v.marcar_gancho_pedido(outros, []) == ("G2", None)
    marcado, aviso = v.marcar_gancho_pedido([{"id": "G1", "texto": "Esse rato tem dono."}], pedidos)
    assert marcado is None and "não escreveu o gancho que você pediu" in aviso
    # o pedido não perde por falta de padrão e vem primeiro entre os melhores, mesmo com nota menor
    criterios = [c["id"] for c in v.rubrica_ganchos()["criterios"]]
    sem_padrao = {"criterios": [{"id": c, "nota": 1 if c == "padroes-observados" else 2} for c in criterios]}
    pedido = v.pontuar_gancho({"id": "G2", "pedido_pelo_usuario": True}, sem_padrao, "longo")
    assert pedido["nota_percentual"] == 100 and "o gancho que o usuário pediu" in pedido["ajustes"][0]
    lista = [{"id": "G1", "elegivel": True, "nota_percentual": 100},
             {"id": "G2", "elegivel": True, "nota_percentual": 80, "pedido_pelo_usuario": True},
             {"id": "G3", "elegivel": True, "nota_percentual": 90}]
    assert v.melhores_ganchos(lista) == ["G2", "G1", "G3"]
    lista[1]["elegivel"] = False  # não passou num bloqueante: fica fora dos melhores
    assert v.melhores_ganchos(lista) == ["G1", "G3"]

def test_esquemas_dos_ganchos_usam_os_ids_da_biblioteca():
    ids = v.ids_da_biblioteca()
    assert {"negacao-revelacao", "identificacao-condicional", "prova-demonstracao"} <= set(ids["tipos_gancho"])
    assert {"revelacao-progressiva", "prova-visual", "negacao-revelacao"} <= set(ids["tecnicas"])
    assert len(ids["gatilhos"]) == 10
    item = v.esquema_ganchos("longo")["properties"]["ganchos"]
    assert (item["minItems"], item["maxItems"]) == (6, 8)
    assert item["items"]["properties"]["tipo_gancho"]["enum"] == ids["tipos_gancho"]
    assert "visual" in v.esquema_ganchos("short")["properties"]["ganchos"]["items"]["required"]
    avaliacao = v.esquema_avaliacao_ganchos(["G1", "G2"])
    assert avaliacao["properties"]["ganchos"]["items"]["properties"]["id"]["enum"] == ["G1", "G2"]


def test_tem_verificar(tmp_path):
    roteiro = tmp_path / "r.md"
    roteiro.write_text("---\nformato: short\n---\n\n## [B01] A\n> função: gancho\n\nO rei morreu em 1300. "
                       "[VERIFICAR: ano da morte]\n", encoding="utf-8")
    assert v.tem_verificar(roteiro) == [{"bloco": "B01", "item": "ano da morte"}]
    assert v.tem_verificar(tmp_path / "nao-existe.md") == []


def test_blocos_com_defeito_e_alertas_do_cenas():
    tempos = {"qualidade": [{"bloco": "B01", "cobertura_pct": 99, "palavras_extras": 0},
                            {"bloco": "B02", "cobertura_pct": 70, "palavras_extras": 0},
                            {"bloco": "B03", "cobertura_pct": 95, "palavras_extras": 5}],
              "alertas": ["B04: só 80% das palavras"]}
    assert v.blocos_com_defeito(tempos) == ["B02", "B03", "B04"]
    saida = ("cenas: 3\nalertas: 0 erro(s), 3 aviso(s)\n- aviso [B01-02] P01 está na cena mas o prompt_cena não cita 'the boy'\n"
             "- aviso [B02-01] 1.2 s: troca de imagem rápida demais\n- aviso [thumbnail] texto com 6 palavras; o máximo é 4\n")
    alertas = v.alertas_do_cenas(saida)
    assert len(alertas) == 3
    assert [a["onde"] for a in v.avisos_de_coerencia(alertas)] == ["B01-02", "thumbnail"]


def test_enquadramentos_iguais_aos_do_cenas():
    sys.path.insert(0, str(RAIZ / "ferramentas"))
    import cenas
    assert set(v.ENQUADRAMENTOS) == set(cenas.ENQUADRAMENTOS)
    assert set(v.POSICOES_TEXTO_THUMB) == set(cenas.AREAS_TEXTO_THUMB)


def ficha(**mudar) -> dict:
    descricao = ("Moranguete the strawberry, a cute anthropomorphic strawberry young adult woman, glossy bright-red "
                 "heart-shaped body dotted with tiny golden seeds, a crown of green leaves with a curly stem, huge "
                 "sparkling dark-brown eyes with long curled lashes, a small dark beauty mark under her left eye, "
                 "glossy pink lips, slim little arms and legs, wearing a plain navy-blue cashier vest")
    f = {"slug": "moranguete", "nome": "Moranguete", "referencia_en": "Moranguete the strawberry", "descricao_en": descricao,
         "estilo": {"prompt_base": "3d"}, "fisico": {"traco_marcante": "a pinta", "paleta": "Vermelho, verde, azul e rosa."},
         "comportamento": {"personalidade": "x"}, "voz": {"voz": "Leda"}}
    f.update(mudar)
    return f


def test_erros_da_ficha():
    assert v.erros_da_ficha(ficha(), "Sadaltager", {"Leda", "Kore"}) == []
    erros = v.erros_da_ficha(ficha(descricao_en="the girl, small", voz={"voz": "Sadaltager"},
                                   fisico={"traco_marcante": "x", "paleta": "vermelho"}), "Sadaltager", {"Leda"})
    texto = " | ".join(erros)
    assert "palavras" in texto and "referencia_en" in texto and "paleta" in texto and "Sadaltager" in texto
    assert v.cores_da_paleta("Vermelho vivo, verde-folha, azul-marinho e rosa.") == 4


@precisa_ffmpeg
def test_loudness(tmp_path):
    com_audio, sem_audio = tmp_path / "a.m4a", tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:a", "aac", str(com_audio)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
                    str(sem_audio)], check=True)
    valor = v.loudness(com_audio)
    assert isinstance(valor, float) and -40 < valor < 0
    assert v.loudness(sem_audio) is None
    assert v.aviso_de_loudness(-14.5) is None and "LUFS" in v.aviso_de_loudness(-20.0)
