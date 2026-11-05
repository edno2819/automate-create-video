"""Scripts de produção (ferramentas/producao/ e ferramentas/personagem.py): narração com o espeak de verdade num
projeto mínimo, a verificação que não roda (sai com 1, nunca em silêncio), as imagens com um provedor falso
(texto e hash iguais aos de antes, bloqueio no manifesto, sem cota) e o modo provisório com o ffmpeg."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from nucleo import RAIZ
from nucleo.provedores.erros import Bloqueado, SemCota
from nucleo.provedores.imagem.base import ImagemGerada

PRODUCAO = RAIZ / "ferramentas" / "producao"
precisa_espeak = pytest.mark.skipif(not shutil.which("espeak-ng") or not shutil.which("ffmpeg"),
                                    reason="espeak-ng ou ffmpeg não instalado")
precisa_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg não instalado")

ROTEIRO = """---
projeto: teste
versao: 1
formato: short
duracao_alvo: "00:20"
ppm: 150
titulo: "Teste"
tecnicas: [loop-aberto]
tipo_gancho: pergunta-direta
---

## [B01] Abertura
> função: gancho · abre: L1 · fecha: — · conector: — · cortável: não

[VISUAL: um gato no telhado]
Você já viu um gato voar? Hoje você vai ver.

## [B02] Conversa
> função: desenvolvimento · abre: — · fecha: L1 · conector: mas · cortável: não

