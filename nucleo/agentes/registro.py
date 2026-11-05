"""Os agentes são dados: cada um é um arquivo agentes/<nome>.md, com um frontmatter simples e as instruções.

    ---
    name: roteirista
    description: ...
    nivel: julgamento          # julgamento | coleta — escolhe o modelo padrão do provedor (nucleo/rotas.py)
    ferramentas: Read, Write, Edit, Glob, MedirRoteiro
    visao: false               # true quando o agente precisa ver imagens
    max_turnos: 120
    ---
    (instruções)

O frontmatter é `chave: valor`, uma por linha (sem YAML de verdade: as descrições têm dois-pontos e o parser
simples não tropeça neles). agentes/_contexto.md é o contexto comum que o motor acrescenta a todo agente;
arquivos que começam com "_" não são agentes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from nucleo import RAIZ

PASTA = RAIZ / "agentes"
NIVEIS = ("julgamento", "coleta")
MAX_TURNOS_PADRAO = 120
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


class AgenteDesconhecido(LookupError):
    pass


@dataclass
class Agente:
    nome: str
    descricao: str
    nivel: str
    ferramentas: list[str]
    instrucoes: str
    visao: bool = False
    max_turnos: int = MAX_TURNOS_PADRAO
    extras: dict = field(default_factory=dict)  # outras chaves do frontmatter, para quem precisar


def _bool(valor: str) -> bool:
    return valor.strip().lower() in ("true", "sim", "yes", "1")


def ler_texto(texto: str, nome: str = "") -> Agente:
    """Um agente a partir do texto do arquivo (frontmatter + instruções)."""
    m = _FRONTMATTER.match(texto.replace("\r\n", "\n"))
    if not m:
        raise ValueError(f"agente {nome or '?'} sem frontmatter (--- ... ---)")
    meta: dict[str, str] = {}
    for linha in m.group(1).splitlines():
        if (c := re.match(r"\s*([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*$", linha)) and not linha.lstrip().startswith("#"):
            valor = c.group(2)
            if c.group(1) != "description":
                valor = re.sub(r"\s+#.*$", "", valor)  # comentário no fim da linha
            meta[c.group(1)] = valor.strip().strip("\"'")
    nome = meta.pop("name", "") or nome
    nivel = meta.pop("nivel", "julgamento") or "julgamento"
    if nivel not in NIVEIS:
        raise ValueError(f"agente {nome}: nivel '{nivel}' inválido (use {' ou '.join(NIVEIS)})")
    ferramentas = [f.strip() for f in meta.pop("ferramentas", "").split(",") if f.strip()]
    try:
        max_turnos = int(meta.pop("max_turnos", MAX_TURNOS_PADRAO) or MAX_TURNOS_PADRAO)
    except ValueError:
        raise ValueError(f"agente {nome}: max_turnos precisa ser um número") from None
    return Agente(nome=nome, descricao=meta.pop("description", ""), nivel=nivel, ferramentas=ferramentas,
                  instrucoes=m.group(2).strip(), visao=_bool(meta.pop("visao", "false")), max_turnos=max_turnos,
                  extras=meta)


@lru_cache(maxsize=None)
def _carregar(caminho: Path, mtime_ns: int) -> Agente:
    return ler_texto(caminho.read_text(encoding="utf-8"), caminho.stem)


def carregar(nome: str) -> Agente:
    """O agente pelo nome do arquivo. Relê o arquivo quando ele muda (editar um prompt não pede reinício)."""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", nome or ""):
        raise AgenteDesconhecido(f"nome de agente inválido: {nome!r}")
    caminho = PASTA / f"{nome}.md"
    try:
        return _carregar(caminho, caminho.stat().st_mtime_ns)
    except FileNotFoundError:
        raise AgenteDesconhecido(f"agente desconhecido: {nome}") from None


def nomes() -> list[str]:
    return sorted(p.stem for p in PASTA.glob("*.md") if not p.name.startswith("_"))


def todos() -> list[Agente]:
    return [carregar(n) for n in nomes()]


def contexto_comum() -> str:
    """agentes/_contexto.md: as diretrizes do projeto que valem para todo agente."""
    caminho = PASTA / "_contexto.md"
    return caminho.read_text(encoding="utf-8").strip() if caminho.exists() else ""
