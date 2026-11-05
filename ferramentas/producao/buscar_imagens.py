#!/usr/bin/env python3
"""Busca na internet as imagens das cenas marcadas com "fonte": "internet", em bancos de imagens livres.

Lê projetos/<slug>/08-cenas.json e, para cada cena com "fonte": "internet", procura a "busca" da cena no
Wikimedia Commons e no Openverse, só entre imagens de domínio público ou com licença Creative Commons que
permite uso comercial e adaptação (o vídeo pode ser monetizado, e a imagem é recortada e animada). A busca
pode ser uma lista de alternativas, da mais específica à mais geral: vale a primeira que trouxer resultado.
Grava:

  09-imagens/<id>.jpg         a imagem escolhida para a cena
  09-imagens/manifesto.json   fonte, autor, licença e link de cada imagem (o mesmo manifesto das geradas)
  09-imagens/creditos.md      os créditos, prontos para colar na descrição do vídeo

Entre os resultados, prefere os primeiros da busca, com boa resolução e proporção parecida com a do vídeo
(a montagem encaixa as outras inteiras, sobre um fundo desfocado). As outras candidatas ficam no manifesto:
--opcao N troca a imagem da cena pela candidata N, sem buscar de novo.

Cena sem resultado fica com "status": "nao-encontrada" no manifesto; aí gerar_imagens.py gera a imagem
pelo prompt de reserva da cena, se houver.

Uso:
    python3 ferramentas/producao/buscar_imagens.py projetos/<slug> --simular
    python3 ferramentas/producao/buscar_imagens.py projetos/<slug>
    python3 ferramentas/producao/buscar_imagens.py projetos/<slug> --cenas B02-01 --opcao 2
    python3 ferramentas/producao/buscar_imagens.py projetos/<slug> --cenas B02-01 --refazer

Só usa a biblioteca padrão (e o ffmpeg, para converter formatos que a montagem não lê).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "ferramentas"))
from brief import config, ler_brief  # noqa: E402

AGENTE = "story-automate-youtube/1.0 (gerador de videos narrados; uso pessoal)"  # o Wikimedia pede um User-Agent próprio
COMMONS = "https://commons.wikimedia.org/w/api.php"
OPENVERSE = "https://api.openverse.org/v1/images/"
FONTES = ("wikimedia", "openverse")
LARGURA_DOWNLOAD = 2560
MIN_LADO_MENOR, MIN_LADO_MAIOR = 600, 900
MAX_CANDIDATAS = 6
PROPORCOES = {"9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1.0, "4:5": 0.8}
LICENCA_PROIBIDA = re.compile(r"\bNC\b|\bND\b|non-?commercial|no-?deriv|fair use|non-?free", re.IGNORECASE)
EXTENSOES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class ErroBusca(Exception):
    pass


def baixar_json(url: str, params: dict) -> dict:
    req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": AGENTE})
    espera = 5
    for tentativa in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and tentativa < 3:
                time.sleep(espera)
                espera *= 2
                continue
            raise ErroBusca(f"HTTP {e.code} em {urllib.parse.urlparse(url).netloc}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if tentativa < 3:
                time.sleep(espera)
                continue
            raise ErroBusca(f"rede: {e}") from None
    raise ErroBusca("sem resposta")


def texto_limpo(valor: str | None, limite: int = 140) -> str:
    """Os campos do Commons vêm em HTML."""
    texto = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", valor or "")).split())
    return texto if len(texto) <= limite else texto[:limite - 1] + "…"


# ---------------------------------------------------------------- fontes


def buscar_commons(busca: str) -> list[dict]:
    dados = baixar_json(COMMONS, {
        "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{busca} filetype:bitmap",
        "gsrnamespace": "6", "gsrlimit": "20", "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": str(LARGURA_DOWNLOAD),
        "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|AttributionRequired|Restrictions|ObjectName",
    })
    paginas = sorted((dados.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 999))
    candidatas = []
    for p in paginas:
        info = (p.get("imageinfo") or [{}])[0]
        meta = {k: (v or {}).get("value") for k, v in (info.get("extmetadata") or {}).items()}
        licenca = texto_limpo(meta.get("LicenseShortName"), 60)
        if not info.get("url") or not licenca or LICENCA_PROIBIDA.search(licenca):
            continue
        candidatas.append({
            "fonte": "wikimedia", "titulo": texto_limpo(meta.get("ObjectName")) or p["title"].removeprefix("File:"),
            "autor": texto_limpo(meta.get("Artist"), 100) or "autor desconhecido", "licenca": licenca,
            "licenca_url": meta.get("LicenseUrl"), "pagina": info.get("descriptionurl"),
            "url": info.get("thumburl") or info["url"], "largura": info.get("width"), "altura": info.get("height"),
            "restricoes": texto_limpo(meta.get("Restrictions"), 80) or None,
        })
    return candidatas


def buscar_openverse(busca: str) -> list[dict]:
    dados = baixar_json(OPENVERSE, {"q": busca, "license_type": "commercial,modification", "page_size": "20",
                                    "mature": "false"})
    candidatas = []
    for r in dados.get("results", []):
        lic, versao = (r.get("license") or "").lower(), r.get("license_version") or ""
        nome = {"cc0": "CC0 1.0", "pdm": "Domínio público (PDM)"}.get(lic, f"CC {lic.upper()} {versao}".strip())
        if not r.get("url") or LICENCA_PROIBIDA.search(nome):
            continue
        candidatas.append({
            "fonte": f"openverse/{r.get('source') or r.get('provider') or '?'}", "titulo": texto_limpo(r.get("title")) or "sem título",
            "autor": texto_limpo(r.get("creator"), 100) or "autor desconhecido", "licenca": nome,
            "licenca_url": r.get("license_url"), "pagina": r.get("foreign_landing_url"), "url": r["url"],
            "largura": r.get("width"), "altura": r.get("height"), "restricoes": None,
        })
    return candidatas


def ordenar(listas: list[list[dict]], proporcao: float) -> list[dict]:
    """Junta os resultados das fontes, tira repetidas e as pequenas, e ordena: posição na busca primeiro,
    depois proporção parecida com a do vídeo e resolução."""
    vistas, notas = set(), []
    for prioridade, lista in enumerate(listas):
        for pos, c in enumerate(lista):
            chave = (c["pagina"] or c["url"]).split("?")[0]
            w, h = c.get("largura") or 0, c.get("altura") or 0
            if chave in vistas or (w and h and (min(w, h) < MIN_LADO_MENOR or max(w, h) < MIN_LADO_MAIOR)):
                continue
            vistas.add(chave)
            razao = (w / h) / proporcao if w and h else 0.5
            encaixe = min(razao, 1 / razao) if razao else 0.0
            resolucao = min(1.0, min(w, h) / 1080) if w and h else 0.3
            relevancia = 1 - pos / max(1, len(lista))
            notas.append((0.6 * relevancia + 0.25 * encaixe + 0.15 * resolucao - 0.05 * prioridade, c))
    return [c for _, c in sorted(notas, key=lambda t: -t[0])][:MAX_CANDIDATAS]


# ---------------------------------------------------------------- download e registro


def baixar_imagem(c: dict, destino_sem_ext: Path) -> Path:
    req = urllib.request.Request(c["url"], headers={"User-Agent": AGENTE})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            dados, tipo = r.read(), (r.headers.get("Content-Type") or "").split(";")[0].strip()
    except (urllib.error.URLError, TimeoutError) as e:
        raise ErroBusca(f"download falhou: {e}") from None
    for antigo in destino_sem_ext.parent.glob(destino_sem_ext.name + ".*"):
        antigo.unlink()
    if tipo in EXTENSOES:
        destino = destino_sem_ext.with_suffix("." + EXTENSOES[tipo])
        destino.write_bytes(dados)
    else:  # gif, tiff, bmp...: converte para jpg
        bruto = destino_sem_ext.with_suffix(".baixado")
        bruto.write_bytes(dados)
        destino = destino_sem_ext.with_suffix(".jpg")
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(bruto), "-frames:v", "1", "-q:v", "2", str(destino)],
                           capture_output=True, text=True)
        bruto.unlink()
        if r.returncode != 0:
            raise ErroBusca(f"formato não suportado ({tipo or 'desconhecido'})")
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0:s=x", str(destino)], capture_output=True, text=True)
    if not re.match(r"\d+x\d+", r.stdout.strip()):
        destino.unlink()
        raise ErroBusca("o arquivo baixado não é uma imagem")
    return destino


def buscas_da_cena(c: dict) -> list[str]:
    busca = c.get("busca") or []
    return [b.strip() for b in ([busca] if isinstance(busca, str) else busca) if isinstance(b, str) and b.strip()]


def hash_busca(buscas: list[str]) -> str:
    return hashlib.sha1(json.dumps([b.lower() for b in buscas]).encode()).hexdigest()[:12]


def escrever_creditos(pasta: Path, cenas: list[dict], manifesto: dict) -> int:
    linhas = ["# Créditos das imagens", "",
              "Imagens de bancos livres usadas no vídeo. Cole na descrição: CC BY e CC BY-SA exigem o crédito.", ""]
    n = 0
    for c in cenas:
        m = manifesto.get(c["id"], {})
        if m.get("origem") != "internet" or m.get("status") != "ok":
            continue
        licenca = f"{m['licenca']} ({m['licenca_url']})" if m.get("licenca_url") else m["licenca"]
        aviso = f" Atenção: {m['restricoes']}." if m.get("restricoes") else ""
        linhas.append(f"- {c['id']}: \"{m['titulo']}\", {m['autor']}. {licenca}. Fonte: {m['pagina']}{aviso}")
        n += 1
    if n:
        (pasta / "09-imagens" / "creditos.md").write_text("\n".join(linhas) + "\n", encoding="utf-8")
    return n


# ---------------------------------------------------------------- principal


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pasta", type=Path, help="pasta do projeto (projetos/<slug>)")
    ap.add_argument("--cenas", help="só estas cenas, ids separados por vírgula (ex.: B01-03,B02-01)")
    ap.add_argument("--refazer", action="store_true", help="busca de novo mesmo o que já tem imagem")
    ap.add_argument("--opcao", type=int, help="troca a imagem das cenas indicadas pela candidata N da última busca")
    ap.add_argument("--fontes", default=",".join(FONTES), help=f"bancos consultados (padrão: {','.join(FONTES)})")
    ap.add_argument("--simular", action="store_true", help="só mostra as buscas, sem acessar a internet")
    args = ap.parse_args()

    doc_path = args.pasta / "08-cenas.json"
    if not doc_path.exists():
        print(f"erro: {doc_path} não existe; rode a direção visual e ferramentas/cenas.py antes", file=sys.stderr)
        return 2
    if not config(ler_brief(args.pasta), "visual.imagens_da_internet", False):
        print("erro: o pedido não permite imagens da internet (visual.imagens_da_internet: não)", file=sys.stderr)
        return 2
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    proporcao = PROPORCOES.get(doc.get("proporcao", "16:9"), 16 / 9)
    cenas = [c for c in doc["cenas"] if c.get("fonte") == "internet"]
    if args.cenas:
        ids = set(args.cenas.split(","))
        cenas = [c for c in cenas if c["id"] in ids]
    if not cenas:
        print("nenhuma cena com \"fonte\": \"internet\"")
        return 0
    if args.opcao and not args.cenas:
        ap.error("--opcao precisa de --cenas")
    fontes = [f.strip() for f in args.fontes.split(",") if f.strip() in FONTES]

    saida = args.pasta / "09-imagens"
    manifesto_path = saida / "manifesto.json"
    manifesto = json.loads(manifesto_path.read_text(encoding="utf-8")) if manifesto_path.exists() else {}
    if args.simular:
        for c in cenas:
            m = manifesto.get(c["id"], {})
            estado = m.get("status", "a buscar") if m.get("pedido") == hash_busca(buscas_da_cena(c)) else "a buscar"
            print(f"  {c['id']}: {' → '.join(buscas_da_cena(c)) or '(sem busca)'} · {estado}")
        return 0

    saida.mkdir(parents=True, exist_ok=True)
    resumo = {"ok": 0, "nao-encontrada": 0, "pulada": 0, "erro": 0}
    for c in cenas:
        buscas = buscas_da_cena(c)
        m = manifesto.get(c["id"], {})
        tem_imagem = any(saida.glob(f"{c['id']}.*"))
        if not buscas:
            print(f"  erro {c['id']}: cena sem \"busca\"")
            resumo["erro"] += 1
            continue
        if args.opcao:
            candidatas = m.get("candidatas") or []
            if not 1 <= args.opcao <= len(candidatas):
                print(f"  erro {c['id']}: a última busca tem {len(candidatas)} candidata(s)")
                resumo["erro"] += 1
                continue
            escolhidas = [(args.opcao, candidatas[args.opcao - 1])]
        else:
            if not args.refazer and m.get("pedido") == hash_busca(buscas) and (tem_imagem or m.get("status") == "nao-encontrada"):
                resumo["pulada"] += 1
                continue
            if not args.refazer and m.get("origem") == "ia" and tem_imagem:
                print(f"  {c['id']}: já tem a imagem de reserva gerada por IA (use --refazer para buscar de novo)")
                resumo["pulada"] += 1
                continue
            if not args.refazer and not m and tem_imagem:
                print(f"  {c['id']}: já tem uma imagem fora do manifesto, talvez colocada à mão (use --refazer para trocar)")
                resumo["pulada"] += 1
                continue
            candidatas, busca = [], buscas[0]
            try:
                for busca in buscas:  # da mais específica à mais geral
                    listas = [buscar_commons(busca) if f == "wikimedia" else buscar_openverse(busca) for f in fontes]
                    candidatas = ordenar(listas, proporcao)
                    time.sleep(0.5)
                    if candidatas:
                        break
            except ErroBusca as e:
                print(f"  erro {c['id']}: {e}")
                resumo["erro"] += 1
                continue
            escolhidas = list(enumerate(candidatas, 1))
        registro = {"origem": "internet", "busca": m.get("busca") if args.opcao else busca, "pedido": hash_busca(buscas),
                    "candidatas": candidatas, "buscado_em": datetime.now().isoformat(timespec="seconds")}
        for n, candidata in escolhidas:
            try:
                destino = baixar_imagem(candidata, saida / c["id"])
            except ErroBusca as e:
                print(f"  {c['id']}: candidata {n} falhou ({e}); tentando a próxima")
                continue
            registro.update({"status": "ok", "escolhida": n, "arquivo": str(destino.relative_to(args.pasta)),
                             **{k: candidata[k] for k in ("fonte", "titulo", "autor", "licenca", "licenca_url", "pagina", "restricoes")}})
            print(f"  ok {c['id']}: \"{candidata['titulo'][:60]}\" · {candidata['licenca']} · {candidata['fonte']}")
            resumo["ok"] += 1
            break
        else:
            tentadas = " → ".join(buscas)
            if args.opcao:
                print(f"  erro {c['id']}: a candidata {args.opcao} não baixou; a imagem anterior foi mantida")
                resumo["erro"] += 1
                continue
            if m.get("modelo") and m.get("status") == "ok" and tem_imagem:
                # A cena já tinha imagem gerada por IA: fica ela, sem pagar de novo pelo prompt de reserva.
                registro = {**m, "origem": "ia", "busca_sem_resultado": buscas}
                print(f"  nada para {c['id']} ({tentadas}): fica a imagem de IA que a cena já tinha")
            else:
                # A imagem baixada por uma busca antiga perde o crédito no manifesto: não pode ir para o vídeo.
                if m.get("origem") == "internet" and m.get("status") == "ok":
                    for antiga in saida.glob(f"{c['id']}.*"):
                        antiga.unlink()
                registro["status"] = "nao-encontrada"
                reserva = "gerar_imagens.py usa o prompt de reserva" if c.get("prompt_final") else "a cena não tem prompt de reserva"
                print(f"  nada para {c['id']} ({tentadas}): {reserva}")
            resumo["nao-encontrada"] += 1
        manifesto[c["id"]] = registro
        manifesto_path.write_text(json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8")

    n_creditos = escrever_creditos(args.pasta, doc["cenas"], manifesto)
    print(f"\nbaixadas: {resumo['ok']} · sem resultado: {resumo['nao-encontrada']} · já prontas: {resumo['pulada']} · erros: {resumo['erro']}")
    if n_creditos:
        print(f"créditos: {saida / 'creditos.md'} ({n_creditos} imagem(ns))")
    if resumo["nao-encontrada"]:
        print("próximo passo: mude a \"busca\" dessas cenas em 08-cenas/<bloco>.json, ou gere pelo prompt de reserva com gerar_imagens.py")
    return 1 if resumo["erro"] else 0


if __name__ == "__main__":
    sys.exit(main())
