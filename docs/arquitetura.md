# Arquitetura do servidor

O sistema roda num servidor sem o Claude Code: só com as chaves de API dos provedores (Anthropic, OpenAI,
Gemini, ElevenLabs, YouTube). Este documento é o contrato entre as camadas: nomes de módulos, assinaturas,
esquema do banco, variáveis de ambiente e API HTTP. Mudou um contrato? Mude aqui também.

## Princípios

1. **A qualidade mora nos artefatos e nos laços de correção, não no orquestrador.** Os agentes, os prompts, os
   formatos (`biblioteca/formato-*.md`), a rubrica e os scripts determinísticos são os mesmos do pipeline
   original; o que antes uma sessão do Claude Code fazia "por julgamento" (revisar a folha de contato, refazer
   blocos que a voz errou, devolver avisos ao diretor visual...) virou laço explícito em Python
   (`nucleo/pipeline/fluxos.py`).
2. **Os arquivos de `projetos/<slug>/` continuam sendo o formato de trabalho.** Agentes e scripts (ffmpeg,
   whisper) precisam de arquivos locais. A fonte da verdade é o **armazenamento de objetos** (MinIO por padrão,
   AWS S3 quando configurado) com o **índice e o conteúdo de texto no banco** (Postgres). Cada processo tem uma
   cópia local (o *espaço de trabalho*), sincronizada por hash antes e depois de cada execução.
3. **Tudo que é estado vai para o banco**: projetos, formulários, sugestões, execuções e o log de cada uma,
   custos de cada chamada, configuração de modelos, chaves de API (cifradas), contextos, lições de roteiro,
   desempenho dos vídeos e o índice de todos os arquivos (com o texto dos arquivos de texto).
4. **Provedores atrás de interfaces.** LLM, voz, imagem e vídeo têm uma classe base cada; trocar de provedor numa etapa
   é configuração, não código. O prompt é do serviço, não do provedor: os textos que vão para os modelos são
   os mesmos byte a byte em qualquer provedor (o adaptador só traduz o formato da API).
5. **Agentes são dados.** Cada agente é um arquivo `agentes/<nome>.md` (frontmatter + instruções). O motor
   oferece ferramentas tipadas, valida as saídas estruturadas por JSON Schema e devolve o erro ao modelo.
6. **Processos sem estado em memória.** O servidor web só enfileira; quem roda é o trabalhador
   (`python -m nucleo.trabalhador`), que pega execuções do banco (`FOR UPDATE SKIP LOCKED`), manda batimento e
   obedece ao pedido de cancelamento gravado no banco. Dá para ter vários trabalhadores e várias réplicas web.

## Estrutura

```
agentes/                      prompts dos agentes (um .md por agente) e _contexto.md (contexto comum enxuto)
biblioteca/                   técnicas, rubrica, formatos (estáticos, vêm na imagem) e dados que mudam (sincronizados)
ferramentas/                  scripts determinísticos (CLI) — continuam rodando como subprocesso
  producao/                   narrar.py, gerar_imagens.py, buscar_imagens.py, animar_imagens.py, alinhar.py,
                              montar_video.py, combinar.py, dublar.py
  pedido.py brief.py cenas.py medir_roteiro.py conferir_texto.py custos.py personagem.py
  animacao.py efeitos.py      só biblioteca padrão: quais cenas animam e o texto do pedido; a biblioteca de efeitos
  youtube_trends.py fetch_youtube.py desempenho.py migrar.py
  chaves.py llm.py            compatibilidade: reexportam nucleo.chaves e nucleo.provedores.llm
nucleo/
  config.py                   Configuracao (variáveis de ambiente)
  seguranca.py                cifra das chaves de API (Fernet)
  chaves.py                   chaves dos provedores: ambiente > banco (cifrada) > .env (legado, só leitura)
  rotas.py                    configuração de modelos por etapa/agente/mídia e resolução da rota de cada agente
  espaco.py                   espaço de trabalho local ⇄ armazenamento + índice no banco
  roteiro.py                  parser/montador do roteiro para o editor (antigo web/roteiro.py)
  tecnicas.py                 ids novos de biblioteca/tecnicas.json que vêm com o código, acrescentados no arranque
  paralelo.py                 chamadas bloqueantes aos provedores em paralelo (asyncio), com limite e dependências
  banco/                      SQLAlchemy 2: modelos.py, sessao.py, repositorios.py, migracoes/ (Alembic)
  armazenamento/              base.py (ABC), s3.py (MinIO e AWS S3), local.py
  provedores/
    erros.py http.py uso.py   comuns
    llm/                      base.py, anthropic.py, openai.py, gemini.py
    voz/                      base.py, gemini.py, openai.py, elevenlabs.py, espeak.py
    imagem/                   base.py, gemini.py, openai.py
    video/                    base.py, gemini.py (Gemini Omni Flash: anima a imagem de uma cena)
  agentes/                    registro.py, ferramentas.py, motor.py
  pipeline/                   etapas.py, execucao.py, fluxos.py, verificacoes.py
  trabalhador.py              python -m nucleo.trabalhador
web/
  servidor.py                 app FastAPI (lifespan, autenticação, estáticos)
  api/                        roteadores: projetos, execucoes, configuracoes, contextos, biblioteca, personagens,
                              referencias, desempenho, arquivos
  contextos.py                contextos no banco + anexos no armazenamento
  estatico/                   SPA (app.js, estilo.css, index.html)
deploy/                       entrypoint.sh
docker-compose.yml Dockerfile pyproject.toml .env.example alembic.ini
tests/                        pytest
```

O que sai: o Claude Code como executor (`web/claude_code.py`, modos `claude-code*`), `.claude/agents/` (vira
`agentes/`) e `skills/` (os scripts vão para `ferramentas/`; o conhecimento dos `SKILL.md` vai para os prompts
dos agentes e para os fluxos).

`RAIZ` (raiz do repositório, `/app` no contêiner) é ao mesmo tempo o lugar do código e o espaço de trabalho:
`RAIZ/projetos`, `RAIZ/pedidos`, `RAIZ/biblioteca`, `RAIZ/rascunhos` são cópias locais sincronizadas. Os
scripts são chamados com `cwd=RAIZ` e `PYTHONPATH=RAIZ`, então `import nucleo...` funciona em qualquer um.

