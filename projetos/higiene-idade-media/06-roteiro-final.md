---
projeto: higiene-idade-media
versao: 2
formato: short
duracao_alvo: "00:30"
ppm: 150
titulo: "Paris medieval tinha mais de 32 casas de banho"
thumbnail: "Interior de uma casa de banho medieval cheio de vapor, com tinas de madeira enfileiradas; em primeiro plano, uma mão estende duas moedas ao dono, que está na porta. Texto: VAPOR: 2 MOEDAS"
tecnicas: [lacuna-de-curiosidade, mas-entao, callback, final-em-loop]
tipo_gancho: dado-chocante
---

## [B01] Mais de 32 banhos públicos
> função: gancho · abre: L1, L2 · fecha: — · conector: — · cortável: não

[VISUAL: interior de uma casa de banho medieval cheio de vapor, com arcos de pedra, tinas de madeira enfileiradas e luz quente de vela atravessando o vapor; banhistas só ao fundo, cobertos pela água e pelo vapor] Mais de 32 banhos públicos. Numa cidade só.

Dizem que ninguém tomava banho. Paris, século 13.

## [B02] Em casa, só bacia
> função: contexto · abre: L3 · fecha: — · conector: mas · cortável: não

Mas em casa, encher uma tina custava caro.

Água carregada e aquecida. No dia a dia, só bacia.

## [B03] Virada: na rua, era negócio
> função: virada · abre: — · fecha: L1, L3 · conector: mas · cortável: não

Na rua, banho era negócio. Com guilda e regras.

[VISUAL: a composição da capa: uma mão estende duas moedas ao dono, parado na porta da casa de banho, com vapor saindo atrás dele] Vapor: 2 denários. Banho completo: 4 denários.

Até pregoeiros gritavam pelas ruas, chamando clientes.

## [B04] E Paris...
> função: final · abre: L4 · fecha: L2, L4 · conector: e então · cortável: não

Londres tinha 13. Viena chegou a 29. E Paris...

## Loops

- L1: Mais de 32 banhos públicos numa cidade só, e dizem que ninguém tomava banho. O que eram esses banhos e como funcionavam?
- L2: Paris era exceção, ou outras cidades também tinham banhos públicos? E 32 é muito perto delas?
- L3: Em casa, encher uma tina custava caro e o dia a dia era só bacia. Então onde se tomava banho de verdade?
- L4: E Paris...? A frase fica em suspenso e pede o número, que volta no replay com a primeira frase do B01.

## Notas


### Plano

Short de 30 s sem áudio: a legenda é a narração inteira, em pedaços de cerca de 4 palavras. Por isso as
frases têm até 5 palavras ou se partem em pedaços que se leem sozinhos, e os números estão em algarismos ("32", "século 13",
"2 denários"), que se leem num relance. Um parágrafo por imagem: são 8, cada uma termina em fim de frase e
tem de 7 a 10 palavras, ou seja, de 2,8 a 4 s a 150 palavras por minuto. Os pedaços de legenda abaixo foram
conferidos com `pedacos(texto, 4)` do `montar_video.py`.

| Bloco | Imagens | Função | Palavras | Tempo |
|---|---|---|---|---|
| B01 | 1 e 2 | gancho: o número contra o mito | 16 | 0:00 a 0:06 |
| B02 | 3 e 4 | contexto: em casa, tina cara; no dia a dia, só bacia | 18 | 0:06 a 0:14 |
| B03 | 5, 6 e 7 | virada: na rua, era negócio, com guilda, preço e pregoeiro | 23 | 0:14 a 0:23 |
| B04 | 8 | final: Londres 13, Viena 29, "E Paris..." em loop | 9 | 0:23 a 0:26 |

- **Loops.** L1 abre no B01, com o número contra "Dizem que ninguém tomava banho", é retomado no começo do
  B03 ("Na rua, banho era negócio.") e fecha no próprio B03, com guilda, preço e pregoeiro. L2 também abre no
  B01, mas fica implícito: "Numa cidade só" e o mito sugerem a pergunta "e o resto da Europa?". É o loop
  mais fraco, e o B04 o fecha com Londres e Viena. L3 abre com o "Mas em casa" do B02 e fica escrito no "só bacia", que deixa a falta;
  fecha no B03, com "Na rua". L4 abre na última frase e fecha no replay. Nenhum trecho fica sem pergunta aberta: L2
  atravessa a passagem do B03 para o B04.
- **Re-hooks.** Não há marcador `[RE-HOOK]`, que é regra do vídeo longo. Quem faz esse papel é a falta de "só bacia.", em
  0:13, logo antes da virada, e a escada 13, 29, "E Paris..." no fim.
- **Virada.** B03, em 0:14, mais ou menos na metade: "Na rua, banho era negócio.", em contraste com o "Mas em casa". Até ali o espanto é que as casas de banho
  existiam. Na virada, elas viram comércio, com guilda, preço e até pregoeiro na rua.
- **Callbacks.** O número é plantado no gancho ("Mais de 32 banhos públicos. Numa cidade só.") e não volta
  no meio. É colhido no B04, quando 13 e 29 dão escala, e no replay, quando o 32 aparece como resposta a
  "E Paris...". A capa ("VAPOR: 2 MOEDAS") é colhida na imagem 6, "Vapor: 2 denários".
- **Teste de loop.** "Londres tinha 13. Viena chegou a 29. E Paris... Mais de 32 banhos públicos. Numa
  cidade só."
