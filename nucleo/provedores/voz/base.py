"""Interface dos fornecedores de voz sintética (TTS) e as regras de voz que valem para todos.

Cada fornecedor (gemini.py, openai.py, elevenlabs.py, espeak.py) recebe o mesmo pedido: o texto, a voz e a
direção (perfil de quem fala e notas de estilo), e devolve WAV PCM de 16 bits, mono. O que cada um faz com a
direção é dele: o Gemini monta o prompt estruturado, a OpenAI manda em `instructions`, a ElevenLabs e o espeak
ignoram. Planejamento, cache, verificação por transcrição e ritmo ficam em ferramentas/producao/narrar.py,
iguais para todos.

A regra de troca de voz (`voz_equivalente`) também é comum: uma voz que o fornecedor não tem (a voz do canal é
do Gemini) vira uma dele do mesmo gênero, sempre a mesma para o mesmo nome. Assim dá para trocar de fornecedor
sem mexer no pedido, e o cache da narração não muda de uma execução para outra.
"""

from __future__ import annotations

import hashlib
import io
import re
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, ClassVar, Sequence

from nucleo.provedores.erros import ErroProvedor
from nucleo.provedores.uso import Uso

# Vozes prontas do Gemini TTS e a característica que o Google atribui a cada uma.
VOZES_GEMINI = {
    "Zephyr": "clara", "Puck": "animada", "Charon": "informativa", "Kore": "firme", "Fenrir": "empolgada",
    "Leda": "jovem", "Orus": "firme", "Aoede": "leve", "Callirrhoe": "tranquila", "Autonoe": "clara",
    "Enceladus": "sussurrada", "Iapetus": "nítida", "Umbriel": "tranquila", "Algieba": "suave", "Despina": "suave",
    "Erinome": "nítida", "Algenib": "rouca", "Rasalgethi": "informativa", "Laomedeia": "animada", "Achernar": "macia",
    "Alnilam": "firme", "Schedar": "uniforme", "Gacrux": "madura", "Pulcherrima": "direta", "Achird": "amigável",
    "Zubenelgenubi": "casual", "Vindemiatrix": "gentil", "Sadachbia": "viva", "Sadaltager": "conhecedora",
    "Sulafat": "calorosa",
}
# Gênero de cada voz, como aparece no seletor do AI Studio (f: feminina, m: masculina). Confira ouvindo.
GENERO_VOZES = {
    "Zephyr": "f", "Puck": "m", "Charon": "m", "Kore": "f", "Fenrir": "m", "Leda": "f", "Orus": "m", "Aoede": "f",
    "Callirrhoe": "f", "Autonoe": "f", "Enceladus": "m", "Iapetus": "m", "Umbriel": "m", "Algieba": "m",
    "Despina": "f", "Erinome": "f", "Algenib": "m", "Rasalgethi": "m", "Laomedeia": "f", "Achernar": "f",
    "Alnilam": "m", "Schedar": "m", "Gacrux": "f", "Pulcherrima": "f", "Achird": "m", "Zubenelgenubi": "m",
    "Vindemiatrix": "f", "Sadachbia": "m", "Sadaltager": "m", "Sulafat": "f",
}
# Vozes do TTS da OpenAI (/v1/audio/speech).
VOZES_OPENAI = {
    "marin": "natural, a melhor", "cedar": "natural, a melhor", "alloy": "neutra", "ash": "firme", "ballad": "suave",
    "coral": "calorosa", "echo": "clara", "fable": "expressiva", "nova": "animada", "onyx": "grave", "sage": "serena",
    "shimmer": "leve", "verse": "versátil",
}
GENERO_OPENAI = {"marin": "f", "cedar": "m", "alloy": "f", "ash": "m", "ballad": "m", "coral": "f", "echo": "m",
                 "fable": "m", "nova": "f", "onyx": "m", "sage": "f", "shimmer": "f", "verse": "m"}

TAXA_PADRAO = 24000


@dataclass(frozen=True)
class Voz:
    nome: str
    genero: str                 # "f" | "m" | "?"
    descricao: str = ""
    id: str | None = None       # ElevenLabs: o voice_id
    amostra_url: str | None = None