## Configuração (`nucleo/config.py`)

```python
@dataclass(frozen=True)
class Configuracao:
    raiz: Path
    banco_url: str               # DATABASE_URL; padrão sqlite:///<raiz>/dados/app.sqlite3
    armazenamento: str           # "minio" | "s3" | "local"
    bucket: str
    s3_endpoint: str | None      # MinIO: http://minio:9000; S3: None (ou endpoint compatível)
    s3_regiao: str
    s3_chave: str | None
    s3_segredo: str | None
    prefixo: str                 # prefixo opcional das chaves de objeto ("" por padrão)
    pasta_objetos_local: Path    # só para armazenamento "local"
    segredo_app: str | None      # APP_SEGREDO (chave Fernet); sem ele, dados/segredo.key é gerado
    usuario: str | None          # APP_USUARIO / APP_SENHA: autenticação básica (opcional)
    senha: str | None
    concorrencia: int            # TRABALHADOR_CONCORRENCIA (padrão 2)
    trabalhador_embutido: bool   # TRABALHADOR_EMBUTIDO=1: o servidor web roda o trabalhador numa thread
    whisper_modelo: str          # WHISPER_MODELO (padrão "small"; a configuração no banco tem prioridade)

def config() -> Configuracao       # lru_cache; config.cache_clear() nos testes
```

Escolha do armazenamento (`ARMAZENAMENTO`, ou automática):

| Situação | Armazenamento |
|---|---|
| `ARMAZENAMENTO=s3`, ou `AWS_S3_BUCKET` definido (com `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` ou papel IAM) | AWS S3 (`AWS_REGION`, `AWS_S3_ENDPOINT` opcional) |
| `ARMAZENAMENTO=minio`, ou `MINIO_ENDPOINT` definido (padrão no docker-compose) | MinIO (`MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_BUCKET`, padrão `videos`) |
| nada disso (desenvolvimento) | `local`: objetos em `dados/objetos/` |

## Banco (`nucleo/banco`)

SQLAlchemy 2 (`DeclarativeBase`, `Mapped`), Postgres em produção (`postgresql+psycopg://`), SQLite em
desenvolvimento e testes. Migrações com Alembic (`nucleo/banco/migracoes/`); o `deploy/entrypoint.sh` roda
`alembic upgrade head`, e `sessao.iniciar()` faz o mesmo (ou `create_all` no SQLite de teste). Colunas JSON
usam `sqlalchemy.JSON` (vira `jsonb` no Postgres via `with_variant`). Datas em UTC com fuso.

| Tabela | Colunas | Observação |
|---|---|---|
| `projetos` | `slug` PK, `titulo`, `etapa`, `formulario` JSON, `sugestoes` JSON?, `origem` (`pipeline`\|`roteiro-importado`), `criado_em`, `atualizado_em` | |
| `execucoes` | `id` PK, `projeto_slug` FK? , `etapa`, `status` (`pendente`\|`rodando`\|`ok`\|`erro`\|`cancelada`\|`interrompida`), `parametros` JSON, `rota` JSON (provedor/modelo de cada agente usado), `resultado` texto, `estruturado` JSON?, `avisos` JSON (lista de textos), `custo_usd` (soma dos custos), `trabalhador`, `cancelar_solicitado` bool, `criada_em`, `iniciada_em`, `terminada_em`, `batimento_em` | índice único parcial: uma execução `pendente`/`rodando` por `projeto_slug`; execuções sem projeto (personagem, referência) usam `chave_exclusiva` (texto, único entre as ativas) |
| `execucao_logs` | `id` PK (bigint), `execucao_id` FK, `texto`, `criado_em` | uma linha por evento; o cursor do polling é o `id` |
| `custos` | `id`, `execucao_id`?, `projeto_slug`?, `servico` (`llm`\|`busca`\|`imagem`\|`animacao`\|`narracao`), `provedor`, `modelo`, `agente`?, `item`?, `uso` JSON, `extra` JSON, `custo_usd`? (null = sem preço), `estimado` bool, `quando` | toda chamada paga |
| `arquivos` | `id`, `caminho` único (chave lógica relativa à RAIZ, ex.: `projetos/x/05-roteiro-v2.md`), `escopo` (`projeto`\|`biblioteca`\|`pedido`\|`contexto`\|`rascunho`), `projeto_slug`?, `tamanho`, `sha256`, `mime`, `texto`? (conteúdo dos arquivos de texto até 2 MB), `modificado_em` (mtime original), `versao` int, `atualizado_em`, `apagado_em`? | índice de tudo que está no armazenamento |
| `configuracoes` | `chave` PK, `valor` JSON, `atualizado_em` | `modelos` (ver Rotas), `modelos_disponiveis` (lista por provedor), `testes` (resultado do teste de cada chave), `vozes_elevenlabs` |
| `chaves_api` | `provedor` PK, `valor_cifrado`, `mascara`, `atualizado_em` | |
| `contextos` | `id` PK (slug), `nome`, `texto`, `anexos` JSON (`[{arquivo, tipo, descricao}]`), `criado_em`, `atualizado_em` | anexos no armazenamento em `pedidos/contextos/<id>/` |
| `licoes_roteiro` | `id`, `secao`, `regra`, `erro`, `correcao`, `projeto_slug`, `versao`, `criada_em` | `biblioteca/licoes-roteiro.md` é gerado a partir daqui |
| `desempenho` | `id`, `projeto_slug`, `video_id`, `janela` (`48h`\|`7d`\|`28d`), `formato`, `tecnicas` JSON, `tipo_gancho`, `metricas` JSON (`ctr`, `retencao_30s`, `retencao_media`, `viu_vs_passou`, `views`, `inscritos_ganhos`), `extra` JSON (o resto do registro, como `gatilhos`, `titulo` e `roteiro`), `obs`, `registrado_em` | único (`video_id`, `janela`); `biblioteca/aprendizados.md` é gerado a partir daqui (por técnica, tipo de gancho e gatilho) |

