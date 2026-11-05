"""Animação das imagens (visual.animacao): o paralelismo das chamadas (nucleo.paralelo), a escolha das cenas e o texto
do pedido (ferramentas/animacao.py), o script que anima com um provedor falso (animar_imagens.py), o adaptador do
Gemini Omni Flash sem rede, a montagem de verdade com um clipe (ffmpeg), o custo por segundo, a configuração e a
confirmação antes de gastar."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from nucleo import RAIZ, paralelo
from nucleo.provedores.erros import Bloqueado, ErroProvedor

sys.path.insert(0, str(RAIZ / "ferramentas"))
import animacao  # noqa: E402

PRODUCAO = RAIZ / "ferramentas" / "producao"
precisa_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg não instalado")


def carregar(caminho: Path, nome: str):
    spec = importlib.util.spec_from_file_location(nome, caminho)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nome] = modulo
    spec.loader.exec_module(modulo)
    return modulo


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def png(caminho: Path, cor: str = "blue", tamanho: str = "72x128") -> Path:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", f"color=c={cor}:s={tamanho}", "-frames:v", "1", str(caminho))
    return caminho


def mp4(caminho: Path, segundos: float = 1.5, tamanho: str = "72x128") -> Path:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", f"testsrc=s={tamanho}:r=24:d={segundos}", "-pix_fmt", "yuv420p", str(caminho))
    return caminho


CENAS = [
    {"id": "B01-01", "bloco": "B01", "tipo": "ilustracao", "prompt_cena": "a cat on a roof", "movimento": "zoom-in",
     "duracao_s": 4.2, "inicio_s": 0.0, "trecho": "Você já viu um gato voar?", "transicao": "corte"},
    {"id": "B01-02", "bloco": "B01", "tipo": "cartela", "texto_na_tela": "NÍVEL 1", "duracao_s": 2.0, "inicio_s": 4.2,
     "trecho": "Nível um.", "transicao": "corte"},
    {"id": "B02-01", "bloco": "B02", "tipo": "ilustracao", "fonte": "internet", "prompt_cena": "a photo", "duracao_s": 3.0,
     "inicio_s": 6.2, "trecho": "Uma foto antiga.", "transicao": "corte"},
    {"id": "B02-02", "bloco": "B02", "tipo": "ilustracao", "prompt_cena": "two cats", "animacao": "the cats' tails sway",
     "movimento": "pan-direita", "duracao_s": 11.0, "inicio_s": 9.2, "trecho": "Dois gatos.", "transicao": "corte"},
]


# ---------------------------------------------------------------- nucleo.paralelo


def test_paralelo_respeita_o_limite_e_as_dependencias():
    abertas, maximo, ordem, trava = [0], [0], [], threading.Lock()

    def tarefa(nome, espera=0.05):
        def f():
            with trava:
                abertas[0] += 1
                maximo[0] = max(maximo[0], abertas[0])
            time.sleep(espera)
            with trava:
                abertas[0] -= 1
                ordem.append(nome)
            return nome
        return f

    terminadas = []
    tarefas = [paralelo.Tarefa("ficha", tarefa("ficha", 0.15))]
    tarefas += [paralelo.Tarefa(f"c{i}", tarefa(f"c{i}")) for i in range(6)]
    tarefas.append(paralelo.Tarefa("depende", tarefa("depende"), ("ficha",)))
    inicio = time.monotonic()
    r = paralelo.rodar(tarefas, 3, lambda id_, res: terminadas.append(id_))
    assert r["depende"] == "depende" and len(r) == 8 and sorted(terminadas) == sorted(r)
    assert maximo[0] == 3  # nunca mais de 3 abertas
    assert ordem.index("depende") > ordem.index("ficha")
    assert time.monotonic() - inicio < 0.6  # em série seriam 0,15 + 7 × 0,05 = 0,5 s; em paralelo, bem menos


def test_paralelo_com_limite_1_vai_na_ordem_e_levanta_o_erro_no_fim():
    ordem = []
    assert list(paralelo.rodar([paralelo.Tarefa(str(i), lambda i=i: ordem.append(i) or i) for i in range(5)], 1)) == \
        ["0", "1", "2", "3", "4"]
    assert ordem == [0, 1, 2, 3, 4]

    feitas = []
    tarefas = [paralelo.Tarefa("ruim", lambda: 1 / 0), paralelo.Tarefa("boa", lambda: feitas.append("boa")),
               paralelo.Tarefa("depois", lambda: feitas.append("depois"), ("ruim",))]
    with pytest.raises(ZeroDivisionError):
        paralelo.rodar(tarefas, 2)
    assert sorted(feitas) == ["boa", "depois"]  # a falha de uma não cancela as outras, nem a que dependia dela
    assert paralelo.rodar([], 4) == {}


# ---------------------------------------------------------------- ferramentas/animacao.py


def test_quais_cenas_animam():
    m = {"B02-01": {"origem": "internet"}}
    assert animacao.selecionadas(CENAS, "nenhuma", m) == []
    assert [c["id"] for c in animacao.selecionadas(CENAS, "todas", m)] == ["B01-01", "B02-02"]  # sem cartela e internet
    # a busca falhou e a imagem foi gerada: a cena da internet passa a animar
    assert [c["id"] for c in animacao.selecionadas(CENAS, "todas", {"B02-01": {"origem": "ia"}})] == ["B01-01", "B02-01", "B02-02"]
    parada = [{**c, "animar": False} if c["id"] == "B01-01" else c for c in CENAS]
    assert [c["id"] for c in animacao.selecionadas(parada, "todas", m)] == ["B02-02"]
    marcadas = [{**c, "animar": True} if c["id"] == "B02-02" else c for c in CENAS]
    assert [c["id"] for c in animacao.selecionadas(marcadas, "destaques", m)] == ["B02-02"]
    # destaques sem marca: a primeira ilustração de cada bloco e a última do vídeo
    assert [c["id"] for c in animacao.selecionadas(CENAS, "destaques", m)] == ["B01-01", "B02-02"]
    assert animacao.modo({"visual": {"animacao": "todas"}}) == "todas" and animacao.modo({}) == "nenhuma"
    assert animacao.modo({"visual": {"animacao": "inventado"}}) == "nenhuma"


def test_duracoes_segundos_e_texto_do_pedido(tmp_path):
    assert animacao.duracoes(tmp_path, CENAS)["B02-02"] == 11.0  # sem narração: a estimada
    tempos = {"duracao_video_s": 20.0, "cenas": [{"id": c["id"], "inicio_s": i * 5.0} for i, c in enumerate(CENAS)]}
    (tmp_path / "09-narracao").mkdir()
    (tmp_path / "09-narracao" / "tempos.json").write_text(json.dumps(tempos))
    assert animacao.duracoes(tmp_path, CENAS) == {"B01-01": 5.0, "B01-02": 5.0, "B02-01": 5.0, "B02-02": 5.0}
    assert [animacao.segundos_pedidos(s) for s in (1.2, 4.0, 4.04, 4.2, 11.0)] == [3, 4, 4, 5, 10]

    texto = animacao.prompt(CENAS[3], 6)
    assert "6-second" in texto and "the cats' tails sway" in texto and "exact first frame" in texto
    assert "No dialogue, no music and no sound effects." in texto
    padrao = animacao.movimento(CENAS[0])  # sem "animacao": movimento leve, com a câmera do campo "movimento"
    assert "a cat on a roof" in padrao and "push-in" in padrao


def test_clipe_so_vale_com_a_mesma_imagem(tmp_path):
    imagem = tmp_path / "09-imagens" / "B01-01.png"
    imagem.parent.mkdir()
    imagem.write_bytes(b"png-1")
    (tmp_path / "09-animacoes").mkdir()
    (tmp_path / "09-animacoes" / "B01-01.mp4").write_bytes(b"mp4")
    m = {"B01-01": {"status": "ok", "arquivo": "09-animacoes/B01-01.mp4", "imagem": animacao.sha_arquivo(imagem),
                    "duracao_s": 5.1}}
    assert animacao.clipe_valido(tmp_path, "B01-01", m, imagem) == (tmp_path / "09-animacoes" / "B01-01.mp4", 5.1, False)
    # o que termina numa transição vale só com a imagem da cena seguinte de quando foi feito
    proxima = tmp_path / "09-imagens" / "B01-02.png"
    proxima.write_bytes(b"png-seguinte")
    com_transicao = {"B01-01": {**m["B01-01"], "ultimo_quadro": animacao.sha_arquivo(proxima)}}
    assert animacao.clipe_valido(tmp_path, "B01-01", com_transicao, imagem, proxima)[2] is True
    assert animacao.clipe_valido(tmp_path, "B01-01", com_transicao, imagem) is None
    proxima.write_bytes(b"png-seguinte-refeita")
    assert animacao.clipe_valido(tmp_path, "B01-01", com_transicao, imagem, proxima) is None
    imagem.write_bytes(b"png-2")  # a cena foi refeita depois da animação
    assert animacao.clipe_valido(tmp_path, "B01-01", m, imagem) is None
    assert animacao.clipe_valido(tmp_path, "B01-01", {"B01-01": {**m["B01-01"], "status": "bloqueado"}}, imagem) is None


def test_transicoes_no_texto_no_hash_e_nos_cortes():
    texto = animacao.prompt(CENAS[0], 5, "whip-pan")
    assert "transition into the second image, the exact last frame, with a fast whip pan" in texto
    assert "no scene cuts" not in texto and "No dialogue" in texto
    assert all(t not in animacao.prompt(CENAS[0], 5) for t in ("second image", "last frame"))
    # sem transição, o hash é o de antes dela existir: os clipes já pagos continuam valendo
    import hashlib
    antigo = hashlib.sha1(json.dumps(["m", "720p", "9:16", "abc", "mov"]).encode()).hexdigest()[:12]
    assert animacao.hash_pedido("m", "720p", "9:16", "abc", "mov") == antigo
    assert animacao.hash_pedido("m", "720p", "9:16", "abc", "mov", "push", "def") != antigo
    cenas = [{"id": "A"}, {"id": "B", "transicao": "whip-pan"}, {"id": "C", "transicao": "j-cut"}, {"id": "D", "transicao": "fade"}]
    assert [animacao.transicao_de_saida(cenas, k) for k in range(4)] == ["whip-pan", None, None, None]
    cenas += [{"id": "E", "transicao": "l-cut"}]
    # j-cut: a imagem troca 0,5 s depois; l-cut: 0,5 s antes, sem passar de um terço da cena mais curta
    assert animacao.deslocar_cortes(cenas, [0.0, 4.0, 8.0, 12.0, 13.2], 20.0) == pytest.approx([0.0, 4.0, 8.5, 12.0, 12.8])


ROTEIRO = """---
projeto: gato
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

