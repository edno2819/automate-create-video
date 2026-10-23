"""Chaves de API dos provedores: onde ficam, como ler e como gravar sem expor o valor.

As chaves ficam no arquivo .env da raiz (chmod 600, fora do controle de versão), uma por linha, no formato
VARIAVEL=valor. Uma variável de ambiente com o mesmo nome tem prioridade sobre o arquivo. Nada aqui imprime
uma chave: a interface só recebe a versão mascarada (os 4 últimos caracteres).

    sys.path.insert(0, str(RAIZ / "ferramentas"))
    import chaves
    chave = chaves.ler("openai")

Só usa a biblioteca padrão.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
ENV = RAIZ / ".env"

# provedor -> nome, variável do .env (e nomes antigos aceitos), para que serve e onde se cria a chave
PROVEDORES: dict[str, dict] = {
    "anthropic": {"nome": "Claude (Anthropic)", "variavel": "ANTHROPIC_API_KEY", "usos": ["llm"],
                  "painel": "https://platform.claude.com/settings/keys"},
    "openai": {"nome": "GPT (OpenAI)", "variavel": "OPENAI_API_KEY", "usos": ["llm", "narracao"],
               "painel": "https://platform.openai.com/api-keys"},
    "gemini": {"nome": "Gemini (Google)", "variavel": "GEMINI_API_KEY", "aceita": ["GOOGLE_API_KEY"],
               "usos": ["llm", "narracao", "imagem"], "painel": "https://aistudio.google.com/apikey"},
    "elevenlabs": {"nome": "ElevenLabs", "variavel": "ELEVENLABS_API_KEY", "usos": ["narracao"],
                   "painel": "https://elevenlabs.io/app/settings/api-keys"},
    "youtube": {"nome": "YouTube Data API", "variavel": "YOUTUBE_API_KEY", "usos": ["trends"],
                "painel": "https://console.cloud.google.com/apis/credentials"},
}
LINHA = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def ler_env() -> dict[str, str]:
    if not ENV.exists():
        return {}
    valores = {}
    for linha in ENV.read_text(encoding="utf-8").splitlines():
        if (m := LINHA.match(linha)) and not linha.lstrip().startswith("#"):
            valores[m.group(1)] = m.group(2).strip("\"'")
    return valores


def variaveis(provedor: str) -> list[str]:
    p = PROVEDORES[provedor]
    return [p["variavel"], *p.get("aceita", [])]


def origem(provedor: str) -> str | None:
    """'ambiente' (variável exportada, a interface não muda), 'arquivo' (.env) ou None."""
    if any(os.environ.get(v) for v in variaveis(provedor)):
        return "ambiente"
    env = ler_env()
    return "arquivo" if any(env.get(v) for v in variaveis(provedor)) else None


def ler(provedor: str) -> str | None:
    for v in variaveis(provedor):
        if os.environ.get(v):
            return os.environ[v]
    env = ler_env()
    return next((env[v] for v in variaveis(provedor) if env.get(v)), None)


def mascarar(valor: str | None) -> str | None:
    return None if not valor else ("•" * 8 + valor[-4:] if len(valor) > 8 else "•" * len(valor))


def gravar(provedor: str, valor: str | None) -> None:
    """Grava (ou apaga, com None) a chave no .env, preservando as outras linhas, e deixa o arquivo em 600."""
    valor = (valor or "").strip()
    if valor and (any(c in valor for c in "\n\r") or len(valor) > 400):
        raise ValueError("chave inválida")
    nomes = set(variaveis(provedor))
    linhas = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    novas, gravou = [], False
    for linha in linhas:
        m = LINHA.match(linha)
        if m and m.group(1) in nomes and not linha.lstrip().startswith("#"):
            if valor and not gravou and m.group(1) == PROVEDORES[provedor]["variavel"]:
                novas.append(f"{m.group(1)}={valor}")
                gravou = True
            continue  # apaga, ou tira o nome antigo duplicado
        novas.append(linha)
    if valor and not gravou:
        novas.append(f"{PROVEDORES[provedor]['variavel']}={valor}")
    temporario = ENV.with_name(".env.tmp")
    temporario.write_text("\n".join(novas) + "\n", encoding="utf-8")
    os.chmod(temporario, 0o600)
    temporario.replace(ENV)


def ambiente(excluir: tuple[str, ...] = ()) -> dict[str, str]:
    """As chaves do .env como variáveis, para passar aos scripts e ao Claude Code. As exportadas já estão no
    os.environ; excluir tira provedores (a chave da Anthropic faria o Claude Code cobrar pela API)."""
    env = ler_env()
    saida = {}
    for provedor in PROVEDORES:
        if provedor in excluir:
            continue
        for v in variaveis(provedor):
            if env.get(v) and not os.environ.get(v):
                saida[v] = env[v]
    return saida
