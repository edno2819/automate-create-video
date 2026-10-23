#!/usr/bin/env python3
"""Gera as imagens de um projeto com os modelos de imagem do Gemini (Nano Banana).

Lê projetos/<slug>/08-cenas.json e 08-referencias.jsonl (gerados por ferramentas/cenas.py) e:

  1. gera as fichas de referência dos personagens e locais usados nas cenas escolhidas;
  2. gera cada cena enviando junto as fichas dos seus personagens e local, para que o mesmo personagem
     tenha o mesmo rosto e a mesma roupa em todas as imagens, e a thumbnail do mesmo jeito
     (09-imagens/thumbnail.png ou .jpg, sem o texto, que a montagem escreve por cima);
  3. monta uma folha de contato (todas as imagens numa grade) para revisão rápida.

Imagens já geradas são puladas, a não ser que o prompt tenha mudado ou que se use --refazer. O registro
fica em 09-imagens/manifesto.json.

O modelo padrão vem de visual.qualidade no pedido (rascunho, padrao, maxima). Cenas com "fonte": "internet"
ficam com buscar_imagens.py; este script só as gera quando a busca não achou nada e a cena tem prompt de
reserva.

Com --provisorio, não chama a API: desenha quadros de rascunho (id e descrição da cena) em
09-imagens/provisorio/, para validar ritmo, legendas e montagem antes de pagar pelas imagens.

Chave: variável GEMINI_API_KEY (ou GOOGLE_API_KEY), ou o arquivo .env na raiz do projeto.

Uso:
    python3 skills/produzir-video/scripts/gerar_imagens.py projetos/<slug> --simular
    python3 skills/produzir-video/scripts/gerar_imagens.py projetos/<slug>
    python3 skills/produzir-video/scripts/gerar_imagens.py projetos/<slug> --cenas B01-03,B02-01 --refazer
    python3 skills/produzir-video/scripts/gerar_imagens.py projetos/<slug> --provisorio

Só usa a biblioteca padrão (e o ffmpeg para o modo provisório e a folha de contato).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(RAIZ / "ferramentas"))
from brief import config, ler_brief  # noqa: E402
import custos  # noqa: E402

API = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
MODELO_PADRAO = "gemini-3.1-flash-image"
MODELOS_POR_QUALIDADE = {"rascunho": "gemini-3.1-flash-lite-image", "padrao": MODELO_PADRAO, "maxima": "gemini-3-pro-image"}
MAX_REFERENCIAS_POR_CENA = 6
EXTENSOES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


class SemCota(Exception):
    """Cota zero (plano gratuito sem faturamento): não adianta tentar de novo."""


class ErroGeracao(Exception):
    pass


# ---------------------------------------------------------------- utilidades


def carregar_chave() -> str | None:
    for nome in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(nome):
            return os.environ[nome]
    for pasta in [Path.cwd(), *Path.cwd().parents]:
        env = pasta / ".env"
        if env.exists():
            for linha in env.read_text(encoding="utf-8").splitlines():
                m = re.match(r"\s*(GEMINI_API_KEY|GOOGLE_API_KEY)\s*=\s*(.+?)\s*$", linha)
                if m:
                    return m.group(2).strip("\"'")
            break
    return None


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


# ---------------------------------------------------------------- API


def chamar_api(chave: str, modelo: str, partes: list[dict], proporcao: str, tamanho: str | None) -> tuple[bytes, str, dict]:
    """A imagem, a extensão e o uso que a API cobra (usageMetadata), para o registro de custos."""
    config: dict = {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": proporcao}}
    if tamanho:
        config["imageConfig"]["imageSize"] = tamanho
    corpo = json.dumps({"contents": [{"role": "user", "parts": partes}], "generationConfig": config}).encode()
    req = urllib.request.Request(API.format(modelo=modelo), data=corpo, method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": chave})
    espera = 10
    for tentativa in range(6):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                dados = json.load(r)
            break
        except urllib.error.HTTPError as e:
            try:
                erro = json.load(e).get("error", {})
            except (json.JSONDecodeError, AttributeError):
                erro = {}
            msg = erro.get("message", "")
            if e.code == 429 and "limit: 0" in msg:
                raise SemCota(msg.splitlines()[0]) from None
            if e.code in (429, 500, 502, 503, 504) and tentativa < 5:
                m = re.search(r"retry in (\d+(?:\.\d+)?)s", msg)
                time.sleep(min(90, float(m.group(1)) + 1) if m else espera)
                espera = min(90, espera * 2)
                continue
            raise ErroGeracao(f"HTTP {e.code} {erro.get('status', '')}: {msg[:300]}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if tentativa < 5:
                time.sleep(espera)
                continue
            raise ErroGeracao(f"rede: {e}") from None

    bloqueio = (dados.get("promptFeedback") or {}).get("blockReason")
    if bloqueio:
        raise ErroGeracao(f"pedido bloqueado pelo filtro do Gemini ({bloqueio})")
    candidato = (dados.get("candidates") or [{}])[0]
    textos = []
    for parte in (candidato.get("content") or {}).get("parts", []):
        if "inlineData" in parte:
            mime = parte["inlineData"].get("mimeType", "image/png")
            return base64.b64decode(parte["inlineData"]["data"]), EXTENSOES.get(mime, "png"), dados.get("usageMetadata") or {}
        if parte.get("text"):
            textos.append(parte["text"])
    motivo = candidato.get("finishReason", "sem motivo")
    raise ErroGeracao(f"nenhuma imagem na resposta (finishReason={motivo}) {' '.join(textos)[:200]}".strip())


def inline(caminho: Path) -> dict:
    mime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[caminho.suffix[1:]]
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(caminho.read_bytes()).decode()}}


# ---------------------------------------------------------------- geração


class Gerador:
    def __init__(self, args):
        self.args = args
        self.pasta = args.pasta
        self.saida = self.pasta / "09-imagens"
        self.pasta_refs = self.saida / "referencias"
        self.manifesto_path = self.saida / "manifesto.json"
        self.trava = threading.Lock()
        self.sem_cota: str | None = None
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
        return ids[:MAX_REFERENCIAS_POR_CENA]

    def precisa(self, id_: str, pasta: Path, pedido: str) -> bool:
        if self.args.refazer:
            return True
        if arquivo_existente(pasta, id_) is None:
            return True
        return self.manifesto.get(id_, {}).get("pedido") != pedido  # prompt ou referências mudaram

    def gerar_um(self, id_: str, pasta: Path, partes: list[dict], pedido: str, proporcao: str, refs: list[str]) -> str:
        if self.sem_cota:
            return "pulado"
        inicio = time.time()
        try:
            dados, ext, uso = chamar_api(self.chave, self.args.modelo, partes, proporcao, self.args.tamanho)
        except SemCota as e:
            self.sem_cota = str(e)
            return "sem-cota"
        except ErroGeracao as e:
            with self.trava:
                self.manifesto[id_] = {"status": "erro", "origem": "ia", "erro": str(e), "modelo": self.args.modelo, "pedido": None}
            print(f"  erro {id_}: {e}", flush=True)
            return "erro"
        pasta.mkdir(parents=True, exist_ok=True)
        antigo = arquivo_existente(pasta, id_)
        if antigo:
            antigo.unlink()
        destino = pasta / f"{id_}.{ext}"
        destino.write_bytes(dados)
        with self.trava:
            custos.registrar(self.pasta, "imagem", self.args.modelo, uso, item=id_, tamanho=self.args.tamanho)
            self.manifesto[id_] = {"status": "ok", "origem": "ia", "arquivo": str(destino.relative_to(self.pasta)), "modelo": self.args.modelo,
                                   "pedido": pedido, "referencias": refs, "segundos": round(time.time() - inicio, 1),
                                   "gerado_em": datetime.now().isoformat(timespec="seconds")}
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
                                   "fonte": str(origem.relative_to(RAIZ)), "pedido": pedido,
                                   "gerado_em": datetime.now().isoformat(timespec="seconds")}
        self.salvar_manifesto()
        print(f"  ficha {id_}: copiada de {origem.relative_to(RAIZ)}", flush=True)

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
            pedido = hash_pedido(self.args.modelo, r["prompt"], r.get("negativo"))
            if r.get("imagem") and (RAIZ / r["imagem"]).is_file():
                # personagem da biblioteca: a imagem aprovada na skill criar-personagem já é a ficha
                pedido = hash_pedido("biblioteca", r["imagem"])
                if self.precisa(rid, self.pasta_refs, pedido) and not self.args.simular:
                    self.copiar_da_biblioteca(rid, RAIZ / r["imagem"], pedido)
                continue
            if self.precisa(rid, self.pasta_refs, pedido):
                texto = f"{r['prompt']}\n\nAvoid: {r.get('negativo') or 'text'}. Do not render any text, letters, labels or watermarks."
                tarefas_refs.append((rid, self.pasta_refs, [{"text": texto}], pedido, r.get("proporcao", "1:1"), []))

        tarefas_cenas = []
        for c in cenas:
            refs = self.refs_da_cena(c)
            pedido = hash_pedido(self.args.modelo, c["prompt_final"], c.get("negativo_final"), refs, self.proporcao)
            if self.precisa(c["id"], self.saida, pedido):
                tarefas_cenas.append((c, refs, pedido))

        print(f"modelo: {self.args.modelo} · proporção: {self.proporcao}")
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
        self.chave = carregar_chave()
        if not self.chave:
            print("erro: defina GEMINI_API_KEY ou crie .env na raiz do projeto", file=sys.stderr)
            return 2

        resultados = []
        with ThreadPoolExecutor(max_workers=self.args.paralelo) as pool:
            if tarefas_refs:
                print("fichas de referência:")
                resultados += list(pool.map(lambda t: self.gerar_um(*t), tarefas_refs))
                self.salvar_manifesto()
            if tarefas_cenas and not self.sem_cota:
                print("cenas:")

                def cena(t):
                    c, refs, pedido = t
                    partes, rotulos = [], []
                    for i, rid in enumerate(refs, 1):
                        arq = arquivo_existente(self.pasta_refs, rid)
                        if arq:
                            partes.append(inline(arq))
                            tipo = {"personagem": "character", "local": "location", "objeto": "prop"}[self.refs[rid]["tipo"]]
                            rotulos.append(f"image {len(rotulos) + 1} is the {tipo} reference for {self.refs[rid].get('nome', rid)}")
                    instrucao = ""
                    if rotulos:
                        instrucao = ("Reference images are attached: " + "; ".join(rotulos) + ". Keep every character's face, "
                                     "hair, body and clothing exactly as in their reference, and keep the location consistent. "
                                     "Do not copy the reference layout (plain background, turnaround views): compose a new scene.\n\n")
                    texto = (f"{instrucao}{c['prompt_final']}\n\nAvoid: {c.get('negativo_final') or 'text'}. "
                             "Do not render any text, letters, captions, subtitles or watermarks.")
                    return self.gerar_um(c["id"], self.saida, [{"text": texto}, *partes], pedido, self.proporcao, refs)

                resultados += list(pool.map(cena, tarefas_cenas))
        self.salvar_manifesto()

        if self.sem_cota:
            print("\nerro: o projeto do Google não tem cota para geração de imagem (plano gratuito: limite 0).")
            print(f"  detalhe: {self.sem_cota[:200]}")
            print("  solução: ative o faturamento do projeto em https://aistudio.google.com/apikey (Set up billing)")
            print("  enquanto isso: rode com --provisorio para montar o vídeo com quadros de rascunho")
            return 3
        ok, erros = resultados.count("ok"), resultados.count("erro")
        print(f"\ngeradas: {ok} · erros: {erros}")
        self.folha_contato()
        return 1 if erros else 0

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
    ap.add_argument("--modelo", help=f"modelo de imagem (padrão: o de visual.qualidade no pedido, senão {MODELO_PADRAO}; "
                                     "mais barato: gemini-3.1-flash-lite-image; melhor: gemini-3-pro-image)")
    ap.add_argument("--cenas", help="só estas cenas, ids separados por vírgula (ex.: B01-03,B02-01; a thumbnail é 'thumbnail')")
    ap.add_argument("--limite", type=int, help="só as N primeiras cenas (para testar)")
    ap.add_argument("--refazer", action="store_true", help="gera de novo mesmo o que já existe")
    ap.add_argument("--sem-referencias", action="store_true", help="não usa fichas de referência (mais barato, menos coerente)")
    ap.add_argument("--com-objetos", action="store_true", help="também gera e envia fichas de objetos")
    ap.add_argument("--tamanho", choices=["1K", "2K", "4K"], help="resolução (só modelos Gemini 3; padrão do modelo)")
    ap.add_argument("--paralelo", type=int, default=2, help="pedidos simultâneos (padrão 2)")
    ap.add_argument("--simular", action="store_true", help="só mostra o que seria gerado, sem chamar a API")
    ap.add_argument("--provisorio", action="store_true", help="quadros de rascunho locais, sem API")
    ap.add_argument("--so-folha", action="store_true", help="só refaz a folha de contato")
    args = ap.parse_args()
    if not (args.pasta / "08-cenas.json").exists():
        print(f"erro: {args.pasta / '08-cenas.json'} não existe; rode a direção visual e ferramentas/cenas.py antes", file=sys.stderr)
        return 2
    args.modelo = args.modelo or MODELOS_POR_QUALIDADE.get(config(ler_brief(args.pasta), "visual.qualidade"), MODELO_PADRAO)
    g = Gerador(args)
    if args.so_folha:
        g.folha_contato()
        return 0
    return g.executar()


if __name__ == "__main__":
    sys.exit(main())