Você já viu um gato voar? Hoje você vai ver o gato voar de verdade, lá do alto do telhado.

## [B02] Conversa
> função: desenvolvimento · abre: — · fecha: L1 · conector: mas · cortável: não

O gato olhou para o céu azul e pulou bem alto, sem medo nenhum de cair no chão.
"""


def test_cenas_confere_os_campos_da_animacao(tmp_path):
    pasta = tmp_path / "projetos" / "gato"
    (pasta / "08-cenas").mkdir(parents=True)
    (pasta / "06-roteiro-final.md").write_text(ROTEIRO, encoding="utf-8")
    (pasta / "00-brief.json").write_text(json.dumps({"visual": {"animacao": "destaques"}}))
    (pasta / "08-biblia-visual.json").write_text(json.dumps({"projeto": "gato", "proporcao": "9:16",
                                                             "estilo": {"prompt_base": "watercolor"}}))

    def cena(trecho, enq, **extra):
        return {"trecho": trecho, "tipo": "ilustracao", "descricao": "um gato", "enquadramento": enq,
                "prompt_cena": "a cat on a roof", "movimento": "zoom-in", "transicao": "corte", **extra}

    def montar(primeira: dict):
        (pasta / "08-cenas" / "B01.json").write_text(json.dumps([
            cena("Você já viu um gato voar?", "plano-geral", **primeira),
            cena("Hoje você vai ver o gato voar de verdade, lá do alto do telhado.", "close",
                 animacao=" ".join(["sway"] * 70), transicao="slide")]))
        (pasta / "08-cenas" / "B02.json").write_text(json.dumps([
            cena("O gato olhou para o céu azul e pulou bem alto, sem medo nenhum de cair no chão.", "plano-medio",
                 transicao="whip-pan")]))
        return subprocess.run([sys.executable, str(RAIZ / "ferramentas" / "cenas.py"), "montar", str(pasta)],
                              capture_output=True, text=True, cwd=RAIZ, env={**os.environ, "APP_RAIZ": str(tmp_path)})

    r = montar({"animar": "sim"})
    assert r.returncode == 1
    assert "erro [B01-01] animar é true ou false" in r.stdout
    assert "aviso [B01-02] animacao com 70 palavras" in r.stdout
    assert 'nenhuma cena tem "animar": true' in r.stdout and "animação (destaques): 2 cena(s)" in r.stdout
    assert "aviso [B01-02] transição 'slide' desconhecida: vira corte" in r.stdout

    r = montar({"animar": True})
    assert r.returncode == 0, r.stdout
    assert "animação (destaques): 1 cena(s) (B01-01)" in r.stdout
    # o whip-pan da B02-01 seria gerado no clipe da B01-02, que não anima
    assert "aviso [B02-01] a transição whip-pan é gerada no clipe da cena anterior (B01-02), que não anima" in r.stdout
    storyboard = (pasta / "08-storyboard.md").read_text(encoding="utf-8")
    assert "**Animação:** gentle, natural ambient motion in the scene (a cat on a roof)" in storyboard
    assert "(marcada para animar)" in storyboard
    assert json.loads((pasta / "08-cenas.json").read_text())["cenas"][0]["animar"] is True


# ---------------------------------------------------------------- animar_imagens.py com um provedor falso


class VideoFalso:
    def __init__(self, clipe: bytes, falhas: dict | None = None):
        self.clipe, self.falhas, self.pedidos = clipe, falhas or {}, []

    def animar(self, pedido):
        self.pedidos.append(pedido)
        for trecho, erro in self.falhas.items():
            if trecho in pedido.texto:
                raise erro
        from nucleo.provedores.video.base import VideoGerado
        return VideoGerado(self.clipe, {"teste": 1})


@pytest.fixture
def projeto(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg não instalado")
    monkeypatch.setenv("APP_RAIZ", str(tmp_path))
    pasta = tmp_path / "projetos" / "gato"
    pasta.mkdir(parents=True)
    (pasta / "00-brief.json").write_text(json.dumps({"visual": {"animacao": "todas"}}))
    (pasta / "08-cenas.json").write_text(json.dumps({"proporcao": "9:16", "cenas": CENAS}))
    png(pasta / "09-imagens" / "B01-01.png")
    png(pasta / "09-imagens" / "B02-02.png", "red")
    (pasta / "09-imagens" / "manifesto.json").write_text(json.dumps({"B02-01": {"origem": "internet"}}))
    return pasta


def animar_com(monkeypatch, pasta: Path, provedor, *args: str):
    modulo = carregar(PRODUCAO / "animar_imagens.py", "animar_imagens_teste")
    criados, custos = [], []
    monkeypatch.setattr(modulo.provedores_video, "criar", lambda p, m=None, **kw: criados.append((p, m)) or provedor)
    monkeypatch.setattr(modulo.custos, "registrar", lambda *a, **kw: custos.append((a, kw)))
    monkeypatch.setattr(sys, "argv", ["animar_imagens.py", str(pasta), *args])
    return modulo.main(), criados, custos


def test_anima_as_cenas_pula_as_prontas_e_refaz_a_que_mudou(projeto, monkeypatch, tmp_path, capsys):
    falso = VideoFalso(mp4(tmp_path / "clipe.mp4", 1.5).read_bytes())
    codigo, criados, custos = animar_com(monkeypatch, projeto, falso, "--paralelo", "2")
    assert codigo == 0 and criados == [("gemini", "gemini-omni-1.1-flash")]
    assert sorted(p.imagem.mime for p in falso.pedidos) == ["image/png", "image/png"]
    por_cena = {("cats" in p.texto): p for p in falso.pedidos}
    assert por_cena[True].segundos == 10 and por_cena[False].segundos == 5  # 11 s → o máximo; 4,2 s → 5
    assert all(p.proporcao == "9:16" and p.resolucao == "720p" for p in falso.pedidos)
    m = json.loads((projeto / "09-animacoes" / "manifesto.json").read_text())
    assert set(m) == {"B01-01", "B02-02"} and m["B01-01"]["status"] == "ok"
    assert m["B01-01"]["imagem"] == animacao.sha_arquivo(projeto / "09-imagens" / "B01-01.png")
    assert 1.4 < m["B01-01"]["duracao_s"] < 1.6 and (projeto / "09-animacoes" / "B01-01.mp4").stat().st_size > 0
    assert {kw["item"] for _, kw in custos} == {"B01-01", "B02-02"}
    assert all(a[1] == "animacao" and kw["tamanho"] == "720p" and 1.4 < kw["segundos"] < 1.6 for a, kw in custos)

    # o clipe de 1,5 s não cobre a cena de 4,2 s: as duas cenas são pedidas de novo; com clipes longos, nada a fazer
    falso.clipe = mp4(tmp_path / "longo.mp4", 10).read_bytes()
    falso.pedidos.clear()
    assert animar_com(monkeypatch, projeto, falso)[0] == 0 and len(falso.pedidos) == 2
    falso.pedidos.clear()
    assert animar_com(monkeypatch, projeto, falso)[0] == 0 and falso.pedidos == []
    png(projeto / "09-imagens" / "B02-02.png", "green")  # a cena foi refeita: o clipe dela é de outra imagem
    assert animar_com(monkeypatch, projeto, falso)[0] == 0 and [("cats" in p.texto) for p in falso.pedidos] == [True]
    assert "animadas: 1" in capsys.readouterr().out


def test_transicao_de_video_termina_na_imagem_da_cena_seguinte(projeto, monkeypatch, tmp_path, capsys):
    cenas = [dict(c) for c in CENAS]
    cenas[1]["transicao"] = "whip-pan"  # a cartela B01-02 entra com um whip pan, gerado no fim do clipe de B01-01
    (projeto / "08-cenas.json").write_text(json.dumps({"proporcao": "9:16", "cenas": cenas}))
    falso = VideoFalso(mp4(tmp_path / "clipe.mp4", 10).read_bytes())
    assert animar_com(monkeypatch, projeto, falso)[0] == 0
    sem_imagem = next(p for p in falso.pedidos if "cat on a roof" in p.texto)
    assert sem_imagem.ultimo_quadro is None and "whip pan" not in sem_imagem.texto  # a cartela ainda não tem imagem
    assert "a transição whip-pan vira corte" in capsys.readouterr().out

    png(projeto / "09-imagens" / "B01-02.png", "yellow")
    falso.pedidos.clear()
    assert animar_com(monkeypatch, projeto, falso)[0] == 0
    [pedido] = falso.pedidos  # só a B01-01 muda; a B02-02 continua valendo
    assert "whip pan" in pedido.texto and pedido.ultimo_quadro.dados == (projeto / "09-imagens" / "B01-02.png").read_bytes()
    m = json.loads((projeto / "09-animacoes" / "manifesto.json").read_text())["B01-01"]
    assert m["transicao"] == "whip-pan" and m["ultimo_quadro"] == animacao.sha_arquivo(projeto / "09-imagens" / "B01-02.png")

    png(projeto / "09-imagens" / "B01-02.png", "white")  # a imagem de chegada mudou: o clipe é feito de novo
    falso.pedidos.clear()
    assert animar_com(monkeypatch, projeto, falso)[0] == 0 and len(falso.pedidos) == 1


def test_recusa_do_filtro_fica_registrada_e_nao_se_repete(projeto, monkeypatch, tmp_path):
    falso = VideoFalso(mp4(tmp_path / "clipe.mp4", 6).read_bytes(), {"cats": Bloqueado("filtro", "gemini")})
    assert animar_com(monkeypatch, projeto, falso)[0] == 1
    m = json.loads((projeto / "09-animacoes" / "manifesto.json").read_text())
    assert m["B02-02"]["status"] == "bloqueado" and m["B02-02"]["pedido"] and m["B01-01"]["status"] == "ok"
    falso.pedidos.clear()
    assert animar_com(monkeypatch, projeto, falso)[0] == 0 and falso.pedidos == []  # o mesmo pedido não é repetido


def test_simular_proporcao_e_modo(projeto, monkeypatch, capsys):
    modulo = carregar(PRODUCAO / "animar_imagens.py", "animar_imagens_simular")
    monkeypatch.setattr(modulo.provedores_video, "criar", lambda *a, **kw: pytest.fail("não devia criar o provedor"))
    (projeto / "09-imagens" / "B02-02.png").unlink()  # a simulação conta também a cena que ainda vai ter imagem
    monkeypatch.setattr(sys, "argv", ["animar_imagens.py", str(projeto), "--simular"])
    assert modulo.main() == 0
    saida = capsys.readouterr().out
    assert "a animar: 2 cena(s) · 15 s de vídeo · estimado: US$ 1.52" in saida
    monkeypatch.setattr(sys, "argv", ["animar_imagens.py", str(projeto), "--simular", "--resolucao", "1080p"])
    assert modulo.main() == 0 and "sem preço em biblioteca/precos.json para 1080p" in capsys.readouterr().out

    (projeto / "08-cenas.json").write_text(json.dumps({"proporcao": "1:1", "cenas": CENAS}))
    monkeypatch.setattr(sys, "argv", ["animar_imagens.py", str(projeto)])
    assert modulo.main() == 0 and "só existe em 16:9 e 9:16" in capsys.readouterr().out
    (projeto / "00-brief.json").write_text(json.dumps({"visual": {"animacao": "nenhuma"}}))
    assert modulo.main() == 0 and "a animar: 0 cena(s)" in capsys.readouterr().out


# ---------------------------------------------------------------- adaptador do Gemini Omni Flash, sem rede


def test_gemini_omni_pedido_e_entrega_por_uri(monkeypatch):
    from nucleo.provedores.imagem.base import Referencia
    from nucleo.provedores.video import gemini
    from nucleo.provedores.video.base import PedidoVideo

    chamadas = []
    estados = iter(["PROCESSING", "ACTIVE"])

    def pedir(url, corpo, cabecalhos, *, metodo="POST", **kw):
        chamadas.append((metodo, url, json.loads(corpo) if corpo else None, cabecalhos))
        if url.endswith("interactions"):
            return json.dumps({"id": "int-1", "status": "completed", "steps": [{"type": "user_input"}, {"type": "model_output", "content": [
                {"type": "video", "mime_type": "video/mp4", "uri": "https://generativelanguage.googleapis.com/v1beta/files/abc"}]}],
                "usage": {"total_output_tokens": 34752}}).encode(), {}
        if url.endswith(":download?alt=media"):
            return b"MP4", {}
        return b"{}", {}

    monkeypatch.setattr(gemini.http, "pedir", pedir)
    monkeypatch.setattr(gemini.http, "pedir_json", lambda url, corpo, cab, *, metodo="POST", **kw:
                        chamadas.append((metodo, url, None, cab)) or {"state": next(estados)})
    monkeypatch.setattr(gemini, "ESPERA_ARQUIVO_S", 0)
    usos = []
    v = gemini.GeminiVideo(chave="chave-falsa", ao_usar=usos.append)
    r = v.animar(PedidoVideo("Animate.", Referencia(b"PNG", "image/png"), "9:16", "720p", 6))
    assert r.dados == b"MP4" and r.uso == {"total_output_tokens": 34752}
    corpo = chamadas[0][2]
    assert corpo["model"] == "gemini-omni-1.1-flash" and corpo["input"][0] == {
        "type": "image", "data": base64.b64encode(b"PNG").decode(), "mime_type": "image/png"}
    assert corpo["input"][1] == {"type": "text", "text": "Animate."}
    assert corpo["generation_config"] == {"video_config": {"task": "image_to_video"}}
    assert corpo["response_format"] == {"type": "video", "aspect_ratio": "9:16", "resolution": "720p", "delivery": "uri"}
    # a entrega por URI exige store=true (HTTP 400 sem ele); a interação guardada é apagada depois do download
    assert corpo["store"] is True and chamadas[0][3]["x-goog-api-key"] == "chave-falsa"
    metodos = [(m, u.split("v1beta/", 1)[-1]) for m, u, _, _ in chamadas[1:]]
    assert metodos == [("GET", "files/abc"), ("GET", "files/abc"), ("GET", "files/abc:download?alt=media"),
                       ("DELETE", "files/abc"), ("DELETE", "interactions/int-1")]
    assert usos[0].servico == "animacao" and usos[0].tamanho == "720p" and usos[0].segundos == 6


def test_gemini_omni_video_embutido_filtro_e_proporcao(monkeypatch):
    from nucleo.provedores.imagem.base import Referencia
    from nucleo.provedores.video import gemini
    from nucleo.provedores.video.base import PedidoVideo

    pedido = PedidoVideo("Animate.", Referencia(b"PNG", "image/png"), "16:9")
    respostas = iter([
        json.dumps({"output_video": {"mime_type": "video/mp4", "data": base64.b64encode(b"MP4").decode()}}).encode(),
        json.dumps({"status": "failed", "error": {"message": "Blocked by the safety filter"}}).encode(),
        json.dumps({"status": "completed", "steps": []}).encode(),
    ])
    monkeypatch.setattr(gemini.http, "pedir", lambda *a, **kw: (next(respostas), {}))
    v = gemini.GeminiVideo(chave="x")
    assert v.animar(pedido).dados == b"MP4"
    with pytest.raises(Bloqueado):
        v.animar(pedido)
    with pytest.raises(ErroProvedor, match="nenhum vídeo"):
        v.animar(pedido)
    with pytest.raises(ErroProvedor, match="só gera vídeo em 16:9 e 9:16"):
        v.animar(PedidoVideo("x", Referencia(b"PNG", "image/png"), "1:1"))
    # com o último quadro: primeiro e último quadro em ordem, e sem a tarefa image_to_video (é uma interpolação)
    corpo = v.corpo(PedidoVideo("x", Referencia(b"A", "image/png"), "9:16", ultimo_quadro=Referencia(b"B", "image/jpeg")))
    assert [i.get("data") for i in corpo["input"]] == [base64.b64encode(b"A").decode(), base64.b64encode(b"B").decode(), None]
    assert corpo["input"][2] == {"type": "text", "text": "x"} and "generation_config" not in corpo

    def recusa(*a, **kw):
        raise ErroProvedor("HTTP 400: the prompt violates the usage policy", "gemini", 400)
    monkeypatch.setattr(gemini.http, "pedir", recusa)
    with pytest.raises(Bloqueado):
        v.animar(pedido)
    assert gemini.id_do_arquivo("files/abc") == gemini.id_do_arquivo("https://x/v1beta/files/abc:download?alt=media") == "abc"


# ---------------------------------------------------------------- montagem de verdade com um clipe


def test_cadeia_do_clipe_animado():
    montar = carregar(PRODUCAO / "montar_video.py", "montar_video_animado")
    assert montar.cadeia_do_clipe_animado(6.0, 5.0, 1080, 1920, 30) == \
        "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30"  # sobra: só corta
    lento = montar.cadeia_do_clipe_animado(4.0, 4.5, 1080, 1920, 30)
    assert lento.startswith("setpts=1.1250*PTS,") and "tpad" not in lento  # falta pouco: anda mais devagar
    parado = montar.cadeia_do_clipe_animado(2.0, 4.0, 1080, 1920, 30)
    assert parado.startswith("setpts=1.2500*PTS,") and parado.endswith("tpad=stop_mode=clone:stop_duration=1.500")
    # termina numa transição: acaba no corte; o que sobra anda até 25% mais rápido e o resto sai do começo
    assert montar.cadeia_do_clipe_animado(6.0, 4.0, 1080, 1920, 30, True).startswith(
        "setpts=0.8000*PTS,trim=start=0.800,setpts=PTS-STARTPTS,scale=")
    assert montar.cadeia_do_clipe_animado(4.4, 4.0, 1080, 1920, 30, True).startswith("setpts=0.9091*PTS,scale=")


@precisa_ffmpeg
def test_montagem_usa_o_clipe_no_lugar_do_zoom(tmp_path):
    pasta = tmp_path / "projetos" / "gato"
    cenas = [{"id": "B01-01", "bloco": "B01", "tipo": "ilustracao", "inicio_s": 0.0, "duracao_s": 2.0, "trecho": "Um gato.",
              "movimento": "zoom-in", "transicao": "corte"},
             {"id": "B01-02", "bloco": "B01", "tipo": "ilustracao", "inicio_s": 2.0, "duracao_s": 2.0, "trecho": "Outro gato.",
              "movimento": "zoom-out", "transicao": "zoom-blur"},
             {"id": "B01-03", "bloco": "B01", "tipo": "ilustracao", "inicio_s": 4.0, "duracao_s": 2.0, "trecho": "Mais um.",
              "movimento": "zoom-in", "transicao": "j-cut"}]
    pasta.mkdir(parents=True)
    (pasta / "08-cenas.json").write_text(json.dumps({"proporcao": "9:16", "cenas": cenas}))
    (pasta / "00-brief.json").write_text(json.dumps({"visual": {"animacao": "todas"}, "narracao": {"ativa": False},
                                                     "legendas": {"ativas": False}, "musica": {"ativa": False}}))
    imagem = png(pasta / "09-imagens" / "B01-01.png")
    seguinte = png(pasta / "09-imagens" / "B01-02.png", "red")
    png(pasta / "09-imagens" / "B01-03.png", "yellow")
    mp4(pasta / "09-animacoes" / "B01-01.mp4", 2.4)  # termina no zoom-blur para a B01-02: acaba no corte, aos 2 s
    (pasta / "09-animacoes" / "manifesto.json").write_text(json.dumps({"B01-01": {
        "status": "ok", "arquivo": "09-animacoes/B01-01.mp4", "imagem": animacao.sha_arquivo(imagem), "duracao_s": 2.4,
        "transicao": "zoom-blur", "ultimo_quadro": animacao.sha_arquivo(seguinte)}}))
    env = {**os.environ, "PYTHONPATH": str(RAIZ), "APP_RAIZ": str(tmp_path)}

    def montar(*extra):
        return subprocess.run([sys.executable, str(PRODUCAO / "montar_video.py"), str(pasta), "--fps", "10", *extra],
                              capture_output=True, text=True, cwd=RAIZ, env=env, timeout=300)

    r = montar()
    assert r.returncode == 0, r.stderr
    assert "animação: 1 de 3 cena(s) animada(s)" in r.stdout
    video = pasta / "10-video" / "gato.mp4"
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                               capture_output=True, text=True).stdout)
    assert abs(dur - 6.0) < 0.15  # o clipe de 2,4 s coube na cena de 2 s, e o j-cut não muda o tempo do vídeo
    assert (pasta / "10-video" / "base" / "video.mp4").exists()

    png(pasta / "09-imagens" / "B01-02.png", "blue")  # a imagem de chegada mudou: o clipe da transição deixa de valer
    r = montar()
    assert r.returncode == 0 and "animação:" not in r.stdout and "B01-01" in r.stderr
    png(pasta / "09-imagens" / "B01-02.png", "red")
    png(pasta / "09-imagens" / "B01-01.png", "green")  # a imagem mudou depois da animação: a cena volta a ficar parada
    r = montar()
    assert r.returncode == 0 and "animação:" not in r.stdout and "ficaram com a imagem parada" in r.stderr
    assert "animação:" not in montar("--sem-animacao").stdout


def test_guia_da_animacao_so_vai_quando_o_pedido_anima():
    from nucleo.pipeline import fluxos
    assert fluxos._nota_de_animacao({"visual": {"animacao": "todas"}}) == "\nAnimação das imagens (todas): leia biblioteca/animacao.md"
    assert fluxos._nota_de_animacao({"visual": {"animacao": "nenhuma"}}) == fluxos._nota_de_animacao({}) == ""
    guia = (RAIZ / "biblioteca" / "animacao.md").read_text(encoding="utf-8")
    assert all(f"`{t}`" in guia for t in animacao.TRANSICOES if t not in ("corte", "fade"))  # o guia cobre todas
    sempre = (RAIZ / "agentes" / "diretor-visual.md").read_text(encoding="utf-8") + \
        (RAIZ / "biblioteca" / "formato-cenas.md").read_text(encoding="utf-8")
    assert "whip-pan" not in sempre  # as transições não pesam no contexto de quem não anima


# ---------------------------------------------------------------- custo, configuração e confirmação


def test_custo_por_segundo_de_video():
    import custos
    linha = {"servico": "animacao", "modelo": "gemini-omni-1.1-flash", "quando": "2026-10-23T12:00:00", "segundos": 10,
             "tamanho": "720p", "uso": {"qualquer": 1}}
    assert custos.custo(linha, custos.tabela()) == pytest.approx(5792 * 10 * 17.5 / 1_000_000)
    assert custos.custo({**linha, "tamanho": "1080p"}, custos.tabela()) is None  # sem a conta publicada: sem preço
    assert custos.rotulo("animacao", "gemini") == "Animação (Gemini)"


def test_configuracao_da_animacao_e_das_imagens(ambiente):
    from nucleo import rotas
    assert rotas.animacao() == {"provedor": "gemini", "modelo": "gemini-omni-1.1-flash", "resolucao": "720p", "paralelo": 4}
    assert rotas.paralelo_imagem() == 6
    rotas.gravar({"animacao": {"resolucao": "1080P", "paralelo": 2}, "imagem": {"paralelo": 10}})
    assert rotas.animacao()["resolucao"] == "1080p" and rotas.animacao()["paralelo"] == 2 and rotas.paralelo_imagem() == 10
    rotas.gravar({"animacao": {"resolucao": None}})
    assert rotas.animacao()["resolucao"] == "720p"
    for ruim in ({"animacao": {"resolucao": "8k"}}, {"animacao": {"paralelo": 0}}, {"imagem": {"paralelo": "6"}},
                 {"animacao": {"provedor": "openai"}}):
        with pytest.raises(ValueError):
            rotas.gravar(ruim)


def test_api_confirma_a_animacao_antes_de_gastar(ambiente, monkeypatch):
    from fastapi.testclient import TestClient

    from nucleo import espaco
    from nucleo.banco import repositorios
    from web import servidor
    with TestClient(servidor.app) as cliente:
        assert cliente.get("/api/configuracoes").json()["animacao"]["provedores"][0]["resolucao_padrao"] == "720p"
        assert cliente.put("/api/configuracoes/modelos", json={"animacao": {"resolucao": "8k"}}).status_code == 400
        # as imagens na OpenAI: assim a falta da chave do Gemini é a da animação, não a das imagens
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-falsa-para-teste")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-falsa-para-teste")
        assert cliente.put("/api/configuracoes/modelos", json={"imagem": {"provedor": "openai"}}).status_code == 200
        slug = cliente.post("/api/projetos", json={"titulo": "Gatos que voam"}).json()["slug"]
        espaco.gravar_texto(f"projetos/{slug}/00-brief.json", json.dumps({"formato": "short", "visual": {"animacao": "todas"}}))
        espaco.gravar_texto(f"projetos/{slug}/06-roteiro-final.md", "# roteiro\n")
        espaco.gravar_texto(f"projetos/{slug}/08-cenas.json", json.dumps({"proporcao": "9:16", "cenas": CENAS}))

        r = cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "producao", "confirmar_imagens": True})
        assert r.status_code == 400 and "anima as imagens" in r.json()["detail"]  # a animação usa o Gemini
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-falsa-para-teste")
        r = cliente.post(f"/api/projetos/{slug}/executar", json={"etapa": "producao", "confirmar_imagens": True})
        assert r.status_code == 409, r.text
        detalhe = r.json()["detail"]
        assert detalhe["precisa"] == "confirmar_animacao" and detalhe["cenas"] == 2 and detalhe["segundos"] == 15
        assert "US$ 1.52" in detalhe["detail"] and "720p" in detalhe["detail"]
        r = cliente.post(f"/api/projetos/{slug}/executar",
                         json={"etapa": "producao", "confirmar_imagens": True, "confirmar_animacao": True})
        assert r.status_code == 200, r.text
        assert repositorios.execucao(r.json()["id"])["parametros"]["confirmar_animacao"] is True
        cliente.post(f"/api/execucoes/{r.json()['id']}/cancelar")
