"""Chaves de API dos provedores: onde ficam, como ler e como gravar sem expor o valor.

Ordem de leitura:
1. variável de ambiente (ANTHROPIC_API_KEY...): quem implantou decidiu, e a interface não muda;
2. banco (tabela chaves_api), cifrada com o segredo do servidor (nucleo/seguranca.py): o que se cadastra em
   Configurações > Chaves de API;
3. o .env da raiz (legado, só leitura): instalações antigas, até o ferramentas/migrar.py levar as chaves ao banco.

Nada aqui imprime uma chave: a interface só recebe a versão mascarada (os 4 últimos caracteres). Os scripts que
rodam sem o servidor (sem banco acessível) continuam funcionando com o ambiente e o .env.

    from nucleo import chaves
    chave = chaves.ler("openai")
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from nucleo.config import config
from nucleo.seguranca import mascarar

__all__ = ["PROVEDORES", "ambiente", "gravar", "ler", "ler_env", "mascarar", "origem", "variaveis"]

# provedor -> nome, variável de ambiente (e nomes antigos aceitos), para que serve e onde se cria a chave
PROVEDORES: dict[str, dict] = {
    "anthropic": {"nome": "Claude (Anthropic)", "variavel": "ANTHROPIC_API_KEY", "usos": ["llm"],
                  "painel": "https://platform.claude.com/settings/keys"},
    "openai": {"nome": "GPT (OpenAI)", "variavel": "OPENAI_API_KEY", "usos": ["llm", "narracao", "imagem"],
               "painel": "https://platform.openai.com/api-keys"},
    "gemini": {"nome": "Gemini (Google)", "variavel": "GEMINI_API_KEY", "aceita": ["GOOGLE_API_KEY"],
               "usos": ["llm", "narracao", "imagem", "animacao"], "painel": "https://aistudio.google.com/apikey"},
    "elevenlabs": {"nome": "ElevenLabs", "variavel": "ELEVENLABS_API_KEY", "usos": ["narracao"],
                   "painel": "https://elevenlabs.io/app/settings/api-keys"},
    "youtube": {"nome": "YouTube Data API", "variavel": "YOUTUBE_API_KEY", "usos": ["trends"],
                "painel": "https://console.cloud.google.com/apis/credentials"},
}
LINHA = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
TAMANHO_MAXIMO = 400


def _arquivo_env() -> Path:
    return config().raiz / ".env"


def ler_env() -> dict[str, str]:
    """O .env legado da raiz (só leitura)."""
    arquivo = _arquivo_env()
    if not arquivo.exists():
        return {}
    valores = {}
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        if (m := LINHA.match(linha)) and not linha.lstrip().startswith("#"):
            valores[m.group(1)] = m.group(2).strip("\"'")
    return valores


def variaveis(provedor: str) -> list[str]:
    p = PROVEDORES[provedor]
    return [p["variavel"], *p.get("aceita", [])]


def _do_ambiente(provedor: str) -> str | None:
    return next((os.environ[v] for v in variaveis(provedor) if os.environ.get(v)), None)


def _do_banco(provedor: str) -> str | None:
    """A chave do banco, ou None se não há, ou se o banco não está acessível (script rodando sem servidor)."""
    from nucleo import banco
    if not banco.disponivel():
        return None
    try:
        from nucleo.banco import repositorios
        return repositorios.ler_chave(provedor)
    except Exception:  # noqa: BLE001 - banco fora do ar no meio do caminho: segue com o .env
        return None


def _do_arquivo(provedor: str, env: dict[str, str] | None = None) -> str | None:
    env = ler_env() if env is None else env
    return next((env[v] for v in variaveis(provedor) if env.get(v)), None)


def origem(provedor: str) -> str | None:
    """'ambiente' (variável exportada: a interface não muda), 'banco' (cadastrada na interface), 'arquivo'
    (.env legado) ou None."""
    if _do_ambiente(provedor):
        return "ambiente"
    if _do_banco(provedor):
        return "banco"
    return "arquivo" if _do_arquivo(provedor) else None


def ler(provedor: str) -> str | None:
    return _do_ambiente(provedor) or _do_banco(provedor) or _do_arquivo(provedor)


def gravar(provedor: str, valor: str | None) -> None:
    """Grava a chave no banco, cifrada (ou apaga, com None ou vazio).

    Recusa (ValueError) quando a chave vem do ambiente: quem a definiu lá é quem a troca. Apagar tira só a do
    banco; uma chave do .env legado continua valendo até sair do arquivo.
    """
    if provedor not in PROVEDORES:
        raise ValueError(f"provedor desconhecido: {provedor}")
    valor = (valor or "").strip()
    if valor and (any(c in valor for c in "\n\r") or len(valor) > TAMANHO_MAXIMO):
        raise ValueError("chave inválida")
    if _do_ambiente(provedor):
        raise ValueError(f"a chave de {PROVEDORES[provedor]['nome']} vem da variável de ambiente "
                         f"{PROVEDORES[provedor]['variavel']}: troque-a onde o servidor é configurado")
    from nucleo.banco import repositorios
    repositorios.gravar_chave(provedor, valor or None)


def ambiente(excluir: tuple[str, ...] = ()) -> dict[str, str]:
    """As chaves do banco e do .env como variáveis, para os subprocessos (scripts de produção). As que já estão
    no ambiente não entram (o subprocesso herda). `excluir` tira provedores."""
    env = ler_env()
    saida = {}
    for provedor, dados in PROVEDORES.items():
        if provedor in excluir or _do_ambiente(provedor):
            continue
        if valor := _do_banco(provedor) or _do_arquivo(provedor, env):
            saida[dados["variavel"]] = valor
    return saida
