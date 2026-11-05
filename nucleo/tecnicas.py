"""biblioteca/tecnicas.json: vem na imagem e também muda no armazenamento (as técnicas que o usuário aprova).

O armazenamento é a fonte da verdade, e no arranque a cópia da imagem é trocada pela de lá. Sem este módulo, os
gatilhos, tipos de gancho e técnicas que uma versão nova do código traz nunca chegariam a uma instalação que já tem
o arquivo. Por isso o servidor lê o arquivo da imagem antes de sincronizar (`da_imagem`) e, depois, acrescenta ao do
armazenamento os ids que faltam, no fim de cada lista (`completar`). Nenhum id existente muda nem sai: o histórico
de desempenho depende deles.
"""

from __future__ import annotations

import json
import logging

from nucleo import RAIZ as CODIGO

CAMINHO = "biblioteca/tecnicas.json"
LISTAS = ("gatilhos", "tipos_gancho", "tecnicas")

log = logging.getLogger(__name__)


def da_imagem() -> dict:
    """O tecnicas.json que veio com o código. Leia antes de `espaco.inicializar()`: quando o espaço de trabalho é a
    raiz do código (no contêiner), a sincronização troca este arquivo pelo do armazenamento."""
    try:
        return json.loads((CODIGO / CAMINHO).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def mesclar(atual: dict, imagem: dict) -> tuple[dict, list[str]]:
    """(documento, ids acrescentados): o `atual` com as entradas da imagem cujos ids ele não tem, no fim de cada
    lista, e as categorias que faltam. As chaves seguem a ordem da imagem (os gatilhos antes das técnicas)."""
    novos: list[str] = []
    saida = dict(atual)
    for chave in LISTAS:
        lista = [t for t in atual.get(chave) or [] if isinstance(t, dict)]
        ids = {t.get("id") for t in lista}
        faltam = [t for t in imagem.get(chave) or [] if isinstance(t, dict) and t.get("id") not in ids]
        if faltam:
            saida[chave] = lista + faltam
            novos += [t["id"] for t in faltam]
    categorias = {**(imagem.get("categorias") or {}), **(atual.get("categorias") or {})}
    if categorias != (atual.get("categorias") or {}):
        saida["categorias"] = categorias
    ordem = [k for k in imagem if k in saida] + [k for k in saida if k not in imagem]
    return {k: saida[k] for k in ordem}, novos


def completar(imagem: dict) -> list[str]:
    """Acrescenta ao tecnicas.json do espaço de trabalho (já sincronizado) o que a imagem traz e ele não tem, sob a
    trava das técnicas, e envia ao armazenamento. Devolve os ids acrescentados."""
    from nucleo import banco, espaco
    from nucleo.config import config
    if not imagem:
        return []
    with banco.trava("tecnicas"):
        espaco.baixar(CAMINHO)
        arquivo = config().raiz / CAMINHO
        if not arquivo.exists():
            return []  # o arquivo da imagem sobe na semente, inteiro
        atual = json.loads(arquivo.read_text(encoding="utf-8"))
        doc, novos = mesclar(atual, imagem)
        if doc == atual:
            return []
        arquivo.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        espaco.enviar(CAMINHO)
    if novos:
        log.info("tecnicas.json: %d id(s) novo(s) do código: %s", len(novos), ", ".join(novos))
    return novos
