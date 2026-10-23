# Formato da direção visual

A direção visual transforma o roteiro em uma lista de cenas, cada uma com a descrição da imagem e o prompt
para gerá-la. São dois tipos de arquivo escritos pelo agente `diretor-visual` e um script que monta o resto:

| Arquivo | Quem escreve | Conteúdo |
|---|---|---|
| `08-biblia-visual.json` | diretor visual | estilo, personagens, locais e objetos, cada um com uma descrição fixa |
| `08-cenas/B01.json`, `B02.json`... | diretor visual | as cenas de cada bloco do roteiro, em ordem |
| `08-thumbnail.json` | diretor visual | a imagem da thumbnail e o texto que vai por cima dela |
| `08-cenas.json` | `ferramentas/cenas.py` | todas as cenas, com id, tempo e prompt final montado, e a thumbnail |
| `08-storyboard.md` | `ferramentas/cenas.py` | o roteiro com a descrição de cada imagem, para leitura |
| `08-prompts.jsonl` | `ferramentas/cenas.py` | um prompt de imagem por linha, para geração em lote |
| `08-referencias.jsonl` | `ferramentas/cenas.py` | prompts das fichas de personagens e locais (imagens de referência) |

```bash
python3 ferramentas/cenas.py montar projetos/<slug>
```

## Como a coerência é garantida

O agente **nunca escreve a aparência de um personagem dentro de uma cena**. Ele descreve cada personagem,
local e objeto uma única vez na bíblia, e o script cola essa descrição, palavra por palavra, em todos os
prompts em que o personagem aparece, junto com o estilo. Assim o mesmo personagem tem sempre a mesma
descrição, do primeiro ao último prompt. Quando a geração de imagens for escolhida, as fichas de
`08-referencias.jsonl` geram as imagens de referência de cada personagem e local, usadas como referência
visual em todas as cenas.

## `08-biblia-visual.json`

```json
{
  "projeto": "escada-cv",
  "proporcao": "16:9",
  "estilo": {
    "nome": "Pintura digital sombria",
    "descricao": "Ilustração pintada com pinceladas visíveis, luz de cinema, paleta dessaturada com acentos vermelhos.",
    "prompt_base": "dark painterly digital illustration, visible brush strokes, cinematic lighting, muted desaturated palette with deep red accents, subtle film grain",
    "negativo_base": "text, letters, watermark, logo, gore, blood, extra fingers, deformed hands, blurry face",
    "regras_de_camera": "O protagonista ('você') aparece de costas, de perfil ou em POV das mãos; o rosto inteiro só nas viradas."
  },
  "personagens": [
    {
      "id": "P01",
      "nome": "Você (protagonista)",
      "referencia_en": "the protagonist",
      "descricao": "the protagonist, a thin 19-year-old Brazilian man with brown skin, short curly black hair, a faint scar on the left eyebrow, wearing a faded red t-shirt and worn flip-flops",
      "evolucao": [
        {"a_partir_de": "B04", "descricao": "the protagonist, a 21-year-old Brazilian man with brown skin, short curly black hair, a faint scar on the left eyebrow, now muscular, wearing a black tactical vest over a gray t-shirt"}
      ]
    }
  ],
  "locais": [
    {
      "id": "L01",
      "nome": "Laje no alto do morro",
      "descricao": "a rooftop slab of unfinished red brick at the top of a dense Rio favela hillside, laundry lines, water tanks, the city below",
      "descricao_curta": "an unfinished red brick rooftop, a blue water tank in the background"
    }
  ],
  "objetos": [
    {"id": "O01", "nome": "Rádio comunicador", "descricao": "a scratched black walkie-talkie with a bent antenna"}
  ]
}
```

- `prompt_base` e `negativo_base` em inglês: a maioria dos geradores de imagem entende melhor inglês. Os
  campos `nome`, `descricao` do estilo e `regras_de_camera` ficam em português, para leitura.
- `biblioteca` (opcional, personagens): o slug de um personagem de `biblioteca/personagens/`. A imagem
  aprovada dele vira a ficha de referência do projeto, sem gerar outra.
- `descricao` de personagens, locais e objetos: inglês, completa e autossuficiente. Comece pela expressão de
  `referencia_en` ("the protagonist, a..."), para que o prompt da cena possa citar o personagem por ela.
- `descricao_curta` de locais: o fundo que aparece num plano fechado (`close`, `detalhe`, `pov`), com os
  mesmos materiais e cores da descrição completa, sem a vista geral. O script usa essa versão nesses planos;
  sem ela, usa a descrição completa, que costuma brigar com um close de rosto.
- `evolucao`: quando a aparência muda ao longo do vídeo (idade, roupa, ferimento), a partir do bloco
  indicado vale a nova descrição, que substitui a anterior inteira.
- Inclua na bíblia todo personagem, local ou objeto que aparece em **duas ou mais** cenas. Os que aparecem
  uma vez só podem ser descritos direto no `prompt_cena`.

## `08-cenas/<bloco>.json`

Uma lista de cenas na ordem da narração do bloco:

```json
[
  {
    "trecho": "Nível 1: O olheiro.",
    "tipo": "cartela",
    "texto_na_tela": "NÍVEL 1 — O OLHEIRO",
    "descricao": "Cartela de abertura sobre a laje vazia ao amanhecer.",
    "enquadramento": "plano-geral",
    "personagens": [],
    "local": "L01",
    "objetos": [],
    "prompt_cena": "an empty rooftop at dawn, mist over the hillside, quiet and ominous",
    "movimento": "zoom-in",
    "transicao": "corte"
  },
  {
    "trecho": "Você tem 19 anos. Até março, você era entregador de aplicativo.",
    "tipo": "ilustracao",
    "descricao": "O protagonista sentado na laje, de costas, olhando a cidade.",
    "enquadramento": "sobre-o-ombro",
    "personagens": ["P01"],
    "local": "L01",
    "objetos": ["O01"],
    "prompt_cena": "the protagonist sits on the edge of the rooftop seen from behind, holding the walkie-talkie, looking down at the city",
    "movimento": "zoom-in",
    "transicao": "corte"
  }
]
```

