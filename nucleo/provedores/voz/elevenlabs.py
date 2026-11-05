"""ElevenLabs (/v1/text-to-speech), em PCM de 24 kHz.

A voz é o nome de uma voz da conta (ou o voice_id). Não há instrução de estilo: o jeito de falar vem da própria
voz escolhida. Cobrança por caractere.

As vozes da conta (as prontas e as clonadas) ficam em cache para validar pedidos e montar a tela sem rede: no
banco (`configuracoes["vozes_elevenlabs"]`), com reserva no arquivo biblioteca/vozes/elevenlabs.json (o formato
de sempre, que serve também aos scripts rodando sem banco). A lista é de cada instância, nunca da classe:
execuções concorrentes no mesmo processo não pisam umas nas outras.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from nucleo.provedores import http
from nucleo.provedores.erros import ErroProvedor
from nucleo.provedores.voz.base import ProvedorVoz, Voz, para_wav

API = "https://api.elevenlabs.io/v1/text-to-speech/{voz}?output_format=pcm_24000"
API_VOZES = "https://api.elevenlabs.io/v2/voices?page_size=100"
CHAVE_CONFIG = "vozes_elevenlabs"


def arquivo_cache() -> Path:
    from nucleo.config import config
    return config().raiz / "biblioteca" / "vozes" / "elevenlabs.json"


def _de_registros(registros: list[dict]) -> list[Voz]:
    return [Voz(r.get("nome") or r["id"], r.get("genero") or "?", r.get("descricao") or "", r.get("id"), r.get("amostra"))
            for r in registros if r.get("nome") or r.get("id")]


def _para_registros(vozes: list[Voz]) -> list[dict]:
    return [{"nome": v.nome, "id": v.id, "genero": v.genero, "descricao": v.descricao, "amostra": v.amostra_url}
            for v in vozes]


def vozes_em_cache() -> list[Voz]:
    """A lista guardada: a do banco, se houver banco; senão a do arquivo."""
    try:
        from nucleo.banco import repositorios
        guardado = repositorios.ler_config(CHAVE_CONFIG)
        if guardado and guardado.get("vozes"):
            return _de_registros(guardado["vozes"])
    except Exception:  # sem banco (script avulso): vale o arquivo
        pass
    try:
        return _de_registros(json.loads(arquivo_cache().read_text(encoding="utf-8")).get("vozes", []))
    except (OSError, json.JSONDecodeError, KeyError):
        return []


def guardar_vozes(vozes: list[Voz]) -> None:
    dados = {"atualizado_em": time.strftime("%Y-%m-%d %H:%M"), "vozes": _para_registros(vozes)}
    try:
        from nucleo.banco import repositorios
        repositorios.gravar_config(CHAVE_CONFIG, dados)
    except Exception as e:
        print(f"aviso: não consegui guardar as vozes da ElevenLabs no banco ({e}); fica o arquivo", file=sys.stderr)
    try:
        destino = arquivo_cache()
        destino.parent.mkdir(parents=True, exist_ok=True)
        temporario = destino.with_suffix(".json.tmp")
        temporario.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporario.replace(destino)
    except OSError as e:
        print(f"aviso: não consegui gravar {e.filename}: {e.strerror}", file=sys.stderr)


def listar_vozes_da_conta(chave: str) -> list[Voz]:
    dados = http.pedir_json(API_VOZES, None, {"xi-api-key": chave}, metodo="GET", timeout=30, provedor="elevenlabs")
    vozes = []
    for v in dados.get("voices") or []:
        rotulos = v.get("labels") or {}
        genero = {"female": "f", "male": "m"}.get(str(rotulos.get("gender", "")).lower(), "?")
        descricao = ", ".join(str(x) for x in (rotulos.get("description") or rotulos.get("descriptive"), rotulos.get("accent"),
                                                 rotulos.get("use_case")) if x)
        vozes.append(Voz(v.get("name", v["voice_id"]), genero, descricao or v.get("category", ""), v["voice_id"],
                         v.get("preview_url")))
    return vozes


def atualizar_vozes(chave: str) -> list[Voz]:
    """Busca as vozes da conta e guarda no cache (banco e arquivo)."""
    vozes = listar_vozes_da_conta(chave)
    guardar_vozes(vozes)
    return vozes


class ElevenLabsTTS(ProvedorVoz):
    id = "elevenlabs"
    nome = "ElevenLabs"
    modelo_padrao = "eleven_multilingual_v2"
    voz_padrao = None  # a primeira voz da conta
    modelos = ("eleven_multilingual_v2", "eleven_v3", "eleven_flash_v2_5", "eleven_turbo_v2_5")
    aceita_instrucoes = False

    def __init__(self, modelo=None, chave=None, ao_usar=None):
        super().__init__(modelo, chave, ao_usar)
        self._vozes: list[Voz] | None = None
        self._atualizada = False

    @classmethod
    def catalogo(cls) -> list[Voz]:
        return vozes_em_cache()

    def vozes(self) -> list[Voz]:
        if self._vozes is None:
            self._vozes = vozes_em_cache()
            if not self._vozes:
                self.atualizar()
        return self._vozes

    def atualizar(self) -> list[Voz]:
        """A lista da conta, uma vez por instância; sem rede, fica a guardada."""
        if not self._atualizada and self.chave:
            self._atualizada = True
            try:
                self._vozes = atualizar_vozes(self.chave)
            except ErroProvedor as e:
                print(f"aviso: não consegui listar as vozes da ElevenLabs ({e}); usando a lista guardada", file=sys.stderr)
                self._vozes = self._vozes if self._vozes is not None else vozes_em_cache()
        return self._vozes or []

    def voz_id(self, voz: str) -> str:
        """O voice_id da voz pedida: o próprio id, o nome de uma voz da conta ou a equivalente do mesmo gênero."""
        vozes = self.vozes()
        if not any(voz in (v.nome, v.id) for v in vozes):
            vozes = self.atualizar()  # voz nova na conta? confere uma vez antes de trocar por outra
        for v in vozes:
            if voz == v.id:
                return voz
        nome = self.voz_equivalente(voz)
        return next(v.id for v in vozes if v.nome == nome)

    def sintetizar(self, texto: str, voz: str, perfil: str, estilo: str) -> bytes:
        dados = http.pedir_binario(API.format(voz=self.voz_id(voz)), {"text": texto, "model_id": self.modelo},
                                   {"xi-api-key": self.chave}, provedor=self.id)
        self._usou({}, caracteres=len(texto))
        return para_wav(dados, "audio/L16;rate=24000")
