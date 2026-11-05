#!/usr/bin/env python3
"""Gera as imagens de um projeto com um modelo de imagem (Gemini/Nano Banana por padrão, ou gpt-image da OpenAI).

Lê projetos/<slug>/08-cenas.json e 08-referencias.jsonl (gerados por ferramentas/cenas.py) e:

  1. gera as fichas de referência dos personagens e locais usados nas cenas escolhidas;
  2. gera cada cena enviando junto as fichas dos seus personagens e local, para que o mesmo personagem
     tenha o mesmo rosto e a mesma roupa em todas as imagens, e a thumbnail do mesmo jeito
     (09-imagens/thumbnail.png ou .jpg, sem o texto, que a montagem escreve por cima);
  3. monta uma folha de contato (todas as imagens numa grade) para revisão rápida.

Imagens já geradas são puladas, a não ser que o prompt tenha mudado ou que se use --refazer. O registro
fica em 09-imagens/manifesto.json, gravado a cada imagem pronta.

As chamadas rodam em paralelo (nucleo.paralelo, com asyncio): até --paralelo abertas ao mesmo tempo, e cada cena
começa assim que as fichas dela ficam prontas, sem esperar as outras.

O provedor vem de --provedor (padrão gemini; o fluxo passa o configurado em Configurações) e o modelo padrão,
de visual.qualidade no pedido (rascunho, padrao, maxima). Cenas com "fonte": "internet"
ficam com buscar_imagens.py; este script só as gera quando a busca não achou nada e a cena tem prompt de
reserva.

Com --provisorio, não chama a API: desenha quadros de rascunho (id e descrição da cena) em
09-imagens/provisorio/, para validar ritmo, legendas e montagem antes de pagar pelas imagens.

O texto de cada pedido (a instrução das fichas de referência, o "Avoid: ...") e o hash que decide se uma imagem
precisa ser gerada de novo são os mesmos em qualquer provedor; o hash só inclui o provedor quando ele não é o
Gemini, para os projetos já gerados não serem refeitos. Pedido recusado pelo filtro de conteúdo fica com
"status": "bloqueado" no manifesto (o fluxo suaviza o prompt e gera de novo).

Chave: nucleo.chaves (ambiente, depois as cadastradas em Configurações > Chaves de API).

Uso:
    python3 ferramentas/producao/gerar_imagens.py projetos/<slug> --simular
    python3 ferramentas/producao/gerar_imagens.py projetos/<slug>
    python3 ferramentas/producao/gerar_imagens.py projetos/<slug> --cenas B01-03,B02-01 --refazer
    python3 ferramentas/producao/gerar_imagens.py projetos/<slug> --provedor openai
    python3 ferramentas/producao/gerar_imagens.py projetos/<slug> --provisorio

Códigos de saída: 0 ok; 1 nenhuma cena para gerar, ou erro/bloqueio em alguma; 2 sem 08-cenas.json, sem chave ou
sem ffmpeg no provisório; 3 sem cota no provedor.

Usa o ffmpeg para o modo provisório e a folha de contato.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "ferramentas"))
from brief import config, ler_brief  # noqa: E402
import custos  # noqa: E402
from nucleo import paralelo  # noqa: E402
import nucleo.provedores.imagem as provedores_imagem  # noqa: E402
from nucleo.provedores.erros import Bloqueado, ErroProvedor, SemChave, SemCota  # noqa: E402
from nucleo.provedores.imagem.base import PedidoImagem, Referencia  # noqa: E402

# Os dados (biblioteca/, projetos/) ficam no espaço de trabalho: a raiz do repositório, ou APP_RAIZ.
ESPACO = Path(os.environ.get("APP_RAIZ") or RAIZ).resolve()
PROVEDOR_PADRAO = "gemini"
PARALELO_PADRAO = 6  # chamadas abertas ao mesmo tempo; o limite de taxa (429) é tratado com espera pelo http
MODELO_PADRAO = provedores_imagem.classe(PROVEDOR_PADRAO).modelo_padrao
MODELOS_POR_QUALIDADE = dict(provedores_imagem.classe(PROVEDOR_PADRAO).modelos_por_qualidade)
ErroGeracao = ErroProvedor  # nome antigo


# ---------------------------------------------------------------- utilidades


def hash_pedido(*partes: object) -> str:
    return hashlib.sha1(json.dumps(partes, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


def arquivo_existente(pasta: Path, id_: str) -> Path | None:
    for ext in ("png", "jpg", "webp"):
        if (pasta / f"{id_}.{ext}").exists():
            return pasta / f"{id_}.{ext}"
    return None


def fonte_negrito() -> str | None:
    try:
        return subprocess.run(["fc-match", "-f", "%{file}", "Noto Sans:bold"], capture_output=True, text=True).stdout or None
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------- geração


def mensagem_sem_cota(provedor: str, detalhe: str) -> list[str]:
    if provedor == "gemini":
        return ["erro: o projeto do Google não tem cota para geração de imagem (plano gratuito: limite 0).",
                f"  detalhe: {detalhe[:200]}",
                "  solução: ative o faturamento do projeto em https://aistudio.google.com/apikey (Set up billing)"]
    nome = provedores_imagem.classe(provedor).nome
    return [f"erro: a conta de {nome} está sem cota ou sem crédito para gerar imagens.", f"  detalhe: {detalhe[:200]}"]


class Gerador:
    def __init__(self, args):
        self.args = args
        self.pasta = args.pasta
        self.saida = self.pasta / "09-imagens"
        self.pasta_refs = self.saida / "referencias"
        self.manifesto_path = self.saida / "manifesto.json"
        self.trava = threading.Lock()
        self.sem_cota: str | None = None
        self.provedor_id = args.provedor
        self.classe = provedores_imagem.classe(args.provedor)
        self.provedor = None  # criado só na hora de gerar: --simular e --provisorio não precisam de chave
        cenas_doc = json.loads((self.pasta / "08-cenas.json").read_text(encoding="utf-8"))
        self.proporcao = cenas_doc.get("proporcao", "16:9")
        self.cenas = cenas_doc["cenas"]
        self.thumb = cenas_doc.get("thumbnail")  # gerada como uma cena a mais, com o id "thumbnail"
        refs_path = self.pasta / "08-referencias.jsonl"
        self.refs = {}
        if refs_path.exists():
            self.refs = {r["id"]: r for r in map(json.loads, refs_path.read_text(encoding="utf-8").splitlines()) if r}
        self.manifesto = json.loads(self.manifesto_path.read_text(encoding="utf-8")) if self.manifesto_path.exists() else {}

    def salvar_manifesto(self) -> None:
        with self.trava:
            self.saida.mkdir(parents=True, exist_ok=True)
            self.manifesto_path.write_text(json.dumps(self.manifesto, ensure_ascii=False, indent=2), encoding="utf-8")

    def da_internet(self, c: dict) -> bool:
        """Cena que fica com buscar_imagens.py. Se a busca não achou nada, vale o prompt de reserva da cena, e
        a imagem gerada no lugar dela passa a ser tratada como as outras ("origem": "ia")."""
        registro = self.manifesto.get(c["id"], {})
        return (c.get("fonte") == "internet" and registro.get("origem") != "ia"
                and registro.get("status") != "nao-encontrada")

    def selecionar(self) -> list[dict]:
        cenas = [c for c in self.cenas if c.get("prompt_final") and not self.da_internet(c)]
        ids = set(self.args.cenas.split(",")) if self.args.cenas else None
        if ids:
            cenas = [c for c in cenas if c["id"] in ids]
        if self.args.limite:
            return cenas[: self.args.limite]
        if self.thumb and self.thumb.get("prompt_final") and (ids is None or "thumbnail" in ids):
            cenas.append(self.thumb)
        return cenas

    def refs_da_cena(self, cena: dict) -> list[str]:
        if self.args.sem_referencias:
            return []
        ids = [r for r in cena.get("referencias", []) if r in self.refs]
        if not self.args.com_objetos:
            ids = [r for r in ids if self.refs[r]["tipo"] != "objeto"]
        return ids[:self.classe.max_referencias]

    def hash(self, *partes: object) -> str:
        """O hash do pedido. O provedor só entra quando não é o Gemini: os projetos já gerados não mudam."""
        if self.provedor_id != PROVEDOR_PADRAO:
            partes = (*partes, self.provedor_id, self.args.qualidade)
        return hash_pedido(*partes)

    def precisa(self, id_: str, pasta: Path, pedido: str) -> bool:
        if self.args.refazer:
            return True
        if arquivo_existente(pasta, id_) is None:
            return True
        return self.manifesto.get(id_, {}).get("pedido") != pedido  # prompt ou referências mudaram

    def gerar_um(self, id_: str, pasta: Path, texto: str, fichas: list[Path], pedido: str, proporcao: str,
                 refs: list[str]) -> str:
        if self.sem_cota:
            return "pulado"
        inicio = time.time()
        try:
            gerada = self.provedor.gerar(PedidoImagem(texto, tuple(Referencia.de_arquivo(f) for f in fichas), proporcao,
                                                      self.args.tamanho, self.args.qualidade))
        except SemCota as e:
            self.sem_cota = str(e)
            return "sem-cota"
        except Bloqueado as e:
            with self.trava:
                self.manifesto[id_] = {"status": "bloqueado", "origem": "ia", "erro": str(e), "modelo": self.args.modelo,
                                       "provedor": self.provedor_id, "pedido": None}
            print(f"  bloqueado {id_}: {e}", flush=True)
            return "bloqueado"
        except ErroProvedor as e:
            with self.trava:
                self.manifesto[id_] = {"status": "erro", "origem": "ia", "erro": str(e), "modelo": self.args.modelo, "pedido": None}
            print(f"  erro {id_}: {e}", flush=True)
            return "erro"
        dados, ext, uso = gerada.dados, gerada.extensao, gerada.uso
        pasta.mkdir(parents=True, exist_ok=True)
        antigo = arquivo_existente(pasta, id_)
        if antigo:
            antigo.unlink()
        destino = pasta / f"{id_}.{ext}"
        destino.write_bytes(dados)
        with self.trava:
            custos.registrar(self.pasta, "imagem", self.args.modelo, uso, item=id_, tamanho=self.args.tamanho,
                             provedor=self.provedor_id)
            self.manifesto[id_] = {"status": "ok", "origem": "ia", "arquivo": str(destino.relative_to(self.pasta)), "modelo": self.args.modelo,
                                   "pedido": pedido, "referencias": refs, "segundos": round(time.time() - inicio, 1),
                                   "gerado_em": datetime.now().isoformat(timespec="seconds")}
            if self.provedor_id != PROVEDOR_PADRAO:
                self.manifesto[id_]["provedor"] = self.provedor_id
        print(f"  ok {id_} ({time.time() - inicio:.0f} s)", flush=True)
        return "ok"

    def copiar_da_biblioteca(self, id_: str, origem: Path, pedido: str) -> None:
        self.pasta_refs.mkdir(parents=True, exist_ok=True)
        if antigo := arquivo_existente(self.pasta_refs, id_):
            antigo.unlink()
        destino = self.pasta_refs / f"{id_}{origem.suffix.lower()}"
        shutil.copyfile(origem, destino)
        with self.trava:
            self.manifesto[id_] = {"status": "ok", "origem": "biblioteca", "arquivo": str(destino.relative_to(self.pasta)),
                                   "fonte": str(origem.relative_to(ESPACO)), "pedido": pedido,
                                   "gerado_em": datetime.now().isoformat(timespec="seconds")}
        self.salvar_manifesto()
        print(f"  ficha {id_}: copiada de {origem.relative_to(ESPACO)}", flush=True)

    def executar(self) -> int:
        if self.args.provisorio:
            return self.provisorio()
        cenas = self.selecionar()
        if not cenas:
            print("nenhuma cena com prompt para gerar")
            return 1

        refs_usadas = sorted({r for c in cenas for r in self.refs_da_cena(c)})
        tarefas_refs = []
        for rid in refs_usadas:
            r = self.refs[rid]
            pedido = self.hash(self.args.modelo, r["prompt"], r.get("negativo"))
            if r.get("imagem") and (ESPACO / r["imagem"]).is_file():
                # personagem da biblioteca: a imagem aprovada na criação do personagem já é a ficha
                pedido = hash_pedido("biblioteca", r["imagem"])
                if self.precisa(rid, self.pasta_refs, pedido) and not self.args.simular:
                    self.copiar_da_biblioteca(rid, ESPACO / r["imagem"], pedido)
                continue
            if self.precisa(rid, self.pasta_refs, pedido):
                texto = f"{r['prompt']}\n\nAvoid: {r.get('negativo') or 'text'}. Do not render any text, letters, labels or watermarks."
                tarefas_refs.append((rid, self.pasta_refs, texto, [], pedido, r.get("proporcao", "1:1"), []))

        tarefas_cenas = []
        for c in cenas:
            refs = self.refs_da_cena(c)
            pedido = self.hash(self.args.modelo, c["prompt_final"], c.get("negativo_final"), refs, self.proporcao)
            if self.precisa(c["id"], self.saida, pedido):
                tarefas_cenas.append((c, refs, pedido))

        print(f"provedor: {self.provedor_id} · modelo: {self.args.modelo} · proporção: {self.proporcao}")
        sem_busca = [c["id"] for c in self.cenas if self.da_internet(c) and not arquivo_existente(self.saida, c["id"])]
        if sem_busca:
            print(f"aviso: {len(sem_busca)} cena(s) da internet ainda sem imagem ({', '.join(sem_busca[:8])}); "
                  "rode buscar_imagens.py")
        print(f"a gerar: {len(tarefas_refs)} ficha(s) de referência + {len(tarefas_cenas)} cena(s) "
              f"(já prontas: {len(refs_usadas) - len(tarefas_refs)} fichas, {len(cenas) - len(tarefas_cenas)} cenas)")
        if self.args.simular:
            for t in tarefas_refs:
                print(f"  ficha {t[0]}")
            for c, refs, _ in tarefas_cenas:
                print(f"  cena {c['id']} · referências: {', '.join(refs) or '—'}")
            return 0
        try:
            self.provedor = provedores_imagem.criar(self.provedor_id, self.args.modelo)
        except SemChave as e:
            print(f"erro: {e}", file=sys.stderr)
            return 2

        def cena(t):
            c, refs, pedido = t
            fichas, rotulos = [], []
            for rid in refs:  # as fichas que existem agora: uma que falhou fica de fora
                arq = arquivo_existente(self.pasta_refs, rid)
                if arq:
                    fichas.append(arq)
                    tipo = {"personagem": "character", "local": "location", "objeto": "prop"}[self.refs[rid]["tipo"]]
                    rotulos.append(f"image {len(rotulos) + 1} is the {tipo} reference for {self.refs[rid].get('nome', rid)}")
            instrucao = ""
            if rotulos:
                instrucao = ("Reference images are attached: " + "; ".join(rotulos) + ". Keep every character's face, "
                             "hair, body and clothing exactly as in their reference, and keep the location consistent. "
                             "Do not copy the reference layout (plain background, turnaround views): compose a new scene.\n\n")
            texto = (f"{instrucao}{c['prompt_final']}\n\nAvoid: {c.get('negativo_final') or 'text'}. "
                     "Do not render any text, letters, captions, subtitles or watermarks.")
            return self.gerar_um(c["id"], self.saida, texto, fichas, pedido, self.proporcao, refs)

        # Tudo de uma vez, até --paralelo chamadas abertas: cada cena espera só as fichas dela (não todas), e o
        # manifesto é gravado a cada imagem pronta.
        a_gerar = {t[0] for t in tarefas_refs}
        tarefas = [paralelo.Tarefa(t[0], lambda t=t: self.gerar_um(*t)) for t in tarefas_refs]
        tarefas += [paralelo.Tarefa(t[0]["id"], lambda t=t: cena(t), tuple(r for r in t[1] if r in a_gerar))
                    for t in tarefas_cenas]
        print(f"gerando até {self.args.paralelo} imagem(ns) ao mesmo tempo")
        resultados = list(paralelo.rodar(tarefas, self.args.paralelo, lambda *_: self.salvar_manifesto()).values())
        self.salvar_manifesto()

        if self.sem_cota:
            print()
            for linha in mensagem_sem_cota(self.provedor_id, self.sem_cota):
                print(linha)
            print("  enquanto isso: rode com --provisorio para montar o vídeo com quadros de rascunho")
            return 3
        ok, erros, bloqueadas = resultados.count("ok"), resultados.count("erro"), resultados.count("bloqueado")
        print(f"\ngeradas: {ok} · erros: {erros}" + (f" · bloqueadas pelo filtro: {bloqueadas}" if bloqueadas else ""))
        self.folha_contato()
        return 1 if erros or bloqueadas else 0

    # ------------------------------------------------------------ rascunho e revisão

    def provisorio(self) -> int:
        if not shutil.which("ffmpeg"):
            print("erro: ffmpeg não encontrado", file=sys.stderr)
            return 2
        largura, altura = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}.get(self.proporcao, (1920, 1080))
        pasta = self.saida / "provisorio"
        pasta.mkdir(parents=True, exist_ok=True)
        fonte = fonte_negrito()
        tamanho = int(largura * (0.045 if largura < altura else 0.028))
        colunas = int(largura * 0.62 / (tamanho * 0.55))  # o texto ocupa ~60% da largura e sobrevive ao zoom da montagem
        for c in self.cenas:
            cor = "0x" + hashlib.md5(c["bloco"].encode()).hexdigest()[:6]
            texto = f"{c['id']} · {c.get('tipo')} · {c.get('enquadramento', '')}\n\n" + "\n".join(textwrap.wrap(c.get("descricao", ""), colunas))
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
                f.write(texto)
            filtro = (f"drawbox=c=black@0.55:t=fill,drawtext=textfile='{f.name}':fontcolor=white:fontsize={tamanho}"
                      f":x=(w-tw)/2:y=h*0.2:line_spacing=10" + (f":fontfile='{fonte}'" if fonte else ""))
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={cor}:s={largura}x{altura}",
                            "-vf", filtro, "-frames:v", "1", str(pasta / f"{c['id']}.png")], check=True)
            os.unlink(f.name)
        print(f"quadros provisórios: {len(self.cenas)} em {pasta}")
        return 0

    def folha_contato(self) -> None:
        """Grade com todas as cenas geradas, rotuladas pelo id, para revisão numa imagem só."""
        imagens = [(c["id"], arquivo_existente(self.saida, c["id"])) for c in self.cenas]
        imagens = [(i, a) for i, a in imagens if a]
        if not imagens or not shutil.which("ffmpeg"):
            return
        fonte = fonte_negrito()
        vertical = self.proporcao in ("9:16", "4:5")
        w, h = (270, 480) if vertical else (480, 270)
        colunas = 6 if vertical else 4
        tmp = Path(tempfile.mkdtemp())
        for n, (id_, arq) in enumerate(imagens):
            rotulo = f"drawtext=text='{id_}':fontcolor=yellow:fontsize=26:x=8:y=8:box=1:boxcolor=black@0.6" + (f":fontfile='{fonte}'" if fonte else "")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(arq), "-vf",
                            f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},{rotulo}", "-frames:v", "1",
                            str(tmp / f"{n:04d}.png")], check=True)
        linhas = -(-len(imagens) // colunas)
        destino = self.saida / "folha-de-contato.jpg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "1", "-i", str(tmp / "%04d.png"), "-vf",
                        f"tile={colunas}x{linhas}:padding=4:color=black", "-frames:v", "1", "-q:v", "3", str(destino)], check=True)
        shutil.rmtree(tmp)
        print(f"folha de contato: {destino}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pasta", type=Path, help="pasta do projeto (projetos/<slug>)")
    ap.add_argument("--provedor", choices=provedores_imagem.PROVEDORES, default=PROVEDOR_PADRAO,
                    help=f"provedor de imagem (padrão {PROVEDOR_PADRAO})")
    ap.add_argument("--modelo", help=f"modelo de imagem (padrão: o de visual.qualidade no pedido para o provedor; no Gemini, "
                                     f"{MODELO_PADRAO}; mais barato: gemini-3.1-flash-lite-image; melhor: gemini-3-pro-image)")
    ap.add_argument("--cenas", help="só estas cenas, ids separados por vírgula (ex.: B01-03,B02-01; a thumbnail é 'thumbnail')")
    ap.add_argument("--limite", type=int, help="só as N primeiras cenas (para testar)")
    ap.add_argument("--refazer", action="store_true", help="gera de novo mesmo o que já existe")
    ap.add_argument("--sem-referencias", action="store_true", help="não usa fichas de referência (mais barato, menos coerente)")
    ap.add_argument("--com-objetos", action="store_true", help="também gera e envia fichas de objetos")
    ap.add_argument("--tamanho", choices=["1K", "2K", "4K"], help="resolução (só modelos Gemini 3; padrão do modelo)")
    ap.add_argument("--paralelo", type=int, default=PARALELO_PADRAO,
                    help=f"chamadas simultâneas ao provedor (padrão {PARALELO_PADRAO}; o fluxo passa o de Configurações)")
    ap.add_argument("--simular", action="store_true", help="só mostra o que seria gerado, sem chamar a API")
    ap.add_argument("--provisorio", action="store_true", help="quadros de rascunho locais, sem API")
    ap.add_argument("--so-folha", action="store_true", help="só refaz a folha de contato")
    args = ap.parse_args()
    if not (args.pasta / "08-cenas.json").exists():
        print(f"erro: {args.pasta / '08-cenas.json'} não existe; rode a direção visual e ferramentas/cenas.py antes", file=sys.stderr)
        return 2
    args.qualidade = config(ler_brief(args.pasta), "visual.qualidade")
    args.modelo = args.modelo or provedores_imagem.modelo_para(args.provedor, args.qualidade)
    g = Gerador(args)
    if args.so_folha:
        g.folha_contato()
        return 0
    return g.executar()


if __name__ == "__main__":
    sys.exit(main())