| Campo | Regra |
|---|---|
| `trecho` | Texto **exato** da narração coberto pela cena. Os trechos de um bloco, em sequência, têm de reproduzir a narração inteira do bloco, sem pular nem repetir nada. Marcadores como `[RE-HOOK]` não entram. |
| `tipo` | `ilustracao` (imagem gerada) ou `cartela` (texto na tela, com fundo opcional). |
| `texto_na_tela` | Obrigatório na cartela; opcional na ilustração (número, data, nome de lugar). O texto é aplicado na montagem, nunca dentro da imagem gerada. |
| `descricao` | Português, uma ou duas frases: o que a imagem mostra e por quê. |
| `enquadramento` | `plano-geral`, `plano-medio`, `close`, `detalhe`, `pov`, `sobre-o-ombro`, `aereo`, `contra-plongee` ou `plongee`. O script traduz para o prompt. |
| `personagens`, `local`, `objetos` | Ids da bíblia. |
| `prompt_cena` | Inglês, até ~60 palavras: sujeito, ação, composição, clima. Comece pelo sujeito. **Sem** enquadramento (vem do campo `enquadramento`), **sem** estilo e **sem** aparência de personagens (vêm da bíblia). Cite personagens pela `referencia_en`. Na cena da internet, é a reserva. |
| `fonte` | `ia` (padrão: imagem gerada) ou `internet` (imagem real de banco livre; só se o pedido permitir, em `visual.imagens_da_internet`). |
| `busca` | Obrigatória na cena da internet: o texto procurado no Wikimedia Commons e no Openverse, em inglês, ou uma lista de 2 ou 3 alternativas, da mais específica à mais geral. |
| `movimento` | Para a montagem: `estatico`, `zoom-in`, `zoom-out`, `pan-esquerda`, `pan-direita`, `subir` ou `descer`. |
| `transicao` | Para a montagem: `corte` ou `fade`. |

Uma cena com imagem da internet:

```json
{
  "trecho": "Paris, século 13. Trinta e duas casas de banho.",
  "tipo": "ilustracao",
  "fonte": "internet",
  "busca": ["medieval bathhouse illumination", "medieval bathhouse", "public bath middle ages"],
  "descricao": "Iluminura medieval de uma casa de banho, a prova de que o banho era comum.",
  "enquadramento": "plano-geral",
  "personagens": [],
  "prompt_cena": "a crowded medieval public bathhouse with wooden tubs and steam",
  "movimento": "zoom-in",
  "transicao": "corte"
}
```

A imagem é baixada por `skills/produzir-video/scripts/buscar_imagens.py`, só entre as de domínio público ou
Creative Commons com uso comercial, e o crédito vai para `09-imagens/creditos.md`. Se a proporção dela não
for a do vídeo, a montagem a mostra inteira sobre uma cópia desfocada. Se a busca não achar nada, o
`prompt_cena` gera a imagem por IA.

O script calcula `id` (`B01-01`, `B01-02`...), `bloco`, `inicio`, `fim`, `duracao_s`, `prompt_final` e
`negativo_final`. Os tempos são estimados pela contagem de palavras (150 por minuto, ou o `ppm` do
roteiro); quando houver narração gravada, eles serão substituídos pelos tempos reais.

## `08-thumbnail.json`

A thumbnail do vídeo, a partir do conceito do ângulo escolhido (`thumbnail` em `04-angulo-escolhido.json`). Tem
os campos de uma ilustração, sem `trecho`, `movimento` e `transicao`, e mais o texto:

```json
{
  "descricao": "O camponês de frente, bota afundada na lama, cara de nojo; ao fundo, desfocado, o rei na janela.",
  "enquadramento": "plano-medio",
  "personagens": ["P01", "P02"],
  "local": "L01",
  "objetos": [],
  "prompt_cena": "the peasant faces the viewer with an exaggerated expression of disgust, one boot sunk ankle-deep in dark mud; far behind him, blurred, the king watches from a palace window",
  "texto": "VOCÊ AGUENTARIA?",
  "posicao_texto": "alto"
}
```

| Campo | Regra |
|---|---|
| `prompt_cena` | Como o de uma cena, com um sujeito só em destaque, emoção legível num celular e fundo limpo. O script acrescenta o pedido de composição de thumbnail, as descrições da bíblia e o estilo, e as fichas de referência vão junto, como nas cenas. |
| `texto` | O texto da thumbnail do ângulo, até 4 palavras, ou vazio. A montagem o escreve por cima, em caixa alta, com contorno; nunca dentro da imagem gerada. |
| `posicao_texto` | `alto`, `baixo`, `esquerda` ou `direita`: a parte do quadro que a imagem deixa livre para o texto. Evite `baixo` no vídeo horizontal (o YouTube põe a duração no canto de baixo) e lembre que a legenda do vídeo não aparece na thumbnail. |
| `bloco` | Opcional: o bloco cuja aparência dos personagens vale (`evolucao` da bíblia). Padrão: o primeiro. |

`gerar_imagens.py` gera `09-imagens/thumbnail.png` (ou `.jpg`) e `montar_video.py` grava `10-video/thumbnail.jpg`, com o
texto, no tamanho do vídeo e abaixo de 2 MB, e a usa como primeiro quadro do vídeo. Sem `08-thumbnail.json`,
a thumbnail sai da primeira cena, com o texto do ângulo.
