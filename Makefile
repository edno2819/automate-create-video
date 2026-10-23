# Comandos principais do projeto. `make` (ou `make ajuda`) lista tudo.
#
# A venv (.venv) junta as dependências que cada script declara no próprio arquivo (PEP 723): o uv lê esses
# blocos direto, então não há requirements.txt para manter. Python 3.12 porque o faster-whisper (alinhar.py)
# ainda não roda no 3.13+.
#
# As skills abrem o Claude Code interativo na raiz do projeto: elas fazem perguntas no meio (aprovar o
# pedido, escolher o ângulo...). Para o fluxo com tela, use `make web`.

SHELL := /bin/bash
.DEFAULT_GOAL := ajuda

VENV           ?= .venv
PYTHON_VERSAO  ?= 3.12
PY             := $(VENV)/bin/python
UV             := uv
CLAUDE         ?= claude
HOST           ?= 127.0.0.1
PORTA          ?= 8765

# Scripts com dependências (blocos PEP 723); os outros só usam a biblioteca padrão.
SCRIPTS_COM_DEPS := web/servidor.py ferramentas/pedido.py skills/produzir-video/scripts/alinhar.py \
                    skills/pesquisar-trends/scripts/youtube_trends.py skills/youtube-transcript/scripts/fetch_youtube.py

S_PRODUZIR := skills/produzir-video/scripts

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

# ── Ambiente

.PHONY: venv instalar atualizar verificar limpar-venv
venv: ## Cria a venv .venv (Python 3.12, baixado pelo uv se faltar)
	@test -x $(PY) || $(UV) venv --python $(PYTHON_VERSAO) $(VENV) 2>&1 | cat

instalar: venv ## Cria a venv e instala as libs de todos os scripts
	$(UV) pip install --python $(PY) $(addprefix -r ,$(SCRIPTS_COM_DEPS)) 2>&1 | cat
	@echo "pronto: $(PY)"

atualizar: venv ## Atualiza as libs da venv para as versões mais novas
	$(UV) pip install --python $(PY) --upgrade $(addprefix -r ,$(SCRIPTS_COM_DEPS)) 2>&1 | cat

verificar: ## Confere as ferramentas de sistema (ffmpeg, espeak-ng, claude, yt-dlp) e as chaves
	@for cmd in $(UV) $(CLAUDE) ffmpeg ffprobe espeak-ng yt-dlp; do \
	  if command -v $$cmd >/dev/null; then echo "  ok      $$cmd"; else echo "  FALTA   $$cmd"; fi; done
	@test -x $(PY) && echo "  ok      venv ($$($(PY) --version))" || echo "  FALTA   venv: make instalar"
	@ffmpeg -hide_banner -filters 2>/dev/null | grep -q " ass " && echo "  ok      ffmpeg com libass" || echo "  FALTA   ffmpeg com libass (legendas)"
	@grep -q '^GEMINI_API_KEY=.' .env 2>/dev/null && echo "  ok      GEMINI_API_KEY no .env" || echo "  FALTA   GEMINI_API_KEY no .env (imagens e narração)"
	@test -n "$$YOUTUBE_API_KEY" && echo "  ok      YOUTUBE_API_KEY" || echo "  opcional YOUTUBE_API_KEY (sem ela, as trends usam o yt-dlp)"

limpar-venv: ## Apaga a venv
	rm -rf $(VENV)

# ── Interface web

.PHONY: web
web: $(PY) ## Sobe a interface web (HOST=127.0.0.1 PORTA=8765)
	$(PY) web/servidor.py --host $(HOST) --porta $(PORTA)

# ── Skills (Claude Code interativo)

.PHONY: video retomar visual produzir personagem referencia trends desempenho
video: ## Pipeline completo a partir de um pedido: PEDIDO=pedidos/<nome>.yaml (ou TEMA="...")
	@$(if $(PEDIDO)$(TEMA),,$(error falta PEDIDO ou TEMA. Ex.: make video PEDIDO=pedidos/meu-video.yaml))
	$(CLAUDE) "/novo-video $(if $(PEDIDO),$(PEDIDO),$(TEMA))"

retomar: ## Retoma ou refaz uma fase de um projeto: P=<slug> [ARGS="refaz o roteiro"]
	$(call exigir,P,meu-video)
	$(CLAUDE) "/novo-video retoma o projeto $(PASTA) $(ARGS)"

visual: ## Direção visual de um roteiro: P=<slug> (ou ARGS=caminho/do/roteiro.md)
	@$(if $(P)$(ARGS),,$(error falta P ou ARGS. Ex.: make visual P=meu-video))
	$(CLAUDE) "/direcao-visual $(if $(P),$(PASTA)) $(ARGS)"