API do pacote (`nucleo/banco/__init__.py` reexporta):

```python
def iniciar() -> None                         # migra/cria o esquema; idempotente
@contextmanager
def sessao() -> Iterator[Session]             # commit no fim, rollback na exceção
@contextmanager
def trava(nome: str) -> Iterator[None]        # trava entre processos: pg_advisory_lock no Postgres, arquivo+thread no SQLite
def agora() -> datetime                       # UTC
```

`nucleo/banco/repositorios.py` (funções simples, devolvem dicts — a camada web não vê ORM):

```python
# projetos
projeto(slug) -> dict | None;  projetos() -> list[dict];  criar_projeto(slug, titulo, formulario, etapa="configurar", origem="pipeline") -> dict
atualizar_projeto(slug, **campos) -> None;  apagar_projeto(slug) -> None
# execuções
criar_execucao(slug: str | None, etapa: str, parametros: dict, chave_exclusiva: str | None = None) -> int   # ErroOcupado se já há uma ativa
execucao(id_) -> dict | None;  ultima_execucao(slug, etapa) -> dict | None;  execucoes_ativas(slug=None) -> list[dict]
pegar_proxima(trabalhador: str) -> dict | None          # pendente -> rodando, FOR UPDATE SKIP LOCKED
bater(id_) -> bool                                      # atualiza batimento_em; devolve cancelar_solicitado
pedir_cancelamento(id_) -> bool;  terminar_execucao(id_, status, resultado="", estruturado=None, avisos=None) -> None
marcar_interrompidas(sem_batimento_ha_s=120) -> int
anexar_log(id_, texto) -> None;  log_desde(id_, cursor: int) -> tuple[str, int]   # (texto novo, novo cursor)
# custos
registrar_custo(servico, provedor, modelo, uso, custo_usd, *, execucao_id=None, projeto_slug=None, agente=None, item=None, extra=None, estimado=False) -> None
custos_do_projeto(slug) -> dict      # {"total_usd", "por_etapa": [{etapa, execucoes, usd}], "por_servico": [...], "sem_preco": [modelos]}
# configurações e chaves
ler_config(chave, padrao=None);  gravar_config(chave, valor)
ler_chave(provedor) -> str | None (decifrada);  gravar_chave(provedor, valor | None);  mascara_chave(provedor) -> str | None
# arquivos (usado por nucleo.espaco)
indice(prefixo) -> dict[str, dict];  registrar_arquivo(caminho, **campos);  marcar_apagado(caminho)
texto_do_arquivo(caminho) -> str | None
# contextos, lições, desempenho
contextos() / contexto(id_) / salvar_contexto(id_, **campos) / apagar_contexto(id_)
acrescentar_licoes(lista: list[dict]) -> int;  licoes() -> list[dict]
registrar_desempenho(dados: dict) -> dict;  desempenhos(janela=None) -> list[dict]
class ErroOcupado(Exception)
```

## Armazenamento (`nucleo/armazenamento`)

```python
@dataclass(frozen=True)
class Objeto: chave: str; tamanho: int; etag: str | None; modificado: datetime | None

class Armazenamento(ABC):
    tipo: ClassVar[str]                                   # "minio" | "s3" | "local"
    def garantir_bucket(self) -> None: ...
    def enviar(self, chave: str, origem: Path, mime: str | None = None, metadados: dict | None = None) -> None: ...
    def enviar_bytes(self, chave: str, dados: bytes, mime: str | None = None) -> None: ...
    def baixar(self, chave: str, destino: Path) -> None: ...      # grava atômico (tmp + replace)
    def ler_bytes(self, chave: str) -> bytes: ...
    def existe(self, chave: str) -> bool: ...
    def apagar(self, chave: str) -> None: ...
    def listar(self, prefixo: str) -> Iterator[Objeto]: ...
    def url_assinada(self, chave: str, expira_s: int = 3600) -> str | None: ...   # None no local

def armazenamento() -> Armazenamento      # singleton pela config
```

`S3Armazenamento` usa boto3 (MinIO com `endpoint_url` e endereçamento por caminho; S3 com região). Uploads
grandes com `upload_file` (multipart). A chave do objeto é `prefixo + caminho` (o caminho lógico do arquivo).

## Espaço de trabalho (`nucleo/espaco.py`)

```python
SINCRONIZADOS = ("projetos/", "pedidos/", "rascunhos/", "biblioteca/esqueletos/", "biblioteca/personagens/",
                 "biblioteca/musicas/", "biblioteca/efeitos/", "biblioteca/vozes/", "biblioteca/tecnicas.json",
                 "biblioteca/canal.json", "biblioteca/licoes-roteiro.md", "biblioteca/aprendizados.md")
# Os outros arquivos de biblioteca/ (rubrica, formatos, preços) vêm na imagem, com o código.
IGNORADOS = ("**/montagem-*/**", "**/__pycache__/**", "**/*.tmp", "**/.capa.jpg", "**/.DS_Store")
# biblioteca/tecnicas.json vem na imagem e também muda no armazenamento. No arranque, o servidor lê o da imagem
# antes de inicializar() e depois acrescenta ao do armazenamento os ids que faltam (nucleo/tecnicas.py: da_imagem,
# mesclar, completar), sem trocar nem apagar nenhum: assim os gatilhos e tipos de gancho novos do código chegam.

def inicializar() -> None          # no arranque: envia o que existe localmente e falta no armazenamento (semente), depois baixa tudo
def baixar(prefixo: str) -> int    # remoto -> local, pelo índice do banco; devolve quantos arquivos baixou
def enviar(prefixo: str) -> int    # local -> remoto: só o que mudou desde a última sincronização (sha256); marca os apagados
def garantir(caminho: str) -> Path # um arquivo presente no local (baixa se faltar ou estiver velho); FileNotFoundError se não existir
def gravar_texto(caminho: str, texto: str) -> Path      # grava local e envia (escritas da web)
def gravar_bytes(caminho: str, dados: bytes) -> Path
def apagar(caminho_ou_prefixo: str) -> int
def copiar(origem_prefixo: str, destino_prefixo: str, transformar_texto: Callable[[str], str] | None = None) -> int   # fork
def importar_tudo() -> dict        # migração de uma instalação local: envia projetos/, pedidos/, biblioteca/ e rascunhos/
```

