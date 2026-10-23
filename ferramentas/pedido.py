#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["ruamel.yaml>=0.18"]
# ///
"""Pedido de vídeo: o arquivo YAML em que o usuário pede um vídeo (tema, estilo, voz, legendas, música...).

Só o tema é obrigatório (ou, no lugar dele, o título ou a descrição, de onde o orquestrador tira o tema).
A primeira fase do pipeline completa o resto e o usuário aprova antes de começar:

  validar    confere um pedido; no do usuário, lista o que o orquestrador precisa decidir e os padrões
  completar  grava projetos/<slug>/00-pedido.yaml com tudo preenchido e uma cópia do original em
             00-pedido-original.yaml. O que o usuário escreveu fica intocado; as decisões do orquestrador vêm
             de --definir, as respostas que o usuário deu na conversa de --usuario e o resto do padrão.
             Imprime o resumo, separando o que o usuário definiu, o que o sistema decidiu e os padrões
  brief      depois da aprovação, gera 00-brief.json, o arquivo que os agentes e os scripts leem
  modelo     grava o modelo em branco (pedidos/modelo.yaml)

Os campos, os comentários do modelo e os padrões estão em CAMPOS: acrescentar um campo é acrescentar uma
linha ali (e ensinar quem vai usá-lo a ler o brief). A leitura aceita chaves com acento ("descrição") e
alguns apelidos (tempo, ctx, estilo, voz), e durações como "30s", "1min30" ou "01:30".

Uso (o uv instala o ruamel.yaml na primeira vez):
    uv run --script ferramentas/pedido.py validar pedidos/<nome>.yaml 2>&1 | cat
    uv run --script ferramentas/pedido.py completar pedidos/<nome>.yaml --definir slug=<slug> --definir 'tom=...' 2>&1 | cat
    uv run --script ferramentas/pedido.py validar projetos/<slug>/00-pedido.yaml 2>&1 | cat
    uv run --script ferramentas/pedido.py brief projetos/<slug> 2>&1 | cat
    uv run --script ferramentas/pedido.py modelo pedidos/modelo.yaml 2>&1 | cat
"""

from __future__ import annotations

import argparse
import copy
import functools
import importlib.util
import json
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "ferramentas"))
from brief import achar_musica, sha1_arquivo  # noqa: E402

ORIGINAL = "00-pedido-original.yaml"
COMPLETO = "00-pedido.yaml"
BRIEF = "00-brief.json"
REFERENCIA_PADRAO = "https://www.youtube.com/watch?v=DYAYD0UYvvE"  # Dor Pintada, o canal de referência
CORES = {"amarelo": "#FFD400", "verde": "#22C55E", "roxo": "#8B5CF6", "vermelho": "#EF4444", "azul": "#3B82F6",
         "rosa": "#EC4899", "laranja": "#F97316", "ciano": "#06B6D4", "branco": "#FFFFFF", "preto": "#000000"}
SIM = {"sim", "s", "yes", "y", "true", "verdadeiro", "on"}
NAO = {"nao", "n", "no", "false", "falso", "off"}
LARGURA = 104
APELIDOS = {"tempo": "duracao_alvo", "duracao": "duracao_alvo", "ctx": "contexto", "estilo": "estilo_artistico",
            "estilo_de_desenho": "estilo_artistico", "estilo_visual": "estilo_artistico", "voz": "narracao.voz",
            "legenda": "legendas", "proporcao": "visual.proporcao", "imagens_da_internet": "visual.imagens_da_internet"}
EXTENSOES_TEXTO = (".md", ".txt", ".markdown")
SUBCAMPOS_PERSONAGEM = ("nome", "voz", "estilo", "descricao", "cor", "biblioteca")
PALETA_PERSONAGENS = ["#EC4899", "#22C55E", "#3B82F6", "#F97316", "#8B5CF6", "#06B6D4", "#EF4444", "#FFD400"]


class PorFormato:
    """Padrão que depende do formato do vídeo."""

    def __init__(self, longo, short):
        self.longo, self.short = longo, short

    def __call__(self, valores: dict):
        return self.short if valores.get("formato") == "short" else self.longo


def voz_do_canal(_valores: dict) -> str | None:
    canal = RAIZ / "biblioteca" / "canal.json"
    return json.loads(canal.read_text(encoding="utf-8")).get("narracao", {}).get("voz") if canal.exists() else None


def musica_preenchida(valores: dict) -> bool:
    return bool(valores.get("musica.nome") or valores.get("musica.arquivo"))


@dataclass
class Campo:
    chave: str                 # "visual.proporcao": grupo e nome
    tipo: str                  # texto, slug, opcao, booleano, inteiro, numero, tempo, cor, lista
    ajuda: str                 # comentário do campo no modelo
    quem: str = "padrao"       # obrigatorio | decisao (o orquestrador decide) | padrao (o script completa) | opcional
    padrao: Any = None         # valor, ou função(valores) -> valor
    opcoes: tuple = ()
    faixa: tuple | None = None
    vazio: str = ""            # o que acontece quando um campo opcional fica vazio
    secao: tuple = ()          # (título, linhas de explicação...) da seção que começa neste campo


