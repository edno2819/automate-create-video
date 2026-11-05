#!/usr/bin/env python3
"""Monta a direção visual de um projeto a partir da bíblia visual e das cenas por bloco.

Lê projetos/<slug>/06-roteiro-final.md, 08-biblia-visual.json e 08-cenas/<bloco>.json (formato em
biblioteca/formato-cenas.md) e:

  - confere se os trechos das cenas cobrem a narração inteira de cada bloco, em ordem, sem pular nem
    repetir nada;
  - calcula id, início, fim e duração de cada cena pela contagem de palavras;
  - monta o prompt final de cada imagem colando, palavra por palavra, a descrição fixa dos personagens,
    locais e objetos e o estilo da bíblia (é isso que mantém as imagens coerentes entre si);
  - confere as cenas com "fonte": "internet" (imagem real de banco livre, buscada por buscar_imagens.py):
    precisam de "busca" e só valem se o pedido permitir (visual.imagens_da_internet);
  - anota quem fala em cada cena dos blocos com diálogo ([FALA: Nome] no roteiro) e avisa quando nenhum
    dos que falam aparece na imagem;
  - confere os efeitos sonoros das cenas ("efeito"): o id existe na biblioteca de efeitos
    (biblioteca/efeitos/efeitos.json), a palavra em que ele toca está no trecho e o volume vai de 1 a 100;
  - confere os campos da animação (visual.animacao no pedido): "animar" (true ou false), "animacao" (o movimento,
    em inglês) e as transições entre cenas (as de vídeo pedem a animação ligada e a cena anterior animada), e diz
    quantas cenas vão animar;
  - monta o prompt da thumbnail (08-thumbnail.json), com o mesmo estilo e as mesmas fichas das cenas;
  - grava 08-cenas.json (com a thumbnail na chave "thumbnail"), 08-storyboard.md, 08-prompts.jsonl e
    08-referencias.jsonl.

Uso:
    python3 ferramentas/cenas.py montar projetos/<slug>

Sai com código 1 se houver erros (o agente deve corrigi-los e rodar de novo). Só usa a biblioteca padrão.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import animacao
import efeitos
from brief import config, espaco, ler_brief, ppm_do_brief
from medir_roteiro import MARCADOR, PPM_PADRAO, SEGUNDOS_POR_PAUSA, eh_palavra, fmt_tempo, ler_roteiro, sem_acento

ENQUADRAMENTOS = {
    "plano-geral": "Wide establishing shot",
    "plano-medio": "Medium shot",
    "close": "Close-up shot",
    "detalhe": "Extreme close-up detail shot",
    "pov": "First-person point-of-view shot",
    "sobre-o-ombro": "Over-the-shoulder shot",
    "aereo": "High aerial view",
    "contra-plongee": "Low-angle shot",
    "plongee": "High-angle shot",
}
PLANOS_FECHADOS = {"close", "detalhe", "pov"}  # usam a descricao_curta do local, se houver
ENQUADRAMENTO_NO_PROMPT = re.compile(
    r"^\s*(an?\s+)?(extreme\s+)?(close[- ]?up|wide|medium|establishing|over[- ]the[- ]shoulder|aerial|low[- ]angle|"
    r"high[- ]angle|first[- ]person|point[- ]of[- ]view|pov)\b", re.IGNORECASE)
MOVIMENTOS = {"estatico", "zoom-in", "zoom-out", "pan-esquerda", "pan-direita", "subir", "descer"}
TIPOS = {"ilustracao", "cartela"}
FONTES = {"ia", "internet"}
MIN_DURACAO_S = 2.5
MIN_DURACAO_FALA_S = 1.5  # no diálogo, trocar de imagem a cada fala (plano e contraplano) é o normal
MAX_DURACAO_S = 12.0
MAX_PALAVRAS_PROMPT = 80
# Onde fica o texto da thumbnail e a parte do quadro que a imagem gerada deixa livre para ele.
AREAS_TEXTO_THUMB = {"alto": "top third", "baixo": "bottom third", "esquerda": "left third", "direita": "right third"}
MAX_PALAVRAS_THUMB = 4
MAX_FRACAO_EFEITOS = 1 / 3  # efeito sonoro em toda cena vira ruído: o que marca o momento é ser raro


def norm(palavra: str) -> str:
    """Forma de comparação: minúsculas, só letras e dígitos (pontuação e espaços não importam)."""
    return re.sub(r"[\W_]+", "", palavra.lower())


def palavras(texto: str) -> list[str]:
    return [t for t in MARCADOR.sub(" ", texto).split() if eh_palavra(t)]


def relativo(caminho: Path) -> str:
    """O caminho relativo à raiz do espaço de trabalho (como os scripts e os agentes o recebem), se ele está dentro
    dela; senão, o absoluto."""
    rel = os.path.relpath(Path(caminho).resolve(), espaco())
    return Path(rel).as_posix() if not rel.startswith("..") else str(Path(caminho).resolve())


def imagem_da_biblioteca(slug: str | None) -> str | None:
    """A imagem aprovada de um personagem de biblioteca/personagens/<slug>/, relativa à raiz do espaço de trabalho."""
    if not slug:
        return None
    pasta = espaco() / "biblioteca" / "personagens" / slug
    achadas = sorted(pasta.glob("imagem.*")) if pasta.is_dir() else []
    return relativo(achadas[0]) if achadas else None


def prompt_ficha_personagem(descricao: str, estilo: str) -> str:
    """Prompt da ficha de referência de um personagem. A criação de personagens (ferramentas/personagem.py) usa o
    mesmo, para que a imagem de um personagem da biblioteca sirva de ficha num projeto."""
    return (f"Character reference sheet. {descricao}. Front view and three-quarter view, full body, neutral expression, "
            f"plain light gray background. Style: {estilo}.")


class Montagem:
    def __init__(self, pasta: Path):
        self.pasta = pasta
        self.alertas: list[dict[str, str]] = []
        brief = ler_brief(pasta)
        self.internet_permitida = bool(config(brief, "visual.imagens_da_internet", False))
        self.modo_animacao = animacao.modo(brief)
        self.efeitos = efeitos.catalogo()

    def alertar(self, nivel: str, onde: str, msg: str) -> None:
        self.alertas.append({"nivel": nivel, "onde": onde, "mensagem": msg})

    @property
    def erros(self) -> int:
        return sum(a["nivel"] == "erro" for a in self.alertas)

    def ler_json(self, caminho: Path):
        try:
            return json.loads(caminho.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.alertar("erro", caminho.name, "arquivo não encontrado")
        except json.JSONDecodeError as e:
            self.alertar("erro", caminho.name, f"JSON inválido: {e}")
        return None

    # ------------------------------------------------------------ bíblia

    def carregar_biblia(self) -> dict | None:
        biblia = self.ler_json(self.pasta / "08-biblia-visual.json")
        if biblia is None:
            return None
        estilo = biblia.get("estilo") or {}
        if not estilo.get("prompt_base"):
            self.alertar("erro", "bíblia", "estilo.prompt_base vazio")
        self.personagens = {p["id"]: p for p in biblia.get("personagens", []) if p.get("id")}
        self.locais = {l["id"]: l for l in biblia.get("locais", []) if l.get("id")}
        self.objetos = {o["id"]: o for o in biblia.get("objetos", []) if o.get("id")}
        for grupo, itens in (("personagens", self.personagens), ("locais", self.locais), ("objetos", self.objetos)):
            for id_, item in itens.items():
                if not item.get("descricao"):
                    self.alertar("erro", f"bíblia {id_}", f"{grupo}: descrição vazia")
        for p in self.personagens.values():
            ref = p.get("referencia_en", "")
            if ref and not p.get("descricao", "").lower().startswith(ref.lower()):
                self.alertar("aviso", f"bíblia {p['id']}", f"a descrição não começa com a referencia_en '{ref}'")
        return biblia

    def etapa_personagem(self, pid: str, ordem_bloco: int, ordem: dict[str, int]) -> tuple[str, str]:
        """Devolve (id da ficha de referência, descrição) válidos para o personagem neste bloco."""
        p = self.personagens[pid]
        rid, desc = pid, p["descricao"]
        for etapa in sorted(p.get("evolucao", []), key=lambda e: ordem.get(e.get("a_partir_de", ""), 10**6)):
            if ordem.get(etapa.get("a_partir_de", ""), 10**6) <= ordem_bloco and etapa.get("descricao"):
                rid, desc = f"{pid}@{etapa['a_partir_de']}", etapa["descricao"]
        return rid, desc

    # ------------------------------------------------------------ cenas

    def montar(self) -> int:
        roteiro_path = self.pasta / "06-roteiro-final.md"
        if not roteiro_path.exists():
            print(f"erro: {roteiro_path} não existe", file=sys.stderr)
            return 2
        front, blocos, _ = ler_roteiro(roteiro_path)
        biblia = self.carregar_biblia()
        if biblia is None:
            return self.relatar([], None, front)
        estilo = biblia.get("estilo", {})
        proporcao = biblia.get("proporcao") or ("9:16" if str(front.get("formato")) == "short" else "16:9")
        ppm = ppm_do_brief(ler_brief(self.pasta)) or int(front.get("ppm") or PPM_PADRAO)
        spw = 60.0 / ppm
        ordem = {b.id: i for i, b in enumerate(blocos)}

        pasta_cenas = self.pasta / "08-cenas"
        extras = {p.stem for p in pasta_cenas.glob("*.json")} - set(ordem) if pasta_cenas.exists() else set()
        for nome in sorted(extras):
            self.alertar("aviso", nome, "arquivo de cenas de um bloco que não existe no roteiro (ignorado)")

        todas: list[dict] = []
        t_bloco = 0.0
        for b in blocos:
            dur_bloco = len(b.tokens) * spw + len(b.pausas) * SEGUNDOS_POR_PAUSA
            cenas = self.ler_json(pasta_cenas / f"{b.id}.json")
            if isinstance(cenas, dict):
                cenas = cenas.get("cenas")
            if cenas is None:
                t_bloco += dur_bloco
                continue
            if not isinstance(cenas, list) or not cenas:
                self.alertar("erro", b.id, "o arquivo precisa ser uma lista de cenas não vazia")
                t_bloco += dur_bloco
                continue

            # Fluxo de caracteres normalizados da narração do bloco e a posição de início de cada palavra.
            normas = [norm(w) for w in b.tokens]
            inicio_palavra, acumulado = [], 0
            for n in normas:
                inicio_palavra.append(acumulado)
                acumulado += len(n)
            esperado = "".join(normas)

            def palavra_em(pos: int) -> int:
                return max(0, bisect.bisect_right(inicio_palavra, pos) - 1) if pos < len(esperado) else len(b.tokens)

            def tempo(idx: int) -> float:
                pausas = sum(1 for p in b.pausas if p <= idx and idx > 0)
                return t_bloco + idx * spw + pausas * SEGUNDOS_POR_PAUSA

            pos, sincronizado = 0, True
            for i, c in enumerate(cenas, 1):
                cid = f"{b.id}-{i:02d}"
                trecho = palavras(str(c.get("trecho", "")))
                alvo = "".join(norm(w) for w in trecho)
                if sincronizado:
                    if not alvo:
                        self.alertar("erro", cid, "trecho vazio")
                        sincronizado = False
                    elif esperado[pos:pos + len(alvo)] != alvo:
                        idx = palavra_em(pos)
                        self.alertar("erro", cid, "o trecho não continua a narração. Esperado: \""
                                     + " ".join(b.tokens[idx:idx + 12]) + "…\" · encontrado: \"" + " ".join(trecho[:12]) + "…\"")
                        sincronizado = False
                ini_idx = palavra_em(pos) if sincronizado else None
                if sincronizado:
                    pos += len(alvo)
                fim_idx = palavra_em(pos) if sincronizado else None
                cena = self.validar_cena(c, cid, b.id, ordem[b.id], ordem, estilo)
                self.validar_efeito(cena, cid, trecho)
                self.validar_animacao(cena, cid)
                cena.update({
                    "id": cid, "bloco": b.id,
                    "palavras": [ini_idx, fim_idx] if ini_idx is not None else None,  # intervalo [ini, fim) nas palavras do bloco
                    "inicio": fmt_tempo(tempo(ini_idx)) if ini_idx is not None else None,
                    "fim": fmt_tempo(tempo(fim_idx)) if fim_idx is not None else None,
                    "inicio_s": round(tempo(ini_idx), 1) if ini_idx is not None else None,
                    "duracao_s": round(tempo(fim_idx) - tempo(ini_idx), 1) if ini_idx is not None else None,
                })
                if ini_idx is not None and any(f.personagem for f in b.falas):
                    self.anotar_falas(cena, [f for f in b.falas if f.inicio < fim_idx and f.fim > ini_idx])
                if cena["duracao_s"] is not None:
                    minimo = MIN_DURACAO_FALA_S if any(not f.startswith("Narrador") for f in cena.get("falas", [])) else MIN_DURACAO_S
                    if cena["duracao_s"] < minimo and cena.get("tipo") != "cartela":
                        self.alertar("aviso", cid, f"{cena['duracao_s']} s: troca de imagem rápida demais (mínimo {minimo} s); junte com a vizinha")
                    elif cena["duracao_s"] > MAX_DURACAO_S:
                        self.alertar("aviso", cid, f"{cena['duracao_s']} s com a mesma imagem (máximo {MAX_DURACAO_S:.0f} s); divida a cena")
                todas.append(cena)
            if sincronizado and pos < len(esperado):
                idx = palavra_em(pos)
                self.alertar("erro", b.id, "narração sem cena no fim do bloco: \"" + " ".join(b.tokens[idx:idx + 15]) + "…\"")
            t_bloco += dur_bloco

        # Enquadramento repetido três vezes seguidas deixa o ritmo visual monótono.
        for a, b2, c in zip(todas, todas[1:], todas[2:]):
            if a.get("enquadramento") == b2.get("enquadramento") == c.get("enquadramento") and a.get("tipo") == "ilustracao":
                self.alertar("aviso", c["id"], f"três cenas seguidas em '{c['enquadramento']}' ({a['id']}–{c['id']})")
        usados = {p for c in todas for p in c.get("personagens", [])}
        for pid in self.personagens:
            if pid not in usados:
                self.alertar("aviso", f"bíblia {pid}", "personagem não aparece em nenhuma cena")
        if self.modo_animacao == "destaques":
            ilustracoes = [c for c in todas if c.get("tipo") == "ilustracao" and c.get("fonte") != "internet"]
            marcadas = [c for c in ilustracoes if c.get("animar") is True]
            if not marcadas:
                self.alertar("aviso", "animação", "o pedido anima os destaques, mas nenhuma cena tem \"animar\": true: "
                                                  "vão animar a primeira ilustração de cada bloco e a última do vídeo")
            elif len(marcadas) > len(ilustracoes) / 2:
                self.alertar("aviso", "animação", f"{len(marcadas)} de {len(ilustracoes)} ilustrações marcadas para animar: "
                                                  "em destaques, guarde para o gancho, as viradas e o payoff")
        self.validar_transicoes(todas)
        com_efeito = [c for c in todas if isinstance(c.get("efeito"), dict)]
        if len(com_efeito) > max(1, len(todas) * MAX_FRACAO_EFEITOS):
            self.alertar("aviso", "efeitos", f"{len(com_efeito)} efeitos sonoros em {len(todas)} cenas: guarde-os para os "
                                             "momentos que pedem (no máximo um a cada três cenas)")
        for a, b2 in zip(todas, todas[1:]):
            ea, eb = a.get("efeito"), b2.get("efeito")
            if isinstance(ea, dict) and isinstance(eb, dict) and ea["id"] == eb["id"]:
                self.alertar("aviso", b2["id"], f"o mesmo efeito sonoro '{b2['efeito']['id']}' em duas cenas seguidas "
                                                f"({a['id']} e {b2['id']})")

        self.duracao_total = t_bloco
        self.proporcao, self.estilo, self.biblia, self.blocos = proporcao, estilo, biblia, blocos
        self.thumbnail = self.montar_thumbnail(estilo, ordem)
        return self.relatar(todas, roteiro_path, front)

    def montar_thumbnail(self, estilo: dict, ordem: dict[str, int]) -> dict | None:
        """08-thumbnail.json: a imagem da thumbnail, no formato de uma cena (sem trecho nem movimento), mais o texto
        que a montagem escreve por cima. Sem o arquivo, a montagem usa a primeira cena e o texto do ângulo."""
        caminho = self.pasta / "08-thumbnail.json"
        if not caminho.exists():
            self.alertar("aviso", "thumbnail", "sem 08-thumbnail.json: a thumbnail sai da primeira cena, com o texto do ângulo")
            return None
        t = self.ler_json(caminho)
        if not isinstance(t, dict):
            if t is not None:
                self.alertar("erro", "thumbnail", "08-thumbnail.json precisa ser um objeto")
            return None
        if t.get("fonte", "ia") != "ia" or t.get("tipo", "ilustracao") != "ilustracao":
            self.alertar("erro", "thumbnail", "a thumbnail é sempre uma ilustração gerada (tipo ilustracao, fonte ia)")
        texto = (t.get("texto") or "").strip()
        if len(texto.split()) > MAX_PALAVRAS_THUMB:
            self.alertar("aviso", "thumbnail", f"texto com {len(texto.split())} palavras; o máximo é {MAX_PALAVRAS_THUMB}")
        if texto and t.get("posicao_texto") not in AREAS_TEXTO_THUMB:
            self.alertar("erro", "thumbnail", f"posicao_texto '{t.get('posicao_texto')}' inválida (use {', '.join(AREAS_TEXTO_THUMB)})")
        bloco = t.get("bloco") or (self.blocos[0].id if self.blocos else "")
        if bloco not in ordem:
            self.alertar("erro", "thumbnail", f"bloco {bloco} não existe no roteiro")
            bloco = next(iter(ordem), "")
        thumb = self.validar_cena({**t, "tipo": "ilustracao", "fonte": "ia"}, "thumbnail", bloco, ordem.get(bloco, 0),
                                  ordem, estilo, thumbnail=True)
        return {**thumb, "id": "thumbnail", "texto": texto}

    def anotar_falas(self, cena: dict, falas: list) -> None:
        """Quem fala na cena, em ordem. Avisa quando nenhum personagem que fala está na imagem."""
        cena["falas"] = [f"{f.personagem or 'Narrador'}" + (f" ({f.direcao})" if f.direcao else "") for f in falas]
        falantes = {sem_acento(f.personagem).strip() for f in falas if f.personagem}
        na_imagem = " ".join(sem_acento(self.personagens[pid].get("nome", "")) for pid in cena.get("personagens", [])
                             if pid in self.personagens)
        if falantes and cena.get("tipo") == "ilustracao" and not any(nome in na_imagem for nome in falantes):
            self.alertar("aviso", cena["id"], f"fala de {', '.join(sorted(falantes))}, mas quem fala não está na imagem "
                                              "(ok se for uma reação de quem ouve)")

    def validar_efeito(self, cena: dict, cid: str, trecho: list[str]) -> None:
        """O efeito sonoro da cena: um id da biblioteca de efeitos, a palavra do trecho em que ele toca (sem ela, toca
        no corte para a cena) e o volume. Grava na cena o pedido completo ({id, palavra, volume})."""
        uso = efeitos.da_cena(cena.get("efeito"))
        if uso is None:
            cena.pop("efeito", None)
            return
        if not isinstance(uso, dict):
            self.alertar("erro", cid, 'efeito precisa ser um objeto ({"id": ..., "palavra": ..., "volume": ...}) ou o id do efeito')
            return
        if uso["id"] not in self.efeitos:
            ids = sorted(self.efeitos)
            existentes = ", ".join(ids[:30]) + ("…" if len(ids) > 30 else "") if ids else "a biblioteca está vazia"
            self.alertar("erro", cid, f"efeito '{uso['id']}' não existe na biblioteca de efeitos ({efeitos.INDICE}: {existentes})")
        if uso["palavra"] and len(uso["palavra"].split()) > 1:
            self.alertar("erro", cid, f"a palavra do efeito é uma palavra só do trecho, não '{uso['palavra']}'")
        elif uso["palavra"] and efeitos.forma(uso["palavra"]) not in {efeitos.forma(p) for p in trecho}:
            self.alertar("erro", cid, f"a palavra '{uso['palavra']}' do efeito não está no trecho da cena")
        volume = uso["volume"]
        if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not 1 <= volume <= 100:
            self.alertar("erro", cid, f"volume do efeito '{volume}' inválido (de 1 a 100, em %)")
        cena["efeito"] = uso

    def validar_transicoes(self, cenas: list[dict]) -> None:
        """A transição de cada cena (como ela entra): corte, fade ou as de biblioteca/animacao.md. As de vídeo são
        geradas no clipe da cena anterior, que precisa animar; senão, viram corte."""
        animadas = {c["id"] for c in animacao.selecionadas(cenas, self.modo_animacao, {})}
        de_video = 0
        for k, c in enumerate(cenas):
            t = c.get("transicao")
            if t in (None, "corte", "fade"):
                continue
            if t not in animacao.TRANSICOES:
                self.alertar("aviso", c["id"], f"transição '{t}' desconhecida: vira corte (use corte ou fade; com a animação "
                                               "ligada, também as de biblioteca/animacao.md)")
            elif k == 0:
                self.alertar("aviso", c["id"], f"a primeira cena não vem de outra: a transição {t} vira corte")
            elif t in animacao.TRANSICOES_DE_VIDEO:
                de_video += 1
                if self.modo_animacao == "nenhuma":
                    self.alertar("aviso", c["id"], f"a transição {t} só existe com a animação ligada (visual.animacao): vira corte")
                elif cenas[k - 1]["id"] not in animadas:
                    self.alertar("aviso", c["id"], f"a transição {t} é gerada no clipe da cena anterior ({cenas[k - 1]['id']}), "
                                                   "que não anima: vira corte (marque a anterior com \"animar\": true)")
        if de_video > max(2, len(cenas) / 4):
            self.alertar("aviso", "transições", f"{de_video} transições de vídeo em {len(cenas)} cenas: guarde para os "
                                                "momentos fortes e as trocas de bloco")

    def validar_animacao(self, cena: dict, cid: str) -> None:
        """"animar" (true ou false: a escolha do diretor visual no modo destaques) e "animacao" (o movimento da cena
        em inglês, que vai ao modelo de vídeo; sem ele, um movimento leve do que a imagem mostra)."""
        if "animar" in cena and not isinstance(cena["animar"], bool):
            self.alertar("erro", cid, "animar é true ou false")
        elif cena.get("animar") is True and (cena.get("tipo") != "ilustracao" or cena.get("fonte") == "internet"):
            self.alertar("aviso", cid, "animar só vale em ilustração gerada por IA: esta cena fica parada")
        movimento = cena.get("animacao")
        if movimento is not None and not isinstance(movimento, str):
            self.alertar("erro", cid, "animacao é um texto em inglês com o movimento da cena")
        elif isinstance(movimento, str) and len(movimento.split()) > animacao.MAX_PALAVRAS:
            self.alertar("aviso", cid, f"animacao com {len(movimento.split())} palavras; o máximo recomendado é "
                                       f"{animacao.MAX_PALAVRAS}: descreva só o movimento")

    def validar_cena(self, c: dict, cid: str, bloco: str, ordem_bloco: int, ordem: dict[str, int], estilo: dict,
                     thumbnail: bool = False) -> dict:
        tipo = c.get("tipo", "ilustracao")
        if tipo not in TIPOS:
            self.alertar("erro", cid, f"tipo '{tipo}' inválido (use {', '.join(sorted(TIPOS))})")
        enq = c.get("enquadramento", "")
        if enq not in ENQUADRAMENTOS:
            self.alertar("erro", cid, f"enquadramento '{enq}' inválido (use {', '.join(ENQUADRAMENTOS)})")
        if c.get("movimento") and c["movimento"] not in MOVIMENTOS:
            self.alertar("aviso", cid, f"movimento '{c['movimento']}' desconhecido")
        if tipo == "cartela" and not c.get("texto_na_tela"):
            self.alertar("erro", cid, "cartela sem texto_na_tela")
        prompt_cena = (c.get("prompt_cena") or "").strip()
        fonte = c.get("fonte") or "ia"
        if fonte not in FONTES:
            self.alertar("erro", cid, f"fonte '{fonte}' inválida (use {', '.join(sorted(FONTES))})")
        if fonte == "internet":
            if not self.internet_permitida:
                self.alertar("erro", cid, "imagem da internet, mas o pedido não permite (visual.imagens_da_internet: não)")
            busca = c.get("busca")
            buscas = [busca] if isinstance(busca, str) else busca if isinstance(busca, list) else []
            if not any(isinstance(b, str) and b.strip() for b in buscas):
                self.alertar("erro", cid, "imagem da internet sem busca (um texto ou uma lista de alternativas)")
            if not prompt_cena:
                self.alertar("aviso", cid, "imagem da internet sem prompt_cena de reserva: se a busca falhar, a cena fica sem imagem")
        elif tipo == "ilustracao" and not prompt_cena:
            self.alertar("erro", cid, "ilustração sem prompt_cena")
        if ENQUADRAMENTO_NO_PROMPT.match(prompt_cena):
            self.alertar("aviso", cid, "o prompt_cena começa pelo enquadramento, que o script já acrescenta: comece pelo sujeito")
        if len(prompt_cena.split()) > MAX_PALAVRAS_PROMPT:
            self.alertar("aviso", cid, f"prompt_cena com {len(prompt_cena.split())} palavras; o máximo recomendado é {MAX_PALAVRAS_PROMPT}")

        descricoes_p, referencias = [], []
        for pid in c.get("personagens", []):
            if pid not in self.personagens:
                self.alertar("erro", cid, f"personagem {pid} não existe na bíblia")
                continue
            rid, desc = self.etapa_personagem(pid, ordem_bloco, ordem)
            descricoes_p.append(desc)
            referencias.append(rid)
            ref = self.personagens[pid].get("referencia_en", "")
            if ref and prompt_cena and ref.lower() not in prompt_cena.lower():
                self.alertar("aviso", cid, f"{pid} está na cena mas o prompt_cena não cita '{ref}'")
        local = c.get("local")
        if local and local not in self.locais:
            self.alertar("erro", cid, f"local {local} não existe na bíblia")
            local = None
        if local:
            referencias.append(local)
        descricoes_o = []
        for oid in c.get("objetos", []):
            if oid not in self.objetos:
                self.alertar("erro", cid, f"objeto {oid} não existe na bíblia")
            else:
                descricoes_o.append(self.objetos[oid]["descricao"])
                referencias.append(oid)

        prompt = None
        if prompt_cena:
            frase = prompt_cena[0].upper() + prompt_cena[1:].rstrip(".")
            partes = [f"{ENQUADRAMENTOS.get(enq, 'Shot')}. {frase}."]
            if tipo == "cartela":
                partes.append("Background plate for a title card, calm composition with empty space in the center.")
            if thumbnail:
                partes.append("YouTube thumbnail: one clear focal subject that reads at a small size, bold silhouette, "
                              "expressive emotion, strong contrast and saturated accent colors, uncluttered background.")
                if area := AREAS_TEXTO_THUMB.get(c.get("posicao_texto", "")) if c.get("texto") else None:
                    partes.append(f"Keep the {area} of the frame clean and empty for a large title overlay; "
                                  "the subject stays out of it.")
            if descricoes_p:
                partes.append(("Character: " if len(descricoes_p) == 1 else "Characters: ") + "; ".join(descricoes_p) + ".")
            if local:
                info = self.locais[local]
                desc_local = info.get("descricao_curta") if enq in PLANOS_FECHADOS and info.get("descricao_curta") else info["descricao"]
                partes.append(f"Setting: {desc_local.rstrip('.')}.")
            if descricoes_o:
                partes.append("Props: " + "; ".join(descricoes_o) + ".")
            partes.append(f"Style: {estilo.get('prompt_base', '').rstrip('.')}.")
            prompt = " ".join(partes)
        negativo = ", ".join(x for x in (estilo.get("negativo_base", ""), c.get("negativo", "")) if x) or None
        return {**c, "tipo": tipo, "fonte": fonte, "prompt_final": prompt, "negativo_final": negativo,
                "referencias": referencias if prompt else []}

    # ------------------------------------------------------------ saída

    def relatar(self, cenas: list[dict], roteiro_path: Path | None, front: dict) -> int:
        if roteiro_path is not None and cenas:
            self.gravar(cenas, roteiro_path, front)
        ilustracoes = [c for c in cenas if c.get("tipo") == "ilustracao"]
        da_internet = sum(c.get("fonte") == "internet" for c in cenas)
        duracoes = [c["duracao_s"] for c in cenas if c.get("duracao_s") is not None]
        print(f"cenas: {len(cenas)} ({len(ilustracoes)} ilustrações, {len(cenas) - len(ilustracoes)} cartelas"
              f"{f'; {da_internet} com imagem da internet' if da_internet else ''})")
        if duracoes:
            print(f"duração por cena: média {sum(duracoes) / len(duracoes):.1f} s · mín {min(duracoes)} s · máx {max(duracoes)} s")
        if self.modo_animacao != "nenhuma":
            animadas = animacao.selecionadas(cenas, self.modo_animacao, {})
            print(f"animação ({self.modo_animacao}): {len(animadas)} cena(s)"
                  + (f" ({', '.join(c['id'] for c in animadas[:12])}{'…' if len(animadas) > 12 else ''})" if animadas else ""))
        if usados := [c["efeito"]["id"] for c in cenas if isinstance(c.get("efeito"), dict)]:
            print(f"efeitos sonoros: {len(usados)} ({', '.join(sorted(set(usados)))})")
        if cenas:
            print(f"arquivos: {self.pasta / '08-storyboard.md'} · 08-cenas.json · 08-prompts.jsonl · 08-referencias.jsonl")
        if t := getattr(self, "thumbnail", None):
            print(f"thumbnail: {', '.join(t.get('personagens', [])) or 'sem personagem'} · "
                  + (f"texto \"{t['texto']}\" ({t.get('posicao_texto')})" if t["texto"] else "sem texto"))
        erros = self.erros
        print(f"alertas: {erros} erro(s), {len(self.alertas) - erros} aviso(s)")
        for a in self.alertas:
            print(f"- {a['nivel']} [{a['onde']}] {a['mensagem']}")
        return 1 if erros else 0

    def gravar(self, cenas: list[dict], roteiro_path: Path, front: dict) -> None:
        hoje = date.today().isoformat()
        total = fmt_tempo(self.duracao_total)
        (self.pasta / "08-cenas.json").write_text(json.dumps({
            "projeto": self.biblia.get("projeto", self.pasta.name), "roteiro": relativo(roteiro_path), "gerado_em": hoje,
            "proporcao": self.proporcao, "duracao_estimada": total, "total_cenas": len(cenas),
            "alertas": self.alertas, "thumbnail": self.thumbnail, "cenas": cenas,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        with (self.pasta / "08-prompts.jsonl").open("w", encoding="utf-8") as f:
            for c in cenas:
                f.write(json.dumps({
                    "id": c["id"], "bloco": c["bloco"], "inicio": c["inicio"], "fim": c["fim"], "duracao_s": c["duracao_s"],
                    "tipo": c["tipo"], "fonte": c["fonte"], "busca": c.get("busca"), "proporcao": self.proporcao,
                    "prompt": c["prompt_final"], "negativo": c["negativo_final"],
                    "texto_na_tela": c.get("texto_na_tela"), "movimento": c.get("movimento"), "transicao": c.get("transicao"),
                    "personagens": c.get("personagens", []), "local": c.get("local"), "referencias": c["referencias"],
                }, ensure_ascii=False) + "\n")

        base, negativo = self.estilo.get("prompt_base", "").rstrip("."), self.estilo.get("negativo_base", "")
        with (self.pasta / "08-referencias.jsonl").open("w", encoding="utf-8") as f:
            for p in self.personagens.values():
                etapas = [(p["id"], p["descricao"])] + [(f"{p['id']}@{e['a_partir_de']}", e["descricao"]) for e in p.get("evolucao", []) if e.get("descricao")]
                for rid, desc in etapas:
                    ficha = {"id": rid, "tipo": "personagem", "nome": p.get("nome", ""), "proporcao": "1:1", "negativo": negativo,
                             "prompt": prompt_ficha_personagem(desc, base)}
                    if rid == p["id"] and (imagem := imagem_da_biblioteca(p.get("biblioteca"))):
                        ficha["imagem"] = imagem  # gerar_imagens.py copia a imagem aprovada em vez de gerar a ficha
                    f.write(json.dumps(ficha, ensure_ascii=False) + "\n")
            for l in self.locais.values():
                f.write(json.dumps({"id": l["id"], "tipo": "local", "nome": l.get("nome", ""), "proporcao": self.proporcao, "negativo": negativo,
                                    "prompt": f"Environment concept art, no people. Wide establishing shot. {l['descricao']}. Style: {base}."}, ensure_ascii=False) + "\n")
            for o in self.objetos.values():
                f.write(json.dumps({"id": o["id"], "tipo": "objeto", "nome": o.get("nome", ""), "proporcao": "1:1", "negativo": negativo,
                                    "prompt": f"Prop reference. {o['descricao']}, isolated on a plain light gray background. Style: {base}."}, ensure_ascii=False) + "\n")

        self.gravar_storyboard(cenas, front, total, hoje)

    def gravar_storyboard(self, cenas: list[dict], front: dict, total: str, hoje: str) -> None:
        e = self.estilo
        linhas = [
            f"# Storyboard: {front.get('titulo') or self.pasta.name}",
            "",
            f"Gerado em {hoje} por `ferramentas/cenas.py`. Não edite à mão: edite a bíblia ou `08-cenas/` e monte de novo.",
            "",
            f"- **Duração estimada:** {total} · **cenas:** {len(cenas)} · **proporção:** {self.proporcao}",
            f"- **Estilo:** {e.get('nome', '')}. {e.get('descricao', '')}",
        ]
        if e.get("regras_de_camera"):
            linhas.append(f"- **Câmera:** {e['regras_de_camera']}")
        linhas += ["", "## Bíblia visual", "", "| Id | Nome | Descrição fixa (vai em todos os prompts) |", "|---|---|---|"]
        for grupo in (self.personagens, self.locais, self.objetos):
            for item in grupo.values():
                linhas.append(f"| {item['id']} | {item.get('nome', '')} | {item.get('descricao', '')} |")
                for etapa in item.get("evolucao", []):
                    linhas.append(f"| {item['id']} a partir de {etapa.get('a_partir_de')} | | {etapa.get('descricao', '')} |")
        por_bloco: dict[str, list[dict]] = {}
        for c in cenas:
            por_bloco.setdefault(c["bloco"], []).append(c)
        for b in self.blocos:
            if b.id not in por_bloco:
                continue
            lista = por_bloco[b.id]
            linhas += ["", f"## [{b.id}] {b.titulo} ({lista[0]['inicio'] or '?'}–{lista[-1]['fim'] or '?'})", ""]
            for c in lista:
                quem = ", ".join(c.get("personagens", [])) or "—"
                linhas += [
                    f"### {c['id']} · {c['inicio'] or '?'}–{c['fim'] or '?'} ({c['duracao_s'] if c['duracao_s'] is not None else '?'} s) · {c['tipo']} · {c.get('enquadramento', '')}",
                    "",
                    f"> {c.get('trecho', '')}",
                    "",
                    f"**Imagem:** {c.get('descricao', '')}",
                    "",
                ]
                if c.get("texto_na_tela"):
                    linhas += [f"**Na tela:** {c['texto_na_tela']}", ""]
                if c.get("falas"):
                    linhas += [f"**Quem fala:** {' → '.join(c['falas'])}", ""]
                if c.get("animar") is not None or c.get("animacao"):
                    marca = {True: " (marcada para animar)", False: " (fica parada)"}.get(c.get("animar"), "")
                    linhas += [f"**Animação:** {animacao.movimento(c)}{marca}", ""]
                if isinstance(e := c.get("efeito"), dict):
                    nome = (self.efeitos.get(e["id"]) or {}).get("nome") or e["id"]
                    onde = f"na palavra \"{e['palavra']}\"" if e.get("palavra") else "no corte para a cena"
                    volume = f", volume {e['volume']}%" if e.get("volume") != efeitos.VOLUME_PADRAO else ""
                    linhas += [f"**Efeito sonoro:** {nome} (`{e['id']}`) {onde}{volume}", ""]
                if c.get("fonte") == "internet":
                    reserva = "o prompt abaixo é a reserva, se a busca falhar" if c.get("prompt_final") else "sem prompt de reserva"
                    busca = c.get("busca")
                    busca = " → ".join(busca) if isinstance(busca, list) else busca or ""
                    linhas += [f"**Imagem da internet:** busca `{busca}` ({reserva})", ""]
                linhas += [f"**Personagens:** {quem} · **local:** {c.get('local') or '—'} · **movimento:** {c.get('movimento') or '—'} · **transição:** {c.get('transicao') or '—'}", ""]
                if c.get("prompt_final"):
                    linhas += ["```text", c["prompt_final"], "```", ""]
        if t := self.thumbnail:
            texto = f"\"{t['texto']}\" ({t.get('posicao_texto')})" if t["texto"] else "sem texto"
            linhas += ["", "## Thumbnail", "", f"**Imagem:** {t.get('descricao', '')}", "",
                       f"**Texto por cima:** {texto} · **personagens:** {', '.join(t.get('personagens', [])) or '—'} · "
                       f"**local:** {t.get('local') or '—'}", ""]
            if t.get("prompt_final"):
                linhas += ["```text", t["prompt_final"], "```", ""]
        if self.alertas:
            linhas += ["## Alertas", ""] + [f"- **{a['nivel']}** [{a['onde']}] {a['mensagem']}" for a in self.alertas]
        (self.pasta / "08-storyboard.md").write_text("\n".join(linhas) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("montar", help="valida as cenas e gera storyboard, prompts e referências")
    m.add_argument("pasta", type=Path, help="pasta do projeto (projetos/<slug>)")
    args = ap.parse_args()
    return Montagem(args.pasta).montar()


if __name__ == "__main__":
    sys.exit(main())
