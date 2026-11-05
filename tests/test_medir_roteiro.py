"""ferramentas/medir_roteiro.py: o intervalo máximo sem payoff (nenhum loop fechado) no vídeo longo."""

from __future__ import annotations

import sys

from nucleo import RAIZ

sys.path.insert(0, str(RAIZ / "ferramentas"))

import medir_roteiro  # noqa: E402


def longo(blocos: list[tuple[str, str, int]]) -> str:
    """Um roteiro longo a 150 palavras/min: (abre, fecha, palavras) por bloco, com um re-hook a cada ~90 s."""
    partes = ["---\nformato: longo\nppm: 150\n---\n"]
    for i, (abre, fecha, palavras) in enumerate(blocos, 1):
        corpo = []
        for inicio in range(0, palavras, 225):
            corpo.append(("[RE-HOOK] " if inicio else "") + " ".join(["rato"] * min(225, palavras - inicio)) + ".")
        partes.append(f"## [B{i:02d}] Bloco\n> função: desenvolvimento · abre: {abre} · fecha: {fecha} · conector: "
                      f"{'—' if i == 1 else 'então'} · cortável: não\n\n" + "\n\n".join(corpo) + "\n")
    return "\n".join(partes)


def alertas_de_payoff(tmp_path, blocos) -> list[str]:
    arquivo = tmp_path / "05-roteiro-v1.md"
    arquivo.write_text(longo(blocos), encoding="utf-8")
    r = medir_roteiro.medir(arquivo, None)
    return [a["mensagem"] for a in r["alertas"] if a["codigo"] == "payoff"], r["intervalos_payoff"]


def test_payoff_a_cada_bloco_nao_alerta(tmp_path):
    # quatro blocos de 2 min, cada um com o próprio mini-loop aberto e fechado, mais o loop principal
    alertas, intervalos = alertas_de_payoff(tmp_path, [("L1, L2", "L2", 300), ("L3", "L3", 300), ("L4", "L4", 300),
                                                        ("—", "L1", 300)])
    assert alertas == []
    assert [i["segundos"] for i in intervalos] == [120, 120, 120, 120]


def test_tempo_demais_sem_payoff_e_erro(tmp_path):
    # do começo até o fim do B02 (4 min) nenhum loop fecha
    alertas, intervalos = alertas_de_payoff(tmp_path, [("L1, L2", "—", 300), ("L3", "L2", 300), ("—", "L1, L3", 300)])
    assert len(alertas) == 1 and "240 s sem nenhum loop fechado" in alertas[0] and "início–B02" in alertas[0]
    assert intervalos[0] == {"de": "00:00", "ate": "04:00", "segundos": 240, "entre": "início–B02"}


def test_short_nao_mede_payoff(tmp_path):
    arquivo = tmp_path / "corte.md"
    arquivo.write_text(longo([("L1", "—", 300), ("—", "L1", 300)]).replace("formato: longo", "formato: short"),
                       encoding="utf-8")
    r = medir_roteiro.medir(arquivo, None)
    assert r["intervalos_payoff"] == [] and not any(a["codigo"] == "payoff" for a in r["alertas"])
