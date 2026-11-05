"""Do formulário da interface ao pedido em YAML: os campos que a tela mostra e o pedido do usuário gravado.

A tela edita um formulário (guardado no banco, em projetos.formulario). Na hora de sugerir e na de aprovar, o que o
usuário escreveu vira `pedidos/web/<slug>.yaml`, no mesmo formato comentado do modelo (ferramentas/pedido.py), e o
`pedido.py completar` junta as sugestões que ele aceitou como decisões do sistema. As regras dos campos ficam todas
em `pedido.CAMPOS`: aqui só a tradução da tela (modo de voz, personagens, sugestões aceitas).
"""

from __future__ import annotations

import json
from datetime import date

from nucleo import espaco
from nucleo.pipeline import etapas
from web.api.comum import pedido_web

import pedido  # noqa: E402 - ferramentas/pedido.py: o comum põe ferramentas/ no caminho



def vazio(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or v == []


def texto_de(v) -> str:
    if isinstance(v, bool):
        return "sim" if v else "não"
    if isinstance(v, list):
        return ", ".join(map(str, v)) or "(nenhum)"
    return "" if v is None else f"{v:g}" if isinstance(v, float) else str(v)


def padrao_texto(c) -> str:
    if isinstance(c.padrao, pedido.PorFormato):
        return f"{texto_de(c.padrao.longo)} no longo, {texto_de(c.padrao.short)} no short"
    if c.padrao is pedido.voz_do_canal:
        return texto_de(pedido.voz_do_canal({}))
    if c.padrao is pedido.musica_preenchida:
        return "sim se houver música"
    if c.padrao in (None, [], ""):
        return c.vazio
    return texto_de(c.padrao)


def campos() -> list[dict]:
    """Os campos do pedido para a tela, na ordem e com as seções de pedido.CAMPOS."""
    saida, secao = [], ""
    for c in pedido.CAMPOS:
        if c.secao:
            secao = c.secao[0]
        saida.append({"chave": c.chave, "tipo": c.tipo, "ajuda": c.ajuda, "quem": c.quem, "opcoes": list(c.opcoes),
                      "faixa": list(c.faixa) if c.faixa else None, "padrao": padrao_texto(c), "secao": secao})
    return saida


def aplicar_modo_voz(valores: dict, modo: str | None) -> dict:
    """Traduz o modo de voz da interface para os campos do pedido."""
    saida = {}
    if modo == "narrador":
        saida["personagens"] = []
    elif modo == "sem-voz":
        saida["narracao.ativa"] = False
    elif modo == "so-personagens":
        restricoes = list(valores.get("restricoes") or [])
        if etapas.RESTRICAO_SO_PERSONAGENS not in restricoes:
            restricoes.append(etapas.RESTRICAO_SO_PERSONAGENS)
        saida["restricoes"] = restricoes
    return saida


def personagens_limpos(lista) -> list[dict]:
    saida = []
    for x in lista or []:
        if not isinstance(x, dict):
            continue
        item = {k: v for k, v in x.items() if k in pedido.SUBCAMPOS_PERSONAGEM and not vazio(v)}
        if item.get("nome"):
            saida.append(item)
    return saida


def gravar_pedido_do_usuario(slug: str, brutos: dict) -> tuple[str, list[str]]:
    """Grava pedidos/web/<slug>.yaml (local e no armazenamento) só com o que o usuário escreveu, no mesmo formato
    comentado do modelo. Devolve (caminho lógico, erros de validação). No roteiro importado, a duração fica de fora:
    o pedido.py a calcula pelo roteiro."""
    valores, erros = {c.chave: None for c in pedido.CAMPOS}, []
    calculados = etapas.CALCULADOS_NO_IMPORTADO if etapas.importado(slug) else set()
    for c in pedido.CAMPOS:
        v = None if c.chave in calculados else brutos.get(c.chave)
        if c.tipo == "personagens":
            if v is None:
                continue
            v = personagens_limpos(v)
        elif vazio(v):
            continue
        valores[c.chave], erro = pedido.normalizar(c, v)
        if erro:
            erros.append(f"{c.chave}: {erro}")
    cabecalho = [f"# Pedido de vídeo: {valores.get('titulo') or slug}", "#",
                 f"# Gravado pela interface web em {date.today().isoformat()}, só com o que o usuário preencheu.",
                 "# O resto vem das sugestões que ele aprovou na tela, em projetos/" + slug + "/00-pedido.yaml."]
    caminho = pedido_web(slug)
    espaco.gravar_texto(caminho, pedido.renderizar(valores, {}, cabecalho))
    return caminho, erros


def brutos_do_formulario(formulario: dict) -> dict:
    brutos = {k: v for k, v in (formulario.get("valores") or {}).items() if not vazio(v)}
    if formulario.get("personagens"):
        brutos["personagens"] = formulario["personagens"]
    brutos.update(aplicar_modo_voz(brutos, formulario.get("modo_voz")))
    return brutos


def vazios_para_sugerir(slug: str, brutos: dict) -> list[str]:
    """Os campos que o usuário deixou vazios e que o sistema pode sugerir."""
    fora = etapas.fora_das_sugestoes(slug)
    return [c.chave for c in pedido.CAMPOS if c.chave not in fora and vazio(brutos.get(c.chave))
            and not (c.chave == "personagens" and "personagens" in brutos)]


def para_definir(chave: str, v) -> str:
    if isinstance(v, (list, dict)):
        return f"{chave}={json.dumps(v, ensure_ascii=False)}"
    if isinstance(v, bool):
        return f"{chave}={'sim' if v else 'não'}"
    return f"{chave}={v}"


def separar_aprovacao(slug: str, dados: dict) -> tuple[dict, dict]:
    """A aprovação na tela: o que o usuário escreveu ou mudou (vai para o pedido dele) e as sugestões que ele aceitou
    como vieram (entram no completar como decisões do sistema, com --definir)."""
    valores, origens = dados.get("valores") or {}, dados.get("origens") or {}
    do_usuario = {k: v for k, v in valores.items() if origens.get(k, "usuario") == "usuario" and not vazio(v)}
    fora = etapas.fora_das_sugestoes(slug)
    # a lista vazia sugerida é "nenhum" de propósito: sem ela, o completar põe o padrão (as referências do canal)
    sugeridos = {k: v for k, v in valores.items() if origens.get(k) == "sugestao" and (not vazio(v) or v == [])
                 and k not in fora}
    do_usuario["slug"] = slug
    destino = do_usuario if dados.get("origem_personagens", "usuario") == "usuario" else sugeridos
    destino["personagens"] = personagens_limpos(dados.get("personagens"))
    origem_modo = dados.get("origem_modo", "usuario")
    for chave, v in aplicar_modo_voz({**sugeridos, **do_usuario}, dados.get("modo_voz")).items():
        # o que o modo de voz muda num campo do usuário continua dele; nos outros, segue a origem do modo
        if chave in do_usuario or origem_modo == "usuario":
            sugeridos.pop(chave, None)
            do_usuario[chave] = v
        else:
            do_usuario.pop(chave, None)
            sugeridos[chave] = v
    if vazio(do_usuario.get("tema")) and vazio(sugeridos.get("tema")):
        sugeridos["tema"] = do_usuario.get("titulo") or slug
    return do_usuario, sugeridos


def argumentos_de_definir(sugeridos: dict) -> list[str]:
    args = []
    for chave, v in sugeridos.items():
        if chave in pedido.POR_CHAVE:
            args += ["--definir", para_definir(chave, v)]
    return args
