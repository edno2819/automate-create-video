"""O trabalhador: o processo que roda as etapas que a web põe na fila.

    python -m nucleo.trabalhador                  # TRABALHADOR_CONCORRENCIA execuções de cada vez (padrão 2)
    python -m nucleo.trabalhador --concorrencia 1

O servidor web só enfileira; quem chama os agentes e os scripts (ffmpeg, whisper) é este processo, que pode rodar
em outra máquina e em mais de uma cópia: cada execução é pega do banco por um só trabalhador (FOR UPDATE SKIP
LOCKED), os arquivos chegam e voltam pelo armazenamento (MinIO ou S3) e o cancelamento chega pelo banco. Ao
receber SIGTERM (docker stop), ele para de pegar execuções e interrompe as que estão rodando, que ficam como
`interrompida`. A lógica está em nucleo/pipeline/execucao.py.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from nucleo import banco
from nucleo.armazenamento import armazenamento
from nucleo.config import config
from nucleo.pipeline.execucao import Trabalhador


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--concorrencia", type=int, help="execuções ao mesmo tempo (padrão: TRABALHADOR_CONCORRENCIA, ou 2)")
    ap.add_argument("--nome", help="nome do trabalhador no banco (padrão: máquina:pid)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    banco.iniciar()
    armazenamento().garantir_bucket()
    trabalhador = Trabalhador(args.concorrencia or config().concorrencia, args.nome)

    def parar(sinal, _quadro) -> None:
        logging.getLogger("nucleo.trabalhador").info("sinal %s: parando", sinal)
        trabalhador.parar()

    signal.signal(signal.SIGTERM, parar)
    signal.signal(signal.SIGINT, parar)
    print(f"trabalhador {trabalhador.nome}: {trabalhador.concorrencia} execução(ões) de cada vez", flush=True)
    trabalhador.rodar_para_sempre()
    return 0


if __name__ == "__main__":
    sys.exit(main())
