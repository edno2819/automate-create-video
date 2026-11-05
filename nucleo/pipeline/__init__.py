"""O pipeline de um vídeo: as etapas (etapas.py), a fila e o trabalhador (execucao.py), os fluxos com os laços de
qualidade (fluxos.py) e as verificações determinísticas dos artefatos (verificacoes.py).

Os módulos não são importados aqui: a web importa só o que usa (etapas, para a trilha; execucao, para enfileirar),
sem carregar os fluxos e o motor de agentes.
"""
