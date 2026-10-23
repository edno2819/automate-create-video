"""Banco SQLite da interface web (dados/web.sqlite3).

Os arquivos de projetos/<slug>/ continuam sendo a fonte da verdade do pipeline. O banco guarda só o que é
da interface: o formulário como o usuário deixou, as sugestões da primeira etapa, a etapa em que ele está e
o histórico de cada execução do Claude Code (com o log que aparece na tela).

Cada chamada abre a própria conexão, porque as execuções gravam o log de outras threads.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
CAMINHO = RAIZ / "dados" / "web.sqlite3"

ESQUEMA = """
CREATE TABLE IF NOT EXISTS projetos (
    slug          TEXT PRIMARY KEY,
    titulo        TEXT NOT NULL,
    etapa         TEXT NOT NULL DEFAULT 'configurar',
    formulario    TEXT NOT NULL DEFAULT '{}',
    sugestoes     TEXT,
    criado_em     TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execucoes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT NOT NULL REFERENCES projetos(slug),
    etapa        TEXT NOT NULL,
    status       TEXT NOT NULL,          -- rodando | ok | erro | cancelada | interrompida
    sessao       TEXT,                   -- session_id do Claude Code, para retomar à mão com claude -r
    log          TEXT NOT NULL DEFAULT '',
    resultado    TEXT,
    custo_usd    REAL,
    iniciada_em  TEXT NOT NULL,
    terminada_em TEXT
);
CREATE INDEX IF NOT EXISTS execucoes_por_etapa ON execucoes (slug, etapa, id);
"""


def agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def conexao():
    CAMINHO.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(CAMINHO, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def iniciar() -> None:
    """Cria as tabelas e marca como interrompidas as execuções que o servidor anterior deixou rodando."""
    with conexao() as con:
        con.executescript(ESQUEMA)
        con.execute("UPDATE execucoes SET status = 'interrompida', terminada_em = ? WHERE status = 'rodando'", (agora(),))


def _projeto(linha: sqlite3.Row | None) -> dict | None:
    if linha is None:
        return None
    p = dict(linha)
    p["formulario"] = json.loads(p["formulario"] or "{}")
    p["sugestoes"] = json.loads(p["sugestoes"]) if p["sugestoes"] else None
    return p


def projeto(slug: str) -> dict | None:
    with conexao() as con:
        return _projeto(con.execute("SELECT * FROM projetos WHERE slug = ?", (slug,)).fetchone())


def projetos() -> list[dict]:
    with conexao() as con:
        return [_projeto(l) for l in con.execute("SELECT * FROM projetos ORDER BY atualizado_em DESC")]


def criar_projeto(slug: str, titulo: str, formulario: dict, etapa: str = "configurar") -> dict:
    with conexao() as con:
        con.execute("INSERT INTO projetos (slug, titulo, etapa, formulario, criado_em, atualizado_em) VALUES (?, ?, ?, ?, ?, ?)",
                    (slug, titulo, etapa, json.dumps(formulario, ensure_ascii=False), agora(), agora()))
    return projeto(slug)


def atualizar_projeto(slug: str, **campos) -> None:
    if not campos:
        return
    for chave in ("formulario", "sugestoes"):
        if chave in campos and campos[chave] is not None:
            campos[chave] = json.dumps(campos[chave], ensure_ascii=False)
    campos["atualizado_em"] = agora()
    nomes = ", ".join(f"{k} = ?" for k in campos)
    with conexao() as con:
        con.execute(f"UPDATE projetos SET {nomes} WHERE slug = ?", (*campos.values(), slug))


def criar_execucao(slug: str, etapa: str) -> int:
    with conexao() as con:
        cur = con.execute("INSERT INTO execucoes (slug, etapa, status, iniciada_em) VALUES (?, ?, 'rodando', ?)",
                          (slug, etapa, agora()))
        return cur.lastrowid


def anexar_log(id_: int, texto: str) -> None:
    with conexao() as con:
        con.execute("UPDATE execucoes SET log = log || ? WHERE id = ?", (texto, id_))


def atualizar_execucao(id_: int, **campos) -> None:
    nomes = ", ".join(f"{k} = ?" for k in campos)
    with conexao() as con:
        con.execute(f"UPDATE execucoes SET {nomes} WHERE id = ?", (*campos.values(), id_))


def execucao(id_: int) -> dict | None:
    with conexao() as con:
        linha = con.execute("SELECT * FROM execucoes WHERE id = ?", (id_,)).fetchone()
        return dict(linha) if linha else None


def custos_por_etapa(slug: str) -> list[dict]:
    """O que o Claude Code gastou em cada etapa do projeto, somando todas as execuções (as refeitas também)."""
    with conexao() as con:
        linhas = con.execute("SELECT etapa, COUNT(*) AS execucoes, COALESCE(SUM(custo_usd), 0) AS usd FROM execucoes "
                             "WHERE slug = ? GROUP BY etapa ORDER BY MIN(id)", (slug,)).fetchall()
        return [dict(l) for l in linhas]


def ultima_execucao(slug: str, etapa: str) -> dict | None:
    with conexao() as con:
        linha = con.execute("SELECT id, slug, etapa, status, sessao, resultado, custo_usd, iniciada_em, terminada_em, "
                            "length(log) AS tamanho_log FROM execucoes WHERE slug = ? AND etapa = ? ORDER BY id DESC LIMIT 1",
                            (slug, etapa)).fetchone()
        return dict(linha) if linha else None
