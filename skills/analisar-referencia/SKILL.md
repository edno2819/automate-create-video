---
name: analisar-referencia
description: Estuda vídeos de referência do YouTube e alimenta a biblioteca de narrativa — baixa a transcrição, gera o esqueleto estrutural (gancho, blocos, loops, viradas, payoff) em biblioteca/esqueletos/ e propõe técnicas novas para biblioteca/tecnicas.json. Use quando o usuário colar links de vídeos que performaram bem e quiser entender ou reaproveitar a estrutura ("analisa esse vídeo", "estuda esse canal", "quero o esqueleto desse vídeo", "adiciona essa referência à biblioteca"), fora do pipeline de um vídeo novo.
---

# Analisar referência

Constrói a biblioteca de esqueletos e de técnicas a partir de vídeos que funcionaram. O esqueleto é o
padrão, nunca o texto: ele é usado pelo estrategista e pelo roteirista em qualquer tema.

## Passos

1. **Monte a lista de vídeos.** Links soltos vão direto. Se o usuário pedir "os melhores do canal X",
   liste os vídeos com a coleta de canal e escolha os de maior views e razão views/inscritos:

   ```bash
   uv run --script skills/pesquisar-trends/scripts/youtube_trends.py canal "@handle" --max 30 2>&1 | cat
   ```

   Confirme com o usuário quais analisar quando forem mais de 3.

2. **Pule o que já existe.** O esqueleto fica em `biblioteca/esqueletos/<canal-slug>-<video_id>.json`. Se
   o arquivo existir, não refaça, a menos que o usuário peça.

3. **Dispare um `analista-referencias` por vídeo**, em paralelo numa única mensagem, passando a URL e o
   caminho de saída. O agente baixa a transcrição sozinho.

4. **Técnicas novas.** Leia `tecnicas_novas_sugeridas` de cada esqueleto. Se houver, mostre ao usuário
   (id, nome, descrição em uma linha, vídeo de origem) e pergunte quais incluir. Só então acrescente as
   aprovadas em `biblioteca/tecnicas.json`, com `"origem": "esqueleto:<arquivo-sem-extensão>"`. Nunca
   renomeie ids existentes: o histórico de desempenho depende deles.

5. **Comparação**, quando houver mais de um esqueleto: o que se repete entre os vídeos (tipo de gancho,
   duração dos blocos, posição da virada, uso de CTA) e o que muda. Padrões que aparecem em vários vídeos
   que performaram são os mais confiáveis.

6. **Responda** com os caminhos dos esqueletos, o template abstrato de cada um em uma frase, as técnicas
   incluídas e os padrões comuns.

## Limitações a lembrar ao usuário

- A retenção de vídeos de terceiros não é pública: o esqueleto mostra a estrutura, não o que de fato
  segurou o público.
- O desempenho de um vídeo depende também de título, thumbnail, timing e histórico do canal.
