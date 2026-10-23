---
name: direcao-visual
description: Recebe um roteiro pronto (colado, em arquivo ou de um projeto existente), com ou sem um pedido em YAML (pedidos/*.yaml), e devolve uma pasta em projetos/<slug>/ com o roteiro formatado em blocos e a direção visual completa — bíblia visual (estilo, personagens, locais com descrição fixa), uma cena por trecho narrado com descrição da imagem, tempo estimado e prompt de geração, storyboard legível e prompts em lote. Use quando o usuário entregar um roteiro e pedir as imagens, as cenas, o storyboard, a descrição das imagens ou a direção visual ("aqui está meu roteiro", "gera as cenas desse roteiro", "descreve as imagens", "faz o storyboard"), ou quando um projeto do pipeline chegar à fase visual.
---

# Direção visual: do roteiro às cenas

Entrada: um roteiro e, se o usuário tiver, um pedido em YAML (`pedidos/*.yaml`, no formato de
`pedidos/modelo.yaml`). Saída: `projetos/<slug>/` com o roteiro em blocos e a descrição de cada imagem, com
os prompts prontos para a skill `produzir-video`.

## O que o usuário recebe

| Arquivo | Conteúdo |
|---|---|
| `00-pedido.yaml` | o pedido completo e aprovado: tema, formato, estilo artístico, tom, voz, legendas, música |
| `00-brief.json` | o mesmo pedido em JSON, gerado por `ferramentas/pedido.py`, que os agentes e scripts leem |
| `00-roteiro-original.md` | o roteiro exatamente como foi entregue |
| `06-roteiro-final.md` | o mesmo texto, dividido em blocos, com metadados e marcadores |
| `08-biblia-visual.json` | estilo, personagens, locais e objetos com descrição fixa |
| `08-cenas/<bloco>.json` | as cenas de cada bloco, escritas pelo diretor visual |
| `08-thumbnail.json` | a imagem da thumbnail e o texto que vai por cima, escritos pelo diretor visual |
| `08-storyboard.md` | **o arquivo para ler**: cada trecho narrado com a imagem, o tempo e o prompt |
| `08-cenas.json` | todas as cenas, com tempos e prompts finais |
| `08-prompts.jsonl` | um prompt por linha, para gerar as imagens em lote |
| `08-referencias.jsonl` | prompts das fichas de personagens e locais, para gerar as imagens de referência |

## Passos

### 1. Receber o roteiro

- **Colado na conversa:** grave em `projetos/<slug>/00-roteiro-original.md` exatamente como veio, sem
  corrigir nada.
- **Caminho de arquivo:** copie com `cp` para `00-roteiro-original.md` (não reescreva o texto).
- **Projeto existente** com `06-roteiro-final.md` e `00-brief.json`: pule para o passo 4. Projeto antigo,
  sem `00-pedido.yaml`, usa o brief como está; para mudar opções nele, crie o pedido (passo 2) com
  `--sobrescrever`, depois de confirmar com o usuário.

O slug é curto, em kebab-case, sem acentos, tirado do tema. Se a pasta já existir, pergunte antes de
gravar por cima.

### 2. Pedido

Siga a fase 0 da skill `novo-video` (`skills/novo-video/SKILL.md`): mesmo script, mesmas regras de
decisão, mesma aprovação do usuário antes de seguir. Aqui o roteiro já responde boa parte:

- **Pedido do usuário:** se ele passou um arquivo YAML junto com o roteiro, use-o. Senão, grave
  `pedidos/<slug>.yaml` com o que ele disse na conversa (e o título, se o roteiro trouxer um).
- **Decisões tiradas do roteiro:** tema, tom e natureza. O formato vem da contagem de palavras (`wc -w`):
  até ~450 palavras (3 min) é `short`; acima disso, `longo`. Proporção e ritmo vêm do formato.
- **Até onde ir:** `--definir producao.ate=visual`, a não ser que o usuário tenha pedido o vídeo.
- **Estilo artístico:** é a decisão que mais pesa no resultado. Na aprovação, ofereça o estilo que você
  escolheu e até três alternativas, como as da tabela de decisão da `novo-video`. O usuário pode descrever
  outro estilo ou citar uma referência.

Depois da aprovação, gere o brief (`uv run --script ferramentas/pedido.py brief projetos/<slug> 2>&1 | cat`).

### 3. Formatar o roteiro

- Se `00-roteiro-original.md` já estiver no formato do sistema (tem cabeçalhos `## [B01]`), copie para
  `06-roteiro-final.md`.
- Senão, dispare o agente **`formatador`** com a pasta do projeto. Depois confira você mesmo:

  ```bash
  python3 ferramentas/conferir_texto.py projetos/<slug>/00-roteiro-original.md projetos/<slug>/06-roteiro-final.md
  ```

  Se não sair `ok`, devolva ao formatador com a diferença apontada. Não siga com o texto alterado.

### 4. Direção visual

Dispare o agente **`diretor-visual`** com a pasta do projeto. Se o brief não tiver `estilo_artistico`,
volte ao passo 2 antes. Com `visual.imagens_da_internet`, ele pode marcar cenas para usar imagens reais de
bancos livres (`"fonte": "internet"`).

### 5. Conferir

Rode a montagem você mesmo:

```bash
python3 ferramentas/cenas.py montar projetos/<slug>
```

Com erro, devolva ao diretor visual com os erros. Não entregue com erro.

### 6. Entregar

Responda ao usuário com:

- o caminho da pasta e o de `08-storyboard.md`, que é o arquivo para ler;
- número de blocos, duração estimada, número de cenas e duração média por cena;
- o estilo e a bíblia em poucas linhas (personagens e locais), e as decisões de direção relevantes, como o
  modo de mostrar o protagonista e quais cenas usam imagem da internet;
- a thumbnail: o que a imagem mostra e o texto por cima;
- os avisos que sobraram;
- o que dá para ajustar: estilo, ritmo (`visual.segundos_por_imagem`), um personagem. Ajuste na bíblia ou
  numa cena se resolve rodando só a montagem, sem refazer tudo;
- se quer que o `critico` avalie o roteiro (útil para roteiros escritos fora do sistema);
- se quer seguir para as imagens e o vídeo, com a skill `produzir-video`. Se o pedido diz
  `producao.ate: video`, siga direto.

## Ajustes depois da entrega

- **Mudar a aparência de um personagem ou o estilo:** edite `08-biblia-visual.json` e rode `cenas.py
  montar`. Todos os prompts mudam juntos.
- **Refazer as cenas de um bloco:** apague `08-cenas/<bloco>.json` e dispare o `diretor-visual` pedindo só
  aquele bloco, com a bíblia existente.
- **Mudar o ritmo:** altere `visual.segundos_por_imagem` em `00-pedido.yaml`, gere o brief de novo e
  refaça as cenas.
