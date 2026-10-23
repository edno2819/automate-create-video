---
name: registrar-desempenho
description: Registra as métricas de um vídeo publicado (CTR, retenção aos 30 s, retenção média, viu vs. passou, views, inscritos) ligadas às técnicas e ao gancho usados no roteiro, e gera biblioteca/aprendizados.md com o que funciona no canal. Use quando o usuário trouxer números de um vídeo publicado ("o vídeo X teve 7% de CTR", "registra as métricas", "atualiza o desempenho"), pedir um resumo do que está funcionando ("quais ganchos retêm mais?") ou depois de publicar um vídeo do pipeline.
---

# Registrar desempenho

É o ciclo de feedback que torna o sistema melhor com o tempo. Sem ele o estrategista fica preso a boas
práticas genéricas, que todos os concorrentes também usam. Depois de 20 a 30 vídeos registrados, o
`biblioteca/aprendizados.md` passa a dizer coisas como "ganchos do tipo tese contrária tiveram 12% a mais
de retenção aos 30 s neste canal", e o estrategista usa isso ao propor ângulos.

## Onde achar os números

No YouTube Studio, aba **Analytics** do vídeo:

| Métrica | Onde | Opção |
|---|---|---|
| CTR | Alcance → "Taxa de cliques de impressões" | `--ctr` |
| Retenção aos 30 s | Engajamento → retenção do público, ponto de 0:30 | `--retencao-30s` |
| Retenção média | Engajamento → "Porcentagem média visualizada" | `--retencao-media` |
| Viu vs. passou (shorts) | Engajamento → "Visualizaram vs. deslizaram" | `--viu-vs-passou` |
| Views | Visão geral | `--views` |
| Inscritos ganhos | Público | `--inscritos-ganhos` |

Registre sempre na mesma janela para comparar igual com igual: **48h**, **7d** e **28d**. Cada janela é um
registro separado; registrar de novo a mesma janela do mesmo vídeo substitui o anterior.

## Como rodar

A partir da raiz do projeto:

```bash
D=skills/registrar-desempenho/scripts/desempenho.py

# vídeo longo: técnicas e gancho vêm do frontmatter de projetos/<slug>/06-roteiro-final.md
python3 $D registrar --projeto <slug> --video-id <id> --janela 7d \
  --ctr 6.2 --retencao-30s 71 --retencao-media 45 --views 12000 --inscritos-ganhos 120

# corte: aponte o roteiro do corte
python3 $D registrar --projeto <slug> --roteiro projetos/<slug>/07-cortes/corte-02.md \
  --video-id <id> --janela 7d --viu-vs-passou 78 --retencao-media 92 --views 40000

# vídeo feito fora do pipeline: informe técnicas e gancho na mão
python3 $D registrar --projeto avulso --video-id <id> --formato longo \
  --tecnicas loop-aberto,contagem-regressiva --tipo-gancho dado-chocante --ctr 5 --views 3000

# regenerar os aprendizados
python3 $D resumo --janela 7d
```

Os ids de técnicas e de tipos de gancho são os de `biblioteca/tecnicas.json`. Os dados ficam em
`dados/desempenho.jsonl`, um registro por linha.

## Passos

1. Pegue os números com o usuário. Se faltar algum, registre só os que ele tem; não estime.
2. Rode `registrar`. Confira na saída se as técnicas e o gancho vieram do roteiro certo.
3. Rode `resumo` com a janela mais usada nos registros.
4. Conte ao usuário o que mudou nos aprendizados, separando sinal (grupo com 3 ou mais vídeos e diferença
   acima de 10%) de ruído. Com poucos vídeos, diga que ainda é cedo para conclusões.

## Ainda não implementado

A coleta automática pela YouTube Analytics API, que exige OAuth da conta do canal. Quando for feita, ela
substitui a digitação manual destes números.
