"""Armazenamento em MinIO ou AWS S3, pelo boto3 (a mesma API nos dois).

- MinIO: `endpoint_url` (ex.: http://minio:9000), endereçamento por caminho (`http://minio:9000/<bucket>/...`) e
  assinatura v4; o bucket é criado no arranque se não existir.
- AWS S3: sem endpoint (ou um compatível), na região configurada; credenciais das variáveis AWS_* ou do papel
  IAM da máquina. O bucket normalmente já existe: só é criado se faltar e a permissão deixar.

Arquivos grandes (vídeo, áudio) sobem com `upload_file`, que divide em partes (multipart) e repete o que falhar.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError

from nucleo.armazenamento.base import Armazenamento, Objeto, mime_de

TRANSFERENCIA = TransferConfig(multipart_threshold=16 * 1024 * 1024, multipart_chunksize=16 * 1024 * 1024,
                               max_concurrency=4)
NAO_EXISTE = ("404", "NoSuchKey", "NotFound", "NoSuchBucket")


def _codigo(e: ClientError) -> str:
    return str(e.response.get("Error", {}).get("Code", ""))


def _status(e: ClientError) -> int:
    return int(e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)


class S3Armazenamento(Armazenamento):
    def __init__(self, bucket: str, *, tipo: str = "s3", endpoint: str | None = None, regiao: str = "us-east-1",
                 chave: str | None = None, segredo: str | None = None):
        self.tipo = tipo  # "minio" | "s3" (atributo da instância: a classe serve aos dois)
        self.bucket = bucket
        self.endpoint = endpoint
        self.regiao = regiao
        opcoes = {"signature_version": "s3v4", "retries": {"max_attempts": 5, "mode": "standard"},
                  "connect_timeout": 10, "read_timeout": 120}
        if tipo == "minio" or endpoint:
            opcoes["s3"] = {"addressing_style": "path"}
        self.cliente = boto3.client(
            "s3", endpoint_url=endpoint if (tipo == "minio" or endpoint) else None, region_name=regiao,
            aws_access_key_id=chave or None, aws_secret_access_key=segredo or None, config=Config(**opcoes))

    def garantir_bucket(self) -> None:
        try:
            self.cliente.head_bucket(Bucket=self.bucket)
            return
        except ClientError as e:
            if _codigo(e) not in NAO_EXISTE and _status(e) != 404:
                if _status(e) == 403:
                    # existe, mas a credencial não pode consultá-lo: segue, e os envios dirão se podem gravar
                    print(f"aviso: sem permissão para conferir o bucket {self.bucket}; seguindo", file=sys.stderr)
                    return
                raise
        argumentos: dict = {"Bucket": self.bucket}
        if self.tipo == "s3" and self.regiao and self.regiao != "us-east-1":
            argumentos["CreateBucketConfiguration"] = {"LocationConstraint": self.regiao}
        try:
            self.cliente.create_bucket(**argumentos)
        except ClientError as e:
            if _codigo(e) in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                return
            raise RuntimeError(f"o bucket {self.bucket} não existe e não consegui criá-lo ({_codigo(e)}): "
                               "crie-o no console ou dê a permissão s3:CreateBucket") from e

    def enviar(self, chave: str, origem: Path, mime: str | None = None, metadados: dict | None = None) -> None:
        extra: dict = {"ContentType": mime or mime_de(chave)}
        if metadados:
            extra["Metadata"] = {str(k): str(v) for k, v in metadados.items()}
        self.cliente.upload_file(str(origem), self.bucket, chave, ExtraArgs=extra, Config=TRANSFERENCIA)

    def enviar_bytes(self, chave: str, dados: bytes, mime: str | None = None) -> None:
        self.cliente.put_object(Bucket=self.bucket, Key=chave, Body=dados, ContentType=mime or mime_de(chave))

    def baixar(self, chave: str, destino: Path) -> None:
        destino = Path(destino)
        destino.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=destino.parent, prefix=f".{destino.name}.", suffix=".parcial")
        os.close(fd)
        try:
            self.cliente.download_file(self.bucket, chave, tmp, Config=TRANSFERENCIA)
            os.replace(tmp, destino)
        except ClientError as e:
            Path(tmp).unlink(missing_ok=True)
            if _codigo(e) in NAO_EXISTE or _status(e) == 404:
                raise FileNotFoundError(chave) from e
            raise
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def ler_bytes(self, chave: str) -> bytes:
        try:
            return self.cliente.get_object(Bucket=self.bucket, Key=chave)["Body"].read()
        except ClientError as e:
            if _codigo(e) in NAO_EXISTE or _status(e) == 404:
                raise FileNotFoundError(chave) from e
            raise

    def existe(self, chave: str) -> bool:
        try:
            self.cliente.head_object(Bucket=self.bucket, Key=chave)
            return True
        except ClientError as e:
            if _codigo(e) in NAO_EXISTE or _status(e) == 404:
                return False
            raise

    def apagar(self, chave: str) -> None:
        self.cliente.delete_object(Bucket=self.bucket, Key=chave)

    def listar(self, prefixo: str) -> Iterator[Objeto]:
        paginas = self.cliente.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefixo)
        for pagina in paginas:
            for o in pagina.get("Contents", []):
                yield Objeto(chave=o["Key"], tamanho=o.get("Size", 0), etag=(o.get("ETag") or "").strip('"') or None,
                             modificado=o.get("LastModified"))

    def url_assinada(self, chave: str, expira_s: int = 3600) -> str | None:
        return self.cliente.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": chave},
                                                   ExpiresIn=expira_s)

    def descricao(self) -> str:
        onde = self.endpoint or f"AWS {self.regiao}"
        return f"{self.tipo} · bucket {self.bucket} · {onde}"