- **Blocos cortáveis.** Nenhum: o vídeo já é o short.

### Fatos

- Só F1, F15, F3 e F2, com as palavras do dossiê. O mito do B01 é o da seção 3 do dossiê e aparece como
  "dizem", sem "na verdade". Quem derruba o mito são os números.
- F3 diz que a guilda "fixava regras" e, numa frase separada, dá os preços. O texto põe os dois lado a lado
  ("Com guilda e regras. Vapor: 2 denários...") sem afirmar que foi a guilda que fixou o preço.
  Também não dá ano para a guilda: F3 diz só "Baixa Idade Média".
- F3 fala em pregoeiros que chamavam clientes mulheres. Ficou "chamando clientes", como no ângulo. O
  detalhe "mulheres" abriria uma pergunta que 30 s não respondem e puxaria a leitura de bordel, que o
  ângulo manda evitar (F27).
- B02 e B03 ficam em contraste, "Mas em casa" contra "Na rua", sem "por isso". O texto não diz que os
  banhos públicos existiam porque o banho em casa era caro, nem que eram mais baratos. "Só bacia" resume o
  "jarra e bacia" de F15 sem negar a jarra, e o termo "banho completo" só aparece no preço.
- F2 mistura séculos: Londres é do século 13 e Viena, do 14. O texto não diz que as cidades eram da mesma
  época.
- Denários sem conversão para reais e sem equivalência.

### Para a direção visual

| Imagem | Texto | Sugestão |
|---|---|---|
| 1 | Mais de 32 banhos públicos. Numa cidade só. | o `[VISUAL]` do B01 |
| 2 | Dizem que ninguém tomava banho. Paris, século 13. | rua de Paris medieval, vapor saindo de uma porta |
| 3 | Mas em casa, encher uma tina custava caro. | casa simples: água esquentando ao lado de uma tina vazia |
| 4 | Água carregada e aquecida. No dia a dia, só bacia. | detalhe: mãos e uma bacia; vapor da rua entrando pela janela |
| 5 | Na rua, banho era negócio. Com guilda e regras. | os donos reunidos, ou a fachada da casa de banho |
| 6 | Vapor: 2 denários. Banho completo: 4 denários. | a composição da capa, conforme o `[VISUAL]` do B03 |
| 7 | Até pregoeiros gritavam pelas ruas, chamando clientes. | pregoeiro gritando numa rua de Paris |
| 8 | Londres tinha 13. Viena chegou a 29. E Paris... | as três cidades e, por último, Paris com vapor saindo de uma porta |

- Imagem 1: "Mais de 32 banhos públicos." sai num pedaço só, porque a quinta palavra fecha a frase. Se a
  montagem permitir, vale destacar o "32" como texto na tela, nunca dentro da imagem gerada.
- Pedaços de legenda conferidos: imagem 2 "Dizem que ninguém tomava banho." | "Paris, século 13."; imagem 5
  "Na rua," | "banho era negócio." | "Com guilda e regras."; imagem 6 "Vapor: 2 denários." | "Banho completo:"
  | "4 denários.".
- Imagem 8: termine em Paris, de preferência numa porta com vapor, para que o replay, que começa no
  interior da imagem 1, pareça continuação.
- Nas cenas de banho coletivo, os corpos ficam cobertos pela água e pelo vapor, por causa dos anunciantes
  e dos filtros dos geradores, como pede o ângulo.

### Mudanças da versão 2

1. **gancho-2s (prioridade 1).** Imagem 1 virou "Mais de 32 banhos públicos. Numa cidade só."; o primeiro
   pedaço de legenda tem sentido completo. Na imagem 2, o mito vem antes de "Paris, século 13.", e a tensão
   cai em ~3 s. Divergência pequena: usei "Dizem que..." sem o "E" sugerido, porque com ele a frase passa
   de 5 palavras e o `pedacos` gera um pedaço "E dizem" sozinho na tela, morno. Sem o "E", a frase inteira
   sai num pedaço.
2. **sem-ponto-de-abandono (prioridade 2).** B02 abre com "Mas em casa, encher uma tina custava caro." e
   fecha em "No dia a dia, só bacia.", que deixa a falta aberta para o B03 sem afirmar causa. O L3 foi
   reescrito para refletir isso. Na direção visual, a imagem 4 sugere vapor da rua entrando pela janela.
3. **mas-entao (prioridade 3).** Imagem 5 virou "Na rua, banho era negócio. Com guilda e regras.": a virada
   é um contraste escrito com o "Mas em casa". O `pedacos` separa "Na rua," num pedaço próprio, por causa
   da vírgula; mantive, porque funciona como batida de contraste.
4. **linguagem-falada (prioridade 4).** Imagem 8 virou "Londres tinha 13. Viena chegou a 29. E Paris...",
   sem o "casas" ambíguo. Imagem 6 ficou como estava, como a avaliação aceitou.
5. **Metadados e notas (prioridade 5).** O conector do B04 passou a "e então". As notas não dizem mais que as
   frases "quebram limpo em 4 + 4"; a tabela do plano usa as contagens da medição.
6. **Manter.** O [VISUAL] do B01, "Numa cidade só.", as imagens 6 e 7, a escada final e os cuidados factuais
   ficaram intactos. Imagens seguem com 7 a 10 palavras.
7. **Duração.** Caiu de 72 para 66 palavras, 26 s estimados para a meta de 30 s. Não enchi: sem áudio, cada
   imagem fica 3,75 s na tela e as legendas têm folga para serem lidas.
