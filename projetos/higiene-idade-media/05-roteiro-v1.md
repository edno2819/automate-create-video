---
projeto: higiene-idade-media
versao: 1
formato: short
duracao_alvo: "00:30"
ppm: 150
titulo: "Paris medieval tinha mais de 32 casas de banho"
thumbnail: "Interior de uma casa de banho medieval cheio de vapor, com tinas de madeira enfileiradas; em primeiro plano, uma mão estende duas moedas ao dono, que está na porta. Texto: VAPOR: 2 MOEDAS"
tecnicas: [lacuna-de-curiosidade, mas-entao, callback, final-em-loop]
tipo_gancho: dado-chocante
---

## [B01] Mais de 32 casas de banho
> função: gancho · abre: L1, L2 · fecha: — · conector: — · cortável: não

[VISUAL: interior de uma casa de banho medieval cheio de vapor, com arcos de pedra, tinas de madeira enfileiradas e luz quente de vela atravessando o vapor; banhistas só ao fundo, cobertos pela água e pelo vapor] Mais de 32 casas de banho. Numa cidade só.

Paris, século 13. Dizem que ninguém tomava banho.

## [B02] Em casa, jarra e bacia
> função: contexto · abre: L3 · fecha: — · conector: mas · cortável: não

Em casa, banho de tina custava caro: água carregada e aquecida.

No dia a dia, jarra de água quente e bacia.

## [B03] Virada: era negócio
> função: virada · abre: — · fecha: L1, L3 · conector: mas · cortável: não

E as casas de banho? Negócio, com guilda e regras.

[VISUAL: a composição da capa: uma mão estende duas moedas ao dono, parado na porta da casa de banho, com vapor saindo atrás dele] Vapor: 2 denários. Banho completo: 4 denários.

Até pregoeiros gritavam pelas ruas, chamando clientes.

## [B04] E Paris...
> função: final · abre: L4 · fecha: L2, L4 · conector: mas · cortável: não

Londres teve 13 casas. Viena chegou a 29. E Paris...

## Loops

- L1: Mais de 32 casas de banho numa cidade só, e dizem que ninguém tomava banho. O que eram essas casas e como funcionavam?
- L2: Paris era exceção, ou outras cidades também tinham casas de banho? E 32 é muito perto delas?
- L3: Em casa, banho de tina custava caro. E nas casas de banho, como era?
- L4: E Paris...? A frase fica em suspenso e pede o número, que volta no replay com a primeira frase do B01.

## Notas

### Plano

Short de 30 s sem áudio: a legenda é a narração inteira, em pedaços de cerca de 4 palavras. Por isso as
frases têm até 5 palavras ou quebram limpo em 4 + 4, e os números estão em algarismos ("32", "século 13",
"2 denários"), que se leem num relance. Um parágrafo por imagem: são 8, cada uma termina em fim de frase e
tem de 7 a 10 palavras, ou seja, de 2,8 a 4 s a 150 palavras por minuto.

| Bloco | Imagens | Função | Palavras | Tempo |
|---|---|---|---|---|
| B01 | 1 e 2 | gancho: o número contra o mito | 17 | 0:00 a 0:07 |
| B02 | 3 e 4 | contexto: em casa, tina cara; no dia a dia, jarra e bacia | 19 | 0:07 a 0:14 |
| B03 | 5, 6 e 7 | virada: era negócio, com guilda, preço e pregoeiro | 24 | 0:14 a 0:24 |
| B04 | 8 | final: Londres 13, Viena 29, "E Paris..." em loop | 10 | 0:24 a 0:28 |

- **Loops.** L1 abre no B01, com o número contra "Dizem que ninguém tomava banho", é nomeado no começo do
  B03 ("E as casas de banho?") e fecha no próprio B03, com guilda, preço e pregoeiro. L2 também abre no
  B01, mas fica implícito: "Numa cidade só" e o mito sugerem a pergunta "e o resto da Europa?". É o loop
  mais fraco, e o B04 o fecha com Londres e Viena. L3 abre com o "Em casa" do B02, que pede o contraste,
  e fecha no B03. L4 abre na última frase e fecha no replay. Nenhum trecho fica sem pergunta aberta: L2
  atravessa a passagem do B03 para o B04.
