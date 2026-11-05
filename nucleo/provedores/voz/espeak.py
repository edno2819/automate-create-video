"""espeak-ng, local e grátis.

A voz é robótica: serve para testar o vídeo inteiro (ritmo, falas, alinhamento, montagem) antes de pagar pela
voz de verdade. Aceita os nomes das vozes do Gemini (as do pedido), para trocar de fornecedor sem mexer nele:
cada voz vira uma variante do espeak do mesmo gênero, e vozes diferentes do mesmo gênero caem em variantes
diferentes (até dez de cada). O mesmo texto dá sempre o mesmo áudio: não adianta conferir nem repetir.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from nucleo.idiomas import voz_espeak
from nucleo.provedores.erros import ErroProvedor
from nucleo.provedores.voz.base import GENERO_VOZES, VOZES_GEMINI, ProvedorVoz, Voz, genero_de, vozes_de

VARIANTES = {"f": ["f3", "Andrea", "f1", "linda", "f4", "steph", "f2", "Annie", "f5", "belinda"],
             "m": ["m3", "Henrique", "m1", "Diogo", "m7", "antonio", "m2", "marcelo", "m6", "pedro"]}


def variante(voz: str) -> str:
    """A variante do espeak de uma voz: pela posição dela entre as vozes do Gemini do mesmo gênero; uma voz de
    fora da lista cai numa variante do gênero dela pelo sha1 do nome."""
    genero = genero_de(voz)
    mesmas = sorted(v for v, g in GENERO_VOZES.items() if g == genero)
    posicao = mesmas.index(voz) if voz in mesmas else int(hashlib.sha1(voz.encode()).hexdigest(), 16)
    return VARIANTES[genero][posicao % len(VARIANTES[genero])]


class EspeakTTS(ProvedorVoz):
    id = "espeak"
    nome = "espeak-ng (local, grátis)"
    modelo_padrao = "espeak-ng"
    voz_padrao = "Charon"
    modelos = ("espeak-ng",)
    deterministico = True
    aceita_instrucoes = False
    precisa_chave = False

    def __init__(self, modelo=None, chave=None, ao_usar=None):
        if not shutil.which("espeak-ng"):
            raise ErroProvedor("espeak-ng não encontrado (instale com: sudo apt install espeak-ng)", self.id)
        super().__init__(self.modelo_padrao, None, ao_usar)

    @classmethod
    def catalogo(cls) -> list[Voz]:
        return vozes_de(VOZES_GEMINI, GENERO_VOZES)

    def sintetizar(self, texto: str, voz: str, perfil: str, estilo: str) -> bytes:
        # A variante já é a equivalência do espeak (qualquer nome cai numa variante do mesmo gênero).
        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp) / "fala.wav"
            r = subprocess.run(["espeak-ng", "-v", f"{voz_espeak(self.idioma)}+{variante(voz)}", "-s", "160", "-w",
                                str(destino), texto],
                               capture_output=True, text=True)
            if r.returncode != 0 or not destino.exists():
                raise ErroProvedor(f"espeak-ng: {(r.stderr or '').strip()[:200]}", self.id)
            return destino.read_bytes()
