#!/usr/bin/env python3
"""Gera a imagem de um personagem a partir da ficha e, com a aprovação do usuário, guarda-o na biblioteca.

A ficha (rascunhos/personagens/<slug>/ficha.json, escrita pela skill criar-personagem) traz a aparência, o
comportamento, a voz e o estilo de desenho do personagem. Este script:

  gerar    monta o prompt da ficha de referência (o mesmo das fichas de personagem dos projetos, em
           ferramentas/cenas.py) e gera a imagem com o Gemini (Nano Banana). Cada geração é uma versão nova no
           rascunho (v1.png, v2.png...), com um vN.json ao lado: a ficha daquele momento, o prompt e o modelo.
           Com --base, a imagem de uma versão anterior vai junto, para ajustar o personagem sem sortear outro.
  aprovar  copia a versão aprovada para biblioteca/personagens/<slug>/: imagem, fisico.md, comportamento.md e
           personagem.json (tudo em dados, com o prompt e o modelo da geração). A aparência vem da ficha
           daquela versão, que é o que a imagem mostra; o comportamento e a voz vêm da ficha atual.
  editar   abre um rascunho a partir de um personagem da biblioteca: a ficha e a imagem atual, como v0.

Chave: variável GEMINI_API_KEY (ou GOOGLE_API_KEY), ou o arquivo .env na raiz do projeto.

Uso:
    python3 skills/criar-personagem/scripts/personagem.py gerar rascunhos/personagens/<slug> --simular
    python3 skills/criar-personagem/scripts/personagem.py gerar rascunhos/personagens/<slug>
    python3 skills/criar-personagem/scripts/personagem.py gerar rascunhos/personagens/<slug> --base v2 --ajuste "shorter hair"
    python3 skills/criar-personagem/scripts/personagem.py aprovar rascunhos/personagens/<slug> --versao v2
    python3 skills/criar-personagem/scripts/personagem.py editar <slug>

Só usa a biblioteca padrão.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from datetime import date, datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(RAIZ / "ferramentas"))
sys.path.insert(0, str(RAIZ / "skills" / "produzir-video" / "scripts"))
from cenas import prompt_ficha_personagem  # noqa: E402
from gerar_imagens import MODELO_PADRAO, ErroGeracao, SemCota, arquivo_existente, carregar_chave, chamar_api, inline  # noqa: E402
from narrar import GENERO_VOZES, VOZES_GEMINI  # noqa: E402

BIBLIOTECA = RAIZ / "biblioteca" / "personagens"
SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_PALAVRAS_DESCRICAO = 90  # a descrição entra inteira em todo prompt de cena em que o personagem aparece
APARENCIA = ("referencia_en", "descricao_en", "fisico", "estilo")  # o que a imagem mostra
FISICO = {
    "tipo": "Tipo", "idade": "Idade", "porte": "Porte e silhueta", "rosto": "Rosto", "cabelo": "Cabelo",
    "pele": "Pele e textura", "roupa": "Roupa", "acessorios": "Acessórios", "traco_marcante": "Traço marcante",
    "paleta": "Paleta",
}
COMPORTAMENTO = {
    "personalidade": "Personalidade", "motivacao": "O que quer", "medos_e_defeitos": "Medos e defeitos",
    "jeito_de_falar": "Jeito de falar", "bordoes": "Bordões", "gestos_e_expressoes": "Gestos e expressões",
    "sob_pressao": "Sob pressão", "relacoes": "Relações", "nunca_faz": "O que nunca faz", "historia": "História",
}
GENERO = {"f": "feminina", "m": "masculina"}


# ---------------------------------------------------------------- ficha


def ler_ficha(pasta: Path) -> dict | None:
    caminho = pasta / "ficha.json"
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"erro: {caminho} não existe", file=sys.stderr)
    except json.JSONDecodeError as e:
        print(f"erro: {caminho} não é um JSON válido: {e}", file=sys.stderr)
    return None


def conferir(ficha: dict, completa: bool) -> bool:
    """Imprime os erros e avisos da ficha. Para gerar a imagem basta a aparência; para aprovar, a ficha toda."""
    erros, avisos = [], []
    for campo in ("slug", "nome", "referencia_en", "descricao_en"):
        if not str(ficha.get(campo) or "").strip():
            erros.append(f"{campo} vazio")
    if ficha.get("slug") and not SLUG.match(ficha["slug"]):
        erros.append(f"slug '{ficha['slug']}' inválido (minúsculas, sem acentos, com hífens)")
    if not (ficha.get("estilo") or {}).get("prompt_base"):
        erros.append("estilo.prompt_base vazio")
    if not ficha.get("fisico"):
        erros.append("fisico vazio")
    if completa and not ficha.get("comportamento"):
        erros.append("comportamento vazio")
    ref, desc = str(ficha.get("referencia_en") or ""), str(ficha.get("descricao_en") or "")
    if ref and desc and not desc.lower().startswith(ref.lower()):
        avisos.append(f"descricao_en não começa com a referencia_en '{ref}'")
    if len(desc.split()) > MAX_PALAVRAS_DESCRICAO:
        avisos.append(f"descricao_en com {len(desc.split())} palavras (máximo {MAX_PALAVRAS_DESCRICAO}): ela entra inteira "
                      "em todo prompt de cena")
    voz = (ficha.get("voz") or {}).get("voz")
    if voz and voz not in VOZES_GEMINI:
        avisos.append(f"voz '{voz}' não existe no Gemini TTS (lista: python3 skills/produzir-video/scripts/narrar.py --listar-vozes)")
    for msg in erros:
        print(f"- erro: {msg}")
    for msg in avisos:
        print(f"- aviso: {msg}")
    return not erros


def versoes(pasta: Path) -> list[int]:
    return sorted(int(p.stem[1:]) for p in pasta.glob("v*.json") if re.fullmatch(r"v\d+", p.stem))


def aspas(texto: str) -> str:
    """Texto entre aspas duplas, válido em YAML."""
    return json.dumps(texto, ensure_ascii=False)


def relativo(caminho: Path) -> str:
    caminho = caminho.resolve()
    return str(caminho.relative_to(RAIZ)) if caminho.is_relative_to(RAIZ) else str(caminho)


# ---------------------------------------------------------------- gerar


def gerar(args) -> int:
    pasta = args.rascunho
    ficha = ler_ficha(pasta)
    if ficha is None:
        return 2
    if not conferir(ficha, completa=False):
        return 1
    estilo = ficha["estilo"]
    prompt = prompt_ficha_personagem(ficha["descricao_en"].strip().rstrip("."), estilo["prompt_base"].strip().rstrip("."))
    negativo = estilo.get("negativo_base") or "text"
    texto = f"{prompt}\n\nAvoid: {negativo}. Do not render any text, letters, labels or watermarks."
    partes = [{"text": texto}]

    base = None
    if args.ajuste and not args.base:
        print("erro: --ajuste precisa de --base (a imagem que será ajustada)", file=sys.stderr)
        return 2
    if args.base:
        base = arquivo_existente(pasta, args.base) if re.fullmatch(r"v\d+", args.base) else Path(args.base)
        if base is None or not base.is_file() or base.suffix[1:] not in ("png", "jpg", "webp"):
            print(f"erro: imagem de base '{args.base}' não encontrada (use vN do rascunho ou o caminho de um png, jpg ou webp)",
                  file=sys.stderr)
            return 2
        instrucao = ("The attached image is the current version of this character. Keep the same character: face, body "
                     "proportions, colors and design, and change only what the description below or the adjustment asks for.")
        if args.ajuste:
            instrucao += f" Adjustment: {args.ajuste.strip().rstrip('.')}."
        partes = [{"text": f"{instrucao}\n\n{texto}"}, inline(base)]

    n = (versoes(pasta) or [0])[-1] + 1
    print(f"versão: v{n} · modelo: {args.modelo} · proporção: {args.proporcao}" + (f" · base: {relativo(base)}" if base else ""))
    if args.simular:
        print(f"\n{partes[0]['text']}")
        return 0
    chave = carregar_chave()
    if not chave:
        print("erro: defina GEMINI_API_KEY ou crie .env na raiz do projeto", file=sys.stderr)
        return 2

    inicio = time.time()
    try:
        dados, ext, _ = chamar_api(chave, args.modelo, partes, args.proporcao, args.tamanho)
    except SemCota as e:
        print("erro: o projeto do Google não tem cota para geração de imagem (plano gratuito: limite 0).")
        print(f"  detalhe: {str(e)[:200]}")
        print("  solução: ative o faturamento do projeto em https://aistudio.google.com/apikey (Set up billing)")
        return 3
    except ErroGeracao as e:
        print(f"erro: {e}")
        return 1
    destino = pasta / f"v{n}.{ext}"
    destino.write_bytes(dados)
    registro = {
        "versao": f"v{n}", "arquivo": destino.name, "modelo": args.modelo, "proporcao": args.proporcao,
        "tamanho": args.tamanho, "prompt": prompt, "negativo": negativo, "base": relativo(base) if base else None,
        "ajuste": args.ajuste, "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "segundos": round(time.time() - inicio, 1), "ficha": ficha,
    }
    (pasta / f"v{n}.json").write_text(json.dumps(registro, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"ok: {relativo(destino)} ({registro['segundos']:.0f} s)")
    return 0


# ---------------------------------------------------------------- aprovar


def aprovar(args) -> int:
    pasta = args.rascunho
    ficha = ler_ficha(pasta)
    if ficha is None:
        return 2
    disponiveis = versoes(pasta)
    if not disponiveis:
        print(f"erro: nenhuma versão gerada em {pasta}; rode o comando gerar antes", file=sys.stderr)
        return 2
    if args.versao and not re.fullmatch(r"v?\d+", args.versao):
        print(f"erro: versão '{args.versao}' inválida (use v1, v2...)", file=sys.stderr)
        return 2
    n = int(args.versao.lstrip("v")) if args.versao else disponiveis[-1]
    imagem = arquivo_existente(pasta, f"v{n}")
    if n not in disponiveis or imagem is None:
        print(f"erro: a versão v{n} não existe (há: {', '.join(f'v{i}' for i in disponiveis)})", file=sys.stderr)
        return 2
    registro = json.loads((pasta / f"v{n}.json").read_text(encoding="utf-8"))
    aparencia = {k: registro["ficha"][k] for k in APARENCIA if k in registro["ficha"]}
    final = {**ficha, **aparencia}
    mudou = [k for k in APARENCIA if ficha.get(k) != final.get(k)]
    if mudou:
        print(f"- aviso: a aparência ({', '.join(mudou)}) veio da ficha de v{n}, a da imagem aprovada, e não da ficha atual")
    final["descricao_en"] = str(final.get("descricao_en") or "").strip().rstrip(".")  # a bíblia e os prompts põem o ponto
    if not conferir(final, completa=True):
        return 1

    destino = BIBLIOTECA / final["slug"]
    if destino.exists() and not args.sobrescrever:
        print(f"erro: {relativo(destino)} já existe; confirme com o usuário e repita com --sobrescrever", file=sys.stderr)
        return 1
    destino.mkdir(parents=True, exist_ok=True)
    for antiga in destino.glob("imagem.*"):
        antiga.unlink()
    nome_imagem = f"imagem{imagem.suffix}"
    shutil.copy2(imagem, destino / nome_imagem)

    geracao = {k: registro.get(k) for k in ("modelo", "proporcao", "tamanho", "prompt", "negativo", "base", "ajuste", "gerado_em")}
    geracao.update(versao=f"v{n}", rascunho=relativo(pasta), aprovado_em=date.today().isoformat())
    dados = {**final, "imagem": nome_imagem, "geracao": geracao}
    (destino / "personagem.json").write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    (destino / "fisico.md").write_text(fisico_md(dados), encoding="utf-8")
    (destino / "comportamento.md").write_text(comportamento_md(dados), encoding="utf-8")
    print(f"aprovado: v{n} → {relativo(destino)}/ ({nome_imagem}, fisico.md, comportamento.md, personagem.json)")
    return 0


# ---------------------------------------------------------------- editar


def editar(args) -> int:
    origem = BIBLIOTECA / args.slug
    try:
        dados = json.loads((origem / "personagem.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"erro: {relativo(origem)}/personagem.json não existe", file=sys.stderr)
        return 2
    pasta = RAIZ / "rascunhos" / "personagens" / args.slug
    if (pasta / "ficha.json").exists():
        print(f"erro: já há um rascunho em {relativo(pasta)}; continue nele ou apague-o antes", file=sys.stderr)
        return 1
    imagem = origem / dados.get("imagem", "")
    if not imagem.is_file():
        print(f"erro: a imagem {relativo(imagem)} não existe", file=sys.stderr)
        return 2
    ficha = {k: v for k, v in dados.items() if k not in ("imagem", "geracao")}
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / "ficha.json").write_text(json.dumps(ficha, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(imagem, pasta / f"v0{imagem.suffix}")
    registro = {"versao": "v0", "arquivo": f"v0{imagem.suffix}", **(dados.get("geracao") or {}), "ficha": ficha}
    (pasta / "v0.json").write_text(json.dumps(registro, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"rascunho: {relativo(pasta)}/ (ficha.json e a imagem atual como v0)")
    return 0


# ---------------------------------------------------------------- documentos


def secoes(campos: dict, rotulos: dict[str, str]) -> list[str]:
    """Um parágrafo por campo: os conhecidos na ordem dos rótulos, depois os outros na ordem da ficha."""
    linhas = []
    for chave in [*(k for k in rotulos if k in campos), *(k for k in campos if k not in rotulos)]:
        valor = campos[chave]
        if not valor:
            continue
        rotulo = rotulos.get(chave) or chave.replace("_", " ").capitalize()
        if isinstance(valor, list):
            linhas += [f"**{rotulo}.**", "", *(f"- {item}" for item in valor), ""]
        else:
            linhas += [f"**{rotulo}.** {valor}", ""]
    return linhas


def rodape() -> list[str]:
    return ["---", "",
            "Gerado por `skills/criar-personagem/scripts/personagem.py` a partir de `personagem.json`. Para mudar o "
            "personagem, use a skill `criar-personagem`.", ""]


def fisico_md(d: dict) -> str:
    g, estilo = d["geracao"], d["estilo"]
    linhas = [f"# {d['nome']}: descrição física", "", f"![{d['nome']}]({d['imagem']})", ""]
    if d.get("resumo"):
        linhas += [d["resumo"], ""]
    linhas += secoes(d["fisico"], FISICO)
    linhas += ["## Estilo da imagem", "", f"**{estilo.get('nome') or 'Sem nome'}.** {estilo.get('descricao') or ''}".rstrip(), ""]
    linhas += [
        "## Descrição para os prompts (inglês)", "",
        f"Vai, palavra por palavra, em `personagens[].descricao` da bíblia visual de um projeto, com a `referencia_en` "
        f"\"{d['referencia_en']}\", para o personagem sair igual em todas as cenas.", "",
        f"> {d['descricao_en']}", "",
        "## Geração", "",
        f"- **Modelo:** {g['modelo']} · **proporção:** {g['proporcao']}" + (f" · **tamanho:** {g['tamanho']}" if g.get("tamanho") else ""),
        f"- **Versão aprovada:** {g['versao']} de `{g['rascunho']}`, gerada em {g['gerado_em']}, aprovada em {g['aprovado_em']}",
    ]
    if g.get("base"):
        linhas.append(f"- **Feita a partir de:** `{g['base']}`" + (f", com o ajuste \"{g['ajuste']}\"" if g.get("ajuste") else ""))
    linhas += [f"- **Negativo:** {g['negativo']}", "", "Prompt:", "", "```text", g["prompt"], "```", ""]
    return "\n".join(linhas + rodape())


def comportamento_md(d: dict) -> str:
    linhas = [f"# {d['nome']}: descrição comportamental", ""]
    if d.get("resumo"):
        linhas += [d["resumo"], ""]
    linhas += secoes(d["comportamento"], COMPORTAMENTO)
    voz = d.get("voz") or {}
    if voz:
        linhas += ["## Voz", ""]
        if voz.get("voz"):
            info = ", ".join(x for x in (GENERO.get(GENERO_VOZES.get(voz["voz"], "")), VOZES_GEMINI.get(voz["voz"])) if x)
            linhas.append(f"- **Voz (Gemini TTS):** {voz['voz']}" + (f" ({info})" if info else ""))
        if voz.get("estilo"):
            linhas.append(f"- **Como fala:** {voz['estilo']}")
        if voz.get("cor"):
            linhas.append(f"- **Cor na legenda:** {voz['cor']}")
        linhas.append("")
    item = [f"  - nome: {aspas(d['nome'])}"]
    item += [f"    {campo}: {aspas(voz[campo])}" for campo in ("voz", "estilo") if voz.get(campo)]
    item.append(f"    descricao: {aspas(d['descricao_en'])}")
    if voz.get("cor"):
        item.append(f"    cor: {aspas(voz['cor'])}")
    linhas += ["## Num pedido", "", "Para o personagem falar com a voz dele num vídeo, em `pedidos/<nome>.yaml`:", "",
               "```yaml", "personagens:", *item, "```", ""]
    return "\n".join(linhas + rodape())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="comando", required=True)
    g = sub.add_parser("gerar", help="gera uma versão nova da imagem a partir da ficha")
    g.add_argument("rascunho", type=Path, help="pasta do rascunho (rascunhos/personagens/<slug>), com ficha.json")
    g.add_argument("--modelo", default=MODELO_PADRAO, help=f"modelo de imagem (padrão {MODELO_PADRAO}; mais barato: "
                                                           "gemini-3.1-flash-lite-image; melhor: gemini-3-pro-image)")
    g.add_argument("--proporcao", default="1:1", help="proporção da imagem (padrão 1:1, a das fichas dos projetos)")
    g.add_argument("--tamanho", choices=["1K", "2K", "4K"], help="resolução (só modelos Gemini 3; padrão do modelo)")
    g.add_argument("--base", help="versão do rascunho (v2) ou caminho de imagem que vai junto, para ajustar em vez de sortear")
    g.add_argument("--ajuste", help="com --base: o que mudar na imagem, em inglês (ex.: \"shorter hair, add a scar\")")
    g.add_argument("--simular", action="store_true", help="só mostra a versão e o prompt, sem chamar a API")
    a = sub.add_parser("aprovar", help="guarda a versão aprovada em biblioteca/personagens/<slug>/")
    a.add_argument("rascunho", type=Path, help="pasta do rascunho (rascunhos/personagens/<slug>)")
    a.add_argument("--versao", help="versão aprovada (ex.: v2). Padrão: a última")
    a.add_argument("--sobrescrever", action="store_true", help="substitui um personagem que já está na biblioteca")
    e = sub.add_parser("editar", help="abre um rascunho a partir de um personagem da biblioteca")
    e.add_argument("slug", help="slug do personagem em biblioteca/personagens/")
    args = ap.parse_args()
    return {"gerar": gerar, "aprovar": aprovar, "editar": editar}[args.comando](args)


if __name__ == "__main__":
    sys.exit(main())
