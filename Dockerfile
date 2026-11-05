# Imagem única do servidor de vídeos: a interface web (`web`) e o trabalhador que roda as etapas (`trabalhador`).
#
#     docker build -t story-automate-youtube .
#     docker build --build-arg BAIXAR_WHISPER= -t story-automate-youtube .   # sem pré-baixar o modelo do whisper
#
# O código fica em /app, que também é o espaço de trabalho local (projetos/, pedidos/, biblioteca/, rascunhos/):
# a fonte da verdade é o armazenamento de objetos (MinIO ou S3) com o índice no banco; ver docs/arquitetura.md.

# Debian trixie: o ffmpeg 7 tem o que a montagem usa (drawtext com y_align, o layout de canais do aresample);
# o 5.1 do bookworm quebra a thumbnail e a mixagem do áudio.
FROM python:3.12-slim-trixie

# ffmpeg (com libass, para as legendas), espeak-ng (narração de teste), Noto Sans (cartelas e legendas: o
# montar_video.py procura "Noto Sans:bold" no fontconfig), jq e curl (healthcheck), tini (repassa sinais e
# recolhe os processos filhos: ffmpeg e scripts de produção), tzdata (TZ e zoneinfo: a data dos agentes é a de
# America/Sao_Paulo).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg espeak-ng fonts-noto-core fontconfig jq curl tini tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/Sao_Paulo \
    HF_HOME=/cache/hf \
    PORTA=8765

# Usuário sem root; a pasta do cache do whisper já nasce dele, para o volume herdar o dono.
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --home-dir /home/app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app /cache/hf \
    && chown -R app:app /app /cache

WORKDIR /app

# Camada das dependências: só muda quando o pyproject.toml muda. Lê a lista de dependências do próprio
# pyproject (o código não é empacotado).
COPY pyproject.toml /tmp/pyproject.toml
RUN python -c "import tomllib; d = tomllib.load(open('/tmp/pyproject.toml', 'rb')); print('\n'.join(d['project']['dependencies']))" \
        > /tmp/requisitos.txt \
    && pip install -r /tmp/requisitos.txt \
    && rm /tmp/requisitos.txt /tmp/pyproject.toml

# Modelo do faster-whisper do alinhamento, pré-baixado para a primeira narração não esperar o download.
# Vazio (--build-arg BAIXAR_WHISPER=) pula; o modelo é baixado no primeiro uso para o volume de HF_HOME.
ARG BAIXAR_WHISPER=small
USER app
RUN if [ -n "$BAIXAR_WHISPER" ]; then \
        python -c "import sys; from faster_whisper import WhisperModel; WhisperModel(sys.argv[1], device='cpu', compute_type='int8')" \
            "$BAIXAR_WHISPER"; \
    fi
USER root

COPY --chown=app:app . /app
RUN chmod +x /app/deploy/entrypoint.sh

USER app
EXPOSE 8765

# O trabalhador não tem servidor HTTP: no docker-compose.yml o healthcheck dele é desligado.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORTA:-8765}/api/saude" || exit 1

ENTRYPOINT ["tini", "--", "/app/deploy/entrypoint.sh"]
CMD ["web"]
