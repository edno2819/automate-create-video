"""Interface comum dos LLMs (Claude, GPT e Gemini): conversa com ferramentas, busca na web e lista de modelos.

Cada provedor guarda o histórico no formato da própria API e expõe a mesma interface, para que o motor de
agentes (nucleo/agentes/motor.py) rode qualquer agente em qualquer provedor sem saber de qual se trata:

    prov = nucleo.provedores.llm("openai")
    conversa = prov.conversa("gpt-6.1-sol", sistema, ferramentas)
    conversa.usuario("Leia o brief e escreva o dossiê.")
    passo = conversa.passo()               # texto, chamadas de ferramenta e uso de tokens
    conversa.resultados([Resultado(...)])  # a resposta de cada chamada, e o próximo passo

As ferramentas são declaradas uma vez, em JSON Schema ({"nome", "descricao", "parametros"}), e cada conversa
traduz. O texto que vai para o modelo (sistema, mensagens, resultados) é o mesmo byte a byte em qualquer
provedor: o adaptador só muda o envelope da API. O uso volta normalizado (entrada sem cache, saida,
cache_leitura, cache_escrita, buscas) para o custo em ferramentas/custos.py.
"""

from __future__ import annotations

import base64
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Sequence

from nucleo.provedores.uso import somar_uso, uso_vazio  # noqa: F401 - reexportados pelo pacote

MAX_SAIDA = 16000
TIMEOUT_LISTA_S = 30

# A pergunta da busca na web é a mesma em todos os provedores: muda só a ferramenta nativa de cada um.
PEDIDO_BUSCA = ("Pesquise na web e responda em português com o que encontrar sobre a consulta abaixo: fatos, números, "
                "datas e nomes, cada um com a URL da fonte. Diga quando não achar nada confiável. Consulta: {consulta}")

# Modelos que a listagem das APIs traz e que não servem para conversa com ferramentas.
FORA_DO_CHAT = re.compile(r"embed|tts|audio|realtime|transcri|whisper|image|imagen|dall-e|moderation|veo|aqa|"
                          r"search|computer-use|live|robotics|native-audio|instruct|babbage|davinci|sora|banana", re.I)


@dataclass
class Chamada:
    id: str
    nome: str
    args: dict


@dataclass
class Resultado:
    id: str
    nome: str
    texto: str
    imagens: list[tuple[str, bytes]] = field(default_factory=list)  # (mime, bytes)


@dataclass
class Passo:
    texto: str
    chamadas: list[Chamada]
    uso: dict
    cortado: bool = False  # a resposta parou no limite de tokens de saída


def b64(dados: bytes) -> str:
    return base64.b64encode(dados).decode()


def com_fontes(texto: str, fontes: dict[str, str]) -> str:
    """O texto da busca seguido da lista de fontes (url -> título), sem repetir url nem deixar url vazia."""
    fontes = {url: titulo for url, titulo in fontes.items() if url}
    if fontes:
        texto += "\n\nFontes:\n" + "\n".join(f"- {titulo or url}: {url}" for url, titulo in fontes.items())
    return texto.strip() or "(a busca não devolveu nada)"


class Conversa(ABC):
    """Uma conversa com ferramentas. O histórico fica no formato da API do provedor."""

    def __init__(self, chave: str, modelo: str, sistema: str, ferramentas: list[dict], max_saida: int = MAX_SAIDA):
        self.chave, self.modelo, self.sistema, self.ferramentas = chave, modelo, sistema, ferramentas
        self.max_saida = max_saida

    @abstractmethod
    def usuario(self, texto: str, imagens: Sequence[tuple[str, bytes]] = ()) -> None:
        """Acrescenta uma mensagem do usuário (com imagens, se houver)."""

    @abstractmethod
    def passo(self) -> Passo:
        """Chama a API com o histórico e acrescenta a resposta do modelo a ele."""

    @abstractmethod
    def resultados(self, lista: list[Resultado]) -> None:
        """Acrescenta as respostas das ferramentas pedidas no último passo."""


class ProvedorLLM(ABC):
    id: ClassVar[str]
    nome: ClassVar[str]

    def __init__(self, chave: str):
        self.chave = chave
        self.modelos: list[str] | None = None  # a lista do último testar(), para não chamar a API duas vezes

    @abstractmethod
    def conversa(self, modelo: str, sistema: str, ferramentas: list[dict], max_saida: int = MAX_SAIDA) -> Conversa:
        ...

    @abstractmethod
    def buscar_na_web(self, modelo: str, consulta: str) -> tuple[str, dict]:
        """Uma busca com a ferramenta nativa do provedor. Devolve (texto com as fontes, uso)."""

    @abstractmethod
    def listar_modelos(self) -> list[str]:
        """Os modelos de texto que a chave acessa, do mais novo ao mais antigo (pela ordem da API)."""

    def testar(self) -> str:
        """Uma chamada barata que só funciona com a chave válida: a lista de modelos (que a tela de modelos usa).
        Levanta ErroProvedor (ou SemCota) se a chave não serve."""
        self.modelos = self.listar_modelos()
        return f"{len(self.modelos)} modelos de texto disponíveis"
