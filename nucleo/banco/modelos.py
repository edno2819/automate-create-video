"""Tabelas do banco (SQLAlchemy 2, tipado). O esquema é o de docs/arquitetura.md, seção Banco.

Postgres em produção e SQLite em desenvolvimento e testes, com as mesmas definições:

- JSON vira `jsonb` no Postgres (`with_variant`);
- datas são sempre UTC com fuso (`DataUTC`): o SQLite não guarda o fuso, então ele é posto de volta na leitura;
- ids das tabelas que crescem muito (log, custos) são bigint no Postgres e INTEGER no SQLite (onde só INTEGER
  PRIMARY KEY é autoincremento).

A migração inicial do Alembic (migracoes/versions/0001_inicial.py) cria exatamente isto; mudou uma tabela aqui,
escreva a migração correspondente.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

STATUS_ATIVOS = ("pendente", "rodando")
STATUS_FINAIS = ("ok", "erro", "cancelada", "interrompida")
FILTRO_ATIVAS = text("status IN ('pendente', 'rodando')")


class DataUTC(TypeDecorator):
    """Data e hora com fuso, sempre em UTC, nos dois bancos."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, valor: datetime | None, dialeto) -> datetime | None:
        if valor is None:
            return None
        if valor.tzinfo is None:
            valor = valor.replace(tzinfo=timezone.utc)
        return valor.astimezone(timezone.utc)

    def process_result_value(self, valor: datetime | None, dialeto) -> datetime | None:
        if valor is None:
            return None
        return valor.replace(tzinfo=timezone.utc) if valor.tzinfo is None else valor.astimezone(timezone.utc)


def Json(nulo: bool = False) -> Any:
    """JSON no SQLite, jsonb no Postgres. `nulo=True` guarda None como NULL de SQL, não como o JSON null."""
    return JSON(none_as_null=nulo).with_variant(JSONB(none_as_null=nulo), "postgresql")


IdGrande = BigInteger().with_variant(Integer(), "sqlite")


def agora() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Projeto(Base):
    __tablename__ = "projetos"

    slug: Mapped[str] = mapped_column(String(120), primary_key=True)
    titulo: Mapped[str] = mapped_column(Text)
    etapa: Mapped[str] = mapped_column(String(40), default="configurar")
    formulario: Mapped[dict] = mapped_column(Json(), default=dict)
    sugestoes: Mapped[dict | None] = mapped_column(Json(nulo=True), nullable=True)
    origem: Mapped[str] = mapped_column(String(40), default="pipeline")
    criado_em: Mapped[datetime] = mapped_column(DataUTC, default=agora)
    atualizado_em: Mapped[datetime] = mapped_column(DataUTC, default=agora)


class Execucao(Base):
    __tablename__ = "execucoes"
    __table_args__ = (
        # uma execução ativa por projeto, e uma por chave exclusiva (execuções sem projeto: personagem, referência)
        Index("execucoes_ativa_por_projeto", "projeto_slug", unique=True,
              postgresql_where=FILTRO_ATIVAS, sqlite_where=FILTRO_ATIVAS),
        Index("execucoes_ativa_por_chave", "chave_exclusiva", unique=True,
              postgresql_where=FILTRO_ATIVAS, sqlite_where=FILTRO_ATIVAS),
        Index("execucoes_por_etapa", "projeto_slug", "etapa", "id"),
        Index("execucoes_por_status", "status", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    projeto_slug: Mapped[str | None] = mapped_column(
        String(120), ForeignKey("projetos.slug", ondelete="CASCADE"), nullable=True)
    etapa: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="pendente")
    parametros: Mapped[dict] = mapped_column(Json(), default=dict)
    rota: Mapped[dict] = mapped_column(Json(), default=dict)
    resultado: Mapped[str] = mapped_column(Text, default="")
    estruturado: Mapped[Any | None] = mapped_column(Json(nulo=True), nullable=True)
    avisos: Mapped[list] = mapped_column(Json(), default=list)
    custo_usd: Mapped[float] = mapped_column(Float, default=0.0)
    trabalhador: Mapped[str | None] = mapped_column(String(200), nullable=True)
    chave_exclusiva: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cancelar_solicitado: Mapped[bool] = mapped_column(Boolean, default=False)
    criada_em: Mapped[datetime] = mapped_column(DataUTC, default=agora)
    iniciada_em: Mapped[datetime | None] = mapped_column(DataUTC, nullable=True)
    terminada_em: Mapped[datetime | None] = mapped_column(DataUTC, nullable=True)
    batimento_em: Mapped[datetime | None] = mapped_column(DataUTC, nullable=True)


class ExecucaoLog(Base):
    __tablename__ = "execucao_logs"
    __table_args__ = (Index("execucao_logs_por_execucao", "execucao_id", "id"),)

    id: Mapped[int] = mapped_column(IdGrande, primary_key=True, autoincrement=True)
    execucao_id: Mapped[int] = mapped_column(Integer, ForeignKey("execucoes.id", ondelete="CASCADE"))
    texto: Mapped[str] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(DataUTC, default=agora)


class Custo(Base):
    __tablename__ = "custos"
    __table_args__ = (Index("custos_por_projeto", "projeto_slug"), Index("custos_por_execucao", "execucao_id"))

    id: Mapped[int] = mapped_column(IdGrande, primary_key=True, autoincrement=True)
    execucao_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("execucoes.id", ondelete="SET NULL"), nullable=True)
    projeto_slug: Mapped[str | None] = mapped_column(String(120), nullable=True)
    servico: Mapped[str] = mapped_column(String(20))
    provedor: Mapped[str] = mapped_column(String(40))
    modelo: Mapped[str] = mapped_column(String(200))
    agente: Mapped[str | None] = mapped_column(String(80), nullable=True)
    item: Mapped[str | None] = mapped_column(String(200), nullable=True)
    uso: Mapped[dict] = mapped_column(Json(), default=dict)
    extra: Mapped[dict] = mapped_column(Json(), default=dict)
    custo_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimado: Mapped[bool] = mapped_column(Boolean, default=False)
    quando: Mapped[datetime] = mapped_column(DataUTC, default=agora)


