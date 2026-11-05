"""As línguas em que um vídeo pode ser dublado (a etapa Edição: ferramentas/producao/dublar.py).

Cada língua tem:
- o nome na tela;
- como pedir a pronúncia à voz sintética ("inglês americano"), que vai no perfil do narrador;
- o código do whisper, que confere o que a voz disse e mede o tempo de cada palavra para as legendas;
- a voz do espeak, para a dublagem de teste (de graça);
- quantos caracteres por segundo uma voz sintética fala nela, em média. A primeira tradução usa esse número para
  caber no tempo da frase original; depois da primeira síntese, vale o que a voz escolhida falou de fato.

Ficam de fora as línguas escritas sem espaço entre as palavras (japonês, chinês): as legendas e a conferência pelo
whisper casam palavra por palavra.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Idioma:
    codigo: str
    nome: str
    lingua: str          # como a voz deve falar: "inglês americano"
    whisper: str
    espeak: str
    cps: float           # caracteres por segundo (com os espaços) de uma voz sintética no ritmo natural


IDIOMAS: dict[str, Idioma] = {i.codigo: i for i in (
    Idioma("en", "Inglês", "inglês americano", "en", "en-us", 14.0),
    Idioma("es", "Espanhol", "espanhol latino-americano neutro", "es", "es-419", 15.0),
    Idioma("fr", "Francês", "francês da França", "fr", "fr-fr", 14.5),
    Idioma("de", "Alemão", "alemão padrão", "de", "de", 14.0),
    Idioma("it", "Italiano", "italiano padrão", "it", "it", 15.0),
    Idioma("pt-PT", "Português de Portugal", "português europeu", "pt", "pt", 14.5),
    Idioma("pt-BR", "Português do Brasil", "português do Brasil, sotaque neutro", "pt", "pt-br", 15.0),
    Idioma("nl", "Holandês", "holandês", "nl", "nl", 14.0),
    Idioma("pl", "Polonês", "polonês", "pl", "pl", 13.5),
    Idioma("ru", "Russo", "russo", "ru", "ru", 13.5),
    Idioma("tr", "Turco", "turco", "tr", "tr", 14.0),
    Idioma("id", "Indonésio", "indonésio", "id", "id", 14.5),
    Idioma("hi", "Hindi", "hindi", "hi", "hi", 12.5),
)}
ORIGINAL_PADRAO = "pt-BR"  # o idioma do pedido, quando ele não diz


def achar(codigo: str | None) -> Idioma | None:
    """A língua pelo código, sem diferença de maiúsculas ("pt-br", "EN"); "en-US" cai em "en" quando a variante
    não está na lista."""
    if not codigo:
        return None
    por_minusculo = {c.lower(): i for c, i in IDIOMAS.items()}
    codigo = codigo.strip().replace("_", "-").lower()
    return por_minusculo.get(codigo) or por_minusculo.get(codigo.split("-")[0])


def mesmo(a: str | None, b: str | None) -> bool:
    """As duas são a mesma língua da lista (pt-BR e pt-PT não são)."""
    ia, ib = achar(a), achar(b)
    return ia is not None and ia == ib


def nome(codigo: str | None) -> str:
    idioma = achar(codigo)
    return idioma.nome if idioma else (codigo or "?")


def voz_espeak(codigo: str | None) -> str:
    """A voz do espeak para o texto nesta língua (sem língua: português do Brasil, como sempre foi)."""
    idioma = achar(codigo)
    return idioma.espeak if idioma else "pt-br"


def lista() -> list[dict]:
    """As línguas para a tela."""
    return [{"id": i.codigo, "nome": i.nome} for i in IDIOMAS.values()]