Regras:
- O remoto é a fonte da verdade. Um arquivo local só é enviado quando mudou desde a última sincronização deste
  processo (índice local em `RAIZ/.espaco/indice.json`: caminho → sha256, tamanho, mtime_ns, versão remota).
  Arquivo que existe no remoto e não no local é baixado; remoto mais novo que o local (versão) sobrescreve.
- Na baixa, o `mtime` original (`arquivos.modificado_em`) é restaurado com `os.utime` (as etapas ainda usam
  data como desempate em projetos antigos).
- Arquivos de texto (`.md .json .jsonl .yaml .yml .txt .ass .srt`) até 2 MB têm o conteúdo copiado para
  `arquivos.texto`: toda informação textual do pipeline fica consultável no banco.
- Um processo nunca envia um prefixo de projeto enquanto outro roda uma execução nele: a exclusividade vem da
  execução ativa única por projeto. A web só escreve em projeto sem execução ativa (senão 409).

## Chaves (`nucleo/chaves.py`)

Mesma API do antigo `ferramentas/chaves.py`: `PROVEDORES` (anthropic, openai, gemini, elevenlabs, youtube, com
`nome`, `variavel`, `aceita`, `usos`, `painel`), `ler(provedor)`, `origem(provedor)` → `"ambiente"` |
`"banco"` | `"arquivo"` | `None`, `mascarar(valor)`, `gravar(provedor, valor | None)` (no banco, cifrada;
recusa se a origem for o ambiente), `ambiente(excluir=())` (as chaves como variáveis, para os scripts de
produção). Ordem de leitura: variável de ambiente > banco > `.env` legado.

## Provedores (`nucleo/provedores`)

Comuns (já escritos): `erros.py` (`ErroProvedor`, `SemCota`, `Bloqueado`, `SemChave`), `http.py`
(`pedir_json`, `pedir_binario` com novas tentativas em 429/5xx e o `retry in Ns` do Gemini), `uso.py`
(`Uso`, `uso_vazio`, `somar_uso`). Registro em `nucleo/provedores/__init__.py`:

```python
def llm(provedor: str) -> ProvedorLLM
def voz(fornecedor: str, modelo: str | None = None, ao_usar=None) -> ProvedorVoz
def imagem(provedor: str, modelo: str | None = None, ao_usar=None) -> ProvedorImagem
def video(provedor: str, modelo: str | None = None, ao_usar=None) -> ProvedorVideo
LLM = ("anthropic", "openai", "gemini");  VOZ = ("gemini", "openai", "elevenlabs", "espeak");  IMAGEM = ("gemini", "openai")
VIDEO = ("gemini",)
```

### LLM (`llm/base.py`)

```python
@dataclass class Chamada: id: str; nome: str; args: dict
@dataclass class Resultado: id: str; nome: str; texto: str; imagens: list[tuple[str, bytes]] = []
@dataclass class Passo: texto: str; chamadas: list[Chamada]; uso: dict; cortado: bool = False

class Conversa(ABC):
    def usuario(self, texto: str, imagens: Sequence[tuple[str, bytes]] = ()) -> None
    def passo(self) -> Passo
    def resultados(self, lista: list[Resultado]) -> None

class ProvedorLLM(ABC):
    id: ClassVar[str]; nome: ClassVar[str]
    def __init__(self, chave: str)
    def conversa(self, modelo: str, sistema: str, ferramentas: list[dict], max_saida: int = 16000) -> Conversa
    def buscar_na_web(self, modelo: str, consulta: str) -> tuple[str, dict]     # (texto com fontes, uso)
    def listar_modelos(self) -> list[str]
    def testar(self) -> str                                                     # mensagem curta; levanta ErroProvedor
```

Ferramentas declaradas como `{"nome", "descricao", "parametros" (JSON Schema)}`. Uso normalizado:
`{"entrada", "saida", "cache_leitura", "cache_escrita", "buscas"}`. As implementações são as de
`ferramentas/llm.py` (Messages com cache de prompt, Responses com `previous_response_id`, generateContent com as
assinaturas de raciocínio intactas), que vira um módulo de compatibilidade.

### Voz (`voz/base.py`)

```python
@dataclass(frozen=True) class Voz: nome: str; genero: str; descricao: str = ""; id: str | None = None; amostra_url: str | None = None

class ProvedorVoz(ABC):
    id: ClassVar[str]; nome: ClassVar[str]; modelo_padrao: ClassVar[str]; voz_padrao: ClassVar[str]
    modelos: ClassVar[tuple[str, ...]]
    deterministico: ClassVar[bool] = False       # espeak: sem verificação nem novas tentativas
    aceita_instrucoes: ClassVar[bool] = True     # perfil/estilo (ElevenLabs e espeak não)
    precisa_chave: ClassVar[bool] = True
    def __init__(self, modelo: str | None = None, chave: str | None = None, ao_usar: Callable[..., None] | None = None)
    def vozes(self) -> list[Voz]                 # sem mutar atributo de classe (ElevenLabs: cache no banco)
    def voz_equivalente(self, voz: str) -> str   # mesma regra de narrar.py (gênero + sha1), em TODOS os provedores
    @abstractmethod
    def sintetizar(self, texto: str, voz: str, perfil: str, estilo: str) -> bytes   # WAV PCM16 mono 24 kHz
```

O prompt do Gemini TTS (`# PERFIL DE ÁUDIO / ## NOTAS DE DIREÇÃO / ## TRANSCRIÇÃO`), o hash de cache
(`hash_pedido(fornecedor, modelo, voz, perfil, estilo, texto)`), a verificação por transcrição, a regra de
velocidade [0,8; 1,35] e o formato de `09-narracao/blocos.json` não mudam.

### Imagem (`imagem/base.py`)

