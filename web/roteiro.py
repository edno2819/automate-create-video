"""Roteiro no formato de biblioteca/formato-roteiro.md, visto como falas editáveis.

A tela mostra só o que é dito: cada linha de narração ou de [FALA: Nome], com quem fala e como dizer. As
indicações de imagem ([VISUAL: ...]) e a linha de metadados dos blocos ficam escondidas, mas guardadas no
lugar delas: ao salvar, o arquivo é remontado com elas onde estavam, e o diretor visual continua a tê-las.
Os outros marcadores ([RE-HOOK], [PAUSA], [VERIFICAR: ...]) fazem parte da fala e aparecem na tela.
"""

from __future__ import annotations

import re
from pathlib import Path

CABECALHO_BLOCO = re.compile(r"^##\s*\[(B\d+)\]\s*(.*)$")
CABECALHO_SECAO = re.compile(r"^##\s+(?!\[B\d+\])")
VISUAL = re.compile(r"\[VISUAL:[^\]]*\]", re.IGNORECASE)
FALA = re.compile(r"^\[FALA:\s*([^\],]+?)\s*(?:,\s*([^\]]*?))?\s*\]\s*", re.IGNORECASE)
REHOOK = re.compile(r"^\[RE-HOOK\]\s*", re.IGNORECASE)
NARRADOR = "Narrador"


def ler_item(linha: str, quebra: bool) -> dict:
    visuais = VISUAL.findall(linha)
    sem_visual = " ".join(VISUAL.sub(" ", linha).split())
    visual_antes = bool(visuais) and REHOOK.sub("", linha.strip()).upper().startswith("[VISUAL")
    texto, rehook = sem_visual, False
    if m := REHOOK.match(texto):
        rehook, texto = True, texto[m.end():]
    quem, direcao = NARRADOR, ""
    if m := FALA.match(texto):
        quem, direcao, texto = m.group(1).strip(), (m.group(2) or "").strip(), texto[m.end():]
    texto = texto.strip()
    if not texto and quem == NARRADOR and not direcao:
        return {"tipo": "oculto", "bruto": linha, "quebra": quebra}
    return {"tipo": "fala", "quem": quem, "direcao": direcao, "texto": texto, "rehook": rehook,
            "visuais": visuais, "visual_antes": visual_antes, "quebra": quebra}


def ler(caminho: Path) -> dict:
    """{"frontmatter", "intro", "blocos": [{id, titulo, meta, itens}], "resto"}; tudo o que não é bloco fica bruto."""
    linhas = caminho.read_text(encoding="utf-8").split("\n")
    frontmatter, i = "", 0
    if linhas and linhas[0].strip() == "---":
        fim = next((j for j in range(1, len(linhas)) if linhas[j].strip() == "---"), None)
        if fim is not None:
            frontmatter, i = "\n".join(linhas[1:fim]), fim + 1
    intro, blocos, resto = [], [], []
    atual, em_resto, quebra = None, False, False
    for linha in linhas[i:]:
        if m := CABECALHO_BLOCO.match(linha):
            atual, em_resto, quebra = {"id": m.group(1), "titulo": m.group(2).strip(), "meta": [], "itens": []}, False, False
            blocos.append(atual)
            continue
        if CABECALHO_SECAO.match(linha) or em_resto:
            em_resto, atual = True, None
            resto.append(linha)
            continue
        if atual is None:
            intro.append(linha)
            continue
        if linha.startswith(">") and not atual["itens"]:
            atual["meta"].append(linha)
            continue
        if not linha.strip():
            quebra = bool(atual["itens"])
            continue
        atual["itens"].append(ler_item(linha, quebra))
        quebra = False
    return {"frontmatter": frontmatter, "intro": "\n".join(intro).strip("\n"), "blocos": blocos,
            "resto": "\n".join(resto).strip("\n")}


def linha_do_item(item: dict) -> str:
    if item.get("tipo") == "oculto":
        return item["bruto"]
    texto = " ".join(str(item.get("texto", "")).split())
    quem = (item.get("quem") or NARRADOR).strip()
    direcao = " ".join(str(item.get("direcao") or "").split())
    partes = ["[RE-HOOK]"] if item.get("rehook") else []
    visuais = item.get("visuais") or []
    if visuais and item.get("visual_antes", True):
        partes += visuais
    if quem.lower() != NARRADOR.lower() or direcao:
        partes.append(f"[FALA: {quem}{', ' + direcao if direcao else ''}]")
    partes.append(texto)
    if visuais and not item.get("visual_antes", True):
        partes += visuais
    return " ".join(p for p in partes if p)


def montar(doc: dict, versao: int, origem: str | None = None) -> str:
    front = doc.get("frontmatter", "")
    if re.search(r"(?m)^versao:", front):
        front = re.sub(r"(?m)^versao:.*$", f"versao: {versao}", front)
    else:
        front += f"\nversao: {versao}"
    if origem:
        front = re.sub(r"(?m)^origem:.*\n?", "", front).rstrip("\n") + f'\norigem: "{origem}"'
    saida = ["---", front.strip("\n"), "---", ""]
    if doc.get("intro"):
        saida += [doc["intro"], ""]
    for b in doc["blocos"]:
        saida.append(f"## [{b['id']}] {' '.join(str(b.get('titulo', '')).split())}")
        saida += b.get("meta") or []
        saida.append("")
        for n, item in enumerate(b["itens"]):
            if item.get("quebra") and n:
                saida.append("")
            saida.append(linha_do_item(item))
        saida.append("")
    if doc.get("resto"):
        saida += [doc["resto"], ""]
    return "\n".join(saida)


def versoes(pasta: Path) -> list[tuple[int, Path]]:
    achadas = []
    for p in pasta.glob("05-roteiro-v*.md"):
        if m := re.fullmatch(r"05-roteiro-v(\d+)\.md", p.name):
            achadas.append((int(m.group(1)), p))
    return sorted(achadas)


def texto_falado(doc: dict) -> str:
    """Só o que é dito, para comparar versões."""
    return "\n".join(linha_do_item({**i, "visuais": []}) for b in doc["blocos"] for i in b["itens"] if i["tipo"] == "fala")
