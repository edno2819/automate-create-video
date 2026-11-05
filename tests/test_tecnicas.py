"""nucleo/tecnicas.py: os ids novos de biblioteca/tecnicas.json que vêm com o código entram no arquivo do
armazenamento (que vale mais que o da imagem), sem trocar nem apagar nenhum id que já existe."""

from __future__ import annotations

import json

from nucleo import tecnicas

IMAGEM = {
    "versao": 2, "categorias": {"gancho": "abrir", "retencao": "segurar"},
    "gatilhos": [{"id": "curiosidade", "nome": "Curiosidade"}],
    "tipos_gancho": [{"id": "pergunta-direta", "descricao": "da imagem"}, {"id": "negacao-revelacao", "descricao": "nova"}],
    "tecnicas": [{"id": "loop-aberto", "nome": "Loop aberto"}, {"id": "prova-visual", "nome": "Prova visual"}],
}


def test_mesclar_acrescenta_so_o_que_falta():
    atual = {"versao": 1, "categorias": {"gancho": "do usuário"},
             "tipos_gancho": [{"id": "pergunta-direta", "descricao": "editada pelo usuário"}],
             "tecnicas": [{"id": "loop-aberto", "nome": "Loop aberto"}, {"id": "aprovada", "nome": "Do esqueleto"}]}
    doc, novos = tecnicas.mesclar(atual, IMAGEM)
    assert novos == ["curiosidade", "negacao-revelacao", "prova-visual"]
    assert doc["tipos_gancho"][0]["descricao"] == "editada pelo usuário"  # o que existe não muda
    assert [t["id"] for t in doc["tecnicas"]] == ["loop-aberto", "aprovada", "prova-visual"]
    assert doc["categorias"] == {"gancho": "do usuário", "retencao": "segurar"} and doc["versao"] == 1
    assert list(doc).index("gatilhos") < list(doc).index("tecnicas")  # a ordem das chaves é a da imagem
    assert tecnicas.mesclar(doc, IMAGEM) == (doc, [])


def test_completar_grava_e_envia(ambiente):
    from nucleo.banco import repositorios
    arquivo = ambiente / "biblioteca" / "tecnicas.json"
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    arquivo.write_text(json.dumps({"tipos_gancho": [], "tecnicas": [{"id": "loop-aberto"}]}), encoding="utf-8")
    assert tecnicas.completar(IMAGEM) == ["curiosidade", "pergunta-direta", "negacao-revelacao", "prova-visual"]
    assert [t["id"] for t in json.loads(arquivo.read_text())["gatilhos"]] == ["curiosidade"]
    assert "biblioteca/tecnicas.json" in repositorios.indice("biblioteca/")
    assert tecnicas.completar(IMAGEM) == []
    assert tecnicas.completar({}) == []
