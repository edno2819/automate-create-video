"""Espaço de trabalho: a cópia local dos arquivos, sincronizada com o armazenamento de objetos e o índice no banco.

Agentes e scripts (ffmpeg, whisper) trabalham em arquivos locais, nos mesmos caminhos de sempre
(`projetos/<slug>/...`). A fonte da verdade é o armazenamento (MinIO ou S3) mais a tabela `arquivos` do banco,
que guarda de cada arquivo o sha256, a versão, a data original e, nos de texto, o conteúdo. Cada processo
(web, trabalhador) tem a sua cópia local e sincroniza por prefixo:

    from nucleo import espaco
    espaco.baixar("projetos/meu-video/")      # antes de ler ou rodar: traz o que mudou no armazenamento
    ...                                       # agentes e scripts trabalham nos arquivos locais
    espaco.enviar("projetos/meu-video/")      # depois: manda só o que mudou (sha256) e marca os apagados

Regras (docs/arquitetura.md, seção Espaço de trabalho):
- caminho lógico = caminho relativo à raiz com `/`; chave do objeto = prefixo da configuração + caminho;
- só os caminhos de SINCRONIZADOS circulam (o resto da biblioteca vem na imagem, com o código), menos IGNORADOS;
- o índice local (`.espaco/indice.json`: caminho → sha256, tamanho, mtime_ns, versão) diz o que este processo já
  sincronizou; o sha256 só é recalculado quando o tamanho ou o mtime mudou;
- o remoto ganha: versão remota maior que a local é baixada, mesmo que o arquivo local tenha mudado (com um
  aviso no log); um arquivo apagado no remoto é apagado aqui se o local ainda é a versão conhecida (ou, sem
  versão conhecida, se tem o mesmo conteúdo que foi apagado: a cópia velha que a imagem traz não volta);
- na baixa, o mtime original é restaurado (as etapas ainda usam data como desempate em projetos antigos);
- a exclusividade entre processos vem da execução ativa única por projeto (banco); aqui dentro, uma trava de
  thread por prefixo, e o índice é gravado sob trava de arquivo (vários workers da web na mesma pasta).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from nucleo.armazenamento import armazenamento, mime_de
from nucleo.banco import repositorios
from nucleo.config import config

SINCRONIZADOS = ("projetos/", "pedidos/", "rascunhos/", "biblioteca/esqueletos/", "biblioteca/personagens/",
                 "biblioteca/musicas/", "biblioteca/efeitos/", "biblioteca/vozes/", "biblioteca/tecnicas.json",
                 "biblioteca/canal.json", "biblioteca/licoes-roteiro.md", "biblioteca/aprendizados.md")
# Os outros arquivos de biblioteca/ (rubrica, formatos, preços) vêm na imagem, com o código.
IGNORADOS = ("**/montagem-*/**", "**/__pycache__/**", "**/*.tmp", "**/.capa.jpg", "**/.DS_Store")
EXTENSOES_TEXTO = frozenset({".md", ".json", ".jsonl", ".yaml", ".yml", ".txt", ".ass", ".srt"})
LIMITE_TEXTO = 2 * 1024 * 1024
PASTA_INDICE = ".espaco"
# nomes de temporários de gravação atômica (deste módulo e do armazenamento local): nunca sobem
_TEMPORARIO = re.compile(r"^\..+\.(parcial|gravando)$")

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------------- caminhos

def _glob_para_regex(padrao: str) -> re.Pattern:
    partes, i = [], 0
    while i < len(padrao):
        if padrao.startswith("**/", i):
            partes.append("(?:.*/)?")
            i += 3
        elif padrao.startswith("**", i):
            partes.append(".*")
            i += 2
        elif padrao[i] == "*":
            partes.append("[^/]*")
            i += 1
        elif padrao[i] == "?":
            partes.append("[^/]")
            i += 1
        else:
            partes.append(re.escape(padrao[i]))
            i += 1
    return re.compile("".join(partes))


_IGNORADOS_RE = tuple(_glob_para_regex(p) for p in IGNORADOS)


def ignorado(caminho: str) -> bool:
    nome = caminho.rsplit("/", 1)[-1]
    return bool(_TEMPORARIO.match(nome)) or any(r.fullmatch(caminho) for r in _IGNORADOS_RE)


def sincronizado(caminho: str) -> bool:
    """O caminho (de arquivo) é dos que circulam entre os processos?"""
    return any(caminho.startswith(s) if s.endswith("/") else caminho == s for s in SINCRONIZADOS)


def _pode_conter(pasta: str) -> bool:
    """Uma pasta (relativa, sem barra no fim) pode ter arquivos sincronizados dentro? Para não varrer o .venv."""
    p = pasta + "/"
    return any(s.startswith(p) or (s.endswith("/") and p.startswith(s)) for s in SINCRONIZADOS)


def validar(caminho: str) -> str:
    """O caminho lógico, conferido: relativo, com `/`, sem `..` nem partes vazias. ValueError senão."""
    if not isinstance(caminho, str) or not caminho or caminho.startswith("/") or "\\" in caminho or "\x00" in caminho:
        raise ValueError(f"caminho inválido: {caminho!r}")
    partes = caminho.rstrip("/").split("/")
    if any(p in ("", ".", "..") for p in partes):
        raise ValueError(f"caminho inválido: {caminho!r}")
    return caminho


def escopo_de(caminho: str) -> tuple[str, str | None]:
    """(escopo, projeto_slug) do caminho, pelo prefixo."""
    if caminho.startswith("projetos/"):
        partes = caminho.split("/")
        return "projeto", partes[1] if len(partes) > 2 else None
    if caminho.startswith("biblioteca/"):
        return "biblioteca", None
    if caminho.startswith("pedidos/contextos/"):
        return "contexto", None
    if caminho.startswith("pedidos/"):
        return "pedido", None
    if caminho.startswith("rascunhos/"):
        return "rascunho", None
    return "biblioteca", None


def chave_objeto(caminho: str) -> str:
    return config().prefixo + caminho


def local(caminho: str) -> Path:
    """O arquivo local de um caminho lógico (pode não existir)."""
    return config().raiz / validar(caminho)


def _sha256(arquivo: Path) -> str:
    h = hashlib.sha256()
    with open(arquivo, "rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


def _texto(arquivo: Path, caminho: str, tamanho: int) -> str | None:
    if Path(caminho).suffix.lower() not in EXTENSOES_TEXTO or tamanho > LIMITE_TEXTO:
        return None
    try:
        texto = arquivo.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    return None if "\x00" in texto else texto


def _data_iso(valor: str | None) -> datetime | None:
    return datetime.fromisoformat(valor) if valor else None


def _gravar_atomico(destino: Path, dados: bytes) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=destino.parent, prefix=f".{destino.name}.", suffix=".gravando")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(dados)
        os.replace(tmp, destino)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _copiar_atomico(origem: Path, destino: Path) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=destino.parent, prefix=f".{destino.name}.", suffix=".gravando")
    os.close(fd)
    try:
        shutil.copy2(origem, tmp)
        os.replace(tmp, destino)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _locais(prefixo: str) -> list[str]:
    """Os arquivos locais (sincronizados, não ignorados) cujo caminho começa com o prefixo."""
    raiz = config().raiz
    if prefixo.endswith("/"):
        inicio = prefixo.rstrip("/")
    else:
        inicio = prefixo.rsplit("/", 1)[0] if "/" in prefixo else ""
    base = raiz / inicio if inicio else raiz
    if base.is_file():
        ok = inicio.startswith(prefixo) and sincronizado(inicio) and not ignorado(inicio)
        return [inicio] if ok else []
    if not base.is_dir():
        return []
    achados = []
    for atual, pastas, arquivos in os.walk(base):
        rel = Path(atual).relative_to(raiz).as_posix()
        rel = "" if rel == "." else rel
        mantidas = []
        for p in sorted(pastas):
            sub = f"{rel}/{p}" if rel else p
            if p == PASTA_INDICE or not _pode_conter(sub) or ignorado(sub + "/x"):
                continue
            if not ((sub + "/").startswith(prefixo) or prefixo.startswith(sub + "/")):
                continue
            mantidas.append(p)
        pastas[:] = mantidas
        for nome in sorted(arquivos):
            c = f"{rel}/{nome}" if rel else nome
            if c.startswith(prefixo) and sincronizado(c) and not ignorado(c) and not os.path.islink(Path(atual) / nome):
                achados.append(c)
    return achados


# ---------------------------------------------------------------------------------------------- índice local

class _Indice:
    """O que este processo já sincronizou: caminho → {sha256, tamanho, mtime_ns, versao}.

    Lido do disco quando o arquivo mudou (outro worker da web na mesma pasta) e gravado sob trava de arquivo,
    relendo antes de aplicar as mudanças, para dois processos não apagarem o que o outro gravou."""

    def __init__(self, raiz: Path):
        self.pasta = raiz / PASTA_INDICE
        self.arquivo = self.pasta / "indice.json"
        self.trava = threading.Lock()
        self._dados: dict[str, dict] = {}
        self._assinatura: tuple[int, int] | None = None

    def _recarregar(self) -> None:
        try:
            st = self.arquivo.stat()
        except FileNotFoundError:
            self._dados, self._assinatura = {}, None
            return
        assinatura = (st.st_mtime_ns, st.st_size)
        if assinatura == self._assinatura:
            return
        try:
            self._dados = json.loads(self.arquivo.read_text(encoding="utf-8")).get("arquivos", {})
        except (OSError, ValueError, AttributeError):
            log.warning("índice local ilegível (%s): recomeçando do zero", self.arquivo)
            self._dados = {}
        self._assinatura = assinatura

    def ler(self) -> dict[str, dict]:
        with self.trava:
            self._recarregar()
            return dict(self._dados)

    @contextmanager
    def _trava_de_arquivo(self) -> Iterator[None]:
        self.pasta.mkdir(parents=True, exist_ok=True)
        with open(self.pasta / "indice.lock", "a+b") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def aplicar(self, mudancas: dict[str, dict | None]) -> None:
        if not mudancas:
            return
        with self.trava, self._trava_de_arquivo():
            self._assinatura = None
            self._recarregar()
            for caminho, entrada in mudancas.items():
                if entrada is None:
                    self._dados.pop(caminho, None)
                else:
                    self._dados[caminho] = entrada
            conteudo = json.dumps({"arquivos": self._dados}, ensure_ascii=False, separators=(",", ":"))
            _gravar_atomico(self.arquivo, conteudo.encode("utf-8"))
            st = self.arquivo.stat()
            self._assinatura = (st.st_mtime_ns, st.st_size)


_indices: dict[Path, _Indice] = {}
_indices_trava = threading.Lock()
_travas: dict[str, threading.RLock] = {}
_travas_guarda = threading.Lock()


def _indice() -> _Indice:
    raiz = config().raiz
    with _indices_trava:
        if raiz not in _indices:
            _indices[raiz] = _Indice(raiz)
        return _indices[raiz]


@contextmanager
def _trava(prefixo: str) -> Iterator[None]:
    with _travas_guarda:
        t = _travas.setdefault(f"{config().raiz}:{prefixo}", threading.RLock())
    with t:
        yield


def redefinir() -> None:
    """Esquece índices e travas em memória (testes que trocam a raiz)."""
    with _indices_trava:
        _indices.clear()
    with _travas_guarda:
        _travas.clear()


def _entrada(sha: str, st: os.stat_result, versao: int) -> dict:
    return {"sha256": sha, "tamanho": st.st_size, "mtime_ns": st.st_mtime_ns, "versao": versao}


def _inalterado(st: os.stat_result, entrada: dict | None) -> bool:
    return bool(entrada) and entrada["tamanho"] == st.st_size and entrada["mtime_ns"] == st.st_mtime_ns


def _limpar_pastas_vazias(arquivo: Path) -> None:
    raiz = config().raiz
    pai = arquivo.parent
    while pai != raiz and pai.is_dir() and raiz in pai.parents:
        rel = pai.relative_to(raiz).as_posix() + "/"
        if rel in SINCRONIZADOS:
            break  # a pasta-raiz de um sincronizado (projetos/, pedidos/...) fica
        try:
            pai.rmdir()
        except OSError:
            break
        pai = pai.parent


# -------------------------------------------------------------------------------------------- remoto → local

def _baixar_objeto(caminho: str, remoto: dict) -> dict | None:
    """Baixa um arquivo e restaura o mtime original. Devolve a entrada do índice (None se o objeto sumiu)."""
    destino = config().raiz / caminho
    try:
        armazenamento().baixar(chave_objeto(caminho), destino)
    except FileNotFoundError:
        texto = repositorios.texto_do_arquivo(caminho)
        if texto is None:
            log.warning("%s está no índice do banco mas não no armazenamento; ficou sem baixar", caminho)
            return None
        log.warning("%s não está no armazenamento; restaurei pelo texto guardado no banco", caminho)
        _gravar_atomico(destino, texto.encode("utf-8"))
    if quando := _data_iso(remoto.get("modificado_em")):
        st = destino.stat()
        os.utime(destino, ns=(st.st_atime_ns, int(quando.timestamp() * 1_000_000) * 1000))
    st = destino.stat()
    if remoto.get("tamanho") is not None and st.st_size != remoto["tamanho"]:
        log.warning("%s: baixei %d bytes, o índice diz %d", caminho, st.st_size, remoto["tamanho"])
    return _entrada(remoto.get("sha256") or _sha256(destino), st, remoto.get("versao") or 1)


def _sincronizar_entrada(caminho: str, remoto: dict, conhecida: dict | None) -> tuple[str, dict | None]:
    """Aplica ao arquivo local o que o remoto diz dele. Devolve (ação, nova entrada do índice), com ação
    "nada" | "indice" (só o índice muda) | "baixou" | "apagou"."""
    destino = config().raiz / caminho
    if remoto.get("apagado_em"):
        if conhecida is None:
            # Nunca sincronizado aqui. Com o mesmo conteúdo que foi apagado, é uma cópia velha (da imagem, de um
            # espaço antigo) que o próximo enviar poria de volta no armazenamento: sai também. Com outro conteúdo, é
            # um arquivo novo local, e o enviar decide.
            if destino.is_file() and remoto.get("sha256") and _sha256(destino) == remoto["sha256"]:
                destino.unlink()
                _limpar_pastas_vazias(destino)
                return "apagou", None
            return "nada", None
        if destino.is_file():
            st = destino.stat()
            if _inalterado(st, conhecida) or _sha256(destino) == conhecida["sha256"]:
                destino.unlink()
                _limpar_pastas_vazias(destino)
                return "apagou", None
            log.warning("%s foi apagado no armazenamento mas mudou aqui; fica a cópia local", caminho)
        return "indice", None
    if conhecida is not None and conhecida.get("versao", 0) >= (remoto.get("versao") or 0):
        return "nada", conhecida  # nada novo no remoto (se sumiu daqui, o enviar marca como apagado)
    if destino.is_file():
        st = destino.stat()
        if conhecida is not None and _inalterado(st, conhecida):
            if conhecida["sha256"] == remoto.get("sha256"):
                return "indice", {**conhecida, "versao": remoto.get("versao") or 1}
        else:
            sha = _sha256(destino)
            if sha == remoto.get("sha256"):
                return "indice", _entrada(sha, st, remoto.get("versao") or 1)
            if conhecida is not None and sha != conhecida["sha256"]:
                log.warning("%s mudou aqui e no armazenamento; fica a versão do armazenamento", caminho)
            elif conhecida is None:
                log.info("%s local difere do armazenamento (nunca sincronizado aqui); fica a do armazenamento", caminho)
    entrada = _baixar_objeto(caminho, remoto)
    return ("baixou", entrada) if entrada else ("nada", conhecida)


def baixar(prefixo: str) -> int:
    """Remoto → local, pelo índice do banco. Devolve quantos arquivos baixou."""
    validar(prefixo)
    with _trava(prefixo):
        remoto = repositorios.indice(prefixo)
        indice = _indice()
        conhecidos = indice.ler()
        mudancas: dict[str, dict | None] = {}
        baixados = 0
        try:
            for caminho, r in remoto.items():
                if not sincronizado(caminho) or ignorado(caminho):
                    continue
                acao, entrada = _sincronizar_entrada(caminho, r, conhecidos.get(caminho))
                if acao == "baixou":
                    baixados += 1
                if acao != "nada":
                    mudancas[caminho] = entrada
        finally:
            indice.aplicar(mudancas)
        return baixados


def garantir(caminho: str) -> Path:
    """O arquivo presente e atualizado no local (baixa se faltar ou estiver velho). FileNotFoundError se não
    existe em lugar nenhum. Arquivos fora dos sincronizados (rubrica, formatos) são lidos direto do local."""
    validar(caminho)
    destino = config().raiz / caminho
    if sincronizado(caminho) and not ignorado(caminho):
        with _trava(caminho):
            r = repositorios.arquivo(caminho)
            if r is not None:
                indice = _indice()
                acao, entrada = _sincronizar_entrada(caminho, r, indice.ler().get(caminho))
                if acao != "nada":
                    indice.aplicar({caminho: entrada})
    if destino.is_file():
        return destino
    raise FileNotFoundError(caminho)


# -------------------------------------------------------------------------------------------- local → remoto

def _enviar_um(caminho: str, st: os.stat_result, sha: str) -> dict:
    origem = config().raiz / caminho
    mime = mime_de(caminho)
    armazenamento().enviar(chave_objeto(caminho), origem, mime)
    escopo, slug = escopo_de(caminho)
    reg = repositorios.registrar_arquivo(
        caminho, escopo=escopo, projeto_slug=slug, tamanho=st.st_size, sha256=sha, mime=mime,
        texto=_texto(origem, caminho, st.st_size),
        modificado_em=datetime.fromtimestamp(st.st_mtime_ns / 1e9, timezone.utc))
    return _entrada(sha, st, reg["versao"])


def _enviar_arquivos(caminhos: Iterable[str], remoto: dict[str, dict], conhecidos: dict[str, dict],
                     mudancas: dict[str, dict | None]) -> int:
    """Envia os arquivos locais que mudaram desde a última sincronização. Devolve quantos subiram."""
    raiz = config().raiz
    enviados = 0
    for caminho in caminhos:
        try:
            st = (raiz / caminho).stat()
        except FileNotFoundError:
            continue
        l, r = conhecidos.get(caminho), remoto.get(caminho)
        r_vivo = r is not None and not r.get("apagado_em")
        if _inalterado(st, l) and r is not None:
            continue  # igual ao que já foi (ou, se apagado no remoto, o baixar decide)
        sha = l["sha256"] if _inalterado(st, l) else _sha256(raiz / caminho)
        if r_vivo and r.get("sha256") == sha:
            mudancas[caminho] = _entrada(sha, st, r.get("versao") or 1)  # o remoto já tem este conteúdo
            continue
        if l is not None and l["sha256"] == sha and r is not None:
            # só a data mudou: não manda um conteúdo velho por cima de um remoto mais novo (ou apagado)
            mudancas[caminho] = _entrada(sha, st, l.get("versao") or 1)
            continue
        if l is not None and r_vivo and (r.get("versao") or 0) > (l.get("versao") or 0):
            log.warning("%s mudou aqui e no armazenamento; fica a versão local", caminho)
        mudancas[caminho] = _enviar_um(caminho, st, sha)
        enviados += 1
    return enviados


def _apagar_remoto(caminho: str) -> None:
    repositorios.marcar_apagado(caminho)
    try:
        armazenamento().apagar(chave_objeto(caminho))
    except Exception as e:  # noqa: BLE001 - o índice já diz apagado; o objeto órfão só ocupa espaço
        log.warning("não consegui apagar o objeto de %s: %s", caminho, e)


def enviar(prefixo: str) -> int:
    """Local → remoto: só o que mudou desde a última sincronização (sha256); marca como apagado o que este
    processo já tinha sincronizado e sumiu do disco. Devolve quantos arquivos mudaram no remoto."""
    validar(prefixo)
    with _trava(prefixo):
        remoto = repositorios.indice(prefixo)
        indice = _indice()
        conhecidos = indice.ler()
        mudancas: dict[str, dict | None] = {}
        try:
            locais = _locais(prefixo)
            total = _enviar_arquivos(locais, remoto, conhecidos, mudancas)
            presentes = set(locais)
            raiz = config().raiz
            for caminho, l in conhecidos.items():
                if (not caminho.startswith(prefixo) or caminho in presentes or not sincronizado(caminho)
                        or ignorado(caminho) or (raiz / caminho).is_file()):
                    continue
                r = remoto.get(caminho)
                if r is not None and not r.get("apagado_em"):
                    if (r.get("versao") or 0) > (l.get("versao") or 0):
                        log.warning("%s foi apagado aqui mas mudou no armazenamento; fica a do armazenamento", caminho)
                    else:
                        _apagar_remoto(caminho)
                        total += 1
                mudancas[caminho] = None
        finally:
            indice.aplicar(mudancas)
        return total


def gravar_bytes(caminho: str, dados: bytes) -> Path:
    """Grava o arquivo local (atômico) e, se ele é dos sincronizados, envia na hora (escritas da web)."""
    validar(caminho)
    destino = config().raiz / caminho
    with _trava(caminho):
        _gravar_atomico(destino, dados)
        if sincronizado(caminho) and not ignorado(caminho):
            r = repositorios.arquivo(caminho)
            indice = _indice()
            mudancas: dict[str, dict | None] = {}
            try:
                _enviar_arquivos([caminho], {caminho: r} if r else {}, indice.ler(), mudancas)
            finally:
                indice.aplicar(mudancas)
    return destino


def gravar_texto(caminho: str, texto: str) -> Path:
    return gravar_bytes(caminho, texto.encode("utf-8"))


def apagar(caminho_ou_prefixo: str) -> int:
    """Apaga um arquivo (ou tudo sob uma pasta: `projetos/x/` ou `projetos/x`) no local e no remoto.
    Devolve quantos arquivos apagou."""
    validar(caminho_ou_prefixo)
    alvo = caminho_ou_prefixo
    pasta = alvo if alvo.endswith("/") else alvo + "/"
    if not (sincronizado(alvo) or sincronizado(pasta + "x")):
        raise ValueError(f"{alvo} não é um caminho sincronizado")
    raiz = config().raiz
    with _trava(alvo):
        remoto = repositorios.indice(pasta)
        if not alvo.endswith("/") and (r := repositorios.arquivo(alvo)):
            remoto[alvo] = r
        caminhos = {c for c, r in remoto.items() if not r.get("apagado_em")}
        caminhos.update(_locais(pasta))
        if not alvo.endswith("/") and (raiz / alvo).is_file():
            caminhos.add(alvo)
        for caminho in sorted(caminhos):
            arquivo = raiz / caminho
            if arquivo.is_file():
                arquivo.unlink()
            r = remoto.get(caminho)
            if r is not None and not r.get("apagado_em"):
                _apagar_remoto(caminho)
        if (raiz / pasta).is_dir():
            shutil.rmtree(raiz / pasta, ignore_errors=True)
        elif caminhos:
            _limpar_pastas_vazias(raiz / sorted(caminhos)[0])
        conhecidos = _indice().ler()
        _indice().aplicar({c: None for c in conhecidos if c == alvo or c.startswith(pasta)} | {c: None for c in caminhos})
        return len(caminhos)


def copiar(origem_prefixo: str, destino_prefixo: str,
           transformar_texto: Callable[[str], str] | None = None) -> int:
    """Copia tudo sob um prefixo para outro (fork de projeto), transformando o texto dos arquivos de texto
    quando pedido (ex.: trocar o slug), com o mtime preservado, e envia a cópia. Devolve quantos copiou.
    O prefixo é de texto: `projetos/a/05-roteiro-` copia só os roteiros."""
    validar(origem_prefixo)
    validar(destino_prefixo)
    if origem_prefixo.endswith("/") != destino_prefixo.endswith("/"):
        raise ValueError("os dois prefixos precisam terminar (ou não) em /")
    baixar(origem_prefixo)
    raiz = config().raiz
    copiados = 0
    for caminho in _locais(origem_prefixo):
        novo = destino_prefixo + caminho[len(origem_prefixo):]
        if not sincronizado(novo) or ignorado(novo):
            continue
        de, para = raiz / caminho, raiz / validar(novo)
        st = de.stat()
        texto = None
        if transformar_texto and Path(caminho).suffix.lower() in EXTENSOES_TEXTO:
            try:
                texto = de.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                texto = None
        if texto is not None:
            _gravar_atomico(para, transformar_texto(texto).encode("utf-8"))
        else:
            _copiar_atomico(de, para)
        os.utime(para, ns=(st.st_atime_ns, st.st_mtime_ns))
        copiados += 1
    enviar(destino_prefixo)
    return copiados


# --------------------------------------------------------------------------------------- arranque e migração

def inicializar() -> None:
    """No arranque: semente (o que existe aqui e o armazenamento ainda não conhece sobe; o que foi apagado lá não
    volta) e depois baixa todos os sincronizados."""
    for prefixo in SINCRONIZADOS:
        with _trava(prefixo):
            remoto = repositorios.indice(prefixo)
            novos = [c for c in _locais(prefixo) if c not in remoto]
            if not novos:
                continue
            indice = _indice()
            mudancas: dict[str, dict | None] = {}
            try:
                n = _enviar_arquivos(novos, remoto, indice.ler(), mudancas)
            finally:
                indice.aplicar(mudancas)
            log.info("semente: %d arquivo(s) de %s enviados", n, prefixo)
    for prefixo in SINCRONIZADOS:
        baixar(prefixo)


def importar_tudo() -> dict:
    """Migração de uma instalação local: envia projetos/, pedidos/, rascunhos/ e as partes sincronizadas da
    biblioteca. Idempotente (o que o remoto já tem com o mesmo sha256 não sobe de novo). {prefixo: enviados}."""
    return {prefixo: enviar(prefixo) for prefixo in SINCRONIZADOS}
