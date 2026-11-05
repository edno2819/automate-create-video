# Vídeos para o YouTube com agentes de LLM

Sistema que cria vídeos para o YouTube de ponta a ponta com uma rede de agentes especializados. Você pede o
vídeo num formulário (o título, a descrição e até onde ir são obrigatórios; até onde ir já vem no vídeo completo).
O sistema completa o resto e você aprova. Depois ele faz, nesta ordem, até onde você pediu:

1. pesquisa, ângulos (título e thumbnail), ganchos (você escolhe entre os três melhores) e roteiro com crítico;
2. direção visual, imagens, narração e montagem;
3. textos de publicação.

Roda num servidor só com as chaves de API dos provedores: Claude, GPT ou Gemini para os agentes; Gemini ou
OpenAI para as imagens; Gemini, OpenAI, ElevenLabs ou espeak para a voz. Tudo o que é estado fica no banco
(Postgres), e os arquivos ficam no armazenamento de objetos: MinIO por padrão, AWS S3 quando configurado.

## Subir com Docker

```bash
cp .env.example .env && chmod 600 .env
# no .env: APP_SEGREDO (cifra as chaves de API no banco), POSTGRES_PASSWORD e MINIO_ROOT_PASSWORD;
# opcional: APP_USUARIO e APP_SENHA (login da interface)
docker compose up -d --build
```

A interface fica em `http://<servidor>:8765`. O compose sobe quatro serviços:

| Serviço | O que faz |
|---|---|
| `postgres` | o banco |
| `minio` (+ `minio-init`, que cria o bucket) | o armazenamento dos arquivos |
| `web` | a interface e a API |
| `trabalhador` | roda as etapas da fila |

Para rodar mais etapas ao mesmo tempo, aumente `TRABALHADOR_CONCORRENCIA` ou suba mais trabalhadores
(`docker compose up -d --scale trabalhador=3`).

Para gerar o `APP_SEGREDO`:

```bash
python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
```

Use o mesmo valor na web e no trabalhador, e nunca o troque depois de cadastrar chaves: as já cifradas deixariam
de abrir.

### AWS S3 e Postgres próprios

No `.env`:

```bash
COMPOSE_PROFILES=                 # sem o postgres e o minio do compose
DATABASE_URL=postgresql+psycopg://usuario:senha@host:5432/banco
ARMAZENAMENTO=s3
AWS_S3_BUCKET=meu-bucket
AWS_REGION=sa-east-1
AWS_ACCESS_KEY_ID=...             # ou um papel IAM na máquina
AWS_SECRET_ACCESS_KEY=...
```

Com `AWS_S3_BUCKET` definido, o S3 é escolhido mesmo sem `ARMAZENAMENTO=s3`. Sem nenhuma configuração de
armazenamento (desenvolvimento local), os objetos vão para `dados/objetos/`.

## Configurar pela interface

Em **Configurações**:

- **Chaves de API:** cadastre e teste a chave de cada provedor (Anthropic, OpenAI, Gemini, ElevenLabs e YouTube
  Data API). As chaves ficam no banco, cifradas. Uma variável de ambiente com o mesmo nome tem prioridade e não
  pode ser editada pela tela. O teste traz a lista de modelos que a chave acessa.
- **Modelos por etapa:** para cada etapa (sugestões, pesquisa, ângulos, ganchos, roteiro, direção visual,
  produção...), escolha o provedor e o modelo. Abra a etapa para dar a um agente um modelo próprio, por exemplo
  um crítico de outro provedor que o roteirista. Sem modelo escolhido, vale o padrão do provedor para o nível do
  agente: um modelo maior para os de julgamento (estrategista, roteirista, crítico, diretor visual...) e um
  menor para os de coleta (pesquisadores, formatador, publicador). Também dá para escolher quem faz a busca na
  web.
- **Imagens e voz:** o provedor e o modelo de imagem para cada qualidade (rascunho, padrão, máxima) e quantas
  imagens gerar ao mesmo tempo; a animação das imagens (provedor de vídeo, modelo, resolução, 720p por padrão, e
  quantas cenas animar ao mesmo tempo); o fornecedor padrão da narração e o modelo de cada um; e o modelo do whisper
  que confere a voz e alinha as legendas. O pedido de um vídeo pode escolher outro fornecedor de voz e liga a
  animação no campo **Animação das imagens** (`destaques` ou `todas`; antes de gastar, a produção mostra a
  estimativa e pede confirmação).
- **Efeitos sonoros:** a biblioteca de efeitos (nome e arquivo de áudio) que o diretor visual pode pôr nas cenas.
- **Sistema:** onde estão o banco e o armazenamento, e a saúde de cada um.

## Migrar uma instalação antiga

Se você usava a versão local, com a pasta `projetos/`, o `.env` e o `dados/web.sqlite3`, o
`ferramentas/migrar.py` leva tudo para o banco e o armazenamento novos: projetos, execuções com o log, chaves,
escolha de modelos, contextos, lições de roteiro, métricas e custos. Pode rodar mais de uma vez: o que já foi
importado não se repete.

```bash
docker compose run --rm -v /caminho/antigo:/antigo:ro trabalhador migrar --origem /antigo --simular
docker compose run --rm -v /caminho/antigo:/antigo:ro trabalhador migrar --origem /antigo
```

## Desenvolvimento

```bash
make instalar    # venv com Python 3.12 e as dependências do pyproject.toml
make web         # interface em http://127.0.0.1:8765, com o trabalhador numa thread (SQLite e pasta local)
make testes
make             # lista os outros comandos (scripts de produção, medição de roteiro, compose...)
```

Precisa de `ffmpeg` com libass, `espeak-ng` e `fonts-noto-core` (fonte das legendas).

## Arquitetura

- `agentes/`: os prompts dos agentes, um por arquivo. O frontmatter de cada um diz o nível, as ferramentas e se
  ele vê imagens.
- `nucleo/agentes/`: o motor que roda esses agentes com qualquer provedor. Ele oferece ferramentas com caminhos
  validados (ler e gravar arquivos, medir roteiro, montar cenas, pesquisar no YouTube...) e valida as saídas
  estruturadas.
- `nucleo/pipeline/`:
  - as etapas;
  - a fila de execuções no banco e o trabalhador;
  - os fluxos de cada fase, com os laços de qualidade: ganchos lidos a frio e pontuados, leitura fria isolada,
    aprovação recalculada pela rubrica, obrigações do contrato conferidas, revisão visual com refazer, narração
    conferida pelo whisper.
- `nucleo/provedores/`: as interfaces de LLM, voz e imagem e um adaptador por provedor.
- `nucleo/banco/`, `nucleo/armazenamento/` e `nucleo/espaco.py`: Postgres com migrações (Alembic), MinIO/S3 e o
  espaço de trabalho local. A cópia local é sincronizada por hash antes e depois de cada passo: agentes, ffmpeg e
  whisper trabalham com arquivos, e a fonte da verdade fica no armazenamento.
- `ferramentas/`: os scripts determinísticos (pedido, medição, cenas, imagens, narração, alinhamento, montagem).
- `web/`: a API (FastAPI) e a interface.
- `biblioteca/`: técnicas narrativas, rubrica do crítico, formatos, personagens, músicas e preços.

O contrato entre as camadas está em [docs/arquitetura.md](docs/arquitetura.md).