[VISUAL: dois gatos no telhado]
O gato olhou para o céu.
[FALA: Mimi, animada] Eu vou voar!
[PAUSA]
[FALA: Tom] Não vai não.
"""
BRIEF = {"tom": "divertido", "narracao": {"voz": "Charon", "palavras_por_minuto": 150},
         "personagens": [{"nome": "Mimi", "voz": "Kore", "estilo": "animada"}, {"nome": "Tom", "voz": "Puck"}]}


def carregar(caminho: Path, nome: str):
    """Carrega um script como módulo novo (cada teste com o seu, para o ambiente lido na importação valer)."""
    spec = importlib.util.spec_from_file_location(nome, caminho)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nome] = modulo  # as dataclasses do módulo procuram por ele aqui
    spec.loader.exec_module(modulo)
    return modulo


def rodar(script: Path, *args: str, ambiente: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(RAIZ), **(ambiente or {})}
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, cwd=RAIZ, env=env,
                          timeout=300)


@pytest.fixture
def projeto(tmp_path):
    pasta = tmp_path / "projetos" / "teste"
    pasta.mkdir(parents=True)
    (pasta / "06-roteiro-final.md").write_text(ROTEIRO, encoding="utf-8")
    (pasta / "00-brief.json").write_text(json.dumps(BRIEF, ensure_ascii=False), encoding="utf-8")
    return pasta


# ---------------------------------------------------------------- narração


@precisa_espeak
def test_narrar_com_espeak_de_verdade(projeto):
    r = rodar(PRODUCAO / "narrar.py", str(projeto), "--fornecedor", "espeak", "--sem-verificar")
    assert r.returncode == 0, r.stderr + r.stdout
    estado = json.loads((projeto / "09-narracao" / "blocos.json").read_text(encoding="utf-8"))
    assert estado["fornecedor"] == "espeak" and estado["modelo"] == "espeak-ng" and estado["voz"] == "Charon"
    assert estado["personagens"] == {"Mimi": "Kore", "Tom": "Puck"}
    b01, b02 = estado["blocos"]
    assert b01["id"] == "B01" and b01["palavras"] == 10 and "pedido" in b01 and "falas" not in b01
    assert [f["id"] for f in b02["falas"]] == ["B02-F01", "B02-F02", "B02-F03"]
    assert [f["personagem"] for f in b02["falas"]] == [None, "Mimi", "Tom"]
    assert b02["falas"][1]["voz"] == "Kore" and b02["inicio_s"] > b01["inicio_s"]
    # o [PAUSA] entre as falas de Mimi e Tom vira silêncio a mais na emenda
    assert b02["falas"][2]["inicio_no_bloco_s"] > b02["falas"][1]["inicio_no_bloco_s"] + b02["falas"][1]["duracao_s"] + 0.25
    with wave.open(str(projeto / "09-narracao" / "narracao.wav")) as w:
        assert abs(w.getnframes() / w.getframerate() - estado["duracao_s"]) < 0.01
    assert "python3 ferramentas/producao/alinhar.py" in r.stdout and "uv " not in r.stdout

    # de novo: nada muda, nada é gerado (o cache pelo hash do pedido)
    r2 = rodar(PRODUCAO / "narrar.py", str(projeto), "--fornecedor", "espeak", "--sem-verificar")
    assert r2.returncode == 0 and "0 geração(ões)" in r2.stdout
    assert json.loads((projeto / "09-narracao" / "blocos.json").read_text())["blocos"][0]["pedido"] == b01["pedido"]


def test_narrar_hash_do_pedido_nao_muda():
    # o cache da narração dos projetos existentes depende desta fórmula
    import hashlib
    narrar = carregar(PRODUCAO / "narrar.py", "narrar_hash")
    partes = ("gemini", "gemini-3.8-flash-tts", "Charon", "perfil", "estilo", "Olá, você.")
    esperado = hashlib.sha1(json.dumps(partes, ensure_ascii=False).encode()).hexdigest()[:12]
    assert narrar.hash_pedido(*partes) == esperado


@precisa_espeak
def test_narrar_sem_verificacao_sai_com_1(projeto, monkeypatch, capsys):
    narrar = carregar(PRODUCAO / "narrar.py", "narrar_sem_verificacao")
    monkeypatch.setattr(narrar.tts.classe("espeak"), "deterministico", False)  # finge uma voz que precisa conferir
    chamadas = []

    def verificar(pasta, itens, modelo_whisper=None):
        chamadas.append(itens)
        return None  # o whisper não rodou
    monkeypatch.setattr(narrar, "verificar", verificar)
    monkeypatch.setattr(sys, "argv", ["narrar.py", str(projeto), "--fornecedor", "espeak"])
    assert narrar.main() == 1
    saida = capsys.readouterr()
    assert "sem a verificação por transcrição" in saida.err
    assert len(chamadas) == 1 and (projeto / "09-narracao" / "narracao.wav").exists()


def test_verificar_chama_o_alinhador_no_mesmo_python(projeto, monkeypatch):
    narrar = carregar(PRODUCAO / "narrar.py", "narrar_verificar")
    visto = {}

    def run(comando, **kw):
        visto["comando"], visto["env"] = comando, kw.get("env") or {}
        linhas = [json.dumps({"bloco": "B01", "ok": True}), json.dumps({"bloco": "B02-F02", "ok": False})]
        return subprocess.CompletedProcess(comando, 0, "\n".join(linhas) + "\n", "")
    monkeypatch.setattr(narrar.subprocess, "run", run)
    resultado = narrar.verificar(projeto, ["B01=a.wav", "B02-F02=b.wav"], "base")
    assert [r["ok"] for r in resultado] == [True, False]
    assert visto["comando"][:2] == [sys.executable, str(PRODUCAO / "alinhar.py")]
    assert visto["comando"][-2:] == ["--modelo", "base"] and str(RAIZ) in visto["env"]["PYTHONPATH"]
    monkeypatch.setattr(narrar.subprocess, "run",
                        lambda c, **kw: subprocess.CompletedProcess(c, 2, "", "ModuleNotFoundError: faster_whisper"))
    assert narrar.verificar(projeto, ["B01"]) is None


# ---------------------------------------------------------------- imagens

CENAS = {
    "proporcao": "16:9",
    "cenas": [
        {"id": "B01-01", "bloco": "B01", "tipo": "ilustracao", "descricao": "um gato", "prompt_final": "A cat on a roof.",
         "negativo_final": "blurry", "referencias": ["p1", "l1"]},
        {"id": "B01-02", "bloco": "B01", "tipo": "ilustracao", "descricao": "foto", "fonte": "internet", "busca": "gato",
         "prompt_final": "A photo of a cat."},
        {"id": "B02-01", "bloco": "B02", "tipo": "cartela", "descricao": "texto", "prompt_final": "Two cats.",
         "referencias": ["p1"]},
    ],
    "thumbnail": {"id": "thumbnail", "prompt_final": "A surprised cat.", "referencias": ["p1"]},
}
REFERENCIAS = [
    {"id": "p1", "tipo": "personagem", "nome": "Mimi", "proporcao": "1:1", "negativo": "text", "prompt": "Character reference sheet. Mimi."},
    {"id": "l1", "tipo": "local", "nome": "Telhado", "proporcao": "16:9", "prompt": "Environment concept art. Roof."},
]


class ProvedorFalso:
    id = "falso"
    nome = "Falso"

    def __init__(self, png: bytes, falhas: dict | None = None):
        self.png, self.falhas, self.pedidos = png, falhas or {}, []

    def gerar(self, pedido):
        self.pedidos.append(pedido)
        for trecho, erro in self.falhas.items():
            if trecho in pedido.texto:
                raise erro
        return ImagemGerada(self.png, "png", {"promptTokenCount": 1})


@pytest.fixture
def png_pequeno():
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg não instalado")
    return subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x36", "-frames:v", "1",
                           "-f", "image2pipe", "-vcodec", "png", "pipe:1"], capture_output=True, check=True).stdout


@pytest.fixture
def projeto_visual(projeto):
    (projeto / "08-cenas.json").write_text(json.dumps(CENAS), encoding="utf-8")
    (projeto / "08-referencias.jsonl").write_text("\n".join(json.dumps(r) for r in REFERENCIAS) + "\n", encoding="utf-8")
    return projeto


def gerar_com(monkeypatch, pasta: Path, provedor: ProvedorFalso, *args: str):
    modulo = carregar(PRODUCAO / "gerar_imagens.py", "gerar_imagens_teste")
    criados, custos = [], []
    monkeypatch.setattr(modulo.provedores_imagem, "criar", lambda p, m=None, **kw: criados.append((p, m)) or provedor)
    monkeypatch.setattr(modulo.custos, "registrar", lambda *a, **kw: custos.append((a, kw)))
    monkeypatch.setattr(sys, "argv", ["gerar_imagens.py", str(pasta), "--paralelo", "1", *args])
    return modulo, modulo.main(), criados, custos


def test_gerar_imagens_texto_e_hash_iguais_aos_de_antes(projeto_visual, monkeypatch, png_pequeno):
    falso = ProvedorFalso(png_pequeno)
    modulo, codigo, criados, custos = gerar_com(monkeypatch, projeto_visual, falso)
    assert codigo == 0 and criados == [("gemini", "gemini-3.1-flash-image")]
    textos = [p.texto for p in falso.pedidos]
    # fichas antes das cenas, cada uma com o texto de sempre
    assert textos[0] == ("Environment concept art. Roof.\n\nAvoid: text. Do not render any text, letters, labels or watermarks.")
    assert textos[1] == ("Character reference sheet. Mimi.\n\nAvoid: text. Do not render any text, letters, labels or watermarks.")
    cena = next(p for p in falso.pedidos if "A cat on a roof." in p.texto)
    assert cena.texto == (
        "Reference images are attached: image 1 is the character reference for Mimi; image 2 is the location reference "
        "for Telhado. Keep every character's face, hair, body and clothing exactly as in their reference, and keep the "
        "location consistent. Do not copy the reference layout (plain background, turnaround views): compose a new scene."
        "\n\nA cat on a roof.\n\nAvoid: blurry. Do not render any text, letters, captions, subtitles or watermarks.")
    assert len(cena.referencias) == 2 and cena.proporcao == "16:9" and cena.referencias[0].mime == "image/png"
    assert not any("A photo of a cat." in t for t in textos)  # a cena da internet fica com buscar_imagens.py
    manifesto = json.loads((projeto_visual / "09-imagens" / "manifesto.json").read_text())
    assert manifesto["B01-01"]["pedido"] == modulo.hash_pedido("gemini-3.1-flash-image", "A cat on a roof.", "blurry",
                                                               ["p1", "l1"], "16:9")
    assert manifesto["p1"]["pedido"] == modulo.hash_pedido("gemini-3.1-flash-image", "Character reference sheet. Mimi.", "text")
    assert "provedor" not in manifesto["B01-01"] and manifesto["thumbnail"]["status"] == "ok"
    assert {kw["item"] for _, kw in custos} == {"p1", "l1", "B01-01", "B02-01", "thumbnail"}
    assert all(kw["provedor"] == "gemini" and a[1] == "imagem" for a, kw in custos)
    assert (projeto_visual / "09-imagens" / "folha-de-contato.jpg").exists()

    # de novo, nada a gerar: o hash é o mesmo
    falso.pedidos.clear()
    _, codigo, _, _ = gerar_com(monkeypatch, projeto_visual, falso)
    assert codigo == 0 and falso.pedidos == []


def test_gerar_imagens_outro_provedor_muda_o_hash(projeto_visual, monkeypatch, png_pequeno):
    falso = ProvedorFalso(png_pequeno)
    modulo, codigo, criados, _ = gerar_com(monkeypatch, projeto_visual, falso, "--provedor", "openai", "--cenas", "B02-01")
    assert codigo == 0 and criados == [("openai", "gpt-image-1")]
    manifesto = json.loads((projeto_visual / "09-imagens" / "manifesto.json").read_text())
    assert manifesto["B02-01"]["provedor"] == "openai"
    assert manifesto["B02-01"]["pedido"] == modulo.hash_pedido("gpt-image-1", "Two cats.", None, ["p1"], "16:9", "openai", None)
    assert falso.pedidos[-1].qualidade is None


def test_gerar_imagens_bloqueado_vai_ao_manifesto(projeto_visual, monkeypatch, png_pequeno):
    falso = ProvedorFalso(png_pequeno, {"Two cats.": Bloqueado("filtro", "gemini")})
    _, codigo, _, _ = gerar_com(monkeypatch, projeto_visual, falso)
    assert codigo == 1
    registro = json.loads((projeto_visual / "09-imagens" / "manifesto.json").read_text())["B02-01"]
    assert registro["status"] == "bloqueado" and registro["pedido"] is None and "filtro" in registro["erro"]


def test_gerar_imagens_sem_cota(projeto_visual, monkeypatch, png_pequeno, capsys):
    falso = ProvedorFalso(png_pequeno, {"Environment": SemCota("limit: 0", "gemini")})
    _, codigo, _, _ = gerar_com(monkeypatch, projeto_visual, falso)
    assert codigo == 3 and "faturamento" in capsys.readouterr().out


def test_gerar_imagens_simular_nao_precisa_de_chave(projeto_visual, monkeypatch):
    modulo = carregar(PRODUCAO / "gerar_imagens.py", "gerar_imagens_simular")
    monkeypatch.setattr(modulo.provedores_imagem, "criar", lambda *a, **kw: pytest.fail("não devia criar o provedor"))
    monkeypatch.setattr(sys, "argv", ["gerar_imagens.py", str(projeto_visual), "--simular"])
    assert modulo.main() == 0


@precisa_ffmpeg
def test_gerar_imagens_provisorio_de_verdade(projeto_visual):
    r = rodar(PRODUCAO / "gerar_imagens.py", str(projeto_visual), "--provisorio")
    assert r.returncode == 0, r.stderr
    for cena in CENAS["cenas"]:
        assert (projeto_visual / "09-imagens" / "provisorio" / f"{cena['id']}.png").stat().st_size > 0


# ---------------------------------------------------------------- áudio da montagem


@pytest.mark.parametrize("voz,musica", [("/voz.wav", "/musica.mp3"), ("/voz.wav", None), (None, "/musica.mp3"), (None, None)])
def test_grafo_audio_cada_entrada_lida_pelo_seu_indice(voz, musica):
    """Cada arquivo é lido pelo índice dele e uma vez só. Com voz, música e efeitos, o 1º efeito lia o índice da
    música: a música inteira tocava de novo, crua, a partir do primeiro efeito, e a voz sumia por baixo."""
    montar = carregar(PRODUCAO / "montar_video.py", "montar_video_grafo")
    sons = [(Path("/whoosh.mp3"), 13.6, 6.0), (Path("/impacto.mp3"), 31.5, -5.0)]
    entradas, filtros = montar.grafo_audio(Path(voz) if voz else None, Path(musica) if musica else None, 0.0, 0.2, 60.0,
                                           efeitos_sonoros=sons)
    arquivos = [entradas[k + 1] for k, a in enumerate(entradas) if a == "-i"]  # o índice 0 é o vídeo
    lidos = {int(m.group(1)): linha for linha in filtros for m in [re.match(r"\[(\d+):a\]", linha)] if m}
    assert sorted(lidos) == list(range(1, len(arquivos) + 1))
    for indice, linha in lidos.items():
        papel = ("efeito" if "adelay" in linha else "musica" if "atrim=start" in linha else "voz")
        esperado = {"voz": voz, "musica": musica}.get(papel)
        if papel == "efeito":
            atraso = int(re.search(r"adelay=(\d+)", linha).group(1)) / 1000
            esperado = next(str(a) for a, t, _ in sons if t == atraso)
        assert arquivos[indice - 1] == esperado, f"[{indice}:a] ({papel}) lê {arquivos[indice - 1]}"
    assert filtros[-1].endswith("[a]")


# ---------------------------------------------------------------- personagem

FICHA = {"slug": "mimi", "nome": "Mimi", "referencia_en": "a small gray cat",
         "descricao_en": "a small gray cat with a red scarf and green eyes",
         "estilo": {"nome": "Cartum", "prompt_base": "flat cartoon, bold outlines", "negativo_base": "photo"},
         "fisico": {"tipo": "gata"}, "comportamento": {"personalidade": "curiosa"},
         "voz": {"voz": "Kore", "estilo": "animada"}}


def test_personagem_gera_registra_custo_e_aprova(tmp_path, monkeypatch, png_pequeno):
    monkeypatch.setenv("APP_RAIZ", str(tmp_path))
    personagem = carregar(RAIZ / "ferramentas" / "personagem.py", "personagem_teste")
    rascunho = tmp_path / "rascunhos" / "personagens" / "mimi"
    rascunho.mkdir(parents=True)
    (rascunho / "ficha.json").write_text(json.dumps(FICHA), encoding="utf-8")
    falso = ProvedorFalso(png_pequeno)
    criados, custos = [], []
    monkeypatch.setattr(personagem.provedores_imagem, "criar", lambda p, m=None, **kw: criados.append((p, m)) or falso)
    monkeypatch.setattr(personagem.custos, "registrar", lambda *a, **kw: custos.append((a, kw)))

    monkeypatch.setattr(sys, "argv", ["personagem.py", "gerar", str(rascunho)])
    assert personagem.main() == 0
    assert criados == [("gemini", "gemini-3.1-flash-image")]
    esperado = (personagem.prompt_ficha_personagem("a small gray cat with a red scarf and green eyes", "flat cartoon, bold outlines")
                + "\n\nAvoid: photo. Do not render any text, letters, labels or watermarks.")
    assert falso.pedidos[0].texto == esperado and falso.pedidos[0].proporcao == "1:1" and falso.pedidos[0].referencias == ()
    assert custos[0][0][0] == rascunho and custos[0][0][1] == "imagem" and custos[0][1]["provedor"] == "gemini"
    assert (rascunho / "v1.png").exists() and json.loads((rascunho / "v1.json").read_text())["modelo"] == "gemini-3.1-flash-image"

    monkeypatch.setattr(sys, "argv", ["personagem.py", "gerar", str(rascunho), "--base", "v1", "--ajuste", "bigger eyes"])
    assert personagem.main() == 0
    ajuste = falso.pedidos[1]
    assert ajuste.texto.startswith("The attached image is the current version of this character.")
    assert "Adjustment: bigger eyes.\n\n" + esperado in ajuste.texto and len(ajuste.referencias) == 1

    monkeypatch.setattr(sys, "argv", ["personagem.py", "aprovar", str(rascunho), "--versao", "v2"])
    assert personagem.main() == 0
    destino = tmp_path / "biblioteca" / "personagens" / "mimi"
    assert (destino / "imagem.png").exists() and (destino / "fisico.md").exists()
    assert "Voz (Gemini TTS):** Kore" in (destino / "comportamento.md").read_text()


def test_personagem_bloqueado_sai_com_1(tmp_path, monkeypatch, png_pequeno):
    monkeypatch.setenv("APP_RAIZ", str(tmp_path))
    personagem = carregar(RAIZ / "ferramentas" / "personagem.py", "personagem_bloqueado")
    rascunho = tmp_path / "rascunhos" / "personagens" / "mimi"
    rascunho.mkdir(parents=True)
    (rascunho / "ficha.json").write_text(json.dumps(FICHA), encoding="utf-8")
    monkeypatch.setattr(personagem.provedores_imagem, "criar",
                        lambda *a, **kw: ProvedorFalso(png_pequeno, {"cat": Bloqueado("filtro")}))
    monkeypatch.setattr(sys, "argv", ["personagem.py", "gerar", str(rascunho), "--provedor", "openai"])
    assert personagem.main() == 1 and not (rascunho / "v1.png").exists()
