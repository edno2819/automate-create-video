#!/usr/bin/env python3
"""Ciclo de feedback: registra métricas de vídeos publicados e resume o que funciona no canal.

Cada registro liga as métricas de um vídeo às técnicas, ao tipo de gancho e aos gatilhos emocionais usados no
roteiro (lidos do frontmatter do roteiro). O resumo compara cada técnica, cada tipo de gancho e cada gatilho com a
média do canal e grava biblioteca/aprendizados.md, que o estrategista e o redator de ganchos leem.

Os registros ficam no banco (tabela desempenho), e o aprendizados.md é gerado a partir dele e enviado ao
armazenamento, para todos os processos lerem o mesmo. Sem banco acessível (o script rodando fora do servidor), vale
o arquivo antigo dados/desempenho.jsonl, como antes. A interface web registra pela tela (Produção > Desempenho);
este script é a mesma coisa pela linha de comando, e é ele que a web roda para refazer o resumo.

Uso (os caminhos são relativos à raiz do espaço de trabalho):
    python3 ferramentas/desempenho.py registrar --projeto <slug> --video-id <id> --janela 7d \\
        --ctr 6.2 --retencao-30s 71 --retencao-media 45 --views 12000 --inscritos-ganhos 120
    python3 ferramentas/desempenho.py registrar --projeto <slug> --roteiro projetos/<slug>/06-roteiro-final.md \\
        --video-id <id> --janela 7d --viu-vs-passou 78 --retencao-media 92 --views 40000
    python3 ferramentas/desempenho.py resumo [--janela 7d] [--min-amostras 3]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

DADOS = "dados/desempenho.jsonl"           # reserva sem banco (o formato antigo)
APRENDIZADOS = "biblioteca/aprendizados.md"
METRICAS = {
    "ctr": "CTR (%)",
    "retencao_30s": "Retenção aos 30 s (%)",
    "retencao_media": "Retenção média (%)",
    "viu_vs_passou": "Viu vs. passou (%)",
    "views": "Views",
    "inscritos_por_mil": "Inscritos por mil views",
}
JANELAS = ("48h", "7d", "28d")


def espaco() -> Path:
    return Path(os.environ.get("APP_RAIZ") or RAIZ).resolve()


def com_banco() -> bool:
    try:
        from nucleo import banco
        return banco.disponivel()
    except Exception:  # noqa: BLE001 - sem o pacote ou sem banco: vale o arquivo
        return False


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


# ---------------------------------------------------------------- registros (banco, ou o jsonl antigo)


def carregar(janela: str | None = None) -> list[dict]:
    """Os registros, achatados como no antigo dados/desempenho.jsonl (r["ctr"], r["projeto"], r["tecnicas"]...)."""
    if com_banco():
        from nucleo.banco import repositorios
        return repositorios.desempenhos(janela)
    arquivo = espaco() / DADOS
    if not arquivo.exists():
        return []
    registros = [json.loads(l) for l in arquivo.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [r for r in registros if not janela or r.get("janela") == janela]


def _salvar_no_arquivo(registro: dict) -> tuple[bool, int]:
    arquivo = espaco() / DADOS
    anteriores = carregar()
    registros = [r for r in anteriores if not (r["video_id"] == registro["video_id"] and r["janela"] == registro["janela"])]
    substituiu = len(registros) < len(anteriores)
    registros.append(registro)
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    arquivo.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in registros), encoding="utf-8")
    return substituiu, len(registros)


def registrar_metricas(projeto: str, video_id: str, janela: str = "7d", *, roteiro: str | Path | None = None,
                       formato: str | None = None, tecnicas: list[str] | None = None, tipo_gancho: str | None = None,
                       gatilhos: list[str] | None = None, obs: str | None = None, **metricas) -> dict:
    """Grava (ou substitui) as métricas de um vídeo numa janela, com as técnicas, o gancho e os gatilhos do roteiro (o
    frontmatter de projetos/<slug>/06-roteiro-final.md, ou do roteiro indicado). ValueError se não houver de onde
    tirar as técnicas."""
    if janela not in JANELAS:
        raise ValueError(f"janela desconhecida: {janela} (use {', '.join(JANELAS)})")
    caminho = Path(roteiro) if roteiro else Path("projetos") / projeto / "06-roteiro-final.md"
    absoluto = caminho if caminho.is_absolute() else espaco() / caminho
    if com_banco() and not absoluto.exists():
        try:
            from nucleo import espaco as espaco_
            espaco_.garantir(caminho.as_posix())
        except Exception:  # noqa: BLE001 - sem o roteiro, vale o que veio na linha de comando
            pass
    front = ler_frontmatter(absoluto) if absoluto.exists() else {}
    if not front and not (tecnicas and tipo_gancho):
        raise ValueError(f"não achei o frontmatter de {caminho}. Passe --roteiro ou --tecnicas e --tipo-gancho.")
    lista = tecnicas if tecnicas else front.get("tecnicas", [])
    lista_gatilhos = gatilhos if gatilhos else front.get("gatilhos", [])
    registro = {
        "projeto": projeto,
        "video_id": video_id,
        "janela": janela,
        "registrado_em": date.today().isoformat(),
        "roteiro": caminho.as_posix(),
        "titulo": front.get("titulo", ""),
        "formato": formato or front.get("formato", "longo"),
        "tecnicas": [str(t).strip() for t in lista if str(t).strip()],
        "tipo_gancho": tipo_gancho or front.get("tipo_gancho", ""),
        "gatilhos": [str(g).strip() for g in lista_gatilhos or [] if str(g).strip()] if isinstance(lista_gatilhos, list) else [],
        **{m: metricas.get(m) for m in ("ctr", "retencao_30s", "retencao_media", "viu_vs_passou", "views",
                                        "inscritos_ganhos")},
        "observacao": obs,
    }
    ganhos, views = registro.get("inscritos_ganhos"), registro.get("views")
    registro["inscritos_por_mil"] = round(ganhos / views * 1000, 2) if ganhos is not None and views else None
    if com_banco():
        from nucleo.banco import repositorios
        existia = any(r.get("video_id") == video_id for r in repositorios.desempenhos(janela))
        gravado = repositorios.registrar_desempenho({k: v for k, v in registro.items() if k != "registrado_em"})
        return {**registro, **gravado, "substituiu": existia, "total": len(repositorios.desempenhos())}
    substituiu, total = _salvar_no_arquivo(registro)
    return {**registro, "substituiu": substituiu, "total": total}


# ---------------------------------------------------------------- resumo (biblioteca/aprendizados.md)


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


def aprendizados_md(registros: list[dict], janela: str | None, min_amostras: int = 3) -> str:
    hoje = date.today().isoformat()
    linhas = [
        "# Aprendizados do canal",
        "",
        f"Gerado em {hoje} por `ferramentas/desempenho.py resumo`" + (f", janela {janela}" if janela else "")
        + ", a partir das métricas registradas. Não edite à mão: registre as métricas e o resumo é refeito.",
        "",
    ]
    if not registros:
        linhas += ["Ainda não há vídeos registrados. Até lá, o estrategista usa só as boas práticas da biblioteca e os dados de "
                   "trends. Depois de 20 a 30 vídeos, esta página passa a dizer o que funciona **neste** canal."]
        return "\n".join(linhas) + "\n"
    linhas += [
        f"Base: {len(registros)} registro(s). Grupos com menos de {min_amostras} vídeos aparecem como **sinal fraco**: "
        "trate como hipótese, não como regra. Diferenças pequenas (menos de 10%) com poucos vídeos são ruído.",
        "",
    ]
    for formato in sorted({r.get("formato") or "longo" for r in registros}):
        grupo = [r for r in registros if (r.get("formato") or "longo") == formato]
        metricas = [m for m in METRICAS if any(r.get(m) is not None for r in grupo)]
        base = {m: media([r.get(m) for r in grupo]) for m in metricas}
        linhas += [f"## Formato: {formato} ({len(grupo)} vídeos)", "", "Média do canal: "
                   + " · ".join(f"{METRICAS[m]} {fmt(base[m])}" for m in metricas), ""]

        for dimensao, rotulo in (("tecnicas", "Técnica"), ("tipo_gancho", "Tipo de gancho"),
                                 ("gatilhos", "Gatilho emocional")):
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
            for chave, rs in sorted(por_chave.items(), key=lambda kv: -len(kv[1])):
                celulas = []
                for m in metricas:
                    v = media([r.get(m) for r in rs])
                    d = (v - base[m]) / base[m] * 100 if v is not None and base[m] else None
                    celulas.append(f"{fmt(v)} ({fmt(d, delta=True)})")
                aviso = " (sinal fraco)" if len(rs) < min_amostras else ""
                linhas.append(f"| {chave}{aviso} | {len(rs)} | " + " | ".join(celulas) + " |")
            linhas.append("")

        melhores = sorted((r for r in grupo if r.get("retencao_media") is not None), key=lambda r: -r["retencao_media"])[:3]
        if melhores:
            linhas += ["### Maiores retenções médias", ""]
            linhas += [f"- {r.get('titulo') or r['video_id']} ({r['video_id']}): {fmt(r['retencao_media'])}% · "
                       f"gancho {r.get('tipo_gancho') or '—'} · gatilhos {', '.join(r.get('gatilhos') or []) or '—'} · "
                       f"{', '.join(r.get('tecnicas') or [])}" for r in melhores]
            linhas.append("")
    return "\n".join(linhas) + "\n"


def gerar_aprendizados(janela: str | None = None, min_amostras: int = 3) -> str:
    """Refaz biblioteca/aprendizados.md a partir dos registros (sob trava, com banco) e o envia ao armazenamento.
    Devolve o texto."""
    if com_banco():
        from nucleo import banco
        from nucleo import espaco as espaco_
        with banco.trava("aprendizados"):
            texto = aprendizados_md(carregar(janela), janela, min_amostras)
            try:
                espaco_.gravar_texto(APRENDIZADOS, texto)
            except Exception as e:  # noqa: BLE001 - sem armazenamento, fica o arquivo local
                print(f"aviso: {APRENDIZADOS} não foi enviado ao armazenamento: {e}", file=sys.stderr)
                _gravar_local(texto)
        return texto
    texto = aprendizados_md(carregar(janela), janela, min_amostras)
    _gravar_local(texto)
    return texto


def _gravar_local(texto: str) -> None:
    destino = espaco() / APRENDIZADOS
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(texto, encoding="utf-8")


# ---------------------------------------------------------------- linha de comando


def registrar(args) -> int:
    try:
        r = registrar_metricas(args.projeto, args.video_id, args.janela, roteiro=args.roteiro, formato=args.formato,
                               tecnicas=args.tecnicas.split(",") if args.tecnicas else None, tipo_gancho=args.tipo_gancho,
                               gatilhos=args.gatilhos.split(",") if args.gatilhos else None, obs=args.obs, ctr=args.ctr,
                               retencao_30s=args.retencao_30s, retencao_media=args.retencao_media, viu_vs_passou=args.viu_vs_passou, views=args.views,
                               inscritos_ganhos=args.inscritos_ganhos)
    except ValueError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2
    print(f"{'atualizado' if r['substituiu'] else 'registrado'}: {args.video_id} ({args.janela}) · {r['formato']} · "
          f"técnicas {', '.join(r['tecnicas']) or '—'} · gancho {r['tipo_gancho'] or '—'} · "
          f"gatilhos {', '.join(r.get('gatilhos') or []) or '—'}")
    print(f"{'banco' if com_banco() else espaco() / DADOS}: {r['total']} registros")
    return 0


def resumo(args) -> int:
    texto = gerar_aprendizados(args.janela, args.min_amostras)
    print(f"arquivo: {espaco() / APRENDIZADOS}")
    print(texto, end="")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("registrar", help="registra ou atualiza as métricas de um vídeo")
    r.add_argument("--projeto", required=True, help="slug da pasta em projetos/")
    r.add_argument("--video-id", required=True)
    r.add_argument("--janela", default="7d", choices=JANELAS, help="período das métricas (padrão 7d)")
    r.add_argument("--roteiro", help="roteiro de onde ler técnicas e gancho (padrão: projetos/<slug>/06-roteiro-final.md)")
    r.add_argument("--formato", choices=["longo", "short"])
    r.add_argument("--tecnicas", help="ids separados por vírgula (sobrepõe o roteiro)")
    r.add_argument("--tipo-gancho", help="id do tipo de gancho (sobrepõe o roteiro)")
    r.add_argument("--gatilhos", help="ids dos gatilhos emocionais separados por vírgula (sobrepõe o roteiro)")
    r.add_argument("--ctr", type=float, help="taxa de cliques das impressões, em %%")
    r.add_argument("--retencao-30s", type=float, help="%% do público ainda assistindo aos 30 s")
    r.add_argument("--retencao-media", type=float, help="porcentagem média assistida")
    r.add_argument("--viu-vs-passou", type=float, help="shorts: %% que assistiu em vez de passar")
    r.add_argument("--views", type=int)
    r.add_argument("--inscritos-ganhos", type=int)
    r.add_argument("--obs", help="observação livre (ex.: thumb trocada no dia 3)")

    s = sub.add_parser("resumo", help="gera biblioteca/aprendizados.md")
    s.add_argument("--janela", choices=JANELAS, help="só registros desta janela (recomendado, para comparar igual com igual)")
    s.add_argument("--min-amostras", type=int, default=3)

    args = ap.parse_args()
    return registrar(args) if args.cmd == "registrar" else resumo(args)


if __name__ == "__main__":
    sys.exit(main())
