# Animação das imagens

Só vale quando `visual.animacao` no brief é `destaques` ou `todas` (a mensagem do orquestrador indica este arquivo).
Um modelo de vídeo anima a imagem de cada cena escolhida num clipe curto, que entra no lugar do zoom. Cartelas e
imagens da internet ficam paradas.

## Campos da cena (`08-cenas/<bloco>.json`)

| Campo | Regra |
|---|---|
| `animar` | `true` marca a ilustração para animar (em `destaques`); `false` a deixa parada (em `todas`). Em `destaques`, sem nenhuma marcada, animam a primeira ilustração de cada bloco e a última do vídeo. |
| `animacao` | Inglês, uma frase: o que se mexe dentro do quadro e como a câmera anda. Sem o campo, vai um movimento leve genérico, com a câmera do `movimento`. |
| `transicao` | Como a cena entra, vindo da anterior: `corte`, `fade` ou uma das transições abaixo. |

## Movimento

- Leve e dentro do quadro: cabelo, tecido, fumaça, água, luz, folhas, um gesto pequeno, um olhar. Nada de pessoa ou
  objeto novo, troca de cena ou ação grande (o modelo inventa e deforma). Boca falando também não.
- A câmera combina com o `movimento` da cena (push-in no `zoom-in`, pan lento no `pan-direita`).
- Em `destaques`, marque só o gancho, as viradas, a cena espelho e o payoff: no máximo uma a cada três ilustrações.

## Transições

As de vídeo são geradas no clipe da cena anterior, que termina na imagem desta: a anterior precisa animar (senão,
vira corte). Use poucas, nos momentos fortes e nas trocas de bloco, e com um efeito sonoro no corte quando a
biblioteca de efeitos existir (um whoosh no `whip-pan` e no `zoom-blur`).

| `transicao` | Effect | Use for |
|---|---|---|
| `whip-pan` | Fast whip pan with motion blur into the next shot. | fast pace, jump in place or time |
| `zoom-blur` | Fast stylized crash zoom with motion blur. | reveals, jokes, abrupt shifts |
| `match-cut` | Same shape, object or motion kept in place across both shots. | visual echo, before/after, mirror scene |
| `push` | Next shot slides in and pushes the current one out. | moving to the next topic or level |
| `wipe` | Foreground object crosses the camera, revealing the next shot. | cinematic entry into a new place |
| `glitch` | Brief digital glitch: noise, RGB split, pixel distortion. | error, irony, tech |
| `j-cut` | Edit only: the next line starts, the image changes 0.5 s later. | invisible story cuts |
| `l-cut` | Edit only: the image changes 0.5 s before the next line. | invisible story cuts |

No `match-cut`, escreva os dois `prompt_cena` com a forma em comum no mesmo lugar do quadro. `j-cut` e `l-cut` não
precisam de animação.

```json
{"trecho": "E naquela noite, a vela apagou sozinha.", "tipo": "ilustracao", "enquadramento": "detalhe",
 "prompt_cena": "a single candle on a wooden table, its flame bending toward a dark window", "movimento": "zoom-in",
 "transicao": "zoom-blur", "animar": true, "animacao": "the flame flickers, thin smoke drifts up; slow push-in"}
```
