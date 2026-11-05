"""Contextos salvos: material de apoio reaproveitável entre vídeos (texto, imagens e áudios).

O contexto (nome, texto e a lista de anexos, cada um com a sua descrição) fica no banco, na tabela `contextos`. Os
anexos ficam no armazenamento, em `pedidos/contextos/<id>/`, junto com dois arquivos refeitos a cada gravação:

- `contexto.md`: o texto que os agentes leem, com o caminho e a descrição de cada anexo. O campo `contexto` do
  pedido aponta para ele, e o `pedido.py brief` copia o texto para o brief;
- `contexto.json`: o manifesto (nome, texto, anexos), que o `pedido.py brief` lê para pôr os anexos no brief.

Os `.md` soltos em `pedidos/contextos/` (o formato antigo) continuam valendo e aparecem na lista como contextos só
de texto, que a interface não edita. Uma pasta com `contexto.json` que ainda não está no banco (de uma instalação
antiga) entra no banco na primeira vez que a lista é lida.

Erros: id ou anexo desconhecido levanta KeyError (a API responde 404); operação recusada, ValueError (400).
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import BinaryIO

from nucleo import espaco
from nucleo.banco import repositorios
from web.api.comum import EXTENSOES_AUDIO, EXTENSOES_IMAGEM, gravar_upload, json_do_espaco, nome_livre, vivos

PREFIXO = "pedidos/contextos/"
MANIFESTO = "contexto.json"
TEXTO = "contexto.md"
ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
LIMITE_ANEXO_MB = 100
LIMITE_TEXTO = 500_000  # caracteres
LIMITE_DESCRICAO = 2000


def tipo_de(nome: str) -> str | None:
    ext = Path(nome).suffix.lower()
    return "imagem" if ext in EXTENSOES_IMAGEM else "audio" if ext in EXTENSOES_AUDIO else None


def validar_id(id_: str) -> str:
    if not isinstance(id_, str) or len(id_) > 120 or not ID.fullmatch(id_):
        raise KeyError(id_)
    return id_


def pasta_de(id_: str) -> str:
    """A pasta lógica do contexto no espaço (pedidos/contextos/<id>/)."""
    return f"{PREFIXO}{validar_id(id_)}/"


def slug_de(texto: str, max_palavras: int = 6) -> str:
    sem_acento = "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")
    palavras = re.findall(r"[a-z0-9]+", sem_acento)
    vazias = {"a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "um", "uma", "no", "na", "em"}
    uteis = [p for p in palavras if p not in vazias] or palavras
    return "-".join(uteis[:max_palavras])[:60].strip("-") or "contexto"


def markdown(dados: dict, id_: str) -> str:
    """O texto que os agentes leem: o do usuário e, no fim, a lista de anexos com caminho e descrição."""
    partes = [f"# Contexto: {dados['nome']}", ""]
    if (dados.get("texto") or "").strip():
        partes += [dados["texto"].strip(), ""]
    if dados.get("anexos"):
        partes += ["## Anexos", "",
                   "Arquivos que acompanham este contexto, com o caminho a partir da raiz do repositório. Abra as "
                   "imagens antes de usá-las. Os áudios não podem ser ouvidos: vale a descrição de cada um.", ""]
        for a in dados["anexos"]:
            rotulo = "Imagem" if a["tipo"] == "imagem" else "Áudio"
            descricao = (a.get("descricao") or "").strip() or "sem descrição"
            partes.append(f"- {rotulo} `{pasta_de(id_)}{a['arquivo']}`: {descricao}")
        partes.append("")
    return "\n".join(partes)


def _publico(registro: dict) -> dict:
    return {"id": registro["id"], "nome": registro.get("nome") or registro["id"], "texto": registro.get("texto") or "",
            "anexos": list(registro.get("anexos") or []), "criado_em": registro.get("criado_em"),
            "atualizado_em": registro.get("atualizado_em"), "legado": False, "caminho": f"{pasta_de(registro['id'])}{TEXTO}"}


def _gravar(id_: str, **campos) -> dict:
    """Grava no banco e refaz o contexto.md e o contexto.json no armazenamento."""
    registro = repositorios.salvar_contexto(id_, **campos)
    dados = _publico(registro)
    manifesto = {k: dados[k] for k in ("nome", "texto", "anexos", "criado_em", "atualizado_em")}
    espaco.gravar_texto(f"{pasta_de(id_)}{MANIFESTO}", json.dumps(manifesto, ensure_ascii=False, indent=2) + "\n")
    espaco.gravar_texto(f"{pasta_de(id_)}{TEXTO}", markdown(dados, id_))
    return dados


# ---------------------------------------------------------------- leitura


def _legados() -> dict[str, dict]:
    """Os .md soltos em pedidos/contextos/ (o formato antigo, só de texto), pelo índice do armazenamento."""
    saida = {}
    for caminho, r in vivos(PREFIXO).items():
        resto = caminho[len(PREFIXO):]
        if "/" not in resto and resto.endswith(".md") and ID.fullmatch(resto[:-3]):
            saida[resto[:-3]] = r
    return saida


def ler_legado(id_: str, registro: dict | None = None) -> dict | None:
    caminho = f"{PREFIXO}{validar_id(id_)}.md"
    registro = registro or _legados().get(id_)
    if registro is None:
        return None
    texto = repositorios.texto_do_arquivo(caminho)
    if texto is None:
        try:
            texto = espaco.garantir(caminho).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return None
    titulo = re.match(r"#\s+(?:Contexto:\s*)?(.+)", texto)
    return {"id": id_, "nome": titulo.group(1).strip() if titulo else id_, "texto": texto, "anexos": [],
            "legado": True, "caminho": caminho, "atualizado_em": registro.get("modificado_em") or registro.get("atualizado_em")}


def _importar_manifestos(ids_no_banco: set[str]) -> None:
    """Pastas com contexto.json no armazenamento e sem linha no banco (instalação antiga): entram no banco."""
    for caminho in vivos(PREFIXO):
        partes = caminho[len(PREFIXO):].split("/")
        if len(partes) != 2 or partes[1] != MANIFESTO or not ID.fullmatch(partes[0]) or partes[0] in ids_no_banco:
            continue
        doc = json_do_espaco(caminho)
        if not isinstance(doc, dict):
            continue
        anexos = [{"arquivo": a["arquivo"], "tipo": a.get("tipo") or tipo_de(a["arquivo"]) or "imagem",
                   "descricao": a.get("descricao") or ""}
                  for a in doc.get("anexos") or [] if isinstance(a, dict) and isinstance(a.get("arquivo"), str)]
        repositorios.salvar_contexto(partes[0], nome=str(doc.get("nome") or partes[0]), texto=str(doc.get("texto") or ""),
                                     anexos=anexos)
        ids_no_banco.add(partes[0])


def ler(id_: str) -> dict | None:
    validar_id(id_)
    if registro := repositorios.contexto(id_):
        return _publico(registro)
    _importar_manifestos({c["id"] for c in repositorios.contextos()})
    if registro := repositorios.contexto(id_):
        return _publico(registro)
    return ler_legado(id_)


def _resumo(texto: str) -> str:
    sem_titulo = re.sub(r"(?m)^#+\s*", "", re.sub(r"\A\s*#[^\n]*", "", texto or ""))
    return re.sub(r"\s+", " ", sem_titulo).strip()[:160]


def listar() -> list[dict]:
    registros = repositorios.contextos()
    ids = {c["id"] for c in registros}
    antes = len(ids)
    _importar_manifestos(ids)
    if len(ids) != antes:
        registros = repositorios.contextos()
    saida = []
    for r in registros:
        c = _publico(r)
        saida.append({"id": c["id"], "nome": c["nome"], "caminho": c["caminho"], "legado": False,
                      "atualizado_em": c["atualizado_em"], "resumo": _resumo(c["texto"]),
                      "imagens": [a["arquivo"] for a in c["anexos"] if a.get("tipo") == "imagem"],
                      "audios": sum(1 for a in c["anexos"] if a.get("tipo") == "audio")})
    for id_, registro in _legados().items():
        if id_ in ids or (c := ler_legado(id_, registro)) is None:
            continue
        saida.append({"id": id_, "nome": c["nome"], "caminho": c["caminho"], "legado": True, "atualizado_em": c["atualizado_em"],
                      "resumo": _resumo(c["texto"]), "imagens": [], "audios": 0})
    return sorted(saida, key=lambda c: c["nome"].lower())


# ---------------------------------------------------------------- escrita


def criar(nome: str, texto: str = "") -> dict:
    nome = (nome or "").strip()
    if not nome:
        raise ValueError("o nome é obrigatório")
    if len(texto) > LIMITE_TEXTO:
        raise ValueError("o texto do contexto é grande demais")
    base = slug_de(nome)
    ocupados = {c["id"] for c in repositorios.contextos()} | set(_legados())
    ocupados |= {c[len(PREFIXO):].split("/")[0] for c in vivos(PREFIXO) if "/" in c[len(PREFIXO):]}
    id_, n = base, 2
    while id_ in ocupados:
        id_, n = f"{base}-{n}", n + 1
    return _gravar(id_, nome=nome[:300], texto=texto, anexos=[])


def editavel(id_: str) -> dict:
    c = ler(id_)
    if c is None:
        raise KeyError(id_)
    if c["legado"]:
        raise ValueError(f"{c['caminho']} é um contexto antigo, só de texto: copie-o para um contexto novo para editar")
    return c


def salvar(id_: str, nome: str, texto: str, descricoes: dict[str, str]) -> dict:
    c = editavel(id_)
    if not isinstance(texto, str) or len(texto) > LIMITE_TEXTO:
        raise ValueError("o texto do contexto é grande demais")
    anexos = c["anexos"]
    for a in anexos:
        if isinstance(descricoes, dict) and isinstance(descricoes.get(a["arquivo"]), str):
            a["descricao"] = descricoes[a["arquivo"]][:LIMITE_DESCRICAO]
    return _gravar(id_, nome=(nome or "").strip()[:300] or c["nome"], texto=texto, anexos=anexos)


def anexar(id_: str, nome_original: str, fluxo: BinaryIO, descricao: str = "") -> dict:
    c = editavel(id_)
    tipo = tipo_de(nome_original)
    if tipo is None:
        aceitas = ", ".join(sorted(EXTENSOES_IMAGEM | EXTENSOES_AUDIO))
        raise ValueError(f"o anexo precisa ser imagem ou áudio ({aceitas})")
    ext = Path(nome_original).suffix.lower()
    base = slug_de(Path(nome_original).stem, 10)
    if f"{base}{ext}" in (MANIFESTO, TEXTO):
        base += "-anexo"
    caminho = nome_livre(pasta_de(id_), base, ext)
    gravar_upload(caminho, fluxo, LIMITE_ANEXO_MB)
    anexos = [*c["anexos"], {"arquivo": caminho.rsplit("/", 1)[1], "tipo": tipo, "descricao": (descricao or "").strip()[:LIMITE_DESCRICAO]}]
    return _gravar(id_, nome=c["nome"], texto=c["texto"], anexos=anexos)


def remover_anexo(id_: str, arquivo: str) -> dict:
    c = editavel(id_)
    if not any(a["arquivo"] == arquivo for a in c["anexos"]):
        raise KeyError(arquivo)
    espaco.apagar(f"{pasta_de(id_)}{arquivo}")
    anexos = [a for a in c["anexos"] if a["arquivo"] != arquivo]
    return _gravar(id_, nome=c["nome"], texto=c["texto"], anexos=anexos)


def apagar(id_: str) -> None:
    editavel(id_)
    espaco.apagar(pasta_de(id_))
    repositorios.apagar_contexto(id_)


def arquivo(id_: str, nome: str) -> str | None:
    """O caminho lógico de um anexo do contexto, para a interface mostrar; só os que estão no manifesto."""
    c = ler(id_)
    if not c or not any(a["arquivo"] == nome for a in c.get("anexos") or []):
        return None
    return f"{pasta_de(id_)}{nome}"
