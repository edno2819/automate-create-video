"""Motor de agentes: roda os agentes de .claude/agents/ com qualquer LLM (Claude, GPT ou Gemini) pela API.

Com o Claude Code, cada agente roda com as ferramentas dele. Para usar outro modelo numa etapa, o motor faz o
mesmo papel: lê a definição do agente (o frontmatter diz as ferramentas; o corpo é a instrução), monta o prompt
de sistema com o CLAUDE.md, oferece ao modelo as mesmas ferramentas (Read, Write, Edit, Glob, Grep, Bash,
WebSearch, WebFetch), executa aqui cada chamada e devolve o resultado, até o modelo terminar sem pedir mais
nada. As conversas com cada provedor ficam em ferramentas/llm.py.

Segurança, como no modo headless do Claude Code (claude_code.FERRAMENTAS): o Bash só roda os comandos da
lista COMANDOS (sem substituição de comando nem heredoc), a escrita só vale em projetos/, biblioteca/,
pedidos/ e na pasta temporária da execução, e nenhum agente lê o .env.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "ferramentas"))
import chaves  # noqa: E402
import custos  # noqa: E402
import llm  # noqa: E402

AGENTES = RAIZ / ".claude" / "agents"
MAX_TURNOS = 80
MAX_SAIDA_FERRAMENTA = 40_000
ESCRITA_PERMITIDA = ("projetos", "biblioteca", "pedidos")
COMANDOS = {"uv", "python3", "cat", "ls", "cp", "mkdir", "head", "tail", "wc", "grep", "find", "jq", "sed", "ffmpeg",
            "ffprobe", "espeak-ng", "date", "echo", "sort", "uniq", "tr", "cut", "basename", "dirname", "test", "pwd",
            "which", "du", "file", "cd", "true", "printf"}
IMAGENS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
REGRAS = """Você está rodando sem ninguém no terminal, chamado pela interface web do projeto. Não pergunte nada nem espere
resposta: as decisões do usuário já foram tomadas e estão nos arquivos do projeto. Faça só a tarefa pedida, salve as
saídas nos arquivos de sempre e termine com um resumo curto, em português, do que foi feito e do que o usuário
precisa olhar. Para trabalhar, chame as ferramentas: um texto sem chamada de ferramenta encerra a tarefa."""


class Cancelado(Exception):
    pass


class ErroMotor(Exception):
    pass


@dataclass
class Agente:
    nome: str
    ferramentas: list[str]
    instrucoes: str
    descricao: str = ""


@dataclass
class Rota:
    provedor: str  # anthropic | openai | gemini
    modelo: str


@dataclass
class Contexto:
    """O que uma execução da interface compartilha entre os agentes: o log, o pedido de cancelamento, a pasta
    temporária e o uso somado (para o custo)."""
    log: Callable[[str], None]
    cancelado: threading.Event = field(default_factory=threading.Event)
    tmp: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="motor-")))
    uso_usd: float = 0.0
    sem_preco: set = field(default_factory=set)
    processos: set = field(default_factory=set)
    trava: threading.Lock = field(default_factory=threading.Lock)

    def somar(self, modelo: str, uso: dict) -> None:
        valor = custos.custo_llm(modelo, uso)
        with self.trava:
            if valor is None:
                self.sem_preco.add(modelo)
            else:
                self.uso_usd += valor

    def cancelar(self) -> None:
        self.cancelado.set()
        for proc in list(self.processos):
            try:
                os.killpg(proc.pid, 15)
            except (ProcessLookupError, PermissionError):
                pass


def carregar_agente(nome: str) -> Agente:
    caminho = AGENTES / f"{nome}.md"
    if not caminho.exists():
        raise ErroMotor(f"agente desconhecido: {nome}")
    texto = caminho.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n(.*)", texto, re.S)
    if not m:
        raise ErroMotor(f"{caminho} sem frontmatter")
    meta = dict(re.findall(r"(?m)^(\w+):\s*(.*)$", m.group(1)))
    ferramentas = [f.strip() for f in meta.get("tools", "").split(",") if f.strip()]
    return Agente(nome, ferramentas, m.group(2).strip(), meta.get("description", ""))


# ---------------------------------------------------------------- ferramentas


DEFINICOES = {
    "Read": ("Lê um arquivo de texto (com o número de cada linha) ou uma imagem, que você passa a ver. Caminhos "
             "relativos partem da raiz do repositório.",
             {"file_path": {"type": "string", "description": "caminho do arquivo"},
              "offset": {"type": "integer", "description": "primeira linha (1 = início)"},
              "limit": {"type": "integer", "description": "quantas linhas (padrão 2000)"}}, ["file_path"]),
    "Write": ("Grava um arquivo inteiro (cria as pastas que faltarem). Só em projetos/, biblioteca/, pedidos/ ou na "
              "pasta temporária.", {"file_path": {"type": "string"}, "content": {"type": "string"}},
              ["file_path", "content"]),
    "Edit": ("Troca um trecho exato de um arquivo por outro. old_string precisa aparecer uma vez só, a não ser com "
             "replace_all.", {"file_path": {"type": "string"}, "old_string": {"type": "string"},
                              "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}},
             ["file_path", "old_string", "new_string"]),
    "Glob": ("Lista os arquivos que casam com um padrão (ex.: projetos/x/08-cenas/*.json, **/*.md).",
             {"pattern": {"type": "string"}, "path": {"type": "string", "description": "pasta base (padrão: a raiz)"}},
             ["pattern"]),
    "Grep": ("Procura uma expressão regular no conteúdo dos arquivos e devolve arquivo:linha: texto.",
             {"pattern": {"type": "string"}, "path": {"type": "string"},
              "glob": {"type": "string", "description": "filtro de nome, ex.: *.json"},
              "case_insensitive": {"type": "boolean"}}, ["pattern"]),
    "Bash": ("Roda um comando na raiz do repositório e devolve a saída. Só estes comandos: "
             + ", ".join(sorted(COMANDOS)) + " (uv só como 'uv run'; sed só com -n). Sem $(...), crases nem heredoc: "
             "para criar um arquivo, use Write. Com uv, termine com 2>&1 | cat.",
             {"command": {"type": "string"},
              "timeout": {"type": "integer", "description": "limite em segundos (padrão 600, máximo 1800)"}},
             ["command"]),
    "WebSearch": ("Busca na web agora e devolve um resumo do que achou, com as URLs das fontes.",
                  {"query": {"type": "string"}}, ["query"]),
    "WebFetch": ("Baixa uma página da web e devolve o texto dela.",
                 {"url": {"type": "string"}, "prompt": {"type": "string", "description": "o que procurar na página"}},
                 ["url"]),
}


def declaracoes(nomes: list[str]) -> list[dict]:
    return [{"nome": n, "descricao": DEFINICOES[n][0],
             "parametros": {"type": "object", "properties": DEFINICOES[n][1], "required": DEFINICOES[n][2]}}
            for n in nomes if n in DEFINICOES]


class Ferramentas:
    def __init__(self, ctx: Contexto, rota: Rota):
        self.ctx, self.rota = ctx, rota

    # caminhos

    def caminho(self, valor: str, escrita: bool = False) -> Path:
        alvo = Path(valor).expanduser()
        alvo = (alvo if alvo.is_absolute() else RAIZ / alvo).resolve()
        dentro = lambda base: alvo == base or alvo.is_relative_to(base)  # noqa: E731
        tmp = Path(tempfile.gettempdir()).resolve()
        if alvo.name.startswith(".env") or ".git" in alvo.parts:
            raise ErroMotor("acesso negado a esse arquivo")
        if escrita:
            if not (dentro(self.ctx.tmp.resolve()) or dentro(tmp) or any(dentro(RAIZ / p) for p in ESCRITA_PERMITIDA)):
                raise ErroMotor(f"escrita só em {', '.join(ESCRITA_PERMITIDA)}/ ou na pasta temporária ({self.ctx.tmp})")
        elif not (dentro(RAIZ) or dentro(tmp)):
            raise ErroMotor("leitura só dentro do repositório ou da pasta temporária")
        return alvo

    # cada ferramenta devolve (texto, imagens)

    def Read(self, file_path: str, offset: int = 1, limit: int = 2000, **_):
        alvo = self.caminho(file_path)
        if not alvo.exists():
            raise ErroMotor(f"{file_path} não existe")
        if alvo.is_dir():
            raise ErroMotor(f"{file_path} é uma pasta: use Glob ou Bash ls")
        if alvo.suffix.lower() in IMAGENS:
            return f"imagem {file_path} anexada abaixo", [imagem_para_modelo(alvo)]
        linhas = alvo.read_text(encoding="utf-8", errors="replace").splitlines()
        inicio = max(1, int(offset or 1))
        trecho = linhas[inicio - 1:inicio - 1 + int(limit or 2000)]
        corpo = "\n".join(f"{n:6}\t{l[:2000]}" for n, l in enumerate(trecho, inicio))
        resto = len(linhas) - (inicio - 1 + len(trecho))
        return (corpo or "(arquivo vazio)") + (f"\n... mais {resto} linhas (use offset)" if resto > 0 else ""), []

    def Write(self, file_path: str, content: str, **_):
        alvo = self.caminho(file_path, escrita=True)
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_text(content, encoding="utf-8")
        return f"gravado: {file_path} ({len(content)} caracteres)", []

    def Edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False, **_):
        alvo = self.caminho(file_path, escrita=True)
        texto = alvo.read_text(encoding="utf-8")
        n = texto.count(old_string)
        if n == 0:
            raise ErroMotor("old_string não aparece no arquivo (confira espaços e quebras de linha com Read)")
        if n > 1 and not replace_all:
            raise ErroMotor(f"old_string aparece {n} vezes: inclua mais contexto ou use replace_all")
        alvo.write_text(texto.replace(old_string, new_string) if replace_all else texto.replace(old_string, new_string, 1),
                        encoding="utf-8")
        return f"editado: {file_path} ({n if replace_all else 1} troca(s))", []

    def Glob(self, pattern: str, path: str = ".", **_):
        base = self.caminho(path or ".")
        achados = sorted((p for p in base.glob(pattern) if p.is_file() and ".git" not in p.parts),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        nomes = [str(p.relative_to(RAIZ)) if p.is_relative_to(RAIZ) else str(p) for p in achados[:300]]
        return "\n".join(nomes) or "nenhum arquivo", []

    def Grep(self, pattern: str, path: str = ".", glob: str | None = None, case_insensitive: bool = False, **_):
        base = self.caminho(path or ".")
        try:
            rx = re.compile(pattern, re.I if case_insensitive else 0)
        except re.error as e:
            raise ErroMotor(f"expressão inválida: {e}")
        arquivos = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
        saida = []
        for arq in arquivos:
            if {".git", ".venv", "__pycache__", "node_modules"} & set(arq.parts) or arq.name.startswith(".env"):
                continue
            if glob and not fnmatch.fnmatch(arq.name, glob):
                continue
            try:
                for n, linha in enumerate(arq.read_text(encoding="utf-8").splitlines(), 1):
                    if rx.search(linha):
                        saida.append(f"{arq.relative_to(RAIZ) if arq.is_relative_to(RAIZ) else arq}:{n}: {linha[:300]}")
            except (UnicodeDecodeError, OSError):
                continue
            if len(saida) >= 300:
                break
        return "\n".join(saida[:300]) or "nada encontrado", []

    def Bash(self, command: str, timeout: int = 600, **_):
        conferir_comando(command, self)
        proc = subprocess.Popen(["bash", "-c", command], cwd=RAIZ, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, start_new_session=True,
                                env={**os.environ, **chaves.ambiente(excluir=("anthropic",))})
        self.ctx.processos.add(proc)
        try:
            saida, _ = proc.communicate(timeout=min(int(timeout or 600), 1800))
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 15)
            saida, _ = proc.communicate()
            saida += f"\n(interrompido: passou de {timeout} s)"
        finally:
            self.ctx.processos.discard(proc)
        if self.ctx.cancelado.is_set():
            raise Cancelado()
        return encurtar(saida) + f"\n(código de saída {proc.returncode})", []

    def WebSearch(self, query: str, **_):
        texto, uso = llm.buscar_na_web(self.rota.provedor, self.rota.modelo, query)
        self.ctx.somar(self.rota.modelo, uso)
        return texto, []

    def WebFetch(self, url: str, prompt: str = "", **_):
        if not re.match(r"https?://", url):
            raise ErroMotor("a URL precisa começar com http:// ou https://")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (pesquisa de roteiro)"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                bruto = r.read(3_000_000)
                tipo = r.headers.get("Content-Type", "")
        except (urllib.error.URLError, TimeoutError) as e:
            raise ErroMotor(f"não consegui abrir: {e}")
        texto = bruto.decode("utf-8", "replace")
        if "html" in tipo or texto.lstrip().startswith("<"):
            texto = html_para_texto(texto)
        cabecalho = f"Página {url}" + (f" (você procura: {prompt})" if prompt else "") + ":\n\n"
        return cabecalho + encurtar(texto), []


def encurtar(texto: str, limite: int = MAX_SAIDA_FERRAMENTA) -> str:
    if len(texto) <= limite:
        return texto
    return texto[: limite // 2] + f"\n\n... ({len(texto) - limite} caracteres cortados) ...\n\n" + texto[-limite // 2:]


def conferir_comando(comando: str, ferramentas: Ferramentas) -> None:
    """A mesma ideia do --allowedTools do Claude Code: cada pedaço do comando precisa começar por um comando
    da lista, e o que ele grava por redirecionamento precisa estar num lugar em que a escrita é permitida."""
    if re.search(r"\$\(|`|<<|<\(|>\(", comando):
        raise ErroMotor("comando recusado: sem $(...), crases, heredoc nem substituição de processo (use Write para criar arquivos)")
    if re.search(r"(^|[\s/'\"=])\.env\b", comando):
        raise ErroMotor("acesso negado ao .env")
    try:
        lexer = shlex.shlex(re.sub(r"\d?>&\d", " ", comando), posix=True, punctuation_chars="|&;")  # 2>&1 não separa
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError as e:
        raise ErroMotor(f"comando mal formado: {e}")
    segmento: list[str] = []
    for tok in tokens + [";"]:
        if tok and set(tok) <= set("|&;"):
            if segmento:
                checar_segmento(segmento, ferramentas)
            segmento = []
        else:
            segmento.append(tok)


def checar_segmento(seg: list[str], ferramentas: Ferramentas) -> None:
    while seg and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", seg[0]):  # VAR=valor comando
        seg = seg[1:]
    resto, i = [], 0
    while i < len(seg):  # redirecionamento: o arquivo de destino precisa estar onde a escrita é permitida
        tok = seg[i]
        if m := re.fullmatch(r"\d?>>?(.*)", tok):
            alvo = m.group(1) or (seg[i + 1] if i + 1 < len(seg) else "")
            if alvo != "/dev/null":
                ferramentas.caminho(alvo, escrita=True)
            i += 1 if m.group(1) else 2
        elif tok.startswith("<"):
            i += 1 if len(tok) > 1 else 2
        else:
            resto.append(tok)
            i += 1
    seg = resto
    if not seg:
        return
    nome = Path(seg[0]).name
    if nome not in COMANDOS:
        raise ErroMotor(f"comando não permitido: {nome} (permitidos: {', '.join(sorted(COMANDOS))})")
    if nome == "uv" and seg[1:2] != ["run"]:
        raise ErroMotor("uv só como 'uv run'")
    if nome == "sed" and ("-n" not in seg or any(t.startswith("-i") for t in seg)):
        raise ErroMotor("sed só com -n e sem -i (para editar, use Edit)")
    if nome == "find" and {"-exec", "-execdir", "-delete", "-ok"} & set(seg):
        raise ErroMotor("find sem -exec nem -delete")


class _Texto(HTMLParser):
    PULAR = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__()
        self.partes, self.pulando = [], 0

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


# ---------------------------------------------------------------- execução de um agente


def linha_de_chamada(nome: str, args: dict) -> str:
    import claude_code  # o mesmo formato de log das execuções do Claude Code
    return claude_code.linha_de_ferramenta(nome, args) or f"· {nome}"


def sistema_de(agente: Agente, rota: Rota, ctx: Contexto) -> str:
    claude_md = (RAIZ / "CLAUDE.md").read_text(encoding="utf-8") if (RAIZ / "CLAUDE.md").exists() else ""
    return f"""{agente.instrucoes}

