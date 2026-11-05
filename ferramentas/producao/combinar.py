#!/usr/bin/env python3
"""Combina as peças da etapa Edição num arquivo para publicar.

As peças (nucleo/edicao.py): o vídeo sem som e sem legendas (10-video/base/video.mp4), uma faixa de voz (a narração
original ou uma dublagem de 12-dublagens/), a música de fundo (qualquer uma da biblioteca de músicas, com volume e
ponto de início) e, se pedido, as legendas de uma das línguas queimadas na imagem. Grava em 13-exportacoes/:

  <nome>.mp4   o vídeo combinado (--saidas video)
  <nome>.mp3   só o áudio, voz e música, com a duração exata do vídeo (--saidas audio): o arquivo que o YouTube pede
               para uma faixa de áudio em outra língua (YouTube Studio > Legendas > Idiomas > Dublagem)
  <nome>.srt   as legendas da língua da voz, para subir junto
  <nome>.json  a receita (o que entrou) e o resultado

A mistura é a da montagem (montar_video.grafo_audio): a voz normalizada a -14 LUFS e a música no volume pedido o
tempo todo, como na prévia, com fade no começo e no fim (curto no short, que emenda no replay). Os efeitos
sonoros que a montagem pôs (10-video/efeitos.json) entram nos mesmos instantes, com qualquer voz: eles seguem as
imagens, que são as mesmas (--sem-efeitos tira).

Uso:
    python3 ferramentas/producao/combinar.py projetos/<slug> --voz en
    python3 ferramentas/producao/combinar.py projetos/<slug> --voz en --musica biblioteca/musicas/tema.mp3 --volume 15
    python3 ferramentas/producao/combinar.py projetos/<slug> --voz original --musica nenhuma --legendas en --saidas video
    python3 ferramentas/producao/combinar.py projetos/<slug> --listar

--voz e --legendas aceitam "original", o código de uma língua dublada ou "nenhuma"; --musica aceita um arquivo, "pedido"
(a do pedido, o padrão) ou "nenhuma". Códigos de saída: 0 ok; 1 erro do ffmpeg; 2 falta uma peça.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "ferramentas"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import efeitos  # noqa: E402
from brief import achar_musica, config, espaco, ler_brief  # noqa: E402
import montar_video  # noqa: E402
from nucleo import edicao  # noqa: E402

CODIFICAR_VIDEO = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
AAC = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
MP3 = ["-c:a", "libmp3lame", "-b:a", "192k", "-ar", "48000"]


def duracao_do_video(video: Path) -> float | None:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
                        "-of", "csv=p=0", str(video)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def duracao_da_midia(caminho: Path) -> float | None:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(caminho)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def achar_arquivo(caminho: str, pasta: Path) -> Path | None:
    for base in (Path.cwd(), espaco(), pasta):
        if (base / caminho).is_file():
            return (base / caminho).resolve()
    return None


def efeitos_do_video(pasta: Path) -> list[tuple[Path, float, float]]:
    """Os efeitos sonoros que a montagem pôs no vídeo (10-video/efeitos.json), como montar_video.grafo_audio os pede.
    O que saiu da biblioteca de efeitos depois da montagem fica de fora, com aviso."""
    try:
        usados = json.loads((pasta / "10-video" / montar_video.EFEITOS_USADOS).read_text(encoding="utf-8")).get("efeitos") or []
    except (OSError, ValueError, AttributeError):
        return []
    sons = []
    for u in usados:
        caminho = str(u.get("arquivo") or "")
        nome = caminho[len(efeitos.PASTA):] if caminho.startswith(efeitos.PASTA) else ""
        arquivo = espaco() / caminho
        if not nome or "/" in nome or not arquivo.is_file():
            print(f"aviso: o efeito sonoro '{u.get('nome') or u.get('id')}' não está mais na biblioteca de efeitos; "
                  "a exportação sai sem ele", file=sys.stderr)
            continue
        sons.append((arquivo, float(u["inicio_s"]), float(u["ganho_db"])))
    return sons


def listar(pasta: Path, brief: dict) -> int:
    base = pasta / edicao.VIDEO_BASE
    print(f"vídeo-base: {edicao.VIDEO_BASE if base.exists() else '(não existe: montar_video.py --so-base)'}")
    for f in edicao.faixas(pasta, brief):
        extras = [x for x in (f["status"], f.get("desatualizada") and "desatualizada") if x]
        print(f"voz {f['id']:9} {f['nome']:30} {f['audio'] or '(sem áudio)'} · {', '.join(extras)}")
    for e in edicao.exportacoes(pasta):
        print(f"exportação {e['nome']}: {', '.join(e['arquivos'].values())}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pasta", type=Path, help="pasta do projeto (projetos/<slug>)")
    ap.add_argument("--voz", default=edicao.ORIGINAL, help="original, o código de uma língua dublada, ou nenhuma")
    ap.add_argument("--musica", default="pedido", help="arquivo de música, pedido (a do pedido) ou nenhuma")
    ap.add_argument("--volume", type=float, help="volume da música por baixo da voz, em %% (padrão: o do pedido, ou 20)")
    ap.add_argument("--inicio", help="ponto da música em que ela começa, m:ss (padrão: o do pedido)")
    ap.add_argument("--legendas", default=edicao.NENHUMA, help="legendas queimadas na imagem: original, uma língua ou nenhuma")
    ap.add_argument("--sem-efeitos", action="store_true", help="sem os efeitos sonoros que a montagem pôs no vídeo")
    ap.add_argument("--saidas", default="video,audio", help="video (mp4), audio (mp3) ou as duas, separadas por vírgula")
    ap.add_argument("--nome", help="nome dos arquivos em 13-exportacoes/ (padrão: pela combinação)")
    ap.add_argument("--listar", action="store_true", help="lista as peças do projeto e as exportações")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        print("erro: ffmpeg não encontrado", file=sys.stderr)
        return 2
    pasta = args.pasta
    brief = ler_brief(pasta)
    if args.listar:
        return listar(pasta, brief)
    base = pasta / edicao.VIDEO_BASE
    if not base.exists():
        print(f"erro: {base} não existe; monte o vídeo-base com montar_video.py {pasta} --so-base", file=sys.stderr)
        return 2
    saidas = [s.strip() for s in args.saidas.split(",") if s.strip()]
    if not saidas or set(saidas) - set(edicao.SAIDAS):
        print(f"erro: --saidas aceita {', '.join(edicao.SAIDAS)}", file=sys.stderr)
        return 2

    faixa_voz = None
    if args.voz != edicao.NENHUMA:
        faixa_voz = edicao.faixa(pasta, brief, args.voz)
        if not faixa_voz or not faixa_voz["audio"]:
            print(f"erro: a faixa de voz '{args.voz}' não existe (veja --listar)", file=sys.stderr)
            return 2
    faixa_legendas = None
    if args.legendas != edicao.NENHUMA:
        faixa_legendas = edicao.faixa(pasta, brief, args.legendas)
        if not faixa_legendas or not faixa_legendas["ass"]:
            print(f"erro: não há legendas da faixa '{args.legendas}' para queimar na imagem", file=sys.stderr)
            return 2
    musica = None
    if args.musica == "pedido":
        if config(brief, "musica.ativa", False):
            musica = achar_musica(brief.get("musica") or {}, pasta)
            if musica is None:
                print("aviso: a música do pedido não está na biblioteca de músicas; o arquivo sai sem música", file=sys.stderr)
    elif args.musica != edicao.NENHUMA:
        musica = achar_arquivo(args.musica, pasta)
        if musica is None:
            print(f"erro: a música {args.musica} não existe", file=sys.stderr)
            return 2
    sons = [] if args.sem_efeitos else efeitos_do_video(pasta)
    if "audio" in saidas and not faixa_voz and not musica and not sons:
        print("erro: sem voz, sem música e sem efeitos não há áudio para exportar", file=sys.stderr)
        return 2

    duracao = duracao_do_video(base)
    if not duracao:
        print(f"erro: não consegui ler a duração de {base}", file=sys.stderr)
        return 2
    volume = (args.volume if args.volume is not None else config(brief, "musica.volume", 20)) / 100
    inicio = montar_video.segundos(args.inicio or config(brief, "musica.inicio"))
    corte_seco = brief.get("formato") == "short"
    voz = (pasta / faixa_voz["audio"]).resolve() if faixa_voz else None
    nome = edicao.nome_de_exportacao(args.nome or "-".join(
        [args.voz if faixa_voz else "sem-voz", "com-musica" if musica else "sem-musica"]
        + ([f"legendas-{args.legendas}"] if faixa_legendas else [])))
    destino = pasta / edicao.EXPORTACOES
    destino.mkdir(parents=True, exist_ok=True)

    entradas, filtros = montar_video.grafo_audio(voz, musica, inicio, volume, duracao, corte_seco, sons)
    grafo = list(filtros)
    rotulos = {"video": "[av]", "audio": "[am]"}
    if filtros:  # o áudio exatamente do tamanho do vídeo: completa com silêncio e corta o que passar
        ajuste = f"[a]apad=whole_dur={duracao:.3f},atrim=end={duracao:.3f},asetpts=N/SR/TB"
        grafo.append(ajuste + (",asplit=2[av][am]" if len(saidas) == 2 else rotulos[saidas[0]]))
    with tempfile.TemporaryDirectory(prefix="exportacao-") as tmp:
        tmp = Path(tmp)
        queimar = faixa_legendas is not None and "video" in saidas  # a legenda na imagem só existe no mp4
        if queimar:
            shutil.copyfile(pasta / faixa_legendas["ass"], tmp / "legendas.ass")
            grafo.insert(0, "[0:v]subtitles=legendas.ass[v]")
        comando = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(base.resolve()), *entradas]
        if grafo:
            comando += ["-filter_complex", ";".join(grafo)]
        if "video" in saidas:
            comando += (["-map", "[v]", *CODIFICAR_VIDEO] if queimar else ["-map", "0:v:0", "-c:v", "copy"])
            comando += (["-map", "[av]", *AAC] if filtros else ["-an"]) + ["-movflags", "+faststart", "video.mp4"]
        if "audio" in saidas:
            comando += ["-map", "[am]", "-vn", *MP3, "audio.mp3"]
        print(f"exportando {nome}: voz {faixa_voz['nome'] if faixa_voz else 'nenhuma'} · "
              f"música {musica.name + f' a {volume:.0%}' if musica else 'nenhuma'} · efeitos sonoros: {len(sons)} · "
              f"legendas na imagem: {faixa_legendas['nome'] if queimar else 'nenhuma'} · {duracao:.1f} s", flush=True)
        r = subprocess.run(comando, cwd=tmp, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"erro do ffmpeg: {r.stderr.strip()[-1500:]}", file=sys.stderr)
            return 1
        arquivos = {}
        for tipo, ext in edicao.SAIDAS.items():
            alvo = destino / f"{nome}.{ext}"
            if tipo in saidas:  # gravado no temporário e movido pronto: a sincronização nunca leva um arquivo pela metade
                os.replace(tmp / f"{tipo}.{ext}", alvo)
                arquivos[tipo] = f"{edicao.EXPORTACOES}/{alvo.name}"
            else:
                alvo.unlink(missing_ok=True)  # de uma exportação anterior com o mesmo nome
    srt = (faixa_voz or faixa_legendas or {}).get("srt")
    if srt:
        shutil.copyfile(pasta / srt, destino / f"{nome}.srt")
        arquivos["srt"] = f"{edicao.EXPORTACOES}/{nome}.srt"
    else:
        (destino / f"{nome}.srt").unlink(missing_ok=True)

    receita = {"voz": faixa_voz["id"] if faixa_voz else None,
               "musica": str(musica.relative_to(espaco())) if musica and musica.is_relative_to(espaco()) else (musica.name if musica else None),
               "volume": round(volume * 100), "inicio": args.inicio or config(brief, "musica.inicio") or "0:00",
               "legendas": faixa_legendas["id"] if queimar else None, "efeitos": len(sons), "saidas": saidas}
    doc = {"nome": nome, "criada_em": time.strftime("%Y-%m-%dT%H:%M:%S"), "receita": receita, "arquivos": arquivos,
           "duracao_s": round(duracao, 3), "idioma": (faixa_voz or {}).get("idioma"),
           "voz": {k: (faixa_voz or {}).get(k) for k in ("fornecedor", "voz", "status")} if faixa_voz else None}
    (destino / f"{nome}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for tipo, caminho in arquivos.items():
        d = duracao_da_midia(pasta / caminho) if tipo != "srt" else None
        print(f"{tipo}: {pasta / caminho}" + (f" · {d:.2f} s" if d else ""))
    if faixa_voz and faixa_voz.get("desatualizada"):
        print(f"aviso: a dublagem {faixa_voz['id']} está desatualizada ({faixa_voz['desatualizada']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
