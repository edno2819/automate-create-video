"""As etapas do pipeline como a interface web as mostra e o trabalhador as conduz.

Cada etapa sabe dizer, pelos arquivos de projetos/<slug>/, se já está pronta e se vale para o projeto (a direção
visual e a produção dependem de producao.ate; um projeto que nasceu de um roteiro pronto do usuário troca pesquisa,
ângulo, gancho e roteiro pela etapa `importar`). As decisões do usuário
(aprovar o pedido, escolher o ângulo e o gancho, editar o roteiro) ficam na interface e chegam ao pipeline como
arquivos; o que cada etapa faz com os agentes e os scripts está em nucleo/pipeline/fluxos.py.

As funções leem o disco local: o espaço de trabalho (`config().raiz`, que no servidor é a raiz do repositório e nos
testes é APP_RAIZ). Quem chama baixa o projeto antes (`espaco.baixar`); o trabalhador e a web já fazem isso.
`PROJETOS`, `PEDIDOS` e `ESPACO` são calculados na hora (atributos do módulo), porque o espaço de trabalho vem da
configuração.

Versões: quando uma etapa termina, o hash de cada arquivo que ela usou das anteriores (e das partes do brief que
mudam o resultado dela: estilo, ritmo das imagens, vozes, legendas, música...) fica em projetos/<slug>/.etapas.json.
Se uma entrada muda depois, a etapa e as que dependem dela aparecem como desatualizadas.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from nucleo import RAIZ as CODIGO

PEDIDO_PY = CODIGO / "ferramentas" / "pedido.py"
MEDIR_PY = CODIGO / "ferramentas" / "medir_roteiro.py"
ORIGEM_IMPORTADA = "roteiro-importado"


def _espaco() -> Path:
    from nucleo.config import config
    return config().raiz


def __getattr__(nome: str):
    """PROJETOS, PEDIDOS (os pedidos gravados pela interface; os escritos à mão ficam em pedidos/) e ESPACO, sempre
    do espaço de trabalho atual."""
    if nome == "ESPACO":
        return _espaco()
    if nome == "PROJETOS":
        return _espaco() / "projetos"
    if nome == "PEDIDOS":
        return _espaco() / "pedidos" / "web"
    raise AttributeError(f"module {__name__!r} has no attribute {nome!r}")


def _projetos() -> Path:
    return _espaco() / "projetos"


def _pedidos() -> Path:
    return _espaco() / "pedidos" / "web"


@dataclass(frozen=True)
class Etapa:
    id: str
    nome: str
    descricao: str


ETAPAS = [
    Etapa("configurar", "Configuração", "o que você quer no vídeo"),
    Etapa("sugestoes", "Sugestões", "o sistema completa o pedido e você aprova"),
    Etapa("importar", "Roteiro importado", "o seu roteiro, dividido em blocos sem mudar nenhuma palavra"),
    Etapa("pesquisa", "Pesquisa", "trends, referências e dossiê"),
    Etapa("angulos", "Ângulo", "título, thumb e promessa"),
    Etapa("ganchos", "Gancho", "aberturas lidas a frio e pontuadas; você escolhe"),
    Etapa("roteiro", "Roteiro", "roteirista e crítico, versão a versão"),
    Etapa("visual", "Direção visual", "bíblia visual, cenas e storyboard"),
    Etapa("producao", "Produção", "imagens, narração e montagem"),
    Etapa("edicao", "Edição", "dublagens, outra música e legendas sobre o vídeo pronto"),
]
POR_ID = {e.id: e for e in ETAPAS}
SO_DO_PIPELINE = ("pesquisa", "angulos", "ganchos", "roteiro")  # o roteiro importado pula estas


def sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def slug_de(texto: str, max_palavras: int = 6) -> str:
    palavras = re.findall(r"[a-z0-9]+", sem_acento(texto.lower()))
    vazias = {"a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "um", "uma", "no", "na", "em"}
    uteis = [p for p in palavras if p not in vazias] or palavras
    return "-".join(uteis[:max_palavras])[:60].strip("-") or "video"


def pasta(slug: str) -> Path:
    return _projetos() / slug


def ler_json(caminho: Path):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


class Brief(dict):
    """O brief (00-brief.json) com o slug do projeto ao lado, fora das chaves: `aplicavel(etapa, brief)` reconhece
    o projeto de roteiro importado mesmo quando quem chama não passa o slug. Serializa como um dict comum."""
    slug: str | None = None


def brief(slug: str) -> Brief | None:
    dados = ler_json(pasta(slug) / "00-brief.json")
    if not isinstance(dados, dict):
        return None
    b = Brief(dados)
    b.slug = slug
    return b


def trends_ativa(b: dict | None) -> bool:
    return bool(((b or {}).get("trends") or {}).get("ativa", True))


def importado(slug: str) -> bool:
    """O projeto nasceu de um roteiro pronto do usuário: tem 00-roteiro-original.md e não tem ângulo escolhido (o
    projeto do pipeline nunca tem o original), ou o banco diz origem roteiro-importado."""
    p = pasta(slug)
    if (p / "04-angulo-escolhido.json").exists():
        return False
    if (p / "00-roteiro-original.md").exists():
        return True
    try:
        from nucleo import banco
        if banco.disponivel():
            from nucleo.banco import repositorios
            return (repositorios.projeto(slug) or {}).get("origem") == ORIGEM_IMPORTADA
    except Exception:  # noqa: BLE001 - sem banco, vale o que os arquivos dizem
        pass
    return False


def e_importado(b: dict | None, slug: str | None = None) -> bool:
    if (b or {}).get("origem") == ORIGEM_IMPORTADA:
        return True
    slug = slug or getattr(b, "slug", None)
    return bool(slug) and importado(slug)


def aplicavel(etapa: str, b: dict | None, slug: str | None = None) -> bool:
    """A etapa vale para o projeto? `slug` é opcional: sem ele, o projeto de roteiro importado é reconhecido pelo
    brief (o `brief()` daqui carrega o slug, e o pedido.py marca `origem: roteiro-importado`)."""
    imp = e_importado(b, slug)
    if etapa == "importar":
        return imp
    if imp and etapa in SO_DO_PIPELINE:
        return False
    if b is None:
        return True
    ate = ((b.get("producao") or {}).get("ate")) or "video"
    if etapa == "visual":
        return ate in ("visual", "video")
    if etapa in ("producao", "edicao"):
        return ate == "video"
    return True


def pronta(slug: str, etapa: str) -> bool:
    p = pasta(slug)
    if etapa == "configurar":
        return (_pedidos() / f"{slug}.yaml").exists() or (p / "00-pedido.yaml").exists()
    if etapa == "sugestoes":
        return (p / "00-brief.json").exists()
    if etapa == "importar":
        return (p / "06-roteiro-final.md").exists()
    if etapa == "pesquisa":
        return (p / "02-dossie.md").exists() and (not trends_ativa(brief(slug)) or (p / "02-trends.md").exists())
    if etapa == "angulos":
        return (p / "04-angulo-escolhido.json").exists()
    if etapa == "ganchos":
        # projeto de antes desta etapa: o roteiro saiu do gancho proposto do ângulo, e a etapa conta como feita
        return (p / "04-gancho-escolhido.json").exists() or (
            not (p / "04-ganchos.json").exists()
            and ((p / "06-roteiro-final.md").exists() or any(p.glob("05-roteiro-v*.md"))))
    if etapa == "roteiro":
        return (p / "06-roteiro-final.md").exists()
    if etapa == "visual":
        return (p / "08-storyboard.md").exists()
    if etapa == "producao":
        return any((p / "10-video").glob("*.mp4"))
    if etapa == "edicao":  # opcional: pronta quando já tem uma dublagem ou uma exportação
        return any((p / "12-dublagens").glob("*/dublagem.wav")) or any((p / "13-exportacoes").glob("*.json"))
    return False


def proxima(slug: str, etapa: str) -> str | None:
    """A etapa seguinte que vale para o projeto."""
    b = brief(slug)
    ids = [e.id for e in ETAPAS]
    for candidata in ids[ids.index(etapa) + 1:]:
        if aplicavel(candidata, b, slug):
            return candidata
    return None


def primeira_pendente(slug: str) -> str:
    b = brief(slug)
    defasadas = defasagens(slug)
    for e in ETAPAS:
        if aplicavel(e.id, b, slug) and (not pronta(slug, e.id) or e.id in defasadas):
            return e.id
    return ETAPAS[-1].id


# ---------------------------------------------------------------- versões: o que cada etapa usou

# Os arquivos de etapas anteriores que cada etapa lê, e as partes do brief que mudam o resultado dela
# ("00-brief.json#visual.qualidade": o hash do JSON canônico daquela chave). Quando uma etapa termina (e reescreveu
# a própria saída), o hash de cada entrada fica em .etapas.json; se uma entrada muda depois (outro ângulo, outro
# roteiro final, outro estilo), a etapa e as seguintes ficam desatualizadas até rodarem de novo.
BRIEF = "00-brief.json#"
ENTRADAS = {
    "pesquisa": ["00-pedido.yaml"],
    "angulos": ["02-dossie.md"],
    "ganchos": ["04-angulo-escolhido.json"],
    "roteiro": ["04-angulo-escolhido.json", "04-gancho-escolhido.json", BRIEF + "tom", BRIEF + "formato"],
    "visual": ["06-roteiro-final.md", BRIEF + "estilo_artistico", BRIEF + "visual.proporcao",
               BRIEF + "visual.segundos_por_imagem", BRIEF + "visual.numero_de_imagens",
               BRIEF + "visual.imagens_da_internet", BRIEF + "personagens"],
    "producao": ["06-roteiro-final.md", "08-cenas.json", BRIEF + "visual.qualidade", BRIEF + "visual.animacao",
                 BRIEF + "narracao", BRIEF + "personagens", BRIEF + "legendas", BRIEF + "musica"],
}
QUEM_GERA = {"00-pedido.yaml": "sugestoes", "02-dossie.md": "pesquisa", "04-angulo-escolhido.json": "angulos",
             "04-gancho-escolhido.json": "ganchos", "06-roteiro-final.md": "roteiro", "08-cenas.json": "visual",
             "00-brief.json": "sugestoes"}
MUDANCA = {"sugestoes": "o pedido mudou", "pesquisa": "o dossiê mudou", "angulos": "o ângulo escolhido mudou",
           "ganchos": "o gancho escolhido mudou", "roteiro": "o roteiro final mudou", "importar": "o roteiro final mudou",
           "visual": "a direção visual mudou"}
CONTROLE = ".etapas.json"


def _arquivo_da_entrada(nome: str) -> str:
    return nome.split("#", 1)[0]


def quem_gera(nome: str) -> str:
    return QUEM_GERA[_arquivo_da_entrada(nome)]


def saida(slug: str, etapa: str) -> Path | None:
    """O arquivo que marca a saída da etapa (o vídeo mais novo, na produção)."""
    p = pasta(slug)
    if etapa == "producao":
        return max((p / "10-video").glob("*.mp4"), key=lambda a: a.stat().st_mtime, default=None)
    nome = {"pesquisa": "02-dossie.md", "angulos": "04-angulo-escolhido.json", "ganchos": "04-gancho-escolhido.json",
            "roteiro": "06-roteiro-final.md",
            "importar": "06-roteiro-final.md", "visual": "08-cenas.json"}.get(etapa)
    return p / nome if nome and (p / nome).exists() else None


def hash_de(caminho: Path) -> str | None:
    try:
        return hashlib.sha1(caminho.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def _parte(dados: dict | None, chave: str):
    valor = dados
    for parte in chave.split("."):
        if not isinstance(valor, dict):
            return None
        valor = valor.get(parte)
    return valor


def hash_da_entrada(slug: str, nome: str, brief_cache: dict | None = None) -> str | None:
    """O hash de uma entrada: do arquivo inteiro, ou do JSON canônico de uma parte do brief."""
    if "#" not in nome:
        return hash_de(pasta(slug) / nome)
    arquivo, chave = nome.split("#", 1)
    dados = brief_cache if brief_cache is not None else ler_json(pasta(slug) / arquivo)
    if not isinstance(dados, dict):
        return None
    canonico = json.dumps(_parte(dados, chave), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonico.encode("utf-8")).hexdigest()[:16]


def controle(slug: str) -> dict:
    return ler_json(pasta(slug) / CONTROLE) or {}


def registrar(slug: str, etapa: str, desde: float | None = None) -> bool:
    """Anota as entradas que a etapa acabou de usar. Com desde (início da execução), só anota se a saída da etapa
    foi reescrita nessa execução: um roteiro sem aprovação não muda o roteiro final, que continua sendo o antigo."""
    if etapa not in ENTRADAS:
        return False
    marca = saida(slug, etapa)
    if marca is None or (desde is not None and marca.stat().st_mtime < desde - 1):
        return False
    doc = controle(slug)
    b = ler_json(pasta(slug) / "00-brief.json")
    doc[etapa] = {"em": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "entradas": {nome: hash_da_entrada(slug, nome, b if "#" in nome else None) for nome in ENTRADAS[etapa]}}
    (pasta(slug) / CONTROLE).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def mudancas(slug: str, etapa: str, doc: dict | None = None) -> list[str]:
    """As etapas anteriores cuja saída mudou depois que esta etapa rodou. Sem registro (projetos de antes deste
    controle, ou feitos pela conversa), compara as datas dos arquivos (as partes do brief ficam de fora: o brief é
    gerado de novo a cada pesquisa, e a data dele não diz se a parte mudou)."""
    p = pasta(slug)
    registro = (controle(slug) if doc is None else doc).get(etapa)
    marca = saida(slug, etapa)
    b = ler_json(p / "00-brief.json")
    mudaram = []
    for nome in ENTRADAS.get(etapa, []):
        if registro:
            # uma entrada que o registro não tem (registro de antes dela existir) não conta como mudança
            if nome in registro["entradas"] and registro["entradas"][nome] != hash_da_entrada(
                    slug, nome, b if "#" in nome else None):
                mudaram.append(quem_gera(nome))
        elif "#" not in nome:
            entrada = p / nome
            if marca and entrada.exists() and entrada.stat().st_mtime > marca.stat().st_mtime + 1:
                mudaram.append(quem_gera(nome))
    return list(dict.fromkeys(mudaram))


def dependencias(etapa: str) -> set[str]:
    """Todas as etapas de que esta depende, direta ou indiretamente."""
    diretas = {quem_gera(n) for n in ENTRADAS.get(etapa, [])}
    diretas.discard(etapa)
    return diretas.union(*(dependencias(d) for d in diretas)) if diretas else set()


def defasagens(slug: str) -> dict[str, str]:
    """{etapa: motivo} das etapas prontas que ficaram para trás: uma entrada mudou depois que ela rodou, ou ela
    depende de uma etapa que está desatualizada."""
    b, doc = brief(slug), controle(slug)
    saida_ = {}
    anteriores: list[str] = []
    for e in ETAPAS:
        if not aplicavel(e.id, b, slug) or not pronta(slug, e.id):
            continue
        if mudou := mudancas(slug, e.id, doc):
            saida_[e.id] = "; ".join(dict.fromkeys(MUDANCA[m] for m in mudou)) + " depois que esta etapa rodou"
        elif atrasada := next((a for a in anteriores if a in saida_ and a in dependencias(e.id)), None):
            saida_[e.id] = f"ela depende da etapa {POR_ID[atrasada].nome}, que está desatualizada"
        anteriores.append(e.id)
    return saida_


def nota_de_refazer(slug: str, etapa: str) -> str:
    """Para a mensagem do agente que refaz uma etapa: o que mudou e quais arquivos são da versão anterior."""
    if not pronta(slug, etapa) or not (mudou := mudancas(slug, etapa)):
        return ""
    antigos = {"visual": "a bíblia, os arquivos de 08-cenas/ e o 08-thumbnail.json"}
    return (f"\n\nAtenção: {'; '.join(MUDANCA[m] for m in mudou)} desde a última vez que esta etapa rodou. "
            f"Refaça do zero a partir dos arquivos atuais: {antigos.get(etapa, 'os arquivos desta etapa')} são da versão "
            "anterior e não valem mais (pode sobrescrevê-los).")


# ---------------------------------------------------------------- capa


def capa(slug: str) -> Path | None:
    """A imagem que representa o projeto na lista: a thumbnail com o texto, a imagem gerada para ela ou, nos projetos
    produzidos antes da thumbnail, a primeira cena."""
    p = pasta(slug)
    candidatas = [p / "10-video" / "thumbnail.jpg"] + [p / "09-imagens" / f"thumbnail.{ext}" for ext in ("png", "jpg", "webp")]
    cenas = (ler_json(p / "08-cenas.json") or {}).get("cenas") or []
    if cenas:
        candidatas += [p / "09-imagens" / f"{cenas[0]['id']}.{ext}" for ext in ("png", "jpg", "webp")]
    candidatas += sorted((p / "09-imagens").glob("B*.*")) if (p / "09-imagens").is_dir() else []
    return next((c for c in candidatas if c.is_file()), None)


# ---------------------------------------------------------------- forks

# Os arquivos de cada etapa na pasta do projeto, pelo começo do nome.
ARQUIVOS_DA_ETAPA = {
    "sugestoes": ("00-",), "importar": ("06-",), "pesquisa": ("01-", "02-"),
    "angulos": ("03-", "04-angulo", "04-reforco"), "ganchos": ("04-gancho",), "roteiro": ("05-", "06-"),
    "visual": ("08-",),
    "producao": ("09-", "10-", "11-", "custos.jsonl"), "edicao": ("12-",),  # as exportações saem de novo, sem custo
}
TEXTO = {".md", ".json", ".jsonl", ".yaml", ".yml", ".txt", ".ass", ".srt"}


def slug_livre(base: str, ocupados: set[str]) -> str:
    for n in range(1, 1000):
        candidato = f"{base}-fork" if n == 1 else f"{base}-fork-{n}"
        if candidato not in ocupados and not pasta(candidato).exists() and not (_pedidos() / f"{candidato}.yaml").exists():
            return candidato
    raise ValueError("não achei um nome livre para o fork")


def fork(slug: str, novo: str, ate: str) -> list[str]:
    """Copia o projeto para projetos/<novo> com os arquivos das etapas até `ate`, inclusive, trocando o slug nos
    arquivos de texto, com a data original de cada arquivo (a comparação de datas, nos projetos sem registro de
    versões, depende dela). A cópia passa pelo espaço de trabalho (nucleo.espaco.copiar): os arquivos vão para o
    armazenamento do projeto novo. O que vem depois de `ate` fica para o fork refazer. Devolve as etapas copiadas."""
    from nucleo import espaco
    ids = [e.id for e in ETAPAS]
    etapas_copiadas = [e for e in ids[: ids.index(ate) + 1] if e in ARQUIVOS_DA_ETAPA]
    prefixos = tuple(dict.fromkeys(pre for e in etapas_copiadas for pre in ARQUIVOS_DA_ETAPA[e]))
    origem, destino = f"projetos/{slug}/", f"projetos/{novo}/"
    espaco.baixar(origem)
    if not pasta(slug).is_dir():
        raise ValueError(f"o projeto {slug} não tem arquivos para copiar")
    defasadas = defasagens(slug)
    pasta(novo).mkdir(parents=True, exist_ok=True)

    def trocar(texto: str) -> str:
        return texto.replace(slug, novo)

    for item in sorted(pasta(slug).iterdir()):
        if not item.name.startswith(prefixos):
            continue
        if item.is_file():
            espaco.copiar(origem + item.name, destino + item.name, trocar)
            continue
        if item.name != "10-video":
            espaco.copiar(f"{origem}{item.name}/", f"{destino}{item.name}/", trocar)
            continue
        # o vídeo leva o slug no nome: copia um a um, trocando o nome do mp4 (sem subir o vídeo duas vezes)
        for arq in sorted(a for a in item.rglob("*") if a.is_file()):
            rel = arq.relative_to(pasta(slug)).as_posix()
            novo_rel = rel[:-len(f"{slug}.mp4")] + f"{novo}.mp4" if rel == f"10-video/{slug}.mp4" else rel
            espaco.copiar(origem + rel, destino + novo_rel, trocar)
    pedido_web = f"pedidos/web/{slug}.yaml"
    espaco.baixar(pedido_web)
    if (_espaco() / pedido_web).exists():
        espaco.copiar(pedido_web, f"pedidos/web/{novo}.yaml", trocar)
    # O slug trocado mudou o hash das entradas: as etapas que estavam em dia no original continuam em dia no fork.
    for e in etapas_copiadas:
        if e not in defasadas and pronta(novo, e):
            registrar(novo, e)
    espaco.enviar(destino)
    return etapas_copiadas


# ---------------------------------------------------------------- scripts determinísticos


def ambiente_dos_scripts(extra: dict[str, str] | None = None) -> dict[str, str]:
    """O ambiente de um script de ferramentas/: o do processo, com o código no PYTHONPATH e o espaço de trabalho em
    APP_RAIZ (os scripts leem os dados de lá)."""
    caminhos = [str(CODIGO)] + [c for c in (os.environ.get("PYTHONPATH") or "").split(os.pathsep) if c]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(dict.fromkeys(caminhos)), "APP_RAIZ": str(_espaco()),
           "PYTHONIOENCODING": "utf-8"}
    env.update(extra or {})
    return env


def rodar_pedido(*args: str) -> tuple[int, str]:
    """ferramentas/pedido.py com o mesmo Python do servidor (que já tem o ruamel.yaml), na raiz do espaço de
    trabalho."""
    r = subprocess.run([sys.executable, str(PEDIDO_PY), *map(str, args)], cwd=_espaco(), capture_output=True,
                       text=True, env=ambiente_dos_scripts())
    return r.returncode, (r.stdout + r.stderr).strip()


def medir(roteiro: Path) -> dict | None:
    """A medição objetiva do roteiro (ferramentas/medir_roteiro.py --json), gravada ao lado dele como
    05-medicao-vN.json (ou medicao-<nome>.json, nos outros roteiros), ou {"erro": ...}."""
    if roteiro.name.startswith("05-roteiro-"):
        nome = roteiro.name.replace("05-roteiro-", "05-medicao-").replace(".md", ".json")
    else:
        nome = f"medicao-{roteiro.stem}.json"
    saida_ = roteiro.with_name(nome)
    r = subprocess.run([sys.executable, str(MEDIR_PY), str(roteiro), "--json", str(saida_)], cwd=_espaco(),
                       capture_output=True, text=True, env=ambiente_dos_scripts())
    return ler_json(saida_) if r.returncode == 0 and saida_.exists() else {"erro": (r.stdout + r.stderr).strip()[-2000:]}


# ---------------------------------------------------------------- sugestões do pedido

ESQUEMA_SUGESTOES = {
    "type": "object",
    "required": ["resumo", "campos", "modo_voz", "personagens"],
    "properties": {
        "resumo": {"type": "string", "description": "2 ou 3 frases: como você leu o pedido e a linha geral das escolhas"},
        "campos": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["campo", "valor", "motivo"],
                "properties": {
                    "campo": {"type": "string", "description": "chave do pedido, como tom ou visual.imagens_da_internet"},
                    "valor": {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"},
                                        {"type": "array", "items": {"type": "string"}}]},
                    "motivo": {"type": "string", "description": "uma frase"},
                },
            },
        },
        "modo_voz": {
            "type": "object",
            "required": ["valor", "motivo"],
            "properties": {
                "valor": {"type": "string", "enum": ["narrador", "narrador-e-personagens", "so-personagens", "sem-voz"]},
                "motivo": {"type": "string"},
            },
        },
        "personagens": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["nome", "voz", "motivo"],
                "properties": {
                    "nome": {"type": "string"}, "voz": {"type": "string"}, "estilo": {"type": "string"},
                    "descricao": {"type": "string"}, "biblioteca": {"type": "string"}, "motivo": {"type": "string"},
                },
            },
        },
        "alternativas_estilo": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "avisos": {"type": "array", "items": {"type": "string"}},
    },
}

MODOS_VOZ = {
    "narrador": "só o narrador fala (personagens = [])",
    "narrador-e-personagens": "narrador e personagens com voz própria (dublagem)",
    "so-personagens": "só os personagens falam, sem narrador",
    "sem-voz": "sem voz: a história vai na legenda e na música",
}
RESTRICAO_SO_PERSONAGENS = ("sem narrador: todo o texto falado é fala de personagem, com [FALA: Nome]; "
                            "a cena se explica pelas falas e pelas imagens")
# Campos do pedido que a interface decide (ou que só o usuário sabe) e o orquestrador do pedido nunca sugere. A
# animação é paga por segundo: só o usuário liga. Até onde ir é obrigatório na configuração (vem marcado em video).
FORA_DAS_SUGESTOES = {"titulo", "slug", "musica.nome", "musica.arquivo", "musica.ativa", "musica.inicio", "contexto",
                      "imagens_referencia", "trends.links", "trends.ativa", "visual.animacao", "producao.ate"}
# Num roteiro importado, a duração é a das palavras dele na velocidade da narração: o pedido.py calcula, e nem o
# usuário (na interface) nem o orquestrador a escolhem.
CALCULADOS_NO_IMPORTADO = {"duracao_alvo"}


def fora_das_sugestoes(slug: str) -> set[str]:
    return FORA_DAS_SUGESTOES | (CALCULADOS_NO_IMPORTADO if importado(slug) else set())
