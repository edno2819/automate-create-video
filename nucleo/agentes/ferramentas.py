"""As ferramentas que o motor oferece aos agentes, e o que cada uma pode tocar.

As de arquivo e de terminal têm os nomes e o formato que os prompts conhecem (Read, Write, Edit, Glob, Grep,
Bash, WebSearch, WebFetch). As nativas rodam os scripts determinísticos com argumentos tipados e caminhos
conferidos, sem o agente montar linha de comando:

    MedirRoteiro      ferramentas/medir_roteiro.py (medição, texto ouvido, orçamento)
    ConferirTexto     ferramentas/conferir_texto.py (a narração formatada é a mesma do original?)
    MontarCenas       ferramentas/cenas.py montar
    PesquisarYoutube  ferramentas/youtube_trends.py, com a chave do YouTube injetada só nesse processo
    Transcrever       ferramentas/fetch_youtube.py, sempre com a pasta de saída dentro do que é sincronizado

Segurança, sem ninguém para aprovar nada:
- nenhum segredo chega ao agente: o Bash e os scripts rodam com um ambiente mínimo (sem chave, sem
  DATABASE_URL), `$VAR`, `/proc`, `env` e afins são recusados, `.env`, `dados/` (segredo.key, banco local) e
  `.git` não se leem, e qualquer chave que apareça numa saída é trocada por •••;
- a escrita vale só em projetos/ (o projeto da execução, quando há um), biblioteca/esqueletos/, rascunhos/ e
  na pasta temporária: técnicas, lições e personagens da biblioteca mudam só pelos fluxos, com trava;
- o Bash só roda os comandos da lista, e o python3 só os scripts que os agentes usam;
- o WebFetch não abre endereço interno (rede privada, metadados da nuvem, o MinIO, o Postgres).
"""

from __future__ import annotations

import fnmatch
import glob as glob_mod
import ipaddress
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nucleo import RAIZ as CODIGO
from nucleo.agentes.erros import Cancelado, ErroMotor

if TYPE_CHECKING:
    from nucleo.agentes.motor import Rota
    from nucleo.agentes.registro import Agente

MAX_SAIDA_FERRAMENTA = 40_000
MAX_LEITURA = 120_000
TIMEOUT_PADRAO_S = 600
TIMEOUT_MAX_S = 1800
ESCRITA_PERMITIDA = ("projetos", "biblioteca/esqueletos", "rascunhos")
IMAGENS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
PASTAS_PROIBIDAS = {".git", "dados", ".espaco", "__pycache__", "node_modules"}
PASTAS_IGNORADAS = PASTAS_PROIBIDAS | {".venv", ".pytest_cache"}

COMANDOS = {"python3", "python", "uv", "cat", "ls", "cp", "mkdir", "head", "tail", "wc", "grep", "find", "jq", "sed",
            "ffmpeg", "ffprobe", "date", "echo", "sort", "uniq", "tr", "cut", "basename", "dirname", "test", "pwd",
            "which", "du", "file", "true", "printf"}
RECUSADOS = {"env", "printenv", "set", "export", "declare", "typeset", "source", ".", "eval", "exec", "cd", "bash",
             "sh", "zsh", "sudo", "curl", "wget", "rm", "mv", "chmod", "kill"}
# python3 só roda estes scripts (relativos à raiz), alguns só com um subcomando
SCRIPTS_BASH = {
    "ferramentas/medir_roteiro.py": None,
    "ferramentas/conferir_texto.py": None,
    "ferramentas/cenas.py": None,
    "ferramentas/custos.py": None,
    "ferramentas/pedido.py": "validar",
    "ferramentas/producao/narrar.py": "--listar-vozes",
}
SCRIPTS_NATIVOS = {"ferramentas/youtube_trends.py": "PesquisarYoutube", "ferramentas/fetch_youtube.py": "Transcrever"}
# os prompts antigos citam os scripts no lugar de antes
CAMINHOS_ANTIGOS = {
    "skills/produzir-video/scripts/": "ferramentas/producao/",
    "skills/pesquisar-trends/scripts/youtube_trends.py": "ferramentas/youtube_trends.py",
    "skills/youtube-transcript/scripts/fetch_youtube.py": "ferramentas/fetch_youtube.py",
    "skills/criar-personagem/scripts/personagem.py": "ferramentas/personagem.py",
    "skills/registrar-desempenho/scripts/desempenho.py": "ferramentas/desempenho.py",
}
_VARIAVEL = re.compile(r"\$")
_PROIBIDO_TEXTO = re.compile(r"\.env\b|/proc\b|/sys\b|/etc/|/root\b|segredo|\.key\b|~|\.aws\b|\.ssh\b", re.I)


# ---------------------------------------------------------------- definições (o que o modelo vê)


def _p(tipo: str, descricao: str = "", **extra) -> dict:
    d = {"type": tipo, **extra}
    if descricao:
        d["description"] = descricao
    return d


