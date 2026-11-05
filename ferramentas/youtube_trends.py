#!/usr/bin/env python3
"""Coleta dados reais de desempenho de vídeos do YouTube para a pesquisa de trends.

Subcomandos:
  buscar "<termo>"     vídeos recentes sobre um termo, ordenados por views
  canal "<@handle>"    últimos vídeos de um canal (referência ou concorrente)
  em-alta              vídeos populares por região e categoria (só com chave de API)
  tags "<termo>" ...   tags e hashtags dos vídeos recentes que mais crescem sobre os termos, com a força de cada uma

Fontes:
  - YouTube Data API v3, se a variável YOUTUBE_API_KEY existir. Mais rápida e completa. No servidor, quem roda o
    script é a ferramenta PesquisarYoutube do motor de agentes, que injeta a chave só neste processo.
    Cota diária padrão: 10.000 unidades. Cada busca custa 100; detalhes de vídeos e canais custam 1.
  - yt-dlp, sem chave. Mais lenta (uma requisição por vídeo) e sem o ranking "em alta".

Métricas calculadas por vídeo: views por dia desde a publicação, razão views/inscritos do canal
(vídeo que superou o tamanho do canal = sinal de tema ou formato forte) e engajamento.

Uso (a partir da raiz; o yt-dlp vem das dependências do pyproject.toml):
  python3 ferramentas/youtube_trends.py buscar "comando vermelho" --dias 30 --max 25 --out dados.json
  python3 ferramentas/youtube_trends.py canal "@DorPintada" --max 30 --out canal.json
  python3 ferramentas/youtube_trends.py em-alta --regiao BR --categoria 24 --out em-alta.json
  python3 ferramentas/youtube_trends.py tags "higiene idade média" "curiosidades históricas" --formato short --out tags.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://www.googleapis.com/youtube/v3/"
AGORA = datetime.now(timezone.utc)
LIMITE_SHORT_S = 180  # Shorts podem ter até 3 min
LIMITE_LONGO_S = 480  # a partir de 8 min o vídeo aceita anúncios no meio


class ErroColeta(Exception):
    pass


# ---------------------------------------------------------------- utilidades


def iso_duracao_para_s(valor: str) -> int | None:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", valor or "")
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def fmt_duracao(segundos: int | None) -> str:
    if segundos is None:
        return "?"
    h, resto = divmod(int(segundos), 3600)
    m, s = divmod(resto, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def formato_por_duracao(segundos: int | None) -> str:
    if segundos is None:
        return "?"
    if segundos <= LIMITE_SHORT_S:
        return "short"
    return "longo" if segundos >= LIMITE_LONGO_S else "medio"


HASHTAG = re.compile(r"#(\w[\w-]*)")


def hashtags_de(*textos: str | None) -> list[str]:
    vistas: dict[str, str] = {}
    for t in textos:
        for h in HASHTAG.findall(t or ""):
            vistas.setdefault(h.lower(), h)
    return list(vistas.values())


def num(valor) -> int | None:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def enriquecer(v: dict) -> dict:
    """Calcula as métricas derivadas de um vídeo já normalizado."""
    pub = v.get("publicado_em")
    dias = None
    if pub:
        data = datetime.fromisoformat(pub).replace(tzinfo=timezone.utc)
        dias = max(1, (AGORA - data).days)
    v["dias_desde_publicacao"] = dias
    v["formato"] = formato_por_duracao(v.get("duracao_s"))
    v["duracao"] = fmt_duracao(v.get("duracao_s"))
    views, inscritos = v.get("views"), v.get("inscritos_canal")
    v["views_por_dia"] = round(views / dias) if views is not None and dias else None
    v["razao_views_inscritos"] = round(views / inscritos, 2) if views is not None and inscritos else None
    interacoes = (v.get("likes") or 0) + (v.get("comentarios") or 0)
    v["engajamento_pct"] = round(interacoes / views * 100, 2) if views else None
    return v


def filtro_sp(dias: int | None, formato: str) -> str:
    """Monta o parâmetro 'sp' da busca do YouTube: ordenar por views + data de envio + duração."""
    filtros = bytearray()
    if dias:
        periodo = 2 if dias <= 1 else 3 if dias <= 7 else 4 if dias <= 31 else 5 if dias <= 366 else None
        if periodo:
            filtros += bytes([0x08, periodo])
    filtros += bytes([0x10, 0x01])  # só vídeos
    duracao = {"short": 1, "longo": 3}.get(formato)  # 1 = menos de 4 min, 3 = de 4 a 20 min
    if duracao:
        filtros += bytes([0x18, duracao])
    proto = bytes([0x08, 0x03, 0x12, len(filtros)]) + bytes(filtros)  # 0x08 0x03 = ordenar por views
    return base64.b64encode(proto).decode()


def handle_ou_id(valor: str) -> tuple[str, str]:
    """Aceita @handle, URL de canal ou id UC... e devolve (tipo, valor)."""
    valor = valor.strip()
    if m := re.search(r"youtube\.com/(@[\w.-]+)", valor):
        return "handle", m.group(1)
    if m := re.search(r"(UC[\w-]{22})", valor):
        return "id", m.group(1)
    if valor.startswith("@"):
        return "handle", valor
    return "handle", "@" + valor


# ---------------------------------------------------------------- YouTube Data API


def api_get(endpoint: str, params: dict) -> dict:
    params = {**params, "key": os.environ["YOUTUBE_API_KEY"]}
    url = API + endpoint + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            erro = json.load(e).get("error", {})
            motivo = (erro.get("errors") or [{}])[0].get("reason", "")
            raise ErroColeta(f"API {e.code} {motivo}: {erro.get('message', '')}") from None
        except (json.JSONDecodeError, AttributeError):
            raise ErroColeta(f"API {e.code}") from None
    except urllib.error.URLError as e:
        raise ErroColeta(f"rede: {e.reason}") from None


def api_videos(ids: list[str]) -> list[dict]:
    videos = []
    for i in range(0, len(ids), 50):
        dados = api_get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(ids[i:i + 50])})
        videos += dados.get("items", [])
    return videos


def api_inscritos(canal_ids: set[str]) -> dict[str, int | None]:
    ids, resultado = sorted(canal_ids), {}
    for i in range(0, len(ids), 50):
        dados = api_get("channels", {"part": "statistics", "id": ",".join(ids[i:i + 50])})
        for c in dados.get("items", []):
            stats = c.get("statistics", {})
            resultado[c["id"]] = None if stats.get("hiddenSubscriberCount") else num(stats.get("subscriberCount"))
    return resultado


def api_normalizar(itens: list[dict]) -> list[dict]:
    inscritos = api_inscritos({i["snippet"]["channelId"] for i in itens})
    videos = []
    for it in itens:
        sn, st, cd = it["snippet"], it.get("statistics", {}), it.get("contentDetails", {})
        videos.append(enriquecer({
            "id": it["id"],
            "url": f"https://www.youtube.com/watch?v={it['id']}",
            "titulo": sn.get("title", ""),
            "canal": sn.get("channelTitle", ""),
            "canal_id": sn.get("channelId", ""),
            "inscritos_canal": inscritos.get(sn.get("channelId")),
            "publicado_em": sn.get("publishedAt", "")[:10] or None,
            "duracao_s": iso_duracao_para_s(cd.get("duration", "")),
            "views": num(st.get("viewCount")),
            "likes": num(st.get("likeCount")),
            "comentarios": num(st.get("commentCount")),
            "tags": sn.get("tags", [])[:15],
            "hashtags": hashtags_de(sn.get("title"), sn.get("description")),
        }))
    return videos


def api_buscar(args) -> list[dict]:
    params = {
        "part": "id", "q": args.termo, "type": "video", "order": "viewCount",
        "regionCode": args.regiao, "relevanceLanguage": args.idioma,
        "maxResults": min(50, args.max),
        "videoDuration": {"short": "short", "longo": "medium"}.get(args.formato, "any"),
    }
    if args.dias:
        params["publishedAfter"] = (AGORA - timedelta(days=args.dias)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids: list[str] = []
    while len(ids) < args.max:
        dados = api_get("search", params)
        ids += [i["id"]["videoId"] for i in dados.get("items", []) if i.get("id", {}).get("videoId")]
        if not dados.get("nextPageToken"):
            break
        params["pageToken"] = dados["nextPageToken"]
    return api_normalizar(api_videos(ids[:args.max]))


def api_canal(args) -> list[dict]:
    tipo, valor = handle_ou_id(args.canal)
    filtro = {"forHandle": valor} if tipo == "handle" else {"id": valor}
    dados = api_get("channels", {"part": "contentDetails", **filtro})
    if not dados.get("items"):
        raise ErroColeta(f"canal não encontrado: {args.canal}")
    uploads = dados["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, params = [], {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
    while len(ids) < args.max:
        pagina = api_get("playlistItems", params)
        ids += [i["contentDetails"]["videoId"] for i in pagina.get("items", [])]
        if not pagina.get("nextPageToken"):
            break
        params["pageToken"] = pagina["nextPageToken"]
    videos = api_normalizar(api_videos(ids[:args.max]))
    if args.aba == "shorts":
        return [v for v in videos if v["formato"] == "short"]
    if args.aba == "videos":
        return [v for v in videos if v["formato"] != "short"]
    return videos


def api_em_alta(args) -> list[dict]:
    params = {"part": "snippet,statistics,contentDetails", "chart": "mostPopular",
              "regionCode": args.regiao, "maxResults": min(50, args.max)}
    if args.categoria:
        params["videoCategoryId"] = args.categoria
    return api_normalizar(api_get("videos", params).get("items", []))


# ---------------------------------------------------------------- yt-dlp (sem chave)


class _Silencioso:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def ytdlp(opts: dict | None = None):
    import yt_dlp
    base = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": _Silencioso()}
    return yt_dlp.YoutubeDL({**base, **(opts or {})})


def ytdlp_lista(url: str, maximo: int) -> list[dict]:
    with ytdlp({"extract_flat": "in_playlist", "playlistend": maximo}) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:  # yt-dlp levanta vários tipos
            raise ErroColeta(f"yt-dlp: {str(e).splitlines()[0][:200]}") from None
    return [e for e in (info or {}).get("entries") or [] if e and e.get("id")][:maximo]


def ytdlp_detalhes(ids: list[str], avisos: list[str]) -> dict[str, dict]:
    def um(video_id: str):
        try:
            with ytdlp() as ydl:
                return video_id, ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        except Exception as e:
            avisos.append(f"{video_id}: {str(e).splitlines()[0][:160]}")
            return video_id, None
    with ThreadPoolExecutor(max_workers=4) as pool:
        return {vid: info for vid, info in pool.map(um, ids) if info}


def ytdlp_normalizar(entradas: list[dict], detalhes: dict[str, dict], inscritos_padrao: int | None = None) -> list[dict]:
    videos = []
    for e in entradas:
        d = detalhes.get(e["id"], {})
        data = d.get("upload_date") or e.get("upload_date")
        videos.append(enriquecer({
            "id": e["id"],
            "url": f"https://www.youtube.com/watch?v={e['id']}",
            "titulo": d.get("title") or e.get("title", ""),
            "canal": d.get("channel") or e.get("channel") or e.get("uploader", ""),
            "canal_id": d.get("channel_id") or e.get("channel_id", ""),
            "inscritos_canal": d.get("channel_follower_count") or inscritos_padrao,
            "publicado_em": f"{data[:4]}-{data[4:6]}-{data[6:8]}" if data else None,
            "duracao_s": num(d.get("duration") or e.get("duration")),
            "views": num(d.get("view_count") or e.get("view_count")),
            "likes": num(d.get("like_count")),
            "comentarios": num(d.get("comment_count")),
            "tags": (d.get("tags") or [])[:15],
            "hashtags": hashtags_de(d.get("title") or e.get("title"), d.get("description")),
        }))
    return videos


def ytdlp_buscar(args, avisos: list[str]) -> list[dict]:
    url = "https://www.youtube.com/results?" + urllib.parse.urlencode(
        {"search_query": args.termo, "sp": filtro_sp(args.dias, args.formato), "gl": args.regiao, "hl": args.idioma})
    entradas = ytdlp_lista(url, args.max)
    detalhes = {} if args.rapido else ytdlp_detalhes([e["id"] for e in entradas], avisos)
    videos = ytdlp_normalizar(entradas, detalhes)
    if args.dias and not args.rapido:  # o filtro do YouTube é por faixa; aplica o limite exato
        videos = [v for v in videos if v["dias_desde_publicacao"] is None or v["dias_desde_publicacao"] <= args.dias]
    return videos


def ytdlp_canal(args, avisos: list[str]) -> list[dict]:
    tipo, valor = handle_ou_id(args.canal)
    base = f"https://www.youtube.com/{valor}" if tipo == "handle" else f"https://www.youtube.com/channel/{valor}"
    abas = ["videos", "shorts"] if args.aba == "todas" else [args.aba]
    entradas = []
    for aba in abas:
        try:
            entradas += ytdlp_lista(f"{base}/{aba}", args.max)
        except ErroColeta as e:
            avisos.append(f"aba {aba}: {e}")
    detalhes = {} if args.rapido else ytdlp_detalhes([e["id"] for e in entradas], avisos)
    inscritos = next((d.get("channel_follower_count") for d in detalhes.values() if d.get("channel_follower_count")), None)
    return ytdlp_normalizar(entradas, detalhes, inscritos)


# ---------------------------------------------------------------- tags em alta


def chave_tag(tag: str) -> str:
    import unicodedata
    sem = unicodedata.normalize("NFKD", tag).encode("ascii", "ignore").decode()
    return re.sub(r"[\s_#-]+", " ", sem.lower()).strip()


def agregar_tags(videos: list[dict], campo: str, limite: int = 40) -> list[dict]:
    """Junta as tags (ou hashtags) dos vídeos: em quantos aparecem e quantas views por dia esses vídeos somam.
    A força de uma tag é o crescimento dos vídeos que a usam, não só a contagem."""
    grupos: dict[str, dict] = {}
    for v in videos:
        for tag in {chave_tag(t): t for t in v.get(campo) or [] if chave_tag(t)}.values():
            g = grupos.setdefault(chave_tag(tag), {"grafias": {}, "videos": 0, "views_por_dia": 0, "exemplo": None})
            g["grafias"][tag] = g["grafias"].get(tag, 0) + 1
            g["videos"] += 1
            g["views_por_dia"] += v.get("views_por_dia") or 0
            if g["exemplo"] is None or (v.get("views_por_dia") or 0) > g["exemplo"]["views_por_dia"]:
                g["exemplo"] = {"titulo": v["titulo"], "url": v["url"], "views_por_dia": v.get("views_por_dia") or 0}
    saida = [{"tag": max(g["grafias"], key=g["grafias"].get), "videos": g["videos"], "views_por_dia": g["views_por_dia"],
              "exemplo": g["exemplo"]} for g in grupos.values() if g["videos"] >= 2]  # tag de um vídeo só é do canal, não do tema
    return sorted(saida, key=lambda t: (t["views_por_dia"], t["videos"]), reverse=True)[:limite]


def coletar_tags(args, usar_api: bool, avisos: list[str]) -> list[dict]:
    vistos: dict[str, dict] = {}
    for termo in args.termos:
        busca = argparse.Namespace(**{**vars(args), "termo": termo})
        try:
            videos = api_buscar(busca) if usar_api else ytdlp_buscar(busca, avisos)
        except ErroColeta as e:
            avisos.append(f"{termo}: {e}")
            continue
        for v in videos:
            vistos.setdefault(v["id"], v)
    return list(vistos.values())


def tags_md(resultado: dict, limite: int = 25) -> str:
    linhas = [f"fonte: {resultado['fonte']} · {resultado['n_videos']} vídeos · coletado em {resultado['coletado_em']}"]
    linhas += [f"aviso: {a}" for a in resultado["avisos"][:10]]
    for titulo, campo in (("Tags", "tags"), ("Hashtags", "hashtags")):
        linhas += ["", f"## {titulo}", "", "| Tag | Vídeos | Views/dia somadas | Exemplo |", "|---|---|---|---|"]
        linhas += [f"| {t['tag']} | {t['videos']} | {fmt_num(t['views_por_dia'])} | [{t['exemplo']['titulo'][:50].replace('|', '/')}]({t['exemplo']['url']}) |"
                   for t in resultado[campo][:limite]] or ["| (nenhuma em dois vídeos ou mais) | | | |"]
    return "\n".join(linhas)


# ---------------------------------------------------------------- saída


def ordenar(videos: list[dict], chave: str) -> list[dict]:
    campo = {"views": "views", "views_por_dia": "views_por_dia", "outlier": "razao_views_inscritos"}[chave]
    return sorted(videos, key=lambda v: v.get(campo) or 0, reverse=True)


def fmt_num(n) -> str:
    if n is None:
        return "?"
    if isinstance(n, float):
        return f"{n:.1f}".replace(".", ",")
    return f"{n:,}".replace(",", ".")


def resumo_md(resultado: dict, limite: int = 25) -> str:
    videos = resultado["videos"]
    linhas = [f"fonte: {resultado['fonte']} · {len(videos)} vídeos · coletado em {resultado['coletado_em']}"]
    linhas += [f"aviso: {a}" for a in resultado["avisos"][:10]]
    linhas += ["", "| # | Título | Canal | Inscritos | Publicado | Dur. | Formato | Views | Views/dia | Views/insc. |",
               "|---|---|---|---|---|---|---|---|---|---|"]
    for i, v in enumerate(videos[:limite], 1):
        titulo = v["titulo"].replace("|", "/")[:70]
        linhas.append(f"| {i} | [{titulo}]({v['url']}) | {v['canal']} | {fmt_num(v['inscritos_canal'])} | {v['publicado_em'] or '?'} | "
                      f"{v['duracao']} | {v['formato']} | {fmt_num(v['views'])} | {fmt_num(v['views_por_dia'])} | {fmt_num(v['razao_views_inscritos'])} |")
    outliers = [v for v in videos if (v.get("razao_views_inscritos") or 0) >= 3]
    if outliers:
        linhas += ["", "Outliers (views ≥ 3× os inscritos do canal):"]
        linhas += [f"- {v['titulo']} — {fmt_num(v['razao_views_inscritos'])}× ({v['canal']})"
                   for v in ordenar(outliers, "outlier")[:10]]
    return "\n".join(linhas)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    comum = argparse.ArgumentParser(add_help=False)
    comum.add_argument("--max", type=int, default=25, help="número máximo de vídeos (padrão 25)")
    comum.add_argument("--out", type=Path, help="salva o resultado completo em JSON neste caminho")
    comum.add_argument("--ordenar", choices=["views", "views_por_dia", "outlier"], help="ordem da saída")
    comum.add_argument("--fonte", choices=["auto", "api", "yt-dlp"], default="auto")
    comum.add_argument("--rapido", action="store_true", help="yt-dlp: não busca detalhes por vídeo (sem data, likes e inscritos)")

    b = sub.add_parser("buscar", parents=[comum], help="vídeos recentes sobre um termo")
    b.add_argument("termo")
    b.add_argument("--dias", type=int, default=30, help="só vídeos publicados nos últimos N dias (0 = sem limite)")
    b.add_argument("--formato", choices=["todos", "short", "longo"], default="todos")
    b.add_argument("--regiao", default="BR")
    b.add_argument("--idioma", default="pt")

    c = sub.add_parser("canal", parents=[comum], help="últimos vídeos de um canal")
    c.add_argument("canal", help="@handle, URL do canal ou id UC...")
    c.add_argument("--aba", choices=["videos", "shorts", "todas"], default="videos")

    e = sub.add_parser("em-alta", parents=[comum], help="vídeos populares (exige YOUTUBE_API_KEY)")
    e.add_argument("--regiao", default="BR")
    e.add_argument("--categoria", help="id da categoria (ex.: 24 Entretenimento, 27 Educação, 22 Pessoas e blogs)")

    t = sub.add_parser("tags", parents=[comum], help="tags e hashtags em alta nos vídeos recentes sobre os termos")
    t.add_argument("termos", nargs="+", help="um ou mais termos de busca (o tema e variações)")
    t.add_argument("--dias", type=int, default=30, help="só vídeos publicados nos últimos N dias (0 = sem limite)")
    t.add_argument("--formato", choices=["todos", "short", "longo"], default="todos")
    t.add_argument("--regiao", default="BR")
    t.add_argument("--idioma", default="pt")

    args = ap.parse_args()
    usar_api = args.fonte == "api" or (args.fonte == "auto" and os.environ.get("YOUTUBE_API_KEY"))
    if usar_api and not os.environ.get("YOUTUBE_API_KEY"):
        print("erro: --fonte api exige a variável YOUTUBE_API_KEY", file=sys.stderr)
        return 2
    if getattr(args, "dias", None) == 0:
        args.dias = None

    avisos: list[str] = []
    if args.cmd == "tags":
        videos = coletar_tags(args, usar_api, avisos)
        resultado = {"consulta": {k: v for k, v in vars(args).items() if k not in ("out", "fonte") and v is not None},
                     "fonte": "youtube-data-api" if usar_api else "yt-dlp",
                     "coletado_em": AGORA.strftime("%Y-%m-%d %H:%M UTC"), "avisos": avisos, "n_videos": len(videos),
                     "tags": agregar_tags(videos, "tags"), "hashtags": agregar_tags(videos, "hashtags")}
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"arquivo: {args.out.resolve()}")
        print(tags_md(resultado))
        return 0 if videos else 1
    try:
        if args.cmd == "buscar":
            videos = api_buscar(args) if usar_api else ytdlp_buscar(args, avisos)
            if args.formato != "todos":
                videos = [v for v in videos if v["formato"] == args.formato or (args.formato == "longo" and v["formato"] == "medio")]
        elif args.cmd == "canal":
            videos = api_canal(args) if usar_api else ytdlp_canal(args, avisos)
        else:
            if not usar_api:
                print("erro: 'em-alta' só funciona com YOUTUBE_API_KEY (a página de em alta do YouTube não existe mais)", file=sys.stderr)
                return 2
            videos = api_em_alta(args)
    except ErroColeta as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1

    padrao = {"buscar": "views_por_dia", "canal": "views", "em-alta": "views_por_dia"}[args.cmd]
    videos = ordenar(videos, args.ordenar or padrao)
    consulta = {k: v for k, v in vars(args).items() if k not in ("out", "fonte") and v is not None}
    consulta["out"] = str(args.out) if args.out else None
    resultado = {
        "consulta": consulta,
        "fonte": "youtube-data-api" if usar_api else "yt-dlp",
        "coletado_em": AGORA.strftime("%Y-%m-%d %H:%M UTC"),
        "avisos": avisos,
        "videos": videos,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(resultado, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"arquivo: {args.out.resolve()}")
    print(resumo_md(resultado))
    return 0 if videos else 1


if __name__ == "__main__":
    sys.exit(main())
