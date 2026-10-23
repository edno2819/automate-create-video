---
projeto: como-era-cada-cargo-navio-pirata-fork
versao: 2
formato: short
duracao_alvo: "01:00"
ppm: 150
titulo: "Como era cada cargo de um navio pirata"
thumbnail: "Tela dividida: à esquerda um capitão de cinema, chapéu e casaco impecáveis num galeão iluminado (sem rosto de ator real); à direita um marujo magro e sujo num barco pequeno, coçando a cabeça à luz de lanterna. Texto: 'Filme x real'"
tecnicas: [tese-contraria, antes-e-depois, mas-entao, quebra-de-padrao, final-em-loop]
tipo_gancho: tese-contraria
---

## [B01] Gancho: esquece o cinema
> função: gancho · abre: L1, L2 · fecha: — · conector: — · cortável: não

[VISUAL: primeiro frame num galeão enorme de cinema, velas brancas e luz dourada, que se desfaz num barco pequeno e apertado, de madeira escura, à luz de lanterna]

Venha descobrir se você aguentaria ser um pirata. Esquece o cinema: o navio era pequeno, e o capitão podia ser demitido no voto.

## [B02] Capitão
> função: contexto · abre: L3 · fecha: L2 · conector: então · cortável: não

O capitão Charles Vane se recusou a atacar um navio, e a tripulação trocou ele por Calico Jack.

## [B03] Quartel-mestre
> função: desenvolvimento · abre: — · fecha: L3 · conector: mas · cortável: não

Fora da batalha, o capitão mandava igual a um cargo que o cinema esquece: o quartel-mestre, que dividia o tesouro e o rum.

## [B04] Marujo
> função: desenvolvimento · abre: L4 · fecha: — · conector: mas · cortável: não

E você é só marujo. Sua cabeça coça de piolho, você bebe rum porque a água apodrece, e bate o biscoito na mesa pros carunchos caírem.

[RE-HOOK] Pelo menos pirata não tem regra, né?

## [B05] O código
> função: virada · abre: L5 · fecha: L4 · conector: mas · cortável: não

No filme, o código é só uma sugestão. Na real, você jurava regras: luz apagada às oito da noite, e oitocentas moedas de prata pra quem perdesse um braço.

[RE-HOOK] E quem operava esse braço?

## [B06] Cirurgião e carpinteiro
> função: clímax · abre: L6 · fecha: L5 · conector: então · cortável: não

Muitas vezes, um cirurgião sequestrado de outro navio. Sem ele, operava o carpinteiro, com a serra de cortar madeira.

[RE-HOOK] E andar na prancha?

## [B07] A prancha
> função: payoff · abre: — · fecha: L6 · conector: mas · cortável: não

Quase não tem registro. [PAUSA] O castigo real era pior: te largavam sozinho numa ilha, com água, pólvora e uma arma.

## [B08] Fecho
> função: final · abre: — · fecha: L1 · conector: então · cortável: não

No filme, pirata não tem regra. Na real, quem te largou na ilha foi a regra que você jurou. E aí, você teria coragem de ser um tripulante de um navio pirata?

## Loops

- L1: você aguentaria ser um pirata?
- L2: como um capitão pirata era demitido no voto?
- L3: se o capitão podia ser trocado, quem mandava junto com ele?
- L4: pirata tinha regra ou não?
- L5: quem operava quem perdia um braço?
- L6: andar na prancha existiu?

## Notas

### Mudanças da versão 2