DEFINICOES: dict[str, tuple[str, dict, list[str]]] = {
    "Read": ("Lê um arquivo de texto (com o número de cada linha) ou uma imagem, que você passa a ver (só os agentes "
             "com visão). Caminhos relativos partem da raiz do repositório.",
             {"file_path": _p("string", "caminho do arquivo"),
              "offset": _p("integer", "primeira linha (1 = início)"),
              "limit": _p("integer", "quantas linhas (padrão 2000)")}, ["file_path"]),
    "Write": ("Grava um arquivo inteiro (cria as pastas que faltarem). Só na pasta do projeto, em "
              "biblioteca/esqueletos/, em rascunhos/ ou na pasta temporária.",
              {"file_path": _p("string"), "content": _p("string")}, ["file_path", "content"]),
    "Edit": ("Troca um trecho exato de um arquivo por outro. old_string precisa aparecer uma vez só, a não ser com "
             "replace_all. Não copie os números de linha do Read para old_string.",
             {"file_path": _p("string"), "old_string": _p("string"), "new_string": _p("string"),
              "replace_all": _p("boolean")}, ["file_path", "old_string", "new_string"]),
    "Glob": ("Lista os arquivos que casam com um padrão (ex.: projetos/x/08-cenas/*.json, **/*.md), do mais novo "
             "ao mais antigo.",
             {"pattern": _p("string"), "path": _p("string", "pasta base (padrão: a raiz)")}, ["pattern"]),
    "Grep": ("Procura uma expressão regular no conteúdo dos arquivos e devolve arquivo:linha: texto.",
             {"pattern": _p("string"), "path": _p("string"), "glob": _p("string", "filtro de nome, ex.: *.json"),
              "case_insensitive": _p("boolean")}, ["pattern"]),
    "Bash": ("Roda um comando na raiz do repositório e devolve a saída. Só estes comandos: "
             + ", ".join(sorted(COMANDOS - {"uv", "python"})) + ". python3 só roda "
             + ", ".join(sorted(SCRIPTS_BASH)) + " (pedido.py só com validar; narrar.py só com --listar-vozes). "
             "Sem variáveis ($), $(...), crases, heredoc, cd nem várias linhas: para criar um arquivo, use Write; para "
             "procurar em pastas, use Grep. Para medir roteiros, conferir texto e montar cenas há ferramentas próprias.",
             {"command": _p("string"),
              "timeout": _p("integer", "limite em segundos (padrão 600, máximo 1800)")}, ["command"]),
    "WebSearch": ("Busca na web agora e devolve um resumo do que achou, com as URLs das fontes.",
                  {"query": _p("string")}, ["query"]),
    "WebFetch": ("Baixa uma página da web pública e devolve o texto dela.",
                 {"url": _p("string"), "prompt": _p("string", "o que procurar na página")}, ["url"]),
    "MedirRoteiro": ("Mede um roteiro com ferramentas/medir_roteiro.py: palavras, tempo, re-hooks, loops e alertas "
                     "(modo completo, com json_saida para gravar a medição em JSON); só o texto que o espectador ouve, "
                     "para a leitura fria (modo narracao); ou, antes de escrever, quantas palavras cabem no vídeo e em "
                     "cada bloco do ângulo escolhido (modo orcamento, com a pasta do projeto em roteiro).",
                     {"roteiro": _p("string", "o arquivo do roteiro (no modo orcamento, a pasta do projeto)"),
                      "modo": _p("string", "completo (padrão), narracao ou orcamento",
                                 enum=["completo", "narracao", "orcamento"]),
                      "json_saida": _p("string", "só no modo completo: onde gravar a medição em JSON")}, ["roteiro"]),
    "ConferirTexto": ("Confere com ferramentas/conferir_texto.py se a narração do roteiro formatado é a mesma do "
                      "original, palavra por palavra. Sai 'ok' ou a lista de diferenças.",
                      {"original": _p("string"), "formatado": _p("string"),
                       "falantes": _p("array", "nomes de quem fala no original (os trocados por [FALA: Nome])",
                                      items={"type": "string"})}, ["original", "formatado"]),
    "MontarCenas": ("Valida as cenas e gera 08-cenas.json, 08-storyboard.md, 08-prompts.jsonl e 08-referencias.jsonl "
                    "com ferramentas/cenas.py montar. Devolve os erros e os avisos.",
                    {"pasta": _p("string", "a pasta do projeto (projetos/<slug>)")}, ["pasta"]),
    "PesquisarYoutube": (
        "Coleta dados reais de desempenho no YouTube com ferramentas/youtube_trends.py. comando: buscar (vídeos "
        "recentes sobre um termo, por views/dia), canal (últimos vídeos de um canal: @handle, URL ou id UC...), "
        "em-alta (populares por região e categoria; só com a chave do YouTube) ou tags (tags e hashtags dos vídeos "
        "recentes que mais crescem sobre os termos). Grave o resultado completo em saida (um .json dentro do "
        "projeto, como projetos/<slug>/02-trends/tema.json).",
        {"comando": _p("string", enum=["buscar", "canal", "em-alta", "tags"]),
         "termos": _p("array", "buscar: um termo; canal: um canal; tags: de 1 a 4 termos; em-alta: nenhum",
                      items={"type": "string"}),
         "dias": _p("integer", "buscar e tags: só vídeos dos últimos N dias (0 = sem limite; padrão 30)"),
         "formato": _p("string", "buscar e tags: filtro de duração (short até 3 min)", enum=["todos", "short", "longo"]),
         "max": _p("integer", "número de vídeos (padrão 25)"),
         "ordenar": _p("string", "ordem da saída", enum=["views", "views_por_dia", "outlier"]),
         "regiao": _p("string", "padrão BR"), "idioma": _p("string", "padrão pt"),
         "aba": _p("string", "canal: videos (padrão), shorts ou todas", enum=["videos", "shorts", "todas"]),
         "categoria": _p("string", "em-alta: id da categoria (24 Entretenimento, 27 Educação, 22 Pessoas e blogs)"),
         "rapido": _p("boolean", "yt-dlp: não busca detalhes por vídeo (mais rápido, sem data, likes e inscritos)"),
         "fonte": _p("string", "auto (padrão: a API quando há chave, senão yt-dlp), api ou yt-dlp (use yt-dlp se a "
                               "API responder quotaExceeded)", enum=["auto", "api", "yt-dlp"]),
         "saida": _p("string", "onde gravar o JSON completo")}, ["comando"]),
    "Transcrever": ("Baixa título, descrição e transcrição de um vídeo do YouTube com ferramentas/fetch_youtube.py e "
                    "grava um .md na pasta de saída. Devolve o caminho do arquivo e os avisos.",
                    {"url": _p("string", "URL do vídeo ou o id"),
                     "saida_dir": _p("string", "pasta de saída, dentro do projeto ou de "
                                               "biblioteca/esqueletos/transcricoes/"),
                     "idiomas": _p("array", "idiomas preferidos, em ordem (padrão pt, en)", items={"type": "string"}),
                     "json": _p("boolean", "também grava um .json com os dados brutos")}, ["url", "saida_dir"]),
}


