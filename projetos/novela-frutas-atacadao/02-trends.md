# Trends: novela das frutas (coleta de 2026-10-21)

Fonte de todas as coletas: yt-dlp (não há `YOUTUBE_API_KEY`; não houve `em-alta`). Dados brutos em `02-trends/*.json` (logs `.log` ao lado). Os 112 vídeos únicos das 6 primeiras coletas e os das 4 de shorts foram cruzados por id.

## 1. Coletas realizadas

| Termo | Fonte | Janela | Vídeos | Arquivo |
|---|---|---|---|---|
| "novela das frutas" | yt-dlp | 30 d | 25 | 02-trends/tema30.json |
| "moranguete" | yt-dlp | 30 d | 25 | 02-trends/moranguete.json |
| "abacatudo" | yt-dlp | 30 d | 16 | 02-trends/abacatudo.json |
| "frutas novela parte 2" | yt-dlp | 30 d | 25 | 02-trends/parte2.json |
| "fruit drama" | yt-dlp | 30 d | 23 | 02-trends/fruitdrama.json |
| "novela das frutas" | yt-dlp | 365 d | 25 | 02-trends/tema365.json |
| "novela das frutas", só short | yt-dlp | 30 d | 10 | 02-trends/short30.json |
| "frutinovela", só short | yt-dlp | 30 d | 20 | 02-trends/short_frutinovela.json |
| "novela das frutas", só short | yt-dlp | 365 d | 15 | 02-trends/short365.json |
| "novela das frutas parte 2", só short | yt-dlp | 90 d | 5 | 02-trends/short_parte2.json |

Nenhuma coleta falhou (alguns ids indisponíveis foram ignorados, ver `avisos` nos JSON). O brief não tinha canais de referência nem concorrentes; as coletas de canal foram trocadas por buscas só de short, porque os canais dominantes foram descobertos pelos dados. Observação: o filtro "short" do script vai até 3 min, então quase tudo que ele devolve tem 1:40 a 2:40, e não 30 s.

## 2. Outliers (razão views/inscritos, canais pequenos primeiro)

| # | Título | Canal (inscritos) | Views | Razão | Idade | Por que importa |
|---|---|---|---|---|---|---|
| 1 | Cocardo y Mangoberto se encuentran | Deon (2,9 mil) | 164.036 | 56x | 3 d | Short de 0:27 (a duração do nosso alvo) de canal pequeno. Menos de 3 dias: view/dia ainda instável. (short_frutinovela) |
| 2 | Soy cocarado y estas son mis nieves.. | DiegoCapoYT (947) | 77.182 | 81x | 18 d | Short de 0:19, meme curto, 18 d: sinal sólido de que 20-30 s funciona em canal pequeno. (short_frutinovela) |
| 3 | COCARDO TIENE UN SECRETO 😨🥥 Parte 4 | Fruti mundo (3,9 mil) | 232.277 | 60x | 24 d | Gancho "tem um segredo" + numeração de parte no título, 2:32. (short30) |
| 4 | LA PROBRESA LA SORPRENDIÓ DE REPENTE | Frutinovelass (1,8 mil) | 544.056 | 296x | 56 d | Short de 2:03 de canal minúsculo, 56 d. Título é cena de susto. (short365) |
| 5 | BOQUENILCE E A PRIMA FORAM SEQUESTRADAS! 😱 | boquenilce (877) | 82.889 | 95x | 53 d | Único short em português no top; sequestro + emojis. (short_parte2) |
| 6 | ¡FRESA DESCUBRE LA TRAICIÓN Y EL ENGAÑO DE SU MARIDO! | Riqueza Frutal (11,5 mil) | 2.085.885 | 181x | 191 d | Perene: morango traído pelo marido, tema quase idêntico ao nosso. Duração 2:20. (tema365, short365) |
| 7 | MANDY CONOCIÓ AL MANGO DE SUS SUEÑOS | Fruti Novelas (40 mil) | 948.258 | 24x | 21 d | Comédia romântica, 1:40; mesmo canal tem 5 shorts no top com 4 a 24x. (short30) |
| 8 | Historia Do TOMATE (PARTE 1) - NOVELA DE FRUTAS | Universo Alternativo (40,9 mil) | 1.501.711 | 37x | 198 d | Melhor exemplo em português com "(PARTE 1)" no título; perene. (tema365) |
| 9 | 🍌 TRIÂNGULO DE FRUTAS 🔥 OLHA ESSA HISTÓRIA! | Histórias das Frutas (35,5 mil) | 1.781.140 | 50x | 89 d | Triângulo amoroso, formato do nosso episódio. (tema365) |
| 10 | Moranguete, Abacatudo, 67, 42 / Passinho do Sabor | vários (5-8 mil) | 4,3 mi e 4,2 mi | 861x e 545x | 158-170 d | Música/meme que fez os nomes "Moranguete" e "Abacatudo" buscáveis. Não é novela; é busca por nome, não formato. (short365) |