class Arquivo(Base):
    __tablename__ = "arquivos"
    __table_args__ = (Index("arquivos_por_projeto", "projeto_slug"),)

    id: Mapped[int] = mapped_column(IdGrande, primary_key=True, autoincrement=True)
    caminho: Mapped[str] = mapped_column(String(1024), unique=True)
    escopo: Mapped[str] = mapped_column(String(20))
    projeto_slug: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tamanho: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mime: Mapped[str | None] = mapped_column(String(120), nullable=True)
    texto: Mapped[str | None] = mapped_column(Text, nullable=True)
    modificado_em: Mapped[datetime | None] = mapped_column(DataUTC, nullable=True)
    versao: Mapped[int] = mapped_column(Integer, default=1)
    atualizado_em: Mapped[datetime] = mapped_column(DataUTC, default=agora)
    apagado_em: Mapped[datetime | None] = mapped_column(DataUTC, nullable=True)


class Configuracao(Base):
    __tablename__ = "configuracoes"

    chave: Mapped[str] = mapped_column(String(120), primary_key=True)
    valor: Mapped[Any] = mapped_column(Json(nulo=True), nullable=True)
    atualizado_em: Mapped[datetime] = mapped_column(DataUTC, default=agora)


class ChaveApi(Base):
    __tablename__ = "chaves_api"

    provedor: Mapped[str] = mapped_column(String(40), primary_key=True)
    valor_cifrado: Mapped[str] = mapped_column(Text)
    mascara: Mapped[str] = mapped_column(String(40))
    atualizado_em: Mapped[datetime] = mapped_column(DataUTC, default=agora)


class Contexto(Base):
    __tablename__ = "contextos"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    nome: Mapped[str] = mapped_column(Text, default="")
    texto: Mapped[str] = mapped_column(Text, default="")
    anexos: Mapped[list] = mapped_column(Json(), default=list)
    criado_em: Mapped[datetime] = mapped_column(DataUTC, default=agora)
    atualizado_em: Mapped[datetime] = mapped_column(DataUTC, default=agora)


class LicaoRoteiro(Base):
    __tablename__ = "licoes_roteiro"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    secao: Mapped[str] = mapped_column(Text, default="")
    regra: Mapped[str] = mapped_column(Text)
    erro: Mapped[str] = mapped_column(Text, default="")
    correcao: Mapped[str] = mapped_column(Text, default="")
    projeto_slug: Mapped[str | None] = mapped_column(String(120), nullable=True)
    versao: Mapped[int | None] = mapped_column(Integer, nullable=True)
    criada_em: Mapped[datetime] = mapped_column(DataUTC, default=agora)


class Desempenho(Base):
    __tablename__ = "desempenho"
    __table_args__ = (UniqueConstraint("video_id", "janela", name="desempenho_video_janela"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    projeto_slug: Mapped[str | None] = mapped_column(String(120), nullable=True)
    video_id: Mapped[str] = mapped_column(String(120))
    janela: Mapped[str] = mapped_column(String(10))
    formato: Mapped[str] = mapped_column(String(20), default="longo")
    tecnicas: Mapped[list] = mapped_column(Json(), default=list)
    tipo_gancho: Mapped[str] = mapped_column(String(120), default="")
    metricas: Mapped[dict] = mapped_column(Json(), default=dict)
    extra: Mapped[dict] = mapped_column(Json(), default=dict)
    obs: Mapped[str] = mapped_column(Text, default="")
    registrado_em: Mapped[datetime] = mapped_column(DataUTC, default=agora)