```python
@dataclass(frozen=True) class Referencia: dados: bytes; mime: str
@dataclass(frozen=True) class PedidoImagem: texto: str; referencias: tuple[Referencia, ...] = (); proporcao: str = "16:9"; tamanho: str | None = None
@dataclass(frozen=True) class ImagemGerada: dados: bytes; extensao: str; uso: dict

class ProvedorImagem(ABC):
    id: ClassVar[str]; nome: ClassVar[str]; modelo_padrao: ClassVar[str]
    modelos_por_qualidade: ClassVar[dict[str, str]]     # rascunho / padrao / maxima
    max_referencias: ClassVar[int] = 6
    def __init__(self, modelo: str | None = None, chave: str | None = None, ao_usar: Callable[..., None] | None = None)
    @abstractmethod
    def gerar(self, pedido: PedidoImagem) -> ImagemGerada      # SemCota / Bloqueado / ErroProvedor
```

O texto do pedido (instrução das referências, `Avoid: … Do not render any text…`) é montado em
`ferramentas/producao/gerar_imagens.py`, igual ao original; o adaptador Gemini manda
`[{"text": texto}, *inlineData(refs)]`; o da OpenAI (gpt-image) manda as referências em `images/edits` e
ajusta a proporção ao tamanho suportado mais próximo, pedindo a composição para a proporção do vídeo.

As chamadas de `gerar_imagens.py` rodam em paralelo por `nucleo/paralelo.py` (asyncio: cada chamada numa thread,
um semáforo com o limite de `imagem.paralelo` da configuração, padrão 6): cada cena começa assim que as fichas de
referência dela ficam prontas, e o manifesto é gravado a cada imagem pronta.

### Vídeo (`video/base.py`)

```python
@dataclass(frozen=True) class PedidoVideo: texto: str; imagem: Referencia; proporcao: str = "16:9"; resolucao: str = "720p"; segundos: int = 6; ultimo_quadro: Referencia | None = None
@dataclass(frozen=True) class VideoGerado: dados: bytes; uso: dict      # mp4

class ProvedorVideo(ABC):
    id: ClassVar[str]; nome: ClassVar[str]; modelo_padrao: ClassVar[str]; modelos: ClassVar[tuple[str, ...]]
    resolucoes: ClassVar[tuple[str, ...]]; resolucao_padrao: ClassVar[str] = "720p"
    proporcoes: ClassVar[tuple[str, ...]]                # as que o modelo gera (Omni Flash: 16:9 e 9:16)
    @abstractmethod
    def animar(self, pedido: PedidoVideo) -> VideoGerado   # SemCota / Bloqueado / ErroProvedor
```

Anima a imagem de uma cena (o primeiro quadro) num clipe curto, quando o pedido liga `visual.animacao` (`destaques`
ou `todas`). O texto do pedido (o movimento, o que não pode mudar, sem fala nem música: a API não tem campo de
duração, de prompt negativo nem de áudio) é montado em `ferramentas/animacao.py`. O adaptador Gemini usa a
Interactions API (`POST /v1beta/interactions`, `generation_config.video_config.task: image_to_video`,
`response_format: {type: video, aspect_ratio, resolution, delivery: uri}`, `store: true`, que a entrega por URI exige)
e baixa o vídeo da Files API; depois apaga o arquivo e a interação guardada. Os Veo 3.1
em prévia saíram do ar em 22/10/2026; o Omni Flash é o substituto indicado pelo Google.

`ferramentas/producao/animar_imagens.py` roda depois do alinhamento (cada clipe pedido com a duração real da cena,
de 3 a 10 s), em paralelo (`animacao.paralelo`, padrão 4), e grava `09-animacoes/<id>.mp4` e
`09-animacoes/manifesto.json` (`status` ok | bloqueado | erro, `pedido`, `imagem` = sha1 da imagem de origem,
`duracao_s`). O clipe vale enquanto a imagem da cena for a mesma; o pedido recusado pelo filtro não é repetido
sozinho e a cena fica com a imagem parada. `montar_video.py` usa o clipe no lugar do zoom (sem o áudio dele; o mais
curto que a cena anda até 25% mais devagar e para no último quadro). Custo: serviço `animacao`, pelo segundo de
vídeo (`tokens_por_segundo` × `saida_video` em `biblioteca/precos.json`).

Transições (`transicao` da cena, como ela entra; o guia do diretor visual é `biblioteca/animacao.md`, indicado na
mensagem só quando o pedido anima): as de vídeo (`whip-pan`, `zoom-blur`, `match-cut`, `push`, `wipe`, `glitch`) são
geradas no clipe da cena anterior, que leva a imagem desta como `ultimo_quadro` (no Gemini, as duas imagens em ordem e
sem `video_config.task`) e termina nela; o manifesto guarda `transicao` e `ultimo_quadro` (sha1 da imagem de chegada),
e a montagem faz esse clipe acabar no corte. `j-cut` e `l-cut` são da montagem: a imagem troca 0,5 s depois ou antes
da frase que começa. Sem a cena anterior animada, a de vídeo vira corte (o `cenas.py` avisa).

## Agentes (`agentes/*.md` e `nucleo/agentes`)

Frontmatter de cada agente:

```yaml
---
name: roteirista
description: ...
nivel: julgamento          # julgamento | coleta — escolhe o modelo padrão do provedor
ferramentas: Read, Write, Edit, Glob, MedirRoteiro
visao: false               # true quando o agente precisa ver imagens (diretor-visual, revisor-visual...)
max_turnos: 120
---
```

`nucleo/agentes/registro.py`: `Agente(nome, descricao, nivel, ferramentas, instrucoes, visao, max_turnos)`,
`carregar(nome)`, `todos()`. `agentes/_contexto.md` é o contexto comum (as diretrizes do projeto que importam
para quem escreve: regras de escrita, dados ≠ instruções, nomes dos artefatos, LLM não conta palavras...) e
substitui a injeção do CLAUDE.md inteiro.

