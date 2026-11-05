# Comandos principais do projeto. `make` (ou `make ajuda`) lista tudo.
#
# Duas formas de rodar:
#   - Docker (servidor): `make subir` sobe Postgres, MinIO, a web e o trabalhador (docker-compose.yml).
#   - Local (desenvolvimento): `make instalar` cria a .venv com as dependências do pyproject.toml e `make web`
#     sobe a interface com o trabalhador embutido, usando SQLite e a pasta dados/objetos/ quando o .env não
#     aponta para um Postgres e um MinIO/S3.
#
# Python 3.12 porque o faster-whisper (alinhamento) ainda não roda no 3.13+.

SHELL := /bin/bash
.DEFAULT_GOAL := ajuda

VENV           ?= .venv
PYTHON_VERSAO  ?= 3.12
PY             := $(VENV)/bin/python
UV             := uv
COMPOSE        ?= docker compose
HOST           ?= 127.0.0.1
PORTA          ?= 8765
# make web roda o trabalhador numa thread da própria web (EMBUTIDO=0 para usar `make trabalhador` à parte)
EMBUTIDO       ?= 1

S_PRODUCAO := ferramentas/producao

# $(call exigir,VAR,exemplo): para com uma mensagem quando a variável não foi passada.
exigir = $(if $($(1)),,$(error falta $(1). Ex.: make $@ $(1)=$(2)))
# P=<slug> vira projetos/<slug> (aceita também o caminho inteiro).
PASTA = $(if $(filter projetos/%,$(P)),$(P),projetos/$(P))

$(PY):
	@echo "Faltou a venv: rode 'make instalar' antes." >&2; exit 1

.PHONY: ajuda
ajuda: ## Lista os comandos
	@awk 'BEGIN {FS = ":.*## "} /^# ── / {sub(/^# ── /, ""); printf "\n\033[1m%s\033[0m\n", $$0} \
	     /^[a-zA-Z0-9_-]+:.*## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo
	@echo "Variáveis: P=<slug do projeto>  PEDIDO=pedidos/<nome>.yaml  URL=...  TERMO=...  ARGS='opções extras'"

# ── Ambiente local

.PHONY: venv instalar atualizar verificar limpar-venv
venv: ## Cria a venv .venv (Python 3.12, baixado pelo uv se faltar)
	@test -x $(PY) || $(UV) venv --python $(PYTHON_VERSAO) $(VENV) 2>&1 | cat

instalar: venv ## Cria a venv e instala as dependências do pyproject.toml (com as de teste)
	$(UV) pip install --python $(PY) -r pyproject.toml --extra dev 2>&1 | cat
	@echo "pronto: $(PY)"

atualizar: venv ## Atualiza as dependências da venv para as versões mais novas
	$(UV) pip install --python $(PY) --upgrade -r pyproject.toml --extra dev 2>&1 | cat

verificar: ## Confere ferramentas de sistema, fonte, banco, armazenamento e quais chaves existem (sem mostrá-las)
	@test -x $(PY) || { echo "  FALTA   venv: make instalar"; exit 1; }
	$(PY) deploy/verificar.py

limpar-venv: ## Apaga a venv
	rm -rf $(VENV)

# ── Servidor local

.PHONY: web trabalhador banco testes
web: $(PY) ## Sobe a interface (HOST=127.0.0.1 PORTA=8765; EMBUTIDO=0 sem o trabalhador junto)
	TRABALHADOR_EMBUTIDO=$(EMBUTIDO) $(PY) web/servidor.py --host $(HOST) --porta $(PORTA)

trabalhador: $(PY) ## Roda o trabalhador que executa as etapas da fila (use com make web EMBUTIDO=0)
	$(PY) -m nucleo.trabalhador

banco: $(PY) ## Cria ou migra o esquema do banco (alembic upgrade head)
	$(PY) deploy/preparar_banco.py

testes: $(PY) ## Roda os testes (pytest; ARGS="-k migrar -x" para filtrar)
	$(PY) -m pytest $(ARGS)

# ── Docker

.PHONY: imagem subir descer logs estado
imagem: ## Constrói a imagem (ARGS="--build-arg BAIXAR_WHISPER=" para não pré-baixar o whisper)
	docker build -t story-automate-youtube:latest $(ARGS) .

subir: ## Sobe tudo com o docker compose (precisa do .env: cp .env.example .env)
	@test -f .env || { echo "Faltou o .env: cp .env.example .env e preencha APP_SEGREDO e as senhas." >&2; exit 1; }
	$(COMPOSE) up -d --build

descer: ## Para os serviços do docker compose (os volumes ficam)
	$(COMPOSE) down

logs: ## Acompanha o log da web e do trabalhador
	$(COMPOSE) logs -f --tail 200 web trabalhador

estado: ## Mostra os serviços do docker compose e a saúde de cada um
	$(COMPOSE) ps

# ── Migração

.PHONY: migrar migrar-simular
migrar-simular: $(PY) ## Mostra o que a migração de uma instalação antiga faria (ARGS="--origem /caminho")
	$(PY) ferramentas/migrar.py --simular $(ARGS)

migrar: $(PY) ## Importa a instalação local antiga para o banco e o armazenamento (ARGS="--origem /caminho")
	$(PY) ferramentas/migrar.py $(ARGS)

# ── Pedido

.PHONY: modelo validar brief
modelo: $(PY) ## Gera pedidos/modelo.yaml de novo a partir de CAMPOS
	$(PY) ferramentas/pedido.py modelo pedidos/modelo.yaml

