---
name: produzir-video
description: Produz o vídeo de um projeto que já tem direção visual (08-cenas.json), seguindo as opções do pedido (00-pedido.yaml) — busca imagens reais em bancos livres (Wikimedia Commons, Openverse) para as cenas marcadas, gera as outras com o Gemini (Nano Banana) usando fichas de referência para manter os personagens coerentes, narra o roteiro com o Gemini TTS (uma voz para o narrador e uma para cada personagem que fala, ou o espeak local para testar de graça), alinha cada palavra ao áudio com o faster-whisper e monta o vídeo com ffmpeg (movimento de câmera, cartelas, legendas no tempo da voz com destaque colorido na palavra dita, música de fundo que abaixa na fala e áudio normalizado). Use quando o usuário quiser as imagens, a narração, a dublagem, as vozes dos personagens, a música ou o vídeo ("gera as imagens", "busca imagens na internet", "narra o roteiro", "coloca a voz", "testa vozes", "voz da Moranguete", "põe música", "monta o vídeo", "renderiza"), ou quando o pipeline novo-video chegar à produção.
---

# Produzir vídeo: imagens, narração e montagem

Entrada: um projeto com `08-cenas.json` (gerado pela direção visual) e o brief gerado do pedido. Saída: as
imagens em `09-imagens/`, a narração em `09-narracao/` e o vídeo em `10-video/<slug>.mp4`, com legendas no
tempo da voz. Sem narração, o vídeo usa os tempos estimados (`narracao.palavras_por_minuto`, padrão 150).

O pedido (`00-pedido.yaml`, lido pelos scripts via `00-brief.json`) decide o que entra:

| Opção do pedido | Efeito |
|---|---|
| `visual.imagens_da_internet` | cenas com `"fonte": "internet"` usam imagem real de banco livre (passo 1) |
| `visual.qualidade` | modelo das imagens de IA: `rascunho`, `padrao` ou `maxima` (passo 3) |
| `narracao.ativa`, `narracao.voz`, `narracao.estilo` | se há voz, qual é a do narrador e como ele fala (passo 5) |
| `narracao.fornecedor` | `gemini` (Gemini TTS), `openai` (TTS da OpenAI), `elevenlabs` (vozes da conta) ou `espeak` (robótica, local e grátis, para testar) (passo 5) |
| `personagens` | quem fala com voz própria: a voz, o jeito de falar e a cor na legenda de cada um (passos 5 e 7) |
| `legendas.*` | legenda ligada, destaque colorido na palavra dita, cores, caixa alta, palavras por vez, posição (passo 7) |
| `musica.*` | música de fundo: arquivo, ponto de início e volume por baixo da voz (passo 7) |

Mudou o pedido? Gere o brief de novo (`uv run --script ferramentas/pedido.py brief projetos/<slug> 2>&1 | cat`);
os scripts avisam quando o brief está desatualizado.

## Pré-requisitos

- `projetos/<slug>/08-cenas.json` sem erros. Se não existir, rode antes a skill `direcao-visual`.
- Chave da Gemini API em `GEMINI_API_KEY` ou no arquivo `.env` da raiz (protegido com `chmod 600` e
  fora do controle de versão). **Nunca imprima a chave** em comandos, logs ou respostas.
