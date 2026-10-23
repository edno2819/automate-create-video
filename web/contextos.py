"""Contextos salvos: material de apoio reaproveitável entre vídeos (texto, imagens e áudios).

Cada contexto é uma pasta pedidos/contextos/<id>/ com contexto.json (nome, texto e anexos, cada um com a sua
descrição), os arquivos anexados e contexto.md, gerado a partir do JSON a cada gravação. O campo `contexto` do
pedido aponta para esse contexto.md, que o pedido.py copia para o brief: os agentes acham ali o texto e o
caminho de cada anexo. Os .md soltos em pedidos/contextos/ (o formato antigo) continuam valendo e aparecem na
lista como contextos só de texto, que a interface não edita.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import BinaryIO

import etapas

RAIZ = Path(__file__).resolve().parent.parent
PASTA = RAIZ / "pedidos" / "contextos"
MANIFESTO = "contexto.json"
TEXTO = "contexto.md"
EXTENSOES_IMAGEM = {".png", ".jpg", ".jpeg", ".webp"}
EXTENSOES_AUDIO = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}
ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def tipo_de(nome: str) -> str | None:
    ext = Path(nome).suffix.lower()
    return "imagem" if ext in EXTENSOES_IMAGEM else "audio" if ext in EXTENSOES_AUDIO else None


def pasta_de(id_: str) -> Path:
    if not ID.fullmatch(id_ or ""):
        raise KeyError(id_)
    return PASTA / id_


def relativo(caminho: Path) -> str:
    return str(caminho.relative_to(RAIZ))


def markdown(dados: dict, pasta: Path) -> str:
    """O texto que os agentes leem: o do usuário e, no fim, a lista de anexos com caminho e descrição."""
    partes = [f"# Contexto: {dados['nome']}", ""]
    if dados.get("texto", "").strip():
        partes += [dados["texto"].strip(), ""]
    if dados.get("anexos"):
        partes += ["## Anexos", "",
                   "Arquivos que acompanham este contexto, com o caminho a partir da raiz do repositório. Abra as "
                   "imagens antes de usá-las. Os áudios não podem ser ouvidos: vale a descrição de cada um.", ""]
        for a in dados["anexos"]:
            rotulo = "Imagem" if a["tipo"] == "imagem" else "Áudio"
            descricao = (a.get("descricao") or "").strip() or "sem descrição"
            partes.append(f"- {rotulo} `{relativo(pasta / a['arquivo'])}`: {descricao}")
        partes.append("")
    return "\n".join(partes)


def gravar(id_: str, dados: dict) -> dict:
    pasta = pasta_de(id_)
    pasta.mkdir(parents=True, exist_ok=True)
    dados["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
    (pasta / MANIFESTO).write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (pasta / TEXTO).write_text(markdown(dados, pasta), encoding="utf-8")
    return ler(id_)


def ler_legado(id_: str) -> dict | None:
    arquivo = PASTA / f"{id_}.md"
    if not arquivo.is_file():
        return None
    texto = arquivo.read_text(encoding="utf-8")
    titulo = re.match(r"#\s+(?:Contexto:\s*)?(.+)", texto)
    return {"id": id_, "nome": titulo.group(1).strip() if titulo else id_, "texto": texto, "anexos": [],
            "legado": True, "caminho": relativo(arquivo),
            "atualizado_em": datetime.fromtimestamp(arquivo.stat().st_mtime).isoformat(timespec="seconds")}


def ler(id_: str) -> dict | None:
    pasta = pasta_de(id_)
    if not (pasta / MANIFESTO).is_file():
        return ler_legado(id_)
    dados = json.loads((pasta / MANIFESTO).read_text(encoding="utf-8"))
    return {**dados, "id": id_, "legado": False, "caminho": relativo(pasta / TEXTO)}


def listar() -> list[dict]:
    ids = [p.parent.name for p in PASTA.glob(f"*/{MANIFESTO}")] + [p.stem for p in PASTA.glob("*.md")]
    saida = []
    for id_ in sorted(set(i for i in ids if ID.fullmatch(i))):
        c = ler(id_)
        if c is None:
            continue
        anexos = c.get("anexos") or []
        texto = re.sub(r"(?m)^#+\s*", "", re.sub(r"\A\s*#[^\n]*", "", c.get("texto") or ""))  # sem o título
        saida.append({"id": id_, "nome": c["nome"], "caminho": c["caminho"], "legado": c["legado"],
                      "atualizado_em": c.get("atualizado_em"), "resumo": re.sub(r"\s+", " ", texto).strip()[:160],
                      "imagens": [a["arquivo"] for a in anexos if a["tipo"] == "imagem"],
                      "audios": sum(1 for a in anexos if a["tipo"] == "audio")})
    return sorted(saida, key=lambda c: c["nome"].lower())


def criar(nome: str) -> dict:
    base = etapas.slug_de(nome)
    id_, n = base, 2
    while pasta_de(id_).exists() or (PASTA / f"{id_}.md").exists():
        id_, n = f"{base}-{n}", n + 1
    return gravar(id_, {"nome": nome, "texto": "", "anexos": [], "criado_em": date.today().isoformat()})


def editavel(id_: str) -> dict:
    c = ler(id_)
    if c is None:
        raise KeyError(id_)
    if c["legado"]:
        raise ValueError(f"{c['caminho']} é um contexto antigo, só de texto: edite o arquivo ou crie um contexto novo")
    return c


def salvar(id_: str, nome: str, texto: str, descricoes: dict[str, str]) -> dict:
    c = editavel(id_)
    for a in c["anexos"]:
        if a["arquivo"] in descricoes:
            a["descricao"] = descricoes[a["arquivo"]]
    return gravar(id_, {**c, "nome": nome.strip() or c["nome"], "texto": texto})


def anexar(id_: str, nome_original: str, fluxo: BinaryIO, descricao: str = "") -> dict:
    c = editavel(id_)
    tipo = tipo_de(nome_original)
    if tipo is None:
        aceitas = ", ".join(sorted(EXTENSOES_IMAGEM | EXTENSOES_AUDIO))
        raise ValueError(f"o anexo precisa ser imagem ou áudio ({aceitas})")
    pasta, ext = pasta_de(id_), Path(nome_original).suffix.lower()
    base = etapas.slug_de(Path(nome_original).stem, 10)
    destino, n = pasta / f"{base}{ext}", 2
    while destino.exists() or destino.name in (MANIFESTO, TEXTO):
        destino, n = pasta / f"{base}-{n}{ext}", n + 1
    with destino.open("wb") as f:
        shutil.copyfileobj(fluxo, f)
    c["anexos"].append({"arquivo": destino.name, "tipo": tipo, "descricao": descricao.strip()})
    return gravar(id_, c)


def remover_anexo(id_: str, arquivo: str) -> dict:
    c = editavel(id_)
    if not any(a["arquivo"] == arquivo for a in c["anexos"]):
        raise KeyError(arquivo)
    (pasta_de(id_) / arquivo).unlink(missing_ok=True)
    c["anexos"] = [a for a in c["anexos"] if a["arquivo"] != arquivo]
    return gravar(id_, c)


def apagar(id_: str) -> None:
    editavel(id_)
    shutil.rmtree(pasta_de(id_))


def arquivo(id_: str, nome: str) -> Path | None:
    """Um anexo do contexto, para a interface mostrar; só os que estão no manifesto."""
    c = ler(id_)
    if not c or not any(a["arquivo"] == nome for a in c.get("anexos") or []):
        return None
    return pasta_de(id_) / nome