Marcados como instáveis (menos de 3 dias): o 1. Deon (3 d). Vídeos com 0-9 views em canais de 1-17 inscritos têm razão calculada mas não valem como sinal (ex.: "Mundo das Frutas" 9v, razão 9x).

## 3. Padrões de título

- **Personagem + gancho de segredo/traição/susto, com emoji de choque** (😱😨🔥💔): é o padrão dominante entre os outliers (itens 3, 4, 5, 6, 9). Exemplos: "COCARDO TIENE UN SECRETO", "FRESA DESCUBRE LA TRAICIÓN", "O hermano de Cocardo regresó por venganza" (frutapolis, 548 mil views, 84x, 20 d). Na janela de 30 d, entre os cerca de 20 shorts de "frutinovela", 9 usam essa estrutura e 6 deles têm razão ≥ 10.
- **Numeração de capítulo/parte** ("Parte 1/2/4", "Capítulo 2/4", "Episodio 1", "T3 EP 1 E 2", "(PARTE 1)"): aparece em pelo menos 12 vídeos do conjunto, vários outliers (Fruti mundo 60x, Universo Alternativo 37x, La Nieve De Lima Partes 1-3 de canal de 130 inscritos com 13x a 77x). Confirma a lógica de "parte 2" do brief.
- **Hashtags no título** (#frutinovelas #fyp #ia): comum em canais pequenos (Deon, DiegoCapoYT, El Mundo Frutal), com razões de 13x a 81x. Sinal fraco de causa; é apenas o que se repete.
- **Termo de busca**: em espanhol, "frutinovela(s)" domina; em português, "novela das frutas". O nome de personagem no título (Cocardo, Mandy, Boquenilce, Moranguete) é quase universal; "Moranguete/Abacatudo" em português aparecem sobretudo em música e reação, não em novela curta.
- **"Historia completa"/compilado/maratona de 1 hora**: muito frequente em canais maiores (Crazy Fruits Brazil, Ravex, Señales Mixta), mas o formato é longo e não se aplica ao short.

## 4. Formatos e durações

- A trend em 2026-10 é dominada por **personagens e universos de outros canais** (Cocardo/heladeria, Mandy, Boquenilce, La Nieve de Lima), em espanhol. Os canais grandes de reação (Alex Crashers 9,2 mi, Juan Pablo Hollman 4 mi, Soy Relevante 3,9 mi) têm razão baixa (0,3-0,6x) e dizem mais sobre o canal. Em português, a nuvem do ano passado (Felipe Neto, Brino, Roberta Pupi, T3ddy, Emilly Vick) é antiga (170-230 d) e é reação/crítica, não novela nova.
- Entre os outliers em short (até 3 min): faixa de **1:40 a 2:40** é a mais comum (Fruti Novelas, Fruti mundo, frutapolis, FrutiTV), mas há outliers de **0:12 (62 mil), 0:19 (77 mil) e 0:27 (164 mil)**, todos de canais de menos de 3 mil inscritos (short_frutinovela). O alvo de 30 s é viável, mas o volume de provas em ~30 s é só de 3 vídeos; os números de 1:40-2:40 não se transferem direto.
- Longos (8 a 60 min) têm muitos outliers de razão alta em canais minúsculos (ex.: 3333x, 456x), mas são compilados de partes e não servem de modelo para um short.
- Séries por canal: Fruti Novelas (40 mil) publica vários shorts de 1:40-2:38 por episódio da Mandy; FrutiTV e frutapolis publicam capítulos numerados em sequência. A estratégia vencedora de canal pequeno é serializar.
- Quase nenhum vídeo coletado em português tem um short curto de novela com views relevantes; em português os números altos são de música (Passinho do Sabor) e reação.

## 5. Saturação e lacunas

Saturado (muitos vídeos recentes parecidos):
- Cocardo/heladería/Lima: dezenas de vídeos em 3 semanas, em espanhol, incluindo reações de canais de milhões.
- Traição conjugal clássica (morango traído, "Moranguete traiu com o Bananildo"): vários vídeos, mas os que existem em português têm 7-9 views (Mundo das Frutas, Novelinha frutas), ou seja, o tema é conhecido mas ninguém em português o executou bem em short.
- Compilados "história completa" e lives de 1 h (Crazy Fruits Brazil, Ravex).

Lacunas (nenhum vídeo coletado cobre):
- **Cenário de supermercado/trabalho** (caixa, repositor, açougue). Nas buscas só aparecem a sorveteria do Cocardo, o sítio e a casa; nenhum episódio em supermercado. Atacadão é marca real: ver Limitações.
- **Triângulo com personagens de profissão** em português curto: "TRIÂNGULO DE FRUTAS" (50x, 89 d) é de longo; não há short de 30 s com o mesmo tema.
- **Nomes Moranguete/Abacatudo em novela curta dublada em português**: a busca por esses nomes retorna quase só música; o público que os procura (Passinho do Sabor, 4 mi views em shorts de 1-2 min) não encontra novela.
- **Gancho "parte 2" em 30 s**: os shorts coletados raramente terminam anunciando a parte seguinte no próprio vídeo; a numeração fica só no título (isto é inferência a partir dos títulos, não da retenção).

## 6. Ângulos sugeridos para o estrategista

1. **Título de segredo/traição com personagem nomeado + emoji de choque**, estilo "COCARDO TIENE UN SECRETO 😨" (Fruti mundo, 60x) e "FRESA DESCUBRE LA TRAICIÓN..." (Riqueza Frutal, 181x). Ex.: o segredo da caixa Moranguete na câmara fria.
2. **Short de 20-30 s sem enrolação**, com o confronto/encontro como cena única, como Deon (0:27, 164 mil views, 56x) e DiegoCapoYT (0:19, 77 mil, 81x). Sugere cortar a apresentação e abrir direto no clímax.
3. **Série numerada ("Parte 1", "Capítulo 2")**: Fruti mundo Parte 4, La Nieve de Lima Partes 1-3, frutapolis Capítulos 2 e 4, Universo Alternativo "(PARTE 1)". Colocar "Parte 1" no título e terminar no clímax.
4. **Triângulo amoroso em local de trabalho (supermercado)**, ocupando a lacuna de cenário: "TRIÂNGULO DE FRUTAS" (Histórias das Frutas, 1,78 mi, 50x) prova a demanda pelo triângulo; ninguém levou para o mercado.
5. **Aproveitar a busca por "Moranguete" e "Abacatudo"**: o par tem demanda alta (Passinho do Sabor, 4,3 mi e 4,2 mi), mas a oferta de novela é pequena. Usar os dois nomes no título e nas hashtags.
6. **Gancho de sequestro/perigo no fim** ("BOQUENILCE E A PRIMA FORAM SEQUESTRADAS!", 95x, em português): alternativa ao desfecho de traição, para o episódio terminar em risco físico (porta da câmara fria fechando).

## 7. Limitações

- CTR, retenção e fonte de tráfego de terceiros não são públicos; os dados mostram atração (views), não retenção.
- A busca do YouTube é personalizada e varia; yt-dlp não filtra região nem idioma com precisão (aparecem vídeos em espanhol, vietnamita, birmanês, inglês e Blox Fruits, jogo sem relação).
- Views/dia dos vídeos com menos de 3 dias não são confiáveis. Vídeos com 0-30 views em canais de 1-20 inscritos geram razões altas sem significado.
- Os shorts do TikTok e do Reels, onde a trend nasceu, não estão nos dados; só o YouTube foi coletado. O filtro "short" cobre até 3 min, então a prova de 30 s é de 3 vídeos.
- Não há dados de canais de referência do brief (nenhum informado) nem do `em-alta` (sem chave de API).
- "Atacadão" é marca registrada de rede varejista: não foi pesquisada no que diz respeito a risco, e nada nos dados trata disso. Fica como ponto para o estrategista decidir (nome genérico ou não).