CAMPOS = [
    Campo("tema", "texto",
          "Assunto do vídeo, numa frase. É o único campo obrigatório; se você preencher só o título ou a\n"
          "descrição, o sistema tira o tema deles.", quem="obrigatorio", secao=("O vídeo",)),
    Campo("descricao", "texto", "O que o vídeo deve contar: o recorte, os pontos que não podem faltar e o que evitar.",
          quem="decisao"),
    Campo("contexto", "texto",
          "Material de apoio para os agentes: a trend, o universo, os personagens, referências de estilo. Pode ser\n"
          "longo (escreva \"contexto: |\" e o texto nas linhas de baixo, recuado) ou o caminho de um arquivo .md.",
          quem="opcional", vazio="nenhum"),
    Campo("slug", "slug", "Nome da pasta do projeto em projetos/: curto, minúsculas, sem acentos, com hífens.",
          quem="decisao"),
    Campo("titulo", "texto", "Título do vídeo. Vazio: o estrategista propõe títulos a partir dos dados de trends.",
          quem="opcional", vazio="o estrategista propõe"),
    Campo("formato", "opcao", "longo (8 a 15 min, horizontal) | short (30 s a 3 min, vertical)", quem="decisao",
          opcoes=("longo", "short")),
    Campo("duracao_alvo", "tempo", 'Duração desejada, "mm:ss". Padrão: "12:00" no longo, "00:45" no short.',
          padrao=PorFormato("12:00", "00:45")),
    Campo("tom", "texto", 'Tom do texto e da voz. Ex.: "sombrio e sóbrio, sem julgamento explícito".', quem="decisao"),
    Campo("natureza", "opcao", "factual | inspirado-em-fatos (contexto real, personagens compostos) | ficcional",
          quem="decisao", opcoes=("factual", "inspirado-em-fatos", "ficcional")),
    Campo("publico", "texto", "Para quem é o vídeo.", quem="decisao"),
    Campo("nicho", "texto", "Nicho em que o vídeo compete (orienta a pesquisa de trends).", quem="decisao"),
    Campo("idioma", "texto", "Padrão: pt-BR.", padrao="pt-BR"),

    Campo("estilo_artistico", "texto",
          "Estilo de desenho das imagens geradas por IA: técnica, traço, luz e paleta. Ex.: \"pintura digital\n"
          "sombria, pinceladas visíveis, luz de cinema, paleta dessaturada com acentos vermelhos\".",
          quem="decisao", secao=("Imagens",)),
    Campo("imagens_referencia", "lista",
          "Imagens de referência (caminhos): estilo, personagens, lugares. O diretor visual olha antes de\n"
          "escrever a bíblia visual; elas não entram no vídeo.", padrao=[]),
    Campo("visual.proporcao", "opcao", "16:9 | 9:16 | 1:1 | 4:5. Padrão: 16:9 no longo, 9:16 no short.",
          opcoes=("16:9", "9:16", "1:1", "4:5"), padrao=PorFormato("16:9", "9:16")),
    Campo("visual.segundos_por_imagem", "numero",
          "Ritmo de troca das imagens, em segundos (2,5 a 12). Padrão: 7 no longo, 3 no short.",
          faixa=(2.5, 12), padrao=PorFormato(7, 3)),
    Campo("visual.numero_de_imagens", "inteiro", "Número exato de imagens no vídeo. Vazio: livre, pelo ritmo acima.",
          quem="opcional", faixa=(1, 500), vazio="livre"),
    Campo("visual.imagens_da_internet", "booleano",
          "sim: além das imagens de IA, pode usar imagens reais de bancos livres (Wikimedia Commons, Openverse),\n"
          "como documentos, obras de época, mapas, lugares e objetos reais. Só domínio público e Creative Commons\n"
          "que permitem uso comercial; os créditos saem em 09-imagens/creditos.md. não: só imagens de IA.",
          quem="decisao"),
    Campo("visual.qualidade", "opcao",
          "Modelo das imagens de IA: rascunho (barato) | padrao | maxima (bem mais caro). Padrão: padrao.",
          opcoes=("rascunho", "padrao", "maxima"), padrao="padrao"),

    Campo("narracao.ativa", "booleano",
          'sim | não. "não": vídeo sem voz, a história vai só na legenda (e na música). Padrão: sim.',
          padrao=True, secao=("Narração",)),
    Campo("narracao.fornecedor", "opcao",
          "gemini (Gemini TTS, vozes naturais, pago) | openai (TTS da OpenAI, segue instruções de estilo, pago) |\n"
          "elevenlabs (vozes da conta na ElevenLabs, pago por caractere) | espeak (voz robótica, local e grátis: para\n"
          "testar falas, ritmo e montagem antes de pagar). Chaves em Configurações > Chaves de API. Padrão: gemini.",
          opcoes=("gemini", "openai", "elevenlabs", "espeak"), padrao="gemini"),
    Campo("narracao.voz", "texto",
          "Voz do narrador no fornecedor escolhido (lista: python3 skills/produzir-video/scripts/narrar.py\n"
          "--listar-vozes --fornecedor <nome>). Uma voz de outro fornecedor vira uma dele do mesmo gênero.\n"
          "Padrão: a voz do canal, em biblioteca/canal.json.", padrao=voz_do_canal),
    Campo("narracao.estilo", "texto",
          'Como o narrador fala. Ex.: "ritmo rápido, tom de quem revela um segredo". Vazio: vem do tom.',
          quem="opcional", vazio="vem do tom"),
    Campo("narracao.palavras_por_minuto", "inteiro",
          "Velocidade do vídeo, em palavras por minuto. Define quantas palavras o roteiro tem para a duração pedida\n"
          "e o ritmo da voz: a narração é acelerada ou desacelerada até chegar nela, sem mudar o tom. 130: calmo |\n"
          "150: padrão (o canal de referência fala ~153) | 170 a 190: rápido, de short. Sem narração, é o ritmo das\n"
          "legendas. Padrão: 150.", faixa=(100, 220), padrao=150),

    Campo("personagens", "personagens",
          "Quem fala com voz própria, além do narrador. No roteiro, a fala de cada um é a linha que começa com\n"
          "[FALA: Nome]; o resto é do narrador. Vazio: o sistema decide (só o narrador, ou os personagens que a\n"
          "história pede). Um item por personagem; só o nome é obrigatório:\n"
          '  - nome: "Moranguete"\n'
          '    voz: "Leda"          vazio: o sistema escolhe pela personalidade e pelo gênero\n'
          '    estilo: "doce no caixa, vira choro dramático quando é desmascarada"    como fala\n'
          '    descricao: "um morango de olhos grandes e brilhantes"    quem é e como é (vai para as imagens)\n'
          '    cor: "rosa"          cor do destaque da legenda nas falas dele. Vazio: automática\n'
          '    biblioteca: "moranguete"    personagem salvo em biblioteca/personagens/ (aparência, jeito e imagem)\n'
          'Atalho, só com as vozes: personagens: {Moranguete: Leda, Abacate: Puck}',
          quem="decisao", secao=("Personagens",)),

    Campo("legendas.ativas", "booleano", "Legenda queimada no vídeo, no tempo da voz. Padrão: sim.", padrao=True,
          secao=("Legendas",)),
    Campo("legendas.destacar_palavra", "booleano", "Fundo colorido atrás da palavra que está sendo dita. Padrão: sim.",
          padrao=True),
    Campo("legendas.cor_destaque", "cor",
          'Cor do fundo da palavra: "#RRGGBB" (entre aspas) ou amarelo, verde, roxo, vermelho, azul, rosa, laranja,\n'
          'ciano. Padrão: amarelo ("#FFD400"). A cor da letra em cima dele (preta ou branca) é automática.',
          padrao="#FFD400"),
    Campo("legendas.cor_texto", "cor", 'Cor das letras. Padrão: branco ("#FFFFFF").', padrao="#FFFFFF"),
    Campo("legendas.maiusculas", "booleano", "Legenda em caixa alta. Padrão: não.", padrao=False),
    Campo("legendas.palavras_por_legenda", "inteiro", "Máximo de palavras na tela. Vazio: 4 no vertical, 7 no horizontal.",
          quem="opcional", faixa=(1, 12), vazio="4 no vertical, 7 no horizontal"),
    Campo("legendas.posicao", "opcao", "baixo | centro | alto. Padrão: baixo.", opcoes=("baixo", "centro", "alto"),
          padrao="baixo"),

    Campo("musica.ativa", "booleano", "Música de fundo. Padrão: sim se nome ou arquivo estiver preenchido.",
          padrao=musica_preenchida,
          secao=("Música de fundo",
                 "Música famosa costuma gerar reivindicação de Content ID: o dono pode ficar com a monetização ou",
                 "bloquear o vídeo. Para monetizar, use música licenciada ou da Biblioteca de Áudio do YouTube.")),
    Campo("musica.nome", "texto",
          'Música e artista. Ex.: "Stranger - Lory". O sistema não baixa música: ele procura em biblioteca/musicas/\n'
          "um arquivo com essas palavras no nome (stranger-lory.mp3).", quem="opcional"),
    Campo("musica.arquivo", "texto", "Ou o caminho do arquivo de áudio (mp3, m4a, wav, ogg, flac).", quem="opcional"),
    Campo("musica.inicio", "tempo", 'Ponto da música em que o vídeo começa, "mm:ss" (ex.: o refrão). Padrão: "00:00".',
          padrao="00:00"),
    Campo("musica.volume", "inteiro",
          "Volume da música por baixo da narração, em % (1 a 100); ela abaixa sozinha quando a voz fala.\n"
          "Sem narração, a música toca no volume normal. Padrão: 20.", faixa=(1, 100), padrao=20),

    Campo("referencias", "lista", "Links de vídeos cuja estrutura serve de modelo. Padrão: o vídeo do Dor Pintada.",
          padrao=[REFERENCIA_PADRAO], secao=("Pesquisa",)),
    Campo("canais_referencia", "lista", 'Canais de referência, como "@DorPintada". Padrão: ["@DorPintada"].',
          padrao=["@DorPintada"]),
    Campo("concorrentes", "lista", "Canais concorrentes, para a pesquisa de trends.", padrao=[]),
    Campo("restricoes", "lista", "Regras extras para o roteiro e as imagens.", padrao=[]),
    Campo("trends.ativa", "booleano",
          "Pesquisar o que está em alta no YouTube antes dos ângulos. não: os ângulos saem sem dados de trends\n"
          "(viram apostas). Padrão: sim.", padrao=True),
    Campo("trends.links", "lista",
          "Links que o pesquisador de trends olha primeiro: vídeos, canais ou buscas do YouTube que você já viu\n"
          "performando.", padrao=[]),

    Campo("producao.ate", "opcao",
          "Até onde ir: roteiro | visual (cenas e prompts) | video (imagens, voz e montagem). Padrão: video.",
          opcoes=("roteiro", "visual", "video"), padrao="video", secao=("Produção",)),
    Campo("producao.cortes", "booleano", "Gerar shorts a partir do vídeo longo. Padrão: sim no longo.",
          padrao=PorFormato(True, False)),
]
POR_CHAVE = {c.chave: c for c in CAMPOS}
GRUPOS = {c.chave.split(".")[0] for c in CAMPOS if "." in c.chave}
ATIVA = {"narracao": "narracao.ativa", "legendas": "legendas.ativas", "musica": "musica.ativa", "trends": "trends.ativa"}