def declaracoes(nomes: list[str]) -> list[dict]:
    return [{"nome": n, "descricao": DEFINICOES[n][0],
             "parametros": {"type": "object", "properties": DEFINICOES[n][1], "required": DEFINICOES[n][2]}}
            for n in nomes if n in DEFINICOES]


# ---------------------------------------------------------------- utilidades


def raiz() -> Path:
    """O espaço de trabalho (projetos/, biblioteca/...): a raiz do repositório, ou APP_RAIZ."""
    from nucleo.config import config
    return config().raiz


def encurtar(texto: str, limite: int = MAX_SAIDA_FERRAMENTA) -> str:
    if len(texto) <= limite:
        return texto
    return texto[: limite // 2] + f"\n\n... ({len(texto) - limite} caracteres cortados) ...\n\n" + texto[-limite // 2:]


def relativo(caminho: str | Path) -> str:
    try:
        p = Path(caminho)
        p = (p if p.is_absolute() else raiz() / p).resolve()
        for base in (raiz(), CODIGO):
            if p.is_relative_to(base):
                return str(p.relative_to(base))
        return str(caminho)
    except (OSError, ValueError):
        return str(caminho)


def _curto(texto: Any, limite: int) -> str:
    texto = " ".join(str(texto).split())
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


def linha_de_ferramenta(nome: str, args: dict) -> str:
    """Uma linha de log por chamada, com os prefixos que a tela colore ($ comando, ✎ grava, · lê, ⌕ busca)."""
    if nome == "Bash":
        return f"$ {_curto(args.get('command', ''), 220)}"
    if nome in ("Write", "Edit"):
        return f"✎ {relativo(args.get('file_path', ''))}"
    if nome == "Read":
        return f"· lê {relativo(args.get('file_path', ''))}"
    if nome in ("Glob", "Grep"):
        return f"· procura {_curto(args.get('pattern', ''), 120)}"
    if nome == "WebSearch":
        return f"⌕ busca: {_curto(args.get('query', ''), 160)}"
    if nome == "WebFetch":
        return f"⌕ abre: {_curto(args.get('url', ''), 160)}"
    if nome == "MedirRoteiro":
        modo = args.get("modo") or "completo"
        if modo == "orcamento":
            return f"$ medir_roteiro.py --orcamento {relativo(args.get('roteiro', ''))}"
        return (f"$ medir_roteiro.py {relativo(args.get('roteiro', ''))}" + (" --narracao" if modo == "narracao" else "")
                + (f" --json {relativo(args['json_saida'])}" if args.get("json_saida") else ""))
    if nome == "ConferirTexto":
        return f"$ conferir_texto.py {relativo(args.get('original', ''))} {relativo(args.get('formatado', ''))}"
    if nome == "MontarCenas":
        return f"$ cenas.py montar {relativo(args.get('pasta', ''))}"
    if nome == "PesquisarYoutube":
        termos = " ".join(f'"{t}"' for t in args.get("termos") or [])
        return f"⌕ youtube {args.get('comando', '?')} {_curto(termos, 160)}".rstrip()
    if nome == "Transcrever":
        return f"⌕ transcrição: {_curto(args.get('url', ''), 160)}"
    if nome == "Entregar":
        return "✎ entrega (saída estruturada)"
    return f"· {nome}"


class _Texto(HTMLParser):
    PULAR = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__()
        self.partes: list[str] = []
        self.pulando = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.PULAR:
            self.pulando += 1
        elif tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div"):
            self.partes.append("\n")

    def handle_endtag(self, tag):
        if tag in self.PULAR and self.pulando:
            self.pulando -= 1

    def handle_data(self, data):
        if not self.pulando:
            self.partes.append(data)


def html_para_texto(html: str) -> str:
    p = _Texto()
    p.feed(html)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", "".join(p.partes))).strip()


def imagem_para_modelo(caminho: Path) -> tuple[str, bytes]:
    """Imagens grandes vão reduzidas (lado maior de 1568 px, o que os modelos aproveitam), para não estourar o
    limite de tamanho das APIs."""
    dados = caminho.read_bytes()
    if len(dados) <= 3_500_000:
        return IMAGENS[caminho.suffix.lower()], dados
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(caminho), "-vf",
                        "scale='min(1568,iw)':'min(1568,ih)':force_original_aspect_ratio=decrease",
                        "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "-"], capture_output=True)
    return ("image/jpeg", r.stdout) if r.returncode == 0 and r.stdout else (IMAGENS[caminho.suffix.lower()], dados)


def conferir_url(url: str) -> None:
    """Só http(s) e só endereço público: o servidor não pode abrir a rede interna para um agente."""
    partes = urllib.parse.urlsplit(url)
    if partes.scheme not in ("http", "https") or not partes.hostname:
        raise ErroMotor("a URL precisa começar com http:// ou https://")
    try:
        enderecos = {i[4][0] for i in socket.getaddrinfo(partes.hostname, partes.port or 443, proto=socket.IPPROTO_TCP)}
    except socket.gaierror as e:
        raise ErroMotor(f"não achei o endereço {partes.hostname}: {e}") from None
    for endereco in enderecos:
        if not ipaddress.ip_address(endereco.split("%")[0]).is_global:
            raise ErroMotor(f"{partes.hostname} é um endereço interno: só páginas públicas")


class _Redirecionar(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        conferir_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def baixar_pagina(url: str, timeout: int = 60) -> tuple[bytes, str]:
    conferir_url(url)
    abridor = urllib.request.build_opener(_Redirecionar)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (pesquisa de roteiro)"})
    try:
        with abridor.open(req, timeout=timeout) as r:
            return r.read(3_000_000), r.headers.get("Content-Type", "")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ErroMotor(f"não consegui abrir: {e}") from None


def ambiente_minimo(tmp: Path) -> dict[str, str]:
    """O ambiente dos comandos e scripts dos agentes: nenhuma chave, nenhuma credencial de infraestrutura."""
    env = {"PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH') or '/usr/local/bin:/usr/bin:/bin'}",
           "HOME": str(tmp), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "America/Sao_Paulo",
           "PYTHONPATH": str(CODIGO), "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    if raiz() != CODIGO:
        env["APP_RAIZ"] = str(raiz())
    return env


def _segredos() -> list[str]:
    """As chaves cadastradas, para tirá-las de qualquer saída que volte ao modelo ou ao log."""
    try:
        from nucleo import chaves
        valores = [chaves.ler(p) for p in chaves.PROVEDORES]
    except Exception:  # noqa: BLE001 - sem banco ou sem o módulo: não há o que esconder
        return []
    return sorted({v for v in valores if v and len(v) >= 8}, key=len, reverse=True)


# ---------------------------------------------------------------- a caixa de ferramentas de um agente


class Ferramentas:
    """As ferramentas de uma execução de agente. Cada método devolve (texto, imagens)."""

    def __init__(self, ctx, rota: "Rota", agente: "Agente | None" = None):
        self.ctx, self.rota, self.agente = ctx, rota, agente
        self.raiz = raiz()
        self.tmp = Path(ctx.tmp).resolve()
        self._segredos: list[str] | None = None

    # ------------------------------------------------ caminhos

    def _proibido(self, alvo: Path) -> bool:
        for base in (self.raiz, CODIGO):
            if alvo.is_relative_to(base):
                partes = alvo.relative_to(base).parts
                if partes and partes[0] in PASTAS_PROIBIDAS or ".git" in partes:
                    return True
        nome = alvo.name.lower()
        return nome.startswith(".env") or nome.endswith(".key") or "segredo" in nome

    def _legivel(self, alvo: Path) -> bool:
        return any(alvo == b or alvo.is_relative_to(b) for b in (self.raiz, CODIGO, self.tmp))

    def _gravavel(self, alvo: Path) -> bool:
        if alvo == self.tmp or alvo.is_relative_to(self.tmp):
            return True
        if not alvo.is_relative_to(self.raiz) or alvo.name.startswith("."):
            return False
        rel = alvo.relative_to(self.raiz).as_posix()
        slug = getattr(self.ctx, "slug", None)
        if rel.startswith("projetos/") and slug:
            return rel.startswith(f"projetos/{slug}/")
        return any(rel.startswith(p + "/") for p in ESCRITA_PERMITIDA)

    def onde_grava(self) -> str:
        slug = getattr(self.ctx, "slug", None)
        lugares = [f"projetos/{slug}/" if slug else "projetos/", "biblioteca/esqueletos/", "rascunhos/"]
        return ", ".join(lugares) + f" ou a pasta temporária ({self.tmp})"

    def caminho(self, valor: str, escrita: bool = False) -> Path:
        if not isinstance(valor, str) or not valor.strip():
            raise ErroMotor("caminho vazio")
        if valor.strip().startswith("~"):
            raise ErroMotor("caminhos com ~ não valem: use caminhos relativos à raiz")
        alvo = Path(valor.strip())
        alvo = (alvo if alvo.is_absolute() else self.raiz / alvo).resolve()
        if self._proibido(alvo):
            raise ErroMotor("acesso negado a esse arquivo")
        if escrita and not self._gravavel(alvo):
            raise ErroMotor(f"escrita só em {self.onde_grava()}: o resto da biblioteca muda só pelos fluxos do sistema")
        if not self._legivel(alvo):
            raise ErroMotor("leitura só dentro do repositório ou da pasta temporária")
        return alvo

    def arg(self, alvo: Path) -> str:
        """Um caminho para a linha de comando dos scripts (que rodam na raiz): relativo quando dá."""
        return alvo.relative_to(self.raiz).as_posix() if alvo.is_relative_to(self.raiz) else str(alvo)

    def limpar(self, texto: str) -> str:
        """Tira as chaves cadastradas de uma saída."""
        if self._segredos is None:
            self._segredos = _segredos()
        for s in self._segredos:
            texto = texto.replace(s, "•••")
        return texto

    # ------------------------------------------------ despacho

    def executar(self, nome: str, args: dict, disponiveis: list[str]) -> tuple[str, list[tuple[str, bytes]]]:
        if nome not in disponiveis or nome not in DEFINICOES:
            raise ErroMotor(f"a ferramenta {nome} não está disponível para este agente "
                            f"(disponíveis: {', '.join(disponiveis) or 'nenhuma'})")
        if not isinstance(args, dict):
            raise ErroMotor("os argumentos precisam ser um objeto JSON")
        if erro := args.get("_erro"):
            raise ErroMotor(erro)
        texto, imagens = getattr(self, nome)(**args)
        return self.limpar(texto), imagens

    # ------------------------------------------------ arquivos

    def Read(self, file_path: str, offset: int = 1, limit: int = 2000, **_):
        alvo = self.caminho(file_path)
        if not alvo.exists():
            raise ErroMotor(f"{file_path} não existe")
        if alvo.is_dir():
            raise ErroMotor(f"{file_path} é uma pasta: use Glob")
        if alvo.suffix.lower() in IMAGENS:
            if not (self.agente and self.agente.visao):
                return (f"{file_path} é uma imagem, e este agente não vê imagens: use a descrição que vier com ela "
                        "(no brief, no manifesto ou nas cenas)."), []
            return f"imagem {relativo(alvo)} anexada abaixo", [imagem_para_modelo(alvo)]
        linhas = alvo.read_text(encoding="utf-8", errors="replace").splitlines()
        inicio = max(1, int(offset or 1))
        trecho = linhas[inicio - 1:inicio - 1 + max(1, int(limit or 2000))]
        corpo = "\n".join(f"{n:6}\t{linha[:2000]}" for n, linha in enumerate(trecho, inicio))
        resto = len(linhas) - (inicio - 1 + len(trecho))
        texto = (corpo or "(arquivo vazio)") + (f"\n... mais {resto} linhas (use offset)" if resto > 0 else "")
        if len(texto) > MAX_LEITURA:
            texto = texto[:MAX_LEITURA] + "\n... (cortado: leia em partes com offset e limit)"
        return texto, []

    def Write(self, file_path: str, content: str, **_):
        alvo = self.caminho(file_path, escrita=True)
        if alvo.is_dir():
            raise ErroMotor(f"{file_path} é uma pasta")
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_text(content, encoding="utf-8")
        return f"gravado: {relativo(alvo)} ({len(content)} caracteres)", []

    def Edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False, **_):
        alvo = self.caminho(file_path, escrita=True)
        if not alvo.is_file():
            raise ErroMotor(f"{file_path} não existe: para criar, use Write")
        texto = alvo.read_text(encoding="utf-8")
        n = texto.count(old_string) if old_string else 0
        if n == 0:
            raise ErroMotor("old_string não aparece no arquivo (confira espaços e quebras de linha com Read)")
        if n > 1 and not replace_all:
            raise ErroMotor(f"old_string aparece {n} vezes: inclua mais contexto ou use replace_all")
        novo = texto.replace(old_string, new_string) if replace_all else texto.replace(old_string, new_string, 1)
        alvo.write_text(novo, encoding="utf-8")
        return f"editado: {relativo(alvo)} ({n if replace_all else 1} troca(s))", []

    def Glob(self, pattern: str, path: str = ".", **_):
        base = self.caminho(path or ".")
        if Path(pattern).is_absolute():
            raise ErroMotor("o padrão é relativo à pasta base (path)")
        achados = []
        for p in base.glob(pattern):
            try:
                r = p.resolve()
            except OSError:
                continue
            if r.is_file() and self._legivel(r) and not self._proibido(r) and not (PASTAS_IGNORADAS & set(r.parts)):
                achados.append(r)
        achados.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return "\n".join(relativo(p) for p in achados[:300]) or "nenhum arquivo", []

    def Grep(self, pattern: str, path: str = ".", glob: str | None = None, case_insensitive: bool = False, **_):
        base = self.caminho(path or ".")
        try:
            rx = re.compile(pattern, re.I if case_insensitive else 0)
        except re.error as e:
            raise ErroMotor(f"expressão inválida: {e}") from None
        if base.is_file():
            arquivos = [base]
        else:
            arquivos = []
            for pasta, dirs, nomes in os.walk(base):
                dirs[:] = sorted(d for d in dirs if d not in PASTAS_IGNORADAS and not d.startswith(".git"))
                arquivos += [Path(pasta) / n for n in sorted(nomes)]
        saida: list[str] = []
        for arq in arquivos:
            if self._proibido(arq.resolve()) or (glob and not fnmatch.fnmatch(arq.name, glob)):
                continue
            try:
                for n, linha in enumerate(arq.read_text(encoding="utf-8").splitlines(), 1):
                    if rx.search(linha):
                        saida.append(f"{relativo(arq)}:{n}: {linha[:300]}")
            except (UnicodeDecodeError, OSError):
                continue
            if len(saida) >= 300:
                break
        return "\n".join(saida[:300]) or "nada encontrado", []

    # ------------------------------------------------ processos

    def _rodar(self, argv: list[str] | str, env: dict, timeout: int, shell: bool = False) -> tuple[int, str]:
        if self.ctx.cancelado.is_set():
            raise Cancelado()
        cmd = ["bash", "-c", argv] if shell else [str(a) for a in argv]
        proc = subprocess.Popen(cmd, cwd=self.raiz, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, errors="replace", start_new_session=True, env=env)
        self.ctx.processos.add(proc)
        try:
            saida, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            saida, _ = proc.communicate()
            saida += f"\n(interrompido: passou de {timeout} s)"
        finally:
            self.ctx.processos.discard(proc)
        if self.ctx.cancelado.is_set():
            raise Cancelado()
        return proc.returncode, saida

    def _script(self, script: str, args: list[str], timeout: int = TIMEOUT_PADRAO_S, env_extra: dict | None = None) -> str:
        env = {**ambiente_minimo(self.tmp), **(env_extra or {})}
        codigo, saida = self._rodar([sys.executable, str(CODIGO / script), *args], env, timeout)
        return encurtar(saida.strip() or "(sem saída)") + f"\n(código de saída {codigo})"

    def Bash(self, command: str, timeout: int = TIMEOUT_PADRAO_S, **_):
        comando = conferir_comando(command, self)
        limite = min(max(1, int(timeout or TIMEOUT_PADRAO_S)), TIMEOUT_MAX_S)
        codigo, saida = self._rodar(comando, ambiente_minimo(self.tmp), limite, shell=True)
        return encurtar(saida) + f"\n(código de saída {codigo})", []

    # ------------------------------------------------ web

    def WebSearch(self, query: str, **_):
        from nucleo.provedores import llm as provedores_llm
        rota = self.rota
        try:
            from nucleo import rotas
            rota = rotas.rota_busca(self.rota)
        except Exception:  # noqa: BLE001 - sem configuração legível, a busca usa o provedor do agente
            pass
        texto, uso = provedores_llm.buscar_na_web(rota.provedor, rota.modelo, query)
        self.ctx.somar(rota.provedor, rota.modelo, uso, self.agente.nome if self.agente else None, servico="busca")
        return texto, []

    def WebFetch(self, url: str, prompt: str = "", **_):
        bruto, tipo = baixar_pagina(url)
        texto = bruto.decode("utf-8", "replace")
        if "html" in tipo or texto.lstrip().startswith("<"):
            texto = html_para_texto(texto)
        cabecalho = f"Página {url}" + (f" (você procura: {prompt})" if prompt else "") + ":\n\n"
        return cabecalho + encurtar(texto), []

    # ------------------------------------------------ nativas

    def MedirRoteiro(self, roteiro: str, modo: str = "completo", json_saida: str | None = None, **_):
        alvo = self.caminho(roteiro)
        if modo == "orcamento":
            if not alvo.is_dir():
                raise ErroMotor("no modo orcamento, roteiro é a pasta do projeto")
            return self._script("ferramentas/medir_roteiro.py", ["--orcamento", self.arg(alvo)]), []
        if not alvo.is_file():
            raise ErroMotor(f"{roteiro} não existe")
        args = [self.arg(alvo)]
        if modo == "narracao":
            args.append("--narracao")
        elif modo != "completo":
            raise ErroMotor("modo: completo, narracao ou orcamento")
        if json_saida:
            if modo != "completo":
                raise ErroMotor("json_saida só vale no modo completo")
            args += ["--json", self.arg(self.caminho(json_saida, escrita=True))]
        return self._script("ferramentas/medir_roteiro.py", args), []

    def ConferirTexto(self, original: str, formatado: str, falantes: list[str] | str | None = None, **_):
        args = [self.arg(self.caminho(original)), self.arg(self.caminho(formatado))]
        if isinstance(falantes, str):
            falantes = [f for f in falantes.split(",")]
        nomes = [f.strip() for f in falantes or [] if str(f).strip()]
        if nomes:
            args += ["--falantes", ",".join(nomes)]
        return self._script("ferramentas/conferir_texto.py", args), []

    def MontarCenas(self, pasta: str, **_):
        alvo = self.caminho(pasta, escrita=True)
        if not alvo.is_dir():
            raise ErroMotor(f"{pasta} não é uma pasta de projeto")
        return self._script("ferramentas/cenas.py", ["montar", self.arg(alvo)]), []

    def PesquisarYoutube(self, comando: str, termos: list[str] | str | None = None, dias: int | None = None,
                         formato: str | None = None, max: int | None = None, ordenar: str | None = None,  # noqa: A002
                         regiao: str | None = None, idioma: str | None = None, aba: str | None = None,
                         categoria: str | None = None, rapido: bool = False, fonte: str | None = None,
                         saida: str | None = None, **_):
        termos = [termos] if isinstance(termos, str) else [str(t) for t in termos or [] if str(t).strip()]
        quantos = {"buscar": (1, 1), "canal": (1, 1), "em-alta": (0, 0), "tags": (1, 4)}
        if comando not in quantos:
            raise ErroMotor("comando: buscar, canal, em-alta ou tags")
        minimo, maximo = quantos[comando]
        if not minimo <= len(termos) <= maximo:
            raise ErroMotor(f"{comando} pede {minimo if minimo == maximo else f'de {minimo} a {maximo}'} termo(s)")
        args = [comando, *termos]
        if max:
            args += ["--max", str(int(max))]
        if ordenar:
            args += ["--ordenar", ordenar]
        if rapido:
            args.append("--rapido")
        if fonte:
            if fonte not in ("auto", "api", "yt-dlp"):
                raise ErroMotor("fonte: auto, api ou yt-dlp")
            args += ["--fonte", fonte]
        if comando in ("buscar", "tags"):
            if dias is not None:
                args += ["--dias", str(int(dias))]
            if formato:
                args += ["--formato", formato]
            if idioma:
                args += ["--idioma", idioma]
        if comando in ("buscar", "tags", "em-alta") and regiao:
            args += ["--regiao", regiao]
        if comando == "canal" and aba:
            args += ["--aba", aba]
        if comando == "em-alta" and categoria:
            args += ["--categoria", str(categoria)]
        if saida:
            alvo = self.caminho(saida, escrita=True)
            if alvo.suffix != ".json":
                raise ErroMotor("saida precisa ser um arquivo .json")
            args += ["--out", self.arg(alvo)]
        extra = {}
        try:
            from nucleo import chaves
            if chave := chaves.ler("youtube"):
                extra["YOUTUBE_API_KEY"] = chave  # só neste processo: o agente nunca vê a chave
        except Exception:  # noqa: BLE001 - sem a chave, o script usa o yt-dlp
            pass
        return self._script("ferramentas/youtube_trends.py", args, timeout=900, env_extra=extra), []

    def Transcrever(self, url: str, saida_dir: str, idiomas: list[str] | None = None, json: bool = False, **_):  # noqa: A002
        if not isinstance(url, str) or not url.strip() or url.strip().startswith("-"):
            raise ErroMotor("informe a URL do vídeo")
        pasta = self.caminho(saida_dir, escrita=True)
        args = [url.strip(), "--out", self.arg(pasta)]
        if idiomas:
            args += ["--lang", *[str(i) for i in idiomas]]
        if json:
            args.append("--json")
        return self._script("ferramentas/fetch_youtube.py", args, timeout=600), []


# ---------------------------------------------------------------- Bash


def reescrever(comando: str) -> str:
    """Compatibilidade com os prompts antigos: uv run vira python3, os caminhos de skills/ viram os novos e o
    `2>&1 | cat` do fim (exigência do uv do snap) sai."""
    comando = re.sub(r"(^|[;&|]\s*)uv\s+run\s+(?:--script\s+)?", r"\1python3 ", comando.strip())
    for antigo, novo in CAMINHOS_ANTIGOS.items():
        comando = comando.replace(antigo, novo)
    return re.sub(r"\s*2>&1\s*\|\s*cat\s*$", "", comando)


def conferir_comando(comando: str, ferramentas: Ferramentas) -> str:
    """Confere o comando e devolve a versão que vai rodar. Cada pedaço precisa começar por um comando da lista,
    e o que ele grava (redirecionamento, cp, mkdir, ffmpeg, --json) precisa estar onde a escrita é permitida."""
    if not isinstance(comando, str) or not comando.strip():
        raise ErroMotor("comando vazio")
    comando = reescrever(comando)
    if "\n" in comando or "\r" in comando:
        raise ErroMotor("um comando por linha: junte com ; ou && (para criar um arquivo, use Write)")
    if re.search(r"`|<<|<\(|>\(", comando):
        raise ErroMotor("comando recusado: sem crases, heredoc nem substituição de processo (use Write para criar arquivos)")
    if _VARIAVEL.search(comando):
        raise ErroMotor("comando recusado: sem variáveis nem $(...); escreva os valores por extenso")
    if _PROIBIDO_TEXTO.search(comando):
        raise ErroMotor("acesso negado: esse caminho não é do projeto")
    try:
        lexer = shlex.shlex(re.sub(r"\d?>&\d", " ", comando), posix=True, punctuation_chars="|&;")  # 2>&1 não separa
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError as e:
        raise ErroMotor(f"comando mal formado: {e}") from None
    segmento: list[str] = []
    for tok in tokens + [";"]:
        if tok and set(tok) <= set("|&;"):
            if segmento:
                _checar_segmento(segmento, ferramentas)
            segmento = []
        else:
            segmento.append(tok)
    return comando


def _caminhos_do_token(tok: str, ferramentas: Ferramentas) -> list[Path]:
    """Os caminhos a que um argumento pode se referir (com os globs expandidos como o bash faria)."""
    valor = tok.split("=", 1)[1] if tok.startswith("-") and "=" in tok else tok
    if not valor or (valor.startswith("-") and "=" not in tok):
        return []
    if valor in ("/dev/null", "-"):
        return []
    if "://" in valor:
        return []
    base = ferramentas.raiz
    if any(c in valor for c in "*?["):
        achados = glob_mod.glob(valor, root_dir=None if valor.startswith("/") else str(base), recursive=True)
        return [(Path(a) if a.startswith("/") else base / a).resolve() for a in achados]
    if valor.startswith("/") or "/" in valor or ".." in valor or (base / valor).exists():
        return [(Path(valor) if valor.startswith("/") else base / valor).resolve()]
    return []


def _checar_leitura(tok: str, ferramentas: Ferramentas) -> None:
    for p in _caminhos_do_token(tok, ferramentas):
        if ferramentas._proibido(p):
            raise ErroMotor("acesso negado a esse arquivo")
        if not ferramentas._legivel(p):
            raise ErroMotor(f"{tok}: só caminhos dentro do repositório ou da pasta temporária")


def _checar_escrita(tok: str, ferramentas: Ferramentas) -> None:
    if tok in ("/dev/null", "-"):
        return
    ferramentas.caminho(tok, escrita=True)


def _checar_segmento(seg: list[str], ferramentas: Ferramentas) -> None:
    if seg and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", seg[0]):  # VAR=valor comando
        raise ErroMotor("comando recusado: sem definir variáveis")
    resto, i = [], 0
    while i < len(seg):  # redirecionamento: o destino precisa estar onde a escrita é permitida
        tok = seg[i]
        if m := re.fullmatch(r"\d?>>?(.*)", tok):
            alvo = m.group(1) or (seg[i + 1] if i + 1 < len(seg) else "")
            if not alvo:
                raise ErroMotor("redirecionamento sem destino")
            _checar_escrita(alvo, ferramentas)
            i += 1 if m.group(1) else 2
        elif m := re.fullmatch(r"<(.*)", tok):
            alvo = m.group(1) or (seg[i + 1] if i + 1 < len(seg) else "")
            _checar_leitura(alvo, ferramentas)
            i += 1 if m.group(1) else 2
        else:
            resto.append(tok)
            i += 1
    if not resto:
        return
    nome, args = resto[0], resto[1:]
    if nome in RECUSADOS or Path(nome).name in RECUSADOS:
        raise ErroMotor(f"comando recusado: {nome}")
    if "/" in nome or nome not in COMANDOS:
        raise ErroMotor(f"comando não permitido: {nome} (permitidos: {', '.join(sorted(COMANDOS - {'uv', 'python'}))})")
    if nome in ("python3", "python"):
        _checar_python(args, ferramentas)
    elif nome == "uv":
        raise ErroMotor("uv só como 'uv run --script <script>' (que roda como python3)")
    elif nome == "sed" and ("-n" not in args or any(t.startswith("-i") or t.startswith("--in-place") for t in args)):
        raise ErroMotor("sed só com -n e sem -i (para editar, use Edit)")
    elif nome == "find" and {"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf",
                             "-fls"} & set(args):
        raise ErroMotor("find sem -exec, -delete nem -fprint")
    elif nome == "grep" and any(re.match(r"-[A-Za-z]*[rR]", t) and not t.startswith("--") or
                                t in ("--recursive", "--dereference-recursive") for t in args):
        raise ErroMotor("grep sem -r: para procurar em pastas, use a ferramenta Grep")
    elif nome == "sort" and any(t.startswith("-o") or t.startswith("--output") for t in args):
        raise ErroMotor("sort sem -o: para gravar, use Write")
    elif nome in ("ffmpeg", "ffprobe") and any("://" in t or t.startswith(("concat:", "subfile:", "file:")) for t in args):
        raise ErroMotor(f"{nome} só com arquivos locais do projeto")
    posicionais = [t for t in args if not t.startswith("-")]
    if nome == "cp":
        if len(posicionais) < 2:
            raise ErroMotor("cp precisa de origem e destino")
        _checar_escrita(posicionais[-1], ferramentas)
        posicionais = posicionais[:-1]
    elif nome == "mkdir":
        for t in posicionais:
            _checar_escrita(t, ferramentas)
        posicionais = []
    elif nome == "ffmpeg" and posicionais:
        _checar_escrita(posicionais[-1], ferramentas)
        posicionais = posicionais[:-1]
    elif nome == "uniq" and len(posicionais) >= 2:
        _checar_escrita(posicionais[1], ferramentas)
        posicionais = posicionais[:1]
    for t in args:
        if t in posicionais or t.startswith("-"):
            _checar_leitura(t, ferramentas)


def _checar_python(args: list[str], ferramentas: Ferramentas) -> None:
    if not args or args[0].startswith("-"):
        raise ErroMotor("python3 só roda um script de ferramentas/ (sem -c, -m nem modo interativo)")
    alvo = (ferramentas.raiz / args[0]).resolve() if not args[0].startswith("/") else Path(args[0]).resolve()
    rel = None
    for base in (ferramentas.raiz, CODIGO):
        if alvo.is_relative_to(base):
            rel = alvo.relative_to(base).as_posix()
            break
    if rel in SCRIPTS_NATIVOS:
        raise ErroMotor(f"{rel} roda pela ferramenta {SCRIPTS_NATIVOS[rel]}, não pelo Bash")
    if rel not in SCRIPTS_BASH:
        raise ErroMotor(f"python3 só roda estes scripts: {', '.join(sorted(SCRIPTS_BASH))}")
    exigido = SCRIPTS_BASH[rel]
    if exigido and exigido not in args[1:]:
        raise ErroMotor(f"{rel} só com {exigido}")
    if rel == "ferramentas/medir_roteiro.py" and "--json" in args:
        i = args.index("--json")
        if i + 1 < len(args):
            _checar_escrita(args[i + 1], ferramentas)


__all__ = ["CODIGO", "COMANDOS", "DEFINICOES", "ESCRITA_PERMITIDA", "Ferramentas", "SCRIPTS_BASH", "ambiente_minimo",
           "baixar_pagina", "conferir_comando", "conferir_url", "declaracoes", "encurtar", "html_para_texto",
           "imagem_para_modelo", "linha_de_ferramenta", "raiz", "reescrever", "relativo"]
