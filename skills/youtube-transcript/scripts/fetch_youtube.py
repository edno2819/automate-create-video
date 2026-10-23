#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "youtube-transcript-api>=1.2",
#     "yt-dlp",
# ]
# ///
"""Baixa metadados, descrição e transcrição completa de um vídeo do YouTube.

Estratégia:
  1. Metadados + descrição via yt-dlp (sem baixar o vídeo).
  2. Transcrição via youtube-transcript-api.
  3. Se (2) falhar, transcrição via faixas de legenda expostas pelo yt-dlp.

Uso:
  uv run fetch_youtube.py <url-ou-id> [--lang pt en] [--out DIR] [--plain] [--json]
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
PATH_PREFIXES = {"shorts", "embed", "live", "v", "e"}


def extract_video_id(value: str) -> str:
    value = value.strip()
    if ID_RE.match(value):
        return value
    if "://" not in value:
        value = "https://" + value
    url = urlparse(value)
    host = (url.hostname or "").lower()
    for prefix in ("www.", "m."):
        host = host.removeprefix(prefix)

    candidate = ""
    if host == "youtu.be":
        candidate = url.path.lstrip("/").split("/")[0]
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        query = parse_qs(url.query)
        if "v" in query:
            candidate = query["v"][0]
        else:
            parts = [p for p in url.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in PATH_PREFIXES:
                candidate = parts[1]

    if not ID_RE.match(candidate):
        raise ValueError(f"não consegui extrair o ID do vídeo de: {value}")
    return candidate


def lang_rank(code: str, prefs: list[str]) -> int | None:
    """Posição do idioma na lista de preferência (menor = melhor); None se não bate."""
    code = code.lower().removesuffix("-orig")
    for i, pref in enumerate(prefs):
        pref = pref.lower()
        if code == pref:
            return i * 2
        if code.split("-")[0] == pref.split("-")[0]:
            return i * 2 + 1
    return None


def fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def short_error(exc: Exception) -> str:
    """Resume a exceção; as do youtube-transcript-api trazem a causa no 2º parágrafo."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", str(exc)) if p.strip()]
    if len(parts) >= 2 and "most likely caused by" in parts[0]:
        detail = parts[1]
    else:
        detail = parts[0] if parts else ""
    detail = " ".join(detail.split()).removeprefix("ERROR: ")
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


class _SilentLogger:
    """Evita que o yt-dlp imprima 'ERROR: ...' por conta própria; o erro vira exceção."""

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-")


# --------------------------------------------------------------------------- metadados


def fetch_info(video_id: str, cookies_from_browser: str | None):
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "logger": _SilentLogger(),
    }
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    ydl = yt_dlp.YoutubeDL(opts)
    info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    return ydl, info


# --------------------------------------------------------------------------- transcrição


def transcript_via_api(video_id: str, prefs: list[str]) -> dict:
    from youtube_transcript_api import YouTubeTranscriptApi

    transcripts = list(YouTubeTranscriptApi().list(video_id))
    if not transcripts:
        raise RuntimeError("nenhuma transcrição disponível")

    def key(t):
        rank = lang_rank(t.language_code, prefs)
        # idioma preferido primeiro; no mesmo idioma, manual antes de automática
        return (rank is None, rank or 0, t.is_generated)

    best = min(transcripts, key=key)
    fetched = best.fetch()
    return {
        "source": "youtube-transcript-api",
        "language": best.language,
        "language_code": best.language_code,
        "is_generated": best.is_generated,
        "available": [
            f"{t.language_code}{' (auto)' if t.is_generated else ''}" for t in transcripts
        ],
        "segments": [
            {"start": s.start, "duration": s.duration, "text": s.text} for s in fetched
        ],
    }


