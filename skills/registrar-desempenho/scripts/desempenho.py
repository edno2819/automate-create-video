#!/usr/bin/env python3
"""Ciclo de feedback: registra métricas de vídeos publicados e resume o que funciona no canal.

Cada registro liga as métricas de um vídeo às técnicas e ao tipo de gancho usados no roteiro (lidos do
frontmatter do roteiro). O resumo compara cada técnica e cada tipo de gancho com a média do canal e
grava biblioteca/aprendizados.md, que o estrategista lê ao propor ângulos.

Uso:
    python3 desempenho.py registrar --projeto <slug> --video-id <id> --janela 7d \\
        --ctr 6.2 --retencao-30s 71 --retencao-media 45 --views 12000 --inscritos-ganhos 120
    python3 desempenho.py registrar --projeto <slug> --roteiro projetos/<slug>/07-cortes/corte-01.md \\
        --video-id <id> --janela 7d --viu-vs-passou 78 --retencao-media 92 --views 40000
    python3 desempenho.py resumo [--janela 7d] [--min-amostras 3]

Rode a partir da raiz do projeto. Só usa a biblioteca padrão.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ARQUIVO_DADOS = Path("dados/desempenho.jsonl")
ARQUIVO_APRENDIZADOS = Path("biblioteca/aprendizados.md")
METRICAS = {
    "ctr": "CTR (%)",
    "retencao_30s": "Retenção aos 30 s (%)",
    "retencao_media": "Retenção média (%)",
    "viu_vs_passou": "Viu vs. passou (%)",
    "views": "Views",
    "inscritos_por_mil": "Inscritos por mil views",
}


def ler_frontmatter(caminho: Path) -> dict[str, object]:
    linhas = caminho.read_text(encoding="utf-8").splitlines()
    if not linhas or linhas[0].strip() != "---":
        return {}
    dados: dict[str, object] = {}
    for linha in linhas[1:]:
        if linha.strip() == "---":
            break
        linha = re.sub(r"\s+#.*$", "", linha)
        if ":" not in linha:
            continue
        chave, valor = (p.strip() for p in linha.split(":", 1))
        if valor.startswith("[") and valor.endswith("]"):
            dados[chave] = [v.strip().strip("\"'") for v in valor[1:-1].split(",") if v.strip()]
        else:
            dados[chave] = valor.strip("\"'")
    return dados


def carregar() -> list[dict]:
    if not ARQUIVO_DADOS.exists():
        return []
    return [json.loads(l) for l in ARQUIVO_DADOS.read_text(encoding="utf-8").splitlines() if l.strip()]


def salvar(registros: list[dict]) -> None:
    ARQUIVO_DADOS.parent.mkdir(parents=True, exist_ok=True)
    ARQUIVO_DADOS.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in registros), encoding="utf-8")


def registrar(args) -> int:
    roteiro = Path(args.roteiro) if args.roteiro else Path("projetos") / args.projeto / "06-roteiro-final.md"
    front = ler_frontmatter(roteiro) if roteiro.exists() else {}
    if not front and not (args.tecnicas and args.tipo_gancho):
        print(f"erro: não achei o frontmatter de {roteiro}. Passe --roteiro ou --tecnicas e --tipo-gancho.", file=sys.stderr)
        return 2

    tecnicas = args.tecnicas.split(",") if args.tecnicas else front.get("tecnicas", [])
    registro = {
        "projeto": args.projeto,
        "video_id": args.video_id,
        "janela": args.janela,
        "registrado_em": date.today().isoformat(),
        "roteiro": str(roteiro),
        "titulo": front.get("titulo", ""),
        "formato": args.formato or front.get("formato", "longo"),
        "tecnicas": [t.strip() for t in tecnicas if t.strip()],
        "tipo_gancho": args.tipo_gancho or front.get("tipo_gancho", ""),
        "ctr": args.ctr,
        "retencao_30s": args.retencao_30s,
        "retencao_media": args.retencao_media,
        "viu_vs_passou": args.viu_vs_passou,
        "views": args.views,
        "inscritos_ganhos": args.inscritos_ganhos,
        "inscritos_por_mil": round(args.inscritos_ganhos / args.views * 1000, 2) if args.inscritos_ganhos is not None and args.views else None,
        "observacao": args.obs,
    }
    anteriores = carregar()
    registros = [r for r in anteriores if not (r["video_id"] == args.video_id and r["janela"] == args.janela)]
    substituiu = len(registros) < len(anteriores)
    registros.append(registro)
    salvar(registros)
    print(f"{'atualizado' if substituiu else 'registrado'}: {args.video_id} ({args.janela}) · {registro['formato']} · "
          f"técnicas {', '.join(registro['tecnicas']) or '—'} · gancho {registro['tipo_gancho'] or '—'}")
    print(f"arquivo: {ARQUIVO_DADOS.resolve()} ({len(registros)} registros)")
    return 0


def media(valores: list[float]) -> float | None:
    valores = [v for v in valores if v is not None]
    return sum(valores) / len(valores) if valores else None


def fmt(v: float | None, delta: bool = False) -> str:
    if v is None:
        return "—"
    if delta:
        return f"{v:+.0f}%"
    if abs(v) >= 1000:
        return f"{v:,.0f}".replace(",", ".")
    return f"{v:.1f}".replace(".", ",")


def resumo(args) -> int:
    registros = [r for r in carregar() if not args.janela or r["janela"] == args.janela]
    hoje = date.today().isoformat()
    linhas = [
        "# Aprendizados do canal",
        "",
        f"Gerado em {hoje} por `skills/registrar-desempenho/scripts/desempenho.py resumo`"
        + (f", janela {args.janela}" if args.janela else "") + ". Não edite à mão: rode o resumo de novo.",
        "",
    ]
    if not registros:
        linhas += ["Ainda não há vídeos registrados. Até lá, o estrategista usa só as boas práticas da biblioteca e os dados de "
                   "trends. Depois de 20 a 30 vídeos, esta página passa a dizer o que funciona **neste** canal."]
        ARQUIVO_APRENDIZADOS.parent.mkdir(parents=True, exist_ok=True)
        ARQUIVO_APRENDIZADOS.write_text("\n".join(linhas) + "\n", encoding="utf-8")
        print(f"sem registros; {ARQUIVO_APRENDIZADOS} reiniciado")
        return 0

    linhas += [
        f"Base: {len(registros)} registro(s). Grupos com menos de {args.min_amostras} vídeos aparecem como **sinal fraco**: "
        "trate como hipótese, não como regra. Diferenças pequenas (menos de 10%) com poucos vídeos são ruído.",
        "",
    ]
    for formato in sorted({r["formato"] for r in registros}):
        grupo = [r for r in registros if r["formato"] == formato]
        metricas = [m for m in METRICAS if any(r.get(m) is not None for r in grupo)]
        base = {m: media([r.get(m) for r in grupo]) for m in metricas}
        linhas += [f"## Formato: {formato} ({len(grupo)} vídeos)", "", "Média do canal: "
                   + " · ".join(f"{METRICAS[m]} {fmt(base[m])}" for m in metricas), ""]

        for dimensao, rotulo in (("tecnicas", "Técnica"), ("tipo_gancho", "Tipo de gancho")):
            por_chave: dict[str, list[dict]] = defaultdict(list)
            for r in grupo:
                chaves = r.get(dimensao) or []
                for chave in (chaves if isinstance(chaves, list) else [chaves]):
                    if chave:
                        por_chave[chave].append(r)
            if not por_chave:
                continue
            cab = " | ".join(f"{METRICAS[m]} (vs. média)" for m in metricas)
            linhas += [f"### Por {rotulo.lower()}", "", f"| {rotulo} | Vídeos | {cab} |", "|---|---|" + "---|" * len(metricas)]
            ordenados = sorted(por_chave.items(), key=lambda kv: -len(kv[1]))
            for chave, rs in ordenados:
                celulas = []
                for m in metricas:
                    v = media([r.get(m) for r in rs])
                    d = (v - base[m]) / base[m] * 100 if v is not None and base[m] else None
                    celulas.append(f"{fmt(v)} ({fmt(d, delta=True)})")
                aviso = " (sinal fraco)" if len(rs) < args.min_amostras else ""
                linhas.append(f"| {chave}{aviso} | {len(rs)} | " + " | ".join(celulas) + " |")
            linhas.append("")

        melhores = sorted((r for r in grupo if r.get("retencao_media") is not None), key=lambda r: -r["retencao_media"])[:3]
        if melhores:
            linhas += ["### Maiores retenções médias", ""]
            linhas += [f"- {r['titulo'] or r['video_id']} ({r['video_id']}): {fmt(r['retencao_media'])}% · "
                       f"gancho {r['tipo_gancho'] or '—'} · {', '.join(r['tecnicas'])}" for r in melhores]
            linhas.append("")

    ARQUIVO_APRENDIZADOS.parent.mkdir(parents=True, exist_ok=True)
    ARQUIVO_APRENDIZADOS.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    print(f"arquivo: {ARQUIVO_APRENDIZADOS.resolve()}")
    print("\n".join(linhas))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("registrar", help="registra ou atualiza as métricas de um vídeo")
    r.add_argument("--projeto", required=True, help="slug da pasta em projetos/")
    r.add_argument("--video-id", required=True)
    r.add_argument("--janela", default="7d", help="período das métricas: 48h, 7d, 28d... (padrão 7d)")
    r.add_argument("--roteiro", help="roteiro de onde ler técnicas e gancho (padrão: projetos/<slug>/06-roteiro-final.md)")
    r.add_argument("--formato", choices=["longo", "short"])
    r.add_argument("--tecnicas", help="ids separados por vírgula (sobrepõe o roteiro)")
    r.add_argument("--tipo-gancho", help="id do tipo de gancho (sobrepõe o roteiro)")
    r.add_argument("--ctr", type=float, help="taxa de cliques das impressões, em %%")
    r.add_argument("--retencao-30s", type=float, help="%% do público ainda assistindo aos 30 s")
    r.add_argument("--retencao-media", type=float, help="porcentagem média assistida")
    r.add_argument("--viu-vs-passou", type=float, help="shorts: %% que assistiu em vez de passar")
    r.add_argument("--views", type=int)
    r.add_argument("--inscritos-ganhos", type=int)
    r.add_argument("--obs", help="observação livre (ex.: thumb trocada no dia 3)")

    s = sub.add_parser("resumo", help="gera biblioteca/aprendizados.md")
    s.add_argument("--janela", help="só registros desta janela (recomendado, para comparar igual com igual)")
    s.add_argument("--min-amostras", type=int, default=3)

    args = ap.parse_args()
    return registrar(args) if args.cmd == "registrar" else resumo(args)


if __name__ == "__main__":
    sys.exit(main())