def vozes_de(descricoes: dict[str, str], generos: dict[str, str]) -> list[Voz]:
    return [Voz(nome, generos.get(nome, "?"), descricao) for nome, descricao in descricoes.items()]


def genero_de(voz: str, vozes: Sequence[Voz] = ()) -> str:
    """O gênero de uma voz de qualquer fornecedor conhecido; "m" quando não se sabe (como sempre foi)."""
    if voz in GENERO_VOZES:
        return GENERO_VOZES[voz]
    if voz in GENERO_OPENAI:
        return GENERO_OPENAI[voz]
    return next((v.genero for v in vozes if v.nome == voz and v.genero in ("f", "m")), "m")


def voz_equivalente(vozes: Sequence[Voz], voz: str) -> str:
    """A voz pedida, se o fornecedor a tem; senão uma dele do mesmo gênero, sempre a mesma para o mesmo nome
    (sha1 do nome sobre a lista ordenada das vozes do gênero)."""
    nomes = [v.nome for v in vozes]
    if voz in nomes:
        return voz
    por_nome = {n.lower(): n for n in nomes}
    if voz.lower() in por_nome:
        return por_nome[voz.lower()]
    genero = genero_de(voz, vozes)
    mesmas = sorted(v.nome for v in vozes if v.genero == genero) or sorted(nomes)
    if not mesmas:
        raise ErroProvedor(f"o fornecedor não tem vozes disponíveis para usar no lugar de '{voz}'")
    return mesmas[int(hashlib.sha1(voz.encode()).hexdigest(), 16) % len(mesmas)]


def para_wav(dados: bytes, mime: str) -> bytes:
    """WAV pronto passa direto; PCM cru de 16 bits mono (audio/L16;rate=24000) ganha o cabeçalho."""
    if dados[:4] == b"RIFF":
        return dados
    taxa = int(m.group(1)) if (m := re.search(r"rate=(\d+)", mime or "")) else TAXA_PADRAO
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(taxa)
        w.writeframes(dados)
    return buf.getvalue()


def duracao_bytes(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav), "rb") as w:
        return w.getnframes() / w.getframerate()


class ProvedorVoz(ABC):
    """Um fornecedor de voz. `ao_usar` recebe um `Uso` a cada chamada paga (o registro de custos do projeto)."""

    id: ClassVar[str]
    nome: ClassVar[str]
    modelo_padrao: ClassVar[str]
    voz_padrao: ClassVar[str | None]
    modelos: ClassVar[tuple[str, ...]]
    deterministico: ClassVar[bool] = False      # espeak: sem verificação nem novas tentativas
    aceita_instrucoes: ClassVar[bool] = True    # perfil/estilo (ElevenLabs e espeak não)
    precisa_chave: ClassVar[bool] = True

    def __init__(self, modelo: str | None = None, chave: str | None = None,
                 ao_usar: Callable[[Uso], None] | None = None):
        self.modelo = modelo or self.modelo_padrao
        self.chave = chave
        self.ao_usar = ao_usar
        # A língua do texto (código de nucleo.idiomas; None: português do Brasil). Os modelos de voz reconhecem a
        # língua pelo próprio texto; o espeak precisa dela para escolher a voz (a dublagem a define).
        self.idioma: str | None = None

    @classmethod
    def catalogo(cls) -> list[Voz]:
        """As vozes conhecidas sem rede nem chave (para validar pedidos e montar a tela)."""
        return []

    def vozes(self) -> list[Voz]:
        return self.catalogo()

    def voz_equivalente(self, voz: str) -> str:
        return voz_equivalente(self.vozes(), voz)

    def _usou(self, bruto: dict | None = None, *, caracteres: int | None = None, segundos: float | None = None) -> None:
        if self.ao_usar:
            self.ao_usar(Uso(servico="narracao", provedor=self.id, modelo=self.modelo, bruto=bruto or {},
                             caracteres=caracteres, segundos=segundos))

    @abstractmethod
    def sintetizar(self, texto: str, voz: str, perfil: str, estilo: str) -> bytes:
        """WAV PCM16 mono. Levanta SemCota, SemChave ou ErroProvedor."""
