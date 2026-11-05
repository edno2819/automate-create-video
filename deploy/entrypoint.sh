#!/bin/sh
# Ponto de entrada da imagem: prepara o banco e roda o papel pedido.
#
#   web           interface e API (uvicorn), porta $PORTA (8765)
#   trabalhador   pega as execuções da fila no banco e roda as etapas
#   migrar ...    importa uma instalação local antiga (ferramentas/migrar.py; os argumentos seguem)
#   banco         só espera o banco e aplica as migrações
#   outro         roda o comando como veio (ex.: `docker compose run --rm web bash`)
#
# Antes de web, trabalhador e migrar, espera o banco responder e aplica as migrações do Alembic, sob uma
# trava do Postgres (vários contêineres sobem ao mesmo tempo e só um migra).
set -eu

# a raiz do código (/app no contêiner), de onde os módulos e os scripts são achados
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
cd "$RAIZ"

exigir_segredo() {
    # Sem APP_SEGREDO, cada contêiner geraria a própria chave em dados/segredo.key (que some quando o
    # contêiner é recriado) e as chaves de API cifradas por um não abririam no outro.
    if [ -z "${APP_SEGREDO:-}" ]; then
        echo "erro: defina APP_SEGREDO no .env (a mesma para a web e o trabalhador). Para gerar uma:" >&2
        echo "  python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'" >&2
        exit 1
    fi
}

preparar_banco() {
    python "$RAIZ/deploy/preparar_banco.py"
}

papel="${1:-web}"
case "$papel" in
    web)
        [ $# -gt 0 ] && shift
        exigir_segredo
        preparar_banco
        exec uvicorn web.servidor:app --host 0.0.0.0 --port "${PORTA:-8765}" \
            --workers "${WEB_WORKERS:-2}" --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
            --timeout-graceful-shutdown 20 "$@"
        ;;
    trabalhador)
        [ $# -gt 0 ] && shift
        exigir_segredo
        preparar_banco
        exec python -m nucleo.trabalhador "$@"
        ;;
    migrar)
        [ $# -gt 0 ] && shift
        exigir_segredo
        preparar_banco
        exec python "$RAIZ/ferramentas/migrar.py" "$@"
        ;;
    banco)
        preparar_banco
        ;;
    *)
        exec "$@"
        ;;
esac