- **Faturamento ativo no projeto do Google.** No plano gratuito a cota de geração de imagem é zero, em
  todos os modelos. Para ativar: [aistudio.google.com/apikey](https://aistudio.google.com/apikey), no
  projeto da chave, "Set up billing" (vincula uma conta do Cloud Billing).
- Com música no pedido: o arquivo em `biblioteca/musicas/`, com as palavras de `musica.nome` no nome do
  arquivo (ou o caminho em `musica.arquivo`). O sistema não baixa música; peça o arquivo ao usuário.
- `ffmpeg` com libass e libfreetype (já instalado nesta máquina).

## Passos

```bash
B=skills/produzir-video/scripts/buscar_imagens.py
G=skills/produzir-video/scripts/gerar_imagens.py
N=skills/produzir-video/scripts/narrar.py
A=skills/produzir-video/scripts/alinhar.py
M=skills/produzir-video/scripts/montar_video.py
```

As imagens (passos 1 a 4) e a narração (passos 5 e 6) não dependem uma da outra: podem rodar em qualquer
ordem. Dentro das imagens, busque as da internet antes de gerar: a cena que a busca não resolve vira imagem
de IA pelo prompt de reserva.

### 1. Buscar as imagens da internet (se houver cenas com `"fonte": "internet"`)

```bash
python3 $B projetos/<slug> --simular
python3 $B projetos/<slug>
```

Procura a `busca` de cada cena no Wikimedia Commons e no Openverse, só entre imagens de domínio público ou
Creative Commons que permitem uso comercial, e baixa a melhor candidata em `09-imagens/<id>.jpg`. Os
créditos saem em `09-imagens/creditos.md`, para a descrição do vídeo. Não custa nada e não usa chave.

- Cena sem resultado: o script avisa. Troque a `busca` em `08-cenas/<bloco>.json` (termos mais gerais, ou
  uma lista de alternativas), rode `python3 ferramentas/cenas.py montar projetos/<slug>` e busque de novo
  com `--cenas <id> --refazer`. Ou deixe: o passo 3 gera a imagem pelo prompt de reserva.
- Imagem ruim na revisão (passo 4): `--cenas <id> --opcao 2` troca pela segunda candidata da mesma busca.

### 2. Simular

```bash
python3 $G projetos/<slug> --simular
```

Mostra quantas fichas de referência e quantas cenas serão geradas. **Acima de 30 imagens, confirme com o
usuário antes**, porque cada imagem é cobrada. Um short de 45 s tem cerca de 15 cenas e de 3 a 6 fichas.

### 3. Gerar

```bash
python3 $G projetos/<slug>
```

O script gera primeiro as fichas (personagens e locais) e depois as cenas, mandando junto as fichas de cada
cena, e a thumbnail do mesmo jeito (`09-imagens/thumbnail.png` ou `.jpg`, sem texto: o texto entra na
montagem). O que já foi gerado é pulado, a não ser que o prompt tenha mudado. As cenas da internet ficam de
fora, menos as que a busca não resolveu. O modelo vem de `visual.qualidade` no pedido (veja a tabela no fim).

| Código de saída | Significado | O que fazer |
|---|---|---|
| 0 | tudo gerado | seguir |
| 1 | algumas cenas falharam | ver os erros; cena bloqueada pelo filtro do Gemini → suavizar o `prompt_cena` em `08-cenas/<bloco>.json`, rodar `ferramentas/cenas.py montar` e gerar só aquela cena |
| 2 | falta arquivo ou chave | corrigir o pré-requisito |
| 3 | cota zero | o projeto do Google precisa de faturamento (veja os pré-requisitos). Ofereça seguir com `--provisorio` |

Opções úteis: `--cenas B01-03,B02-01` (só essas; `--cenas thumbnail` só a thumbnail), `--refazer` (gera de
novo), `--limite 3` (teste rápido, sem a thumbnail), `--modelo` (sobrepõe a qualidade do pedido),
`--sem-referencias` (mais barato, menos coerente), `--paralelo N`.

**Sem imagens reais** (sem faturamento, ou para validar ritmo e legendas antes de pagar):

```bash
python3 $G projetos/<slug> --provisorio
```

Desenha quadros de rascunho com o id e a descrição de cada cena. A montagem usa esses quadros quando falta a
imagem real.

### 4. Revisar as imagens

Abra `09-imagens/folha-de-contato.jpg` com a ferramenta de leitura (ela mostra imagens) e confira:

- o protagonista e os personagens recorrentes têm o mesmo rosto, cabelo e roupa em todas as cenas;
- não há letras ou textos deformados dentro das imagens;
- não há violência gráfica, nudez nem algo que contradiga a descrição da cena;
- as imagens da internet mostram o que a narração diz, sem marca d'água nem texto que atrapalhe.

Abra também a thumbnail (`09-imagens/thumbnail.png` ou `.jpg`): um sujeito só em destaque, a emoção legível
e a parte do quadro de `posicao_texto` livre para o texto. Para olhar uma cena de perto, abra
`09-imagens/<id>.png` (ou `.jpg`). Refaça as ruins com `--cenas <ids> --refazer`, no máximo duas vezes por
cena. Se uma ficha de referência saiu ruim, refaça a
ficha com `--cenas <ids das cenas que a usam> --refazer` depois de apagar `09-imagens/referencias/<id>.png`.
Imagem da internet ruim: `python3 $B projetos/<slug> --cenas <id> --opcao N`.

### 5. Narrar (se `narracao.ativa` não for `false`)

**Vozes.** A do narrador vem de `narracao.voz` no pedido (por padrão, a do canal em `biblioteca/canal.json`);
a de cada personagem, de `personagens[].voz`. Só teste vozes se o usuário pedir: gere a primeira fala com
algumas vozes e peça a ele que ouça os arquivos de `09-narracao/teste-vozes/` e escolha:

```bash
python3 $N --listar-vozes                                                   # nome, gênero (f/m) e característica
python3 $N projetos/<slug> --testar-vozes Charon,Algenib,Sadaltager         # o narrador
python3 $N projetos/<slug> --testar-vozes Leda,Kore,Aoede --personagem Moranguete
```

Grave a escolha em `00-pedido.yaml` (`narracao.voz` ou a `voz` do personagem) e gere o brief de novo; se for
a nova voz do canal, grave também em `biblioteca/canal.json`. `narracao.estilo` e `personagens[].estilo`
dizem como cada um fala; sem eles, o estilo vem do `tom`.

**Diálogo.** As linhas do roteiro com `[FALA: Nome]` são geradas uma a uma, na voz do personagem, com o
jeito de falar dele e a direção da fala (`[FALA: Abacate, chorando]`); o script tira o silêncio das pontas
de cada fala e as emenda no áudio do bloco, com 0,25 s entre elas (`--intervalo-falas`). Personagem que fala
no roteiro e não está no pedido, ou está sem voz, para a narração com erro.

**Teste grátis.** Com `--fornecedor espeak` (ou `narracao.fornecedor: espeak` no pedido), a narração sai
na hora, local e sem custo, com voz robótica: serve para conferir falas, ritmo, alinhamento e montagem antes
de pagar. Cada voz do pedido vira uma variante do espeak do mesmo gênero. O alinhamento erra mais nessa voz;
para o vídeo final, narre de novo com `--fornecedor gemini`.

**Narração:**

```bash
python3 $N projetos/<slug>
```

Gera um WAV por bloco do roteiro (por fala, nos blocos com diálogo) e emenda tudo em
`09-narracao/narracao.wav`. O script confere a duração de cada áudio contra o número de palavras e transcreve
cada um para conferir o texto, e gera de novo sozinho quando a voz repete o texto ou lê a instrução de
estilo (em teste, isso aconteceu em parte das chamadas, mais nas falas curtas). Uma fala com emoção forte
(choro, grito) pode passar do tempo esperado; o aviso de duração, aí, é só informativo. Opções: `--voz`,
`--estilo`, `--modelo`, `--blocos B02 --refazer` (refaz um bloco), `--intervalo` (silêncio entre blocos,
padrão 0,35 s).

**Velocidade:** o script mede o ritmo em que a voz falou e acelera ou desacelera a fala inteira pelo mesmo
fator até `narracao.palavras_por_minuto` do pedido, sem mudar o tom (ffmpeg `atempo`, entre 0,8x e 1,35x;
fora disso ele avisa, e vale pedir um ritmo mais rápido ou mais lento em `narracao.estilo`). `--ppm N`
sobrepõe o pedido e `--velocidade 1.15` fixa o fator. Com a narração mais longa que a meta (as vozes do Gemini
costumam esticar as reticências), `--pausa-max 0.3` encurta as pausas dentro das falas. Velocidade e pausas
valem na emenda, sem gerar nada de novo. Com a narração desligada no pedido, o script não gera nada
(`--forcar` narra mesmo assim).

O estilo vai num prompt estruturado (perfil, notas de direção, transcrição). Não troque por "Diga com tom
X: texto": nesse formato a voz lê a instrução em voz alta.

### 6. Alinhar

```bash
uv run --script $A projetos/<slug> 2>&1 | cat
```

Transcreve cada bloco com o faster-whisper (CPU, modelo `small`; o primeiro uso baixa o modelo) e casa as
palavras reconhecidas com as do roteiro. Grava `09-narracao/tempos.json` com o início de cada cena e de cada
palavra. Também confere a voz: sai com código 1 e um alerta quando um bloco tem menos de 85% das palavras
reconhecidas ou palavras que não estão no roteiro. Nesse caso, refaça o bloco (`python3 $N projetos/<slug>
--blocos <id> --refazer`) e alinhe de novo. Se as cenas mudarem depois do alinhamento, alinhe de novo.
A última imagem fica 0,6 s na tela depois da última palavra; para segurar um final (o sorriso, a reação),
use `--cauda 1.5`.

### 7. Montar

```bash
python3 $M projetos/<slug>
```

Com `09-narracao/tempos.json`, cada imagem troca quando a voz chega ao trecho dela e cada legenda entra com
a primeira palavra que mostra. Do pedido vêm:

- **legendas:** com `destacar_palavra`, a palavra dita ganha um fundo na `cor_destaque` (a letra por cima
  fica preta ou branca, pelo contraste); sem narração, o destaque segue o ritmo estimado. Nas falas dos
  personagens, o fundo é a `cor` de cada um, e um pedaço de legenda nunca junta duas vozes;
- **música:** começa em `musica.inicio`, repete até o fim, entra e sai em fade; por baixo da voz fica em
  `musica.volume` % e abaixa sozinha quando a voz fala. A voz sai normalizada a -14 LUFS, o volume de
  referência do YouTube.

Imagem com proporção diferente da do vídeo (as da internet, em geral) entra inteira, sobre uma cópia
desfocada dela mesma. As opções da linha de comando sobrepõem o pedido: `--sem-legendas`, `--sem-destaque`,
`--maiusculas`, `--palavras-por-legenda N`, `--sem-narracao` (ou `--sem-audio`), `--sem-musica`,
`--musica <arquivo>`, `--sem-movimento`, `--sem-provisorio` (falha se faltar imagem real), `--corte-seco`
(sem fade no começo e no fim, para o short com final em loop emendar no replay).

A montagem grava também a **thumbnail**, `10-video/thumbnail.jpg`: a imagem gerada para ela com o `texto` de
`08-thumbnail.json` por cima (caixa alta, contorno, na `posicao_texto`), no tamanho do vídeo e abaixo de 2 MB,
o limite do YouTube. Sem a imagem (projeto sem `08-thumbnail.json`, modo provisório), vale a primeira cena com
o texto do ângulo. Ela é também o primeiro quadro do vídeo (1/30 s, no lugar do quadro preto do fade de
entrada), que TikTok, Reels e Shorts mostram como capa; `--sem-quadro-thumbnail` tira.

Se o pedido tem música e o arquivo não está em `biblioteca/musicas/`, a montagem para com uma mensagem:
peça o arquivo ao usuário ou monte com `--sem-musica`.

### 8. Conferir o vídeo

Extraia dois ou três quadros e olhe:

```bash
ffmpeg -y -loglevel error -ss 3 -i projetos/<slug>/10-video/<slug>.mp4 -frames:v 1 -vf scale=540:-1 <scratchpad>/quadro.jpg
```

Abra `10-video/thumbnail.jpg` e confira se o texto está inteiro e não cobre o rosto. Nos quadros, confira se a
legenda está legível, se o destaque está numa palavra do trecho certo, se não cobre o essencial da imagem e se
as cartelas estão inteiras. Para o áudio, meça o volume integrado (deve ficar
perto de -14 LUFS):

```bash
ffmpeg -hide_banner -i projetos/<slug>/10-video/<slug>.mp4 -af ebur128 -f null - 2>&1 | grep -E "^\s+I:"
```

### 9. Entregar

Responda ao usuário com o caminho do vídeo, a duração, a voz usada (ou que o vídeo não tem narração), a
música (ou nenhuma), quantas imagens são de IA, quantas da internet e quantas provisórias, quantas imagens e
blocos de narração foram gerados nesta rodada (custo), os alertas do alinhamento e os arquivos para a
publicação: `10-video/thumbnail.jpg` (a thumbnail para subir), `legendas.srt` (closed caption no YouTube) e
`09-imagens/creditos.md` (créditos das imagens da internet, para a descrição). Se a música for comercial, lembre que ela costuma gerar reivindicação de
Content ID: o dono da música pode ficar com a monetização ou bloquear o vídeo.

## Modelos de voz disponíveis

| Modelo | Quando usar |
|---|---|
| `gemini-3.8-flash-tts` | padrão |
| `gemini-3.8-flash-lite-tts` | mais barato |
| `gemini-2.5-pro-preview-tts` | alternativa para comparar expressividade |

Outros fornecedores (`--fornecedor` ou `narracao.fornecedor`), com a chave cadastrada em Configurações > Chaves de API (interface web):

| Fornecedor | Modelo padrão | Vozes | Estilo |
|---|---|---|---|
| `openai` | `gpt-4o-mini-tts` | 13 (`--listar-vozes --fornecedor openai`; `marin` e `cedar` são as melhores) | perfil e estilo vão como instruções |
| `elevenlabs` | `eleven_multilingual_v2` (também `eleven_v3`, `eleven_flash_v2_5`) | as da conta, pelo nome ou voice_id | sem instrução: o jeito vem da voz |

Uma voz do pedido que não existe no fornecedor (a do canal é do Gemini) vira uma dele do mesmo gênero, sempre a
mesma. Cada chamada fica em `custos.jsonl` (a da OpenAI pela duração do áudio, a da ElevenLabs pelos
caracteres). Outro fornecedor entra como uma classe nova em `narrar.py`, registrada em `FORNECEDORES`. O
alinhamento e a montagem não mudam.

## Modelos de imagem disponíveis

| Modelo | Nome | `visual.qualidade` | Quando usar |
|---|---|---|---|
| `gemini-3.1-flash-image` | Nano Banana 2 | `padrao` | padrão: bom equilíbrio entre qualidade, coerência e custo |
| `gemini-3.1-flash-lite-image` | Nano Banana 2 Lite | `rascunho` | rascunhos e testes baratos |
| `gemini-3-pro-image` | Nano Banana Pro | `maxima` | qualidade máxima e melhor fidelidade às referências; bem mais caro |
| `gemini-2.5-flash-image` | Nano Banana | (só com `--modelo`) | geração anterior |

## Arquivos

| Caminho | Conteúdo |
|---|---|
| `09-imagens/referencias/<id>.png` | fichas de personagens e locais |
| `09-imagens/<id>.png` (ou `.jpg`) | uma imagem por cena, gerada ou da internet |
| `09-imagens/provisorio/<id>.png` | quadros de rascunho |
| `09-imagens/manifesto.json` | o que foi gerado ou baixado: modelo e referências, ou fonte, autor, licença e candidatas |
| `09-imagens/creditos.md` | créditos das imagens da internet, para a descrição do vídeo |
| `09-imagens/folha-de-contato.jpg` | todas as cenas numa grade |
| `09-narracao/blocos/<bloco>.wav` | narração de cada bloco |
| `09-narracao/falas/<bloco>-Fnn.wav` | cada fala dos blocos com diálogo, na voz de quem fala |
| `09-narracao/narracao.wav` | narração completa |
| `09-narracao/blocos.json` | vozes, modelo, início e duração de cada bloco e de cada fala |
| `09-narracao/tempos.json` | tempo real de cada cena e de cada palavra, e a conferência da voz |
| `09-narracao/teste-vozes/<voz>.wav` | a primeira fala do narrador em cada voz testada (`<personagem>-<voz>.wav` para um personagem) |
| `10-video/<slug>.mp4` | vídeo final (com narração e música, se houver) |
| `10-video/legendas.ass`, `legendas.srt` | legendas |
| `09-imagens/thumbnail.png` (ou `.jpg`) | a imagem da thumbnail, sem texto |
| `10-video/thumbnail.jpg` | a thumbnail com o texto, que também é o primeiro quadro do vídeo |