produzir: ## Imagens, narração e montagem: P=<slug> [ARGS="teste com espeak"]
	$(call exigir,P,meu-video)
	$(CLAUDE) "/produzir-video $(PASTA) $(ARGS)"

personagem: ## Cria um personagem para a biblioteca: DESC="um morango vaidoso de olhos grandes"
	$(call exigir,DESC,"um morango vaidoso")
	$(CLAUDE) "/criar-personagem $(DESC)"

referencia: ## Estuda um vídeo de referência e alimenta a biblioteca: URL=<link do YouTube>
	$(call exigir,URL,https://www.youtube.com/watch?v=...)
	$(CLAUDE) "/analisar-referencia $(URL)"

trends: ## O que está performando num nicho, com análise: TERMO="novela das frutas"
	$(call exigir,TERMO,"novela das frutas")
	$(CLAUDE) "/pesquisar-trends $(TERMO)"

desempenho: ## Registra métricas de um vídeo publicado: ARGS="o vídeo X teve 7% de CTR..."
	$(CLAUDE) "/registrar-desempenho $(ARGS)"

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

.PHONY: medir conferir cenas
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

# ── Produção (scripts, sem LLM)

.PHONY: buscar-imagens imagens imagens-teste vozes narrar narrar-teste alinhar montar video-teste
buscar-imagens: $(PY) ## Busca as imagens da internet das cenas marcadas: P=<slug> [ARGS=--simular]
	$(call exigir,P,meu-video)
	$(PY) $(S_PRODUZIR)/buscar_imagens.py $(PASTA) $(ARGS)

imagens: $(PY) ## Gera fichas e cenas com o Gemini (pago): P=<slug> [ARGS="--simular" ou "--cenas B01-02 --refazer"]
	$(call exigir,P,meu-video)
	$(PY) $(S_PRODUZIR)/gerar_imagens.py $(PASTA) $(ARGS)

imagens-teste: $(PY) ## Quadros provisórios, sem API: P=<slug>
	$(call exigir,P,meu-video)
	$(PY) $(S_PRODUZIR)/gerar_imagens.py $(PASTA) --provisorio

vozes: $(PY) ## Lista as vozes do Gemini TTS
	$(PY) $(S_PRODUZIR)/narrar.py --listar-vozes

narrar: $(PY) ## Narra com as vozes do pedido (Gemini, pago): P=<slug> [ARGS="--blocos B03 --refazer"]
	$(call exigir,P,meu-video)
	$(PY) $(S_PRODUZIR)/narrar.py $(PASTA) $(ARGS)

narrar-teste: $(PY) ## Narra com o espeak local, de graça: P=<slug>
	$(call exigir,P,meu-video)
	$(PY) $(S_PRODUZIR)/narrar.py $(PASTA) --fornecedor espeak $(ARGS)

alinhar: $(PY) ## Mede o tempo de cada palavra na narração (faster-whisper): P=<slug>
	$(call exigir,P,meu-video)
	$(PY) $(S_PRODUZIR)/alinhar.py $(PASTA) $(ARGS)

montar: $(PY) ## Monta o vídeo com ffmpeg: P=<slug> [ARGS="--sem-movimento"]
	$(call exigir,P,meu-video)
	$(PY) $(S_PRODUZIR)/montar_video.py $(PASTA) $(ARGS)

video-teste: imagens-teste narrar-teste alinhar montar ## Vídeo de teste inteiro, sem gastar: P=<slug> (precisa de 08-cenas.json)

# ── Pesquisa e dados

.PHONY: trends-dados transcricao resumo-desempenho
trends-dados: $(PY) ## Só os dados de trends, sem análise: TERMO="..." [ARGS="--dias 90 --formato short"]
	$(call exigir,TERMO,"novela das frutas")
	$(PY) skills/pesquisar-trends/scripts/youtube_trends.py buscar "$(TERMO)" $(ARGS)

transcricao: $(PY) ## Baixa a transcrição e a descrição de um vídeo: URL=<link do YouTube>
	$(call exigir,URL,https://www.youtube.com/watch?v=...)
	$(PY) skills/youtube-transcript/scripts/fetch_youtube.py "$(URL)" $(ARGS)

resumo-desempenho: $(PY) ## Resumo do que funciona no canal (gera biblioteca/aprendizados.md)
	$(PY) skills/registrar-desempenho/scripts/desempenho.py resumo $(ARGS)
