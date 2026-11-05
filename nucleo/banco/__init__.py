"""Banco de dados do servidor: tudo o que é estado (projetos, execuções e log, custos, configuração, chaves
cifradas, contextos, lições, desempenho e o índice dos arquivos do armazenamento).

    from nucleo import banco
    from nucleo.banco import repositorios as repo
    banco.iniciar()                      # cria ou migra o esquema (idempotente)
    repo.criar_projeto("meu-video", "Meu vídeo", {})
    with banco.trava("licoes"):          # exclusão entre processos
        ...

Os repositórios devolvem dicts: a camada web e os fluxos não veem o ORM. Veja docs/arquitetura.md, seção Banco.
"""

from __future__ import annotations

from nucleo.banco.sessao import agora, disponivel, e_sqlite, iniciar, motor, redefinir, sessao, trava

__all__ = ["agora", "disponivel", "e_sqlite", "iniciar", "motor", "redefinir", "sessao", "trava"]