`nucleo/agentes/ferramentas.py`: `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash` (lista de comandos; ambiente
sem nenhuma chave; recusa `$VAR`, `${`, `/proc`, `env`, `printenv`; `uv run --script X` é reescrito para
`python3 X`), `WebSearch` (busca nativa do provedor configurado para busca), `WebFetch` e as ferramentas
nativas tipadas, que rodam os scripts com caminhos validados dentro do projeto:

| Ferramenta | Faz |
|---|---|
| `MedirRoteiro(roteiro, modo="completo"\|"narracao"\|"orcamento", json_saida=None)` | `ferramentas/medir_roteiro.py` |
| `ConferirTexto(original, formatado, falantes=None)` | `ferramentas/conferir_texto.py` |
| `MontarCenas(pasta)` | `ferramentas/cenas.py montar` |
| `PesquisarYoutube(comando, termos, dias, formato, max, ordenar, regiao, idioma, aba, saida)` | `ferramentas/youtube_trends.py` com a chave do YouTube injetada só nele |
| `Transcrever(url, saida_dir)` | `ferramentas/fetch_youtube.py --out <saida_dir>` |
| `Entregar(...)` | só quando o fluxo pede saída estruturada: os parâmetros são o JSON Schema pedido; o motor valida e devolve os erros ao modelo até 3 vezes |

`nucleo/agentes/motor.py`:

```python
@dataclass class Rota: provedor: str; modelo: str
class Cancelado(Exception); class ErroMotor(Exception)
def rodar_agente(agente: str | Agente, mensagem: str, rota: Rota, ctx: "Contexto", *, esquema: dict | None = None,
                 imagens: Sequence[tuple[str, bytes]] = (), recuo: str = "    ") -> str | dict
def chamada_unica(sistema: str, mensagem: str, rota: Rota, ctx, *, esquema: dict | None = None,
                  imagens=()) -> str | dict      # sem ferramentas (leitor frio, tradução de ajuste...)
```

O log de cada agente usa os prefixos que a tela colore: `▶` agente, `$` comando, `✎` arquivo gravado, `·` leitura,
`⌕` busca, `›` fala do modelo, `✓` fim, `✗` erro, `■` cancelado. Cada passo soma o uso no contexto e grava uma
linha em `custos` (serviço `llm`, agente, provedor, modelo).

