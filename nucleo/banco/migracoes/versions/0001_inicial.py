"""Esquema inicial: projetos, execuções e log, custos, índice de arquivos, configurações, chaves, contextos,
lições de roteiro e desempenho (docs/arquitetura.md, seção Banco).

Revision ID: 0001_inicial
Revises:
Create Date: 2026-10-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_inicial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ATIVAS = sa.text("status IN ('pendente', 'rodando')")


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _data() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True)


def _id_grande() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "projetos",
        sa.Column("slug", sa.String(120), primary_key=True),
        sa.Column("titulo", sa.Text(), nullable=False),
        sa.Column("etapa", sa.String(40), nullable=False),
        sa.Column("formulario", _json(), nullable=False),
        sa.Column("sugestoes", _json(), nullable=True),
        sa.Column("origem", sa.String(40), nullable=False),
        sa.Column("criado_em", _data(), nullable=False),
        sa.Column("atualizado_em", _data(), nullable=False),
    )
    op.create_table(
        "execucoes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("projeto_slug", sa.String(120), sa.ForeignKey("projetos.slug", ondelete="CASCADE"), nullable=True),
        sa.Column("etapa", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("parametros", _json(), nullable=False),
        sa.Column("rota", _json(), nullable=False),
        sa.Column("resultado", sa.Text(), nullable=False),
        sa.Column("estruturado", _json(), nullable=True),
        sa.Column("avisos", _json(), nullable=False),
        sa.Column("custo_usd", sa.Float(), nullable=False),
        sa.Column("trabalhador", sa.String(200), nullable=True),
        sa.Column("chave_exclusiva", sa.String(200), nullable=True),
        sa.Column("cancelar_solicitado", sa.Boolean(), nullable=False),
        sa.Column("criada_em", _data(), nullable=False),
        sa.Column("iniciada_em", _data(), nullable=True),
        sa.Column("terminada_em", _data(), nullable=True),
        sa.Column("batimento_em", _data(), nullable=True),
    )
    op.create_index("execucoes_ativa_por_projeto", "execucoes", ["projeto_slug"], unique=True,
                    postgresql_where=ATIVAS, sqlite_where=ATIVAS)
    op.create_index("execucoes_ativa_por_chave", "execucoes", ["chave_exclusiva"], unique=True,
                    postgresql_where=ATIVAS, sqlite_where=ATIVAS)
    op.create_index("execucoes_por_etapa", "execucoes", ["projeto_slug", "etapa", "id"])
    op.create_index("execucoes_por_status", "execucoes", ["status", "id"])

    op.create_table(
        "execucao_logs",
        sa.Column("id", _id_grande(), primary_key=True, autoincrement=True),
        sa.Column("execucao_id", sa.Integer(), sa.ForeignKey("execucoes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("criado_em", _data(), nullable=False),
    )
    op.create_index("execucao_logs_por_execucao", "execucao_logs", ["execucao_id", "id"])

    op.create_table(
        "custos",
        sa.Column("id", _id_grande(), primary_key=True, autoincrement=True),
        sa.Column("execucao_id", sa.Integer(), sa.ForeignKey("execucoes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("projeto_slug", sa.String(120), nullable=True),
        sa.Column("servico", sa.String(20), nullable=False),
        sa.Column("provedor", sa.String(40), nullable=False),
        sa.Column("modelo", sa.String(200), nullable=False),
        sa.Column("agente", sa.String(80), nullable=True),
        sa.Column("item", sa.String(200), nullable=True),
        sa.Column("uso", _json(), nullable=False),
        sa.Column("extra", _json(), nullable=False),
        sa.Column("custo_usd", sa.Float(), nullable=True),
        sa.Column("estimado", sa.Boolean(), nullable=False),
        sa.Column("quando", _data(), nullable=False),
    )
    op.create_index("custos_por_projeto", "custos", ["projeto_slug"])
    op.create_index("custos_por_execucao", "custos", ["execucao_id"])

    op.create_table(
        "arquivos",
        sa.Column("id", _id_grande(), primary_key=True, autoincrement=True),
        sa.Column("caminho", sa.String(1024), nullable=False, unique=True),
        sa.Column("escopo", sa.String(20), nullable=False),
        sa.Column("projeto_slug", sa.String(120), nullable=True),
        sa.Column("tamanho", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("mime", sa.String(120), nullable=True),
        sa.Column("texto", sa.Text(), nullable=True),
        sa.Column("modificado_em", _data(), nullable=True),
        sa.Column("versao", sa.Integer(), nullable=False),
        sa.Column("atualizado_em", _data(), nullable=False),
        sa.Column("apagado_em", _data(), nullable=True),
    )
    op.create_index("arquivos_por_projeto", "arquivos", ["projeto_slug"])

    op.create_table(
        "configuracoes",
        sa.Column("chave", sa.String(120), primary_key=True),
        sa.Column("valor", _json(), nullable=True),
        sa.Column("atualizado_em", _data(), nullable=False),
    )
    op.create_table(
        "chaves_api",
        sa.Column("provedor", sa.String(40), primary_key=True),
        sa.Column("valor_cifrado", sa.Text(), nullable=False),
        sa.Column("mascara", sa.String(40), nullable=False),
        sa.Column("atualizado_em", _data(), nullable=False),
    )
    op.create_table(
        "contextos",
        sa.Column("id", sa.String(120), primary_key=True),
        sa.Column("nome", sa.Text(), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("anexos", _json(), nullable=False),
        sa.Column("criado_em", _data(), nullable=False),
        sa.Column("atualizado_em", _data(), nullable=False),
    )
    op.create_table(
        "licoes_roteiro",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("secao", sa.Text(), nullable=False),
        sa.Column("regra", sa.Text(), nullable=False),
        sa.Column("erro", sa.Text(), nullable=False),
        sa.Column("correcao", sa.Text(), nullable=False),
        sa.Column("projeto_slug", sa.String(120), nullable=True),
        sa.Column("versao", sa.Integer(), nullable=True),
        sa.Column("criada_em", _data(), nullable=False),
    )
    op.create_table(
        "desempenho",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("projeto_slug", sa.String(120), nullable=True),
        sa.Column("video_id", sa.String(120), nullable=False),
        sa.Column("janela", sa.String(10), nullable=False),
        sa.Column("formato", sa.String(20), nullable=False),
        sa.Column("tecnicas", _json(), nullable=False),
        sa.Column("tipo_gancho", sa.String(120), nullable=False),
        sa.Column("metricas", _json(), nullable=False),
        sa.Column("extra", _json(), nullable=False),
        sa.Column("obs", sa.Text(), nullable=False),
        sa.Column("registrado_em", _data(), nullable=False),
        sa.UniqueConstraint("video_id", "janela", name="desempenho_video_janela"),
    )


def downgrade() -> None:
    for tabela in ("desempenho", "licoes_roteiro", "contextos", "chaves_api", "configuracoes", "arquivos",
                   "custos", "execucao_logs", "execucoes", "projetos"):
        op.drop_table(tabela)