---

## Ambiente

Você é o agente `{agente.nome}`, rodando no motor de agentes da interface web com o modelo {rota.modelo}. O
diretório de trabalho é a raiz do repositório ({RAIZ}): caminhos relativos partem dela. Hoje é
{date.today().isoformat()}. Pasta temporária para arquivos de trabalho: {ctx.tmp}. Ferramentas disponíveis:
{", ".join(agente.ferramentas)}.

{REGRAS}

## Instruções do projeto (CLAUDE.md)

{claude_md}"""


def rodar_agente(agente: Agente | str, mensagem: str, rota: Rota, ctx: Contexto, recuo: str = "    ") -> str:
    """Roda o agente até ele terminar e devolve o texto final. O uso vai somado em ctx."""
    if isinstance(agente, str):
        agente = carregar_agente(agente)
    disponiveis = [f for f in agente.ferramentas if f in DEFINICOES]
    conversa = llm.nova_conversa(rota.provedor, rota.modelo, sistema_de(agente, rota, ctx), declaracoes(disponiveis))
    conversa.usuario(mensagem)
    ferramentas = Ferramentas(ctx, rota)
    final = ""
    for _ in range(MAX_TURNOS):
        if ctx.cancelado.is_set():
            raise Cancelado()
        passo = conversa.passo()
        ctx.somar(rota.modelo, passo.uso)
        if passo.texto:
            final = passo.texto
            ctx.log(recuo + "› " + " ".join(passo.texto.split())[:400])
        if not passo.chamadas:
            if passo.cortado:
                conversa.usuario("Sua resposta foi cortada no limite de tamanho. Continue de onde parou; para um arquivo "
                                 "longo, grave em partes (Write e depois Edit).")
                continue
            return final
        resultados = []
        for ch in passo.chamadas:
            ctx.log(recuo + linha_de_chamada(ch.nome, ch.args))
            try:
                if ch.nome not in disponiveis:
                    raise ErroMotor(f"ferramenta {ch.nome} não está disponível para este agente")
                texto, imagens = getattr(ferramentas, ch.nome)(**ch.args)
            except Cancelado:
                raise
            except (ErroMotor, OSError, TypeError, ValueError, llm.ErroLLM) as e:
                texto, imagens = f"erro: {e}", []
                ctx.log(recuo + f"  ✗ {str(e)[:200]}")
            resultados.append(llm.Resultado(ch.id, ch.nome, texto, imagens))
        conversa.resultados(resultados)
    raise ErroMotor(f"o agente {agente.nome} passou de {MAX_TURNOS} turnos sem terminar")


def agente_avulso(nome: str, instrucoes: str, ferramentas: list[str]) -> Agente:
    """Um papel sem arquivo em .claude/agents/ (a sessão principal, por exemplo)."""
    return Agente(nome, ferramentas, instrucoes)


def json_do_texto(texto: str):
    m = re.search(r"\{.*\}", texto, re.S)
    if not m:
        raise ErroMotor("a resposta não trouxe o JSON pedido")
    return json.loads(m.group(0))