Ponto a ponto da avaliação da v1:
- Confusão B05-B06 e prioridade 1 (diga a ação): a perda do braço é dita antes da cirurgia ("pra quem
  perdesse um braço" → "E quem operava esse braço?").
- Prioridade 2 (forca sem fonte): a frase saiu; o fecho agora é a regra que te larga na ilha, sustentada
  pelos artigos.
- Prioridade 3 (cozinheiro → músico em "e depois"): os dois blocos saíram com a troca de ângulo; toda
  passagem tem ligação dita (porque, fora da batalha, e você, pelo menos, e quem operava, e a prancha).
- Prioridades 4 e 5 (concretude): o artilheiro saiu; ficaram "moedas de prata" e o quartel-mestre que divide
  "o tesouro e o rum".
- `manter`: o rum porque a água apodrece, o biscoito batido na mesa e a pausa antes do castigo continuam. O
  primeiro frame do biscoito, a bolsa de couro de Henry Morgan e a escada até a forca não: pertenciam ao A1
  (a bolsa de couro era do cozinheiro, que não está no A3, e não coube no orçamento), e o primeiro frame do A3 é o
  galeão que vira barco pequeno.

### Por que a estrutura mudou tanto em relação à v1

A v1 foi escrita para o ângulo A1 (escada de cargos em segunda pessoa, thumb "Seu jantar hoje"). O
`04-angulo-escolhido.json` agora é o A3 ("Filme x real", gancho de tese contrária, escolhido em 2026-10-22 às
14:07, depois da v1). O ângulo é o contrato, então a v2 é um roteiro novo sobre a estrutura do A3. Da
avaliação da v1 aproveitei o que vale para qualquer ângulo: os fatos que o crítico validou, as correções de
clareza e de factualidade e os trechos em `manter` que cabem no A3 (o rum porque a água apodrece, o biscoito
batido na mesa, a pausa antes do castigo).

### Cadeia lógica

- Esquece o cinema: o navio era pequeno e o capitão podia ser demitido no voto (tese contrária)
- → porque: o capitão Charles Vane se recusou a atacar e a tripulação trocou ele por Calico Jack
- → mas, fora da batalha, o capitão mandava igual ao cargo que o cinema esquece: o quartel-mestre, que dividia o tesouro e o rum
- → mas você não é nenhum dos dois: é marujo (piolho, rum porque a água apodrece, biscoito com caruncho)
- → então, pelo menos pirata não tem regra? Mas tem: regras juradas, luz apagada às oito, oitocentas moedas por um braço (virada)
- → então quem operava esse braço? Um cirurgião sequestrado, ou o carpinteiro com a serra de madeira
- → mas a cena mais famosa do cinema, a prancha, quase não tem registro; o castigo real era pior: a ilha
- → então: quem te largou na ilha foi a regra que você jurou → você teria coragem?

### Fecho

"No filme, pirata não tem regra. Na real, quem te largou na ilha foi a regra que você jurou." Inverte o mito
que o re-hook do B04 deixou no ar ("pelo menos pirata não tem regra, né?") e fecha a tese contrária do
gancho: o pirata do cinema é livre; o real assinou um contrato, e o contrato é o que mais assusta. A
pergunta fixa do usuário vem logo depois. No replay, "...de um navio pirata?" emenda em "Venha descobrir se
você aguentaria ser um pirata."

### Blocos, re-hooks e callbacks

- B01 gancho: abertura fixa + tese contrária (navio pequeno, capitão demissível). Abre L1 e L2.
- B02 prova 1 da tese (Vane trocado por Calico Jack) e abre "quem mandava junto com o capitão?".
- B03 o cargo que o cinema esquece. B04 o "você" entra no seu papel, que fica fixo até o fim: marujo.
- Re-hook do B04 planta o mito "pirata não tem regra"; o B05 desmonta (virada, por volta de 50%) e o B08
  colhe ("a regra que você jurou").
- Re-hooks em pergunta curta no fim de B05 e B06, cada um respondido na primeira frase do bloco seguinte.
- B07 quebra de padrão: a cena mais famosa do cinema é a única coisa que não existiu; [PAUSA] antes do castigo.
- Blocos cortáveis: nenhum, o short é uma cadeia só.

### Ajustes de estrutura em relação ao ângulo

- O músico saiu: repetia o "sequestrado" do cirurgião e não se liga à cadeia das regras. É o primeiro a
  voltar se o usuário aceitar mais tempo: "Até o músico era sequestrado, pra tocar violino no ataque e
  assustar o outro navio." (15 palavras, entre B06 e B07).
- O lado "filme" do marujo saiu: no próprio cinema os marujos já aparecem sujos, e "lenço limpo" seria
  espantalho (sinal de falha da tese contrária). O contraste do marujo é com o capitão e o quartel-mestre
  que o cinema mostra.
- Não cito cenas específicas de Piratas do Caribe, só "o cinema" e "o filme"; a única referência é o código
  como "sugestão", que o dossiê registra (Barbossa: "mais diretrizes do que regras").
- Correções da avaliação da v1 que valem aqui: a ação do ferimento é dita antes da cirurgia ("pra quem
  perdesse um braço" → "quem operava esse braço?"), as moedas são "de prata", o quartel-mestre divide "o
  tesouro e o rum" (as rações de bebida), e saiu a frase sem fonte sobre o capitão subir primeiro na forca.

### Tamanho

O orçamento é de 150 palavras (135 a 165). O ângulo previa cerca de 180, e o brief pede narração 20% mais
rápida (cerca de 180 palavras por minuto). A medição deu **206 palavras, 01:23 a 150 ppm**, que dão cerca
de 69 s na velocidade pedida: 41 palavras acima da faixa. Ficou fora porque o A3 pede duas frases por item
(o filme, depois o real) e porque não cortei as frases que ligam um bloco ao outro ("Fora da batalha...",
"E quem operava esse braço?", "Pelo menos pirata não tem regra, né?"), seguindo a lição "Clareza vale mais
que a meta de duração". Já saíram o músico e o lado "filme" do marujo. Se o usuário quiser chegar a 1 min
na velocidade pedida (cerca de 180 palavras), corte nesta ordem:
1. "luz apagada às oito da noite," (6 palavras; fica "você jurava regras: oitocentas moedas...");
2. ", e o navio era pequeno" do B01, mudando para "Esquece o cinema: o capitão podia ser demitido no voto."
   (5 palavras; a imagem do galeão que vira barco segura a ideia);
3. o B02 inteiro, a prova de Vane (18 palavras), trocando o começo do B03 por "Na real, fora da batalha, o
   capitão mandava igual...". A tese perde a prova concreta, por isso é o último.
Para chegar a 165 seria preciso tirar um cargo inteiro (o quartel-mestre ou o cirurgião), e aí o título
deixa de ser cumprido; não recomendo.

### Fatos

- Navio pequeno: National Geographic (sloops e escunas). Capitão eleito e deposto no voto; autoridade total
  só em batalha: Wikipédia, governança (alta/média).
- Vane substituído por Rackham, o Calico Jack, após se recusar a atacar: World History.
- Quartel-mestre dividia o butim e definia rações de comida e bebida: Wikipédia, governança.
- Piolho, água que apodrece, rum, biscoito batido na mesa: dossiê (navios da época em geral; o texto não diz
  que era exclusivo de pirata).
- Luz apagada às oito e oitocentas peças de oito por um membro: artigos de Roberts (e Phillips). "Moedas de
  prata" = peças de oito.
- Cirurgião muitas vezes forçado a embarcar ao ser capturado; sem cirurgião, o carpinteiro operava: dossiê.
- Prancha: pouca ou nenhuma evidência (NatGeo, Mental Floss). Abandono em ilha com pólvora, água e arma:
  artigos reais. A regra "foi o que te largou na ilha" é a leitura de que o abandono era castigo previsto
  nos artigos jurados, o que o dossiê sustenta.
- "Calico Jack" e "Charles Vane" podem sair com pronúncia errada na voz do Gemini: conferir na narração.
