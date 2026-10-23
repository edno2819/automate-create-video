# Formato do arquivo de roteiro

Todo roteiro (longo, short ou corte) segue este formato. A ferramenta
[ferramentas/medir_roteiro.py](../ferramentas/medir_roteiro.py) lê o arquivo, conta palavras por bloco,
estima os tempos e aponta problemas objetivos (trechos longos sem re-hook, trechos sem loop aberto, frases
proibidas no início). O roteirista escreve nesse formato e o crítico avalia a partir dele.

## Estrutura

```markdown
---
projeto: escada-cv
versao: 1
formato: longo            # longo | short
duracao_alvo: "12:00"     # mm:ss
ppm: 150                  # palavras por minuto: narracao.palavras_por_minuto do brief (padrão 150)
titulo: "Como É Ser Cada Cargo de um Navio Pirata"
thumbnail: "Rosto de grumete jovem em primeiro plano, capitão desfocado ao fundo. Texto: 'NÍVEL 1'"
tecnicas: [escada-de-niveis-pov, pov-segunda-pessoa, cena-espelho, callback]
tipo_gancho: pov-segunda-pessoa
---

## [B01] Nível 1: o grumete
> função: gancho · abre: L1, L2 · fecha: — · conector: — · cortável: sim

Nível 1: o grumete. Você tem 14 anos...

[RE-HOOK] Mas limpar o convés é o degrau mais baixo...

## [B02] Nível 2: o artilheiro
> função: desenvolvimento · abre: L3 · fecha: L2 · conector: mas · cortável: sim

...
```

## Regras

- **Cabeçalho de bloco:** `## [B01] Título curto`. Ids sequenciais com dois dígitos: B01, B02...
- **Linha de metadados**, logo abaixo do cabeçalho, começando com `>` e campos separados por `·`:
  - `função`: gancho, contexto, desenvolvimento, virada, clímax, payoff, final;
  - `abre`: ids dos loops que este bloco abre (L1, L2...). Use `—` se nenhum;
  - `fecha`: ids dos loops que este bloco fecha;
  - `conector`: como o bloco se liga ao anterior: `mas`, `então` ou `—` no primeiro bloco. Se a relação
    for só sequência ("e depois"), escreva `e-depois`: o crítico vai cobrar;
  - `cortável`: `sim` se o bloco funciona sozinho como short depois de ganhar um gancho próprio.
- **Loops:** liste todos no fim do arquivo, numa seção `## Loops`, uma linha por loop:
  `- L1: pergunta que o loop abre`.
- **Marcadores na narração** (não são lidos em voz alta e não contam como palavras):
  - `[RE-HOOK]` no início do parágrafo que funciona como re-hook;
  - `[VERIFICAR: o que checar]` logo depois de um fato que não está no dossiê;
  - `[VISUAL: descrição]` para indicações visuais essenciais (obrigatório no primeiro bloco de shorts);
  - `[PAUSA]` para silêncio dramático;
  - `[FALA: Nome]` ou `[FALA: Nome, como dizer]` no início da linha que um personagem fala (veja Diálogo).
- **Narração:** todo o resto do bloco. Escreva para o ouvido: frases curtas, números por extenso quando
  ficarem mais naturais falados, sem parênteses, sem siglas que não se leem em voz alta.
- Nada além disso entra no corpo do bloco. Notas para o editor ficam fora dos blocos, numa seção final
  `## Notas`.

## Diálogo

Quando o pedido tem `personagens`, eles podem falar com a própria voz. A linha que começa com
`[FALA: Nome]` é do personagem, até o fim da linha; as linhas sem a marca são do narrador.

```markdown
## [B02] A câmara fria
> função: virada · abre: — · fecha: L1 · conector: mas · cortável: não

[FALA: Pepinão, sussurrando] Hoje, depois do expediente?
[FALA: Moranguete] Na câmara fria. Ninguém pode saber.
E o Abacate ouviu tudo.
[FALA: Abacate, chorando] Moranguete... era tudo mentira?
```

- **Nome:** exatamente como está em `personagens` no pedido (acentos e maiúsculas não importam). Quem não
  está lá não tem voz: o `medir_roteiro.py` acusa erro.
- **Como dizer** (opcional, depois da vírgula): uma direção curta para a voz nesta fala: `chorando`,
  `sussurrando`, `gritando`, `com deboche`. Não é narrada.
- **Uma fala por linha**, curta (até ~20 palavras; acima de 40 o medidor avisa). Cada fala é gerada
  separadamente, na voz do personagem.
- Sem "disse ela" depois da fala: a voz já mostra quem fala. O narrador situa a cena; os personagens agem.
- `[FALA: Narrador, como dizer]` dá uma direção a uma linha do narrador.
- A marca e o nome não contam como palavras e não entram no `trecho` das cenas.
