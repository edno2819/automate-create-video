"""As peças da etapa Edição de um projeto, como a tela, os fluxos e os scripts as veem.

Depois da produção, o vídeo vira peças separadas que o usuário combina como quiser:

    projetos/<slug>/
      10-video/base/video.mp4     o vídeo sem som e sem legendas (montar_video.py)
      09-narracao/narracao.wav    a voz original, com as legendas de 10-video/ (legendas.srt e legendas.ass)
      12-dublagens/<idioma>/      uma dublagem (dublar.py): dublagem.wav com a duração do vídeo, legendas.srt e .ass
                                  na língua dela, o título e a descrição traduzidos (publicacao.json e .md) e o
                                  caminho até lá (segmentos, tradução, estado)
      13-exportacoes/<nome>.*     cada combinação exportada (combinar.py): .mp4, .mp3 (a faixa de áudio que o YouTube
                                  aceita para outra língua), .srt e a receita em .json

A faixa de voz "original" é a narração do projeto; as outras têm o código da língua (nucleo/idiomas.py). Uma
dublagem guarda o hash das entradas de que depende (a narração alinhada e o roteiro final): se elas mudam, a
dublagem fica desatualizada, porque as frases e os tempos dela são os da narração antiga.

Só biblioteca padrão e nucleo.idiomas: os scripts de ferramentas/producao/ também importam este módulo.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

from nucleo import idiomas

VIDEO_BASE = "10-video/base/video.mp4"
DUBLAGENS = "12-dublagens"
EXPORTACOES = "13-exportacoes"
MUSICAS = "biblioteca/musicas/"
EXTENSOES_AUDIO = frozenset({".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"})
ORIGINAL = "original"
NENHUMA = "nenhuma"
SAIDAS = {"video": "mp4", "audio": "mp3"}
# o que a dublagem usa dos arquivos do projeto, e a frase que a tela mostra quando um deles muda
ENTRADAS_DUBLAGEM = {"09-narracao/tempos.json": "a narração original mudou",
                     "06-roteiro-final.md": "o roteiro final mudou"}
NOME_EXPORTACAO = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TAMANHO_NOME = 60


def ler_json(caminho: Path):
    try:
        return json.loads(Path(caminho).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def hash_de(caminho: Path) -> str | None:
    try:
        return hashlib.sha1(Path(caminho).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def entradas(pasta: Path) -> dict[str, str | None]:
    """O hash de cada arquivo de que a dublagem depende, como está agora."""
    return {nome: hash_de(pasta / nome) for nome in ENTRADAS_DUBLAGEM}


def idioma_original(brief: dict | None) -> str:
    return str((brief or {}).get("idioma") or idiomas.ORIGINAL_PADRAO)


def _rel(pasta: Path, caminho: str) -> str | None:
    return caminho if (pasta / caminho).is_file() else None


# ---------------------------------------------------------------- faixas de voz


def faixa_original(pasta: Path, brief: dict | None) -> dict:
    """A narração do projeto como faixa de voz, com as legendas que a montagem gravou."""
    tempos = ler_json(pasta / "09-narracao" / "tempos.json") or {}
    blocos = ler_json(pasta / "09-narracao" / "blocos.json") or {}
    codigo = idioma_original(brief)
    audio = _rel(pasta, "09-narracao/narracao.wav")
    return {"id": ORIGINAL, "idioma": codigo, "nome": f"{idiomas.nome(codigo)} (original)", "original": True,
            "audio": audio, "srt": _rel(pasta, "10-video/legendas.srt"), "ass": _rel(pasta, "10-video/legendas.ass"),
            "duracao_s": tempos.get("duracao_audio_s") or blocos.get("duracao_s"), "voz": tempos.get("voz") or blocos.get("voz"),
            "fornecedor": blocos.get("fornecedor"), "status": "pronta" if audio else "sem narração", "desatualizada": None}


def dublagem(pasta: Path, codigo: str) -> dict:
    """O estado de uma dublagem: pronta (a faixa existe), traduzida (falta a voz) ou incompleta; e se ficou para trás."""
    base = f"{DUBLAGENS}/{codigo}/"
    estado = ler_json(pasta / base / "dublagem.json") or {}
    segmentos = ler_json(pasta / base / "segmentos.json") or {}
    traducao = ler_json(pasta / base / "traducao.json") or {}
    audio = _rel(pasta, base + "dublagem.wav")
    feitas = estado.get("entradas") or segmentos.get("entradas") or {}
    atuais = entradas(pasta)
    mudou = [motivo for nome, motivo in ENTRADAS_DUBLAGEM.items() if nome in feitas and feitas[nome] != atuais.get(nome)]
    status = "pronta" if audio and estado else "traduzida" if traducao.get("trechos") else "incompleta"
    return {"id": codigo, "idioma": codigo, "nome": idiomas.nome(codigo), "original": False,
            "publicacao": publicacao(pasta, codigo),
            "audio": audio, "srt": _rel(pasta, base + "legendas.srt"), "ass": _rel(pasta, base + "legendas.ass"),
            "duracao_s": estado.get("duracao_s"), "fornecedor": estado.get("fornecedor"), "modelo": estado.get("modelo"),
            "voz": estado.get("voz"), "personagens": estado.get("personagens") or {},
            "trechos": len(segmentos.get("segmentos") or []), "acelerados": len(estado.get("encurtar") or []),
            "reprovados": len(estado.get("reprovados") or []), "avisos": estado.get("avisos") or [],
            "gerada_em": estado.get("gerada_em"), "status": status,
            "desatualizada": "; ".join(mudou) + " depois desta dublagem" if mudou else None}


def publicacao(pasta: Path, codigo: str) -> dict | None:
    """O título, as descrições e as tags na língua da dublagem (publicacao.json, gravado pelo fluxo da dublagem), e se
    os textos de publicação do vídeo original mudaram depois da tradução."""
    base = f"{DUBLAGENS}/{codigo}/"
    doc = ler_json(pasta / base / "publicacao.json")
    if not isinstance(doc, dict) or not doc.get("titulo"):
        return None
    agora = hash_de(pasta / "11-publicacao.json")
    motivo = None
    if doc.get("fonte_hash") != agora:
        motivo = ("os textos de publicação do vídeo original mudaram" if doc.get("fonte")
                  else "o vídeo original ganhou textos de publicação depois")
    return {**{k: doc.get(k) for k in ("titulo", "titulos_alternativos", "plataformas", "tags_youtube", "fonte", "notas",
                                       "avisos", "gerada_em")},
            "md": _rel(pasta, base + "publicacao.md"), "desatualizada": motivo}


def codigos_dublados(pasta: Path) -> list[str]:
    d = pasta / DUBLAGENS
    return sorted(p.name for p in d.iterdir() if p.is_dir() and idiomas.achar(p.name)) if d.is_dir() else []


def faixas(pasta: Path, brief: dict | None) -> list[dict]:
    """As faixas de voz do projeto: a original primeiro, depois as dublagens."""
    return [faixa_original(pasta, brief)] + [dublagem(pasta, c) for c in codigos_dublados(pasta)]


def faixa(pasta: Path, brief: dict | None, id_: str) -> dict | None:
    if id_ == ORIGINAL:
        return faixa_original(pasta, brief)
    achado = idiomas.achar(id_)
    if achado is None or not (pasta / DUBLAGENS / achado.codigo).is_dir():
        return None
    return dublagem(pasta, achado.codigo)


# ---------------------------------------------------------------- exportações


def exportacoes(pasta: Path) -> list[dict]:
    """As combinações exportadas, da mais nova para a mais velha, com os arquivos que existem de cada uma."""
    d = pasta / EXPORTACOES
    if not d.is_dir():
        return []
    saida = []
    for receita in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        doc = ler_json(receita) or {}
        nome = receita.stem
        arquivos = {tipo: f"{EXPORTACOES}/{nome}.{ext}" for tipo, ext in (*SAIDAS.items(), ("srt", "srt"))
                    if (d / f"{nome}.{ext}").is_file()}
        saida.append({"nome": nome, "receita": doc.get("receita") or {}, "arquivos": arquivos,
                      "duracao_s": doc.get("duracao_s"), "criada_em": doc.get("criada_em")})
    return saida


def nome_de_exportacao(texto: str) -> str:
    """Um nome de arquivo seguro para a exportação: minúsculas, números e hífens."""
    sem_acento = "".join(c for c in unicodedata.normalize("NFD", str(texto or "").lower()) if unicodedata.category(c) != "Mn")
    return "-".join(re.findall(r"[a-z0-9]+", sem_acento))[:TAMANHO_NOME].strip("-")


# ---------------------------------------------------------------- música


def _palavras(texto: str) -> set[str]:
    sem_acento = "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")
    return set(re.findall(r"[a-z0-9]+", sem_acento))


def musica_do_pedido(brief: dict | None, musicas: list[str]) -> dict:
    """A música que o pedido escolheu, como arquivo da biblioteca (a mesma busca de ferramentas/brief.py:
    musica.arquivo, senão o arquivo com todas as palavras de musica.nome), com o volume e o início."""
    m = (brief or {}).get("musica") or {}
    escolhida = None
    arquivo = str(m.get("arquivo") or "")
    if arquivo.startswith(MUSICAS) and arquivo[len(MUSICAS):] in musicas:
        escolhida = arquivo[len(MUSICAS):]
    elif not arquivo and m.get("nome"):
        nome = str(m["nome"])
        procurado = _palavras(Path(nome).stem if Path(nome).suffix.lower() in EXTENSOES_AUDIO else nome)
        candidatas = [n for n in musicas if procurado and procurado <= _palavras(Path(n).stem)]
        escolhida = min(candidatas, key=lambda n: len(Path(n).stem)) if candidatas else None
    return {"ativa": bool(m.get("ativa")) and escolhida is not None, "arquivo": MUSICAS + escolhida if escolhida else None,
            "volume": m.get("volume") if isinstance(m.get("volume"), (int, float)) else 20,
            "inicio": str(m.get("inicio") or "0:00")}
