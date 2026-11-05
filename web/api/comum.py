"""O que os roteadores da API dividem: validação de slugs e caminhos, a cópia local de um projeto, a recusa de
escrita durante uma execução, a entrega de arquivos e os scripts determinísticos que a própria web roda.

A web não guarda estado em memória. Cada requisição que lê arquivos de um projeto primeiro traz para a cópia local
o que mudou no armazenamento (`sincronizar_projeto`, barato quando nada mudou), e toda escrita sobe na hora
(`nucleo.espaco.gravar_texto`/`gravar_bytes`, ou `espaco.enviar` depois de um script). Escrever num projeto com
execução ativa é recusado com 409: quem está rodando é o trabalhador, e a cópia dele vale até ele terminar.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, BinaryIO

from fastapi import HTTPException
from fastapi.responses import FileResponse

from nucleo import RAIZ as RAIZ_CODIGO
from nucleo import espaco
from nucleo.banco import repositorios
from nucleo.config import config

# os módulos de ferramentas/ (pedido.py, custos.py) são importados pelo nome, como os scripts fazem
if str(RAIZ_CODIGO / "ferramentas") not in sys.path:
    sys.path.insert(0, str(RAIZ_CODIGO / "ferramentas"))

SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TAMANHO_SLUG = 120
TIMEOUT_SCRIPT_S = 300
EXTENSOES_IMAGEM = frozenset({".png", ".jpg", ".jpeg", ".webp"})
EXTENSOES_AUDIO = frozenset({".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"})
MEGA = 1024 * 1024


def raiz() -> Path:
    """A raiz do espaço de trabalho (projetos/, pedidos/, biblioteca/...): APP_RAIZ, ou a do repositório."""
    return config().raiz


def exigir(condicao: Any, mensagem: str, status: int = 400) -> None:
    if not condicao:
        raise HTTPException(status, mensagem)


def validar_slug(slug: str, o_que: str = "projeto") -> str:
    """Slugs de projeto, personagem e contexto: minúsculas, números e hífens. Qualquer outra coisa (`..`, barras,
    espaços) não chega a virar caminho: responde 404, como um projeto que não existe."""
    if not isinstance(slug, str) or len(slug) > TAMANHO_SLUG or not SLUG.fullmatch(slug):
        raise HTTPException(404, f"{o_que} não encontrado")
    return slug


def caminho_dentro(prefixo: str, relativo: str) -> str:
    """O caminho lógico `prefixo + relativo`, conferido: relativo, sem `..` nem partes vazias e dentro do
    prefixo. 400 se não for."""
    try:
        logico = espaco.validar(prefixo + relativo)
    except ValueError:
        raise HTTPException(400, "caminho inválido") from None
    if not logico.startswith(prefixo) or logico.endswith("/") or not relativo:
        raise HTTPException(400, "caminho inválido")
    return logico


# ---------------------------------------------------------------- projetos no espaço local


def prefixo(slug: str) -> str:
    return f"projetos/{slug}/"


def pasta(slug: str) -> Path:
    """A pasta local do projeto (pode não existir)."""
    return raiz() / "projetos" / slug


def pedido_web(slug: str) -> str:
    """O caminho lógico do pedido que a interface grava (só o que o usuário escreveu)."""
    return f"pedidos/web/{slug}.yaml"


def sincronizar_projeto(slug: str) -> Path:
    """Traz para a cópia local o que mudou no armazenamento (o projeto e o pedido da interface) e devolve a pasta."""
    espaco.baixar(prefixo(slug))
    garantir_se_existe(pedido_web(slug))
    return pasta(slug)


def garantir_se_existe(caminho: str) -> Path | None:
    try:
        return espaco.garantir(caminho)
    except FileNotFoundError:
        return None


def enviar_projeto(slug: str) -> None:
    espaco.enviar(prefixo(slug))


def em_andamento(slug: str) -> dict | None:
    """A execução pendente ou rodando no projeto (uma por projeto, o banco garante), ou None."""
    ativas = repositorios.execucoes_ativas(slug)
    return ativas[0] if ativas else None


def exigir_sem_execucao(slug: str) -> None:
    """409 quando há execução ativa: o trabalhador está mexendo nos arquivos do projeto."""
    if e := em_andamento(slug):
        raise HTTPException(409, f"há uma execução {'na fila' if e['status'] == 'pendente' else 'rodando'} neste projeto "
                                 f"({e['etapa']}, #{e['id']}): espere terminar ou cancele antes de mudar o projeto")


def existe_no_espaco(caminho: str) -> bool:
    """O arquivo existe no armazenamento (índice do banco, sem os apagados) ou na cópia local."""
    r = repositorios.arquivo(caminho)
    return bool(r and not r.get("apagado_em")) or (raiz() / caminho).is_file()


def vivos(prefixo_: str) -> dict[str, dict]:
    """O índice do armazenamento sob o prefixo, sem os arquivos apagados."""
    return {c: r for c, r in repositorios.indice(prefixo_).items() if not r.get("apagado_em")}


# ---------------------------------------------------------------- leitura de arquivos


def ler_json(caminho: Path) -> Any:
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ler_texto(caminho: Path) -> str | None:
    try:
        return caminho.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def json_do_espaco(caminho: str) -> Any:
    """O conteúdo de um JSON do armazenamento sem baixar o arquivo: o texto vem do índice do banco (arquivos de texto
    até 2 MB); maiores, ou sem texto guardado, são baixados."""
    texto = repositorios.texto_do_arquivo(caminho)
    if texto is None and (local := garantir_se_existe(caminho)):
        texto = ler_texto(local)
    try:
        return json.loads(texto) if texto else None
    except ValueError:
        return None


def servir(caminho: str, *, media_type: str | None = None, cabecalhos: dict | None = None) -> FileResponse:
    """Um arquivo do espaço (baixado se faltar ou estiver velho), com Range para vídeo e áudio. 404 se não existe."""
    try:
        arquivo = espaco.garantir(caminho)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "arquivo não encontrado") from None
    real, base = arquivo.resolve(), raiz().resolve()
    if not real.is_relative_to(base) or not real.is_file():  # um link simbólico para fora da raiz não sai daqui
        raise HTTPException(404, "arquivo não encontrado")
    return FileResponse(real, media_type=media_type, headers=cabecalhos)


# ---------------------------------------------------------------- uploads


def nome_livre(prefixo_: str, base: str, ext: str) -> str:
    """`prefixo/base.ext`, ou `base-2.ext`, `base-3.ext`... o primeiro que ainda não existe."""
    caminho, n = f"{prefixo_}{base}{ext}", 2
    while existe_no_espaco(caminho):
        caminho, n = f"{prefixo_}{base}-{n}{ext}", n + 1
    return caminho


def gravar_upload(caminho: str, fluxo: BinaryIO, limite_mb: int) -> Path:
    """Grava o arquivo enviado no espaço local (em pedaços, atômico) e sobe para o armazenamento. 413 se passar do
    limite. O temporário tem o nome que o espaço ignora, para uma sincronização no meio não levá-lo."""
    destino = raiz() / espaco.validar(caminho)
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario = destino.with_name(f".{destino.name}.{secrets.token_hex(4)}.parcial")
    total = 0
    try:
        with temporario.open("wb") as f:
            while bloco := fluxo.read(MEGA):
                total += len(bloco)
                if total > limite_mb * MEGA:
                    raise HTTPException(413, f"arquivo grande demais (limite de {limite_mb} MB)")
                f.write(bloco)
        os.replace(temporario, destino)
    finally:
        temporario.unlink(missing_ok=True)
    espaco.enviar(caminho)
    return destino


# ---------------------------------------------------------------- scripts determinísticos


def ambiente_dos_scripts() -> dict[str, str]:
    """O ambiente dos scripts que a web roda: o do servidor, com o código no PYTHONPATH e a raiz do espaço em
    APP_RAIZ. Nenhuma chave de provedor entra: esses scripts (pedido, medição, simulação, aprovação) não chamam API."""
    env = dict(os.environ)
    caminhos = [str(RAIZ_CODIGO)] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and p != str(RAIZ_CODIGO)]
    env["PYTHONPATH"] = os.pathsep.join(caminhos)
    env["APP_RAIZ"] = str(raiz())
    env.pop("EXECUCAO_ID", None)
    return env


def rodar_script(script: str, *args: object, timeout: int = TIMEOUT_SCRIPT_S) -> tuple[int, str]:
    """Um script de ferramentas/ com o Python do servidor, rodando na raiz do espaço de trabalho: os caminhos
    relativos dos argumentos (projetos/<slug>, pedidos/web/<slug>.yaml) valem a partir dela. Devolve (código, saída)."""
    try:
        r = subprocess.run([sys.executable, str(RAIZ_CODIGO / script), *map(str, args)], cwd=raiz(),
                           env=ambiente_dos_scripts(), capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, f"{script} passou de {timeout} s e foi interrompido"
    return r.returncode, (r.stdout + r.stderr).strip()


def ffmpeg_disponivel() -> bool:
    return shutil.which("ffmpeg") is not None
