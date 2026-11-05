"""Chamadas bloqueantes aos provedores (gerar uma imagem, animar uma cena) em paralelo, com asyncio.

Cada tarefa roda numa thread (asyncio.to_thread: o urllib bloqueia, mas solta o GIL enquanto espera a rede), e um
semáforo limita quantas chamadas ficam abertas ao mesmo tempo. Uma tarefa pode depender de outras e só começa quando
elas terminam: a cena espera as fichas de referência dela, não todas. Uma dependência que falhou não cancela a
tarefa; a função dela decide o que fazer (a cena sai sem a ficha que faltou).

    resultados = paralelo.rodar([Tarefa("P01", gerar_ficha), Tarefa("B01-01", gerar_cena, ("P01",))], limite=6)

O semáforo atende na ordem de chegada: com limite 1, as tarefas rodam na ordem da lista (respeitadas as dependências).
Uma exceção que a função deixou escapar é levantada de novo depois que todas as tarefas terminam.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Tarefa:
    id: str
    funcao: Callable[[], Any]
    depende_de: tuple[str, ...] = ()


def rodar(tarefas: list[Tarefa], limite: int, ao_terminar: Callable[[str, Any], None] | None = None) -> dict[str, Any]:
    """Roda as tarefas, no máximo `limite` ao mesmo tempo, e devolve {id: resultado}. `ao_terminar(id, resultado)` é
    chamado quando cada uma termina (para gravar o progresso: uma execução cancelada no meio não perde o que já foi
    pago)."""
    if not tarefas:
        return {}
    return asyncio.run(_rodar(list(tarefas), max(1, int(limite)), ao_terminar))


async def _rodar(tarefas: list[Tarefa], limite: int, ao_terminar) -> dict[str, Any]:
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=limite))
    semaforo = asyncio.Semaphore(limite)
    futuros: dict[str, asyncio.Task] = {}

    async def uma(t: Tarefa) -> Any:
        if deps := [futuros[d] for d in t.depende_de if d in futuros]:
            await asyncio.gather(*deps, return_exceptions=True)
        async with semaforo:
            resultado = await asyncio.to_thread(t.funcao)
        if ao_terminar:
            ao_terminar(t.id, resultado)
        return resultado

    # todas criadas antes de qualquer uma começar: quando a primeira roda, o mapa das dependências já está completo
    for t in tarefas:
        futuros[t.id] = asyncio.create_task(uma(t))
    resultados = dict(zip(futuros, await asyncio.gather(*futuros.values(), return_exceptions=True)))
    if erro := next((r for r in resultados.values() if isinstance(r, BaseException)), None):
        raise erro
    return resultados
