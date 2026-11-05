"""Que modelo roda cada agente de cada etapa, e que provedor faz as imagens, a animação, a narração e o alinhamento.

A configuração fica no banco (configuracoes["modelos"]) e só guarda o que o usuário mudou; ler() devolve tudo
com os padrões preenchidos:

    {
      "padroes":  {"anthropic": {"julgamento": "claude-opus-5-5", "coleta": "claude-sonnet-5-5"}, ...},
      "etapas":   {"roteiro": {"provedor": "anthropic", "modelo": ""}, ...},
      "agentes":  {"critico": {"provedor": "openai", "modelo": "gpt-6.1-sol"}, ...},
      "busca":    {"provedor": "", "modelo": ""},
      "imagem":   {"provedor": "gemini", "modelos": {"rascunho": "", "padrao": "", "maxima": ""}, "tamanho": "1K",
                   "paralelo": 6},
      "animacao": {"provedor": "gemini", "modelo": "", "resolucao": "720p", "paralelo": 4},
      "narracao": {"fornecedor": "gemini", "modelos": {"gemini": "", "openai": "", "elevenlabs": "", "espeak": ""}},
      "alinhamento": {"modelo": "small"}
    }

Modelo vazio = o padrão. A rota de um agente numa etapa: agentes[agente] (quando tem provedor) > etapas[etapa]
(provedor; modelo vazio → padroes[provedor][nível do agente]) > o primeiro provedor de LLM com chave (anthropic,
openai, gemini). O nível vem do frontmatter do agente: julgamento (escrever, avaliar, dirigir) ou coleta
(pesquisar, formatar, publicar), o antigo opus/sonnet. Uma entrada de agente sem provedor herda tudo da etapa.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

from nucleo.agentes.motor import Rota

log = logging.getLogger(__name__)

CHAVE_CONFIG = "modelos"
NIVEIS = ("julgamento", "coleta")
QUALIDADES = ("rascunho", "padrao", "maxima")
WHISPER = ("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo", "turbo", "distil-large-v3")
TAMANHOS_IMAGEM = ("1K", "2K", "4K")
PARALELO_IMAGEM = 6       # chamadas simultâneas ao provedor de imagem (o 429 é tratado com espera, em http.py)
PARALELO_ANIMACAO = 4     # cada clipe leva um ou dois minutos: poucos de cada vez já cortam bem o tempo
MAX_PARALELO = 16

PADROES_LLM = {
    "anthropic": {"julgamento": "claude-opus-5-5", "coleta": "claude-sonnet-5-5"},
    "openai": {"julgamento": "gpt-6.1-sol", "coleta": "gpt-6-luna"},
    "gemini": {"julgamento": "gemini-4-argon", "coleta": "gemini-3.8-flash"},
}
# reserva, enquanto os provedores de imagem e voz não carregam (os valores de verdade vêm das classes)
_RESERVA_IMAGEM = {
    "gemini": {"modelo_padrao": "gemini-3.1-flash-image",
               "modelos_por_qualidade": {"rascunho": "gemini-3.1-flash-lite-image", "padrao": "gemini-3.1-flash-image",
                                         "maxima": "gemini-3-pro-image"}},
    "openai": {"modelo_padrao": "gpt-image-1",
               "modelos_por_qualidade": {"rascunho": "gpt-image-1-mini", "padrao": "gpt-image-1", "maxima": "gpt-image-1"}},
}
_RESERVA_VIDEO = {"gemini": {"modelo_padrao": "gemini-omni-1.1-flash", "resolucao_padrao": "720p",
                             "resolucoes": ("720p", "1080p", "4k")}}
_RESERVA_VOZ = {"gemini": "gemini-3.8-flash-tts", "openai": "gpt-4o-mini-tts", "elevenlabs": "eleven_multilingual_v2",
                "espeak": "espeak-ng"}

ETAPAS: dict[str, dict] = {
    "sugestoes": {"nome": "Sugestões do pedido", "agentes": ["orquestrador-pedido"]},
    "pesquisa": {"nome": "Pesquisa", "agentes": ["analista-referencias", "pesquisador-conteudo", "pesquisador-trends"]},
    "angulos": {"nome": "Ângulos", "agentes": ["estrategista"]},
    "ganchos": {"nome": "Ganchos", "agentes": ["redator-ganchos", "leitor-frio", "critico"]},
    "roteiro": {"nome": "Roteiro e avaliação", "agentes": ["roteirista", "leitor-frio", "critico"]},
    "visual": {"nome": "Direção visual", "agentes": ["diretor-visual"]},
    "producao": {"nome": "Produção e publicação", "agentes": ["revisor-visual", "diretor-visual", "publicador"]},
    "edicao": {"nome": "Edição (dublagem)", "agentes": ["tradutor"]},
    "importar": {"nome": "Importar roteiro", "agentes": ["formatador"]},
    "personagem": {"nome": "Criar personagem", "agentes": ["designer-personagem", "revisor-visual"]},
    "referencia": {"nome": "Analisar referência", "agentes": ["analista-referencias"]},
}
# execuções que usam a configuração de outra etapa
APELIDOS = {"avaliar": "roteiro", "publicacao": "producao", "refazer-cenas": "producao", "dublagem": "edicao",
            "exportar": "edicao"}


def _provedores() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    from nucleo import provedores
    return provedores.LLM, provedores.VOZ, provedores.IMAGEM


def etapa_base(etapa: str) -> str:
    base = APELIDOS.get(etapa, etapa)
    if base not in ETAPAS:
        raise ValueError(f"etapa desconhecida: {etapa}")
    return base


def nivel_do_agente(agente: str) -> str:
    from nucleo.agentes import registro
    try:
        return registro.carregar(agente).nivel
    except (LookupError, ValueError, OSError):
        return "julgamento"


def agentes_conhecidos() -> list[str]:
    from nucleo.agentes import registro
    return sorted(set(registro.nomes()) | {a for e in ETAPAS.values() for a in e["agentes"]})


# ---------------------------------------------------------------- padrões das mídias


def _info_imagem(provedor: str) -> dict:
    try:
        from nucleo import provedores
        cls = provedores.classe("imagem", provedor)
        return {"modelo_padrao": cls.modelo_padrao, "modelos_por_qualidade": dict(cls.modelos_por_qualidade)}
    except Exception:  # noqa: BLE001 - o provedor ainda não carrega: vale a reserva
        return copy.deepcopy(_RESERVA_IMAGEM.get(provedor, _RESERVA_IMAGEM["gemini"]))


def _video() -> tuple[str, ...]:
    from nucleo import provedores
    return provedores.VIDEO


def _info_video(provedor: str) -> dict:
    try:
        from nucleo import provedores
        cls = provedores.classe("video", provedor)
        return {"modelo_padrao": cls.modelo_padrao, "resolucao_padrao": cls.resolucao_padrao,
                "resolucoes": tuple(cls.resolucoes)}
    except Exception:  # noqa: BLE001 - o provedor ainda não carrega: vale a reserva
        return copy.deepcopy(_RESERVA_VIDEO.get(provedor, _RESERVA_VIDEO["gemini"]))


def _modelo_padrao_voz(fornecedor: str) -> str:
    try:
        from nucleo import provedores
        return provedores.classe("voz", fornecedor).modelo_padrao
    except Exception:  # noqa: BLE001
        return _RESERVA_VOZ.get(fornecedor, "")


def _whisper_padrao() -> str:
    try:
        from nucleo.config import config
        return config().whisper_modelo or "small"
    except Exception:  # noqa: BLE001
        return "small"


def padrao() -> dict:
    """A configuração inteira só com os padrões."""
    llm, voz, imagem = _provedores()
    video = _video()
    return {
        "padroes": copy.deepcopy(PADROES_LLM),
        "etapas": {e: {"provedor": "", "modelo": ""} for e in ETAPAS},
        "agentes": {a: {"provedor": "", "modelo": ""} for a in agentes_conhecidos()},
        "busca": {"provedor": "", "modelo": ""},
        "imagem": {"provedor": "gemini" if "gemini" in imagem else imagem[0],
                   "modelos": {q: "" for q in QUALIDADES}, "tamanho": "1K", "paralelo": PARALELO_IMAGEM},
        "animacao": {"provedor": "gemini" if "gemini" in video else video[0], "modelo": "",
                     "resolucao": _info_video("gemini" if "gemini" in video else video[0])["resolucao_padrao"],
                     "paralelo": PARALELO_ANIMACAO},
        "narracao": {"fornecedor": "gemini" if "gemini" in voz else voz[0], "modelos": {f: "" for f in voz}},
        "alinhamento": {"modelo": _whisper_padrao()},
    }


# ---------------------------------------------------------------- leitura e gravação


def _ler_bruto() -> dict:
    """O que está gravado no banco (só o que o usuário mudou)."""
    try:
        from nucleo.banco import repositorios
        valor = repositorios.ler_config(CHAVE_CONFIG, {})
    except Exception as e:  # noqa: BLE001 - sem banco (um script avulso): valem os padrões
        log.warning("configuração de modelos indisponível (%s): usando os padrões", e)
        return {}
    if not isinstance(valor, dict):
        return {}
    if isinstance(valor.get("etapas"), dict):  # uma etapa que saiu do sistema (os cortes) não impede gravar o resto
        valor["etapas"] = {e: v for e, v in valor["etapas"].items() if e in ETAPAS}
    return valor


def _gravar_bruto(valor: dict) -> None:
    from nucleo.banco import repositorios
    repositorios.gravar_config(CHAVE_CONFIG, valor)


def _mesclar(base: dict, parcial: dict) -> dict:
    """Mescla profunda; None apaga a chave (volta ao padrão)."""
    for k, v in parcial.items():
        if v is None:
            base.pop(k, None)
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            _mesclar(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
    return base


def ler() -> dict:
    """A configuração com os padrões preenchidos."""
    return _mesclar(padrao(), _ler_bruto())


def _texto(valor: Any, onde: str) -> str:
    if not isinstance(valor, str):
        raise ValueError(f"{onde}: precisa ser texto")
    return valor.strip()


def _validar_escolha(valor: Any, onde: str, llm: tuple[str, ...], permitir_modelo: bool = True) -> dict:
    if not isinstance(valor, dict):
        raise ValueError(f"{onde}: precisa ser um objeto com provedor e modelo")
    sobra = set(valor) - {"provedor", "modelo"}
    if sobra:
        raise ValueError(f"{onde}: campo desconhecido {', '.join(sorted(sobra))}")
    saida = {}
    if "provedor" in valor:
        prov = _texto(valor["provedor"] or "", f"{onde}.provedor")
        if prov and prov not in llm:
            raise ValueError(f"{onde}.provedor: '{prov}' não é um provedor de LLM ({', '.join(llm)})")
        saida["provedor"] = prov
    if "modelo" in valor:
        if not permitir_modelo:
            raise ValueError(f"{onde}: só o provedor")
        saida["modelo"] = _texto(valor["modelo"] or "", f"{onde}.modelo")
    return saida


def _paralelo(valor: Any, onde: str) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int) or not 1 <= valor <= MAX_PARALELO:
        raise ValueError(f"{onde}: um número inteiro de 1 a {MAX_PARALELO}")
    return valor


def _validar(bruto: dict) -> dict:
    """Confere a configuração gravada (já mesclada) e devolve-a normalizada. ValueError com o que está errado."""
    llm, voz, imagem = _provedores()
    conhecidos = set(agentes_conhecidos())
    permitidas = {"padroes", "etapas", "agentes", "busca", "imagem", "animacao", "narracao", "alinhamento"}
    if sobra := set(bruto) - permitidas:
        raise ValueError(f"seção desconhecida: {', '.join(sorted(sobra))}")
    saida: dict = {}
    if "padroes" in bruto:
        saida["padroes"] = {}
        for prov, niveis in (bruto["padroes"] or {}).items():
            if prov not in llm:
                raise ValueError(f"padroes: '{prov}' não é um provedor de LLM")
            if not isinstance(niveis, dict) or set(niveis) - set(NIVEIS):
                raise ValueError(f"padroes.{prov}: use os níveis {', '.join(NIVEIS)}")
            saida["padroes"][prov] = {n: _texto(m, f"padroes.{prov}.{n}") for n, m in niveis.items()}
            if any(not m for m in saida["padroes"][prov].values()):
                raise ValueError(f"padroes.{prov}: o modelo padrão não pode ficar vazio")
    if "etapas" in bruto:
        saida["etapas"] = {}
        for etapa, escolha in (bruto["etapas"] or {}).items():
            if etapa not in ETAPAS:
                raise ValueError(f"etapas: etapa desconhecida '{etapa}'")
            saida["etapas"][etapa] = _validar_escolha(escolha, f"etapas.{etapa}", llm)
    if "agentes" in bruto:
        saida["agentes"] = {}
        for agente, escolha in (bruto["agentes"] or {}).items():
            if agente not in conhecidos:
                raise ValueError(f"agentes: agente desconhecido '{agente}'")
            saida["agentes"][agente] = _validar_escolha(escolha, f"agentes.{agente}", llm)
    if "busca" in bruto:
        saida["busca"] = _validar_escolha(bruto["busca"], "busca", llm)
    if "imagem" in bruto:
        img = bruto["imagem"] or {}
        if not isinstance(img, dict) or set(img) - {"provedor", "modelos", "tamanho", "paralelo"}:
            raise ValueError("imagem: use provedor, modelos, tamanho e paralelo")
        saida["imagem"] = {}
        if "provedor" in img:
            if img["provedor"] not in imagem:
                raise ValueError(f"imagem.provedor: use um de {', '.join(imagem)}")
            saida["imagem"]["provedor"] = img["provedor"]
        if "modelos" in img:
            if not isinstance(img["modelos"], dict) or set(img["modelos"]) - set(QUALIDADES):
                raise ValueError(f"imagem.modelos: use as qualidades {', '.join(QUALIDADES)}")
            saida["imagem"]["modelos"] = {q: _texto(m or "", f"imagem.modelos.{q}") for q, m in img["modelos"].items()}
        if "tamanho" in img:
            tamanho = _texto(img["tamanho"] or "", "imagem.tamanho").upper()
            if tamanho and not re.fullmatch(r"\d+K", tamanho):
                raise ValueError(f"imagem.tamanho: use {', '.join(TAMANHOS_IMAGEM)}")
            saida["imagem"]["tamanho"] = tamanho or "1K"
        if "paralelo" in img:
            saida["imagem"]["paralelo"] = _paralelo(img["paralelo"], "imagem.paralelo")
    if "animacao" in bruto:
        ani = bruto["animacao"] or {}
        if not isinstance(ani, dict) or set(ani) - {"provedor", "modelo", "resolucao", "paralelo"}:
            raise ValueError("animacao: use provedor, modelo, resolucao e paralelo")
        saida["animacao"] = {}
        provedor = ani.get("provedor")
        if "provedor" in ani:
            if provedor not in _video():
                raise ValueError(f"animacao.provedor: use um de {', '.join(_video())}")
            saida["animacao"]["provedor"] = provedor
        if "modelo" in ani:
            saida["animacao"]["modelo"] = _texto(ani["modelo"] or "", "animacao.modelo")
        if "resolucao" in ani:
            resolucoes = _info_video(provedor or "gemini")["resolucoes"]
            resolucao = _texto(ani["resolucao"] or "", "animacao.resolucao").lower()
            if resolucao and resolucao not in resolucoes:
                raise ValueError(f"animacao.resolucao: use {', '.join(resolucoes)}")
            saida["animacao"]["resolucao"] = resolucao
        if "paralelo" in ani:
            saida["animacao"]["paralelo"] = _paralelo(ani["paralelo"], "animacao.paralelo")
    if "narracao" in bruto:
        nar = bruto["narracao"] or {}
        if not isinstance(nar, dict) or set(nar) - {"fornecedor", "modelos"}:
            raise ValueError("narracao: use fornecedor e modelos")
        saida["narracao"] = {}
        if "fornecedor" in nar:
            if nar["fornecedor"] not in voz:
                raise ValueError(f"narracao.fornecedor: use um de {', '.join(voz)}")
            saida["narracao"]["fornecedor"] = nar["fornecedor"]
        if "modelos" in nar:
            if not isinstance(nar["modelos"], dict) or set(nar["modelos"]) - set(voz):
                raise ValueError(f"narracao.modelos: use os fornecedores {', '.join(voz)}")
            saida["narracao"]["modelos"] = {f: _texto(m or "", f"narracao.modelos.{f}") for f, m in nar["modelos"].items()}
    if "alinhamento" in bruto:
        ali = bruto["alinhamento"] or {}
        if not isinstance(ali, dict) or set(ali) - {"modelo"}:
            raise ValueError("alinhamento: só o modelo")
        if "modelo" in ali:
            modelo = _texto(ali["modelo"] or "", "alinhamento.modelo")
            if modelo and not re.fullmatch(r"[A-Za-z0-9._/-]+", modelo):
                raise ValueError("alinhamento.modelo: nome de modelo do faster-whisper (tiny, base, small, medium...)")
            saida["alinhamento"] = {"modelo": modelo}
    return saida


def _podar(d: dict) -> dict:
    """Tira do que vai para o banco o que é igual a vazio (as escolhas "herdar" e "padrão")."""
    saida = {}
    for k, v in d.items():
        if isinstance(v, dict):
            v = _podar(v)
            if v:
                saida[k] = v
        elif v not in ("", None):
            saida[k] = v
    return saida


def _sem_nulos(d: dict) -> dict:
    return {k: _sem_nulos(v) if isinstance(v, dict) else v for k, v in d.items() if v is not None}


def gravar(parcial: dict) -> dict:
    """Mescla `parcial` no que está gravado (null apaga = volta ao padrão), valida e devolve a configuração inteira."""
    if not isinstance(parcial, dict):
        raise ValueError("a configuração de modelos precisa ser um objeto")
    _validar(_sem_nulos(parcial))  # erro do pedido aparece com o caminho do pedido, antes de mesclar
    novo = _validar(_mesclar(copy.deepcopy(_ler_bruto()), parcial))
    _gravar_bruto(_podar(novo))
    return ler()


# ---------------------------------------------------------------- resolução


def _tem_chave(provedor: str) -> bool:
    try:
        from nucleo import chaves
        return bool(chaves.ler(provedor))
    except Exception:  # noqa: BLE001
        return False


def provedor_com_chave() -> str | None:
    llm, _, _ = _provedores()
    return next((p for p in llm if _tem_chave(p)), None)


def _resolver(cfg: dict, etapa: str, agente: str) -> Rota:
    base = etapa_base(etapa)
    nivel = nivel_do_agente(agente)
    escolha = cfg["agentes"].get(agente) or {}
    if not escolha.get("provedor"):
        escolha = cfg["etapas"].get(base) or {}
    provedor = escolha.get("provedor") or provedor_com_chave() or "anthropic"
    modelo = escolha.get("modelo") if escolha.get("provedor") else ""
    padroes = cfg["padroes"].get(provedor) or PADROES_LLM.get(provedor) or {}
    return Rota(provedor, modelo or padroes.get(nivel) or padroes.get("julgamento") or "")


def rota(etapa: str, agente: str) -> Rota:
    """O provedor e o modelo de um agente numa etapa (avaliar, publicacao e refazer-cenas usam a etapa base)."""
    return _resolver(ler(), etapa, agente)


def resolvidas() -> dict[str, dict[str, dict]]:
    """O modelo efetivo de cada agente em cada etapa, para a tela: {etapa: {agente: {provedor, modelo, nivel}}}."""
    cfg = ler()
    saida: dict[str, dict[str, dict]] = {}
    for etapa, info in ETAPAS.items():
        saida[etapa] = {}
        for agente in info["agentes"]:
            r = _resolver(cfg, etapa, agente)
            saida[etapa][agente] = {"provedor": r.provedor, "modelo": r.modelo, "nivel": nivel_do_agente(agente)}
    return saida


def rota_busca(rota_do_agente: Rota) -> Rota:
    """Quem faz a busca na web de um agente: o provedor de busca configurado ou o do próprio agente."""
    cfg = ler()["busca"]
    provedor = cfg.get("provedor") or ""
    if not provedor:
        return rota_do_agente
    if cfg.get("modelo"):
        return Rota(provedor, cfg["modelo"])
    if provedor == rota_do_agente.provedor:
        return rota_do_agente
    padroes = ler()["padroes"].get(provedor) or PADROES_LLM.get(provedor) or {}
    return Rota(provedor, padroes.get("coleta") or padroes.get("julgamento") or "")


def imagem(qualidade: str | None = None) -> tuple[str, str]:
    """(provedor, modelo) das imagens de IA para a qualidade do pedido (visual.qualidade: rascunho, padrao, maxima)."""
    cfg = ler()["imagem"]
    provedor = cfg.get("provedor") or "gemini"
    qualidade = qualidade if qualidade in QUALIDADES else "padrao"
    info = _info_imagem(provedor)
    modelo = ((cfg.get("modelos") or {}).get(qualidade) or (info["modelos_por_qualidade"] or {}).get(qualidade)
              or info["modelo_padrao"])
    return provedor, modelo


def tamanho_imagem() -> str:
    return ler()["imagem"].get("tamanho") or "1K"


def paralelo_imagem() -> int:
    return int(ler()["imagem"].get("paralelo") or PARALELO_IMAGEM)


def animacao() -> dict:
    """{provedor, modelo, resolucao, paralelo} da animação das imagens, com o modelo e a resolução padrão do provedor
    quando ficaram vazios."""
    cfg = ler()["animacao"]
    provedor = cfg.get("provedor") or "gemini"
    info = _info_video(provedor)
    return {"provedor": provedor, "modelo": cfg.get("modelo") or info["modelo_padrao"],
            "resolucao": cfg.get("resolucao") or info["resolucao_padrao"],
            "paralelo": int(cfg.get("paralelo") or PARALELO_ANIMACAO)}


def narracao(fornecedor_do_pedido: str | None) -> tuple[str, str]:
    """(fornecedor, modelo) da narração: o fornecedor do pedido vale mais (é escolha do vídeo); a configuração dá o
    padrão e o modelo de cada fornecedor."""
    _, voz, _ = _provedores()
    cfg = ler()["narracao"]
    fornecedor = fornecedor_do_pedido if fornecedor_do_pedido in voz else (cfg.get("fornecedor") or "gemini")
    modelo = (cfg.get("modelos") or {}).get(fornecedor) or _modelo_padrao_voz(fornecedor)
    return fornecedor, modelo


def modelo_whisper() -> str:
    return ler()["alinhamento"].get("modelo") or _whisper_padrao()


def _nome_provedor(provedor: str) -> str:
    try:
        from nucleo import chaves
        return chaves.PROVEDORES[provedor]["nome"]
    except Exception:  # noqa: BLE001
        return provedor


def problemas(etapa: str, *, teste: bool = False) -> list[str]:
    """O que impede a etapa de rodar agora (falta de chave), em frases para a tela. Vazio = pode rodar.
    Na produção e na criação de personagem fora do modo teste, confere também a chave do provedor de imagem."""
    base = etapa_base(etapa)
    if base == "producao" and etapa == "producao" and teste:
        return []  # o modo teste não chama nenhuma API paga: espeak, quadros provisórios e sem o publicador
    if etapa == "exportar":
        return []  # só o ffmpeg
    if not provedor_com_chave():
        return ["nenhuma chave de LLM cadastrada (Claude, GPT ou Gemini): cadastre uma em Configurações > Chaves de API"]
    cfg = ler()
    saida: list[str] = []
    vistos: set[str] = set()
    for agente in ETAPAS[base]["agentes"]:
        r = _resolver(cfg, base, agente)
        if r.provedor not in vistos and not _tem_chave(r.provedor):
            vistos.add(r.provedor)
            saida.append(f"{agente} está configurado para {_nome_provedor(r.provedor)}, sem chave cadastrada: "
                         "cadastre em Configurações > Chaves de API ou troque o provedor em Modelos por etapa")
        if not r.modelo:
            saida.append(f"{agente}: nenhum modelo definido para {_nome_provedor(r.provedor)}")
    busca = cfg["busca"].get("provedor")
    if base == "pesquisa" and busca and busca not in vistos and not _tem_chave(busca):
        saida.append(f"a busca na web está configurada para {_nome_provedor(busca)}, sem chave cadastrada")
    if base in ("producao", "personagem") and not teste:
        prov_img = cfg["imagem"].get("provedor") or "gemini"
        if not _tem_chave(prov_img):
            saida.append(f"as imagens são do provedor {_nome_provedor(prov_img)}, sem chave cadastrada")
    return saida
