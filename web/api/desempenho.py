"""Desempenho dos vídeos publicados: as métricas de cada janela (48h, 7d, 28d) ligadas às técnicas e ao tipo de
gancho do roteiro, e o resumo do que funciona no canal (biblioteca/aprendizados.md), que o estrategista e o
roteirista leem.

Os registros ficam no banco (tabela desempenho). Registrar de novo a mesma janela do mesmo vídeo substitui os
números. Depois de cada registro, o resumo é refeito pelo `ferramentas/desempenho.py resumo`, que lê o banco.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

from nucleo import espaco
from nucleo.banco import repositorios
from web.api.comum import RAIZ_CODIGO, exigir, ler_texto, raiz, rodar_script, sincronizar_projeto
from web.api.projetos import carregar_projeto

roteador = APIRouter()

APRENDIZADOS = "biblioteca/aprendizados.md"
SCRIPT = "ferramentas/desempenho.py"
METRICAS = ("ctr", "retencao_30s", "retencao_media", "viu_vs_passou", "views", "inscritos_ganhos")
VIDEO_ID = re.compile(r"^[\w-]{6,64}$")
LIMITE_OBS = 1000


def frontmatter(caminho: Path) -> dict:
    """O frontmatter do roteiro (técnicas, tipo de gancho, formato, título), como o registro de desempenho sempre leu."""
    texto = ler_texto(caminho) or ""
    linhas = texto.splitlines()
    if not linhas or linhas[0].strip() != "---":
        return {}
    dados: dict = {}
    for linha in linhas[1:]:
        if linha.strip() == "---":
            break
        linha = re.sub(r"\s+#.*$", "", linha)
        if ":" not in linha:
            continue
        chave, valor = (p.strip() for p in linha.split(":", 1))
        if valor.startswith("[") and valor.endswith("]"):
            dados[chave] = [v.strip().strip("\"'") for v in valor[1:-1].split(",") if v.strip()]
        else:
            dados[chave] = valor.strip("\"'")
    return dados


def refazer_aprendizados() -> str | None:
    """Refaz biblioteca/aprendizados.md a partir do banco e sobe o arquivo. Devolve o aviso, se não deu."""
    if not (RAIZ_CODIGO / SCRIPT).exists():
        return "o resumo do canal não foi refeito: ferramentas/desempenho.py não existe"
    codigo, saida = rodar_script(SCRIPT, "resumo")
    if codigo != 0:
        return f"o resumo do canal não foi refeito: {saida[-300:]}"
    if (raiz() / APRENDIZADOS).exists():
        espaco.enviar(APRENDIZADOS)
    return None


@roteador.get("/api/projetos/{slug}/desempenho")
def desempenho_do_projeto(slug: str):
    carregar_projeto(slug)
    return [r for r in repositorios.desempenhos() if r.get("projeto_slug") == slug]


@roteador.post("/api/projetos/{slug}/desempenho")
def registrar(slug: str, dados: dict = Body(...)):
    carregar_projeto(slug)
    video_id = dados.get("video_id")
    exigir(isinstance(video_id, str) and VIDEO_ID.fullmatch(video_id.strip()), "escreva o código do vídeo no YouTube")
    janela = dados.get("janela") or "7d"
    exigir(janela in ("48h", "7d", "28d"), "janela é 48h, 7d ou 28d")
    metricas = dados.get("metricas") if isinstance(dados.get("metricas"), dict) else {}
    limpas = {}
    for chave, valor in metricas.items():
        exigir(chave in METRICAS, f"métrica desconhecida: {chave}")
        if valor is None or valor == "":
            continue
        exigir(isinstance(valor, (int, float)) and not isinstance(valor, bool) and valor >= 0, f"{chave} precisa ser um número")
        limpas[chave] = int(valor) if chave in ("views", "inscritos_ganhos") else float(valor)
    exigir(limpas, "preencha pelo menos uma métrica")
    obs = dados.get("obs") if isinstance(dados.get("obs"), str) else ""
    pasta = sincronizar_projeto(slug)
    front = frontmatter(pasta / "06-roteiro-final.md")
    tecnicas = front.get("tecnicas") if isinstance(front.get("tecnicas"), list) else []
    gatilhos = front.get("gatilhos") if isinstance(front.get("gatilhos"), list) else []
    try:
        registro = repositorios.registrar_desempenho({
            "projeto_slug": slug, "video_id": video_id.strip(), "janela": janela, "metricas": limpas,
            "obs": obs.strip()[:LIMITE_OBS], "formato": front.get("formato") or "longo", "tecnicas": tecnicas,
            "tipo_gancho": front.get("tipo_gancho") or "", "gatilhos": gatilhos, "titulo": front.get("titulo") or "",
            "roteiro": f"projetos/{slug}/06-roteiro-final.md"})
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    avisos = [] if front else ["o roteiro final não tem frontmatter: o registro ficou sem as técnicas, o tipo de gancho "
                               "e os gatilhos"]
    if aviso := refazer_aprendizados():
        avisos.append(aviso)
    return {"ok": True, "registro": registro, "avisos": avisos}


@roteador.get("/api/desempenho")
def desempenho_do_canal(janela: str | None = None):
    exigir(janela in (None, "48h", "7d", "28d"), "janela é 48h, 7d ou 28d")
    local = raiz() / APRENDIZADOS
    try:
        espaco.garantir(APRENDIZADOS)
    except FileNotFoundError:
        pass
    return {"registros": repositorios.desempenhos(janela), "aprendizados": ler_texto(local)}
