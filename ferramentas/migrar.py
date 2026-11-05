#!/usr/bin/env python3
"""Importa uma instalação local antiga (de antes do servidor) para o banco e o armazenamento novos.

Antes do servidor, o estado vivia em arquivos da máquina; agora vive no banco (Postgres) e no armazenamento de
objetos (MinIO ou S3). Este script leva cada coisa para o seu lugar:

| Antes (na pasta de origem)                         | Agora                                                   |
|----------------------------------------------------|---------------------------------------------------------|
| `.env` (chaves dos provedores)                     | tabela `chaves_api`, cifradas com APP_SEGREDO           |
| `dados/conexoes.json` (quem roda cada etapa)       | `configuracoes["modelos"]` (modos `claude-code*` → Claude pela API) |
| `dados/web.sqlite3`: projetos                      | tabela `projetos` (com as datas originais)              |
| `dados/web.sqlite3`: execuções e o log de cada uma | tabelas `execucoes` e `execucao_logs`                   |
| pastas de `projetos/` sem linha no banco antigo    | tabela `projetos` (formulário tirado do pedido)         |
| `projetos/`, `pedidos/`, `rascunhos/` e as partes sincronizadas de `biblioteca/` | armazenamento + índice `arquivos` (`nucleo.espaco.importar_tudo`) |
| `pedidos/contextos/<id>/contexto.json`             | tabela `contextos` (os anexos vão com os arquivos)      |
| `biblioteca/licoes-roteiro.md`                     | tabela `licoes_roteiro`, uma lição por item `**regra**`  |
| `dados/desempenho.jsonl`                           | tabela `desempenho`                                     |
| `projetos/<slug>/custos.jsonl`                     | tabela `custos` (com o preço de `biblioteca/precos.json`) |

Uso (da raiz, com DATABASE_URL e o armazenamento do servidor no ambiente ou no .env):

    python3 ferramentas/migrar.py --simular                   # só mostra o que faria
    python3 ferramentas/migrar.py                             # importa a instalação desta pasta
    python3 ferramentas/migrar.py --origem /caminho/antigo    # a instalação antiga está em outra pasta
    python3 ferramentas/migrar.py --so chaves,configuracoes   # só algumas partes (ou --pular arquivos)
    python3 ferramentas/migrar.py --sobrescrever              # o que já existe no destino dá lugar ao antigo

No Docker, com a instalação antiga montada só para leitura:

    docker compose run --rm -v /caminho/antigo:/antigo:ro trabalhador migrar --origem /antigo

Pode rodar de novo: o que já foi importado não se repete. Projetos, contextos, chaves, etapas e métricas que já
existem no destino ficam como estão (a não ser com --sobrescrever); execuções e custos ficam registrados em
`configuracoes["migracao_local"]` e nunca entram duas vezes (nem com --sobrescrever: o custo de um projeto já
migrado, inclusive o que o sistema novo acrescentou depois ao custos.jsonl, já está no banco); lições iguais são
reconhecidas pelo texto. Os arquivos da origem só são copiados para o espaço de trabalho quando faltam nele (ou,
com --sobrescrever, quando diferem).

Nunca imprime uma chave: só o nome do provedor.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parents[1]
for _caminho in (RAIZ, RAIZ / "ferramentas"):
    if str(_caminho) not in sys.path:
        sys.path.insert(0, str(_caminho))

from nucleo.config import config  # noqa: E402

PARTES = ("chaves", "configuracoes", "projetos", "execucoes", "contextos", "licoes", "desempenho", "custos",
          "arquivos")
ESTADO = "migracao_local"  # configuracoes: execuções e custos já importados
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MODOS_CLAUDE_CODE = ("claude-code", "claude-code-api")
PROVEDORES_LLM = ("anthropic", "openai", "gemini")
STATUS_FINAIS = ("ok", "erro", "cancelada", "interrompida")
ORIGEM_EXECUCOES = "dados/web.sqlite3"

# Os mesmos de nucleo/espaco.py (docs/arquitetura.md); usados só se o módulo ainda não os tiver.
SINCRONIZADOS_PADRAO = ("projetos/", "pedidos/", "rascunhos/", "biblioteca/esqueletos/", "biblioteca/personagens/",
                        "biblioteca/musicas/", "biblioteca/efeitos/", "biblioteca/vozes/", "biblioteca/tecnicas.json",
                        "biblioteca/canal.json", "biblioteca/licoes-roteiro.md", "biblioteca/aprendizados.md")
IGNORADOS_PADRAO = ("**/montagem-*/**", "**/__pycache__/**", "**/*.tmp", "**/.capa.jpg", "**/.DS_Store")


# ------------------------------------------------------------------------------------------------- auxiliares

def hora(texto: Any) -> datetime | None:
    """Data do registro antigo em UTC. As antigas não têm fuso: eram a hora local da máquina."""
    if not texto:
        return None
    try:
        d = datetime.fromisoformat(str(texto))
    except ValueError:
        return None
    return d.astimezone(timezone.utc)


def agora() -> datetime:
    return datetime.now(timezone.utc)


def json_ou(texto: Any, padrao: Any) -> Any:
    if texto in (None, ""):
        return padrao
    try:
        return json.loads(texto)
    except (TypeError, ValueError):
        return padrao


def ler_env(arquivo: Path) -> dict[str, str]:
    """As variáveis de um .env, sem passar por os.environ (o da origem não é o deste processo)."""
    if not arquivo.is_file():
        return {}
    linha_re = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
    valores = {}
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        if linha.lstrip().startswith("#"):
            continue
        if m := linha_re.match(linha):
            valores[m.group(1)] = m.group(2).strip("\"'")
    return valores


def sem_senha(url: str) -> str:
    if "@" not in url:
        return url
    esquema, _, resto = url.partition("://")
    return f"{esquema}://…@{resto.rsplit('@', 1)[1]}"


def ler_sqlite(arquivo: Path, consulta: str) -> list[dict]:
    """Linhas do banco antigo. Lê uma cópia (com o -wal e o -shm): a origem pode estar montada só para leitura,
    e um SQLite em modo WAL precisa escrever ao lado para ser lido sem perder o que ainda está no -wal."""
    if not arquivo.is_file():
        return []
    with tempfile.TemporaryDirectory(prefix="migrar-") as tmp:
        copia = Path(tmp) / arquivo.name
        for sufixo in ("", "-wal", "-shm"):
            parte = arquivo.with_name(arquivo.name + sufixo)
            if parte.is_file():
                shutil.copy2(parte, copia.with_name(copia.name + sufixo))
        con = sqlite3.connect(copia)
        con.row_factory = sqlite3.Row
        try:
            return [dict(l) for l in con.execute(consulta)]
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return []
            raise
        finally:
            con.close()


def ler_jsonl(arquivo: Path) -> tuple[list[dict], int]:
    """As linhas válidas e quantas estavam quebradas."""
    validas, quebradas = [], 0
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        if not linha.strip():
            continue
        try:
            item = json.loads(linha)
        except ValueError:
            quebradas += 1
            continue
        if isinstance(item, dict):
            validas.append(item)
        else:
            quebradas += 1
    return validas, quebradas


def provedor_do_modelo(modelo: str) -> str:
    """Quem cobra por um modelo, pelo prefixo do nome (os registros antigos não guardavam o provedor)."""
    m = (modelo or "").lower()
    if m.startswith("gemini") or m.startswith("imagen"):
        return "gemini"
    if m.startswith(("gpt", "tts-1", "dall-e", "o1", "o3", "o4")):
        return "openai"
    if m.startswith("eleven"):
        return "elevenlabs"
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("espeak"):
        return "espeak"
    return ""


def ignorado_padrao(caminho: str) -> bool:
    """Reserva de nucleo.espaco.ignorado, para a simulação rodar mesmo sem o módulo."""
    return any(fnmatch.fnmatch(caminho, p) or fnmatch.fnmatch(caminho, p.removeprefix("**/"))
               for p in IGNORADOS_PADRAO)


def arquivos_sincronizados(base: Path, sincronizados: tuple[str, ...],
                           e_ignorado: Callable[[str], bool]) -> Iterator[str]:
    """Os caminhos relativos (com /) dos arquivos de `base` que o espaço de trabalho sincroniza."""
    for item in sincronizados:
        alvo = base / item.rstrip("/")
        if item.endswith("/"):
            if not alvo.is_dir():
                continue
            for pasta, subpastas, nomes in os.walk(alvo):  # não segue links simbólicos
                subpastas.sort()
                for nome in sorted(nomes):
                    rel = (Path(pasta) / nome).relative_to(base).as_posix()
                    if not e_ignorado(rel) and not (Path(pasta) / nome).is_symlink():
                        yield rel
        elif alvo.is_file() and not alvo.is_symlink() and not e_ignorado(item):
            yield item


# Lições: "## Seção" e itens "- **Regra.** Erro: ... Correção: ... Origem: projeto, data."
def ler_licoes(texto: str) -> list[dict]:
    licoes: list[dict] = []
    secao, atual = "", None

    def fechar(item: str | None) -> None:
        if not item:
            return
        m = re.match(r"\*\*(.+?)\*\*\s*(.*)", item, re.S)
        if not m:
            return
        regra, resto = m.group(1).strip(), m.group(2).strip()
        projeto = None
        antes, achou, origem = resto.rpartition("Origem:")
        if achou:
            resto = antes.strip()
            primeiro = origem.strip().rstrip(".").split(",")[0].strip()
            projeto = primeiro if SLUG.fullmatch(primeiro) else None
        partes = re.match(r"Erro:\s*(.*?)\s*Correção:\s*(.*)$", resto, re.S)
        if partes:
            erro, correcao = partes.group(1).strip(), partes.group(2).strip()
        else:
            erro, correcao = "", resto
        licoes.append({"secao": secao, "regra": regra, "erro": erro, "correcao": correcao, "projeto_slug": projeto})

    for linha in texto.splitlines():
        if m := re.match(r"##\s+(.+?)\s*$", linha):
            fechar(atual)
            secao, atual = m.group(1), None
        elif linha.startswith("- ") and secao:
            fechar(atual)
            atual = linha[2:].strip()
        elif atual is not None and linha.startswith((" ", "\t")) and linha.strip():
            atual += " " + linha.strip()
        else:
            fechar(atual)
            atual = None
    fechar(atual)
    return licoes


def chave_licao(secao: str, regra: str) -> tuple[str, str]:
    """A mesma comparação de repositorios.acrescentar_licoes (sem diferença de espaços e caixa)."""
    return " ".join((secao or "").lower().split()), " ".join((regra or "").lower().split())


# --------------------------------------------------------------------------------------------------- relatório

@dataclass
class Relatorio:
    simular: bool
    partes: dict[str, dict] = field(default_factory=dict)

    def _parte(self, parte: str) -> dict:
        return self.partes.setdefault(parte, {"contagens": {}, "nomes": {}, "notas": [], "erro": None})

    def contar(self, parte: str, rotulo: str, nome: str | None = None, n: int = 1) -> None:
        p = self._parte(parte)
        p["contagens"][rotulo] = p["contagens"].get(rotulo, 0) + n
        if nome:
            p["nomes"].setdefault(rotulo, []).append(nome)

    def nota(self, parte: str, texto: str) -> None:
        self._parte(parte)["notas"].append(texto)

    def erro(self, parte: str, texto: str) -> None:
        self._parte(parte)["erro"] = texto

    @property
    def com_erro(self) -> list[str]:
        return [p for p, d in self.partes.items() if d["erro"]]

    def texto(self) -> str:
        linhas = []
        for parte, d in self.partes.items():
            itens = []
            for rotulo, n in d["contagens"].items():
                nomes = d["nomes"].get(rotulo) or []
                lista = ", ".join(nomes[:8]) + (", …" if len(nomes) > 8 else "")
                itens.append(f"{rotulo} {n}" + (f" ({lista})" if nomes else ""))
            linhas.append(f"{parte:14} {' · '.join(itens) or 'nada a fazer'}")
            linhas += [f"{'':14}   {n}" for n in d["notas"]]
            if d["erro"]:
                linhas.append(f"{'':14}   ERRO: {d['erro']}")
        return "\n".join(linhas)


# ---------------------------------------------------------------------------------------------------- migração

class Migrador:
    def __init__(self, origem: Path, *, simular: bool = False, sobrescrever: bool = False):
        self.origem = origem
        self.destino = config().raiz
        self.simular = simular
        self.sobrescrever = sobrescrever
        self.rel = Relatorio(simular)
        self.banco_ok = False
        self.estado: dict = {"execucoes": {}, "custos": {}}
        self.projetos_no_destino: set[str] = set()

    # banco ---------------------------------------------------------------------------------------------------

    def preparar_banco(self) -> None:
        from nucleo import banco

        if not self.simular:
            banco.iniciar()
            self.banco_ok = True
        else:
            # simulação não cria esquema: sem banco pronto, tudo conta como novo
            self.banco_ok = banco.disponivel()
        if self.banco_ok:
            from nucleo.banco import repositorios as repo
            salvo = repo.ler_config(ESTADO) or {}
            self.estado = {"execucoes": dict(salvo.get("execucoes") or {}), "custos": dict(salvo.get("custos") or {})}
            self.projetos_no_destino = {p["slug"] for p in repo.projetos()}

    def ler(self, funcao: Callable[[], Any], padrao: Any = None) -> Any:
        """Leitura do destino; na simulação sem banco pronto, o padrão."""
        return funcao() if self.banco_ok else padrao

    def _gravar_estado(self, s) -> None:
        """Grava o estado da migração na mesma transação do que acabou de ser importado."""
        from nucleo.banco.modelos import Configuracao

        valor = {"execucoes": dict(self.estado["execucoes"]), "custos": dict(self.estado["custos"]),
                 "origem": str(self.origem), "atualizado_em": agora().isoformat()}
        s.merge(Configuracao(chave=ESTADO, valor=valor, atualizado_em=agora()))

    # partes --------------------------------------------------------------------------------------------------

    def chaves(self) -> None:
        from nucleo import chaves
        from nucleo.banco import repositorios as repo

        env = ler_env(self.origem / ".env")
        if not env:
            self.rel.nota("chaves", "sem .env na origem")
            return
        if not config().segredo_app and not config().banco_e_sqlite:
            # a chave gerada aqui em dados/segredo.key não seria a do servidor: as chaves não abririam lá
            self.rel.erro("chaves", "APP_SEGREDO não está definido: defina o mesmo do servidor e rode de novo "
                                    "(--so chaves)")
            return
        for provedor in chaves.PROVEDORES:
            valor = next((env[v].strip() for v in chaves.variaveis(provedor) if env.get(v, "").strip()), None)
            if not valor:
                continue
            if any(c in valor for c in "\r\n") or len(valor) > 400:
                self.rel.contar("chaves", "inválidas", provedor)
                continue
            if self.ler(lambda: repo.mascara_chave(provedor)) and not self.sobrescrever:
                self.rel.contar("chaves", "já no banco", provedor)
                continue
            if not self.simular:
                repo.gravar_chave(provedor, valor)
            self.rel.contar("chaves", "importadas", provedor)
            if any(os.environ.get(v) for v in chaves.variaveis(provedor)):
                self.rel.nota("chaves", f"{provedor}: a variável de ambiente deste processo vale mais que a do banco")

    def configuracoes(self) -> None:
        from nucleo.banco import repositorios as repo

        arquivo = self.origem / "dados" / "conexoes.json"
        if not arquivo.is_file():
            self.rel.nota("configuracoes", "sem dados/conexoes.json na origem")
            return
        antigas = (json_ou(arquivo.read_text(encoding="utf-8"), {}) or {}).get("etapas") or {}
        atual = self.ler(lambda: repo.ler_config("modelos"), None) or {}
        novas: dict[str, dict] = {}
        for etapa, escolha in sorted(antigas.items()):
            modo = str((escolha or {}).get("modo") or "")
            modelo = str((escolha or {}).get("modelo") or "")
            if modo in MODOS_CLAUDE_CODE:
                # o Claude Code aceitava apelidos (opus, sonnet); pela API vale o padrão do nível de cada agente
                provedor, modelo = "anthropic", modelo if modelo.startswith("claude-") else ""
            elif modo in PROVEDORES_LLM:
                provedor = modo
            else:
                self.rel.nota("configuracoes", f"etapa {etapa}: modo desconhecido {modo!r}, ignorado")
                continue
            if ((atual.get("etapas") or {}).get(etapa) or {}).get("provedor") and not self.sobrescrever:
                self.rel.contar("configuracoes", "etapas já configuradas", etapa)
                continue
            novas[etapa] = {"provedor": provedor, "modelo": modelo}
            self.rel.contar("configuracoes", "etapas", f"{etapa}→{provedor}" + (f" {modelo}" if modelo else ""))
        self.rel.nota("configuracoes", "listas de modelos e testes das chaves não são copiados: a interface refaz "
                                       "ao testar cada chave")
        if not novas or self.simular:
            return
        try:
            from nucleo import rotas
        except ImportError:
            rotas = None
        if rotas is not None:
            rotas.gravar({"etapas": novas})
        else:
            repo.gravar_config("modelos", {**atual, "etapas": {**(atual.get("etapas") or {}), **novas}})

    def _formulario_da_pasta(self, pasta: Path) -> dict:
        """O formulário de um projeto feito pela conversa: o pedido original, como a interface antiga fazia."""
        formulario: dict = {"valores": {}, "personagens": []}
        try:
            import pedido
        except Exception:  # noqa: BLE001 — sem ruamel ou com o pedido.py mudando: formulário vazio
            return formulario
        for nome in ("00-pedido-original.yaml", "00-pedido.yaml"):
            if (pasta / nome).is_file():
                try:
                    p = pedido.carregar(pasta / nome)
                except Exception as e:  # noqa: BLE001
                    self.rel.nota("projetos", f"{pasta.name}: não li {nome} ({type(e).__name__})")
                    break
                formulario["valores"] = {k: v for k, v in p.valores.items()
                                         if v not in (None, [], "") and k != "personagens"}
                formulario["personagens"] = p.valores.get("personagens") or []
                break
        return formulario

    def _etapa_da_pasta(self, slug: str) -> str:
        try:
            from nucleo.pipeline import etapas
            return etapas.primeira_pendente(slug)
        except Exception:  # noqa: BLE001 — sem o módulo, ou a pasta ainda não está no destino
            return "configurar"

    def _gravar_projeto(self, slug: str, campos: dict) -> str:
        """Cria (ou, com --sobrescrever, substitui) a linha do projeto. Devolve o rótulo do que aconteceu."""
        from nucleo import banco
        from nucleo.banco.modelos import Projeto

        if slug in self.projetos_no_destino and not self.sobrescrever:
            return "já existiam"
        rotulo = "substituídos" if slug in self.projetos_no_destino else "criados"
        if not self.simular:
            with banco.sessao() as s:
                p = s.get(Projeto, slug)
                if p is None:
                    s.add(Projeto(slug=slug, **campos))
                else:
                    for nome, valor in campos.items():
                        setattr(p, nome, valor)
        self.projetos_no_destino.add(slug)
        return rotulo

    def projetos(self) -> None:
        antigos = ler_sqlite(self.origem / "dados" / "web.sqlite3", "SELECT * FROM projetos")
        vistos = set()
        for linha in antigos:
            slug = linha["slug"]
            vistos.add(slug)
            if not SLUG.fullmatch(slug or ""):
                self.rel.contar("projetos", "slug inválido", slug)
                continue
            pasta = self.origem / "projetos" / slug
            campos = {
                "titulo": linha.get("titulo") or slug,
                "etapa": linha.get("etapa") or "configurar",
                "formulario": json_ou(linha.get("formulario"), {}) or {},
                "sugestoes": json_ou(linha.get("sugestoes"), None),
                "origem": "roteiro-importado" if (pasta / "00-roteiro-original.md").is_file() else "pipeline",
                "criado_em": hora(linha.get("criado_em")) or agora(),
                "atualizado_em": hora(linha.get("atualizado_em")) or agora(),
            }
            self.rel.contar("projetos", self._gravar_projeto(slug, campos), slug)
        base = self.origem / "projetos"
        for pasta in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
            slug = pasta.name
            if slug in vistos:
                continue
            if not SLUG.fullmatch(slug):
                self.rel.contar("projetos", "pasta com nome inválido", slug)
                continue
            brief = json_ou((pasta / "00-brief.json").read_text(encoding="utf-8"), {}) \
                if (pasta / "00-brief.json").is_file() else {}
            formulario = self._formulario_da_pasta(pasta)
            titulo = brief.get("titulo") or brief.get("tema") or formulario["valores"].get("titulo") or slug
            datas = [datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) for f in pasta.iterdir() if f.is_file()]
            campos = {
                "titulo": titulo, "etapa": self._etapa_da_pasta(slug), "formulario": formulario, "sugestoes": None,
                "origem": "roteiro-importado" if (pasta / "00-roteiro-original.md").is_file() else "pipeline",
                "criado_em": min(datas) if datas else agora(), "atualizado_em": max(datas) if datas else agora(),
            }
            rotulo = self._gravar_projeto(slug, campos)
            self.rel.contar("projetos", f"{rotulo} (só a pasta)" if rotulo != "já existiam" else rotulo, slug)

    def execucoes(self) -> None:
        from nucleo import banco
        from nucleo.banco.modelos import Execucao, ExecucaoLog

        linhas = ler_sqlite(self.origem / "dados" / "web.sqlite3", "SELECT * FROM execucoes ORDER BY id")
        for linha in linhas:
            chave = str(linha["id"])
            if chave in self.estado["execucoes"]:
                self.rel.contar("execucoes", "já importadas")
                continue
            slug = linha.get("slug")
            if slug not in self.projetos_no_destino:
                self.rel.contar("execucoes", "sem projeto no destino", slug)
                continue
            # "rodando" no banco antigo é uma execução que o servidor antigo largou no meio
            status = linha.get("status") if linha.get("status") in STATUS_FINAIS else "interrompida"
            inicio = hora(linha.get("iniciada_em")) or agora()
            fim = hora(linha.get("terminada_em")) or inicio
            parametros: dict = {"migrado_de": ORIGEM_EXECUCOES, "id_antigo": linha["id"]}
            if linha.get("sessao"):
                parametros["sessao_claude_code"] = linha["sessao"]
            self.rel.contar("execucoes", "importadas")
            if self.simular:
                continue
            with banco.sessao() as s:
                e = Execucao(projeto_slug=slug, etapa=linha.get("etapa") or "", status=status,
                             parametros=parametros, rota={}, resultado=linha.get("resultado") or "",
                             estruturado=None, avisos=[], custo_usd=float(linha.get("custo_usd") or 0.0),
                             trabalhador="migracao", chave_exclusiva=None, cancelar_solicitado=False,
                             criada_em=inicio, iniciada_em=inicio, terminada_em=fim, batimento_em=None)
                s.add(e)
                s.flush()
                if linha.get("log"):
                    # o log antigo inteiro numa linha só: o texto sai igual, qualquer que seja a junção das linhas
                    s.add(ExecucaoLog(execucao_id=e.id, texto=linha["log"], criado_em=fim))
                self.estado["execucoes"][chave] = e.id
                self._gravar_estado(s)

    def contextos(self) -> None:
        from nucleo.banco import repositorios as repo

        base = self.origem / "pedidos" / "contextos"
        for manifesto in sorted(base.glob("*/contexto.json")) if base.is_dir() else []:
            id_ = manifesto.parent.name
            if not SLUG.fullmatch(id_):
                self.rel.contar("contextos", "id inválido", id_)
                continue
            dados = json_ou(manifesto.read_text(encoding="utf-8"), None)
            if not isinstance(dados, dict):
                self.rel.contar("contextos", "manifesto ilegível", id_)
                continue
            if self.ler(lambda: repo.contexto(id_)) and not self.sobrescrever:
                self.rel.contar("contextos", "já existiam", id_)
                continue
            anexos = [{"arquivo": a["arquivo"], "tipo": a.get("tipo") or "", "descricao": a.get("descricao") or ""}
                      for a in dados.get("anexos") or [] if isinstance(a, dict) and a.get("arquivo")]
            if not self.simular:
                repo.salvar_contexto(id_, nome=dados.get("nome") or id_, texto=dados.get("texto") or "",
                                     anexos=anexos)
            self.rel.contar("contextos", "importados", id_)
        soltos = sorted(base.glob("*.md")) if base.is_dir() else []
        if soltos:
            self.rel.nota("contextos", f"{len(soltos)} contexto(s) antigo(s) em .md solto seguem como arquivos "
                                       "(só leitura)")

    def licoes(self) -> None:
        from nucleo.banco import repositorios as repo

        arquivo = self.origem / "biblioteca" / "licoes-roteiro.md"
        if not arquivo.is_file():
            self.rel.nota("licoes", "sem biblioteca/licoes-roteiro.md na origem")
            return
        lista = ler_licoes(arquivo.read_text(encoding="utf-8"))
        existentes = {chave_licao(l["secao"], l["regra"]) for l in self.ler(repo.licoes, [])}
        novas = [l for l in lista if chave_licao(l["secao"], l["regra"]) not in existentes]
        if len(lista) - len(novas):
            self.rel.contar("licoes", "já no banco", n=len(lista) - len(novas))
        if not novas:
            return
        n = len(novas) if self.simular else repo.acrescentar_licoes(novas)
        self.rel.contar("licoes", "importadas", n=n)

    def desempenho(self) -> None:
        from nucleo.banco import repositorios as repo

        arquivo = self.origem / "dados" / "desempenho.jsonl"
        if not arquivo.is_file():
            self.rel.nota("desempenho", "sem dados/desempenho.jsonl na origem")
            return
        registros, quebradas = ler_jsonl(arquivo)
        if quebradas:
            self.rel.nota("desempenho", f"{quebradas} linha(s) ilegível(is) ignorada(s)")
        existentes = {(d["video_id"], d["janela"]) for d in self.ler(repo.desempenhos, [])}
        for r in registros:
            nome = f"{r.get('video_id')}/{r.get('janela')}"
            if (r.get("video_id"), r.get("janela") or "7d") in existentes and not self.sobrescrever:
                self.rel.contar("desempenho", "já no banco", nome)
                continue
            if not self.simular:
                try:
                    repo.registrar_desempenho(r)
                except ValueError as e:
                    self.rel.contar("desempenho", "recusados", nome)
                    self.rel.nota("desempenho", f"{nome}: {e}")
                    continue
            self.rel.contar("desempenho", "importados", nome)

    def custos(self) -> None:
        import custos as tabela_de_custos
        from nucleo import banco
        from nucleo.banco.modelos import Custo

        precos = tabela_de_custos.tabela()
        base = self.origem / "projetos"
        for arquivo in sorted(base.glob("*/custos.jsonl")) if base.is_dir() else []:
            slug = arquivo.parent.name
            if slug in self.estado["custos"]:
                self.rel.contar("custos", "projetos já importados", slug)
                continue
            itens, quebradas = ler_jsonl(arquivo)
            if quebradas:
                self.rel.nota("custos", f"{slug}: {quebradas} linha(s) ilegível(is) ignorada(s)")
            linhas = []
            for item in itens:
                if not item.get("servico") or not item.get("modelo"):
                    continue
                item.setdefault("quando", agora().isoformat())
                item.setdefault("uso", {})
                try:
                    if item["servico"] in ("llm", "busca"):
                        valor = tabela_de_custos.custo_llm(item["modelo"], item["uso"] or {}, item["quando"], precos)
                    else:
                        valor = tabela_de_custos.custo(item, precos)
                except Exception:  # noqa: BLE001 — registro torto: fica sem preço
                    valor = None
                conhecidos = {"quando", "servico", "modelo", "uso", "item", "estimado", "provedor", "agente"}
                linhas.append(Custo(
                    execucao_id=None, projeto_slug=slug, servico=item["servico"],
                    provedor=item.get("provedor") or provedor_do_modelo(item["modelo"]), modelo=item["modelo"],
                    agente=item.get("agente"), item=str(item["item"])[:200] if item.get("item") is not None else None,
                    uso=item.get("uso") or {}, extra={k: v for k, v in item.items() if k not in conhecidos},
                    custo_usd=valor, estimado=bool(item.get("estimado")), quando=hora(item["quando"]) or agora()))
            self.rel.contar("custos", "chamadas", n=len(linhas))
            self.rel.contar("custos", "projetos", slug)
            if self.simular:
                continue
            with banco.sessao() as s:
                s.add_all(linhas)
                self.estado["custos"][slug] = len(linhas)
                self._gravar_estado(s)

    def copiar_para_o_destino(self, sincronizados: tuple[str, ...], e_ignorado: Callable[[str], bool]) -> None:
        """Traz os arquivos da origem para o espaço de trabalho, de onde importar_tudo() os envia."""
        copiados = iguais = mantidos = 0
        for rel in arquivos_sincronizados(self.origem, sincronizados, e_ignorado):
            de, para = self.origem / rel, self.destino / rel
            if para.exists():
                a, b = de.stat(), para.stat()
                if a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime):
                    iguais += 1
                    continue
                if not self.sobrescrever:
                    mantidos += 1
                    continue
            if not self.simular:
                para.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(de, para)  # copy2 preserva o mtime, que algumas etapas usam como desempate
            copiados += 1
        self.rel.contar("arquivos", "copiados da origem", n=copiados)
        if iguais:
            self.rel.contar("arquivos", "já iguais no destino", n=iguais)
        if mantidos:
            self.rel.nota("arquivos", f"{mantidos} arquivo(s) diferente(s) já no destino ficaram como estavam "
                                      "(--sobrescrever para trocar)")

    @staticmethod
    def _regras_do_espaco() -> tuple[tuple[str, ...], Callable[[str], bool]]:
        """O que o espaço de trabalho sincroniza e o que ele ignora (as mesmas regras do envio)."""
        try:
            from nucleo import espaco
        except ImportError:
            return SINCRONIZADOS_PADRAO, ignorado_padrao
        return (tuple(getattr(espaco, "SINCRONIZADOS", SINCRONIZADOS_PADRAO)),
                getattr(espaco, "ignorado", ignorado_padrao))

    def arquivos(self) -> None:
        if self.simular:
            sincronizados, e_ignorado = self._regras_do_espaco()
            base = self.origem if self.origem.resolve() != self.destino.resolve() else self.destino
            caminhos = list(arquivos_sincronizados(base, sincronizados, e_ignorado))
            tamanho = sum((base / c).stat().st_size for c in caminhos)
            self.rel.contar("arquivos", "a enviar (no máximo)", n=len(caminhos))
            self.rel.nota("arquivos", f"{tamanho / 1e6:.1f} MB no total; só o que falta no armazenamento é enviado")
            return
        from nucleo import espaco

        resultado = espaco.importar_tudo() or {}
        for chave, valor in resultado.items():
            if isinstance(valor, bool) or not isinstance(valor, (int, float)):
                self.rel.nota("arquivos", f"{chave}: {valor}")
            else:
                self.rel.contar("arquivos", f"enviados {chave}", n=int(valor))

    # execução ------------------------------------------------------------------------------------------------

    def rodar(self, partes: tuple[str, ...]) -> Relatorio:
        self.preparar_banco()
        sincronizados, e_ignorado = self._regras_do_espaco()
        # A cópia vem antes de tudo (a etapa de um projeto só de pasta é calculada no destino); o envio ao
        # armazenamento, que é a parte demorada, fica por último.
        if "arquivos" in partes and self.origem.resolve() != self.destino.resolve():
            try:
                self.copiar_para_o_destino(sincronizados, e_ignorado)
            except Exception as e:  # noqa: BLE001
                self.rel.erro("arquivos", f"cópia: {type(e).__name__}: {e}")
                partes = tuple(p for p in partes if p != "arquivos")
        for parte in (p for p in PARTES if p in partes):
            try:
                getattr(self, parte)()
            except Exception as e:  # noqa: BLE001 — uma parte com erro não impede as outras
                self.rel.erro(parte, f"{type(e).__name__}: {e}")
        return self.rel


def escolher_partes(so: str | None, pular: str | None) -> tuple[str, ...]:
    def lista(texto: str | None) -> list[str]:
        nomes = [n.strip() for n in (texto or "").split(",") if n.strip()]
        desconhecidas = [n for n in nomes if n not in PARTES]
        if desconhecidas:
            raise SystemExit(f"erro: parte desconhecida: {', '.join(desconhecidas)} (use {', '.join(PARTES)})")
        return nomes

    escolhidas = lista(so) or list(PARTES)
    fora = set(lista(pular))
    return tuple(p for p in PARTES if p in escolhidas and p not in fora)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--origem", type=Path, help="raiz da instalação antiga (padrão: esta raiz)")
    ap.add_argument("--simular", action="store_true", help="só mostra o que faria, sem gravar nada")
    ap.add_argument("--sobrescrever", action="store_true",
                    help="substitui no destino o que já existe (projetos, contextos, chaves, etapas, métricas, arquivos)")
    ap.add_argument("--so", metavar="PARTES", help=f"só estas partes, separadas por vírgula ({', '.join(PARTES)})")
    ap.add_argument("--pular", metavar="PARTES", help="pula estas partes")
    ap.add_argument("--json", action="store_true", help="imprime o relatório em JSON")
    args = ap.parse_args(argv)

    partes = escolher_partes(args.so, args.pular)
    origem = (args.origem or config().raiz).resolve()
    if not origem.is_dir():
        print(f"erro: {origem} não é uma pasta", file=sys.stderr)
        return 2
    c = config()
    print(f"{'simulação: ' if args.simular else ''}origem {origem} → banco {sem_senha(c.banco_url)}, "
          f"armazenamento {c.armazenamento} ({c.bucket if c.armazenamento != 'local' else c.pasta_objetos_local})",
          file=sys.stderr)
    rel = Migrador(origem, simular=args.simular, sobrescrever=args.sobrescrever).rodar(partes)
    if args.json:
        print(json.dumps({"simular": args.simular, "partes": rel.partes}, ensure_ascii=False, indent=2))
    else:
        print(rel.texto())
        if args.simular:
            print("\nsimulação: nada foi gravado. Rode sem --simular para importar.")
    return 1 if rel.com_erro else 0


if __name__ == "__main__":
    sys.exit(main())
