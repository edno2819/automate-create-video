#!/usr/bin/env python3
"""Confere se a narração de dois arquivos é a mesma, palavra por palavra.

Usado depois de formatar um roteiro recebido de fora: o formatador pode dividir em blocos, quebrar
parágrafos e acrescentar marcadores ([RE-HOOK], [VERIFICAR]...), mas não pode mudar nenhuma palavra
narrada. Pontuação, maiúsculas e espaços não contam.

O original pode ser texto corrido (linhas com '#' ou '>' e marcas de tempo como **[01:10]** são
ignoradas) ou já estar no formato de biblioteca/formato-roteiro.md. Num original com diálogo, em que cada
fala começa pelo nome de quem fala ("Moranguete: Você de novo?"), passe os nomes em --falantes: o nome no
começo da linha não conta como palavra narrada (no formatado, ele vira a marca [FALA: Moranguete]).

Uso:
    python3 ferramentas/conferir_texto.py projetos/<slug>/00-roteiro-original.md projetos/<slug>/06-roteiro-final.md
    python3 ferramentas/conferir_texto.py <original> <formatado> --falantes Moranguete,Abacate

Sai com código 0 se a narração for idêntica e 1 se houver diferença. Só usa a biblioteca padrão.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # importável também de fora de ferramentas/ (os fluxos)
from cenas import norm, palavras  # noqa: E402
from medir_roteiro import CABECALHO_BLOCO, ler_frontmatter, ler_roteiro, sem_acento  # noqa: E402

MARCA_TEMPO = re.compile(r"\*{0,2}\[\d{1,2}:\d{2}(?::\d{2})?\]\*{0,2}")
NOME_NO_INICIO = re.compile(r"^\s*[-—–]?\s*\**\s*([^:*\n]{1,40}?)\s*\**\s*:\s*\**\s*")


def sem_falante(linha: str, falantes: set[str]) -> str:
    """Tira "Nome:" (ou "— **Nome:**") do começo da linha, se Nome for um dos falantes."""
    m = NOME_NO_INICIO.match(linha)
    return linha[m.end():] if m and sem_acento(m.group(1)).strip() in falantes else linha


def narracao(caminho: Path, falantes: set[str] = frozenset()) -> list[str]:
    texto = caminho.read_text(encoding="utf-8")
    linhas = texto.splitlines()
    if any(CABECALHO_BLOCO.match(l) for l in linhas):
        _, blocos, _ = ler_roteiro(caminho)
        return [w for b in blocos for w in b.tokens]
    _, inicio = ler_frontmatter(linhas)
    corpo = [MARCA_TEMPO.sub(" ", sem_falante(l, falantes)) for l in linhas[inicio:] if not l.lstrip().startswith(("#", ">"))]
    return palavras("\n".join(corpo))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("original", type=Path)
    ap.add_argument("formatado", type=Path)
    ap.add_argument("--falantes", default="", help="nomes de quem fala no original, separados por vírgula")
    args = ap.parse_args()
    falantes = {sem_acento(n).strip() for n in args.falantes.split(",") if n.strip()}
    a, b = narracao(args.original, falantes), narracao(args.formatado)
    na, nb = [norm(w) for w in a], [norm(w) for w in b]
    na_f = [(w, i) for i, w in enumerate(na) if w]
    nb_f = [(w, i) for i, w in enumerate(nb) if w]
    for k, ((wa, ia), (wb, ib)) in enumerate(zip(na_f, nb_f)):
        if wa != wb:
            print(f"diferença na palavra {k + 1}:")
            print(f"  original:  \"{' '.join(a[max(0, ia - 6):ia + 10])}\"")
            print(f"  formatado: \"{' '.join(b[max(0, ib - 6):ib + 10])}\"")
            return 1
    if len(na_f) != len(nb_f):
        maior, nome = (a, "original") if len(na_f) > len(nb_f) else (b, "formatado")
        resto = (na_f if nome == "original" else nb_f)[min(len(na_f), len(nb_f))][1]
        print(f"o {nome} tem palavras a mais no fim: \"{' '.join(maior[resto:resto + 15])}\"")
        return 1
    print(f"ok: narração idêntica ({len(na_f)} palavras)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