Agentes: `orquestrador-pedido` (completa o pedido; antigo papel da sessão principal, com a tabela "Como
decidir" da skill novo-video), `analista-referencias`, `pesquisador-trends`, `pesquisador-conteudo`,
`estrategista`, `redator-ganchos` (novo: de 6 a 8 ganchos para o ângulo escolhido, com padrão, referência,
gatilhos e obrigações), `roteirista`, `leitor-frio` (novo: lê só o texto ouvido, sem contexto, e aponta
confusões; também cada gancho candidato), `critico` (também dá as notas dos ganchos, pela rubrica de ganchos), `formatador`, `diretor-visual`, `revisor-visual` (novo: confere imagens, folha de contato,
thumbnail e quadros do vídeo com o checklist da antiga skill produzir-video), `publicador`,
`designer-personagem` (novo: a ficha de personagem da antiga skill criar-personagem).

## Rotas de modelo (`nucleo/rotas.py`)

Guardado em `configuracoes["modelos"]`:

```json
{
  "padroes": {
    "anthropic": {"julgamento": "claude-opus-5-5", "coleta": "claude-sonnet-5-5"},
    "openai":    {"julgamento": "gpt-6.1-sol",     "coleta": "gpt-6-luna"},
    "gemini":    {"julgamento": "gemini-4-argon",  "coleta": "gemini-3.8-flash"}
  },
  "etapas":  {"roteiro": {"provedor": "anthropic", "modelo": ""}},
  "agentes": {"critico": {"provedor": "openai", "modelo": "gpt-6.1-sol"}},
  "busca":   {"provedor": ""},
  "imagem":  {"provedor": "gemini", "modelos": {"rascunho": "", "padrao": "", "maxima": ""}, "tamanho": "1K", "paralelo": 6},
  "animacao": {"provedor": "gemini", "modelo": "", "resolucao": "720p", "paralelo": 4},
  "narracao": {"fornecedor": "gemini", "modelos": {"gemini": "", "openai": "", "elevenlabs": ""}},
  "alinhamento": {"modelo": "small"}
}
```

Modelo vazio = o padrão. Resolução da rota de um agente numa etapa: `agentes[agente]` > `etapas[etapa]`
(provedor; modelo vazio → `padroes[provedor][nivel do agente]`) > o primeiro provedor de LLM com chave
(anthropic, openai, gemini). Busca na web: `busca.provedor` ou o provedor do agente. Imagem: o provedor
configurado; o modelo vem de `modelos[visual.qualidade do pedido]` ou do padrão do provedor. Animação: o provedor,
o modelo (vazio = o padrão dele) e a resolução (vazia = 720p) configurados; o pedido só liga e escolhe as cenas
(`visual.animacao`). `paralelo` é quantas chamadas ficam abertas ao mesmo tempo (1 a 16). Narração: o
`narracao.fornecedor` **do pedido** vale mais (é escolha do vídeo); a configuração dá o padrão e o modelo de
cada fornecedor.

```python
ETAPAS: dict[str, dict]   # id -> {"nome", "agentes": [nomes]} (sugestoes, pesquisa, angulos, ganchos, roteiro,
                          #  visual, producao, personagem, importar, referencia)
def ler() -> dict                                   # com os padrões preenchidos
def gravar(parcial: dict) -> dict                   # mescla profunda; valida provedores e etapas; devolve tudo
                                                    # (a etapa gravada que saiu de ETAPAS é descartada na leitura)
def rota(etapa: str, agente: str) -> Rota
def rota_busca(rota_do_agente: Rota) -> Rota
def imagem(qualidade: str | None = None) -> tuple[str, str]           # (provedor, modelo)
def paralelo_imagem() -> int
def animacao() -> dict                              # {provedor, modelo, resolucao, paralelo}, com os padrões
def narracao(fornecedor_do_pedido: str | None) -> tuple[str, str]     # (fornecedor, modelo)
def modelo_whisper() -> str
def problemas(etapa: str) -> list[str]              # o que impede a etapa de rodar agora (falta de chave...)
```

`ETAPAS`:

| Etapa | Agentes |
|---|---|
| `sugestoes` | orquestrador-pedido |
| `pesquisa` | analista-referencias, pesquisador-conteudo, pesquisador-trends |
| `angulos` | estrategista |
| `ganchos` | redator-ganchos, leitor-frio, critico |
| `roteiro` (também `avaliar`) | roteirista, leitor-frio, critico |
| `visual` | diretor-visual |
| `producao` (também `publicacao`, `refazer-cenas`) | revisor-visual, diretor-visual, publicador |
| `importar` | formatador |
| `personagem` | designer-personagem, revisor-visual |
| `referencia` | analista-referencias |

## Pipeline (`nucleo/pipeline`)

- `etapas.py`: a trilha (configurar, sugestoes, pesquisa, angulos, ganchos, roteiro, visual, producao, edicao),
  `pronta` (a etapa `ganchos` também conta como feita no projeto de antes dela, que já tem roteiro e não tem
  `04-ganchos.json`), `aplicavel` (projeto com roteiro importado pula pesquisa, ângulos, ganchos e roteiro),
  `defasagens` (as `ENTRADAS` passam a incluir partes do brief: `00-brief.json#visual`, `#narracao`,
  `#estilo_artistico`, `#personagens`, `#legendas`, `#musica`, `#tom`, `#formato`; o roteiro depende do ângulo e do
  gancho escolhidos, e os ganchos, do ângulo), `fork`, `capa`. O ângulo e o gancho valem quando o usuário escolhe:
  a execução das duas etapas não registra as entradas, a escolha registra.
- A linha da execução guarda a etapa da trilha (`avaliar` conta como `roteiro`; `publicacao` e `refazer-cenas`, como
  `producao`), para a tela mostrar a última execução de cada etapa; o fluxo que rodou fica em `parametros["fluxo"]`.
- `execucao.py`: `Contexto` (log no banco, cancelamento, pasta temporária, uso e custo, `avisar(texto)`,
  `sincronizar()` para enviar o progresso), `enfileirar(slug, etapa, parametros) -> int`, `cancelar(id_)`,
  `Trabalhador` (loop de execuções, `concorrencia` threads, batimento a cada 5 s, cancelamento pelo banco,
  baixa o projeto e a biblioteca antes e envia depois de cada passo e no fim).
- `fluxos.py`: `FLUXOS = {etapa: função(ctx, slug, parametros) -> str | dict}`, com as fases do antigo
  `skills/novo-video` e os laços de qualidade:
  - pesquisa: confere cada saída e repete uma vez o agente que não gravou;
  - ganchos: reforço da pesquisa quando o ângulo aponta falta de material; o redator entrega de 6 a 8 ganchos
    (ids de tipo de gancho e de gatilho como enum do esquema); o sistema conta palavras e segundos; leitura fria
    isolada de cada um, em paralelo; o crítico entrega as notas pela seção `ganchos` da rubrica; `pontuar_gancho`
    corta a clareza pelas confusões e a fluidez pelo tamanho e calcula a nota; `melhores_ganchos` separa os 3
    melhores elegíveis. O gancho que o usuário pediu entre aspas na `descricao`, nas `restricoes`, nos ajustes do
    ângulo ou na orientação (`ganchos_pedidos`) vai na mensagem do redator; `marcar_gancho_pedido` marca o candidato
    pelo texto (`pedido_pelo_usuario`), que não perde em `padroes-observados`, vem primeiro nos melhores e é o
    recomendado se passar nos bloqueantes (o pedido vale mais que a nota). Tudo em `04-ganchos.json`; a escolha do usuário em `04-gancho-escolhido.json` (com o
    hash do ângulo: o gancho de outro ângulo não vale);
  - roteiro: orçamento calculado; o gancho escolhido e as obrigações do ângulo e do gancho (O1, O2...) na
    mensagem do roteirista; medição antes do crítico com uma rodada de correção quando há erro (inclusive tempo
    demais sem payoff), total fora de ±10% ou B01 sem o gancho escolhido; tipo de gancho e gatilhos do gancho
    carimbados no frontmatter; leitura fria isolada (`05-leitura-fria-vN.json`) entregue ao crítico; avaliação
    validada por esquema, com uma entrada em `obrigacoes` para cada obrigação; obrigação `nao` zera
    `promessas-cumpridas` e `parcial` deixa no máximo 1, antes da aprovação recalculada pela rubrica; orientação
    em todas as versões; edições do usuário preservadas; outro gancho no mesmo ângulo reescreve só a abertura;
  - visual: estilo obrigatório; erros e avisos de coerência do `cenas.py` devolvidos ao diretor;
  - produção: imagens da internet com nova busca para as não encontradas; cenas bloqueadas pelo filtro com o
    prompt suavizado e geradas de novo; revisão visual (revisor-visual) com até 2 rodadas de refazer cenas,
    fichas e opções da internet; `--sem-provisorio` fora do modo teste; narração com `--pausa-max` quando
    passa da duração; alinhamento com código 1 refaz os blocos apontados (até 2 vezes); `--corte-seco` em
    shorts; conferência dos quadros e da thumbnail; loudness medido; publicador;
  - importar (formatador + `conferir_texto` até sair ok), personagem (ficha, geração, autorrevisão, aprovação),
    referência (esqueleto + técnicas propostas para aprovação), refazer-cenas.
- `verificacoes.py`: validação de cada artefato por esquema (`03-angulos.json` com 2 ou 3 ângulos, gatilhos e
  obrigações, e os ids de tipo de gancho e gatilho conferidos contra `tecnicas.json`; `05-avaliacao-vN.json`;
  `08-cenas/*.json`, `11-publicacao.json`, esqueleto), os esquemas de entrega dos ganchos e da avaliação deles,
  recálculo da aprovação pela rubrica, `aplicar_obrigacoes`, `pontuar_gancho`, `melhores_ganchos`, leitura de
  `[VERIFICAR]`, medição de loudness.

## API HTTP (`web/api`)

Autenticação básica opcional (`APP_USUARIO`/`APP_SENHA`) em tudo menos `/api/saude`. Slugs validados por
`^[a-z0-9]+(?:-[a-z0-9]+)*$`. Escritas em projeto com execução ativa respondem 409.

Mantidas (mesmo formato de antes): `GET /api/opcoes`, `GET|POST /api/projetos`, `GET /api/projetos/{slug}`,
`GET /api/projetos/{slug}/capa`, `POST /api/projetos/{slug}/fork`, `PUT /api/projetos/{slug}/formulario`,
`PUT /api/projetos/{slug}/etapa`, `POST /api/projetos/{slug}/anexos`, `GET /api/projetos/{slug}/arquivos/{caminho}`,
`POST /api/projetos/{slug}/confirmar`, `POST /api/projetos/{slug}/executar` (`{etapa, ...parametros}` → `{id}`),
`GET /api/execucoes/{id}?desde=<cursor>` (→ `{log, tamanho: <novo cursor>, status, iniciada_em, resultado, avisos}`),
`POST /api/execucoes/{id}/cancelar`, `GET /api/projetos/{slug}/pesquisa|angulos|ganchos|roteiros|visual|producao`,
`POST /api/projetos/{slug}/angulos/escolher`, `POST /api/projetos/{slug}/ganchos/escolher` (`{id, texto}`: o
texto ajustado pelo usuário vale), `POST /api/projetos/{slug}/roteiros`,
`POST /api/projetos/{slug}/roteiros/{v}/final`, contextos (`/api/contextos...`),
`GET /api/biblioteca/personagens/{slug}/imagem`, `GET /api/biblioteca/vozes/{nome}`.

Mudanças e novas:

| Rota | Corpo / resposta |
|---|---|
| `GET /api/saude` | `{ok, banco, armazenamento}` (sem autenticação) |
| `GET /api/configuracoes` | `{provedores:[{id,nome,usos,painel,origem,mascara,teste,modelos}], modelos:<config>, etapas:[{id,nome,agentes:[{nome,nivel,descricao}]}], niveis, llm:[ids], imagem:{provedores:[{id,nome,modelo_padrao,modelos_por_qualidade,precisa_chave}]}, animacao:{provedores:[{id,nome,modelo_padrao,modelos,resolucoes,resolucao_padrao,proporcoes,precisa_chave}]}, narracao:{fornecedores:[{id,nome,modelos,modelo_padrao,precisa_chave}]}, whisper:[...], precos:[ids], sistema:{armazenamento,bucket,banco}}` |
| `PUT /api/configuracoes/modelos` | configuração parcial (mescla) → configuração completa |
| `PUT /api/configuracoes/chaves/{provedor}` | `{valor}` → `{teste, mascara}`; `DELETE` apaga; `POST .../testar` → `{ok, mensagem}` |
| `POST /api/projetos` | `{titulo, roteiro?}`: com `roteiro`, grava `00-roteiro-original.md` e o projeto nasce com `origem: roteiro-importado`. O formulário nasce com `producao.ate: video`: até onde ir é obrigatório na configuração e fica fora das sugestões (`FORA_DAS_SUGESTOES`) |
| `GET /api/projetos/{slug}` | acrescenta `roteiro_importado: {palavras}` (as palavras narradas de `00-roteiro-original.md`; `null` no projeto do pipeline), para a tela calcular a duração com a velocidade escolhida. Nesse projeto, `duracao_alvo` fica fora do pedido do usuário e das sugestões (`etapas.fora_das_sugestoes(slug)`): o `pedido.py completar` a calcula pelo roteiro |
| `POST /api/projetos/{slug}/executar` | etapas: `sugestoes`, `pesquisa`, `angulos` (`orientacao`), `ganchos` (`orientacao`), `roteiro` (`orientacao`, `rodadas`), `avaliar` (`versao`), `visual` (`confirmar_verificar`), `producao` (`teste`, `confirmar_imagens`, `confirmar_animacao`: com `visual.animacao` ligada, 400 sem a chave do provedor de vídeo e 409 com `{cenas, segundos}` e a estimativa do custo até confirmar), `publicacao`, `importar`, `refazer-cenas` (`cenas`, `ajuste`) |
| `GET /api/projetos/{slug}/producao` | acrescenta `cenas:[{id, bloco, imagem, origem, status}]`, `revisao`, `avisos`, `custo` (do banco) |
| `GET|POST /api/projetos/{slug}/desempenho`, `GET /api/desempenho` | métricas por janela; o resumo regenera `biblioteca/aprendizados.md` |
| `GET /api/personagens` · `POST /api/personagens` (`{descricao, nome?}`) · `GET /api/personagens/rascunhos/{slug}` · `POST /api/personagens/rascunhos/{slug}/gerar` (`{ajuste?, base?}`) · `POST /api/personagens/rascunhos/{slug}/aprovar` (`{versao}`) | criação de personagem (execuções sem projeto) |
| `GET /api/referencias` · `POST /api/referencias` (`{url}`) · `POST /api/referencias/{arquivo}/tecnicas` (`{aprovar:[ids]}`) | analisar vídeo de referência e aprovar técnicas novas |
| `GET /api/arquivos/{caminho}` | arquivos de `biblioteca/` e `rascunhos/` (lista branca de prefixos) |

## Implantação

`docker-compose.yml`: `postgres` (16), `minio` (+ `minio-init` que cria o bucket), `web` (uvicorn,
porta 8765) e `trabalhador` (mesma imagem, `python -m nucleo.trabalhador`). Volumes: dados do Postgres, do
MinIO, cache do modelo whisper (`HF_HOME`). A imagem (`python:3.12-slim-trixie`, pelo ffmpeg 7: o 5.1 do bookworm quebra a thumbnail e a mixagem) tem `ffmpeg` (com libass),
`espeak-ng`, `fonts-noto-core`, `fontconfig`, `jq` e as dependências de `pyproject.toml`. Para usar AWS S3 em
vez do MinIO, defina `ARMAZENAMENTO=s3` e as variáveis `AWS_*` no `.env` (o MinIO pode ficar desligado com
`docker compose up web trabalhador postgres`).
