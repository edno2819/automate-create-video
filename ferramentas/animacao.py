"""Animação das imagens das cenas (visual.animacao no pedido): quais cenas animam, o texto que vai ao modelo de vídeo,
a duração de cada clipe e se um clipe pronto ainda vale. Lido por producao/animar_imagens.py (que gera os clipes),
producao/montar_video.py (que os põe no lugar do zoom sobre a imagem parada) e cenas.py (que confere os campos das
cenas). Só a biblioteca padrão.

Modos do pedido:
    nenhuma     (padrão) todas as cenas com o zoom ou o pan sobre a imagem parada
    destaques   as ilustrações que o diretor visual marcou com "animar": true; sem nenhuma marcada, a primeira
                ilustração de cada bloco e a última do vídeo
    todas       todas as ilustrações geradas por IA (menos as marcadas com "animar": false)

Ficam paradas em qualquer modo as cartelas, as imagens da internet e as cenas sem imagem real, e o vídeo inteiro
quando a proporção não é uma das que o modelo gera.

    09-animacoes/<id>.mp4         o clipe da cena (o áudio que o modelo gera vem junto; a montagem descarta)
    09-animacoes/manifesto.json   {id: {status, arquivo, pedido, imagem, duracao_s, segundos_pedidos, modelo, ...}}

Um clipe vale enquanto a imagem da cena for a mesma de quando ele foi feito (o sha1 dela fica em "imagem"): uma
cena refeita depois volta à imagem parada até ser animada de novo.

Transições ("transicao" da cena, como ela entra vindo da anterior; o guia do diretor visual é biblioteca/animacao.md):
    corte, fade              as de sempre (a montagem faz)
    TRANSICOES_DE_VIDEO      geradas pelo modelo no clipe da cena anterior, que termina na imagem desta (primeiro e
                             último quadro); a anterior precisa animar, senão vira corte. O sha1 da imagem de chegada
                             fica em "ultimo_quadro": se ela muda, o clipe deixa de valer
    j-cut, l-cut             da montagem: a imagem troca meio segundo depois (j) ou antes (l) da frase que começa
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

MODOS = ("nenhuma", "destaques", "todas")
PASTA = "09-animacoes"
MANIFESTO = f"{PASTA}/manifesto.json"
MIN_SEGUNDOS, MAX_SEGUNDOS = 3, 10   # o que se pede ao modelo: a montagem corta o que sobra
FOLGA_S = 1.0                         # um clipe até 1 s mais curto que a cena ainda vale (a montagem estica um pouco)
MAX_PALAVRAS = 60
CAMERA = {"zoom-in": "a slow, gentle camera push-in", "zoom-out": "a slow, gentle camera pull-back",
          "pan-direita": "a slow camera pan to the right", "pan-esquerda": "a slow camera pan to the left",
          "subir": "a slow camera tilt up", "descer": "a slow camera tilt down", "estatico": "a static camera"}
EXTENSOES_IMAGEM = ("png", "jpg", "webp")
# O texto que vai ao modelo de vídeo para cada transição: o clipe termina no último quadro (a imagem da cena seguinte).
TRANSICOES_DE_VIDEO = {
    "whip-pan": "a fast whip pan with strong motion blur",
    "zoom-blur": "a fast stylized crash zoom with motion blur",
    "match-cut": "a match cut: the main shape, object or movement keeps its place in the frame and flows into the last frame",
    "push": "the last frame slides in from the right and smoothly pushes the current shot out of the frame",
    "wipe": "a foreground object (a passing figure, a wall or a pillar) crosses close to the camera, covering the frame "
            "and revealing the last frame behind it",
    "glitch": "a brief digital glitch: signal noise, RGB split and pixel distortion",
}
TRANSICOES_DE_MONTAGEM = ("j-cut", "l-cut")
TRANSICOES = ("corte", "fade", *TRANSICOES_DE_VIDEO, *TRANSICOES_DE_MONTAGEM)
SEGUNDOS_DA_TRANSICAO = 1    # a transição ocupa o último segundo do clipe
DESLOCAMENTO_J_L_S = 0.5     # quanto o j-cut e o l-cut atrasam ou adiantam o corte da imagem


def modo(brief: dict) -> str:
    valor = ((brief or {}).get("visual") or {}).get("animacao")
    return valor if valor in MODOS else "nenhuma"


def ler_json(caminho: Path) -> dict:
    try:
        dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dados if isinstance(dados, dict) else {}


def sha_arquivo(caminho: Path) -> str:
    return hashlib.sha1(Path(caminho).read_bytes()).hexdigest()[:12]


def imagem_da_cena(pasta: Path, id_: str) -> Path | None:
    """A imagem real da cena em 09-imagens/ (o quadro provisório não conta)."""
    return next((pasta / "09-imagens" / f"{id_}.{ext}" for ext in EXTENSOES_IMAGEM
                 if (pasta / "09-imagens" / f"{id_}.{ext}").is_file()), None)


def gerada_por_ia(cena: dict, manifesto_imagens: dict) -> bool:
    """Ilustração com imagem de IA: a cena da internet só conta quando a busca falhou e a imagem foi gerada."""
    if cena.get("tipo", "ilustracao") != "ilustracao":
        return False
    if cena.get("fonte") == "internet":
        return (manifesto_imagens.get(cena["id"]) or {}).get("origem") == "ia"
    return True


def selecionadas(cenas: list[dict], modo_: str, manifesto_imagens: dict) -> list[dict]:
    """As cenas que o modo do pedido anima, na ordem do vídeo."""
    if modo_ not in ("destaques", "todas"):
        return []
    candidatas = [c for c in cenas if gerada_por_ia(c, manifesto_imagens)]
    if modo_ == "todas":
        return [c for c in candidatas if c.get("animar") is not False]
    if marcadas := [c for c in candidatas if c.get("animar") is True]:
        return marcadas
    primeiras, blocos = set(), set()
    for c in candidatas:
        if c.get("bloco") not in blocos:
            blocos.add(c.get("bloco"))
            primeiras.add(c["id"])
    if candidatas:
        primeiras.add(candidatas[-1]["id"])
    return [c for c in candidatas if c["id"] in primeiras]


def duracoes(pasta: Path, cenas: list[dict]) -> dict[str, float]:
    """A duração de cada cena: a real, pela narração alinhada (09-narracao/tempos.json), ou a estimada pelas palavras."""
    tempos = ler_json(pasta / "09-narracao" / "tempos.json")
    alinhadas = tempos.get("cenas") or []
    if [c.get("id") for c in alinhadas] == [c["id"] for c in cenas] and tempos.get("duracao_video_s"):
        inicios = [float(c["inicio_s"]) for c in alinhadas]
        fins = inicios[1:] + [float(tempos["duracao_video_s"])]
        return {c["id"]: max(0.0, f - i) for c, i, f in zip(cenas, inicios, fins)}
    return {c["id"]: float(c.get("duracao_s") or 0) for c in cenas}


def segundos_pedidos(duracao: float) -> int:
    return max(MIN_SEGUNDOS, min(MAX_SEGUNDOS, math.ceil(duracao - 0.05)))


def movimento(cena: dict) -> str:
    """O movimento da cena: o que o diretor visual escreveu em "animacao" ou, sem isso, um movimento leve do que a
    imagem mostra, com a câmera do campo "movimento"."""
    if texto := " ".join(str(cena.get("animacao") or "").split()):
        return texto.rstrip(".")
    camera = CAMERA.get(cena.get("movimento") or "", CAMERA["estatico"])
    assunto = " ".join(str(cena.get("prompt_cena") or "").split()).rstrip(".")
    return f"gentle, natural ambient motion in the scene ({assunto}), with {camera}" if assunto else \
        f"gentle, natural ambient motion in the scene, with {camera}"


def transicao_de_saida(cenas: list[dict], indice: int) -> str | None:
    """A transição de vídeo que o clipe da cena `indice` faz no fim: a da cena seguinte, se for uma das geradas pelo
    modelo."""
    seguinte = cenas[indice + 1] if indice + 1 < len(cenas) else {}
    return seguinte.get("transicao") if seguinte.get("transicao") in TRANSICOES_DE_VIDEO else None


def prompt(cena: dict, segundos: int, transicao: str | None = None) -> str:
    """O texto enviado ao modelo de vídeo, igual em qualquer provedor (a API não tem campo de duração, de prompt
    negativo nem de áudio desligado). Com transição, o pedido leva também o último quadro (a imagem da cena seguinte),
    e o movimento termina nele."""
    texto = (f"Animate this image into a {segundos}-second video clip. The image is the exact first frame: keep its "
             "composition, framing, characters, faces, clothing, colors and art style unchanged. "
             f"Motion: {movimento(cena)}. Keep every movement subtle and slow, like a living painting. ")
    if transicao in TRANSICOES_DE_VIDEO:
        texto += (f"In the final {SEGUNDOS_DA_TRANSICAO} second, transition into the second image, the exact last frame, "
                  f"with {TRANSICOES_DE_VIDEO[transicao]}. Before that, no new people or objects and no cuts. ")
    else:
        texto += "No new people or objects, no scene cuts, no camera cuts. "
    return texto + "No text, captions or watermarks. No dialogue, no music and no sound effects."


def hash_pedido(modelo: str, resolucao: str, proporcao: str, imagem: str | None, movimento_: str,
                transicao: str | None = None, ultimo_quadro: str | None = None) -> str:
    """O que decide se o clipe precisa ser feito de novo (a duração não entra: cobrir a cena é conferido à parte). Sem
    transição, o hash é o de antes dela existir: os clipes já pagos continuam valendo."""
    partes = [modelo, resolucao, proporcao, imagem, movimento_] + ([transicao, ultimo_quadro] if transicao else [])
    return hashlib.sha1(json.dumps(partes, ensure_ascii=False).encode()).hexdigest()[:12]


def clipe_valido(pasta: Path, id_: str, manifesto: dict, imagem: Path | None,
                 proxima: Path | None = None) -> tuple[Path, float, bool] | None:
    """(clipe, duração, termina numa transição) da cena, se o clipe existe e foi feito a partir da imagem que ela tem
    agora; o que termina numa transição vale só se a imagem da cena seguinte (`proxima`) também é a mesma."""
    r = manifesto.get(id_) or {}
    if r.get("status") != "ok" or not r.get("arquivo") or imagem is None:
        return None
    arquivo = pasta / r["arquivo"]
    if not arquivo.is_file() or r.get("imagem") != sha_arquivo(imagem):
        return None
    if r.get("ultimo_quadro") and (proxima is None or r["ultimo_quadro"] != sha_arquivo(proxima)):
        return None
    return arquivo, float(r.get("duracao_s") or 0), bool(r.get("ultimo_quadro"))


def deslocar_cortes(cenas: list[dict], inicios: list[float], fim: float) -> list[float]:
    """Os inícios das cenas com o j-cut (a imagem troca DESLOCAMENTO_J_L_S depois da frase que começa) e o l-cut
    (antes), sem passar de um terço da cena mais curta das duas."""
    saida = list(inicios)
    fins = inicios[1:] + [fim]
    for k in range(1, len(cenas)):
        if (t := cenas[k].get("transicao")) in TRANSICOES_DE_MONTAGEM:
            folga = min(DESLOCAMENTO_J_L_S, (fins[k - 1] - inicios[k - 1]) / 3, (fins[k] - inicios[k]) / 3)
            saida[k] = inicios[k] + (folga if t == "j-cut" else -folga)
    return saida
