#!/usr/bin/env python3
"""Anima as imagens das cenas com um modelo de vídeo (Gemini Omni Flash por padrão), quando o pedido liga
visual.animacao (destaques ou todas): cada clipe parte da imagem da cena, com um movimento leve, e a montagem o põe
no lugar do zoom sobre a imagem parada.

Lê 08-cenas.json, as imagens de 09-imagens/ e, se houver, os tempos reais da narração (09-narracao/tempos.json), para
pedir cada clipe com a duração da cena (de 3 a 10 s; a montagem corta o que sobra e estica um pouco o que falta).
Grava 09-animacoes/<id>.mp4 e 09-animacoes/manifesto.json, a cada clipe pronto.

Quais cenas, o texto enviado ao modelo (igual em qualquer provedor) e quando um clipe ainda vale ficam em
ferramentas/animacao.py. Um clipe pronto é pulado enquanto a imagem, o movimento, o modelo e a resolução forem os
mesmos e ele cobrir a cena (--refazer gera de novo). O pedido que o filtro recusou fica com "status": "bloqueado" e a
cena, com a imagem parada: o mesmo pedido não é repetido sozinho. As chamadas rodam em paralelo (nucleo.paralelo),
até --paralelo de cada vez. O custo de cada clipe vai para custos.jsonl e o banco (serviço "animacao", pelo segundo
de vídeo gerado).

Quando a cena seguinte entra com uma transição de vídeo ("transicao": whip-pan, zoom-blur, match-cut, push, wipe ou
glitch), o clipe leva a imagem dela como último quadro e termina nela; se essa imagem muda, o clipe é feito de novo.

Uso:
    python3 ferramentas/producao/animar_imagens.py projetos/<slug> --simular
    python3 ferramentas/producao/animar_imagens.py projetos/<slug>
    python3 ferramentas/producao/animar_imagens.py projetos/<slug> --cenas B01-02,B03-01 --refazer
    python3 ferramentas/producao/animar_imagens.py projetos/<slug> --modo todas --resolucao 1080p

Códigos de saída: 0 ok (ou nada a animar); 1 erro ou bloqueio em alguma cena; 2 sem 08-cenas.json, sem chave ou sem
ffprobe; 3 sem cota no provedor.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "ferramentas"))
import animacao  # noqa: E402
from brief import ler_brief  # noqa: E402
import custos  # noqa: E402
from nucleo import paralelo  # noqa: E402
import nucleo.provedores.video as provedores_video  # noqa: E402
from nucleo.provedores.erros import Bloqueado, ErroProvedor, SemChave, SemCota  # noqa: E402
from nucleo.provedores.imagem.base import Referencia  # noqa: E402
from nucleo.provedores.video.base import PedidoVideo  # noqa: E402

PROVEDOR_PADRAO = "gemini"
PARALELO_PADRAO = 4


def duracao_midia(caminho: Path) -> float | None:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(caminho)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


class Animador:
    def __init__(self, args, doc: dict):
        self.args = args
        self.pasta = args.pasta
        self.saida = self.pasta / animacao.PASTA
        self.manifesto_path = self.pasta / animacao.MANIFESTO
        self.manifesto = animacao.ler_json(self.manifesto_path)
        self.proporcao = doc.get("proporcao", "16:9")
        self.cenas = doc["cenas"]
        self.trava = threading.Lock()
        self.sem_cota: str | None = None
        self.provedor = None  # criado só na hora de animar: --simular não precisa de chave

    def salvar_manifesto(self) -> None:
        with self.trava:
            self.saida.mkdir(parents=True, exist_ok=True)
            self.manifesto_path.write_text(json.dumps(self.manifesto, ensure_ascii=False, indent=2), encoding="utf-8")

    def precisa(self, id_: str, pedido: str, duracao: float) -> bool:
        if self.args.refazer:
            return True
        r = self.manifesto.get(id_) or {}
        if r.get("status") == "bloqueado":
            return r.get("pedido") != pedido  # o filtro recusaria de novo: só um pedido diferente vale a tentativa
        if r.get("status") != "ok" or r.get("pedido") != pedido or not (self.pasta / str(r.get("arquivo") or "")).is_file():
            return True
        return float(r.get("duracao_s") or 0) + animacao.FOLGA_S < min(duracao, animacao.MAX_SEGUNDOS)

    def animar_um(self, c: dict, imagem: Path, segundos: int, pedido: str, transicao: str | None = None,
                  proxima: Path | None = None) -> str:
        if self.sem_cota:
            return "pulado"
        id_, inicio = c["id"], time.time()
        base = {"modelo": self.args.modelo, "provedor": self.args.provedor, "resolucao": self.args.resolucao,
                "pedido": pedido, "imagem": animacao.sha_arquivo(imagem), "segundos_pedidos": segundos}
        if transicao:
            base.update(transicao=transicao, ultimo_quadro=animacao.sha_arquivo(proxima))
        try:
            gerado = self.provedor.animar(PedidoVideo(animacao.prompt(c, segundos, transicao), Referencia.de_arquivo(imagem),
                                                      self.proporcao, self.args.resolucao, segundos,
                                                      Referencia.de_arquivo(proxima) if transicao else None))
        except SemCota as e:
            self.sem_cota = str(e)
            return "sem-cota"
        except Bloqueado as e:
            with self.trava:
                self.manifesto[id_] = {**base, "status": "bloqueado", "erro": str(e)}
            print(f"  bloqueado {id_}: {e}", flush=True)
            return "bloqueado"
        except ErroProvedor as e:
            with self.trava:
                self.manifesto[id_] = {**base, "status": "erro", "erro": str(e), "pedido": None}
            print(f"  erro {id_}: {e}", flush=True)
            return "erro"
        self.saida.mkdir(parents=True, exist_ok=True)
        destino = self.saida / f"{id_}.mp4"
        temporario = destino.with_name(f".{destino.name}.parcial")  # o espaço ignora: a sincronização não leva pela metade
        temporario.write_bytes(gerado.dados)
        duracao = duracao_midia(temporario)
        if not duracao:
            temporario.unlink(missing_ok=True)
            with self.trava:
                self.manifesto[id_] = {**base, "status": "erro", "erro": "o vídeo devolvido não abre", "pedido": None}
            print(f"  erro {id_}: o vídeo devolvido não abre", flush=True)
            return "erro"
        os.replace(temporario, destino)
        with self.trava:
            custos.registrar(self.pasta, "animacao", self.args.modelo, gerado.uso, provedor=self.args.provedor, item=id_,
                             segundos=round(duracao, 2), tamanho=self.args.resolucao)
            self.manifesto[id_] = {**base, "status": "ok", "arquivo": f"{animacao.PASTA}/{destino.name}",
                                   "duracao_s": round(duracao, 2), "segundos": round(time.time() - inicio, 1),
                                   "gerado_em": datetime.now().isoformat(timespec="seconds")}
        print(f"  ok {id_} ({duracao:.1f} s de vídeo em {time.time() - inicio:.0f} s)", flush=True)
        return "ok"

    def executar(self) -> int:
        cls = provedores_video.classe(self.args.provedor)
        if self.proporcao not in cls.proporcoes:
            print(f"aviso: a animação ({cls.nome}) só existe em {' e '.join(cls.proporcoes)}; o vídeo em {self.proporcao} "
                  "fica com o zoom sobre as imagens paradas")
            return 0
        manifesto_imagens = animacao.ler_json(self.pasta / "09-imagens" / "manifesto.json")
        cenas = animacao.selecionadas(self.cenas, self.args.modo, manifesto_imagens)
        if self.args.cenas:
            ids = set(self.args.cenas.split(","))
            cenas = [c for c in cenas if c["id"] in ids]
        duracoes = animacao.duracoes(self.pasta, self.cenas)
        tarefas, sem_imagem, prontas = [], [], 0
        posicao = {c["id"]: k for k, c in enumerate(self.cenas)}
        for c in cenas:
            imagem = animacao.imagem_da_cena(self.pasta, c["id"])
            duracao = duracoes.get(c["id"]) or float(c.get("duracao_s") or animacao.MIN_SEGUNDOS)
            segundos = animacao.segundos_pedidos(duracao)
            if imagem is None and not self.args.simular:
                sem_imagem.append(c["id"])
                continue
            # a transição de vídeo para a cena seguinte: o clipe termina na imagem dela (sem ela, fica um corte)
            transicao = animacao.transicao_de_saida(self.cenas, posicao[c["id"]])
            proxima = animacao.imagem_da_cena(self.pasta, self.cenas[posicao[c["id"]] + 1]["id"]) if transicao else None
            if transicao and proxima is None and not self.args.simular:
                print(f"aviso: {c['id']}: a cena seguinte ainda não tem imagem real; a transição {transicao} vira corte")
                transicao = None
            pedido = animacao.hash_pedido(self.args.modelo, self.args.resolucao, self.proporcao,
                                          animacao.sha_arquivo(imagem) if imagem else None, animacao.movimento(c),
                                          transicao, animacao.sha_arquivo(proxima) if proxima else None)
            if imagem is None or self.precisa(c["id"], pedido, duracao):  # na simulação, a imagem ainda vai ser gerada
                tarefas.append((c, imagem, segundos, pedido, transicao, proxima))
            else:
                prontas += 1

        total_s = sum(t[2] for t in tarefas)
        por_segundo = custos.preco_por_segundo(self.args.modelo, self.args.resolucao)
        estimativa = (f"estimado: US$ {por_segundo * total_s:.2f}" if por_segundo is not None
                      else f"sem preço em biblioteca/precos.json para {self.args.resolucao}")
        print(f"provedor: {self.args.provedor} · modelo: {self.args.modelo} · {self.args.resolucao} · {self.proporcao} · "
              f"modo: {self.args.modo}")
        print(f"a animar: {len(tarefas)} cena(s) · {total_s} s de vídeo · {estimativa} (já prontas: {prontas})")
        if sem_imagem:
            print(f"aviso: {len(sem_imagem)} cena(s) sem imagem real ficam paradas: {', '.join(sem_imagem[:10])}")
        if self.args.simular:
            for c, _, segundos, _, transicao, _ in tarefas:
                print(f"  cena {c['id']} · {segundos} s · {animacao.movimento(c)[:90]}"
                      + (f" · termina em {transicao}" if transicao else ""))
            return 0
        if not tarefas:
            return 0
        if not shutil.which("ffprobe"):
            print("erro: ffprobe não encontrado (vem com o ffmpeg): sem ele não dá para conferir os clipes", file=sys.stderr)
            return 2
        try:
            self.provedor = provedores_video.criar(self.args.provedor, self.args.modelo)
        except SemChave as e:
            print(f"erro: {e}", file=sys.stderr)
            return 2

        print(f"animando até {self.args.paralelo} cena(s) ao mesmo tempo")
        resultados = paralelo.rodar([paralelo.Tarefa(t[0]["id"], lambda t=t: self.animar_um(*t)) for t in tarefas],
                                    self.args.paralelo, lambda *_: self.salvar_manifesto())
        self.salvar_manifesto()
        if self.sem_cota:
            print(f"erro: o provedor de vídeo está sem cota ou sem crédito: {self.sem_cota[:200]}")
            return 3
        valores = list(resultados.values())
        ok, erros, bloqueadas = valores.count("ok"), valores.count("erro"), valores.count("bloqueado")
        print(f"\nanimadas: {ok} · erros: {erros}" + (f" · bloqueadas pelo filtro: {bloqueadas}" if bloqueadas else ""))
        return 1 if erros or bloqueadas else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pasta", type=Path, help="pasta do projeto (projetos/<slug>)")
    ap.add_argument("--provedor", choices=provedores_video.PROVEDORES, default=PROVEDOR_PADRAO,
                    help=f"provedor de vídeo (padrão {PROVEDOR_PADRAO})")
    ap.add_argument("--modelo", help="modelo de vídeo (padrão: o do provedor)")
    ap.add_argument("--resolucao", help="720p (padrão), 1080p ou 4k (as duas últimas são ampliadas a partir de 720p)")
    ap.add_argument("--modo", choices=animacao.MODOS, help="destaques ou todas (padrão: visual.animacao do pedido)")
    ap.add_argument("--cenas", help="só estas cenas, ids separados por vírgula")
    ap.add_argument("--refazer", action="store_true", help="anima de novo mesmo o que já tem clipe")
    ap.add_argument("--paralelo", type=int, default=PARALELO_PADRAO,
                    help=f"chamadas simultâneas ao provedor (padrão {PARALELO_PADRAO}; o fluxo passa o de Configurações)")
    ap.add_argument("--simular", action="store_true", help="só mostra o que seria animado e a estimativa, sem chamar a API")
    args = ap.parse_args()
    caminho = args.pasta / "08-cenas.json"
    if not caminho.exists():
        print(f"erro: {caminho} não existe; rode a direção visual antes", file=sys.stderr)
        return 2
    cls = provedores_video.classe(args.provedor)
    args.modelo = args.modelo or cls.modelo_padrao
    args.resolucao = (args.resolucao or cls.resolucao_padrao).lower()
    if args.resolucao not in cls.resolucoes:
        print(f"erro: resolução {args.resolucao}; o {cls.nome} gera {', '.join(cls.resolucoes)}", file=sys.stderr)
        return 2
    args.modo = args.modo or animacao.modo(ler_brief(args.pasta))
    if args.modo == "nenhuma":
        print("a animar: 0 cena(s) · 0 s de vídeo · a animação está desligada no pedido (visual.animacao: nenhuma)")
        return 0
    return Animador(args, json.loads(caminho.read_text(encoding="utf-8"))).executar()


if __name__ == "__main__":
    sys.exit(main())