validar: $(PY) ## Confere um pedido e lista o que falta decidir: PEDIDO=pedidos/<nome>.yaml
	$(call exigir,PEDIDO,pedidos/meu-video.yaml)
	$(PY) ferramentas/pedido.py validar $(PEDIDO)

brief: $(PY) ## Gera 00-brief.json do pedido aprovado: P=<slug>
	$(call exigir,P,meu-video)
	$(PY) ferramentas/pedido.py brief $(PASTA)

# ── Roteiro e cenas

.PHONY: medir conferir cenas custos
medir: $(PY) ## Mede um roteiro (palavras, tempos, re-hooks): P=<slug> (o final) ou ROTEIRO=<arquivo.md>
	@$(if $(P)$(ROTEIRO),,$(error falta P ou ROTEIRO. Ex.: make medir P=meu-video))
	$(PY) ferramentas/medir_roteiro.py $(if $(ROTEIRO),$(ROTEIRO),$(PASTA)/06-roteiro-final.md)

conferir: $(PY) ## A narração é a mesma, palavra por palavra? ORIGINAL=<arquivo> FORMATADO=<arquivo>
	$(call exigir,ORIGINAL,roteiro.txt)
	$(call exigir,FORMATADO,projetos/meu-video/06-roteiro-final.md)
	$(PY) ferramentas/conferir_texto.py $(ORIGINAL) $(FORMATADO)

cenas: $(PY) ## Valida as cenas e monta prompts e storyboard: P=<slug>
	$(call exigir,P,meu-video)
	$(PY) ferramentas/cenas.py montar $(PASTA)

custos: $(PY) ## Custo das imagens e da narração de um projeto: P=<slug>
	$(call exigir,P,meu-video)
	$(PY) ferramentas/custos.py $(PASTA) $(ARGS)

# ── Produção (scripts, sem LLM)

.PHONY: buscar-imagens imagens imagens-teste vozes amostras-vozes narrar narrar-teste alinhar montar video-teste
buscar-imagens: $(PY) ## Busca as imagens da internet das cenas marcadas: P=<slug> [ARGS=--simular]
	$(call exigir,P,meu-video)
	$(PY) $(S_PRODUCAO)/buscar_imagens.py $(PASTA) $(ARGS)

imagens: $(PY) ## Gera fichas e cenas (pago): P=<slug> [ARGS="--simular", "--provedor openai" ou "--cenas B01-02 --refazer"]
	$(call exigir,P,meu-video)
	$(PY) $(S_PRODUCAO)/gerar_imagens.py $(PASTA) $(ARGS)

imagens-teste: $(PY) ## Quadros provisórios, sem API: P=<slug>
	$(call exigir,P,meu-video)
	$(PY) $(S_PRODUCAO)/gerar_imagens.py $(PASTA) --provisorio

vozes: $(PY) ## Lista as vozes de narração
	$(PY) $(S_PRODUCAO)/narrar.py --listar-vozes $(ARGS)

amostras-vozes: $(PY) ## Grava de novo as amostras de voz de biblioteca/vozes/ (pago)
	$(PY) $(S_PRODUCAO)/narrar.py --amostras $(ARGS)

narrar: $(PY) ## Narra com as vozes do pedido (pago): P=<slug> [ARGS="--blocos B03 --refazer"]
	$(call exigir,P,meu-video)
	$(PY) $(S_PRODUCAO)/narrar.py $(PASTA) $(ARGS)

narrar-teste: $(PY) ## Narra com o espeak local, de graça: P=<slug>
	$(call exigir,P,meu-video)
	$(PY) $(S_PRODUCAO)/narrar.py $(PASTA) --fornecedor espeak $(ARGS)

alinhar: $(PY) ## Mede o tempo de cada palavra na narração (faster-whisper): P=<slug>
	$(call exigir,P,meu-video)
	$(PY) $(S_PRODUCAO)/alinhar.py $(PASTA) $(ARGS)

montar: $(PY) ## Monta o vídeo com ffmpeg: P=<slug> [ARGS="--sem-movimento"]
	$(call exigir,P,meu-video)
	$(PY) $(S_PRODUCAO)/montar_video.py $(PASTA) $(ARGS)

video-teste: imagens-teste narrar-teste alinhar montar ## Vídeo de teste inteiro, sem gastar: P=<slug> (precisa de 08-cenas.json)

# ── Pesquisa e dados

.PHONY: trends-dados transcricao desempenho resumo-desempenho
trends-dados: $(PY) ## Dados de trends do YouTube, sem análise: TERMO="..." [ARGS="--dias 90 --formato short"]
	$(call exigir,TERMO,"novela das frutas")
	$(PY) ferramentas/youtube_trends.py buscar "$(TERMO)" $(ARGS)

transcricao: $(PY) ## Baixa a transcrição e a descrição de um vídeo: URL=<link do YouTube>
	$(call exigir,URL,https://www.youtube.com/watch?v=...)
	$(PY) ferramentas/fetch_youtube.py "$(URL)" $(ARGS)

desempenho: $(PY) ## Registra as métricas de um vídeo publicado: ARGS="registrar --projeto x --video-id y --janela 7d --ctr 6.2 ..."
	$(call exigir,ARGS,"registrar --projeto meu-video --video-id abc --janela 7d --ctr 6.2")
	$(PY) ferramentas/desempenho.py $(ARGS)

resumo-desempenho: $(PY) ## Resumo do que funciona no canal (gera biblioteca/aprendizados.md)
	$(PY) ferramentas/desempenho.py resumo $(ARGS)