# ---------------------------------------------------------------- leitura e normalização


def sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def para_bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = sem_acento(v.strip().lower())
        return True if s in SIM else False if s in NAO else None
    return None


def para_segundos(v) -> int | None:
    """30, "30s", "45 seg", "1min", "1min30", "1m30s", "2 minutos", "01:30" -> segundos."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(round(v))
    s = sem_acento(str(v).strip().lower())
    if m := re.fullmatch(r"(\d{1,3}):([0-5]\d)", s):
        return int(m[1]) * 60 + int(m[2])
    m = re.fullmatch(r"(?:(\d+)\s*(?:m|min|mins|minutos?)\s*)?(?:(\d+)\s*(?:s|seg|segs|segundos?)?)?", s)
    return int(m[1] or 0) * 60 + int(m[2] or 0) if m and any(m.groups()) else None


def normalizar(c: Campo, v) -> tuple[Any, str | None]:
    """Converte o valor escrito pelo usuário para o tipo do campo. Devolve (valor, erro)."""
    if c.tipo == "personagens":
        return normalizar_personagens(v)
    if v is None or (isinstance(v, str) and not v.strip()):
        return None, None
    if c.tipo == "lista":
        v = [v] if isinstance(v, (str, int, float)) else v
        if not isinstance(v, list) or any(isinstance(x, (dict, list)) for x in v):
            return None, "esperava uma lista de textos"
        return [str(x).strip() for x in v if x is not None and str(x).strip()], None
    if isinstance(v, (dict, list)):
        return None, "esperava um valor simples, não uma lista ou um grupo"
    if c.tipo == "booleano":
        b = para_bool(v)
        return (b, None) if b is not None else (None, f"use sim ou não (recebi {v!r})")
    if c.tipo in ("inteiro", "numero"):
        try:
            n = float(str(v).replace(",", ".")) if not isinstance(v, bool) else None
        except ValueError:
            n = None
        if n is None or (c.tipo == "inteiro" and n != int(n)):
            return None, f"esperava um número{' inteiro' if c.tipo == 'inteiro' else ''} (recebi {v!r})"
        n = int(n) if n == int(n) else n
        if c.faixa and not c.faixa[0] <= n <= c.faixa[1]:
            return None, f"fora da faixa de {c.faixa[0]:g} a {c.faixa[1]:g} (recebi {n:g})"
        return n, None
    if c.tipo == "tempo":
        total = para_segundos(v)
        return (f"{total // 60:02d}:{total % 60:02d}", None) if total is not None else (
            None, f'use "mm:ss", "30s" ou "1min30" (recebi {v!r})')
    if c.tipo == "cor":
        s = sem_acento(str(v).strip().lower())
        if s in CORES:
            return CORES[s], None
        m = re.fullmatch(r"#?([0-9a-f]{6})", s)
        return ("#" + m.group(1).upper(), None) if m else (None, f'use "#RRGGBB" ou {", ".join(CORES)} (recebi {v!r})')
    s = str(v).strip()
    if c.tipo == "opcao":
        chave = sem_acento(s.lower()).replace(" ", "-")
        if c.chave == "visual.proporcao":
            chave = chave.replace("x", ":")
        return (chave, None) if chave in c.opcoes else (None, f"use {' | '.join(c.opcoes)} (recebi {v!r})")
    if c.tipo == "slug" and not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", s):
        return None, f"use minúsculas sem acento, números e hífens, como higiene-idade-media (recebi {v!r})"
    return s, None


def normalizar_personagens(v) -> tuple[list | None, str | None]:
    """Lista de personagens (nome, voz, estilo, descricao, cor). Aceita também o mapa nome -> voz ou
    nome -> {voz, estilo...}. Lista vazia é uma decisão válida: só o narrador."""
    if v is None:
        return None, None
    if isinstance(v, dict):
        itens = [{"nome": nome, **(d if isinstance(d, dict) else {} if d is None else {"voz": d})} for nome, d in v.items()]
    elif isinstance(v, list):
        itens = [x if isinstance(x, dict) else {"nome": x} for x in v]
    else:
        return None, "esperava uma lista de personagens (veja pedidos/modelo.yaml)"
    saida, vistos = [], set()
    for item in itens:
        item = {nome_de_chave(k): x for k, x in item.items()}
        nome = str(item.get("nome") or "").strip()
        if not nome:
            return None, "personagem sem nome"
        if desconhecidos := set(item) - set(SUBCAMPOS_PERSONAGEM):
            return None, f"{nome}: campo desconhecido {', '.join(sorted(desconhecidos))} (use {', '.join(SUBCAMPOS_PERSONAGEM)})"
        if chave_nome(nome) in ("narrador", "narradora"):
            return None, "o narrador não entra em personagens: a voz dele é narracao.voz"
        if chave_nome(nome) in vistos:
            return None, f"{nome} aparece duas vezes"
        vistos.add(chave_nome(nome))
        p = {"nome": nome}
        for sub in ("voz", "estilo", "descricao"):
            x = item.get(sub)
            p[sub] = str(x).strip() if x is not None and str(x).strip() else None
        p["voz"] = voz_canonica(p["voz"])
        p["cor"], erro = normalizar(POR_CHAVE["legendas.cor_destaque"], item.get("cor"))
        if erro:
            return None, f"{nome}, cor: {erro}"
        if slug := str(item.get("biblioteca") or "").strip():  # só aparece no arquivo quando preenchido
            if not (RAIZ / "biblioteca" / "personagens" / slug / "personagem.json").exists():
                return None, f"{nome}, biblioteca: não existe biblioteca/personagens/{slug}/"
            p["biblioteca"] = slug
        saida.append(p)
    return saida, None


def chave_nome(nome: str) -> str:
    return sem_acento(nome).strip().lower()


def voz_canonica(voz: str | None) -> str | None:
    """ "leda" -> "Leda". Voz fora da lista fica como veio (a coerência avisa)."""
    return {k.lower(): k for k in vozes_conhecidas()}.get((voz or "").lower(), voz)


@dataclass
class Pedido:
    valores: dict    # chave pontuada -> valor normalizado (None = vazio)
    extras: dict     # campos que não estão no modelo (seguem para o brief)
    erros: list
    avisos: list


def nome_de_chave(k) -> str:
    """ "Descrição" -> "descricao", "estilo de desenho" -> "estilo_de_desenho"."""
    return re.sub(r"[\s-]+", "_", sem_acento(str(k)).strip().lower())


def carregar(caminho: Path) -> Pedido:
    texto = caminho.read_text(encoding="utf-8")
    erros, avisos, plano, extras = [], [], {}, {}
    try:
        dados = YAML(typ="safe", pure=True).load(texto)
    except YAMLError as e:
        return Pedido({c.chave: None for c in CAMPOS}, {}, [f"YAML inválido: {' '.join(str(e).split())}"], [])
    if dados is None:
        dados = {}
    if not isinstance(dados, dict):
        return Pedido({c.chave: None for c in CAMPOS}, {}, ["o pedido precisa ser uma lista de campos (campo: valor)"], [])
    for k, v in dados.items():
        k = APELIDOS.get(nome_de_chave(k), nome_de_chave(k))
        if "." in k:  # apelido de um campo de grupo, como "voz"
            plano[k] = v
        elif k in GRUPOS:
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    chave = f"{k}.{nome_de_chave(k2)}"
                    (plano if chave in POR_CHAVE else extras)[chave] = v2
            elif v is None:
                continue
            elif k in ATIVA and para_bool(v) is not None:
                plano[ATIVA[k]] = v  # atalho: "musica: não"
            elif k == "musica" and isinstance(v, str):
                plano["musica.nome"] = v  # atalho: musica: "Stranger - Lory"
            else:
                erros.append(f"{k}: esperava um grupo de campos (veja pedidos/modelo.yaml)")
        else:
            (plano if k in POR_CHAVE else extras)[k] = v
    for k in extras:
        avisos.append(f"campo fora do modelo: {k} (segue para o brief como está)")
    valores = {}
    for c in CAMPOS:
        valores[c.chave], erro = normalizar(c, plano.get(c.chave))
        if erro:
            erros.append(f"{c.chave}: {erro}")
    valores["narracao.voz"] = voz_canonica(valores["narracao.voz"])
    if valores["tema"] is None and not (valores["titulo"] or valores["descricao"]):
        erros.append("tema: é o único campo obrigatório e está vazio (vale também preencher o título ou a descrição)")
    if re.search(r"(?m)^[ \t]*cor_\w+[ \t]*:[ \t]*#", texto):
        avisos.append('cor sem aspas: em YAML o # começa um comentário e o campo fica vazio. Escreva "#FFD400"')
    return Pedido(valores, extras, erros, avisos)


def padrao_de(c: Campo, valores: dict):
    v = c.padrao(valores) if callable(c.padrao) else c.padrao
    return list(v) if isinstance(v, list) else v


def aplicar_padroes(valores: dict) -> list[str]:
    preenchidos = []
    for c in CAMPOS:
        if c.quem == "padrao" and valores.get(c.chave) is None:
            valores[c.chave] = padrao_de(c, valores)
            if valores[c.chave] is not None:
                preenchidos.append(c.chave)
    # Cada personagem sem cor ganha uma da paleta que ninguém usa (nem o destaque do narrador).
    personagens = valores.get("personagens") or []
    usadas = {x["cor"] for x in personagens if x.get("cor")} | {valores.get("legendas.cor_destaque")}
    livres = [cor for cor in PALETA_PERSONAGENS if cor not in usadas] or PALETA_PERSONAGENS
    for i, x in enumerate(y for y in personagens if not y.get("cor")):
        x["cor"] = livres[i % len(livres)]
        preenchidos.append(f"personagens.{x['nome']}.cor")
    return preenchidos


def faltando(valores: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """O que o orquestrador precisa decidir, como (campo, dica), e as perguntas que só o usuário responde."""
    decidir = [(c.chave, dica(c)) for c in CAMPOS if c.quem in ("obrigatorio", "decisao") and valores.get(c.chave) is None]
    for x in valores.get("personagens") or []:
        if not x.get("voz"):
            decidir.append((f"personagens.{x['nome']}.voz", "a voz do personagem, pela personalidade e pelo gênero."))
    perguntar = []
    if valores.get("musica.ativa") and not musica_preenchida(valores):
        perguntar.append("musica.nome: a música de fundo está ativa, mas sem nome nem arquivo. Qual música?")
    return decidir, perguntar


def parece_arquivo(texto: str | None) -> bool:
    return bool(texto) and "\n" not in texto and texto.lower().endswith(EXTENSOES_TEXTO)


def arquivo_de_contexto(texto: str | None, pasta: Path | None = None) -> Path | None:
    """O contexto pode ser o caminho de um arquivo: a partir do diretório atual, do projeto ou da raiz."""
    if not parece_arquivo(texto):
        return None
    for base in (Path.cwd(), pasta, RAIZ):
        if base is not None and (base / texto).is_file():
            return (base / texto).resolve()
    return None


@functools.lru_cache(maxsize=1)
def modulo_narrar():
    spec = importlib.util.spec_from_file_location("narrar", RAIZ / "skills" / "produzir-video" / "scripts" / "narrar.py")
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["narrar"] = modulo  # as dataclasses do módulo procuram por ele aqui
    spec.loader.exec_module(modulo)
    return modulo


def vozes_conhecidas(fornecedor: str | None = None) -> dict[str, str]:
    """As vozes de um fornecedor (as da ElevenLabs vêm da lista guardada da conta), ou de todos, sem fornecedor."""
    narrar = modulo_narrar()
    if fornecedor:
        classe = narrar.FORNECEDORES.get(fornecedor, narrar.GeminiTTS)
        return dict(classe.VOZES)
    return {k: v for classe in narrar.FORNECEDORES.values() for k, v in classe.VOZES.items()}


def lista_de_vozes(fornecedor: str | None = None) -> str:
    narrar = modulo_narrar()
    classe = narrar.FORNECEDORES.get(fornecedor or "gemini", narrar.GeminiTTS)
    grupos: dict[str, list[str]] = {"f": [], "m": []}
    for nome, desc in classe.VOZES.items():
        grupos.setdefault(classe.GENEROS.get(nome, "?"), []).append(f"{nome} ({desc})")
    return f"femininas: {', '.join(grupos['f'])}\n  masculinas: {', '.join(grupos['m'])}"


def coerencia(valores: dict, pasta: Path | None = None) -> list[str]:
    """Avisos de combinações que provavelmente não são o que o usuário quer."""
    v, avisos = valores, []
    seg = lambda t: int(t[:2]) * 60 + int(t[3:]) if t else None  # noqa: E731
    if v.get("formato") == "short" and v.get("visual.proporcao") == "16:9":
        avisos.append("short em 16:9: o YouTube só trata como short o vídeo vertical ou quadrado")
    if v.get("formato") == "longo" and v.get("visual.proporcao") == "9:16":
        avisos.append("vídeo longo na vertical: fora do padrão do YouTube para vídeos longos")
    duracao = seg(v.get("duracao_alvo"))
    if duracao and v.get("formato") == "short" and duracao > 180:
        avisos.append(f"short de {v['duracao_alvo']}: acima de 3 min o YouTube não trata como short")
    if duracao and v.get("formato") == "longo" and duracao < 180:
        avisos.append(f"vídeo longo de {v['duracao_alvo']}: com menos de 3 min, o formato short combina mais")
    if v.get("narracao.ativa") is False and v.get("legendas.ativas") is False:
        avisos.append("sem narração e sem legendas, o texto do roteiro não aparece no vídeo")
    if v.get("narracao.ativa") is False and v.get("musica.ativa") is False:
        avisos.append("sem narração e sem música: o vídeo sai mudo")
    if v.get("producao.cortes") and v.get("formato") == "short":
        avisos.append("producao.cortes só vale para o formato longo; no short é ignorado")
    fornecedor = v.get("narracao.fornecedor") or "gemini"
    conhecidas = vozes_conhecidas(fornecedor)
    lista_de = {"gemini": "do Gemini TTS", "openai": "da OpenAI", "elevenlabs": "da conta na ElevenLabs"}.get(fornecedor)
    if v.get("narracao.voz") and conhecidas and lista_de and v["narracao.voz"] not in conhecidas:
        avisos.append(f"narracao.voz '{v['narracao.voz']}' não está na lista {lista_de} ({', '.join(list(conhecidas)[:8])}...): "
                      "a narração usa uma voz dele do mesmo gênero")
    if v.get("musica.ativa") and musica_preenchida(v):
        musica = {"nome": v.get("musica.nome"), "arquivo": v.get("musica.arquivo")}
        if not achar_musica(musica, pasta):
            onde = (f"o arquivo {v['musica.arquivo']}" if v.get("musica.arquivo") else
                    f"em biblioteca/musicas/ um arquivo com as palavras de '{v['musica.nome']}' no nome")
            avisos.append(f"não achei a música: coloque {onde} antes da montagem")
    personagens = v.get("personagens") or []
    for x in personagens:
        if x.get("voz") and conhecidas and lista_de and x["voz"] not in conhecidas:
            avisos.append(f"personagens.{x['nome']}.voz '{x['voz']}' não está na lista {lista_de}: vai uma voz dele do mesmo gênero")
    vozes = [x["voz"] for x in personagens if x.get("voz")] + ([v["narracao.voz"]] if personagens and v.get("narracao.voz") else [])
    if repetidas := sorted({voz for voz in vozes if vozes.count(voz) > 1}):
        avisos.append(f"voz repetida entre personagens (ou com o narrador): {', '.join(repetidas)}; fica difícil saber quem fala")
    if personagens and v.get("narracao.ativa") is False:
        avisos.append("narração desligada: as vozes dos personagens não são usadas, as falas ficam só na legenda")
    if arquivo_de_contexto(v.get("contexto"), pasta) is None and parece_arquivo(v.get("contexto")):
        avisos.append(f"contexto parece o caminho de um arquivo, mas {v['contexto']} não existe")
    for url in v.get("referencias") or []:
        if not re.search(r"youtube\.com|youtu\.be", url):
            avisos.append(f"referência que não é do YouTube: {url}")
    return avisos


# ---------------------------------------------------------------- escrita


def yaml_valor(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "sim" if v else "não"
    if isinstance(v, (int, float)):
        return f"{v:g}"
    return json.dumps(v, ensure_ascii=False)  # string JSON é string YAML entre aspas; listas e mapas viram fluxo


def linhas_do_valor(recuo: str, nome: str, v, obrigatorio: bool) -> list[str]:
    if isinstance(v, str) and "\n" in v:  # texto de várias linhas: bloco literal, como o usuário escreveria
        return [f"{recuo}{nome}: |-"] + [f"{recuo}  {linha}" if linha else "" for linha in v.split("\n")]
    if isinstance(v, list) and v and isinstance(v[0], dict):  # personagens: um bloco por item
        return [f"{recuo}{nome}:"] + [f"{recuo}  {'- ' if i == 0 else '  '}{k}: {yaml_valor(x)}".rstrip()
                                      for item in v for i, (k, x) in enumerate(item.items())]
    if isinstance(v, list) and v:
        return [f"{recuo}{nome}:"] + [f"{recuo}  - {yaml_valor(x)}" for x in v]
    if v is None and obrigatorio:
        return [f'{recuo}{nome}: ""']
    return [f"{recuo}{nome}: {yaml_valor(v)}".rstrip()]


def renderizar(valores: dict, extras: dict, cabecalho: list[str]) -> str:
    linhas = list(cabecalho)
    grupo_atual = ""

    def fechar_grupo(grupo: str) -> None:
        for chave, valor in extras.items():
            if grupo and chave.startswith(grupo + "."):
                linhas.append("")
                linhas.extend(linhas_do_valor("  ", chave.split(".", 1)[1], valor, False))

    for c in CAMPOS:
        grupo, _, nome = c.chave.rpartition(".")
        mudou = grupo != grupo_atual
        if mudou:
            fechar_grupo(grupo_atual)
        if c.secao:
            titulo, *explicacao = c.secao
            linhas += ["", f"# ── {titulo} " + "─" * (LARGURA - len(titulo) - 6)] + [f"# {l}" for l in explicacao]
        if mudou:
            grupo_atual = grupo
            if grupo:
                linhas += ["", f"{grupo}:"]
        recuo = "  " if grupo else ""
        if not (grupo and linhas[-1] == f"{grupo}:"):
            linhas.append("")
        linhas += [f"{recuo}# {l}" for l in c.ajuda.split("\n")]
        linhas += linhas_do_valor(recuo, nome, valores.get(c.chave), c.quem == "obrigatorio")
    fechar_grupo(grupo_atual)
    soltos = {k: v for k, v in extras.items() if k.split(".")[0] not in GRUPOS}
    if soltos:
        linhas += ["", "# ── Outros campos " + "─" * (LARGURA - 18)]
        for chave, valor in soltos.items():
            linhas += [""] + linhas_do_valor("", chave, valor, False)
    return "\n".join(linhas) + "\n"


def cabecalho_modelo() -> list[str]:
    return [
        "# Pedido de vídeo",
        "#",
        "# Só o tema é obrigatório (ou o título, ou a descrição). Deixe vazio o que quiser que o sistema decida: na",
        "# primeira etapa ele completa este pedido, mostra tudo para você aprovar e só depois começa a pesquisa.",
        "#",
        '# Uso: copie para pedidos/<nome>.yaml, preencha o tema e peça "/novo-video pedidos/<nome>.yaml".',
        "# Escreva textos entre aspas e responda sim ou não nos campos de sim/não.",
        "# Gerado por ferramentas/pedido.py modelo: para mudar campos, comentários ou padrões, edite CAMPOS no",
        "# script e gere o modelo de novo.",
    ]


def cabecalho_completo(valores: dict) -> list[str]:
    return [
        f"# Pedido de vídeo: {valores['tema']}",
        "#",
        f"# Completado em {date.today().isoformat()} a partir de {ORIGINAL} (o pedido como foi escrito).",
        "# Para mudar algo, edite este arquivo e gere o brief de novo:",
        f"#   uv run --script ferramentas/pedido.py brief projetos/{valores['slug']} 2>&1 | cat",
    ]


def aninhar(valores: dict, extras: dict) -> dict:
    saida: dict = {}
    for chave, valor in [*((c.chave, valores.get(c.chave)) for c in CAMPOS), *extras.items()]:
        grupo, _, nome = chave.rpartition(".")
        (saida.setdefault(grupo, {}) if grupo else saida)[nome] = valor
    return saida


# ---------------------------------------------------------------- resumo


def dica(c: Campo) -> str:
    """A primeira frase da ajuda do campo."""
    texto = " ".join(c.ajuda.split())
    return texto.split(". ")[0].rstrip(".") + "."


def exibir(v, c: Campo | None = None) -> str:
    if v is None:
        return c.vazio if c and c.vazio else "—"
    if isinstance(v, bool):
        return "sim" if v else "não"
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return ", ".join(f"{x['nome']} ({x.get('voz') or 'voz a decidir'})" for x in v)
    if isinstance(v, list):
        return ", ".join(v) if v else ("nenhum: só o narrador" if c and c.chave == "personagens" else "(nenhum)")
    s = " ".join(str(v).split())
    return s if len(s) <= 110 else s[:109] + "…"


def origens(valores: dict, do_usuario: dict, definidos: set) -> dict[str, str]:
    saida = {}
    for c in CAMPOS:
        v = valores.get(c.chave)
        if do_usuario.get(c.chave) is not None:
            saida[c.chave] = "você"
        elif c.chave in definidos:
            saida[c.chave] = "sistema"
        elif v is None:
            saida[c.chave] = "vazio"
        elif c.quem == "padrao" and v == padrao_de(c, valores):
            saida[c.chave] = "padrão"
        else:
            saida[c.chave] = "sistema"
    return saida


def imprimir_resumo(valores: dict, org: dict[str, str], extras: dict) -> None:
    rotulos = [("você", "você definiu"), ("sistema", "o sistema decidiu"), ("padrão", "padrões"), ("vazio", "vazios")]
    largura = max(len(c.chave) for c in CAMPOS) + 2
    for origem, rotulo in rotulos:
        itens = [c for c in CAMPOS if org.get(c.chave) == origem]
        if itens:
            print(f"\n{rotulo} ({len(itens)}):")
            for c in itens:
                print(f"  {c.chave:<{largura}}{exibir(valores.get(c.chave), c)}")
    if extras:
        print(f"\nfora do modelo ({len(extras)}):")
        for k, v in extras.items():
            print(f"  {k:<{largura}}{exibir(v)}")


def imprimir_valores(valores: dict, extras: dict) -> None:
    largura = max(len(c.chave) for c in CAMPOS) + 2
    for c in CAMPOS:
        if c.secao:
            print(f"\n{c.secao[0]}:")
        print(f"  {c.chave:<{largura}}{exibir(valores.get(c.chave), c)}")
    for k, v in extras.items():
        print(f"  {k:<{largura}}{exibir(v)}  (fora do modelo)")


def imprimir_personagens(valores: dict, do_usuario: dict, definidos: set, preenchidos: set) -> None:
    """Uma linha por personagem, com a origem de cada valor quando ela é conhecida."""
    personagens = valores.get("personagens") or []
    if not personagens:
        return
    originais = {chave_nome(x["nome"]): x for x in do_usuario.get("personagens") or []}
    print(f"\npersonagens ({len(personagens)}):")
    for x in personagens:
        partes = []
        for sub in SUBCAMPOS_PERSONAGEM[1:]:
            if not x.get(sub):
                continue
            chave = f"personagens.{x['nome']}.{sub}"
            origem = ("você" if (originais.get(chave_nome(x["nome"])) or {}).get(sub) else "sistema" if chave in definidos
                      else "padrão" if chave in preenchidos else "")
            partes.append(f"{sub} {exibir(x[sub])}" + (f" ({origem})" if origem and do_usuario else ""))
        print(f"  {x['nome']}: {' · '.join(partes) or 'só o nome'}")


def definir_personagem(valores: dict, do_usuario: dict, chave: str, texto: str, opcao: str) -> str | None:
    """--definir/--usuario personagens.<Nome>.<voz|estilo|descricao|cor>=valor. Cria o personagem se ele não
    existir. Devolve o erro, ou None."""
    nome, _, sub = chave.split(".", 1)[1].rpartition(".")
    sub, nome = nome_de_chave(sub), nome.strip()
    if sub not in SUBCAMPOS_PERSONAGEM[1:] or not nome:
        return f"use personagens.<Nome>.<{'|'.join(SUBCAMPOS_PERSONAGEM[1:])}>=valor"
    if chave_nome(nome) in ("narrador", "narradora"):
        return "o narrador não entra em personagens: a voz dele é narracao.voz"
    original = next((x for x in do_usuario.get("personagens") or [] if chave_nome(x["nome"]) == chave_nome(nome)), None)
    if opcao == "--definir" and original and original.get(sub):
        return f"o usuário já definiu ({exibir(original[sub])}); só ele muda (--usuario, com a resposta dele)"
    if sub == "cor":
        valor, erro = normalizar(POR_CHAVE["legendas.cor_destaque"], texto)
        if erro:
            return erro
    else:
        valor = voz_canonica(texto.strip()) if sub == "voz" else texto.strip() or None
    alvos = [valores] + ([do_usuario] if opcao == "--usuario" else [])
    for destino in alvos:
        lista = destino.get("personagens")
        if lista is None:
            lista = destino["personagens"] = []
        alvo = next((x for x in lista if chave_nome(x["nome"]) == chave_nome(nome)), None)
        if alvo is None:
            alvo = {"nome": nome, "voz": None, "estilo": None, "descricao": None, "cor": None}
            lista.append(alvo)
        alvo[sub] = valor
    return None


def imprimir_problemas(p: Pedido, avisos_extra: list[str] = ()) -> None:
    for e in p.erros:
        print(f"erro: {e}")
    for a in [*p.avisos, *avisos_extra]:
        print(f"aviso: {a}")


# ---------------------------------------------------------------- comandos


def cmd_validar(caminho: Path) -> int:
    p = carregar(caminho)
    completo = caminho.name == COMPLETO
    print(f"pedido: {caminho}{' (completo)' if completo else ''}")
    if not completo:
        definidos = [c for c in CAMPOS if p.valores[c.chave] is not None]
        provisorio = copy.deepcopy(p.valores)
        aplicar_padroes(provisorio)
        decidir, perguntar = faltando(provisorio)
        print(f"\nvocê definiu ({len(definidos)}):")
        for c in definidos:
            print(f"  {c.chave}: {exibir(p.valores[c.chave], c)}")
        if decidir:
            print(f"\nfalta decidir ({len(decidir)}), o orquestrador decide e passa em completar --definir:")
            for chave, texto in decidir:
                print(f"  {chave}: {texto}")
            if any(chave.startswith("personagens") for chave, _ in decidir):
                fornecedor = p.valores.get("narracao.fornecedor") or "gemini"
                print(f"  vozes de {fornecedor} para os personagens:\n  {lista_de_vozes(fornecedor)}")
        if perguntar:
            print(f"\npergunte ao usuário ({len(perguntar)}):")
            for q in perguntar:
                print(f"  {q}")
        padroes = [c for c in CAMPOS if c.quem == "padrao" and p.valores[c.chave] is None]
        if padroes:
            print(f"\no script completa com o padrão ({len(padroes)}):")
            for c in padroes:
                if isinstance(c.padrao, PorFormato) and not p.valores.get("formato"):
                    texto = f"{exibir(c.padrao.longo)} no longo, {exibir(c.padrao.short)} no short"
                else:
                    texto = exibir(provisorio[c.chave], c)
                print(f"  {c.chave} = {texto}")
        print()
        imprimir_problemas(p, coerencia(provisorio) if not p.erros else [])
        return 1 if p.erros else 0

    preenchidos = aplicar_padroes(p.valores)
    decidir, perguntar = faltando(p.valores)
    imprimir_valores(p.valores, p.extras)
    imprimir_personagens(p.valores, {}, set(), set(preenchidos))
    print()
    original = caminho.parent / ORIGINAL
    if original.exists():
        escrito = carregar(original).valores
        for c in CAMPOS:
            if escrito.get(c.chave) is not None and escrito[c.chave] != p.valores.get(c.chave):
                print(f"diferente do pedido original: {c.chave} (original: {exibir(escrito[c.chave], c)})")
    if preenchidos:
        print(f"aviso: campos vazios que o brief vai completar com o padrão: {', '.join(preenchidos)}")
    for chave, _ in decidir:
        print(f"erro: {chave} está vazio")
    for q in perguntar:
        print(f"erro: {q}")
    if p.valores.get("slug") and p.valores["slug"] != caminho.parent.name:
        print(f"aviso: slug '{p.valores['slug']}' diferente da pasta '{caminho.parent.name}'")
    imprimir_problemas(p, coerencia(p.valores, caminho.parent) if not p.erros else [])
    ok = not (p.erros or decidir or perguntar)
    print("ok: pedido completo" if ok else "pedido incompleto ou com erros")
    return 0 if ok else 1


def cmd_completar(entrada: Path, respostas: list[str], definicoes: list[str], raiz_projetos: Path,
                  sobrescrever: bool) -> int:
    p = carregar(entrada)
    do_usuario = copy.deepcopy(p.valores)
    definidos = set()
    for opcao, itens in (("--usuario", respostas), ("--definir", definicoes)):
        for item in itens:
            chave, igual, texto = item.partition("=")
            chave = chave.strip()
            if igual and chave.startswith("personagens.") and chave.count(".") >= 2:
                if erro := definir_personagem(p.valores, do_usuario, chave, texto, opcao):
                    p.erros.append(f"{opcao} {chave}: {erro}")
                elif opcao == "--definir":
                    definidos.add(chave)
                    if do_usuario.get("personagens") is None:
                        definidos.add("personagens")
                continue
            c = POR_CHAVE.get(chave)
            if not igual or c is None:
                p.erros.append(f"{opcao} {item!r}: use CAMPO=VALOR com um campo do modelo")
                continue
            if opcao == "--definir" and do_usuario.get(chave) is not None:
                p.erros.append(f"--definir {chave}: o usuário já definiu esse campo ({exibir(do_usuario[chave], c)}); "
                               "só o usuário muda o que ele escreveu (--usuario, com a resposta dele)")
                continue
            bruto = texto
            if c.tipo in ("lista", "personagens") and texto.strip().startswith(("[", "{")):
                try:
                    bruto = YAML(typ="safe", pure=True).load(texto)
                except YAMLError as e:
                    p.erros.append(f"{opcao} {chave}: YAML inválido ({' '.join(str(e).split())[:120]})")
                    continue
            valor, erro = normalizar(c, bruto)
            if erro:
                p.erros.append(f"{opcao} {chave}: {erro}")
                continue
            p.valores[chave] = valor
            if opcao == "--usuario":
                do_usuario[chave] = valor
            else:
                definidos.add(chave)
    preenchidos = aplicar_padroes(p.valores)
    decidir, perguntar = faltando(p.valores)
    if p.erros or decidir or perguntar:
        imprimir_problemas(p)
        for chave, texto in decidir:
            print(f"falta decidir: {chave}: {texto}")
        for q in perguntar:
            print(f"pergunte ao usuário: {q}")
        print("nada foi gravado")
        return 1

    pasta = raiz_projetos / p.valores["slug"]
    destino = pasta / COMPLETO
    if (destino.exists() or (pasta / BRIEF).exists()) and not sobrescrever:  # um roteiro sozinho na pasta não conta
        motivo = "já tem um pedido" if destino.exists() else "já tem um brief (projeto anterior)"
        print(f"erro: o projeto {pasta} {motivo}. Confirme com o usuário e rode de novo com --sobrescrever, "
              "ou edite o pedido existente. Nada foi gravado.")
        return 1
    pasta.mkdir(parents=True, exist_ok=True)
    if entrada.resolve() != (pasta / ORIGINAL).resolve():
        shutil.copyfile(entrada, pasta / ORIGINAL)
    destino.write_text(renderizar(p.valores, p.extras, cabecalho_completo(p.valores)), encoding="utf-8")
    print(f"gravado: {destino} (original em {pasta / ORIGINAL})")
    imprimir_resumo(p.valores, origens(p.valores, do_usuario, definidos), p.extras)
    imprimir_personagens(p.valores, do_usuario, definidos, set(preenchidos))
    print()
    imprimir_problemas(p, coerencia(p.valores, pasta))
    return 0


def video_id(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/|embed/)([\w-]{11})", url)
    return m.group(1) if m else None


def cmd_brief(pasta: Path) -> int:
    caminho = pasta / COMPLETO
    if not caminho.exists():
        print(f"erro: {caminho} não existe; rode completar antes", file=sys.stderr)
        return 2
    p = carregar(caminho)
    preenchidos = aplicar_padroes(p.valores)
    decidir, perguntar = faltando(p.valores)
    if p.erros or decidir or perguntar:
        imprimir_problemas(p)
        for chave, _ in decidir:
            print(f"erro: {chave} está vazio")
        for q in perguntar:
            print(f"erro: {q}")
        print("o brief não foi gerado")
        return 1
    brief_path = pasta / BRIEF
    anterior = json.loads(brief_path.read_text(encoding="utf-8")) if brief_path.exists() else {}
    refs_anteriores = {r.get("url"): r.get("esqueleto") for r in anterior.get("referencias", []) if isinstance(r, dict)}
    referencias = []
    for url in p.valores["referencias"]:
        vid = video_id(url)
        achados = sorted((RAIZ / "biblioteca" / "esqueletos").glob(f"*{vid}.json")) if vid else []
        esqueleto = str(achados[0].relative_to(RAIZ)) if achados else refs_anteriores.get(url)
        referencias.append({"url": url, "esqueleto": esqueleto})

    hoje = date.today().isoformat()
    novo = aninhar(p.valores, p.extras)
    novo["referencias"] = referencias
    if arquivo := arquivo_de_contexto(p.valores.get("contexto"), pasta):
        novo["contexto"] = arquivo.read_text(encoding="utf-8").strip()
        novo["contexto_arquivo"] = str(arquivo.relative_to(RAIZ)) if arquivo.is_relative_to(RAIZ) else str(arquivo)
        if (manifesto := arquivo.parent / "contexto.json").is_file():  # contexto salvo pela interface web
            novo["contexto_anexos"] = [
                {"caminho": str((arquivo.parent / a["arquivo"]).relative_to(RAIZ)), "tipo": a["tipo"],
                 "descricao": a.get("descricao") or ""}
                for a in json.loads(manifesto.read_text(encoding="utf-8")).get("anexos") or []]
    novo["pedido"] = {"arquivo": COMPLETO, "sha1": sha1_arquivo(caminho), "gerado_em": hoje}
    novo["criado_em"] = anterior.get("criado_em") or hoje
    for chave, valor in anterior.items():  # o que outras fases acrescentaram ao brief continua lá
        # "audio" era o nome antigo de narracao.ativa; os do contexto mudam com ele e não podem sobrar de outro
        if chave not in novo and chave not in ("audio", "contexto_arquivo", "contexto_anexos"):
            novo[chave] = valor
    brief_path.write_text(json.dumps(novo, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    com_esqueleto = sum(1 for r in referencias if r["esqueleto"])
    print(f"brief: {brief_path} · {len(referencias)} referência(s), {com_esqueleto} com esqueleto")
    if preenchidos:
        print(f"aviso: completados com o padrão: {', '.join(preenchidos)}")
    if p.valores["slug"] != pasta.name:
        print(f"aviso: slug '{p.valores['slug']}' diferente da pasta '{pasta.name}'")
    imprimir_problemas(p, coerencia(p.valores, pasta))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("modelo", help="grava o modelo em branco")
    m.add_argument("saida", type=Path, nargs="?", help="arquivo de saída (sem ele, imprime)")
    v = sub.add_parser("validar", help="confere um pedido e lista o que falta decidir")
    v.add_argument("pedido", type=Path)
    c = sub.add_parser("completar", help="grava projetos/<slug>/00-pedido.yaml completo")
    c.add_argument("pedido", type=Path, help="o pedido do usuário")
    c.add_argument("--definir", action="append", default=[], metavar="CAMPO=VALOR",
                   help="decisão do orquestrador para um campo vazio (repita para cada campo)")
    c.add_argument("--usuario", action="append", default=[], metavar="CAMPO=VALOR",
                   help="resposta que o usuário deu na conversa (conta como dele; pode mudar o que ele escreveu)")
    c.add_argument("--projetos", type=Path, default=RAIZ / "projetos", help="pasta dos projetos (padrão: projetos/)")
    c.add_argument("--sobrescrever", action="store_true", help="grava mesmo que o projeto já tenha pedido ou brief")
    b = sub.add_parser("brief", help="gera 00-brief.json a partir do pedido completo")
    b.add_argument("pasta", type=Path, help="pasta do projeto (projetos/<slug>)")
    args = ap.parse_args()

    if args.cmd == "modelo":
        texto = renderizar({}, {}, cabecalho_modelo())
        if args.saida:
            args.saida.parent.mkdir(parents=True, exist_ok=True)
            args.saida.write_text(texto, encoding="utf-8")
            print(f"modelo: {args.saida}")
        else:
            print(texto, end="")
        return 0
    if args.cmd == "brief":
        return cmd_brief(args.pasta)
    if not args.pedido.exists():
        print(f"erro: {args.pedido} não existe", file=sys.stderr)
        return 2
    if args.cmd == "validar":
        return cmd_validar(args.pedido)
    return cmd_completar(args.pedido, args.usuario, args.definir, args.projetos, args.sobrescrever)


if __name__ == "__main__":
    sys.exit(main())
