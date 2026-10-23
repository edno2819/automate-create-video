# Regras de formato

Regras que estrategista, roteirista, crítico e cortador precisam respeitar. Quando uma regra daqui conflitar
com uma técnica da biblioteca, vale a regra daqui.

Ritmo de referência: **150 palavras por minuto** em português falado. O vídeo de referência do Dor Pintada
tem cerca de 153 (2.369 palavras em 15:32). O pedido pode mudar a velocidade (`narracao.palavras_por_minuto`):
o roteiro é escrito para ela, a narração é acelerada ou desacelerada até chegar nela, e as contagens abaixo
mudam na mesma proporção (a 180, um minuto tem 180 palavras). `medir_roteiro.py` já usa a do pedido.

| Duração | Palavras (aprox.) |
|---|---|
| 5 s | 12 |
| 30 s | 75 |
| 1 min | 150 |
| 8 min | 1.200 |
| 12 min | 1.800 |
| 15 min | 2.250 |

## Clareza (todo formato)

O espectador ouve cada frase uma vez e não leu nada além do que ouve. Toda frase se entende de primeira:
sujeito sem ambiguidade, ação dita (não só o efeito), mudança de lugar, época ou papel explicada, ligação
com a frase anterior dita no texto e, em piadas e curiosidades, o motivo que dá a graça. Curiosidade é saber
o que falta; confusão é não entender o que foi dito. Quando clareza e velocidade conflitarem (gancho em 5
palavras, meta de duração), a clareza vence. O crítico reprova (critério `clareza`).

## Vídeo longo (8 a 15 min)

- **0 a 5 s:** promessa mais tensão. Nada de introdução, "fala galera", nome do canal ou pedido de inscrição.
- **0 a 30 s:** promessa mais prova de que o vídeo vai entregar. O espectador precisa sentir que já está
  recebendo o que o título prometeu.
- **Re-hook a cada 60 a 120 s** (150 a 300 palavras). Marque com `[RE-HOOK]` no roteiro.
- **Nenhum trecho sem pergunta aberta.** Se todos os loops estão fechados, o espectador não tem motivo para
  ficar. Esse é o ponto de abandono.
- **Virada no meio** (entre 40% e 65% da duração): algo muda o significado do que veio antes.
- **Blocos semiautônomos**, cada um com mini-gancho e mini-payoff. Isso segura a retenção do longo e permite
  os cortes. Um roteiro em fluxo contínuo gera cortes sem contexto.
- **CTA de inscrição:** no máximo um, contextual, entre 3 e 6 min. Nunca nos primeiros 30 s.
- **Duração honesta.** A partir de 8 min o vídeo permite anúncios no meio, mas esticar o conteúdo para bater
  essa marca derruba a retenção. Se o tema rende 6 minutos, o roteiro tem 6 minutos e o estrategista diz isso.
- **Payoff cumpre o título e a thumb.** Se o título promete "cada cargo", todos os cargos aparecem.

## Short (30 s a 3 min; ideal entre 30 e 60 s)

- **Gancho em 1 a 2 s** (5 palavras), verbal e visual juntos. O roteiro indica a imagem do primeiro frame
  em `[VISUAL: ...]`.
- **Uma ideia por vídeo.** Sem subtramas.
- **Sem respiro:** nenhuma frase de transição vazia. Cada frase adiciona informação ou tensão. A frase que
  diz como uma cena leva à outra não é respiro: sem ela o short vira uma lista de saltos.
- **Fecho com virada:** a última ideia muda ou amplia o sentido do que veio antes. Mais um fato não é fecho.
- **Final em loop**, quando possível: a última frase emenda na primeira para favorecer o replay. Se a emenda
  enfraquecer o fecho, o fecho vence.
- **Sem CTA falado** (a não ser que seja a própria emenda do loop).
- Métrica principal: **viu vs. passou** (no YouTube Studio, "Viewed vs. swiped away") e retenção. Views
  brutas enganam.

## Corte (short derivado de um longo)

- Parte de um bloco autônomo do longo, mas é **reescrito**: ganha gancho próprio de 1 a 2 s e não depende
  de nada dito antes.
- Não começa com "como eu disse", "nesse nível", nem referências a blocos anteriores.
- Termina com payoff próprio. Opcionalmente, uma frase que leva ao longo, sem citar "vídeo completo no
  canal" de forma genérica.

## Adequação a anunciantes

Temas de crime, violência e tragédia são monetizáveis quando tratados como informação e narrativa, não como
espetáculo. Prefira a consequência indireta (cartaz de desaparecido, cadeira vazia) à descrição gráfica.
Evite palavrões nos primeiros 30 s e no título. O crítico sinaliza riscos, mas não bloqueia por isso.

**Sexo e nudez** ficam na sugestão (um olhar, um bilhete, a porta que se fecha), nunca descritos nem
mostrados. **Personagem de visual infantil ou fofo** (fruta, bicho, boneco, animação com cara de desenho
para crianças) **nunca aparece em situação sexual**, mesmo num vídeo feito para adultos. Além de inaceitável,
é o que a política de segurança infantil do YouTube remove: animação com cara de conteúdo infantil e tema
sexual derruba o vídeo e pode render strike ao canal. O crítico reprova (critério `conteudo-sexual`).

## Natureza do conteúdo

Definida no brief:

- `factual`: todo fato verificável precisa estar no dossiê de pesquisa ou marcado com `[VERIFICAR: ...]`.
- `inspirado-em-fatos` (padrão do Dor Pintada): o contexto, os números e a história são reais, e os
  personagens com apelido são compostos e ilustrativos. Nunca atribua atos a pessoas reais identificáveis
  sem fonte.
- `ficcional`: liberdade total. Ainda assim, sem acusar pessoas reais.
