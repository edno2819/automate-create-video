"""Verificações determinísticas dos artefatos do pipeline.

No Claude Code, uma sessão conferia "por julgamento" o que os agentes entregavam: se o crítico gravou a avaliação,
se a nota batia com a rubrica, se o esqueleto saiu no formato. Sem ela, a conferência virou código, e o que não
passa volta ao agente com o erro apontado (os laços ficam em nucleo/pipeline/fluxos.py). Aqui ficam:

- os esquemas JSON (dicts) de cada artefato que um agente grava ou entrega: ângulos, avaliação, cenas, thumbnail,
  publicação, esqueleto, lições, ficha de personagem, e as entregas estruturadas (leitura fria, revisão visual,
  designer de personagem, ganchos e a avaliação deles). Os esquemas de entrega vão como parâmetros da ferramenta
  Entregar, então ficam no JSON Schema simples que os três provedores aceitam; as regras que pedem mais (nota abaixo
  de 2 com evidência, critérios da rubrica que faltam, obrigações sem veredito) ficam nas funções em Python;
- `validar(caminho, esquema)`: os erros de um arquivo (lista vazia = ok), no formato que o fluxo devolve ao agente;
- `aprovacao_pela_rubrica`: a aprovação recalculada pelas notas de cada critério e por biblioteca/rubrica.json.
  Vale mais que a conta do crítico: modelos diferentes erram a soma ponderada de jeitos diferentes;
- `aplicar_obrigacoes`: a obrigação do contrato (ângulo e gancho) que o crítico disse não cumprida desce a nota de
  `promessas-cumpridas`, antes do recálculo;
- `pontuar_gancho` e `melhores_ganchos`: a nota de cada gancho candidato pela seção `ganchos` da rubrica, com os
  cortes da leitura fria e do tamanho medido, e os melhores para o usuário escolher; `ganchos_pedidos` e
  `marcar_gancho_pedido`: o gancho que o usuário pediu no pedido, que vem primeiro e não perde por falta de padrão;
- `tem_verificar`: os [VERIFICAR] que sobraram num roteiro (a web pede confirmação antes do visual e da produção);
- `loudness`: o volume integrado do vídeo (LUFS), para avisar quando ele foge dos -14 do YouTube;
- a leitura das saídas dos scripts que os fluxos usam para decidir (alertas do cenas.py, blocos com defeito no
  alinhamento, a ficha de personagem).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any

from nucleo import RAIZ as CODIGO

PERCENTUAL_MINIMO_PADRAO = 80
LUFS_ALVO = -14.0
LUFS_TOLERANCIA = 1.0
ENQUADRAMENTOS = ("plano-geral", "plano-medio", "close", "detalhe", "pov", "sobre-o-ombro", "aereo", "contra-plongee",
                  "plongee")  # os mesmos de ferramentas/cenas.py (o teste confere)
POSICOES_TEXTO_THUMB = ("alto", "baixo", "esquerda", "direita")
POSICOES_LEGENDA = ("baixo", "centro", "alto")
PALAVRAS_DESCRICAO_PERSONAGEM = (50, 90)
CORES_PALETA = (3, 4)
CUMPRIDA = ("sim", "parcial", "nao")
CRITERIO_PROMESSAS = "promessas-cumpridas"


def _texto(descricao: str = "", **extra) -> dict:
    d: dict[str, Any] = {"type": "string", **extra}
    if descricao:
        d["description"] = descricao
    return d


def _lista(itens: dict, descricao: str = "", **extra) -> dict:
    d: dict[str, Any] = {"type": "array", "items": itens, **extra}
    if descricao:
        d["description"] = descricao
    return d


_CONFUSAO = {
    "type": "object",
    "required": ["bloco", "trecho", "problema"],
    "properties": {
        "bloco": _texto("o id do bloco, como B01"),
        "trecho": _texto("a frase exata do texto, com no máximo 20 palavras"),
        "problema": _texto("o que faltou para entender, numa frase"),
    },
}

# ---------------------------------------------------------------- entregas estruturadas (ferramenta Entregar)

ESQUEMA_LEITURA_FRIA = {
    "type": "object",
    "required": ["leitura_fria", "confusoes"],
    "properties": {
        "leitura_fria": _lista(_texto(), "o que o espectador entendeu, em palavras simples, uma linha por frase ou "
                                         "par de frases, com o bloco no começo (\"B01: ...\")"),
        "confusoes": _lista(_CONFUSAO, "cada ponto em que a leitura travou; [] quando tudo se entende de primeira"),
    },
}

ESQUEMA_REVISAO_IMAGENS = {
    "type": "object",
    "required": ["resumo", "cenas_refazer", "fichas_refazer", "internet_trocar", "thumbnail"],
    "properties": {
        "resumo": _texto("duas ou três frases sobre o conjunto"),
        "cenas_refazer": _lista({
            "type": "object",
            "required": ["id", "motivo", "ajuste_prompt"],
            "properties": {
                "id": _texto("o id da cena (B03-02) ou \"thumbnail\""),
                "motivo": _texto("o defeito, numa frase"),
                "ajuste_prompt": _texto("em inglês, o que mudar no prompt_cena (uma ou duas frases), ou \"\" quando "
                                        "gerar de novo resolve"),
            },
        }, "as cenas a gerar de novo"),
        "fichas_refazer": _lista({
            "type": "object",
            "required": ["id", "motivo"],
            "properties": {"id": _texto("o id da ficha de referência (P01, L02, P01@B05)"), "motivo": _texto()},
        }, "as fichas de referência que saíram erradas"),
        "internet_trocar": _lista({
            "type": "object",
            "required": ["id", "opcao", "motivo"],
            "properties": {"id": _texto("o id da cena da internet"),
                           "opcao": {"type": "integer", "minimum": 1,
                                     "description": "o número da candidata no manifesto (a contagem começa em 1)"},
                           "motivo": _texto()},
        }, "as imagens da internet a trocar por outra candidata"),
        "thumbnail": {"type": "object", "required": ["ok", "motivo"],
                      "properties": {"ok": {"type": "boolean"}, "motivo": _texto()}},
    },
}

ESQUEMA_REVISAO_VIDEO = {
    "type": "object",
    "required": ["resumo", "quadros", "thumbnail", "ajustes_montagem"],
    "properties": {
        "resumo": _texto("duas ou três frases"),
        "quadros": {"type": "object", "required": ["ok", "problemas"],
                    "properties": {"ok": {"type": "boolean"},
                                   "problemas": _lista(_texto(), "um problema por item, com o quadro em que aparece")}},
        "thumbnail": {"type": "object", "required": ["ok", "motivo"],
                      "properties": {"ok": {"type": "boolean"}, "motivo": _texto()}},
        "ajustes_montagem": {
            "type": "object",
            "description": "só as opções que resolvem um problema visto; {} quando nada precisa mudar",
            "additionalProperties": False,
            "properties": {
                "palavras_por_legenda": {"type": "integer", "minimum": 1, "maximum": 12},
                "maiusculas": {"type": "boolean"},
                "posicao_legenda": {"type": "string", "enum": list(POSICOES_LEGENDA)},
            },
        },
    },
}

ESQUEMA_REVISAO_PERSONAGEM = {
    "type": "object",
    "required": ["ok", "defeitos", "ajuste_en"],
    "properties": {
        "ok": {"type": "boolean", "description": "falso só com defeito claro"},
        "defeitos": _lista(_texto(), "um defeito por item"),
        "ajuste_en": _texto("em inglês, a instrução para gerar de novo a partir desta imagem corrigindo só os "
                            "defeitos, ou \"\" quando ok"),
    },
}

ESQUEMA_DESIGNER_CRIAR = {
    "type": "object",
    "required": ["slug", "resumo", "mudancas"],
    "properties": {
        "slug": _texto("o slug informado na mensagem"),
        "resumo": _texto("o personagem em poucas linhas: aparência com o traço marcante, comportamento, voz e estilo"),
        "mudancas": _lista(_texto(), "o que você acrescentou, mudou ou descartou do que o usuário escreveu, e por quê"),
    },
}

ESQUEMA_DESIGNER_AJUSTAR = {
    "type": "object",
    "required": ["resumo", "ajuste_en", "sem_base", "precisa_imagem"],
    "properties": {
        "resumo": _texto("o que mudou na ficha"),
        "ajuste_en": _texto("em inglês, só o que muda na imagem; \"\" quando não precisa de imagem"),
        "sem_base": {"type": "boolean", "description": "mudança grande: gerar sem a imagem de base"},
        "precisa_imagem": {"type": "boolean", "description": "falso quando só o comportamento ou a voz mudaram"},
    },
}

# ---------------------------------------------------------------- arquivos que os agentes gravam

ESQUEMA_ANGULOS = {
    "type": "object",
    "required": ["angulos", "recomendacao"],
    "properties": {
        "projeto": _texto(),
        "recomendacao": {"type": "object", "required": ["angulo"],
                         "properties": {"angulo": _texto(), "motivo": _texto()}},
        "angulos": _lista({
            "type": "object",
            "required": ["id", "titulo", "thumbnail", "promessa", "duracao_recomendada", "tecnicas", "tipo_gancho",
                         "gatilhos", "gancho_proposto", "obrigacoes", "estrutura"],
            "properties": {
                "id": _texto(minLength=1),
                "titulo": _texto(minLength=1),
                "titulos_alternativos": _lista(_texto()),
                "thumbnail": {"type": "object"},
                "promessa": _texto(minLength=1),
                "formato": {"type": "string", "enum": ["longo", "short"]},
                "duracao_recomendada": _texto("mm:ss", pattern=r"^\s*\d+:\d{1,2}\s*$"),
                "tecnicas": _lista(_texto()),
                "tipo_gancho": _texto(),
                "gatilhos": _lista(_texto(), minItems=1, maxItems=3),
                "gancho_proposto": _texto(),
                "obrigacoes": _lista(_texto(minLength=1), minItems=1),
                "estrutura": _lista(_texto(), minItems=1),
                "esqueleto_base": {"type": ["string", "null"]},
                "evidencias": _lista(_texto()),
                "aposta": {"type": "boolean"},
                "riscos": _lista(_texto()),
                "precisa_pesquisa": {"type": "boolean"},
            },
        }, minItems=2, maxItems=3),
    },
}

ESQUEMA_AVALIACAO = {
    "type": "object",
    "required": ["aprovado", "nota_percentual", "criterios", "leitura_fria", "confusoes", "prioridades", "manter"],
    "properties": {
        "roteiro": _texto(),
        "versao": {"type": ["integer", "string"]},
        "formato": _texto(),
        "aprovado": {"type": "boolean"},
        "nota_percentual": {"type": "number", "minimum": 0, "maximum": 100},
        "bloqueantes_reprovados": _lista(_texto()),
        "leitura_fria": _lista(_texto()),
        "confusoes": _lista(_CONFUSAO),
        "promessa": _texto(),
        "onde_cumpre": _texto(),
        "criterios": _lista({
            "type": "object",
            "required": ["id", "nota"],
            "properties": {
                "id": _texto(minLength=1),
                "nota": {"type": "integer", "minimum": 0, "maximum": 2},
                "peso": {"type": "number"},
                "bloco": _texto(),
                "evidencia": _texto(),
                "problema": _texto(),
                "instrucao": _texto(),
            },
            # nota abaixo de 2 sem bloco, evidência e instrução não ensina o roteirista a corrigir
            "if": {"properties": {"nota": {"enum": [0, 1]}}, "required": ["nota"]},
            "then": {"required": ["bloco", "evidencia", "instrucao"],
                     "properties": {"bloco": _texto(minLength=1), "evidencia": _texto(minLength=1),
                                    "instrucao": _texto(minLength=1)}},
        }, minItems=1),
        "pontos_de_abandono": _lista({"type": "object"}),
        "prioridades": _lista(_texto()),
        "manter": _lista(_texto()),
        "alertas_de_medicao": {"type": "array"},
        "obrigacoes": _lista({
            "type": "object",
            "required": ["id", "cumprida"],
            "properties": {
                "id": _texto(minLength=1),
                "obrigacao": _texto(),
                "cumprida": {"type": "string", "enum": list(CUMPRIDA)},
                "onde": _texto(),
                "evidencia": _texto(),
            },
        }),
    },
}

_CENA = {
    "type": "object",
    "required": ["trecho", "descricao", "enquadramento"],
    "properties": {
        "trecho": _texto(minLength=1),
        "tipo": {"type": "string", "enum": ["ilustracao", "cartela"]},
        "fonte": {"type": "string", "enum": ["ia", "internet"]},
        "busca": {"anyOf": [_texto(), _lista(_texto())]},
        "descricao": _texto(),
        "enquadramento": {"type": "string", "enum": list(ENQUADRAMENTOS)},
        "personagens": _lista(_texto()),
        "local": {"type": ["string", "null"]},
        "objetos": _lista(_texto()),
        "prompt_cena": _texto(),
        "texto_na_tela": _texto(),
        "movimento": _texto(),
        "transicao": _texto(),
        "negativo": _texto(),
    },
}
ESQUEMA_CENAS = {"anyOf": [_lista(_CENA, minItems=1),
                           {"type": "object", "required": ["cenas"], "properties": {"cenas": _lista(_CENA, minItems=1)}}]}

ESQUEMA_THUMBNAIL = {
    "type": "object",
    "required": ["descricao", "enquadramento", "prompt_cena"],
    "properties": {
        "bloco": _texto(),
        "descricao": _texto(),
        "enquadramento": {"type": "string", "enum": list(ENQUADRAMENTOS)},
        "personagens": _lista(_texto()),
        "local": {"type": ["string", "null"]},
        "objetos": _lista(_texto()),
        "prompt_cena": _texto(minLength=1),
        "texto": _texto(),
        "posicao_texto": {"type": "string", "enum": list(POSICOES_TEXTO_THUMB)},
    },
}

ESQUEMA_PUBLICACAO = {
    "type": "object",
    "required": ["titulo", "plataformas", "tags", "tags_youtube"],
    "properties": {
        "titulo": _texto(minLength=1),
        "titulos_alternativos": _lista(_texto()),
        "plataformas": _lista({
            "type": "object",
            "required": ["id", "descricao"],
            "properties": {"id": _texto(minLength=1), "nome": _texto(), "titulo": _texto(),
                           "descricao": _texto(minLength=1)},
        }, minItems=1),
        "tags": {
            "type": "object",
            "required": ["em_alta", "tema"],
            "properties": {
                "em_alta": _lista({"anyOf": [_texto(), {"type": "object", "required": ["tag"],
                                                        "properties": {"tag": _texto(), "evidencia": _texto()}}]}),
                "tema": _lista(_texto()),
            },
        },
        "tags_youtube": _texto(maxLength=500),
        "fonte_tags": _texto(),
        "avisos": {"type": "array"},
    },
}

_TECNICA = {
    "type": "object",
    "required": ["id", "nome", "categoria", "descricao"],
    "properties": {
        "id": _texto(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
        "nome": _texto(minLength=1),
        "categoria": _texto(),
        "formatos": _lista(_texto()),
        "descricao": _texto(minLength=1),
        "quando_usar": _lista(_texto()),
        "quando_evitar": _lista(_texto()),
        "beats": _lista(_texto()),
        "exemplo": _texto(),
        "sinais_de_falha": _lista(_texto()),
        "combina_com": _lista(_texto()),
        "metricas_alvo": _lista(_texto()),
        "origem": _texto(),
    },
}

ESQUEMA_ESQUELETO = {
    "type": "object",
    "required": ["fonte", "formato", "gancho", "blocos", "tecnicas_detectadas", "tecnicas_novas_sugeridas",
                 "template_abstrato", "limitacoes"],
    "properties": {
        "fonte": {"type": "object", "required": ["url"], "properties": {"url": _texto(), "video_id": _texto()}},
        "formato": _texto(),
        "gancho": {"type": "object"},
        "blocos": _lista({"type": "object", "required": ["id", "funcao"]}, minItems=1),
        "tecnicas_detectadas": {"type": "array"},
        "tecnicas_novas_sugeridas": _lista(_TECNICA),
        "template_abstrato": {"type": "object"},
        "limitacoes": {"type": "array"},
    },
}

ESQUEMA_LICOES = _lista({
    "type": "object",
    "required": ["secao", "regra"],
    "properties": {"secao": _texto(), "regra": _texto(minLength=1), "erro": _texto(), "correcao": _texto()},
})

ESQUEMA_FICHA = {
    "type": "object",
    "required": ["slug", "nome", "referencia_en", "descricao_en", "estilo", "fisico", "comportamento", "voz"],
    "properties": {
        "slug": _texto(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
        "nome": _texto(minLength=1),
        "resumo": _texto(),
        "referencia_en": _texto(minLength=1),
        "descricao_en": _texto(minLength=1),
        "estilo": {"type": "object", "required": ["prompt_base"],
                   "properties": {"prompt_base": _texto(minLength=1), "negativo_base": _texto()}},
        "fisico": {"type": "object", "required": ["traco_marcante", "paleta"],
                   "properties": {"traco_marcante": _texto(minLength=1), "paleta": _texto(minLength=1)}},
        "comportamento": {"type": "object"},
        "voz": {"type": "object", "required": ["voz"], "properties": {"voz": _texto(minLength=1)}},
    },
}


# ---------------------------------------------------------------- validação

def erros_do_esquema(dados: Any, esquema: dict, limite: int = 20) -> list[str]:
    """Os erros de `dados` contra o esquema, como "caminho: mensagem" (o formato que o agente recebe de volta)."""
    from jsonschema import Draft202012Validator
    erros = sorted(Draft202012Validator(esquema).iter_errors(dados), key=lambda e: list(map(str, e.absolute_path)))
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(raiz)'}: {e.message}"[:300] for e in erros[:limite]]


def ler_json(caminho: Path | str) -> tuple[Any, str | None]:
    """(dados, erro): o JSON do arquivo, ou o motivo de não haver um."""
    caminho = Path(caminho)
    try:
        return json.loads(caminho.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"{caminho.name} não existe"
    except json.JSONDecodeError as e:
        return None, f"{caminho.name} não é um JSON válido: {e}"
    except (OSError, UnicodeDecodeError) as e:
        return None, f"{caminho.name} não pôde ser lido: {e}"


def validar(caminho: Path | str, esquema: dict) -> list[str]:
    """Os erros do arquivo contra o esquema; lista vazia quando ele existe e segue o formato."""
    dados, erro = ler_json(caminho)
    if erro:
        return [erro]
    return erros_do_esquema(dados, esquema)


def validar_angulos(caminho: Path | str) -> list[str]:
    dados, erro = ler_json(caminho)
    if erro:
        return [erro]
    erros = erros_do_esquema(dados, ESQUEMA_ANGULOS)
    if erros or not isinstance(dados, dict):
        return erros
    ids = [a.get("id") for a in dados.get("angulos") or []]
    if len(set(ids)) != len(ids):
        erros.append("angulos: há ids repetidos")
    recomendado = (dados.get("recomendacao") or {}).get("angulo")
    if recomendado not in ids:
        erros.append(f"recomendacao/angulo: '{recomendado}' não é o id de nenhum ângulo ({', '.join(map(str, ids))})")
    # tipo de gancho e gatilhos são ids da biblioteca: o registro de desempenho agrupa por eles
    biblioteca = ids_da_biblioteca()
    for a in dados.get("angulos") or []:
        if biblioteca["tipos_gancho"] and a.get("tipo_gancho") not in biblioteca["tipos_gancho"]:
            erros.append(f"angulos/{a.get('id')}/tipo_gancho: '{a.get('tipo_gancho')}' não é um id de tipos_gancho em "
                         "biblioteca/tecnicas.json")
        if biblioteca["gatilhos"] and (fora := [g for g in a.get("gatilhos") or [] if g not in biblioteca["gatilhos"]]):
            erros.append(f"angulos/{a.get('id')}/gatilhos: {', '.join(map(str, fora))} não são ids de gatilhos em "
                         "biblioteca/tecnicas.json")
    return erros


# ---------------------------------------------------------------- rubrica e avaliação

def _arquivo_da_biblioteca(nome: str) -> Path:
    """Um arquivo estático da biblioteca (rubrica, formatos): o do espaço de trabalho, se houver, senão o do código."""
    try:
        from nucleo.config import config
        local = config().raiz / "biblioteca" / nome
        if local.is_file():
            return local
    except Exception:  # noqa: BLE001 - sem configuração legível, vale o arquivo que vem com o código
        pass
    return CODIGO / "biblioteca" / nome


@lru_cache(maxsize=8)
def _json_da_biblioteca(caminho: str, mtime_ns: int) -> dict:
    return json.loads(Path(caminho).read_text(encoding="utf-8"))


def rubrica() -> dict:
    caminho = _arquivo_da_biblioteca("rubrica.json")
    return _json_da_biblioteca(str(caminho), caminho.stat().st_mtime_ns)


def tecnicas() -> dict:
    """biblioteca/tecnicas.json do espaço de trabalho (as técnicas aprovadas pelo usuário estão lá), ou a do código."""
    caminho = _arquivo_da_biblioteca("tecnicas.json")
    try:
        return _json_da_biblioteca(str(caminho), caminho.stat().st_mtime_ns)
    except (OSError, json.JSONDecodeError):
        return {}


def ids_da_biblioteca() -> dict[str, list[str]]:
    """Os ids de tecnicas.json por lista (tipos_gancho, gatilhos, tecnicas), na ordem do arquivo."""
    doc = tecnicas()
    return {chave: [t["id"] for t in doc.get(chave) or [] if isinstance(t, dict) and t.get("id")]
            for chave in ("tipos_gancho", "gatilhos", "tecnicas")}


def formato_da_rubrica(formato: str | None) -> str:
    """O corte é avaliado como short; qualquer coisa que não seja short vale como longo."""
    return "short" if str(formato or "").lower() in ("short", "corte") else "longo"


def criterios_aplicaveis(formato: str | None) -> list[dict]:
    f = formato_da_rubrica(formato)
    return [c for c in rubrica().get("criterios") or [] if f in (c.get("formatos") or [])]


def _nota(valor: Any) -> int | None:
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)) and float(valor).is_integer() and 0 <= valor <= 2:
        return int(valor)
    return None


def _numero(valor: float) -> float | int:
    valor = round(valor, 1)
    return int(valor) if float(valor).is_integer() else valor


def aprovacao_pela_rubrica(avaliacao: dict, formato: str | None) -> tuple[bool, float, list[str]]:
    """(aprovado, nota percentual, bloqueantes que impedem a aprovação), pelas notas de cada critério e pela regra
    de biblioteca/rubrica.json: nenhum bloqueante com nota 0 e nota ponderada de pelo menos `percentual_minimo` do
    máximo, contando só os critérios do formato (o corte conta como short). O peso é o da rubrica, não o que o
    crítico escreveu. Um bloqueante que o crítico não avaliou também impede a aprovação; os outros critérios que
    faltam ficam fora da conta."""
    regra = rubrica().get("aprovacao") or {}
    minimo = float(regra.get("percentual_minimo", PERCENTUAL_MINIMO_PADRAO))
    notas = {}
    for c in (avaliacao or {}).get("criterios") or []:
        if isinstance(c, dict) and c.get("id") and (n := _nota(c.get("nota"))) is not None:
            notas[str(c["id"])] = n
    soma = maximo = 0.0
    bloqueantes: list[str] = []
    for c in criterios_aplicaveis(formato):
        peso = float(c.get("peso") or 1)
        if c["id"] not in notas:
            if c.get("bloqueante"):
                bloqueantes.append(c["id"])
            continue
        soma += notas[c["id"]] * peso
        maximo += 2 * peso
        if c.get("bloqueante") and notas[c["id"]] == 0:
            bloqueantes.append(c["id"])
    percentual = soma / maximo * 100 if maximo else 0.0
    return (not bloqueantes and percentual >= minimo), _numero(percentual), bloqueantes


def criterios_faltando(avaliacao: dict, formato: str | None) -> list[str]:
    avaliados = {str(c.get("id")) for c in (avaliacao or {}).get("criterios") or [] if isinstance(c, dict)}
    return [c["id"] for c in criterios_aplicaveis(formato) if c["id"] not in avaliados]


def erros_da_avaliacao(avaliacao: Any, formato: str | None, obrigacoes: list[str] | None = None) -> list[str]:
    """O esquema mais o que o esquema não diz: todos os critérios da rubrica do formato precisam estar lá, e cada
    obrigação do contrato (ids O1, O2..., que o fluxo mandou) com o veredito em `obrigacoes`."""
    erros = erros_do_esquema(avaliacao, ESQUEMA_AVALIACAO)
    if isinstance(avaliacao, dict) and isinstance(avaliacao.get("criterios"), list):
        if faltam := criterios_faltando(avaliacao, formato):
            erros.append(f"criterios: faltam {', '.join(faltam)} (a rubrica do formato {formato_da_rubrica(formato)} "
                         "pede todos, inclusive os de nota 2, com id, nota e peso)")
    if obrigacoes and isinstance(avaliacao, dict):
        vistas = {str(o.get("id")) for o in avaliacao.get("obrigacoes") or [] if isinstance(o, dict)}
        if faltam := [o for o in obrigacoes if o not in vistas]:
            erros.append(f"obrigacoes: faltam {', '.join(faltam)} (uma entrada por obrigação do contrato, com id, "
                         "cumprida sim|parcial|nao, onde e evidencia)")
    return erros


def aplicar_obrigacoes(avaliacao: dict) -> str | None:
    """A regra de `promessas-cumpridas`: uma obrigação ou promessa não cumprida zera o critério, uma cumprida pela
    metade deixa no máximo 1. A nota que o crítico deu acima disso desce, com o problema e a instrução tirados da
    obrigação. Devolve o que mudou (para o log), ou None."""
    lista = [o for o in avaliacao.get("obrigacoes") or [] if isinstance(o, dict)]
    nao = [o for o in lista if o.get("cumprida") == "nao"]
    parcial = [o for o in lista if o.get("cumprida") == "parcial"]
    if not nao and not parcial:
        return None
    teto, culpadas = (0, nao) if nao else (1, parcial)
    criterios = avaliacao.setdefault("criterios", [])
    item = next((c for c in criterios if isinstance(c, dict) and c.get("id") == CRITERIO_PROMESSAS), None)
    if item is None:
        peso = next((c.get("peso") for c in rubrica().get("criterios") or [] if c.get("id") == CRITERIO_PROMESSAS), 3)
        item = {"id": CRITERIO_PROMESSAS, "nota": 2, "peso": peso}
        criterios.append(item)
    nota = _nota(item.get("nota"))
    if nota is not None and nota <= teto:
        return None
    descricao = "; ".join(f"{o.get('id')}: {o.get('obrigacao') or '?'}" for o in culpadas)
    item.update(nota=teto, bloco=item.get("bloco") or str(culpadas[0].get("onde") or "—"),
                evidencia=item.get("evidencia") or str(culpadas[0].get("evidencia") or culpadas[0].get("obrigacao") or "—"),
                problema=f"{'não cumprida' if teto == 0 else 'cumprida só em parte'}: {descricao}",
                instrucao=item.get("instrucao") or f"Cumpra {descricao}, com a força que o texto anunciou, ou tire a "
                                                  "promessa do gancho ou do re-hook que a faz.")
    return f"{CRITERIO_PROMESSAS} de {'—' if nota is None else nota} para {teto} ({descricao})"


# ---------------------------------------------------------------- ganchos (etapa Gancho)

CRITERIO_PADROES = "padroes-observados"


def rubrica_ganchos() -> dict:
    return rubrica().get("ganchos") or {}


def contar_palavras(texto: str) -> int:
    """As palavras faladas, como o medir_roteiro.py conta: token com pelo menos uma letra ou número."""
    return sum(1 for t in str(texto or "").split() if any(c.isalnum() for c in t))


def esquema_ganchos(formato: str | None) -> dict:
    """A entrega do redator de ganchos. Os ids de tipo de gancho e de gatilho vão como enum: o motor devolve ao
    agente o id que não existe em biblioteca/tecnicas.json. Os ids G1, G2... o fluxo dá, pela ordem."""
    faixa = rubrica_ganchos().get("candidatos") or {}
    ids = ids_da_biblioteca()
    item: dict[str, Any] = {
        "type": "object",
        "required": ["texto", "tipo_gancho", "gatilhos", "padrao", "referencia", "obrigacoes"],
        "properties": {
            "texto": _texto("o gancho exatamente como será falado", minLength=1),
            "tipo_gancho": {"type": "string", "enum": ids["tipos_gancho"]} if ids["tipos_gancho"] else _texto(),
            "gatilhos": _lista({"type": "string", "enum": ids["gatilhos"]} if ids["gatilhos"] else _texto(),
                               "os gatilhos que o texto aciona de fato", minItems=1, maxItems=3),
            "padrao": _texto("a estrutura aplicada, numa linha"),
            "referencia": _texto("de onde vem o padrão (esqueleto e trecho, vídeo de 02-trends.md, linha de "
                                 "aprendizados.md), ou 'sem referência: estrutura da biblioteca'"),
            "obrigacoes": _lista(_texto(), "o que o resto do vídeo precisa entregar para este gancho não mentir"),
            "pedido_pelo_usuario": {"type": "boolean",
                                    "description": "true só no gancho que reproduz o que o usuário pediu"},
        },
    }
    if formato_da_rubrica(formato) == "short":
        item["required"].append("visual")
        item["properties"]["visual"] = _texto("o primeiro frame, que aparece junto com o texto")
    return {"type": "object", "required": ["ganchos"],
            "properties": {"ganchos": _lista(item, "na ordem; o primeiro é o que o usuário pediu, se houver, ou o "
                                                   "proposto do ângulo", minItems=int(faixa.get("minimo", 6)),
                                             maxItems=int(faixa.get("maximo", 8)))}}


def esquema_avaliacao_ganchos(ids: list[str]) -> dict:
    """A entrega do crítico na etapa Gancho: as notas de cada critério da rubrica de ganchos, para cada gancho."""
    criterios = [c["id"] for c in rubrica_ganchos().get("criterios") or []]
    return {
        "type": "object",
        "required": ["ganchos", "recomendacao"],
        "properties": {
            "ganchos": _lista({
                "type": "object",
                "required": ["id", "criterios", "resumo"],
                "properties": {
                    "id": {"type": "string", "enum": ids},
                    "criterios": _lista({
                        "type": "object",
                        "required": ["id", "nota"],
                        "properties": {"id": {"type": "string", "enum": criterios},
                                       "nota": {"type": "integer", "minimum": 0, "maximum": 2},
                                       "problema": _texto("numa frase, em toda nota abaixo de 2")},
                    }, "um item por critério da rubrica de ganchos", minItems=len(criterios)),
                    "resumo": _texto("uma frase: o ponto forte e o ponto fraco"),
                },
            }, "um item por gancho", minItems=len(ids)),
            "recomendacao": {"type": "object", "required": ["gancho", "motivo"],
                             "properties": {"gancho": {"type": "string", "enum": ids}, "motivo": _texto()}},
        },
    }


def pontuar_gancho(gancho: dict, avaliado: dict | None, formato: str | None) -> dict:
    """A nota de um gancho pela rubrica de ganchos, com as notas do crítico e dois cortes que o sistema aplica: a
    clareza pelas confusões da leitura fria (uma: no máximo 1; duas ou mais: 0) e a fluidez pelo tamanho medido
    (mais que o dobro da janela: no máximo 1). Elegível é o gancho sem bloqueante com 0 ou sem nota. Devolve
    {criterios, nota_percentual, elegivel, bloqueantes, ajustes}."""
    r = rubrica_ganchos()
    notas: dict[str, dict] = {}
    for c in (avaliado or {}).get("criterios") or []:
        if isinstance(c, dict) and (n := _nota(c.get("nota"))) is not None:
            notas[str(c.get("id"))] = {"nota": n, "problema": str(c.get("problema") or "").strip()}
    ajustes: list[str] = []

    def teto(criterio: str, maximo: int, motivo: str) -> None:
        if criterio in notas and notas[criterio]["nota"] > maximo:
            ajustes.append(f"{criterio}: {notas[criterio]['nota']} → {maximo} ({motivo})")
            notas[criterio]["nota"] = maximo
            notas[criterio]["problema"] = "; ".join(p for p in (notas[criterio]["problema"], motivo) if p)

    if confusoes := len(gancho.get("confusoes") or []):
        teto("clareza-imediata", 0 if confusoes >= 2 else 1, f"{confusoes} confusão(ões) na leitura fria")
    janela = float((r.get("janela_segundos") or {}).get(formato_da_rubrica(formato), 5))
    if isinstance(gancho.get("segundos"), (int, float)) and gancho["segundos"] > 2 * janela:
        teto("fluidez-e-duracao", 1, f"{_numero(gancho['segundos'])} s, mais que o dobro da janela de {janela:g} s")
    if gancho.get("pedido_pelo_usuario") and notas.get(CRITERIO_PADROES, {}).get("nota", 2) < 2:
        # o pedido do usuário vale mais que qualquer padrão: o gancho que ele pediu não perde por falta de referência
        ajustes.append(f"{CRITERIO_PADROES}: {notas[CRITERIO_PADROES]['nota']} → 2 (o gancho que o usuário pediu)")
        notas[CRITERIO_PADROES] = {"nota": 2, "problema": ""}
    soma = maximo = 0.0
    criterios: list[dict] = []
    bloqueantes: list[str] = []
    for c in r.get("criterios") or []:
        peso = float(c.get("peso") or 1)
        if c["id"] not in notas:
            if c.get("bloqueante"):
                bloqueantes.append(c["id"])
            continue
        n = notas[c["id"]]["nota"]
        criterios.append({"id": c["id"], "nota": n, "peso": c.get("peso"),
                          **({"problema": notas[c["id"]]["problema"]} if notas[c["id"]]["problema"] else {})})
        soma += n * peso
        maximo += 2 * peso
        if c.get("bloqueante") and n == 0:
            bloqueantes.append(c["id"])
    return {"criterios": criterios, "nota_percentual": _numero(soma / maximo * 100) if maximo else 0,
            "elegivel": not bloqueantes, "bloqueantes": bloqueantes, "ajustes": ajustes}


def melhores_ganchos(ganchos: list[dict], n: int | None = None) -> list[str]:
    """Os ids dos melhores ganchos elegíveis: o que o usuário pediu primeiro, depois a maior nota, menos confusões e
    a ordem de entrega."""
    n = n or int(rubrica_ganchos().get("melhores", 3))
    elegiveis = [(i, g) for i, g in enumerate(ganchos) if g.get("elegivel")]
    elegiveis.sort(key=lambda x: (not x[1].get("pedido_pelo_usuario"), -(x[1].get("nota_percentual") or 0),
                                  len(x[1].get("confusoes") or []), x[0]))
    return [g["id"] for _, g in elegiveis[:n]]


# A frase entre aspas que vem logo depois de um pedido de abertura: 'Inicie o vídeo com um hook como "..."'.
PEDIDO_DE_GANCHO = re.compile(r"\b(?:hook|gancho|abertura|comece|come[cç]ar|inicie|iniciar|abra|abrir)\b"
                              r"[^\"“”«»]{0,80}[\"“«]([^\"“”«»]{8,300})[\"”»]", re.I)
ABREVIACOES = {"vc": "voce", "vcs": "voces", "q": "que", "pq": "porque", "tb": "tambem", "tbm": "tambem",
               "td": "tudo", "mt": "muito", "msm": "mesmo"}
SEMELHANCA_DO_PEDIDO = 0.6


def ganchos_pedidos(*textos) -> list[str]:
    """As aberturas que o usuário escreveu entre aspas na descrição, nas restrições ou na orientação dos ganchos."""
    achados: list[str] = []
    for texto in textos:
        for t in texto if isinstance(texto, list) else [texto]:
            for m in PEDIDO_DE_GANCHO.finditer(str(t or "")):
                if (frase := " ".join(m.group(1).split())) not in achados:
                    achados.append(frase)
    return achados


def _palavras_comparaveis(texto: str) -> list[str]:
    t = "".join(c for c in unicodedata.normalize("NFD", str(texto).lower()) if not unicodedata.combining(c))
    return [ABREVIACOES.get(p, p) for p in re.findall(r"[a-z0-9]+", t)]


def semelhanca(a: str, b: str) -> float:
    """Quanto dois textos falam as mesmas palavras na mesma ordem (0 a 1), sem acento, caixa nem pontuação."""
    return SequenceMatcher(None, _palavras_comparaveis(a), _palavras_comparaveis(b)).ratio()


def marcar_gancho_pedido(ganchos: list[dict], pedidos: list[str]) -> tuple[str | None, str | None]:
    """Deixa `pedido_pelo_usuario` em um gancho só: o mais parecido com a frase que o usuário pediu, ou, sem frase
    entre aspas, o primeiro que o redator marcou. Devolve (id, aviso); o aviso sai quando o usuário pediu uma frase
    e nenhum gancho a reproduz."""
    marcado = None
    if pedidos:
        nota, achado = max(((max(semelhanca(g["texto"], q) for q in pedidos), g) for g in ganchos),
                           key=lambda x: x[0], default=(0.0, None))
        if achado is not None and nota >= SEMELHANCA_DO_PEDIDO:
            marcado = achado
    if marcado is None:
        marcado = next((g for g in ganchos if g.get("pedido_pelo_usuario") is True), None)
    for g in ganchos:
        g.pop("pedido_pelo_usuario", None)
    if marcado is not None:
        marcado["pedido_pelo_usuario"] = True
        return marcado["id"], None
    if pedidos:
        return None, f"o redator não escreveu o gancho que você pediu (“{pedidos[0]}”): escolha um e ajuste o texto"
    return None, None


def validar_avaliacao(caminho: Path | str, formato: str | None) -> list[str]:
    dados, erro = ler_json(caminho)
    if erro:
        return [erro]
    return erros_da_avaliacao(dados, formato)


# ---------------------------------------------------------------- roteiro

def _ferramentas() -> None:
    pasta = str(CODIGO / "ferramentas")
    if pasta not in sys.path:
        sys.path.insert(0, pasta)


def tem_verificar(roteiro: Path | str) -> list[dict]:
    """Os itens [VERIFICAR: ...] que sobraram no roteiro, como [{bloco, item}]. Lista vazia = nada a checar."""
    roteiro = Path(roteiro)
    if not roteiro.is_file():
        return []
    _ferramentas()
    from medir_roteiro import ler_roteiro
    _, blocos, _ = ler_roteiro(roteiro)
    return [{"bloco": b.id, "item": v} for b in blocos for v in b.verificar]


def blocos_com_defeito(tempos: dict | None) -> list[str]:
    """Os blocos que o alinhamento apontou (cobertura abaixo de 85% ou palavras fora do roteiro), em ordem."""
    if not isinstance(tempos, dict):
        return []
    ids: list[str] = []
    for q in tempos.get("qualidade") or []:
        if not isinstance(q, dict) or not q.get("bloco"):
            continue
        if (q.get("cobertura_pct") is not None and q["cobertura_pct"] < 85) or (q.get("palavras_extras") or 0) > 2:
            ids.append(q["bloco"])
    for alerta in tempos.get("alertas") or []:
        if m := re.match(r"\s*(B\d+)\b", str(alerta)):
            ids.append(m.group(1))
    return list(dict.fromkeys(ids))


# ---------------------------------------------------------------- saída do cenas.py

_ALERTA_CENAS = re.compile(r"^- (erro|aviso) \[([^\]]*)\] (.*)$")
# Os avisos que mexem na coerência das imagens (o personagem certo, a cena sem imagem, a thumbnail): voltam ao
# diretor visual uma vez, mesmo com a montagem sem erro.
_COERENCIA = ("não cita", "quem fala não está na imagem", "sem prompt_cena de reserva",
              "não começa com a referencia_en")


def alertas_do_cenas(saida: str) -> list[dict]:
    """Os alertas que `cenas.py montar` imprime ("- aviso [B01-02] mensagem"), como {nivel, onde, mensagem}."""
    alertas = []
    for linha in (saida or "").splitlines():
        if m := _ALERTA_CENAS.match(linha.strip()):
            alertas.append({"nivel": m.group(1), "onde": m.group(2), "mensagem": m.group(3)})
    return alertas


def avisos_de_coerencia(alertas: list[dict]) -> list[dict]:
    return [a for a in alertas if a["nivel"] == "aviso"
            and (a["onde"] == "thumbnail" or any(t in a["mensagem"] for t in _COERENCIA))]


# ---------------------------------------------------------------- personagem

def cores_da_paleta(paleta: str) -> int:
    """Quantas cores a paleta cita ("Vermelho vivo, verde-folha, azul-marinho e rosa." = 4)."""
    partes = re.split(r",|;|\s+e\s+|\s+and\s+", str(paleta or "").strip().rstrip("."))
    return len([p for p in partes if p.strip()])


def erros_da_ficha(ficha: Any, voz_do_canal: str | None = None, vozes: set[str] | None = None) -> list[str]:
    """O que a ficha precisa ter para gerar um personagem que se reconhece em qualquer cena (a régua da antiga skill
    criar-personagem): descricao_en de 50 a 90 palavras começando pela referencia_en, traço marcante, paleta de 3 a
    4 cores e uma voz do Gemini TTS diferente da voz do canal."""
    erros = erros_do_esquema(ficha, ESQUEMA_FICHA)
    if not isinstance(ficha, dict):
        return erros
    ref, desc = str(ficha.get("referencia_en") or "").strip(), str(ficha.get("descricao_en") or "").strip()
    n = len(desc.split())
    minimo, maximo = PALAVRAS_DESCRICAO_PERSONAGEM
    if desc and not minimo <= n <= maximo:
        erros.append(f"descricao_en: {n} palavras (contadas pelo sistema); precisa ter de {minimo} a {maximo}")
    if ref and desc and not desc.lower().startswith(ref.lower()):
        erros.append(f"descricao_en: precisa começar pela referencia_en ('{ref}')")
    paleta = ((ficha.get("fisico") or {}) if isinstance(ficha.get("fisico"), dict) else {}).get("paleta")
    if paleta:
        cores = cores_da_paleta(paleta)
        if not CORES_PALETA[0] <= cores <= CORES_PALETA[1]:
            erros.append(f"fisico.paleta: {cores} cor(es); use de {CORES_PALETA[0]} a {CORES_PALETA[1]}")
    voz = ((ficha.get("voz") or {}) if isinstance(ficha.get("voz"), dict) else {}).get("voz")
    if voz and vozes is not None and voz not in vozes:
        erros.append(f"voz.voz: '{voz}' não é uma voz do Gemini TTS")
    if voz and voz_do_canal and voz == voz_do_canal:
        erros.append(f"voz.voz: '{voz}' é a voz do narrador do canal; escolha outra")
    return erros


# ---------------------------------------------------------------- vídeo

def duracao_midia(caminho: Path | str) -> float | None:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(caminho)],
                           capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def loudness(video: Path | str) -> float | None:
    """O volume integrado (LUFS) do áudio do vídeo, pelo filtro ebur128 do ffmpeg. None sem áudio ou sem ffmpeg."""
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-map", "0:a:0?", "-af", "ebur128",
                            "-f", "null", "-"], capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired):
        return None
    achados = re.findall(r"^\s*I:\s*(-?inf|-?[\d.]+)\s*LUFS", r.stderr or "", re.M)
    if not achados or "inf" in achados[-1]:
        return None
    try:
        return float(achados[-1])
    except ValueError:
        return None


def aviso_de_loudness(valor: float | None) -> str | None:
    if valor is None or abs(valor - LUFS_ALVO) <= LUFS_TOLERANCIA:
        return None
    return (f"o áudio do vídeo ficou em {valor:.1f} LUFS; o volume de referência do YouTube é {LUFS_ALVO:.0f} LUFS "
            f"(±{LUFS_TOLERANCIA:.0f})")
