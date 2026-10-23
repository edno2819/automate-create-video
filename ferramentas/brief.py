"""Leitura do brief de um projeto (00-brief.json), usada pelos scripts de produção.

O brief é gerado a partir do pedido aprovado (00-pedido.yaml) por ferramentas/pedido.py. Este módulo só
usa a biblioteca padrão, para que os scripts que rodam com python3 possam importá-lo:

    sys.path.insert(0, str(RAIZ / "ferramentas"))
    from brief import config, ler_brief
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PASTA_MUSICAS = RAIZ / "biblioteca" / "musicas"
EXTENSOES_AUDIO = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}


def sha1_arquivo(caminho: Path) -> str:
    return hashlib.sha1(caminho.read_bytes()).hexdigest()


def ler_brief(pasta: Path, avisar: bool = True) -> dict:
    """Lê 00-brief.json do projeto ({} se não existir). Avisa quando 00-pedido.yaml mudou depois do brief."""
    caminho = pasta / "00-brief.json"
    if not caminho.exists():
        return {}
    brief = json.loads(caminho.read_text(encoding="utf-8"))
    pedido = pasta / "00-pedido.yaml"
    sha = (brief.get("pedido") or {}).get("sha1")
    if avisar and sha and pedido.exists() and sha1_arquivo(pedido) != sha:
        print(f"aviso: {pedido} mudou depois do brief; rode `uv run --script ferramentas/pedido.py brief {pasta} 2>&1 | cat`",
              file=sys.stderr)
    return brief


def config(brief: dict, caminho: str, padrao=None):
    """brief["a"]["b"] para o caminho "a.b"; o padrão quando falta ou é nulo."""
    valor = brief
    for parte in caminho.split("."):
        if not isinstance(valor, dict) or valor.get(parte) is None:
            return padrao
        valor = valor[parte]
    return valor


def ppm_do_brief(brief: dict) -> int | None:
    """A velocidade pedida (narracao.palavras_por_minuto), que vale mais que o ppm do frontmatter do roteiro."""
    valor = config(brief, "narracao.palavras_por_minuto")
    return int(valor) if valor else None


def narracao_ativa(brief: dict) -> bool:
    """O pedido pode desligar a narração (narracao.ativa: não). O "audio": false dos briefs antigos não
    conta: os scripts nunca o seguiram, e projetos com ele foram narrados depois."""
    return bool(config(brief, "narracao.ativa", True))


def palavras_do_nome(texto: str) -> set[str]:
    sem_acento = "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")
    return set(re.findall(r"[a-z0-9]+", sem_acento))


def achar_musica(musica: dict, pasta: Path | None = None) -> Path | None:
    """O arquivo da música de fundo. Com musica.arquivo, procura o caminho como está, a partir do projeto e a
    partir da raiz; sem ele, procura em biblioteca/musicas/ um arquivo cujo nome tenha todas as palavras de
    musica.nome ("Stranger - Lory" acha "lory-stranger.mp3")."""
    arquivo = (musica or {}).get("arquivo")
    if arquivo:
        for base in (Path.cwd(), pasta, RAIZ):
            if base is not None and (base / arquivo).is_file():
                return (base / arquivo).resolve()
        return None
    nome = (musica or {}).get("nome") or ""
    if Path(nome).suffix.lower() in EXTENSOES_AUDIO:  # o nome veio com a extensão: "tema.mp3"
        nome = Path(nome).stem
    nome = palavras_do_nome(nome)
    if not nome or not PASTA_MUSICAS.is_dir():
        return None
    candidatos = [p for p in sorted(PASTA_MUSICAS.iterdir())
                  if p.suffix.lower() in EXTENSOES_AUDIO and nome <= palavras_do_nome(p.stem)]
    return min(candidatos, key=lambda p: len(p.stem)) if candidatos else None