- **Re-hooks.** Não há marcador `[RE-HOOK]`, que é regra do vídeo longo. Quem faz esse papel é a pergunta
  "E as casas de banho?", em 0:14, logo antes da virada, e a escada 13, 29, "E Paris..." no fim.
- **Virada.** B03, em 0:14, mais ou menos na metade: "Negócio". Até ali o espanto é que as casas de banho
  existiam. Na virada, elas viram comércio, com guilda, preço e até pregoeiro na rua.
- **Callbacks.** O número é plantado no gancho ("Mais de 32 casas de banho. Numa cidade só.") e não volta
  no meio. É colhido no B04, quando 13 e 29 dão escala, e no replay, quando o 32 aparece como resposta a
  "E Paris...". A capa ("VAPOR: 2 MOEDAS") é colhida na imagem 6, "Vapor: 2 denários".
- **Teste de loop.** "Londres teve 13 casas. Viena chegou a 29. E Paris... Mais de 32 casas de banho.
  Numa cidade só."
- **Blocos cortáveis.** Nenhum: o vídeo já é o short.

### Fatos

- Só F1, F15, F3 e F2, com as palavras do dossiê. O mito do B01 é o da seção 3 do dossiê e aparece como
  "dizem", sem "na verdade". Quem derruba o mito são os números.
- F3 diz que a guilda "fixava regras" e, numa frase separada, dá os preços. O texto põe os dois lado a lado
  ("Negócio, com guilda e regras. Vapor: 2 denários...") sem afirmar que foi a guilda que fixou o preço.
  Também não dá ano para a guilda: F3 diz só "Baixa Idade Média".
- F3 fala em pregoeiros que chamavam clientes mulheres. Ficou "chamando clientes", como no ângulo. O
  detalhe "mulheres" abriria uma pergunta que 30 s não respondem e puxaria a leitura de bordel, que o
  ângulo manda evitar (F27).
- B02 e B03 só ficam lado a lado, sem "por isso". O texto não diz que as casas de banho existiam porque o
  banho em casa era caro, nem que eram mais baratas. Para não convidar essa comparação, o B02 diz "banho
  de tina", e o termo "banho completo" só aparece no preço.
- F2 mistura séculos: Londres é do século 13 e Viena, do 14. O texto não diz que as cidades eram da mesma
  época.
- Denários sem conversão para reais e sem equivalência.

### Para a direção visual

| Imagem | Texto | Sugestão |
|---|---|---|
| 1 | Mais de 32 casas de banho. Numa cidade só. | o `[VISUAL]` do B01 |
| 2 | Paris, século 13. Dizem que ninguém tomava banho. | rua de Paris medieval, vapor saindo de uma porta |
| 3 | Em casa, banho de tina custava caro: água carregada e aquecida. | casa simples: água esquentando ao lado de uma tina vazia |
| 4 | No dia a dia, jarra de água quente e bacia. | detalhe: mãos, jarra soltando vapor, bacia |
| 5 | E as casas de banho? Negócio, com guilda e regras. | os donos reunidos, ou a fachada da casa de banho |
| 6 | Vapor: 2 denários. Banho completo: 4 denários. | a composição da capa, conforme o `[VISUAL]` do B03 |
| 7 | Até pregoeiros gritavam pelas ruas, chamando clientes. | pregoeiro gritando numa rua de Paris |
| 8 | Londres teve 13 casas. Viena chegou a 29. E Paris... | as três cidades e, por último, Paris com vapor saindo de uma porta |

- Imagem 1: com 4 palavras por legenda, "Mais de 32 casas de banho." sai em dois pedaços ("Mais de 32 casas"
  e "de banho."). Funciona como uma revelação rápida, mas, se a montagem permitir, vale mostrar a frase
  inteira ou pôr o "32" como texto na tela, nunca dentro da imagem gerada.
- Imagem 8: termine em Paris, de preferência numa porta com vapor, para que o replay, que começa no
  interior da imagem 1, pareça continuação.
- Nas cenas de banho coletivo, os corpos ficam cobertos pela água e pelo vapor, por causa dos anunciantes
  e dos filtros dos geradores, como pede o ângulo.