def transcript_via_ytdlp(ydl, info: dict, prefs: list[str]) -> dict:
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    # Legendas automáticas vêm traduzidas para dezenas de idiomas; fica só com a original.
    auto_orig = {k: v for k, v in auto.items() if k.endswith("-orig")}
    if not auto_orig and info.get("language") in auto:
        auto_orig = {info["language"]: auto[info["language"]]}

    candidates = [(code, tracks, False) for code, tracks in manual.items()]
    candidates += [(code, tracks, True) for code, tracks in auto_orig.items()]
    if not candidates:
        raise RuntimeError("o vídeo não expõe faixas de legenda")

    def key(c):
        rank = lang_rank(c[0], prefs)
        return (rank is None, rank or 0, c[2])

    code, tracks, is_generated = min(candidates, key=key)
    by_ext = {t.get("ext"): t for t in tracks}
    track = by_ext.get("json3") or by_ext.get("vtt")
    if not track:
        raise RuntimeError(f"nenhum formato suportado para a legenda {code}")

    raw = ydl.urlopen(track["url"]).read().decode("utf-8", "replace")
    segments = parse_json3(raw) if track["ext"] == "json3" else parse_vtt(raw)
    return {
        "source": "yt-dlp",
        "language": track.get("name") or code,
        "language_code": code.removesuffix("-orig"),
        "is_generated": is_generated,
        "available": sorted(manual) + [f"{c.removesuffix('-orig')} (auto)" for c in auto_orig],
        "segments": segments,
    }


def parse_json3(raw: str) -> list[dict]:
    segments = []
    for event in json.loads(raw).get("events", []):
        text = "".join(s.get("utf8", "") for s in event.get("segs") or [])
        text = " ".join(text.split())
        if text:
            segments.append(
                {
                    "start": event.get("tStartMs", 0) / 1000,
                    "duration": event.get("dDurationMs", 0) / 1000,
                    "text": html.unescape(text),
                }
            )
    return segments


def parse_vtt(raw: str) -> list[dict]:
    def to_seconds(ts: str) -> float:
        parts = [float(p) for p in ts.replace(",", ".").split(":")]
        while len(parts) < 3:
            parts.insert(0, 0.0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]

    segments, last = [], None
    for block in re.split(r"\n\s*\n", raw):
        lines = block.strip().splitlines()
        idx = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if idx is None:
            continue
        start = to_seconds(lines[idx].split("-->")[0].strip())
        for line in lines[idx + 1 :]:
            text = html.unescape(re.sub(r"<[^>]+>", "", line)).strip()
            # legendas automáticas em VTT repetem a linha anterior (efeito "rolagem")
            if text and text != last:
                segments.append({"start": start, "duration": 0.0, "text": text})
                last = text
    return segments


def to_paragraphs(segments: list[dict], target_chars: int = 600) -> list[tuple[float, str]]:
    """Agrupa os trechos em parágrafos, quebrando preferencialmente em fim de frase."""
    paragraphs, current, start, size = [], [], 0.0, 0
    for seg in segments:
        text = " ".join(seg["text"].split())
        if not text:
            continue
        if not current:
            start = seg["start"]
        current.append(text)
        size += len(text) + 1
        ends_sentence = text[-1] in ".!?…"
        if size >= target_chars and (ends_sentence or size >= target_chars * 1.6):
            paragraphs.append((start, " ".join(current)))
            current, size = [], 0
    if current:
        paragraphs.append((start, " ".join(current)))
    return paragraphs


# --------------------------------------------------------------------------- saída


