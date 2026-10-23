#!/usr/bin/env python3
"""Monta o vídeo de um projeto a partir das cenas, das imagens e, se houver, da narração e da música.

Lê projetos/<slug>/08-cenas.json (movimento, transição, texto na tela), as imagens de 09-imagens/ (ou os
quadros de rascunho de 09-imagens/provisorio/ quando falta a imagem real), as opções do pedido em
00-brief.json (legendas, música, narração) e grava:

  10-video/<slug>.mp4        vídeo final, H.264, com o áudio em AAC (narração e/ou música), se houver
  10-video/legendas.ass      legendas estilizadas (as que vão queimadas no vídeo)
  10-video/legendas.srt      as mesmas legendas em SRT, para subir como closed caption
  10-video/thumbnail.jpg     a thumbnail: a imagem gerada para ela (09-imagens/thumbnail.*) com o texto de
                             08-thumbnail.json por cima, no tamanho do vídeo e abaixo de 2 MB (limite do YouTube).
                             Sem a imagem, vale a primeira cena, com o texto do ângulo escolhido. Ela é também o
                             primeiro quadro do vídeo (1/30 s), que as plataformas usam como capa
                             (--sem-quadro-thumbnail tira).

Tempos:
  - com narração alinhada (09-narracao/narracao.wav e 09-narracao/tempos.json, gerados por narrar.py e
    alinhar.py), cada imagem troca quando a voz chega ao trecho dela e cada legenda aparece junto com a
    primeira palavra que mostra;
  - sem narração (narracao.ativa: não no pedido, ou --sem-narracao), usa a estimativa de 150 palavras por
    minuto de 08-cenas.json.

Legendas (legendas.* no pedido): com destacar_palavra, a palavra que está sendo dita ganha um fundo
colorido; sem narração, o destaque segue o ritmo estimado. Nas falas dos personagens ([FALA: Nome] no
roteiro), o destaque usa a cor de cada um (personagens[].cor no pedido; sem destaque, a cor vai na letra), e
um pedaço de legenda nunca mistura duas vozes.

Música (musica.* no pedido): o arquivo de musica.arquivo ou o de biblioteca/musicas/ com o nome da música.
Ela começa em musica.inicio, repete até o fim e entra e sai em fade. Por baixo da narração, fica em
musica.volume % e abaixa sozinha quando a voz fala. A voz sai normalizada a -14 LUFS, o volume de
referência do YouTube.

Cada cena vira um clipe com movimento de câmera (zoom ou pan sobre a imagem parada). Imagem com proporção
diferente da do vídeo (as da internet, por exemplo) entra inteira, sobre uma cópia desfocada dela mesma.
As cartelas recebem o texto_na_tela por cima da imagem escurecida.

O pedido define o padrão; as opções da linha de comando têm prioridade.

Uso:
    python3 skills/produzir-video/scripts/montar_video.py projetos/<slug>
    python3 skills/produzir-video/scripts/montar_video.py projetos/<slug> --sem-narracao --sem-movimento
    python3 skills/produzir-video/scripts/montar_video.py projetos/<slug> --musica caminho/da/musica.mp3

Só usa a biblioteca padrão e o ffmpeg (com libass e libfreetype).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from concurrent.futures import ThreadPoolExecutor
from itertools import groupby
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(RAIZ / "ferramentas"))
from brief import achar_musica, config, ler_brief, narracao_ativa  # noqa: E402
from medir_roteiro import ler_roteiro, sem_acento  # noqa: E402

RESOLUCOES = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}
INTENSIDADE_MOVIMENTO = 0.10  # zoom de 10% ao longo da cena
FADE_S = 0.4
FADE_FINAL_S = 0.6
DIFERENCA_PROPORCAO = 1.12  # acima disso, a imagem entra inteira sobre um fundo desfocado
POSICOES = {"baixo": 2, "centro": 5, "alto": 8}  # alinhamento da legenda no ASS
PALETA_PERSONAGENS = ["#EC4899", "#22C55E", "#3B82F6", "#F97316", "#8B5CF6", "#06B6D4", "#EF4444", "#FFD400"]
PONTUACAO_FINAL = re.compile(r"[.,!?;:…]$")
THUMB_MAX_BYTES = 2_000_000  # limite do YouTube para thumbnail personalizada
LARGURA_LETRA = 0.62  # largura média de uma maiúscula em negrito, em frações do tamanho da fonte
# Palavras antes das quais é natural quebrar a legenda (assim "na Avenida Brasil" não vira "na Avenida | Brasil").
INICIO_BOM = {"e", "mas", "que", "porque", "quando", "se", "pra", "para", "com", "sem", "na", "no", "nas", "nos", "num",
              "numa", "de", "do", "da", "dos", "das", "em", "o", "a", "os", "as", "um", "uma", "ou", "porém", "então",
              "só", "até", "pelo", "pela", "ao", "à", "onde", "como", "nem", "lá", "aí"}


def fonte_negrito() -> str | None:
    try:
        return subprocess.run(["fc-match", "-f", "%{file}", "Noto Sans:bold"], capture_output=True, text=True).stdout or None
    except FileNotFoundError:
        return None


def ffmpeg(*args: str, cwd: Path | None = None) -> None:
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-1500:])


def achar_imagem(pasta: Path, id_: str) -> tuple[Path | None, str]:
    for ext in ("png", "jpg", "webp"):
        if (pasta / "09-imagens" / f"{id_}.{ext}").exists():
            return pasta / "09-imagens" / f"{id_}.{ext}", "real"
    prov = pasta / "09-imagens" / "provisorio" / f"{id_}.png"
    return (prov, "provisorio") if prov.exists() else (None, "faltando")


def dimensoes(imagem: Path) -> tuple[int, int] | None:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0:s=x", str(imagem)], capture_output=True, text=True)
    m = re.match(r"(\d+)x(\d+)", r.stdout.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def segundos(tempo: str | None) -> float:
    m = re.fullmatch(r"(\d+):(\d{2})", tempo or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 0.0


# ---------------------------------------------------------------- clipes


def expr_movimento(mov: str, n: int) -> tuple[str, str, str]:
    a, p = INTENSIDADE_MOVIMENTO, f"on/{max(1, n - 1)}"
    centro_x, centro_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    return {
        "zoom-in": (f"1+{a}*{p}", centro_x, centro_y),
        "zoom-out": (f"1+{a}-{a}*{p}", centro_x, centro_y),
        "pan-direita": (f"1+{a}", f"(iw-iw/zoom)*{p}", centro_y),
        "pan-esquerda": (f"1+{a}", f"(iw-iw/zoom)*(1-{p})", centro_y),
        "descer": (f"1+{a}", centro_x, f"(ih-ih/zoom)*{p}"),
        "subir": (f"1+{a}", centro_x, f"(ih-ih/zoom)*(1-{p})"),
    }.get(mov, ("1", "0", "0"))


def encaixada(w: int, h: int) -> str:
    """A imagem inteira no centro, sobre uma cópia dela ampliada, desfocada e escurecida."""
    return (f"split=2[fundo][frente];[fundo]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
            f"boxblur=40:2,eq=brightness=-0.12[fundo2];[frente]scale={w}:{h}:force_original_aspect_ratio=decrease[frente2];"
            "[fundo2][frente2]overlay=(W-w)/2:(H-h)/2,setsar=1")


def filtros_texto(texto: str, largura: int, altura: int, fonte: str | None, tmp: Path, nome: str, cartela: bool,
                  y_texto: float) -> list[str]:
    """Uma linha de drawtext por linha de texto, centralizadas (o drawtext não centraliza várias linhas)."""
    vertical = altura > largura
    tamanho = int(largura * (0.085 if vertical else 0.055)) if cartela else int(largura * (0.05 if vertical else 0.032))
    if cartela:  # "NÍVEL 1 — O OLHEIRO" quebra no travessão, não no meio da frase
        linhas = [l for parte in re.split(r"\s+[—–-]\s+", texto) for l in textwrap.wrap(parte, 14 if vertical else 30)]
    else:
        linhas = [texto]
    altura_linha = int(tamanho * 1.3)
    topo = f"(h-{altura_linha * len(linhas)})/2" if cartela else str(int(altura * y_texto))
    filtros = []
    for i, linha in enumerate(linhas):
        arq = tmp / f"{nome}-{i}.txt"
        arq.write_text(linha, encoding="utf-8")
        f = (f"drawtext=textfile='{arq}':fontcolor=white:fontsize={tamanho}:x=(w-tw)/2:y={topo}+{i * altura_linha}"
             f":borderw={max(2, tamanho // 14)}:bordercolor=black")
        if not cartela:
            f += ":box=1:boxcolor=black@0.45:boxborderw=18"
        if fonte:
            f += f":fontfile='{fonte}'"
        filtros.append(f)
    return filtros


def montar_clipe(c: dict, i: int, total: int, n: int, imagem: Path | None, args, largura: int, altura: int,
                 fonte: str | None, tmp: Path) -> Path:
    dur = n / args.fps
    w2, h2 = int(largura * 1.5) // 2 * 2, int(altura * 1.5) // 2 * 2  # margem para o zoom sem perder nitidez
    if imagem:
        entrada = ["-i", str(imagem)]
        base = f"scale={w2}:{h2}:force_original_aspect_ratio=increase,crop={w2}:{h2},setsar=1"
        dim = dimensoes(imagem)
        razao = (dim[0] / dim[1]) / (largura / altura) if dim else 1.0
        if max(razao, 1 / razao) > DIFERENCA_PROPORCAO:
            base = encaixada(w2, h2)
    else:
        entrada = ["-f", "lavfi", "-i", f"color=c=0x111111:s={w2}x{h2}:r=1:d=1"]
        base = "setsar=1"
    mov = "estatico" if args.sem_movimento else c.get("movimento", "estatico")
    z, x, y = expr_movimento(mov, n)
    cadeia = [base, f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={largura}x{altura}:fps={args.fps}"]
    if c.get("tipo") == "cartela":
        if imagem:
            cadeia.append("drawbox=c=black@0.55:t=fill")
        cadeia += filtros_texto(c.get("texto_na_tela") or "", largura, altura, fonte, tmp, f"c{i}", True, args.y_texto)
    elif c.get("texto_na_tela"):
        cadeia += filtros_texto(c["texto_na_tela"], largura, altura, fonte, tmp, f"c{i}", False, args.y_texto)
    if c.get("transicao") == "fade" or (i == 0 and not args.corte_seco):
        cadeia.append(f"fade=t=in:st=0:d={min(FADE_S, dur / 3):.2f}")
    if i == total - 1 and not args.corte_seco:
        cadeia.append(f"fade=t=out:st={max(0, dur - FADE_FINAL_S):.2f}:d={min(FADE_FINAL_S, dur):.2f}")
    cadeia.append("format=yuv420p")
    saida = tmp / f"clipe-{i:04d}.mp4"
    ffmpeg(*entrada, "-vf", ",".join(cadeia), "-frames:v", str(n), "-r", str(args.fps), "-an",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(saida))
    return saida


# ---------------------------------------------------------------- thumbnail


def ler_json(caminho: Path) -> dict:
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def linhas_do_titulo(texto: str, area_w: float, area_h: float, tamanho_max: float) -> tuple[int, list[str]]:
    """Entre todas as quebras do texto em linhas, a que deixa a letra maior dentro da área (no empate, a de menos
    linhas e, depois, a mais equilibrada). Devolve (tamanho, linhas)."""
    palavras = texto.upper().split()[:10]  # 2^9 quebras no máximo; a thumbnail tem até 4 palavras
    opcoes = []
    for cortes in range(2 ** (len(palavras) - 1)):
        linhas, atual = [], [palavras[0]]
        for i, palavra in enumerate(palavras[1:]):
            if cortes >> i & 1:
                linhas.append(" ".join(atual))
                atual = []
            atual.append(palavra)
        linhas.append(" ".join(atual))
        tamanho = min(tamanho_max, area_w / (max(map(len, linhas)) * LARGURA_LETRA), area_h / (len(linhas) * 1.1))
        opcoes.append((int(tamanho), -len(linhas), -max(map(len, linhas)), linhas))
    tamanho, _, _, linhas = max(opcoes, key=lambda o: o[:3])
    return tamanho, linhas


def filtros_titulo(texto: str, posicao: str, largura: int, altura: int, cor: str, fonte: str | None, tmp: Path) -> list[str]:
    """O texto da thumbnail: grande, em caixa alta, com contorno e sombra, na parte do quadro que a imagem deixou livre."""
    margem_x, margem_y = largura * 0.05, altura * 0.06
    lateral = posicao in ("esquerda", "direita")
    area_w = largura * 0.46 if lateral else largura - 2 * margem_x
    area_h = altura - 2 * margem_y if lateral else altura * 0.34
    tamanho_max = (largura if altura > largura else altura) * (0.16 if altura > largura else 0.2)
    tamanho, linhas = linhas_do_titulo(texto, area_w, area_h, tamanho_max)
    altura_linha = int(tamanho * 1.1)
    bloco = altura_linha * len(linhas)
    topo = {"alto": margem_y, "baixo": altura - margem_y - bloco}.get(posicao, (altura - bloco) / 2)
    x = {"esquerda": f"{margem_x:.0f}", "direita": f"w-tw-{margem_x:.0f}"}.get(posicao, "(w-tw)/2")
    filtros = []
    for i, linha in enumerate(linhas):
        arq = tmp / f"thumb-{i}.txt"
        arq.write_text(linha, encoding="utf-8")
        f = (f"drawtext=textfile='{arq}':expansion=none:y_align=font:fontcolor={cor}:fontsize={tamanho}:x={x}:y={topo + i * altura_linha:.0f}"
             f":borderw={max(4, tamanho // 9)}:bordercolor=black:shadowx={max(2, tamanho // 18)}:shadowy={max(2, tamanho // 18)}"
             ":shadowcolor=black@0.7")
        if fonte:
            f += f":fontfile='{fonte}'"
        filtros.append(f)
    return filtros


def compor_thumbnail(pasta: Path, thumb: dict | None, cenas: list[dict], imagens: list[Path | None], largura: int,
                     altura: int, cor: str, fonte: str | None, tmp: Path, destino: Path) -> str:
    """Grava a thumbnail em destino e devolve de onde veio a imagem."""
    base, origem = achar_imagem(pasta, "thumbnail")
    if origem != "real":
        reais = [(c, i) for c, i in zip(cenas, imagens) if i and c.get("tipo") != "cartela"] or [(c, i) for c, i in zip(cenas, imagens) if i]
        if not reais:
            raise RuntimeError("nenhuma imagem para a thumbnail")
        cena, base = reais[0]
        origem = f"cena {cena['id']}"
    else:
        origem = "imagem gerada para ela"
    if thumb is not None:
        texto, posicao = thumb.get("texto") or "", thumb.get("posicao_texto") or "alto"
    else:
        texto, posicao = (ler_json(pasta / "04-angulo-escolhido.json").get("thumbnail") or {}).get("texto") or "", "alto"
    dim = dimensoes(base)
    razao = (dim[0] / dim[1]) / (largura / altura) if dim else 1.0
    cadeia = [encaixada(largura, altura) if max(razao, 1 / razao) > DIFERENCA_PROPORCAO else
              f"scale={largura}:{altura}:force_original_aspect_ratio=increase,crop={largura}:{altura},setsar=1"]
    if texto.strip():
        cadeia += filtros_titulo(texto, posicao, largura, altura, cor, fonte, tmp)
    png = tmp / "thumbnail.png"
    ffmpeg("-i", str(base), "-vf", ",".join(cadeia), "-frames:v", "1", str(png))
    for qualidade in (2, 3, 5, 8, 12):
        ffmpeg("-i", str(png), "-q:v", str(qualidade), str(destino))
        if destino.stat().st_size <= THUMB_MAX_BYTES:
            break
    return origem + (f', texto "{texto}"' if texto.strip() else ", sem texto")


# ---------------------------------------------------------------- legendas


def agrupar(palavras: list[str], max_palavras: int) -> list[list[int]]:
    """Divide as palavras em pedaços de legenda e devolve os índices de cada pedaço."""
    grupos, atual = [], []
    for k, p in enumerate(palavras):
        atual.append(k)
        if PONTUACAO_FINAL.search(p) and len(atual) >= 2:
            grupos.append(atual)
            atual = []
        elif len(atual) > max_palavras:
            # Passou do limite: corta antes da palavra de ligação mais próxima do fim, se houver.
            ligacao = [re.sub(r"\W", "", palavras[j].lower()) in INICIO_BOM for j in atual]
            recuo = max(3, max_palavras // 2)
            corte = next((m for m in range(len(atual) - 1, max(1, len(atual) - 1 - recuo), -1) if ligacao[m]), max_palavras)
            while corte > 2 and ligacao[corte - 1]:  # "e a parcela": corta antes do "e", não entre "e" e "a"
                corte -= 1
            grupos.append(atual[:corte])
            atual = atual[corte:]
    if atual:
        if len(atual) == 1 and grupos and len(grupos[-1]) < max_palavras + 1:
            grupos[-1] += atual  # não deixa uma palavra sozinha na tela
        else:
            grupos.append(atual)
    return grupos


def tempo_ass(s: float) -> str:
    cs = round(s * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def tempo_srt(s: float) -> str:
    ms = round(s * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def falas_do_roteiro(pasta: Path) -> dict[str, tuple[list[str], list[str | None]]]:
    """Para cada bloco, as palavras e quem diz cada uma (None: o narrador), pelas marcas [FALA] do roteiro."""
    roteiro = pasta / "06-roteiro-final.md"
    if not roteiro.exists():
        return {}
    saida = {}
    for b in ler_roteiro(roteiro)[1]:
        quem: list[str | None] = [None] * len(b.tokens)
        for f in b.falas:
            quem[f.inicio:f.fim] = [f.personagem] * (f.fim - f.inicio)
        saida[b.id] = (b.tokens, quem)
    return saida


def marcar_falantes(cenas: list[dict], palavras_por_cena: list[list[dict]], roteiro: dict) -> None:
    """Põe em cada palavra quem a diz. A palavra k da cena é a palavra palavras[0] + k do bloco."""
    for c, palavras in zip(cenas, palavras_por_cena):
        tokens, quem = roteiro.get(c["bloco"], ([], []))
        a = (c.get("palavras") or [None])[0]
        for k, p in enumerate(palavras):
            p["fala"] = quem[a + k] if a is not None and a + k < len(quem) else None


def palavras_estimadas(c: dict, ini: float, dur: float, roteiro: dict) -> list[dict]:
    """Sem narração: distribui o tempo da cena pelas palavras do trecho, pelo tamanho de cada uma. As palavras
    vêm do roteiro (as mesmas do alinhamento); sem ele, do trecho da cena."""
    tokens = roteiro.get(c["bloco"], ([], []))[0]
    if c.get("palavras") and c["palavras"][1] <= len(tokens):
        palavras = tokens[c["palavras"][0]:c["palavras"][1]]
    else:
        palavras = [p for p in c.get("trecho", "").split() if any(ch.isalnum() for ch in p)]
    pesos = [len(p) + 1 for p in palavras]
    saida, t = [], ini
    for p, peso in zip(palavras, pesos):
        d = dur * peso / sum(pesos)
        saida.append({"texto": p, "inicio": t, "fim": t + d})
        t += d
    return saida


def palavras_alinhadas(c: dict, tempos: dict[str, dict]) -> list[dict]:
    return [p for p in tempos[c["id"]]["palavras"] if any(ch.isalnum() for ch in p["texto"])]


def gerar_legendas(cenas: list[dict], palavras_por_cena: list[list[dict]], args, largura: int,
                   altura: int) -> list[tuple[float, float, list[dict]]]:
    """Pedaços de legenda com as palavras e o tempo de cada uma. Cada pedaço entra na primeira palavra e sai
    quando o próximo entra (ou logo depois da última palavra, se vier uma pausa)."""
    max_palavras = args.palavras_por_legenda or (4 if altura > largura else 7)
    brutos = []
    for c, palavras in zip(cenas, palavras_por_cena):
        if c.get("tipo") == "cartela" and not args.legendar_cartelas:
            continue
        for _, mesma_voz in groupby(palavras, key=lambda p: p.get("fala")):  # um pedaço nunca mistura duas vozes
            mesma_voz = list(mesma_voz)
            for g in agrupar([p["texto"] for p in mesma_voz], max_palavras):
                grupo = [mesma_voz[k] for k in g]
                brutos.append([grupo[0]["inicio"], grupo[-1]["fim"], grupo])
    for atual, prox in zip(brutos, brutos[1:]):
        atual[1] = prox[0] if prox[0] - atual[1] < 0.6 else atual[1] + 0.3  # sem piscar entre pedaços seguidos
    if brutos:
        brutos[-1][1] += 0.3
    return [tuple(b) for b in brutos]


def cor_ass(hexa: str) -> str:
    r, g, b = (int(hexa[k:k + 2], 16) for k in (1, 3, 5))
    return f"&H00{b:02X}{g:02X}{r:02X}"


def tag_cor(hexa: str) -> str:
    """A cor no formato das marcas de sobreposição do ASS (\\1c, \\3c): &HBBGGRR&."""
    return "&H" + cor_ass(hexa)[4:] + "&"


def cores_dos_personagens(brief: dict) -> dict[str, str]:
    """Cor de cada personagem (pelo nome sem acento): a do pedido, ou uma da paleta."""
    cores = {}
    for i, p in enumerate(brief.get("personagens") or []):
        if p.get("nome"):
            cores[sem_acento(p["nome"]).strip()] = p.get("cor") or PALETA_PERSONAGENS[i % len(PALETA_PERSONAGENS)]
    return cores


def letra_sobre(hexa: str) -> str:
    """Letra preta sobre fundo claro, branca sobre fundo escuro."""
    r, g, b = (int(hexa[k:k + 2], 16) for k in (1, 3, 5))
    return "#000000" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#FFFFFF"


def limpar_ass(texto: str) -> str:
    return texto.replace("\\", "").replace("{", "(").replace("}", ")")


def texto_destacado(palavras: list[str], k: int) -> str:
    """A linha inteira invisível, menos a palavra k, que aparece com a caixa do estilo Destaque. Por ser a
    mesma linha, a palavra cai exatamente sobre a da legenda normal."""
    partes = [f"{{\\alpha&H00&}}{p}{{\\alpha&HFF&}}" if j == k else p for j, p in enumerate(palavras)]
    return "{\\alpha&HFF&}" + " ".join(partes)


def escrever_ass(eventos, caminho: Path, largura: int, altura: int, args) -> None:
    vertical = altura > largura
    tamanho = int(altura * (0.044 if vertical else 0.066))
    contorno = max(3, tamanho // 14)
    folga = max(contorno + 4, round(tamanho * 0.14))  # margem da caixa do destaque em volta da palavra
    margem_v = int(altura * {"baixo": 0.27 if vertical else 0.07,  # nos shorts, acima da interface do app
                             "alto": 0.12 if vertical else 0.07}.get(args.posicao, 0))
    margem_h = int(largura * (0.08 if vertical else 0.09))
    posicao = f"{POSICOES.get(args.posicao, 2)},{margem_h},{margem_h},{margem_v},1"
    texto, letra = cor_ass(args.cor_texto), cor_ass(letra_sobre(args.cor_destaque))
    linhas = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {largura}", f"PlayResY: {altura}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Legenda,Noto Sans,{tamanho},{texto},{texto},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{contorno},2,{posicao}",
        # BorderStyle 3: a "borda" vira uma caixa cheia atrás de cada letra, na cor do contorno.
        f"Style: Destaque,Noto Sans,{tamanho},{letra},{letra},{cor_ass(args.cor_destaque)},&H00000000,-1,0,0,0,100,100,0,0,3,"
        f"{folga},0,{posicao}",
        "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for ini, fim, palavras in eventos:
        textos = [limpar_ass(p["texto"].upper() if args.maiusculas else p["texto"]) for p in palavras]
        quem = palavras[0].get("fala")
        cor = args.cores_personagens.get(sem_acento(quem).strip()) if quem else None
        # Fala de personagem: a cor dele na caixa do destaque ou, sem destaque, na letra.
        base = f"{{\\1c{tag_cor(cor)}}}" if cor and not args.destaque else ""
        linhas.append(f"Dialogue: 0,{tempo_ass(ini)},{tempo_ass(fim)},Legenda,,0,0,0,,{base}{' '.join(textos)}")
        if not args.destaque:
            continue
        caixa = f"{{\\3c{tag_cor(cor)}\\1c{tag_cor(letra_sobre(cor))}}}" if cor else ""
        for k, p in enumerate(palavras):  # uma linha por palavra, do início dela até o início da próxima
            de = ini if k == 0 else p["inicio"]
            ate = palavras[k + 1]["inicio"] if k + 1 < len(palavras) else fim
            if round(ate * 100) > round(de * 100):
                linhas.append(f"Dialogue: 1,{tempo_ass(de)},{tempo_ass(ate)},Destaque,,0,0,0,,{caixa}{texto_destacado(textos, k)}")
    caminho.write_text("\n".join(linhas) + "\n", encoding="utf-8")


def escrever_srt(eventos, caminho: Path, maiusculas: bool) -> None:
    blocos = []
    for n, (ini, fim, palavras) in enumerate(eventos, 1):
        texto = " ".join(p["texto"] for p in palavras)
        blocos.append(f"{n}\n{tempo_srt(ini)} --> {tempo_srt(fim)}\n{texto.upper() if maiusculas else texto}\n")
    caminho.write_text("\n".join(blocos), encoding="utf-8")


# ---------------------------------------------------------------- áudio


def grafo_audio(narracao: Path | None, musica: Path | None, inicio_musica: float, volume: float,
                duracao: float, corte_seco: bool = False) -> tuple[list[str], list[str]]:
    """Entradas e filtros do áudio final ([a]): a narração, a música ou as duas. A entrada 0 é o vídeo.
    Com corte_seco, a música entra e sai em 0,15 s (só para não estalar), para o vídeo emendar no replay."""
    fade_in, fade_out = (0.15, 0.15) if corte_seco else (1.5, 2.5)
    formato = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    # O loudnorm desloca os tempos do áudio; asetpts refaz a contagem para os fades caírem no lugar certo.
    normalizar = "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,asetpts=N/SR/TB"
    entradas, n, voz, mus = [], 1, "", ""
    if narracao:
        entradas += ["-i", str(narracao)]
        voz, n = f"[{n}:a]", n + 1
    if musica:
        # O ponto de início vai no atrim: com -ss junto de -stream_loop, o ffmpeg devolve áudio a mais.
        entradas += ["-stream_loop", "-1", "-i", str(musica)]
        mus = (f"[{n}:a]{formato},atrim=start={inicio_musica:.3f}:duration={duracao:.3f},asetpts=PTS-STARTPTS,{normalizar}"
               + (f",volume={volume:.3f}" if narracao else "")
               + f",afade=t=in:d={fade_in},afade=t=out:st={max(0.0, duracao - fade_out):.3f}:d={fade_out}")
    if narracao and musica:
        return entradas, [
            f"{voz}{formato},{normalizar},apad=whole_dur={duracao:.3f},asplit=2[voz][chave]",
            f"{mus}[mus]",
            "[mus][chave]sidechaincompress=threshold=0.05:ratio=3:attack=20:release=400[fundo]",  # abaixa na fala
            "[voz][fundo]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.84:level=0[a]",
        ]
    if narracao:
        return entradas, [f"{voz}{normalizar}[a]"]
    if musica:
        return entradas, [f"{mus}[a]"]
    return [], []


# ---------------------------------------------------------------- principal


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pasta", type=Path, help="pasta do projeto (projetos/<slug>)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--sem-legendas", action="store_true")
    ap.add_argument("--sem-destaque", action="store_true", help="legendas sem o fundo colorido na palavra dita")
    ap.add_argument("--corte-seco", action="store_true",
                    help="sem fade no começo e no fim (imagem e música), para o short emendar no replay")
    ap.add_argument("--sem-movimento", action="store_true", help="imagens paradas, sem zoom nem pan")
    ap.add_argument("--maiusculas", action="store_true", help="legendas em caixa alta")
    ap.add_argument("--palavras-por-legenda", type=int, help="máximo de palavras na tela (padrão 4 no vertical, 7 no horizontal)")
    ap.add_argument("--legendar-cartelas", action="store_true", help="também legenda a narração das cartelas")
    ap.add_argument("--sem-provisorio", action="store_true", help="falha se faltar imagem real, em vez de usar o rascunho")
    ap.add_argument("--sem-narracao", "--sem-audio", dest="sem_narracao", action="store_true",
                    help="ignora a narração e usa os tempos estimados")
    ap.add_argument("--sem-musica", action="store_true", help="ignora a música de fundo do pedido")
    ap.add_argument("--musica", type=Path, help="arquivo de música de fundo (no lugar do que está no pedido)")
    ap.add_argument("--paralelo", type=int, default=4, help="clipes codificados ao mesmo tempo")
    ap.add_argument("--sem-quadro-thumbnail", action="store_true",
                    help="não põe a thumbnail no primeiro quadro do vídeo (ela é gravada do mesmo jeito)")
    ap.add_argument("--manter-clipes", action="store_true")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        print("erro: ffmpeg não encontrado", file=sys.stderr)
        return 2
    doc_path = args.pasta / "08-cenas.json"
    if not doc_path.exists():
        print(f"erro: {doc_path} não existe", file=sys.stderr)
        return 2
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    cenas = doc["cenas"]
    if any(c.get("inicio_s") is None for c in cenas):
        print("erro: há cenas sem tempo; rode ferramentas/cenas.py montar e corrija os erros", file=sys.stderr)
        return 2
    largura, altura = RESOLUCOES.get(doc.get("proporcao", "16:9"), (1920, 1080))

    # Opções do pedido (00-brief.json), que a linha de comando pode sobrepor.
    brief = ler_brief(args.pasta)
    args.sem_narracao = args.sem_narracao or not narracao_ativa(brief)
    args.sem_legendas = args.sem_legendas or not config(brief, "legendas.ativas", True)
    args.destaque = not args.sem_destaque and bool(config(brief, "legendas.destacar_palavra", False))
    args.maiusculas = args.maiusculas or bool(config(brief, "legendas.maiusculas", False))
    args.palavras_por_legenda = args.palavras_por_legenda or config(brief, "legendas.palavras_por_legenda")
    args.posicao = config(brief, "legendas.posicao", "baixo")
    args.cor_destaque = config(brief, "legendas.cor_destaque", "#FFD400")
    args.cor_texto = config(brief, "legendas.cor_texto", "#FFFFFF")
    args.cores_personagens = cores_dos_personagens(brief)
    roteiro = falas_do_roteiro(args.pasta)
    # Legenda no alto: o texto_na_tela das ilustrações desce para não ficar embaixo dela.
    args.y_texto = 0.80 if args.posicao == "alto" and not args.sem_legendas else 0.08

    musica = None
    if args.musica:
        musica = args.musica.resolve()
        if not musica.is_file():
            print(f"erro: {args.musica} não existe", file=sys.stderr)
            return 2
    elif not args.sem_musica and config(brief, "musica.ativa", False):
        musica = achar_musica(brief.get("musica") or {}, args.pasta)
        if musica is None:
            m = brief.get("musica") or {}
            onde = m.get("arquivo") or f"biblioteca/musicas/ (um arquivo com as palavras de '{m.get('nome')}' no nome)"
            print(f"erro: o pedido tem música de fundo, mas o arquivo não foi encontrado em {onde}. "
                  "Coloque o arquivo ou monte com --sem-musica.", file=sys.stderr)
            return 2

    # Tempos: os reais da narração alinhada, se houver; senão, a estimativa por palavras.
    narracao = (args.pasta / "09-narracao" / "narracao.wav").resolve()
    tempos_path = args.pasta / "09-narracao" / "tempos.json"
    tempos, doc_t = None, {}
    if not args.sem_narracao and narracao.exists():
        if not tempos_path.exists():
            print("erro: há narração mas falta 09-narracao/tempos.json; rode alinhar.py (ou use --sem-narracao)", file=sys.stderr)
            return 2
        doc_t = json.loads(tempos_path.read_text(encoding="utf-8"))
        if [c["id"] for c in cenas] != [c["id"] for c in doc_t["cenas"]]:
            print("erro: as cenas mudaram depois do alinhamento; rode alinhar.py de novo", file=sys.stderr)
            return 2
        tempos = {c["id"]: c for c in doc_t["cenas"]}
        if doc_t.get("alertas"):
            print(f"aviso: o alinhamento tem {len(doc_t['alertas'])} alerta(s); veja 09-narracao/tempos.json")
    if tempos:
        inicios = [tempos[c["id"]]["inicio_s"] for c in cenas]
        fim_total = doc_t["duracao_video_s"]
    else:
        inicios = [c["inicio_s"] for c in cenas]
        fim_total = cenas[-1]["inicio_s"] + cenas[-1]["duracao_s"]
    fronteiras = inicios[1:] + [fim_total]
    duracoes = [f - i for i, f in zip(inicios, fronteiras)]
    # Quadros pelas fronteiras acumuladas: o arredondamento de cada cena não se soma ao longo do vídeo.
    quadros = [max(1, round(f * args.fps) - round(i * args.fps)) for i, f in zip(inicios, fronteiras)]

    imagens, origem = [], {"real": 0, "provisorio": 0, "fundo": 0}
    faltando = []
    for c in cenas:
        img, tipo = achar_imagem(args.pasta, c["id"])
        if tipo == "provisorio" and args.sem_provisorio:
            img, tipo = None, "faltando"
        if tipo == "faltando":
            if c.get("tipo") == "cartela":
                tipo = "fundo"
            else:
                faltando.append(c["id"])
        imagens.append(img)
        origem[tipo] = origem.get(tipo, 0) + 1
    if faltando:
        print(f"erro: {len(faltando)} cena(s) sem imagem: {', '.join(faltando[:15])}{'…' if len(faltando) > 15 else ''}", file=sys.stderr)
        print("  gere as imagens (gerar_imagens.py, buscar_imagens.py) ou os quadros de rascunho (gerar_imagens.py --provisorio)", file=sys.stderr)
        return 1

    slug = args.pasta.name
    saida = (args.pasta / "10-video").resolve()
    saida.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="montagem-", dir=saida))
    fonte = fonte_negrito()
    print(f"montando {len(cenas)} cenas · {largura}x{altura} · {args.fps} fps · {fim_total:.1f} s")
    try:
        with ThreadPoolExecutor(max_workers=args.paralelo) as pool:
            clipes = list(pool.map(lambda k: montar_clipe(cenas[k], k, len(cenas), quadros[k], imagens[k], args,
                                                           largura, altura, fonte, tmp), range(len(cenas))))
        lista = tmp / "lista.txt"
        lista.write_text("".join(f"file '{p.name}'\n" for p in clipes), encoding="utf-8")
        sem_legenda = tmp / "sem-legendas.mp4"
        ffmpeg("-f", "concat", "-safe", "0", "-i", lista.name, "-c", "copy", sem_legenda.name, cwd=tmp)

        thumbnail, info_thumb = saida / "thumbnail.jpg", None
        try:
            info_thumb = compor_thumbnail(args.pasta, doc.get("thumbnail"), cenas, imagens, largura, altura,
                                          "0x" + args.cor_destaque.lstrip("#")[:6], fonte, tmp, thumbnail)
        except RuntimeError as e:
            print(f"aviso: a thumbnail falhou ({e}); o vídeo sai sem ela no primeiro quadro", file=sys.stderr)

        final = saida / f"{slug}.mp4"
        volume = config(brief, "musica.volume", 20) / 100
        entradas_audio, filtros_audio = grafo_audio(narracao if tempos else None, musica,
                                                    segundos(config(brief, "musica.inicio")), volume, fim_total,
                                                    args.corte_seco)
        n_legendas = 0
        filtros_video, atual = [], "[0:v]"
        if not args.sem_legendas:
            if tempos:
                palavras = [palavras_alinhadas(c, tempos) for c in cenas]
            else:
                palavras = [palavras_estimadas(c, i, d, roteiro) for c, i, d in zip(cenas, inicios, duracoes)]
            marcar_falantes(cenas, palavras, roteiro)
            eventos = gerar_legendas(cenas, palavras, args, largura, altura)
            escrever_ass(eventos, saida / "legendas.ass", largura, altura, args)
            escrever_srt(eventos, saida / "legendas.srt", args.maiusculas)
            n_legendas = len(eventos)
            filtros_video.append(f"{atual}subtitles=legendas.ass[vl]")
            atual = "[vl]"
        entrada_thumb = []
        if info_thumb and not args.sem_quadro_thumbnail:
            # Só o quadro 0 (o do fade de entrada): o tempo do vídeo e da voz não muda.
            k = 1 + entradas_audio.count("-i")
            entrada_thumb = ["-i", thumbnail.name]
            filtros_video += [f"[{k}:v]scale={largura}:{altura},setsar=1[th]", f"{atual}[th]overlay=enable='eq(n,0)'[vt]"]
            atual = "[vt]"
        if filtros_video:
            filtros_video.append(f"{atual}null[v]")
            mapa_video = ["-map", "[v]", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
        else:
            mapa_video = ["-map", "0:v:0", "-c:v", "copy"]
        grafo = filtros_video + filtros_audio
        opcoes_audio = ["-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"] if filtros_audio else ["-an"]
        ffmpeg("-i", str(sem_legenda.relative_to(saida)), *entradas_audio, *entrada_thumb,
               *(["-filter_complex", ";".join(grafo)] if grafo else []),
               *mapa_video, *opcoes_audio, "-movflags", "+faststart", final.name, cwd=saida)
    except RuntimeError as e:
        print(f"erro do ffmpeg: {e}", file=sys.stderr)
        return 1
    finally:
        if not args.manter_clipes:
            shutil.rmtree(tmp, ignore_errors=True)

    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(final)],
                         capture_output=True, text=True).stdout.strip()
    print(f"vídeo: {final.resolve()}")
    elenco = "".join(f"; {nome}: {voz}" for nome, voz in (doc_t.get("personagens") or {}).items())
    audio = [f"narração alinhada (narrador {doc_t.get('voz')}{elenco})" if tempos else "sem narração (tempos estimados)"]
    if musica:
        audio.append(f"música {musica.name}" + (f" a {volume:.0%}, abaixando na fala" if tempos else ""))
    print(f"duração: {float(dur):.1f} s · tamanho: {final.stat().st_size / 1e6:.1f} MB · {' + '.join(audio)}")
    print(f"imagens: {origem['real']} reais, {origem['provisorio']} provisórias, {origem.get('fundo', 0)} cartelas sobre fundo liso")
    if n_legendas:
        print(f"legendas: {n_legendas}{' com destaque na palavra' if args.destaque else ''} (10-video/legendas.ass e legendas.srt)")
    if info_thumb:
        quadro = "" if args.sem_quadro_thumbnail else " (também o primeiro quadro do vídeo)"
        print(f"thumbnail: {thumbnail.resolve()}{quadro} · {info_thumb}")
    if origem["provisorio"]:
        print("aviso: há quadros provisórios no vídeo; gere as imagens reais e monte de novo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