def build_markdown(video_id: str, info: dict | None, transcript: dict | None,
                   errors: list[str], plain: bool) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    info = info or {}
    out = [f"# {info.get('title') or video_id}", ""]

    meta = [("URL", url)]
    if info.get("channel") or info.get("uploader"):
        meta.append(("Canal", info.get("channel") or info.get("uploader")))
    if info.get("upload_date"):
        d = info["upload_date"]
        meta.append(("Publicado em", f"{d[:4]}-{d[4:6]}-{d[6:]}"))
    if info.get("duration"):
        meta.append(("Duração", fmt_ts(info["duration"])))
    if info.get("view_count") is not None:
        meta.append(("Visualizações", f"{info['view_count']:,}".replace(",", ".")))
    if transcript:
        kind = "gerada automaticamente" if transcript["is_generated"] else "manual"
        meta.append(
            ("Transcrição",
             f"{transcript['language']} [{transcript['language_code']}], {kind}, via {transcript['source']}")
        )
        meta.append(("Idiomas disponíveis", ", ".join(transcript["available"])))
    out += [f"- **{k}:** {v}" for k, v in meta] + [""]

    if info.get("tags"):
        out += [f"**Tags:** {', '.join(info['tags'])}", ""]

    out += ["## Descrição", ""]
    if info.get("description"):
        out += ["````text", info["description"].strip(), "````", ""]
    else:
        out += ["_(sem descrição disponível)_", ""]

    if info.get("chapters"):
        out += ["## Capítulos", ""]
        out += [f"- [{fmt_ts(c['start_time'])}] {c['title']}" for c in info["chapters"]]
        out.append("")

    out += ["## Transcrição", ""]
    if transcript and transcript["segments"]:
        for start, text in to_paragraphs(transcript["segments"]):
            out += [text if plain else f"**[{fmt_ts(start)}]** {text}", ""]
    else:
        out += ["_(transcrição indisponível)_", ""]

    if errors:
        out += ["## Problemas encontrados", ""] + [f"- {e}" for e in errors] + [""]
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video", help="URL do YouTube (watch, youtu.be, shorts, embed, live) ou ID")
    parser.add_argument("--lang", nargs="+", default=["pt", "en"],
                        help="idiomas preferidos, em ordem (padrão: pt en)")
    parser.add_argument("--out", default="transcripts", help="pasta de saída (padrão: ./transcripts)")
    parser.add_argument("--plain", action="store_true", help="transcrição sem marcas de tempo")
    parser.add_argument("--json", action="store_true", help="também salva um .json com os dados brutos")
    parser.add_argument("--cookies-from-browser", metavar="BROWSER",
                        help="usa cookies do navegador no yt-dlp (chrome, firefox, ...)")
    args = parser.parse_args()

    try:
        video_id = extract_video_id(args.video)
    except ValueError as e:
        print(f"ERRO: {e}", file=sys.stderr)
        return 2

    errors: list[str] = []
    ydl = info = transcript = None

    try:
        ydl, info = fetch_info(video_id, args.cookies_from_browser)
    except Exception as e:  # noqa: BLE001 — yt-dlp levanta vários tipos
        errors.append(f"metadados/descrição (yt-dlp): {short_error(e)}")

    try:
        transcript = transcript_via_api(video_id, args.lang)
    except Exception as e:  # noqa: BLE001
        errors.append(f"transcrição (youtube-transcript-api): {short_error(e)}")
        if info is not None:
            try:
                transcript = transcript_via_ytdlp(ydl, info, args.lang)
            except Exception as e2:  # noqa: BLE001
                errors.append(f"transcrição (yt-dlp): {short_error(e2)}")

    if info is None and transcript is None:
        print("ERRO: não foi possível obter nada do vídeo.", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify((info or {}).get("title") or "")
    stem = f"{slug}-{video_id}" if slug else video_id
    md_path = out_dir / f"{stem}.md"
    md_path.write_text(build_markdown(video_id, info, transcript, errors, args.plain), encoding="utf-8")

    if args.json:
        payload = {
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": (info or {}).get("title"),
            "channel": (info or {}).get("channel") or (info or {}).get("uploader"),
            "upload_date": (info or {}).get("upload_date"),
            "duration": (info or {}).get("duration"),
            "description": (info or {}).get("description"),
            "chapters": (info or {}).get("chapters"),
            "tags": (info or {}).get("tags"),
            "transcript": transcript,
            "errors": errors,
        }
        (out_dir / f"{stem}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    words = sum(len(s["text"].split()) for s in transcript["segments"]) if transcript else 0
    print(f"arquivo: {md_path.resolve()}")
    if args.json:
        print(f"json: {(out_dir / f'{stem}.json').resolve()}")
    print(f"título: {(info or {}).get('title') or '(desconhecido)'}")
    print(f"descrição: {'ok' if (info or {}).get('description') else 'indisponível'}")
    if transcript:
        kind = "auto" if transcript["is_generated"] else "manual"
        print(f"transcrição: {transcript['language_code']} ({kind}) via {transcript['source']}, ~{words} palavras")
    else:
        print("transcrição: indisponível")
    for e in errors:
        print(f"aviso: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
