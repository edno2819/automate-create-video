// Interface web do pipeline de vídeos. Sem framework: cada tela monta o DOM com h() e fala com a API do
// servidor (web/servidor.py). O estado de verdade fica no servidor (banco e armazenamento de arquivos); aqui
// só o que está na tela. Quem roda as etapas é o trabalhador do servidor: a tela só enfileira e acompanha o log.

'use strict';

const app = document.getElementById('app');
let opcoes = null;      // /api/opcoes: campos do pedido, vozes, músicas, personagens da biblioteca...
let estado = null;      // /api/projetos/<slug>: projeto, etapas, brief, execução rodando
let polling = null;
let pararPrevia = null; // a prévia da edição toca as faixas em objetos Audio fora da página: a troca de tela a para
const revisoes = {};    // slug -> revisão das sugestões em andamento (não vai para o banco até confirmar)
const avisosPendentes = {};

const ROTULOS = {
  tema: 'Tema', descricao: 'Descrição', contexto: 'Contexto', slug: 'Pasta do projeto', titulo: 'Título',
  formato: 'Formato', duracao_alvo: 'Duração', tom: 'Tom', natureza: 'Natureza', publico: 'Público', nicho: 'Nicho',
  idioma: 'Idioma', estilo_artistico: 'Estilo artístico', imagens_referencia: 'Imagens de referência',
  'visual.proporcao': 'Proporção', 'visual.segundos_por_imagem': 'Segundos por imagem',
  'visual.numero_de_imagens': 'Número de imagens', 'visual.imagens_da_internet': 'Imagens da internet',
  'visual.qualidade': 'Qualidade das imagens', 'visual.animacao': 'Animação das imagens', 'narracao.ativa': 'Narração', 'narracao.fornecedor': 'Fornecedor da voz',
  'narracao.voz': 'Voz do narrador', 'narracao.estilo': 'Jeito do narrador',
  'narracao.palavras_por_minuto': 'Velocidade (palavras por minuto)', personagens: 'Personagens',
  'legendas.ativas': 'Legendas', 'legendas.destacar_palavra': 'Destacar a palavra dita', 'legendas.cor_destaque': 'Cor do destaque',
  'legendas.cor_texto': 'Cor das letras', 'legendas.maiusculas': 'Caixa alta', 'legendas.palavras_por_legenda': 'Palavras na tela',
  'legendas.posicao': 'Posição', 'musica.ativa': 'Música de fundo', 'musica.nome': 'Música e artista', 'musica.arquivo': 'Arquivo',
  'musica.inicio': 'Começa em', 'musica.volume': 'Volume (%)', referencias: 'Vídeos de referência (estrutura)',
  canais_referencia: 'Canais de referência', concorrentes: 'Concorrentes', restricoes: 'Restrições',
  'trends.ativa': 'Pesquisar trends', 'trends.links': 'Links sugeridos para a pesquisa', 'producao.ate': 'Até onde ir',
  modo_voz: 'Vozes do vídeo',
};
const ROTULOS_OPCOES = {
  'producao.ate': { roteiro: 'roteiro (para no roteiro final)', visual: 'visual (cenas e prompts das imagens)', video: 'vídeo (imagens, voz e montagem)' },
};
const LONGOS = { descricao: 5, estilo_artistico: 3, tom: 2, 'narracao.estilo': 2, publico: 2 };
const CUSTOM = new Set(['slug', 'contexto', 'imagens_referencia', 'personagens', 'musica.arquivo', 'trends.ativa', 'trends.links']);
// Preenchidos pelo usuário na configuração; o sistema nunca sugere. Até onde ir já vem marcado no vídeo completo.
const OBRIGATORIOS = new Set(['titulo', 'descricao', 'producao.ate']);
const PALETA = ['#EC4899', '#22C55E', '#3B82F6', '#F97316', '#8B5CF6', '#06B6D4', '#EF4444', '#EAB308'];

// ---------------------------------------------------------------- utilidades

function h(tag, attrs, ...filhos) {
  const el = document.createElement(tag);
  for (const f of filhos.flat(Infinity)) {
    if (f == null || f === false) continue;
    el.append(f instanceof Node ? f : document.createTextNode(String(f)));
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'readOnly' || k === 'selected') el[k] = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  return el;
}

async function api(metodo, url, corpo) {
  const opts = { method: metodo, headers: {} };
  if (corpo instanceof FormData) opts.body = corpo;
  else if (corpo !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(corpo); }
  const r = await fetch(url, opts);
  const texto = await r.text();
  let dados = null;
  try { dados = texto ? JSON.parse(texto) : null; } catch { dados = texto; }
  if (!r.ok) {
    // o FastAPI põe o motivo em detail (texto, ou um objeto com detail/mensagem e campos como precisa)
    const det = dados && typeof dados === 'object' ? dados.detail : null;
    const msg = typeof det === 'string' ? det
      // erro de validação do FastAPI (422): uma lista com o campo e o motivo de cada problema
      : Array.isArray(det) ? det.map((d) => (d && d.msg ? `${(d.loc || []).slice(1).join('.') || 'corpo'}: ${d.msg}` : JSON.stringify(d))).join('; ')
      : det && typeof det === 'object' && (det.detail || det.mensagem) ? det.detail || det.mensagem
        : det ? JSON.stringify(det) : typeof dados === 'string' && dados ? dados : r.statusText;
    const erro = new Error(msg);
    erro.status = r.status;
    erro.dados = dados;
    throw erro;
  }
  return dados;
}

// Campo extra de uma resposta de erro, no corpo ou dentro de detail (ex.: precisa: "confirmar_imagens").
function campoDoErro(e, nome) {
  const d = e && e.dados;
  if (!d || typeof d !== 'object') return undefined;
  if (d[nome] !== undefined) return d[nome];
  return d.detail && typeof d.detail === 'object' ? d.detail[nome] : undefined;
}

// Guarda pequena por navegador (a última execução avulsa acompanhada, por exemplo). Pode falhar numa janela
// privada ou com os dados do site bloqueados: aí a tela só não lembra.
function lembrar(chave, valor) {
  try {
    if (valor == null) sessionStorage.removeItem(chave); else sessionStorage.setItem(chave, JSON.stringify(valor));
  } catch { /* sem armazenamento: segue sem lembrar */ }
}
function lembrado(chave) {
  try { const v = sessionStorage.getItem(chave); return v ? JSON.parse(v) : null; } catch { return null; }
}

function aviso(texto, tipo = 'info', ms = 6000) {
  const el = h('div', { class: `aviso ${tipo}` }, texto);
  document.getElementById('avisos').append(el);
  setTimeout(() => el.remove(), ms);
}

function falhou(e) { console.error(e); aviso(e.message || String(e), 'erro', 12000); }

function md(texto) {
  const div = h('div', { class: 'md' });
  if (window.marked && window.DOMPurify) div.innerHTML = DOMPurify.sanitize(marked.parse(texto || ''));
  else div.append(h('pre', { class: 'saida' }, texto || ''));
  return div;
}

function vazio(v) { return v == null || (typeof v === 'string' && !v.trim()) || (Array.isArray(v) && !v.length); }
function norm(s) { return String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').trim().toLowerCase(); }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function clonar(x) { return JSON.parse(JSON.stringify(x)); }
function rotulo(chave) { return ROTULOS[chave] || chave; }
function campo(chave) { return opcoes.campos.find((c) => c.chave === chave); }
function boolDe(v) { return v === true || v === 'sim' ? true : v === false || v === 'não' || v === 'nao' ? false : null; }
function texto(v) {
  if (v === true) return 'sim';
  if (v === false) return 'não';
  if (Array.isArray(v)) return v.join(', ');
  return v == null ? '' : String(v);
}
function quando(iso) {
  if (!iso) return '';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return 'agora';
  if (s < 3600) return `há ${Math.round(s / 60)} min`;
  if (s < 86400) return `há ${Math.round(s / 3600)} h`;
  return new Date(iso).toLocaleDateString('pt-BR');
}
function autoAltura(t) {
  if (!t.offsetParent) return; // escondida (num <details> fechado): mede quando aparecer
  t.style.height = 'auto';
  t.style.height = `${Math.max(t.scrollHeight + 2, 38)}px`;
}
const remedir = () => app.querySelectorAll('textarea[data-auto]').forEach(autoAltura);
window.addEventListener('resize', debounce(remedir, 150));
if (document.fonts) document.fonts.ready.then(remedir);
document.addEventListener('toggle', (e) => e.target.querySelectorAll?.('textarea[data-auto]').forEach(autoAltura), true);

// replaceChildren sem os filhos vazios (null viraria o texto "null")
function trocar(el, ...filhos) { el.replaceChildren(...filhos.flat(Infinity).filter((f) => f != null && f !== false)); }

function pararPolling() { if (polling) { clearInterval(polling); polling = null; } }

// Nome legível de uma etapa: a trilha do projeto aberto, a lista geral das opções ou as etapas que não aparecem
// na trilha (avaliar, publicação, refazer cenas, as da biblioteca).
const NOMES_ETAPA = {
  importar: 'Importar roteiro', avaliar: 'Avaliação do roteiro', publicacao: 'Textos de publicação',
  'refazer-cenas': 'Refazer cenas', personagem: 'Personagem', referencia: 'Referência', dublagem: 'Dublagem',
  exportar: 'Exportação',
};
function nomeEtapa(id) {
  const e = ((estado && estado.etapas) || []).find((x) => x.id === id) || ((opcoes && opcoes.etapas) || []).find((x) => x.id === id);
  return e ? e.nome : NOMES_ETAPA[id] || id || '';
}

// A tela onde cada execução aparece: avaliar é parte do roteiro; publicação e refazer cenas, da produção; dublagem e
// exportação, da edição.
const TELA_DA_ETAPA = { avaliar: 'roteiro', publicacao: 'producao', 'refazer-cenas': 'producao', dublagem: 'edicao', exportar: 'edicao' };
function telaDa(etapa) { return TELA_DA_ETAPA[etapa] || etapa; }

// Uma execução ainda não terminou enquanto espera na fila (pendente) ou roda.
function ativa(status) { return status === 'pendente' || status === 'rodando'; }

// Endereço de um arquivo do projeto aberto. O servidor às vezes devolve o caminho a partir da raiz
// (projetos/<slug>/...), às vezes a partir da pasta do projeto, às vezes já a URL.
let carimboImagens = Date.now(); // muda quando uma execução termina, para o navegador buscar as imagens refeitas

// Codifica cada parte do caminho, mas não a consulta (?v=...) que o servidor às vezes já põe no fim.
function caminhoCodificado(caminho, recente) {
  const [rel, consulta] = caminho.split('?');
  const partes = rel.split('/').map(encodeURIComponent).join('/');
  const parametros = [consulta, recente ? `v=${carimboImagens}` : null].filter(Boolean).join('&');
  return parametros ? `${partes}?${parametros}` : partes;
}

function urlDoProjeto(slug, caminho, { recente = false } = {}) {
  if (!caminho) return null;
  if (/^(\/|https?:|data:)/.test(caminho)) return caminho;
  const prefixo = `projetos/${slug}/`;
  const rel = caminho.startsWith(prefixo) ? caminho.slice(prefixo.length) : caminho;
  return `/api/projetos/${encodeURIComponent(slug)}/arquivos/${caminhoCodificado(rel, recente)}`;
}

// Arquivos da biblioteca e dos rascunhos (personagens em criação), servidos por /api/arquivos/.
function urlDoArquivo(caminho, { recente = false } = {}) {
  if (!caminho) return null;
  if (/^(\/|https?:|data:)/.test(caminho)) return caminho;
  return `/api/arquivos/${caminhoCodificado(caminho, recente)}`;
}

// Texto de um arquivo servido pela API (roteiro original, por exemplo), ou null se ainda não existe.
async function lerTexto(url) {
  const r = await fetch(url);
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.text();
}

// Uma lista que pode vir sozinha ou dentro de um objeto ({esqueletos: [...]}, {registros: [...]}...).
function listaDeDados(x, ...chaves) {
  if (Array.isArray(x)) return x;
  for (const k of chaves) if (x && Array.isArray(x[k])) return x[k];
  return [];
}

// ---------------------------------------------------------------- rotas

window.addEventListener('hashchange', rota);
window.addEventListener('beforeunload', (e) => { if (editor && editor.sujo) { e.preventDefault(); e.returnValue = ''; } });

async function rota() {
  pararPolling();
  if (pararPrevia) { pararPrevia(); pararPrevia = null; }
  try {
    if (!opcoes) opcoes = await api('GET', '/api/opcoes');
    const partes = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
    // endereços antigos: a tela de chaves era #/conexoes e a de narração virou "Imagens e voz"
    const legado = partes[0] === 'conexoes' ? ['configuracoes', 'chaves']
      : partes[0] === 'configuracoes' && partes[1] === 'narracao' ? ['configuracoes', 'midia'] : null;
    if (legado) {
      history.replaceState(null, '', `#/${legado.join('/')}`);
      partes.splice(0, partes.length, ...legado);
    }
    const aba = ['contextos', 'configuracoes', 'biblioteca'].includes(partes[0]) ? partes[0] : 'videos';
    document.querySelectorAll('.abas-topo a').forEach((a) => a.classList.toggle('ativa', a.dataset.aba === aba));
    if (partes[0] === 'p' && partes[1]) await abrirProjeto(decodeURIComponent(partes[1]), partes[2]);
    else if (partes[0] === 'contextos' && partes[1]) await telaContexto(decodeURIComponent(partes[1]));
    else if (partes[0] === 'contextos') await telaContextos();
    else if (partes[0] === 'configuracoes') await telaConfiguracoes(partes[1]);
    else if (partes[0] === 'biblioteca') await telaBiblioteca(partes[1], partes[2] && decodeURIComponent(partes[2]));
    else await telaInicial();
  } catch (e) {
    falhou(e);
    trocar(app, h('div', { class: 'faixa erro' }, `Não consegui carregar: ${e.message}`));
  }
}

function irPara(slug, etapa) {
  const alvo = `#/p/${encodeURIComponent(slug)}/${etapa}`;
  if (location.hash === alvo) rota(); else location.hash = alvo;
}

// ---------------------------------------------------------------- tela inicial

async function telaInicial() {
  document.getElementById('topo-projeto').textContent = '';
  const projetos = await api('GET', '/api/projetos');
  const titulo = h('input', { type: 'text', placeholder: 'Título do vídeo, ex.: Como era ser cada cargo de um navio pirata' });
  // "Já tenho o roteiro": o texto vai como veio; o sistema só o divide em blocos e pula pesquisa, ângulo e roteiro.
  const roteiro = h('textarea', {
    class: 'grande', rows: 12,
    placeholder: 'Cole aqui o roteiro inteiro. O texto narrado não muda: o sistema só o divide em blocos, marca as falas dos personagens e confere palavra por palavra.',
  });
  const caixaRoteiro = h('div', { style: { marginTop: '.7rem', display: 'none' } }, roteiro);
  const temRoteiro = h('input', {
    type: 'checkbox', onchange: (e) => {
      caixaRoteiro.style.display = e.target.checked ? '' : 'none';
      botao.textContent = e.target.checked ? 'Criar vídeo com este roteiro' : 'Criar vídeo';
      if (e.target.checked) roteiro.focus();
    },
  });
  const criar = async () => {
    if (!titulo.value.trim()) { titulo.focus(); return aviso('Escreva o título do vídeo.'); }
    const corpoReq = { titulo: titulo.value.trim() };
    if (temRoteiro.checked) {
      if (!roteiro.value.trim()) { roteiro.focus(); return aviso('Cole o roteiro, ou desmarque "Já tenho o roteiro".'); }
      corpoReq.roteiro = roteiro.value;
    }
    botao.disabled = true;
    try {
      const p = await api('POST', '/api/projetos', corpoReq);
      irPara(p.slug, 'configurar');
    } catch (e) { botao.disabled = false; falhou(e); }
  };
  const botao = h('button', { class: 'btn primario', onclick: criar }, 'Criar vídeo');
  titulo.addEventListener('keydown', (e) => { if (e.key === 'Enter') criar(); });
  trocar(app,
    h('div', { class: 'card' },
      h('h1', {}, 'Novo vídeo'),
      h('p', { class: 'mudo' }, 'Comece pelo título. Na próxima tela, a descrição e o que mais você quiser definir; o resto o sistema sugere.'),
      h('div', { class: 'linha' }, h('div', { style: { flex: '1', minWidth: '240px' } }, titulo), botao),
      h('label', { class: 'interruptor', style: { marginTop: '.8rem' } }, temRoteiro, 'Já tenho o roteiro'),
      caixaRoteiro),
    h('h2', { style: { margin: '1.4rem 0 .7rem' } }, 'Projetos'),
    projetos.length ? h('div', { class: 'lista-projetos' }, projetos.map((p) =>
      h('a', { class: 'projeto', href: `#/p/${encodeURIComponent(p.slug)}` },
        h('div', { class: 'card' },
          // a capa inteira, com margens pretas onde a proporção dela não é a do quadro (a thumbnail vertical do short)
          p.capa ? h('div', { class: 'capa com-imagem' }, h('img', { src: p.capa, alt: `capa de ${p.titulo}`, loading: 'lazy' })) :
            h('div', { class: 'capa' }, h('span', { class: 'mudo pequeno' }, 'sem thumbnail ainda')),
          h('strong', {}, p.titulo),
          h('div', { class: 'mudo pequeno' }, p.slug),
          h('div', { class: 'linha', style: { marginTop: '.5rem' } },
            h('span', { class: 'etiqueta' }, nomeEtapa(p.etapa || 'configurar')),
            p.origem === 'roteiro-importado' ? h('span', { class: 'etiqueta usuario' }, 'roteiro próprio') : null,
            h('span', { class: 'mudo pequeno' }, quando(p.atualizado_em))))))) :
      h('p', { class: 'mudo' }, 'Nenhum projeto ainda.'),
  );
  titulo.focus();
}

// ---------------------------------------------------------------- configurações

const USOS = { llm: 'LLM', narracao: 'Narração', imagem: 'Imagens', animacao: 'Animação', trends: 'Trends', busca: 'Busca na web' };
const NOMES_LLM = { anthropic: 'Claude', openai: 'GPT', gemini: 'Gemini' };
const QUALIDADES = { rascunho: 'Rascunho', padrao: 'Padrão', maxima: 'Máxima' };
const TAMANHOS_IMAGEM = ['1K', '2K', '4K'];
const ARMAZENAMENTOS = { minio: 'MinIO', s3: 'AWS S3', local: 'pasta local do servidor' };

const SUBABAS_CONFIG = {
  chaves: 'Chaves de API',
  modelos: 'Modelos por etapa',
  midia: 'Imagens e voz',
  efeitos: 'Efeitos sonoros',
  sistema: 'Sistema',
};

// As linhas de agentes abertas na tabela de modelos continuam abertas quando a tabela é redesenhada.
const etapasAbertas = new Set();

async function telaConfiguracoes(sub) {
  if (!SUBABAS_CONFIG[sub]) {
    sub = 'chaves';
    history.replaceState(null, '', '#/configuracoes/chaves');
  }
  document.getElementById('topo-projeto').textContent = `· Configurações · ${SUBABAS_CONFIG[sub]}`;
  const dados = await api('GET', '/api/configuracoes');
  dados.provedores = dados.provedores || [];
  dados.modelos = dados.modelos || {};
  const porId = Object.fromEntries(dados.provedores.map((p) => [p.id, p]));
  const conteudo = h('div');
  const ctx = {
    dados, porId, sub,
    temChave: (id) => !!(porId[id] && porId[id].origem),
    recarregar: () => telaConfiguracoes(sub).catch(falhou),
  };
  const semChaves = !dados.provedores.some((p) => p.origem);
  const subabas = h('nav', { class: 'subabas' }, Object.entries(SUBABAS_CONFIG).map(([id, nome]) =>
    h('a', { href: `#/configuracoes/${id}`, class: id === sub ? 'ativa' : null }, nome)));
  trocar(app, h('h1', {}, 'Configurações'),
    h('p', { class: 'mudo' }, 'As chaves dos provedores, o modelo que roda cada etapa do pipeline, os modelos de imagem e de voz, a biblioteca de efeitos sonoros e o estado do servidor.'),
    subabas,
    ['modelos', 'midia'].includes(sub) && semChaves ? h('div', { class: 'faixa alerta' }, 'Nenhuma chave cadastrada ainda. ',
      h('a', { href: '#/configuracoes/chaves' }, 'Cadastre em Chaves de API'), '.') : null,
    conteudo);
  const desenhar = { chaves: subabaChaves, modelos: subabaModelos, midia: subabaMidia, efeitos: subabaEfeitos, sistema: subabaSistema }[sub];
  await desenhar(conteudo, ctx);
}

// Grava uma parte da configuração de modelos (o servidor mescla) e redesenha a sub-aba com a configuração
// completa que ele devolve. null numa chave apaga a escolha (o agente volta a herdar da etapa, por exemplo).
async function gravarModelos(ctx, parcial, mensagem) {
  try {
    const r = await api('PUT', '/api/configuracoes/modelos', parcial);
    if (r && typeof r === 'object') ctx.dados.modelos = r.modelos && !r.padroes ? r.modelos : r;
    if (mensagem) aviso(mensagem, 'ok', 2500);
  } catch (e) { falhou(e); }
  ctx.redesenhar();
}

// Escolha de modelo: "padrão" (vazio), a lista conhecida (a que o teste da chave trouxe) e "outro…" para
// digitar o nome exato de um modelo que não está na lista.
function seletorModelo(valor, conhecidos, aoMudar, rotuloPadrao) {
  const OUTRO = '__outro__';
  const lista = [...new Set((conhecidos || []).filter(Boolean))];
  const naLista = !valor || lista.includes(valor);
  const livre = h('input', {
    type: 'text', value: naLista ? '' : valor, placeholder: 'nome exato do modelo', style: { display: naLista ? 'none' : '' },
    onchange: (e) => { const v = e.target.value.trim(); if (v && v !== valor) aoMudar(v); },
  });
  const sel = h('select', {
    onchange: (e) => {
      if (e.target.value === OUTRO) { livre.style.display = ''; livre.focus(); return; }
      livre.style.display = 'none';
      aoMudar(e.target.value);
    },
  },
  rotuloPadrao != null ? h('option', { value: '', selected: !valor }, rotuloPadrao) : null,
  lista.map((m) => h('option', { value: m, selected: m === valor }, m)),
  h('option', { value: OUTRO, selected: !naLista }, naLista ? 'outro…' : `outro: ${valor}`));
  return h('div', { class: 'seletor-modelo' }, sel, livre);
}

// Quantas chamadas ao provedor ficam abertas ao mesmo tempo (1 a 16); grava ao sair do campo.
function campoParalelo(valor, padrao, aoMudar) {
  const atual = valor || padrao;
  return h('input', {
    type: 'number', min: 1, max: 16, step: 1, value: atual, onchange: (e) => {
      const n = Math.round(Number(e.target.value));
      if (!Number.isFinite(n) || n < 1 || n > 16) { e.target.value = atual; return aviso('Use um número de 1 a 16.'); }
      if (n !== atual) aoMudar(n);
    },
  });
}

function avisoPreco(dados, modelo) {
  if (!modelo || (dados.precos || []).includes(modelo)) return null;
  return h('div', { class: 'pequeno texto-alerta' }, `${modelo} não tem preço na tabela: o custo dele fica fora do total.`);
}

// ---------------------------------------------------------------- configurações · chaves

function subabaChaves(raiz, ctx) {
  ctx.redesenhar = ctx.recarregar;
  trocar(raiz, h('section', { class: 'card' }, h('h2', {}, 'Chaves de API'),
    h('p', { class: 'mudo pequeno' }, 'As chaves ficam guardadas no banco, cifradas, e só aparecem aqui mascaradas. Salvar já testa a chave. Uma chave definida como variável de ambiente do servidor vale mais que a daqui e não se edita por esta tela.'),
    h('div', { class: 'grade-provedores' }, ctx.dados.provedores.map((p) => cartaoProvedor(p, ctx)))));
}

function cartaoProvedor(p, ctx) {
  if (!p.origem) p.teste = null;  // o teste antigo não diz nada sobre uma chave que não existe mais
  const url = `/api/configuracoes/chaves/${encodeURIComponent(p.id)}`;
  const status = !p.origem ? h('span', { class: 'etiqueta' }, 'sem chave')
    : p.teste && p.teste.ok ? h('span', { class: 'etiqueta ok' }, 'conectado')
      : p.teste ? h('span', { class: 'etiqueta erro' }, 'falhou') : h('span', { class: 'etiqueta' }, 'não testado');
  const campo = h('input', { type: 'password', placeholder: p.origem ? 'colar outra chave para trocar' : 'colar a chave', autocomplete: 'off' });
  // as vozes da ElevenLabs vêm da conta: depois de salvar ou testar a chave, a lista de vozes muda
  const recarregar = async () => {
    if (p.id === 'elevenlabs') { try { opcoes = await api('GET', '/api/opcoes'); } catch (e) { console.error(e); } }
    await ctx.recarregar();
  };
  const salvar = h('button', {
    class: 'btn primario pequeno', onclick: async () => {
      if (!campo.value.trim()) { campo.focus(); return aviso('Cole a chave.'); }
      salvar.disabled = true;
      try {
        const r = await api('PUT', url, { valor: campo.value.trim() });
        const t = (r && r.teste) || {};
        aviso(t.ok ? `Chave salva e testada: ${t.mensagem}.` : `Chave salva, mas o teste falhou: ${t.mensagem || 'sem resposta'}`, t.ok ? 'ok' : 'erro');
        await recarregar();
      } catch (e) { salvar.disabled = false; falhou(e); }
    },
  }, p.origem ? 'Trocar e testar' : 'Salvar e testar');
  const testar = h('button', {
    class: 'btn pequeno', disabled: !p.origem, onclick: async () => {
      testar.disabled = true;
      try {
        const r = await api('POST', `${url}/testar`);
        aviso(`${p.nome}: ${r.mensagem}${r.ok ? '.' : ''}`, r.ok ? 'ok' : 'erro');
        await recarregar();
      } catch (e) { testar.disabled = false; falhou(e); }
    },
  }, 'Testar');
  const remover = p.origem === 'banco' ? h('button', {
    class: 'btn pequeno perigo', onclick: async () => {
      if (!confirm(`Apagar a chave de ${p.nome}? O que usa ${p.nome} para de funcionar até você cadastrar outra.`)) return;
      try { await api('DELETE', url); await recarregar(); } catch (e) { falhou(e); }
    },
  }, 'Remover') : null;
  const deOnde = {
    banco: 'guardada no banco, cifrada',
    ambiente: `vem de uma variável de ambiente do servidor${p.variavel ? ` (${p.variavel})` : ''}: troque onde ela é definida`,
    arquivo: 'vem do arquivo de chaves antigo do servidor, só leitura: cole uma chave aqui para guardá-la no banco',
  }[p.origem];
  return h('div', { class: 'provedor' },
    h('div', { class: 'linha-topo' }, h('strong', {}, p.nome), status),
    h('div', { class: 'linha', style: { marginBottom: '.5rem' } }, (p.usos || []).map((u) => h('span', { class: 'chip' }, USOS[u] || u))),
    p.origem ? h('div', { class: 'mudo pequeno' }, h('code', {}, p.mascara || '••••'), deOnde ? ` · ${deOnde}` : '') : null,
    p.teste ? h('div', { class: `pequeno ${p.teste.ok ? 'mudo' : 'texto-erro'}`, style: { marginTop: '.3rem' } },
      `${p.teste.ok ? '✓' : '✗'} ${p.teste.mensagem}${p.teste.quando ? ` · ${quando(p.teste.quando)}` : ''}`) : null,
    p.origem === 'ambiente' ? null : h('div', { class: 'linha', style: { marginTop: '.6rem' } },
      h('div', { style: { flex: '1', minWidth: '200px' } }, campo), salvar),
    h('div', { class: 'linha', style: { marginTop: '.5rem' } }, testar, remover, h('span', { class: 'espaco' }),
      p.painel ? h('a', { href: p.painel, target: '_blank', rel: 'noopener', class: 'pequeno' }, 'criar chave ↗') : null));
}

// ---------------------------------------------------------------- configurações · modelos por etapa

// O modelo que um agente usa de fato numa etapa, pela mesma regra do servidor (nucleo/rotas.py): a escolha
// do agente > a da etapa (modelo vazio = o padrão do provedor para o nível do agente) > o primeiro provedor de
// LLM com chave.
function resolverRota(ctx, etapaId, agente) {
  const cfg = ctx.dados.modelos;
  const doAgente = (cfg.agentes || {})[agente.nome];
  const daEtapa = (cfg.etapas || {})[etapaId];
  let provedor = '';
  let modelo = '';
  let fonte = 'automático';
  if (doAgente && doAgente.provedor) { provedor = doAgente.provedor; modelo = doAgente.modelo || ''; fonte = 'próprio'; }
  else if (daEtapa && daEtapa.provedor) { provedor = daEtapa.provedor; modelo = daEtapa.modelo || ''; fonte = 'da etapa'; }
  else provedor = primeiroLlmComChave(ctx);
  if (provedor && !modelo) modelo = ((cfg.padroes || {})[provedor] || {})[agente.nivel] || '';
  return { provedor, modelo, fonte };
}

function llmIds(ctx) { return ctx.dados.llm || ['anthropic', 'openai', 'gemini']; }
function primeiroLlmComChave(ctx) { return llmIds(ctx).find(ctx.temChave) || ''; }
function nomeLlm(ctx, id) { return id ? NOMES_LLM[id] || (ctx.porId[id] || {}).nome || id : 'nenhum'; }
// Os modelos que o teste da chave listou, mais os padrões do provedor (aparecem mesmo antes do teste).
function modelosDe(ctx, id) {
  const padroes = Object.values(((ctx.dados.modelos.padroes || {})[id]) || {});
  return [...new Set([...((ctx.porId[id] && ctx.porId[id].modelos) || []), ...padroes].filter(Boolean))];
}
function listouModelos(ctx, id) { return !!(ctx.porId[id] && (ctx.porId[id].modelos || []).length); }
function niveisDe(ctx) {
  const n = ctx.dados.niveis;
  if (Array.isArray(n) && n.length) return n.map((x) => (typeof x === 'object' ? x.id : x));
  if (n && typeof n === 'object' && Object.keys(n).length) return Object.keys(n);
  return ['julgamento', 'coleta'];
}

// Opções de provedor de LLM; sem chave fica desabilitado (menos o que já está escolhido, para não sumir).
function opcoesLlm(ctx, atual) {
  return llmIds(ctx).map((id) => h('option', { value: id, selected: id === atual, disabled: !ctx.temChave(id) && id !== atual },
    nomeLlm(ctx, id) + (ctx.temChave(id) ? '' : ' (sem chave)')));
}

function subabaModelos(raiz, ctx) {
  ctx.redesenhar = () => subabaModelos(raiz, ctx);
  const { dados } = ctx;
  const cfg = dados.modelos;
  const etapas = dados.etapas || [];
  const automatico = primeiroLlmComChave(ctx);
  const niveis = niveisDe(ctx);

  // padrões de cada provedor: o modelo de julgamento e o de coleta
  const padroes = h('section', { class: 'card' }, h('h2', {}, 'Padrões de cada provedor'),
    h('p', { class: 'mudo pequeno' }, 'Cada agente tem um nível: julgamento (escrever, avaliar, decidir, onde o melhor modelo faz diferença) ou coleta (pesquisar, formatar, onde um modelo mais barato basta). Uma etapa sem modelo escolhido usa, para cada agente, o padrão do nível dele no provedor da etapa.'),
    h('div', { class: 'rolagem' }, h('table', { class: 'tabela-etapas tabela-padroes' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Provedor'), niveis.map((n) => h('th', {}, `Modelo de ${n}`)))),
      h('tbody', {}, llmIds(ctx).map((id) => h('tr', {},
        h('td', {}, h('strong', {}, nomeLlm(ctx, id)), ctx.temChave(id) ? null : h('div', { class: 'mudo pequeno' }, 'sem chave')),
        niveis.map((n) => {
          const valor = ((cfg.padroes || {})[id] || {})[n] || '';
          return h('td', { 'data-rotulo': `Modelo de ${n}` }, seletorModelo(valor, modelosDe(ctx, id),
            // vazio não vale como padrão de provedor: null apaga a escolha e volta ao padrão do sistema
            (m) => gravarModelos(ctx, { padroes: { [id]: { [n]: m || null } } }, `${nomeLlm(ctx, id)} · ${n}: ${m || 'padrão do sistema'}`), 'padrão do sistema'),
          avisoPreco(dados, valor));
        })))))),
    h('div', { class: 'grade', style: { marginTop: '1rem' } },
      h('div', { class: 'campo' }, h('label', {}, 'Busca na web'),
        h('select', {
          onchange: (e) => gravarModelos(ctx, { busca: { provedor: e.target.value } }, `Busca na web: ${e.target.value ? nomeLlm(ctx, e.target.value) : 'o provedor do agente'}`),
        }, h('option', { value: '', selected: !(cfg.busca || {}).provedor }, 'o mesmo provedor do agente que busca'), opcoesLlm(ctx, (cfg.busca || {}).provedor)),
        h('div', { class: 'ajuda' }, 'Os pesquisadores buscam com a busca nativa do provedor. Escolha um aqui para todas as buscas passarem por ele.'))));

  // a mesma escolha para todas as etapas de uma vez
  const todas = h('select', {}, h('option', { value: '' }, 'aplicar o mesmo provedor a todas as etapas…'), opcoesLlm(ctx, null));
  todas.addEventListener('change', () => {
    const prov = todas.value;
    if (!prov) return;
    const proprios = Object.keys(cfg.agentes || {}).filter((a) => (cfg.agentes[a] || {}).provedor);
    if (!confirm(`Usar ${nomeLlm(ctx, prov)} em todas as etapas, com o modelo padrão de cada nível?${proprios.length ? `\n\nOs agentes com escolha própria (${proprios.join(', ')}) continuam com ela.` : ''}`)) {
      todas.value = '';
      return;
    }
    gravarModelos(ctx, { etapas: Object.fromEntries(etapas.map((e) => [e.id, { provedor: prov, modelo: '' }])) }, `Todas as etapas: ${nomeLlm(ctx, prov)}`);
  });

  const linhas = etapas.flatMap((e) => linhasEtapa(ctx, e, automatico));
  const tabela = h('section', { class: 'card' },
    h('div', { class: 'linha-topo' }, h('h2', {}, 'Modelo de cada etapa'), h('span', { class: 'espaco' }), h('div', { style: { minWidth: '260px' } }, todas)),
    h('p', { class: 'mudo pequeno' }, 'Cada etapa roda com os agentes dela. Escolha o provedor (Claude, GPT ou Gemini) e, se quiser, um modelo fixo; abra a etapa para dar a um agente um provedor e um modelo só dele. A escolha de um agente vale em todas as etapas em que ele aparece. Na produção, o modelo só revisa as imagens e escreve os textos de publicação: as imagens e a narração seguem a sub-aba Imagens e voz.'),
    automatico ? null : h('div', { class: 'faixa alerta' }, 'Nenhum provedor de LLM tem chave: nenhuma etapa com agentes consegue rodar.'),
    h('div', { class: 'rolagem' }, h('table', { class: 'tabela-etapas tabela-modelos' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Etapa'), h('th', {}, 'Provedor'), h('th', {}, 'Modelo'))),
      h('tbody', {}, linhas))));

  trocar(raiz, tabela, padroes);
}

function linhasEtapa(ctx, e, automatico) {
  const { dados } = ctx;
  const cfg = dados.modelos;
  const escolha = (cfg.etapas || {})[e.id] || {};
  const prov = escolha.provedor || '';
  const efetivo = prov || automatico;
  const agentes = e.agentes || [];
  const nivelDoPadrao = [...new Set(agentes.map((a) => a.nivel).filter(Boolean))];
  const padroesProv = (cfg.padroes || {})[efetivo] || {};
  const rotuloPadrao = nivelDoPadrao.length
    ? `padrão do nível (${nivelDoPadrao.map((n) => `${n}: ${padroesProv[n] || '?'}`).join(' · ')})` : 'padrão do provedor';
  const nomeEtapaCfg = e.nome || e.id;

  const selProv = h('select', {
    onchange: (ev) => gravarModelos(ctx, { etapas: { [e.id]: { provedor: ev.target.value, modelo: '' } } },
      `${nomeEtapaCfg}: ${ev.target.value ? nomeLlm(ctx, ev.target.value) : 'automático'}`),
  },
  h('option', { value: '', selected: !prov }, `automático (${automatico ? nomeLlm(ctx, automatico) : 'nenhum com chave'})`),
  opcoesLlm(ctx, prov));

  // com "automático", escolher um modelo fixa também o provedor (senão o modelo mudaria de dono sozinho)
  const selModelo = efetivo ? seletorModelo(escolha.modelo || '', modelosDe(ctx, efetivo),
    (m) => gravarModelos(ctx, { etapas: { [e.id]: { provedor: prov || efetivo, modelo: m } } }, `${nomeEtapaCfg}: ${m || rotuloPadrao}`),
    rotuloPadrao) : h('span', { class: 'pequeno texto-alerta' }, 'cadastre uma chave de LLM');

  const resolvidos = agentes.map((a) => resolverRota(ctx, e.id, a));
  const semPreco = [...new Set(resolvidos.map((r) => r.modelo).filter((m) => m && !(dados.precos || []).includes(m)))];
  const semChave = [...new Set(resolvidos.map((r) => r.provedor).filter((p) => p && !ctx.temChave(p)))];
  const proprios = agentes.filter((a) => ((cfg.agentes || {})[a.nome] || {}).provedor).length;
  const problemas = e.problemas || [];

  const aberta = etapasAbertas.has(e.id);
  const alternar = h('button', {
    type: 'button', class: 'btn pequeno alternar-agentes', 'aria-expanded': aberta ? 'true' : 'false',
    onclick: () => {
      if (etapasAbertas.has(e.id)) etapasAbertas.delete(e.id); else etapasAbertas.add(e.id);
      const agora = etapasAbertas.has(e.id);
      linhaAgentes.hidden = !agora;
      alternar.setAttribute('aria-expanded', agora ? 'true' : 'false');
      alternar.firstChild.textContent = agora ? '▾' : '▸';
    },
  }, aberta ? '▾' : '▸', ` ${agentes.length} agente${agentes.length === 1 ? '' : 's'}${proprios ? ` · ${proprios} com escolha própria` : ''}`);

  const linha = h('tr', {},
    h('td', {}, h('strong', {}, nomeEtapaCfg), agentes.length ? h('div', { style: { marginTop: '.3rem' } }, alternar) : null),
    h('td', { 'data-rotulo': 'Provedor' }, selProv),
    h('td', { 'data-rotulo': 'Modelo' }, h('div', { class: 'modelo-etapa' }, selModelo,
      listouModelos(ctx, efetivo) || !efetivo ? null : h('div', { class: 'pequeno mudo' }, h('a', { href: '#/configuracoes/chaves' }, 'teste a chave'), ' para listar todos os modelos'),
      semChave.length ? h('div', { class: 'pequeno texto-erro' }, `sem chave: ${semChave.map((p) => nomeLlm(ctx, p)).join(', ')}`) : null,
      semPreco.length ? h('div', { class: 'pequeno texto-alerta' }, `sem preço na tabela (o custo fica fora do total): ${semPreco.join(', ')}`) : null,
      problemas.map((p) => h('div', { class: 'pequeno texto-erro' }, p)))));

  const linhaAgentes = h('tr', { class: 'linha-agentes', hidden: !aberta },
    h('td', { colspan: '3' }, h('div', { class: 'agentes-etapa' }, agentes.map((a, i) => linhaAgente(ctx, a, resolvidos[i])))));
  return [linha, linhaAgentes];
}

function linhaAgente(ctx, agente, resolvido) {
  const cfg = ctx.dados.modelos;
  const proprio = (cfg.agentes || {})[agente.nome] || {};
  const prov = proprio.provedor || '';
  const selProv = h('select', {
    onchange: (ev) => {
      const v = ev.target.value;
      gravarModelos(ctx, { agentes: { [agente.nome]: v ? { provedor: v, modelo: '' } : null } },
        `${agente.nome}: ${v ? nomeLlm(ctx, v) : 'herda da etapa'}`);
    },
  }, h('option', { value: '', selected: !prov }, 'herdar da etapa'), opcoesLlm(ctx, prov));
  const padraoNivel = ((cfg.padroes || {})[prov] || {})[agente.nivel];
  const selModelo = prov ? seletorModelo(proprio.modelo || '', modelosDe(ctx, prov),
    (m) => gravarModelos(ctx, { agentes: { [agente.nome]: { provedor: prov, modelo: m } } }, `${agente.nome}: ${m || 'padrão do nível'}`),
    `padrão do nível (${padraoNivel || '?'})`) : null;
  const semPreco = resolvido.modelo && !(ctx.dados.precos || []).includes(resolvido.modelo);
  return h('div', { class: 'agente-modelo' },
    h('div', { class: 'agente-nome' }, h('strong', {}, agente.nome), ' ', agente.nivel ? h('span', { class: 'etiqueta' }, agente.nivel) : null,
      agente.descricao ? h('div', { class: 'mudo pequeno descricao-agente', title: agente.descricao }, agente.descricao) : null),
    h('div', {}, selProv),
    h('div', {}, selModelo),
    h('div', { class: 'pequeno efetivo' }, h('span', { class: 'mudo' }, `usa (${resolvido.fonte}): `),
      resolvido.provedor ? h('strong', {}, `${nomeLlm(ctx, resolvido.provedor)} · ${resolvido.modelo || '?'}`) : h('span', { class: 'texto-erro' }, 'nenhum provedor com chave'),
      resolvido.provedor && !ctx.temChave(resolvido.provedor) ? h('span', { class: 'texto-erro' }, ' · sem chave') : null,
      semPreco ? h('span', { class: 'texto-alerta' }, ' · sem preço') : null));
}

// ---------------------------------------------------------------- configurações · imagens e voz

function subabaMidia(raiz, ctx) {
  ctx.redesenhar = () => subabaMidia(raiz, ctx);
  const { dados } = ctx;
  const cfg = dados.modelos;
  const img = cfg.imagem || {};
  const provsImg = (dados.imagem && dados.imagem.provedores) || [];
  const infoImg = provsImg.find((p) => p.id === img.provedor) || provsImg[0] || { modelos_por_qualidade: {} };
  const mpq = infoImg.modelos_por_qualidade || {};
  const conhecidosImg = [...(infoImg.modelos || []), ...Object.values(mpq)];
  const precisaChave = (p) => p.precisa_chave !== false && !ctx.temChave(p.id);

  const imagens = h('section', { class: 'card' }, h('h2', {}, 'Imagens'),
    h('p', { class: 'mudo pequeno' }, 'Quem gera as cenas, as fichas dos personagens e a thumbnail. Cada vídeo escolhe a qualidade no pedido (campo Qualidade das imagens); aqui fica o modelo de cada qualidade.'),
    h('div', { class: 'grade' },
      h('div', { class: 'campo' }, h('label', {}, 'Provedor de imagem'),
        h('select', {
          onchange: (e) => gravarModelos(ctx, { imagem: { provedor: e.target.value, modelos: { rascunho: '', padrao: '', maxima: '' } } },
            `Imagens: ${(provsImg.find((p) => p.id === e.target.value) || {}).nome || e.target.value}`),
        }, provsImg.map((p) => h('option', { value: p.id, selected: p.id === infoImg.id, disabled: precisaChave(p) && p.id !== infoImg.id },
          p.nome + (precisaChave(p) ? ' (sem chave)' : '')))),
        infoImg.id && precisaChave(infoImg) ? h('div', { class: 'pequeno texto-erro' }, 'Sem chave para este provedor: a produção não consegue gerar imagens.') : null),
      h('div', { class: 'campo' }, h('label', {}, 'Tamanho'),
        h('select', { onchange: (e) => gravarModelos(ctx, { imagem: { tamanho: e.target.value } }, `Tamanho das imagens: ${e.target.value}`) },
          (infoImg.tamanhos || TAMANHOS_IMAGEM).map((t) => h('option', { value: t, selected: t === (img.tamanho || '1K') }, t))),
        h('div', { class: 'ajuda' }, 'Resolução das imagens geradas. Maior custa mais. Quando o provedor não tem esse tamanho, vai o mais próximo.')),
      Object.entries(QUALIDADES).map(([q, nome]) => {
        const valor = (img.modelos || {})[q] || '';
        const efetivo = valor || mpq[q] || infoImg.modelo_padrao || '';
        return h('div', { class: 'campo' }, h('label', {}, `Qualidade ${nome.toLowerCase()}`),
          seletorModelo(valor, conhecidosImg, (m) => gravarModelos(ctx, { imagem: { modelos: { [q]: m } } }, `Qualidade ${nome.toLowerCase()}: ${m || 'padrão'}`),
            `padrão: ${mpq[q] || infoImg.modelo_padrao || '?'}`),
          avisoPreco(dados, efetivo));
      }),
      h('div', { class: 'campo' }, h('label', {}, 'Imagens ao mesmo tempo'),
        campoParalelo(img.paralelo, 6, (n) => gravarModelos(ctx, { imagem: { paralelo: n } }, `Imagens: ${n} ao mesmo tempo`)),
        h('div', { class: 'ajuda' }, 'Chamadas abertas ao provedor de uma vez; cada cena começa assim que as fichas dela ficam prontas. Quando o provedor limita a taxa, o sistema espera e tenta de novo.'))));

  const ani = cfg.animacao || {};
  const provsVideo = (dados.animacao && dados.animacao.provedores) || [];
  const infoVideo = provsVideo.find((p) => p.id === ani.provedor) || provsVideo[0] || { modelos: [], resolucoes: ['720p'] };
  const resolucaoAtual = ani.resolucao || infoVideo.resolucao_padrao || '720p';
  const animacao = h('section', { class: 'card' }, h('h2', {}, 'Animação das imagens'),
    h('p', { class: 'mudo pequeno' }, 'Anima levemente as imagens geradas das cenas quando o pedido do vídeo liga o campo Animação das imagens (destaques ou todas). Cada clipe parte da imagem da cena e entra no lugar do zoom. Antes de gastar, a produção mostra a estimativa e pede confirmação: cerca de US$ 0,10 por segundo animado a 720p. Só em 16:9 e 9:16; nas outras proporções, e nas cenas que o filtro recusar, a imagem fica parada.'),
    h('div', { class: 'grade' },
      h('div', { class: 'campo' }, h('label', {}, 'Provedor de vídeo'),
        h('select', {
          onchange: (e) => gravarModelos(ctx, { animacao: { provedor: e.target.value, modelo: '' } },
            `Animação: ${(provsVideo.find((p) => p.id === e.target.value) || {}).nome || e.target.value}`),
        }, provsVideo.map((p) => h('option', { value: p.id, selected: p.id === infoVideo.id, disabled: precisaChave(p) && p.id !== infoVideo.id },
          p.nome + (precisaChave(p) ? ' (sem chave)' : '')))),
        infoVideo.id && precisaChave(infoVideo) ? h('div', { class: 'pequeno texto-erro' }, 'Sem chave para este provedor: a produção não consegue animar as imagens.') : null),
      h('div', { class: 'campo' }, h('label', {}, 'Modelo'),
        seletorModelo(ani.modelo || '', infoVideo.modelos, (m) => gravarModelos(ctx, { animacao: { modelo: m } }, `Animação: ${m || 'modelo padrão'}`),
          `padrão: ${infoVideo.modelo_padrao || '?'}`),
        avisoPreco(dados, ani.modelo || infoVideo.modelo_padrao)),
      h('div', { class: 'campo' }, h('label', {}, 'Resolução'),
        h('select', { onchange: (e) => gravarModelos(ctx, { animacao: { resolucao: e.target.value } }, `Resolução da animação: ${e.target.value}`) },
          (infoVideo.resolucoes || ['720p']).map((r) => h('option', { value: r, selected: r === resolucaoAtual },
            r + (r === infoVideo.resolucao_padrao ? ' (padrão)' : '')))),
        h('div', { class: 'ajuda' }, 'O modelo gera em 720p; 1080p e 4K são ampliadas a partir dela, e só a 720p tem preço publicado.')),
      h('div', { class: 'campo' }, h('label', {}, 'Cenas ao mesmo tempo'),
        campoParalelo(ani.paralelo, 4, (n) => gravarModelos(ctx, { animacao: { paralelo: n } }, `Animação: ${n} cena(s) ao mesmo tempo`)),
        h('div', { class: 'ajuda' }, 'Cada clipe leva um ou dois minutos: animar várias cenas de uma vez encurta a produção.'))));

  const narr = cfg.narracao || {};
  const fornecedores = (dados.narracao && dados.narracao.fornecedores) || [];
  const vozesEleven = ((opcoes && opcoes.vozes_por_fornecedor) || {}).elevenlabs || [];
  const situacao = (f) => (f.precisa_chave === false ? h('span', { class: 'etiqueta ok' }, 'grátis')
    : ctx.temChave(f.id) ? h('span', { class: 'etiqueta ok' }, f.id === 'elevenlabs' ? `${vozesEleven.length} vozes da conta` : 'pronto')
      : h('span', { class: 'etiqueta' }, 'sem chave'));
  const narracao = h('section', { class: 'card' }, h('h2', {}, 'Narração'),
    h('p', { class: 'mudo pequeno' }, 'O fornecedor padrão vale quando o pedido do vídeo não escolhe outro no campo Fornecedor da voz; o modelo de cada fornecedor vem daqui. Uma voz de outro fornecedor vira uma voz do mesmo gênero no escolhido. O espeak é grátis e roda no servidor, com voz robótica: serve para testar o vídeo antes de pagar.'),
    h('div', { class: 'grade' }, h('div', { class: 'campo' }, h('label', {}, 'Fornecedor padrão'),
      h('select', { onchange: (e) => gravarModelos(ctx, { narracao: { fornecedor: e.target.value } }, `Narração: ${(fornecedores.find((f) => f.id === e.target.value) || {}).nome || e.target.value}`) },
        fornecedores.map((f) => h('option', { value: f.id, selected: f.id === narr.fornecedor, disabled: precisaChave(f) && f.id !== narr.fornecedor },
          f.nome + (precisaChave(f) ? ' (sem chave)' : '')))))),
    h('div', { class: 'rolagem', style: { marginTop: '1rem' } }, h('table', { class: 'tabela-etapas tabela-narracao' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Fornecedor'), h('th', {}, 'Situação'), h('th', {}, 'Modelo'))),
      h('tbody', {}, fornecedores.map((f) => {
        const valor = ((narr.modelos || {})[f.id]) || '';
        const temModelos = (f.modelos || []).length > 0;
        return h('tr', {}, h('td', {}, h('strong', {}, f.nome)), h('td', {}, situacao(f)),
          h('td', { 'data-rotulo': 'Modelo' }, temModelos || valor ? seletorModelo(valor, f.modelos, (m) => gravarModelos(ctx, { narracao: { modelos: { [f.id]: m } } }, `${f.nome}: ${m || 'modelo padrão'}`),
            `padrão: ${f.modelo_padrao || '?'}`) : h('span', { class: 'mudo pequeno' }, 'sem escolha de modelo'),
          avisoPreco(dados, f.precisa_chave === false ? '' : valor || (temModelos ? f.modelo_padrao : ''))));
      })))));

  const whisper = dados.whisper || [];
  const atualWhisper = (cfg.alinhamento || {}).modelo || '';
  const alinhamento = h('section', { class: 'card' }, h('h2', {}, 'Alinhamento da voz'),
    h('p', { class: 'mudo pequeno' }, 'Depois da narração, o reconhecimento de fala (whisper) mede o tempo de cada palavra, para as legendas e a troca das imagens, e confere se a voz disse exatamente o roteiro. Modelos maiores erram menos, mas são bem mais lentos (rodam no processador do servidor) e ocupam mais memória.'),
    h('div', { class: 'grade' }, h('div', { class: 'campo' }, h('label', {}, 'Modelo do whisper'),
      h('select', { onchange: (e) => gravarModelos(ctx, { alinhamento: { modelo: e.target.value } }, `Alinhamento: ${e.target.value}`) },
        atualWhisper && !whisper.includes(atualWhisper) ? h('option', { value: atualWhisper, selected: true }, atualWhisper) : null,
        whisper.map((m) => h('option', { value: m, selected: m === atualWhisper }, m))))));

  trocar(raiz, imagens, animacao, narracao, alinhamento);
}

// ---------------------------------------------------------------- configurações · efeitos sonoros

// A biblioteca de efeitos: um nome e um arquivo de áudio cada. O diretor visual escolhe pelo nome os que tocam em
// momentos do vídeo, e a montagem os mistura à voz e à música. O id sai do nome no cadastro e não muda depois
// (as cenas guardam o id): renomear muda só o nome.
async function subabaEfeitos(raiz, ctx) {
  ctx.redesenhar = () => subabaEfeitos(raiz, ctx).catch(falhou);
  const dados = await api('GET', '/api/efeitos');
  const lista = dados.efeitos || [];
  const nome = h('input', { type: 'text', placeholder: 'Nome do efeito, ex.: Impacto grave', maxlength: 80 });
  const arquivo = h('input', {
    type: 'file', accept: (dados.extensoes || []).join(',') || 'audio/*', onchange: () => {
      // sem nome escrito, o do arquivo (sem a extensão, com espaço no lugar de - e _) é um começo
      const f = arquivo.files[0];
      if (f && !nome.value.trim()) nome.value = f.name.replace(/\.[^.]+$/, '').replace(/[-_]+/g, ' ').trim();
    },
  });
  const adicionar = h('button', {
    class: 'btn primario', onclick: async () => {
      const f = arquivo.files[0];
      if (!f) return aviso('Escolha o arquivo de áudio do efeito.');
      if (!nome.value.trim()) { nome.focus(); return aviso('Dê um nome ao efeito.'); }
      const corpo = new FormData();
      corpo.append('nome', nome.value.trim());
      corpo.append('arquivo', f);
      adicionar.disabled = true;
      try {
        const e = await api('POST', '/api/efeitos', corpo);
        aviso(`Efeito "${e.nome}" guardado na biblioteca.`, 'ok');
        await ctx.redesenhar();
      } catch (e) { adicionar.disabled = false; falhou(e); }
    },
  }, 'Adicionar efeito');
  nome.addEventListener('keydown', (e) => { if (e.key === 'Enter') adicionar.click(); });

  const cartao = (e) => {
    const url = `/api/efeitos/${encodeURIComponent(e.id)}`;
    const campoNome = h('input', {
      type: 'text', value: e.nome, maxlength: 80, 'aria-label': `Nome do efeito ${e.id}`, onchange: async (ev) => {
        const novo = ev.target.value.trim();
        if (!novo || novo === e.nome) { ev.target.value = e.nome; return; }
        try {
          Object.assign(e, await api('PUT', url, { nome: novo }));
          aviso(`Nome salvo: ${e.nome}.`, 'ok');
        } catch (err) { ev.target.value = e.nome; falhou(err); }
      },
    });
    const apagar = h('button', {
      class: 'btn pequeno perigo', onclick: async () => {
        if (!confirm(`Apagar o efeito "${e.nome}"? Os vídeos já montados continuam com ele; numa montagem nova, as cenas que o pediam ficam sem efeito.`)) return;
        try { await api('DELETE', url); await ctx.redesenhar(); } catch (err) { falhou(err); }
      },
    }, 'Apagar');
    const duracao = e.duracao_s != null ? ` · ${Number(e.duracao_s).toLocaleString('pt-BR', { maximumFractionDigits: 1 })} s` : '';
    return h('div', { class: 'anexo-audio' },
      h('div', { class: 'linha' }, h('div', { style: { flex: '1', minWidth: '200px' } }, campoNome), apagar),
      h('div', { class: 'mudo pequeno' }, h('code', {}, e.id), duracao),
      h('audio', { controls: true, preload: 'none', src: urlDoArquivo(e.caminho) }));
  };

  trocar(raiz,
    h('section', { class: 'card' }, h('h2', {}, 'Novo efeito'),
      h('p', { class: 'mudo pequeno' }, 'Sons curtos para momentos estratégicos do vídeo: um impacto na virada, um whoosh na troca de cena, uma batida de coração na tensão. O diretor visual escolhe pelo nome, então dê nomes que digam o som ("Impacto grave", "Whoosh rápido", "Caixa registradora"). Na montagem, cada efeito toca no corte da cena ou numa palavra da narração, com o volume acertado em relação à voz. Valem para a direção visual feita daqui em diante.'),
      h('div', { class: 'grade' },
        h('div', { class: 'campo' }, h('label', {}, 'Nome'), nome),
        h('div', { class: 'campo' }, h('label', {}, 'Arquivo de áudio'), arquivo,
          h('div', { class: 'ajuda' }, `Até ${dados.limite_mb || 20} MB. Use sons com licença para uso comercial (a Biblioteca de Áudio do YouTube Studio tem efeitos).`))),
      h('div', { class: 'linha', style: { marginTop: '.8rem' } }, adicionar)),
    h('section', { class: 'card' }, h('h2', {}, lista.length ? `Biblioteca (${lista.length})` : 'Biblioteca'),
      lista.length ? h('div', { class: 'anexos-audios' }, lista.map(cartao))
        : h('p', { class: 'mudo pequeno' }, 'Nenhum efeito ainda. Sem efeitos na biblioteca, os vídeos saem só com a narração e a música.')));
}

// ---------------------------------------------------------------- configurações · sistema

async function subabaSistema(raiz, ctx) {
  ctx.redesenhar = ctx.recarregar;
  const s = ctx.dados.sistema || {};
  let saude;
  // com algo fora do ar a saúde responde 503, mas o corpo ainda diz o que está ok e o que falhou
  try { saude = await api('GET', '/api/saude'); } catch (e) {
    const corpo = e.dados && typeof e.dados === 'object' ? e.dados : {};
    const det = corpo.detail && typeof corpo.detail === 'object' ? corpo.detail : corpo;
    saude = { ...det, ok: false, erro: e.message };
  }
  const estadoDe = (v) => (v === true || v === 'ok' ? h('span', { class: 'etiqueta ok' }, 'ok')
    : v === false ? h('span', { class: 'etiqueta erro' }, 'falhou')
      : v == null ? h('span', { class: 'etiqueta' }, '—')
        : typeof v === 'object' ? (v.ok ? h('span', { class: 'etiqueta ok' }, v.mensagem || 'ok') : h('span', { class: 'etiqueta erro' }, v.mensagem || v.erro || 'falhou'))
          : h('span', { class: 'etiqueta erro' }, String(v)));
  const conhecidos = ['armazenamento', 'bucket', 'banco'];
  const extras = Object.entries(s).filter(([k, v]) => !conhecidos.includes(k) && v != null && v !== '');
  trocar(raiz,
    h('section', { class: 'card' }, h('div', { class: 'linha-topo' }, h('h2', {}, 'Saúde'), h('span', { class: 'espaco' }),
      h('button', { class: 'btn pequeno', onclick: () => ctx.recarregar() }, 'Conferir de novo')),
    h('dl', { class: 'lista-sistema' },
      h('dt', {}, 'Servidor'), h('dd', {}, estadoDe(saude.ok)),
      h('dt', {}, 'Banco'), h('dd', {}, estadoDe(saude.banco)),
      h('dt', {}, 'Armazenamento'), h('dd', {}, estadoDe(saude.armazenamento))),
    saude.erro ? h('div', { class: 'faixa erro', style: { marginTop: '.6rem' } }, saude.erro) : null),
    h('section', { class: 'card' }, h('h2', {}, 'Onde ficam os dados'),
      h('p', { class: 'mudo pequeno' }, 'Só leitura: estes valores vêm das variáveis de ambiente do servidor. Os arquivos dos vídeos ficam no armazenamento; o estado, os custos, os logs e o índice dos arquivos, no banco.'),
      h('dl', { class: 'lista-sistema' },
        h('dt', {}, 'Armazenamento'), h('dd', {}, ARMAZENAMENTOS[s.armazenamento] || texto(s.armazenamento) || '—'),
        h('dt', {}, 'Bucket'), h('dd', {}, s.bucket ? h('code', {}, s.bucket) : '—'),
        h('dt', {}, 'Banco'), h('dd', {}, s.banco ? h('code', {}, texto(s.banco)) : '—'),
        extras.flatMap(([k, v]) => [h('dt', {}, k.replace(/_/g, ' ')), h('dd', {}, typeof v === 'object' ? JSON.stringify(v) : texto(v))]))));
}

// ---------------------------------------------------------------- contextos salvos

function urlDoContexto(id, arquivo) {
  return `/api/contextos/${encodeURIComponent(id)}/arquivos/${encodeURIComponent(arquivo)}`;
}

function contagemAnexos(x) {
  const n = x.imagens.length;
  const partes = [n && `${n} imagem${n > 1 ? 'ns' : ''}`, x.audios && `${x.audios} áudio${x.audios > 1 ? 's' : ''}`].filter(Boolean);
  return partes.join(', ');
}

async function recarregarContextos() {
  opcoes.contextos = await api('GET', '/api/contextos');
}

async function telaContextos() {
  document.getElementById('topo-projeto').textContent = '· Contextos';
  await recarregarContextos();
  const nome = h('input', { type: 'text', placeholder: 'Nome do contexto, ex.: Novela das frutas' });
  const criar = async () => {
    if (!nome.value.trim()) { nome.focus(); return aviso('Dê um nome ao contexto.'); }
    try {
      const c = await api('POST', '/api/contextos', { nome: nome.value.trim() });
      location.hash = `#/contextos/${encodeURIComponent(c.id)}`;
    } catch (e) { falhou(e); }
  };
  nome.addEventListener('keydown', (e) => { if (e.key === 'Enter') criar(); });
  trocar(app,
    h('div', { class: 'card' },
      h('h1', {}, 'Novo contexto'),
      h('p', { class: 'mudo' }, 'Material de apoio que se repete entre vídeos: o universo de uma série, os personagens, a estética, referências. Texto, imagens e áudios. Na configuração de um vídeo, escolha o contexto no campo Contexto.'),
      h('div', { class: 'linha' }, h('div', { style: { flex: '1', minWidth: '240px' } }, nome),
        h('button', { class: 'btn primario', onclick: criar }, 'Criar contexto'))),
    h('h2', { style: { margin: '1.4rem 0 .7rem' } }, 'Contextos salvos'),
    opcoes.contextos.length ? h('div', { class: 'lista-projetos' }, opcoes.contextos.map((x) =>
      h('a', { class: 'projeto', href: `#/contextos/${encodeURIComponent(x.id)}` },
        h('div', { class: 'card' },
          h('strong', {}, x.nome),
          h('div', { class: 'mudo pequeno contexto-resumo' }, x.resumo || 'sem texto'),
          x.imagens.length ? h('div', { class: 'miniaturas pequenas' }, x.imagens.slice(0, 5).map((a) =>
            h('div', { class: 'miniatura' }, h('img', { src: urlDoContexto(x.id, a), alt: '' })))) : null,
          h('div', { class: 'linha', style: { marginTop: '.5rem' } },
            x.legado ? h('span', { class: 'etiqueta' }, 'arquivo de texto') : contagemAnexos(x) ? h('span', { class: 'etiqueta' }, contagemAnexos(x)) : null,
            h('span', { class: 'mudo pequeno' }, quando(x.atualizado_em))))))) :
      h('p', { class: 'mudo' }, 'Nenhum contexto ainda.'),
  );
  nome.focus();
}

async function telaContexto(id) {
  let c = await api('GET', `/api/contextos/${encodeURIComponent(id)}`);
  document.getElementById('topo-projeto').textContent = `· Contextos · ${c.nome}`;
  const voltar = h('a', { href: '#/contextos', class: 'mudo pequeno' }, '← Contextos');

  if (c.legado) {
    const copiar = h('button', {
      class: 'btn', onclick: async () => {
        copiar.disabled = true;
        try {
          const novo = await api('POST', '/api/contextos', { nome: c.nome, texto: c.texto });
          location.hash = `#/contextos/${encodeURIComponent(novo.id)}`;
        } catch (e) { copiar.disabled = false; falhou(e); }
      },
    }, 'Copiar para um contexto novo');
    trocar(app, voltar, h('h1', {}, c.nome),
      h('div', { class: 'faixa info' }, 'Contexto antigo, só de texto. A interface não edita esse formato; para mudar o texto ou anexar imagens e áudios, copie-o para um contexto novo.'),
      h('div', { class: 'linha', style: { marginBottom: '1rem' } }, copiar),
      h('section', { class: 'card' }, md(c.texto)));
    return;
  }

  const descricoes = {};
  const salvarJa = async () => {
    c = await api('PUT', `/api/contextos/${encodeURIComponent(id)}`, { nome: c.nome, texto: c.texto, descricoes });
    document.getElementById('topo-projeto').textContent = `· Contextos · ${c.nome}`;
    await recarregarContextos();
  };
  const salvar = debounce(() => salvarJa().catch(falhou), 600);

  const nome = h('input', { type: 'text', value: c.nome, oninput: (e) => { c.nome = e.target.value; salvar(); } });
  const texto = h('textarea', {
    class: 'grande', rows: 14, value: c.texto, 'data-auto': true,
    placeholder: 'O universo, os personagens, a estética, o que funciona e o que evitar. Os agentes leem este texto como material de apoio, não como instrução.',
    oninput: (e) => { c.texto = e.target.value; autoAltura(e.target); salvar(); },
  });
  const imagens = h('section', { class: 'card' });
  const audios = h('section', { class: 'card' });

  const enviar = async (arquivos) => {
    try {
      for (const f of arquivos) {
        const dados = new FormData();
        dados.append('arquivo', f);
        c = await api('POST', `/api/contextos/${encodeURIComponent(id)}/anexos`, dados);
      }
      await recarregarContextos();
      desenharAnexos();
    } catch (e) { falhou(e); desenharAnexos(); }
  };
  const remover = async (a) => {
    if (!confirm(`Tirar ${a.arquivo} do contexto? O arquivo é apagado.`)) return;
    try {
      c = await api('DELETE', `/api/contextos/${encodeURIComponent(id)}/anexos/${encodeURIComponent(a.arquivo)}`);
      delete descricoes[a.arquivo];
      await recarregarContextos();
      desenharAnexos();
    } catch (e) { falhou(e); }
  };
  const campoDescricao = (a, placeholder) => h('input', {
    type: 'text', value: a.descricao || '', placeholder,
    oninput: (e) => { a.descricao = descricoes[a.arquivo] = e.target.value; salvar(); },
  });
  const entrada = (accept, rotuloBotao) => h('label', { class: 'btn pequeno' }, rotuloBotao,
    h('input', { type: 'file', accept, multiple: true, hidden: true, onchange: (e) => enviar([...e.target.files]) }));

  function desenharAnexos() {
    const lista = (tipo) => c.anexos.filter((a) => a.tipo === tipo);
    trocar(imagens,
      h('div', { class: 'linha-topo' }, h('h2', {}, 'Imagens'), h('span', { class: 'espaco' }), entrada('image/png,image/jpeg,image/webp', '+ Imagens')),
      lista('imagem').length ? h('div', { class: 'anexos-imagens' }, lista('imagem').map((a) =>
        h('div', { class: 'anexo-imagem' },
          h('div', { class: 'miniatura grande' }, h('img', { src: urlDoContexto(id, a.arquivo), alt: a.descricao || a.arquivo }),
            h('button', { type: 'button', title: 'tirar', onclick: () => remover(a) }, '×')),
          campoDescricao(a, 'o que a imagem mostra')))) :
        h('p', { class: 'mudo pequeno' }, 'Nenhuma imagem. Personagens, estilo, lugares: o diretor visual olha cada uma antes de escrever a bíblia visual.'));
    trocar(audios,
      h('div', { class: 'linha-topo' }, h('h2', {}, 'Áudios'), h('span', { class: 'espaco' }), entrada('audio/*', '+ Áudios')),
      lista('audio').length ? h('div', { class: 'anexos-audios' }, lista('audio').map((a) =>
        h('div', { class: 'anexo-audio' },
          h('div', { class: 'linha' }, h('strong', { class: 'pequeno' }, a.arquivo), h('span', { class: 'espaco' }),
            h('button', { class: 'btn pequeno perigo', onclick: () => remover(a) }, 'Tirar')),
          h('audio', { controls: true, preload: 'none', src: urlDoContexto(id, a.arquivo) }),
          campoDescricao(a, 'o que é: a voz de um personagem, a trilha de referência, o que ela diz...')))) :
        h('p', { class: 'mudo pequeno' }, 'Nenhum áudio. Os agentes não ouvem o áudio: escreva na descrição o que ele é e para que serve.'));
  }

  const apagar = h('button', {
    class: 'btn perigo', onclick: async () => {
      if (!confirm(`Apagar o contexto "${c.nome}" com todos os anexos? Os vídeos que já geraram o brief guardam o texto, mas perdem os anexos.`)) return;
      try {
        await api('DELETE', `/api/contextos/${encodeURIComponent(id)}`);
        await recarregarContextos();
        location.hash = '#/contextos';
      } catch (e) { falhou(e); }
    },
  }, 'Apagar contexto');

  desenharAnexos();
  trocar(app, voltar,
    h('h1', {}, 'Contexto'),
    h('p', { class: 'mudo' }, 'Tudo é salvo enquanto você digita. Vale para os vídeos que escolherem este contexto, a partir da próxima vez que o pedido for confirmado.'),
    h('section', { class: 'card' }, h('div', { class: 'grade' },
      h('div', { class: 'campo largo' }, h('label', {}, 'Nome'), nome),
      h('div', { class: 'campo largo' }, h('label', {}, 'Texto'), texto))),
    imagens, audios,
    h('div', { class: 'rodape-acoes' }, apagar, h('span', { class: 'espaco' }),
      h('a', { class: 'btn', href: '#/contextos' }, 'Pronto')));
  autoAltura(texto);
}

// ---------------------------------------------------------------- projeto

const TELAS = {
  configurar: telaConfigurar, sugestoes: telaSugestoes, importar: telaImportar, pesquisa: telaPesquisa, angulos: telaAngulos,
  ganchos: telaGanchos, roteiro: telaRoteiro, visual: telaVisual, producao: telaProducao, edicao: telaEdicao,
};

async function abrirProjeto(slug, etapa) {
  estado = await api('GET', `/api/projetos/${encodeURIComponent(slug)}`);
  if (!etapa || !TELAS[etapa]) {
    // a etapa guardada pode ser uma que saiu da trilha (os cortes): vale a primeira que falta
    const salva = estado.projeto.etapa;
    etapa = TELAS[salva] ? salva : (estado.etapas.find((e) => e.aplicavel && !e.pronta) || {}).id || 'configurar';
    history.replaceState(null, '', `#/p/${encodeURIComponent(slug)}/${etapa}`);
  }
  document.getElementById('topo-projeto').textContent = `· ${estado.projeto.titulo}`;
  const corpo = h('div');
  trocar(app, trilha(etapa), h('div', { class: 'barra-projeto' }, painelFork(etapa)), faixaRodando(etapa), faixaDesatualizada(etapa), corpo);
  if (avisosPendentes[slug]) {
    corpo.append(h('div', { class: 'faixa alerta' }, h('strong', {}, 'Avisos do pedido: '), avisosPendentes[slug]));
    delete avisosPendentes[slug];
  }
  await TELAS[etapa](corpo, etapa);
  app.querySelectorAll('textarea[data-auto]').forEach(autoAltura);
}

function infoEtapa(id) { return estado.etapas.find((e) => e.id === id); }

function trilha(atual) {
  const ids = estado.etapas.map((e) => e.id);
  const maxAberta = Math.max(ids.indexOf(estado.projeto.etapa), ...estado.etapas.map((e, i) => (e.pronta || e.execucao ? i : -1)));
  return h('nav', { class: 'etapas' }, estado.etapas.map((e, i) => {
    const classes = ['etapa'];
    if (e.id === atual) classes.push('atual');
    else if (e.pronta && e.desatualizada) classes.push('desatualizada');
    else if (e.pronta) classes.push('feita');
    if (!e.aplicavel) classes.push('pulada');
    else if (i > maxAberta + 1 && e.id !== atual) classes.push('bloqueada');
    if (estado.rodando && telaDa(estado.rodando.etapa) === e.id) classes.push('rodando');
    const titulo = e.desatualizada ? `Desatualizada: ${e.desatualizada}` : e.descricao;
    const marca = e.pronta && e.id !== atual ? (e.desatualizada ? '!' : '✓') : i + 1;
    return h('a', { class: classes.join(' '), href: `#/p/${encodeURIComponent(estado.projeto.slug)}/${e.id}`, title: titulo },
      h('span', { class: 'num' }, marca), e.nome);
  }));
}

// Uma etapa feita antes de uma mudança numa anterior (outro ângulo, outro roteiro final): o que a tela mostra é da
// versão anterior, e o botão refaz a etapa com o que está valendo agora.
const CONFIRMAR_REFAZER = {
  producao: 'Produzir o vídeo de novo com a direção visual atual? As imagens e a narração que mudaram são geradas de novo (com custo).',
};
function faixaDesatualizada(id) {
  const info = infoEtapa(id);
  if (!info || !info.desatualizada || !info.aplicavel || (estado.rodando && telaDa(estado.rodando.etapa) === id)) return null;
  const refazer = () => {
    if (!confirm(CONFIRMAR_REFAZER[id] || `Refazer ${info.nome.toLowerCase()} com o que está valendo agora?`)) return;
    executar(id).catch(falhou);
  };
  return h('div', { class: 'faixa alerta linha' },
    h('span', { class: 'espaco' }, h('strong', {}, 'Desatualizada: '), `${info.desatualizada}. O que aparece abaixo é da versão anterior.`),
    estado.rodando ? null : h('button', { class: 'btn primario pequeno', onclick: refazer }, `Refazer ${info.nome.toLowerCase()}`));
}

// Fork: um projeto novo com tudo deste até a etapa escolhida, para refazer dali em diante sem mexer neste.
function painelFork(atual) {
  const opcoesFork = estado.etapas.filter((e) => e.aplicavel && (e.pronta || e.id === 'configurar'));
  const padrao = opcoesFork.some((e) => e.id === atual) ? atual : opcoesFork[opcoesFork.length - 1].id;
  const ate = h('select', {}, opcoesFork.map((e) => h('option', { value: e.id, selected: e.id === padrao }, e.nome)));
  const titulo = h('input', { type: 'text', value: `${estado.projeto.titulo} (fork)` });
  const botao = h('button', { class: 'btn primario', onclick: async () => {
    botao.disabled = true;
    try {
      const r = await api('POST', `/api/projetos/${encodeURIComponent(estado.projeto.slug)}/fork`, { ate: ate.value, titulo: titulo.value });
      aviso(`Fork criado: ${r.slug}. Daqui em diante, o que você refizer fica só nele.`, 'ok');
      irPara(r.slug, r.etapa);
    } catch (e) { falhou(e); botao.disabled = false; }
  } }, 'Criar fork');
  return h('details', { class: 'fork' }, h('summary', {}, 'Criar fork'),
    h('div', { class: 'card' },
      h('p', { class: 'mudo pequeno' }, 'Um projeto novo com tudo deste até a etapa escolhida, inclusive. As etapas seguintes ficam para refazer no fork; este projeto não muda.'),
      h('div', { class: 'grade-fork' },
        h('div', { class: 'campo' }, h('label', {}, 'Copiar até a etapa'), ate),
        h('div', { class: 'campo' }, h('label', {}, 'Título do fork'), titulo)),
      h('div', { class: 'linha', style: { marginTop: '.8rem' } }, h('span', { class: 'espaco' }), botao)));
}

function faixaRodando(atual) {
  const r = estado.rodando;
  if (!r || telaDa(r.etapa) === atual) return null;
  const naFila = r.status === 'pendente' || !r.iniciada_em;
  return h('div', { class: 'faixa info linha' }, h('span', { class: 'girando' }),
    h('span', {}, naFila ? `Na fila: ${nomeEtapa(r.etapa)}.` : `Rodando agora: ${nomeEtapa(r.etapa)} (${quando(r.iniciada_em)}).`),
    h('a', { href: `#/p/${encodeURIComponent(estado.projeto.slug)}/${telaDa(r.etapa)}` }, 'acompanhar'));
}

function proximaAplicavel(id) {
  const ids = estado.etapas.map((e) => e.id);
  return estado.etapas.slice(ids.indexOf(id) + 1).find((e) => e.aplicavel);
}

// Enfileira uma etapa e vai para a tela dela. Quando o servidor pede confirmação (409 com precisa: o roteiro
// final ainda tem [VERIFICAR], ou a produção vai gerar muitas imagens pagas), pergunta e reenvia confirmado.
async function executar(etapa, extras = {}) {
  const slug = estado.projeto.slug;
  try {
    await api('POST', `/api/projetos/${encodeURIComponent(slug)}/executar`, { etapa, ...extras });
  } catch (e) {
    const precisa = e.status === 409 ? confirmacaoPedida(e, etapa) : null;
    if (!precisa || extras[precisa]) throw e;
    if (!confirm(`${e.message}\n\nSeguir mesmo assim?`)) return;
    return executar(etapa, { ...extras, [precisa]: true });
  }
  irPara(slug, telaDa(etapa));
}

function confirmacaoPedida(e, etapa) {
  const precisa = campoDoErro(e, 'precisa');
  if (typeof precisa === 'string' && /^confirmar_[a-z_]+$/.test(precisa)) return precisa;
  // sem o campo: o 409 do [VERIFICAR] só explica no texto
  if (['visual', 'producao'].includes(etapa) && /VERIFICAR/.test(e.message)) return 'confirmar_verificar';
  return null;
}

// ---------------------------------------------------------------- execuções

// A cor de cada linha do log vem do prefixo que o motor de agentes escreve: ▶ agente, $ comando, ✎ arquivo
// gravado, · leitura, ⌕ busca, › fala do modelo, ✓ fim, ✗ erro, ■ cancelado, ⚠ aviso.
function linhaDeLog(l) {
  const t = l.trimStart();
  const sub = l.startsWith('    ');
  const cls = t.startsWith('▶') ? 'agente' : t.startsWith('$') ? 'cmd' : t.startsWith('✎') ? 'arquivo'
    : t.startsWith('✓') ? 'fim' : t.startsWith('✗') || t.startsWith('■') ? 'falha' : t.startsWith('⌕') ? 'busca'
      : t.startsWith('⚠') ? 'alerta' : t.startsWith('›') ? (sub ? 'sub' : 'fala') : 'sub';
  return h('span', { class: cls }, l + '\n');
}

function listaDeAvisos(avisos, titulo = 'Avisos') {
  const itens = (avisos || []).filter(Boolean);
  if (!itens.length) return null;
  return h('div', { class: 'faixa alerta' }, h('strong', {}, `${titulo}: `),
    itens.length === 1 ? texto(itens[0]) : h('ul', { class: 'lista-avisos' }, itens.map((a) => h('li', {}, typeof a === 'object' ? valorLegivel(a) : a))));
}

// Acompanha uma execução: o log chega aos poucos (o cursor é o tamanho que o servidor devolve) e, quando ela
// sai da fila e termina, chama aoTerminar com o estado final.
function painelLog(execucao, aoTerminar, { titulo = 'O sistema está trabalhando nesta etapa' } = {}) {
  const pre = h('pre', { class: 'log' });
  const relogio = h('span', { class: 'mudo pequeno' });
  const avisosBox = h('div');
  const tituloEl = h('h3', {}, titulo);
  let n = 0;
  const cancelar = h('button', {
    class: 'btn perigo pequeno', onclick: async () => {
      if (!confirm('Cancelar esta execução? O que já foi gravado fica guardado.')) return;
      cancelar.disabled = true;
      try {
        await api('POST', `/api/execucoes/${execucao.id}/cancelar`);
        aviso('Cancelamento pedido: a execução para no próximo passo.', 'info', 4000);
      } catch (e) { cancelar.disabled = false; falhou(e); }
    },
  }, 'Cancelar');
  const card = h('div', { class: 'card' },
    h('div', { class: 'linha-topo' }, h('span', { class: 'girando' }), tituloEl, relogio, h('span', { class: 'espaco' }), cancelar),
    h('p', { class: 'mudo pequeno' }, 'Pode fechar a página: a execução continua no servidor e o log fica guardado.'),
    avisosBox,
    pre);
  const passo = async () => {
    try {
      const e = await api('GET', `/api/execucoes/${execucao.id}?desde=${n}`);
      if (e.log) {
        const perto = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
        e.log.split('\n').filter(Boolean).forEach((l) => pre.append(linhaDeLog(l)));
        if (perto) pre.scrollTop = pre.scrollHeight;
      }
      if (e.tamanho != null) n = e.tamanho;
      relogio.textContent = e.status === 'pendente' ? 'na fila, esperando um trabalhador livre' : e.iniciada_em ? `começou ${quando(e.iniciada_em)}` : '';
      trocar(avisosBox, listaDeAvisos(e.avisos));
      if (!ativa(e.status)) {
        pararPolling();
        carimboImagens = Date.now();
        aoTerminar(e);
      }
    } catch (err) { console.error(err); }
  };
  pararPolling();
  polling = setInterval(passo, 1500);
  passo();
  return card;
}

function cartaoResumo(execucao) {
  if (!execucao || !execucao.resultado) return null;
  const custo = execucao.custo_usd ? ` · ${dolares(execucao.custo_usd)}` : '';
  return h('details', { class: 'card' }, h('summary', {}, `Resumo da execução (${quando(execucao.terminada_em)}${custo})`),
    listaDeAvisos(execucao.avisos), md(execucao.resultado), logGuardado(execucao.id));
}

// O log inteiro de uma execução que já terminou, só quando o usuário abre. O servidor entrega em pedaços
// (cursor pelo id da linha), então pede até não vir mais nada.
function logGuardado(id) {
  const det = h('details', { style: { marginTop: '.6rem' } }, h('summary', { class: 'pequeno' }, 'Log completo'));
  det.addEventListener('toggle', async () => {
    if (!det.open || det.dataset.carregado) return;
    det.dataset.carregado = '1';
    const pre = h('pre', { class: 'log' });
    det.append(pre);
    try {
      let cursor = 0;
      for (let i = 0; i < 200; i++) {
        const e = await api('GET', `/api/execucoes/${id}?desde=${cursor}`);
        if (e.log) pre.append(...e.log.split('\n').filter(Boolean).map(linhaDeLog));
        if (!e.log || e.tamanho == null || e.tamanho === cursor) break;
        cursor = e.tamanho;
      }
      if (!pre.childNodes.length) pre.append('(log vazio)');
    } catch (e) { pre.append(`Não consegui carregar o log: ${e.message}`); }
  });
  return det;
}

const FALHAS = {
  erro: 'A última execução terminou com erro.',
  cancelada: 'A última execução foi cancelada.',
  interrompida: 'A última execução foi interrompida (o trabalhador parou no meio, por exemplo num reinício do servidor).',
};

function cartaoFalha(exec, tentarDeNovo) {
  return h('div', { class: 'card' }, h('div', { class: 'faixa erro' }, FALHAS[exec.status] || `A última execução terminou: ${exec.status}.`),
    listaDeAvisos(exec.avisos), exec.resultado ? md(exec.resultado) : null, logGuardado(exec.id),
    tentarDeNovo ? h('div', { class: 'linha', style: { marginTop: '.8rem' } },
      h('button', { class: 'btn primario', onclick: () => tentarDeNovo().catch(falhou) }, 'Tentar de novo')) : null);
}

// Esqueleto comum das etapas que rodam no servidor: acompanha a execução, mostra o erro da última ou o botão
// de começar, e entrega os dados para a tela quando há o que mostrar.
async function etapaComExecucao(corpo, id, { titulo, explicacao, rotuloIniciar, iniciar, carregar, temDados, mostrar }) {
  const info = infoEtapa(id);
  const rodando = estado.rodando && telaDa(estado.rodando.etapa) === id ? estado.rodando : null;
  corpo.append(h('div', { class: 'linha-topo' }, h('h1', {}, titulo)));
  if (explicacao) corpo.append(h('p', { class: 'mudo' }, explicacao));
  if (!info.aplicavel) {
    corpo.append(h('div', { class: 'faixa info' }, 'Esta etapa não vale para este vídeo, pelo pedido aprovado.'));
  }
  if (rodando) {
    // avaliar, publicação e refazer cenas aparecem na tela da etapa a que pertencem, com o nome no título
    const tituloLog = rodando.etapa !== id ? `${nomeEtapa(rodando.etapa)}: o sistema está trabalhando` : undefined;
    corpo.append(painelLog(rodando, () => rota(), { titulo: tituloLog }));
    if (carregar && temDados) {
      const dados = await carregar();
      if (temDados(dados)) corpo.append(h('h2', { style: { marginTop: '1rem' } }, 'Até agora'), await mostrar(dados, { rodando: true }));
    }
    return;
  }
  const dados = carregar ? await carregar() : null;
  const exec = info.execucao;
  if (exec && FALHAS[exec.status]) corpo.append(cartaoFalha(exec, iniciar));
  if (dados && temDados(dados)) {
    corpo.append(cartaoResumo(exec && exec.status === 'ok' ? exec : null) || '', await mostrar(dados, { rodando: false }));
  } else if (!exec || exec.status === 'ok') {
    corpo.append(h('div', { class: 'card' }, h('p', {}, 'Esta etapa ainda não rodou.'),
      h('button', { class: 'btn primario', disabled: !info.aplicavel, onclick: () => iniciar().catch(falhou) }, rotuloIniciar)));
  }
}

// ---------------------------------------------------------------- campos do pedido

function placeholderDe(c) {
  if (c.quem === 'decisao' || c.quem === 'obrigatorio') return 'vazio: o sistema sugere';
  return c.padrao ? `padrão: ${c.padrao}` : '';
}

// Um só player para as amostras: tocar outra voz interrompe a anterior.
const player = new Audio();

// As vozes do fornecedor de narração do pedido na tela (configuração ou revisão das sugestões). O espeak usa os
// nomes do Gemini.
let fornecedorVoz = 'gemini';
function vozesAtuais() {
  const porFornecedor = opcoes.vozes_por_fornecedor || {};
  return porFornecedor[fornecedorVoz === 'espeak' ? 'gemini' : fornecedorVoz] || opcoes.vozes;
}

function tocarAmostra(nome) {
  const voz = vozesAtuais().find((v) => v.nome === nome);
  if (!voz || !voz.amostra) return;
  player.pause();
  player.src = voz.amostra;
  player.play().catch(() => {});
}

// Seletor de voz com as amostras de ~5 s de cada voz (as da biblioteca de vozes): o botão ▶ toca a
// voz escolhida (vazio: a padrão, se houver), trocar a voz já toca a amostra, e "ouvir todas as vozes" abre a
// lista com um ▶ por voz, para comparar antes de escolher.
function seletorVoz(valor, aoMudar, rotuloVazio, padrao = null) {
  const grupos = { f: 'Femininas', m: 'Masculinas', '?': 'Outras' };
  const vozes = vozesAtuais();
  const atual = () => sel.value || padrao;
  const temAmostra = (nome) => !!vozes.find((v) => v.nome === nome && v.amostra);
  const ouvir = h('button', { type: 'button', class: 'btn icone', onclick: () => tocarAmostra(atual()) }, '▶');
  const escolher = (nome) => { sel.value = nome; aoMudar(nome); sincronizar(); };
  const itens = vozes.filter((v) => v.amostra).map((v) => {
    const item = h('div', { class: 'voz-item', 'data-voz': v.nome },
      h('button', { type: 'button', class: 'btn icone', title: `ouvir ${v.nome}`, onclick: () => tocarAmostra(v.nome) }, '▶'),
      h('button', { type: 'button', class: 'voz-nome', title: 'usar esta voz', onclick: () => escolher(v.nome) },
        h('strong', {}, v.nome), h('span', { class: 'mudo' }, ` ${v.genero === 'f' ? '♀' : '♂'} ${v.descricao}`)));
    return item;
  });
  const sincronizar = () => {
    ouvir.disabled = !temAmostra(atual());
    ouvir.title = ouvir.disabled ? 'sem amostra gravada para esta voz' : `ouvir ${atual()}`;
    for (const i of itens) i.classList.toggle('selecionado', i.dataset.voz === atual());
  };
  const sel = h('select', {
    value: valor || '', onchange: (e) => { aoMudar(e.target.value); sincronizar(); tocarAmostra(atual()); },
  },
  h('option', { value: '' }, rotuloVazio),
  valor && !vozes.some((v) => v.nome === valor) ? h('option', { value: valor }, `${valor} (de outro fornecedor: vai uma voz do mesmo gênero)`) : null,
  Object.entries(grupos).map(([g, nome]) => vozes.some((v) => (v.genero || '?') === g) ? h('optgroup', { label: nome },
    vozes.filter((v) => (v.genero || '?') === g).map((v) => h('option', { value: v.nome }, `${v.nome}${v.descricao ? ` (${v.descricao})` : ''}`))) : null));
  sincronizar();
  return h('div', {}, h('div', { class: 'com-voz' }, sel, ouvir),
    itens.length ? h('details', { class: 'vozes-catalogo' }, h('summary', {}, 'ouvir todas as vozes'),
      h('div', { class: 'ajuda' }, '▶ toca a amostra; clique no nome para usar a voz.'), h('div', { class: 'vozes-lista' }, itens)) : null);
}

// Um campo do pedido, pelo tipo declarado em CAMPOS (ferramentas/pedido.py). get/set leem e gravam o valor.
function widgetCampo(c, get, set, extra = {}) {
  const v = get(c.chave);
  const ph = extra.placeholder ?? placeholderDe(c);
  let entrada;
  if (c.chave === 'narracao.voz') {
    entrada = seletorVoz(v, set, `padrão: ${opcoes.voz_do_canal || 'voz do canal'}`, opcoes.voz_do_canal);
  } else if (c.tipo === 'opcao' || c.tipo === 'booleano') {
    const lista = c.tipo === 'booleano' ? ['sim', 'não'] : c.opcoes;
    const atual = c.tipo === 'booleano' ? (boolDe(v) === null ? '' : boolDe(v) ? 'sim' : 'não') : v || '';
    entrada = h('select', {
      value: atual, onchange: (e) => {
        const x = e.target.value;
        set(c.tipo === 'booleano' ? (x === '' ? null : x === 'sim') : x || null);
      },
    }, OBRIGATORIOS.has(c.chave) ? null : h('option', { value: '' }, c.quem === 'decisao' ? '— o sistema sugere —' : c.padrao ? `padrão: ${c.padrao}` : '—'),
    lista.map((o) => h('option', { value: o }, (ROTULOS_OPCOES[c.chave] || {})[o] || o)));
  } else if (c.tipo === 'lista') {
    entrada = h('textarea', {
      rows: 3, placeholder: ph ? `${ph} · um por linha` : 'um por linha', value: (v || []).join('\n'),
      oninput: (e) => set(e.target.value.split('\n').map((s) => s.trim()).filter(Boolean)),
    });
  } else if (c.tipo === 'cor') {
    const txt = h('input', { type: 'text', value: v || '', placeholder: ph, oninput: (e) => { set(e.target.value || null); sincronizar(); } });
    const cor = h('input', { type: 'color', oninput: (e) => { txt.value = e.target.value.toUpperCase(); set(txt.value); } });
    const sincronizar = () => {
      const hex = opcoes.cores[norm(txt.value)] || txt.value || (c.padrao || '').match(/#[0-9a-f]{6}/i)?.[0] || '#ffffff';
      if (/^#[0-9a-f]{6}$/i.test(hex)) cor.value = hex.toLowerCase();
    };
    sincronizar();
    entrada = h('div', { class: 'com-cor' }, txt, cor);
  } else if (c.tipo === 'inteiro' || c.tipo === 'numero') {
    entrada = h('input', {
      type: 'number', step: c.tipo === 'inteiro' ? '1' : 'any', min: c.faixa ? c.faixa[0] : null, max: c.faixa ? c.faixa[1] : null,
      value: v ?? '', placeholder: ph, oninput: (e) => set(e.target.value === '' ? null : Number(e.target.value)),
    });
  } else if (LONGOS[c.chave]) {
    entrada = h('textarea', { rows: LONGOS[c.chave], 'data-auto': '1', value: v || '', placeholder: ph, oninput: (e) => { autoAltura(e.target); set(e.target.value); } });
  } else {
    entrada = h('input', { type: 'text', value: v ?? '', placeholder: c.tipo === 'tempo' ? `mm:ss · ${ph}` : ph, oninput: (e) => set(e.target.value) });
  }
  const ajuda = c.ajuda.split('\n')[0].replace(/\s*Padrão:.*$/, '');
  return h('div', { class: `campo ${extra.largo || LONGOS[c.chave] >= 3 ? 'largo' : ''}` },
    h('label', {}, h('span', { class: OBRIGATORIOS.has(c.chave) ? 'obrigatorio' : '' }, rotulo(c.chave)), extra.etiqueta),
    entrada,
    extra.motivo ? h('div', { class: 'motivo' }, extra.motivo) : null,
    extra.semAjuda ? null : h('div', { class: 'ajuda' }, ajuda));
}

function secoes() {
  const mapa = new Map();
  for (const c of opcoes.campos) {
    if (!mapa.has(c.secao)) mapa.set(c.secao, []);
    mapa.get(c.secao).push(c);
  }
  return mapa;
}

// ---------------------------------------------------------------- personagens

function avatar(p) {
  const bib = p.biblioteca && opcoes.personagens.find((x) => x.slug === p.biblioteca);
  return bib && bib.imagem ? h('img', { class: 'avatar', src: bib.imagem, alt: '' }) : h('div', { class: 'avatar' }, (p.nome || '?').slice(0, 1).toUpperCase());
}

function deBiblioteca(b) {
  return { nome: b.nome, voz: b.voz || '', estilo: b.estilo || '', descricao: b.descricao || '', cor: b.cor || '', biblioteca: b.slug };
}

// Seleção dos personagens salvos e lista de quem fala. lista é alterada no lugar; aoMudar avisa quem guarda.
function widgetPersonagens(lista, aoMudar, { motivos = false } = {}) {
  const raiz = h('div');
  const desenhar = () => {
    const grade = h('div', { class: 'biblioteca' }, opcoes.personagens.map((b) => {
      const i = lista.findIndex((p) => p.biblioteca === b.slug);
      return h('button', {
        type: 'button', class: `personagem ${i >= 0 ? 'selecionado' : ''}`, title: b.resumo,
        onclick: () => {
          if (i >= 0) lista.splice(i, 1); else lista.push(deBiblioteca(b));
          aoMudar(); desenhar();
        },
      }, b.imagem ? h('img', { src: b.imagem, alt: b.nome, loading: 'lazy' }) : null,
      h('div', { class: 'corpo' }, h('strong', {}, `${i >= 0 ? '✓ ' : ''}${b.nome}`),
        h('span', { class: 'mudo' }, b.voz ? `voz ${b.voz}` : 'sem voz definida')));
    }));
    const falantes = h('div', { class: 'quem-fala' }, lista.map((p, i) => h('div', { class: 'fala-personagem' },
      avatar(p),
      h('input', { type: 'text', value: p.nome || '', placeholder: 'nome', readOnly: !!p.biblioteca, oninput: (e) => { p.nome = e.target.value; aoMudar(); } }),
      seletorVoz(p.voz, (x) => { p.voz = x; aoMudar(); }, 'voz: o sistema escolhe'),
      h('input', { type: 'text', value: p.estilo || '', placeholder: 'como fala (ex.: doce, vira choro quando é pega)', oninput: (e) => { p.estilo = e.target.value; aoMudar(); } }),
      h('button', { type: 'button', class: 'btn icone perigo', title: 'tirar', onclick: () => { lista.splice(i, 1); aoMudar(); desenhar(); } }, '×'),
      p.biblioteca ? null : h('input', {
        type: 'text', class: 'motivo', value: p.descricao || '', placeholder: 'quem é e como é (vai para as imagens)',
        oninput: (e) => { p.descricao = e.target.value; aoMudar(); },
      }),
      motivos && p.motivo ? h('div', { class: 'motivo' }, p.motivo) : null)));
    trocar(raiz,
      opcoes.personagens.length ? h('div', {}, h('div', { class: 'ajuda', style: { marginBottom: '.5rem' } },
        'Personagens salvos na biblioteca: clique para usar. A imagem aprovada vira a referência das cenas. ',
        h('a', { href: '#/biblioteca/personagens' }, 'Criar um personagem novo'), '.'), grade) : null,
      lista.length ? falantes : h('p', { class: 'mudo pequeno', style: { marginTop: '.8rem' } }, 'Ninguém além do narrador, por enquanto.'),
      h('button', { type: 'button', class: 'btn pequeno', style: { marginTop: '.7rem' }, onclick: () => { lista.push({ nome: '', voz: '', estilo: '', descricao: '' }); aoMudar(); desenhar(); } }, '+ Personagem novo'));
  };
  desenhar();
  return raiz;
}

function seletorModoVoz(valor, aoMudar, rotuloVazio) {
  return h('select', { value: valor || '', onchange: (e) => aoMudar(e.target.value) },
    rotuloVazio ? h('option', { value: '' }, rotuloVazio) : null,
    Object.entries(opcoes.modos_voz).map(([k, v]) => h('option', { value: k }, v)));
}

// ---------------------------------------------------------------- 1. configuração

async function enviarArquivo(tipo, arquivo) {
  const dados = new FormData();
  dados.append('tipo', tipo);
  dados.append('arquivo', arquivo);
  return api('POST', `/api/projetos/${encodeURIComponent(estado.projeto.slug)}/anexos`, dados);
}

function urlDoAnexo(caminho) {
  const prefixo = `projetos/${estado.projeto.slug}/`;
  return caminho.startsWith(prefixo) ? urlDoProjeto(estado.projeto.slug, caminho) : null;
}

async function telaConfigurar(corpo) {
  const slug = estado.projeto.slug;
  const form = estado.projeto.formulario;
  form.valores = form.valores || {};
  form.personagens = form.personagens || [];
  form.modo_voz = form.modo_voz || '';
  const salvarJa = () => api('PUT', `/api/projetos/${encodeURIComponent(slug)}/formulario`, form);
  const salvar = debounce(() => salvarJa().catch(falhou), 600);
  const get = (k) => form.valores[k];
  const set = (k, v) => { form.valores[k] = v; salvar(); };
  fornecedorVoz = get('narracao.fornecedor') || 'gemini';
  form.valores['producao.ate'] = ateDoFormulario(form);  // vai para o banco no próximo salvamento
  if (estado.roteiro_importado) delete form.valores.duracao_alvo;  // sai do roteiro e da velocidade
  let duracao = null;  // o campo calculado, refeito quando a velocidade muda

  corpo.append(h('h1', {}, 'Configuração do vídeo'),
    h('p', { class: 'mudo' }, 'Título, descrição e até onde ir são obrigatórios. O que ficar vazio, o sistema sugere na próxima etapa, e você aprova antes de começar. Tudo é salvo enquanto você digita.'));
  if (infoEtapa('sugestoes').pronta) {
    corpo.append(h('div', { class: 'faixa alerta' }, 'Este pedido já foi aprovado. Mudanças aqui só valem quando você gerar e confirmar as sugestões de novo, e as etapas que dependem delas precisam ser refeitas.'));
  }

  let primeira = true;
  for (const [secao, lista] of secoes()) {
    const grade = h('div', { class: 'grade' });
    const card = h('section', { class: 'card' }, h('h2', {}, secao), grade);
    // até onde ir abre a configuração: decide quais etapas o vídeo vai ter
    if (primeira) grade.append(widgetCampo(campo('producao.ate'), get, (v) => set('producao.ate', v)));
    primeira = false;
    for (const c of lista) {
      if (c.chave === 'producao.ate') {
        continue;
      } else if (c.chave === 'slug') {
        grade.append(h('div', { class: 'campo' }, h('label', {}, rotulo('slug')), h('input', { type: 'text', value: slug, readOnly: true }),
          h('div', { class: 'ajuda' }, 'O nome do projeto nos arquivos e nos endereços.')));
      } else if (calculadoPeloRoteiro(c.chave)) {
        duracao = campoDuracaoDoRoteiro(() => get('narracao.palavras_por_minuto'));
        grade.append(duracao);
      } else if (c.chave === 'narracao.palavras_por_minuto' && duracao) {
        grade.append(widgetCampo(c, get, (v) => { set(c.chave, v); duracao.atualizar(); }));
      } else if (c.chave === 'contexto') {
        grade.append(campoContexto(c, get, set));
      } else if (c.chave === 'imagens_referencia') {
        grade.append(campoImagens(get, set));
      } else if (c.chave === 'narracao.ativa') {
        grade.append(h('div', { class: 'campo largo' }, h('label', {}, rotulo('modo_voz')),
          seletorModoVoz(form.modo_voz, (x) => { form.modo_voz = x; salvar(); }, '— o sistema sugere (narrador, dublagem dos personagens...) —'),
          h('div', { class: 'ajuda' }, 'Narrador só, narrador com os personagens dublados, só os personagens falando, ou sem voz.')));
        grade.append(widgetCampo(c, get, (v) => set(c.chave, v)));
      } else if (c.chave === 'personagens') {
        grade.append(h('div', { class: 'campo largo' }, widgetPersonagens(form.personagens, salvar)));
      } else if (c.chave === 'musica.arquivo') {
        grade.append(campoMusica(get, set));
      } else if (c.chave === 'trends.ativa') {
        grade.append(campoTrends(get, set));
      } else if (c.chave === 'narracao.fornecedor') {  // as vozes mudam com o fornecedor: redesenha a tela
        grade.append(widgetCampo(c, get, (v) => { form.valores[c.chave] = v; salvarJa().then(rota).catch(falhou); }));
      } else if (!CUSTOM.has(c.chave)) {
        grade.append(widgetCampo(c, get, (v) => set(c.chave, v)));
      }
    }
    if (grade.childElementCount) corpo.append(card);
  }

  const gerar = h('button', {
    class: 'btn primario', onclick: async () => {
      if (vazio(get('titulo')) || vazio(get('descricao'))) return aviso('Preencha o título e a descrição.', 'erro');
      if (vazio(get('producao.ate'))) return aviso('Escolha até onde a geração vai.', 'erro');
      gerar.disabled = true;
      try { await salvarJa(); await executar('sugestoes'); } catch (e) { gerar.disabled = false; falhou(e); }
    },
  }, 'Gerar: analisar e sugerir →');
  corpo.append(h('div', { class: 'rodape-acoes' }, h('span', { class: 'mudo pequeno espaco' }, 'Próximo passo: o sistema lê o que você preencheu e sugere o resto.'), gerar));
}

// Até onde ir é do usuário. No projeto de antes desta regra, o formulário não tem o valor: vale o do pedido aprovado,
// ou o vídeo completo.
function ateDoFormulario(form) {
  const doBrief = estado.brief && estado.brief.producao && estado.brief.producao.ate;
  return form.valores['producao.ate'] || doBrief || 'video';
}

// Com roteiro pronto, a duração não é escolha: são as palavras narradas dele na velocidade da narração, a mesma conta
// do pedido.py e da medição (60/ppm s por palavra). O servidor ignora a duração escrita nesse projeto.
function calculadoPeloRoteiro(chave) { return chave === 'duracao_alvo' && !!estado.roteiro_importado; }

function duracaoDoRoteiro(ppm) {
  const c = campo('narracao.palavras_por_minuto');
  const v = Number(ppm);
  const velocidade = v >= c.faixa[0] && v <= c.faixa[1] ? v : parseInt(c.padrao, 10) || 150;
  const palavras = estado.roteiro_importado.palavras;
  const s = Math.max(0, Math.round(palavras * 60 / velocidade));
  return { texto: `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`, palavras, velocidade };
}

// O campo da duração no projeto de roteiro importado: só leitura, refeito por atualizar() quando a velocidade muda.
function campoDuracaoDoRoteiro(velocidade, etiqueta) {
  const valor = h('input', { type: 'text', readOnly: true });
  const ajuda = h('div', { class: 'ajuda' });
  const raiz = h('div', { class: 'campo' }, h('label', {}, rotulo('duracao_alvo'), etiqueta), valor, ajuda);
  raiz.atualizar = () => {
    const d = duracaoDoRoteiro(velocidade());
    valor.value = d.texto;
    ajuda.textContent = `Calculada pelo roteiro: ${d.palavras} palavras a ${d.velocidade} palavras por minuto. Para mudar, mude a velocidade da narração.`;
  };
  raiz.atualizar();
  return raiz;
}

function campoContexto(c, get, set) {
  const raiz = h('div', { class: 'campo largo' });
  const salvoEm = (caminho) => opcoes.contextos.find((x) => x.caminho === caminho);
  let escrito = salvoEm(get('contexto')) ? '' : get('contexto') || ''; // o texto livre volta se trocar de ideia
  const desenhar = () => {
    const salvo = salvoEm(get('contexto'));
    const seletor = h('select', { onchange: (e) => { set('contexto', e.target.value || escrito); desenhar(); } },
      h('option', { value: '' }, 'escrever aqui, só para este vídeo'),
      opcoes.contextos.map((x) => h('option', { value: x.caminho, selected: salvo === x },
        `${x.nome}${contagemAnexos(x) ? ` · ${contagemAnexos(x)}` : ''}`)));
    const conteudo = salvo ?
      h('div', { class: 'previa-contexto' },
        h('div', { class: 'mudo pequeno contexto-resumo' }, salvo.resumo || 'sem texto'),
        salvo.imagens.length ? h('div', { class: 'miniaturas pequenas' }, salvo.imagens.map((a) =>
          h('div', { class: 'miniatura' }, h('img', { src: urlDoContexto(salvo.id, a), alt: '' })))) : null,
        salvo.audios ? h('div', { class: 'mudo pequeno' }, `${salvo.audios} áudio${salvo.audios > 1 ? 's' : ''}`) : null) :
      h('textarea', {
        class: 'grande', rows: 14, value: escrito,
        placeholder: 'Material de apoio: a trend, o universo da série, os personagens, referências de estilo, o que evitar...',
        oninput: (e) => { escrito = e.target.value; set('contexto', escrito); },
      });
    trocar(raiz, h('label', {}, rotulo('contexto')), seletor, conteudo,
      h('div', { class: 'ajuda' }, salvo ?
        ['Contexto salvo, com texto, imagens e áudios. ', h('a', { href: `#/contextos/${encodeURIComponent(salvo.id)}` }, 'Editar na aba Contextos'), '.'] :
        [c.ajuda.split('\n')[0], ' Para usar o mesmo material em vários vídeos, com imagens e áudios, ', h('a', { href: '#/contextos' }, 'salve um contexto'), '.']));
  };
  desenhar();
  return raiz;
}

function campoImagens(get, set) {
  const raiz = h('div', { class: 'campo largo' });
  const desenhar = () => {
    const lista = get('imagens_referencia') || [];
    const entrada = h('input', {
      type: 'file', accept: 'image/png,image/jpeg,image/webp', multiple: true, onchange: async (e) => {
        try {
          for (const f of e.target.files) {
            const r = await enviarArquivo('imagem', f);
            set('imagens_referencia', [...(get('imagens_referencia') || []), r.caminho]);
          }
          desenhar();
        } catch (err) { falhou(err); }
      },
    });
    trocar(raiz, h('label', {}, rotulo('imagens_referencia')),
      h('div', { class: 'miniaturas' }, lista.map((p, i) => h('div', { class: 'miniatura', title: p },
        urlDoAnexo(p) ? h('img', { src: urlDoAnexo(p), alt: '' }) : h('span', { class: 'pequeno' }, p),
        h('button', { type: 'button', title: 'tirar', onclick: () => { set('imagens_referencia', lista.filter((_, j) => j !== i)); desenhar(); } }, '×')))),
      entrada,
      h('div', { class: 'ajuda' }, 'Estilo, personagens ou lugares que o diretor visual deve olhar. Ficam guardadas com o projeto e não entram no vídeo.'));
  };
  desenhar();
  return raiz;
}

function campoMusica(get, set) {
  const raiz = h('div', { class: 'campo' });
  const desenhar = () => {
    const atual = (get('musica.arquivo') || '').replace(/^biblioteca\/musicas\//, '');
    trocar(raiz, h('label', {}, 'Arquivo de música'),
      h('select', { value: atual, onchange: (e) => set('musica.arquivo', e.target.value ? `biblioteca/musicas/${e.target.value}` : null) },
        h('option', { value: '' }, '— nenhum (ou pelo nome, abaixo) —'), opcoes.musicas.map((m) => h('option', { value: m }, m))),
      h('input', {
        type: 'file', accept: 'audio/*', onchange: async (e) => {
          const f = e.target.files[0];
          if (!f) return;
          try {
            const r = await enviarArquivo('musica', f);
            opcoes.musicas.push(r.nome);
            set('musica.arquivo', r.caminho);
            if (boolDe(get('musica.ativa')) === null) set('musica.ativa', true);
            desenhar();
            aviso(`Música ${r.nome} guardada na biblioteca de músicas.`, 'ok');
          } catch (err) { falhou(err); }
        },
      }),
      h('div', { class: 'ajuda' }, 'O sistema não baixa música: escolha uma da biblioteca de músicas ou carregue um arquivo (ele fica na biblioteca, para os outros vídeos também).'));
  };
  desenhar();
  return raiz;
}

function campoTrends(get, set) {
  const ativa = boolDe(get('trends.ativa')) !== false;
  const links = widgetCampo(campo('trends.links'), get, (v) => set('trends.links', v),
    { largo: true, placeholder: 'https://www.youtube.com/watch?v=... ou @canal', semAjuda: false });
  links.style.display = ativa ? '' : 'none';
  const chave = h('input', {
    type: 'checkbox', checked: ativa, onchange: (e) => {
      set('trends.ativa', e.target.checked);
      links.style.display = e.target.checked ? '' : 'none';
    },
  });
  return h('div', { class: 'campo largo' },
    h('label', { class: 'interruptor' }, chave, 'Pesquisar o que está em alta no YouTube antes dos ângulos'),
    h('div', { class: 'ajuda' }, 'Desligada, os ângulos saem sem dados de desempenho (viram apostas) e a etapa de pesquisa só monta o dossiê do tema.'),
    links);
}

// ---------------------------------------------------------------- 2. sugestões e aprovação

function montarRevisao(form, sug) {
  const valores = {};
  const origens = {};
  const motivos = {};
  for (const [k, v] of Object.entries(form.valores || {})) if (!vazio(v)) { valores[k] = clonar(v); origens[k] = 'usuario'; }
  if (!valores['producao.ate']) { valores['producao.ate'] = ateDoFormulario(form); origens['producao.ate'] = 'usuario'; }
  if (estado.roteiro_importado) { delete valores.duracao_alvo; delete origens.duracao_alvo; }
  for (const s of sug.campos || []) {
    if (!campo(s.campo) || OBRIGATORIOS.has(s.campo) || calculadoPeloRoteiro(s.campo)) continue;
    motivos[s.campo] = s.motivo;
    const c = campo(s.campo);
    // lista vazia sugerida é "nenhum" de propósito (tirar as referências do canal), não falta de sugestão
    const nenhum = c.tipo === 'lista' && Array.isArray(s.valor) && !s.valor.length;
    if (!(s.campo in valores) && (!vazio(s.valor) || nenhum)) {
      valores[s.campo] = c.tipo === 'booleano' ? boolDe(s.valor) : c.tipo === 'lista' && !Array.isArray(s.valor) ? [String(s.valor)] : s.valor;
      origens[s.campo] = 'sugestao';
    }
  }
  const sugeridos = (sug.personagens || []).map((p) => ({ ...p }));
  const doUsuario = (form.personagens || []).filter((p) => p.nome);
  let personagens;
  if (doUsuario.length) {
    personagens = doUsuario.map((p) => {
      const s = sugeridos.find((x) => norm(x.nome) === norm(p.nome)) || {};
      const limpo = Object.fromEntries(Object.entries(p).filter(([, v]) => !vazio(v)));
      return { ...s, ...limpo };
    });
    personagens.push(...sugeridos.filter((s) => !doUsuario.some((p) => norm(p.nome) === norm(s.nome))));
  } else personagens = sugeridos;
  const modo = form.modo_voz || (sug.modo_voz && sug.modo_voz.valor) || '';
  return {
    valores, origens, motivos, personagens,
    origem_personagens: doUsuario.length ? 'usuario' : 'sugestao',
    modo_voz: modo, origem_modo: form.modo_voz ? 'usuario' : 'sugestao', motivo_modo: sug.modo_voz && sug.modo_voz.motivo,
  };
}

function etiquetaOrigem(origem, c) {
  if (origem === 'usuario') return h('span', { class: 'etiqueta usuario' }, 'você');
  if (origem === 'sugestao') return h('span', { class: 'etiqueta sugestao' }, 'sugerido');
  if (c && c.quem === 'decisao') return h('span', { class: 'etiqueta erro' }, 'falta decidir');
  return h('span', { class: 'etiqueta' }, 'padrão');
}

async function telaSugestoes(corpo) {
  const slug = estado.projeto.slug;
  await etapaComExecucao(corpo, 'sugestoes', {
    titulo: 'Sugestões do sistema',
    explicacao: 'O sistema leu o que você preencheu e completou o resto. Revise, mude o que quiser e confirme: só então a pesquisa começa.',
    rotuloIniciar: 'Analisar o pedido e sugerir',
    iniciar: () => executar('sugestoes'),
    carregar: async () => estado.projeto.sugestoes,
    temDados: (s) => !!s,
    mostrar: async (sug) => revisao(slug, sug),
  });
}

function revisao(slug, sug) {
  const rev = revisoes[slug] || (revisoes[slug] = montarRevisao(estado.projeto.formulario, sug));
  const get = (k) => rev.valores[k];
  const mudar = (k) => (v) => { rev.valores[k] = v; rev.origens[k] = 'usuario'; if (k === 'narracao.fornecedor') rota(); };
  fornecedorVoz = get('narracao.fornecedor') || 'gemini';
  const raiz = h('div');
  const decididos = [];
  const doUsuario = [];
  const padroes = [];
  for (const c of opcoes.campos) {
    if (['slug', 'personagens', 'musica.arquivo', 'contexto', 'imagens_referencia'].includes(c.chave) || calculadoPeloRoteiro(c.chave)) continue;
    const origem = rev.origens[c.chave];
    if (origem === 'sugestao' || (c.quem === 'decisao' && vazio(rev.valores[c.chave]))) decididos.push(c);
    else if (origem === 'usuario') doUsuario.push(c);
    else padroes.push(c);
  }
  const duracao = estado.roteiro_importado ?
    campoDuracaoDoRoteiro(() => get('narracao.palavras_por_minuto'), h('span', { class: 'etiqueta' }, 'calculada')) : null;
  const linhaCampo = (c) => {
    const etq = etiquetaOrigem(rev.origens[c.chave], c);
    const w = widgetCampo(c, get, (v) => {
      mudar(c.chave)(v);
      etq.replaceWith(etiquetaOrigem('usuario'));
      if (duracao && c.chave === 'narracao.palavras_por_minuto') duracao.atualizar();
    }, {
      etiqueta: etq, motivo: rev.origens[c.chave] === 'sugestao' ? rev.motivos[c.chave] : null, semAjuda: true,
    });
    if (c.chave === 'estilo_artistico' && (sug.alternativas_estilo || []).length) {
      w.append(h('div', { class: 'linha pequeno' }, h('span', { class: 'mudo' }, 'Alternativas:'),
        sug.alternativas_estilo.map((alt) => h('button', {
          type: 'button', class: 'btn pequeno', title: alt, onclick: () => {
            mudar(c.chave)(alt);
            const area = w.querySelector('textarea, input');
            area.value = alt;
          },
        }, alt.length > 48 ? `${alt.slice(0, 46)}…` : alt))));
    }
    return w;
  };

  raiz.append(h('div', { class: 'card' }, h('h2', {}, 'Como o sistema leu o pedido'), md(sug.resumo || ''),
    (sug.avisos || []).length ? h('div', { class: 'faixa alerta', style: { marginTop: '.6rem' } }, h('strong', {}, 'Avisos: '), sug.avisos.join(' · ')) : null));

  const etqModo = etiquetaOrigem(rev.origem_modo);
  raiz.append(h('section', { class: 'card' }, h('h2', {}, 'Vozes e personagens'),
    h('div', { class: 'grade' }, h('div', { class: 'campo largo' }, h('label', {}, rotulo('modo_voz'), etqModo),
      seletorModoVoz(rev.modo_voz, (x) => { rev.modo_voz = x; rev.origem_modo = 'usuario'; etqModo.replaceWith(etiquetaOrigem('usuario')); }),
      rev.origem_modo === 'sugestao' && rev.motivo_modo ? h('div', { class: 'motivo' }, rev.motivo_modo) : null)),
    h('div', { style: { marginTop: '.8rem' } }, widgetPersonagens(rev.personagens, () => { rev.origem_personagens = 'usuario'; }, { motivos: true }))));

  raiz.append(h('section', { class: 'card' }, h('h2', {}, `Sugestões (${decididos.length})`), h('div', { class: 'grade' }, duracao, decididos.map(linhaCampo))));
  raiz.append(h('details', { class: 'card' }, h('summary', {}, `O que você definiu (${doUsuario.length})`), h('div', { class: 'grade' }, doUsuario.map(linhaCampo))));
  raiz.append(h('details', { class: 'card' }, h('summary', {}, `Padrões do sistema (${padroes.length})`),
    h('p', { class: 'mudo pequeno' }, 'Vazios ficam com o padrão indicado. Preencha só o que quiser mudar.'), h('div', { class: 'grade' }, padroes.map(linhaCampo))));

  // num projeto com roteiro próprio, depois do pedido vem a importação do roteiro, não a pesquisa
  const seguinte = infoEtapa('importar') && infoEtapa('importar').aplicavel ? 'importar' : 'pesquisa';
  const feita = infoEtapa(seguinte) && infoEtapa(seguinte).pronta;
  const confirmar = h('button', {
    class: 'btn primario', onclick: async () => {
      if (feita && !confirm(`${seguinte === 'importar' ? 'O roteiro já foi importado' : 'A pesquisa já foi feita'} com o pedido anterior. Confirmar regrava o pedido e o brief; as etapas seguintes precisam ser refeitas. Seguir?`)) return;
      confirmar.disabled = true;
      try {
        const corpoReq = {
          valores: Object.fromEntries(Object.entries(rev.valores).filter(([k, v]) => !vazio(v) || v === false
            || (Array.isArray(v) && rev.origens[k] === 'sugestao'))),
          origens: rev.origens,
          personagens: rev.personagens.filter((p) => p.nome),
          origem_personagens: rev.origem_personagens,
          modo_voz: rev.modo_voz || null,
          origem_modo: rev.origem_modo,
        };
        const r = await api('POST', `/api/projetos/${encodeURIComponent(slug)}/confirmar`, corpoReq);
        const avisos = `${r.completar}\n${r.brief}`.split('\n').filter((l) => l.startsWith('aviso:')).map((l) => l.slice(6).trim());
        if (avisos.length) avisosPendentes[slug] = avisos.join(' · ');
        delete revisoes[slug];
        aviso('Pedido aprovado e gravado.', 'ok');
        await executar(seguinte);
      } catch (e) {
        confirmar.disabled = false;
        raiz.append(h('div', { class: 'card' }, h('div', { class: 'faixa erro' }, 'O pedido não foi gravado:'), h('pre', { class: 'saida' }, e.message)));
        falhou(new Error('O pedido não foi gravado; veja o motivo no fim da página.'));
      }
    },
  }, seguinte === 'importar' ? 'Confirmar e formatar o roteiro →' : 'Confirmar e iniciar a pesquisa →');
  raiz.append(h('div', { class: 'rodape-acoes' },
    h('a', { class: 'btn', href: `#/p/${encodeURIComponent(slug)}/configurar` }, '← Voltar à configuração'),
    h('button', { class: 'btn', onclick: () => { delete revisoes[slug]; executar('sugestoes').catch(falhou); } }, 'Sugerir de novo'),
    h('span', { class: 'espaco' }), confirmar));
  return raiz;
}

// ---------------------------------------------------------------- 2b. roteiro importado

// O roteiro sem o frontmatter (versão, origem...), que não interessa a quem lê.
function semFrontmatter(texto) { return (texto || '').replace(/^---\n[\s\S]*?\n---\n/, ''); }

async function telaImportar(corpo) {
  const slug = estado.projeto.slug;
  const final = urlDoProjeto(slug, '06-roteiro-final.md');
  const original = urlDoProjeto(slug, '00-roteiro-original.md');
  await etapaComExecucao(corpo, 'importar', {
    titulo: 'Roteiro importado',
    explicacao: 'O formatador divide o seu roteiro em blocos e marca as falas dos personagens, sem mudar nenhuma palavra narrada. Depois o sistema confere o texto palavra por palavra com o original e mede o tempo.',
    rotuloIniciar: 'Formatar o roteiro',
    iniciar: () => executar('importar'),
    carregar: async () => {
      const [f, o] = await Promise.all([lerTexto(final), lerTexto(original)]);
      return { final: f, original: o };
    },
    temDados: (d) => !!d.final,
    mostrar: async (d, { rodando }) => h('div', {},
      h('section', { class: 'card' }, h('div', { class: 'linha-topo' }, h('h2', {}, 'No formato do sistema'), h('span', { class: 'espaco' }),
        h('a', { class: 'pequeno', href: final, target: '_blank' }, '06-roteiro-final.md')),
      md(semFrontmatter(d.final))),
      d.original ? h('details', { class: 'card' }, h('summary', {}, 'O roteiro como foi colado'), h('pre', { class: 'texto-pronto' }, d.original)) : null,
      rodando ? null : h('div', { class: 'rodape-acoes' },
        h('button', { class: 'btn', onclick: () => confirm('Formatar de novo, a partir do roteiro original? As etapas seguintes ficam desatualizadas.') && executar('importar').catch(falhou) }, 'Formatar de novo'),
        h('span', { class: 'espaco' }), botaoProxima('importar'))),
  });
}

// ---------------------------------------------------------------- 3. pesquisa

async function telaPesquisa(corpo) {
  const slug = estado.projeto.slug;
  await etapaComExecucao(corpo, 'pesquisa', {
    titulo: 'Pesquisa',
    explicacao: 'Trends do YouTube (se ligadas), estrutura das referências e o dossiê de fatos do tema, em paralelo.',
    rotuloIniciar: 'Iniciar a pesquisa',
    iniciar: () => executar('pesquisa'),
    carregar: () => api('GET', `/api/projetos/${encodeURIComponent(slug)}/pesquisa`),
    temDados: (d) => !!(d.dossie || d.trends),
    mostrar: async (d, { rodando }) => {
      const raiz = h('div');
      if (d.trends_ativa) {
        raiz.append(d.trends ? h('section', { class: 'card' }, h('div', { class: 'linha-topo' }, h('h2', {}, 'Relatório de trends'),
          h('a', { class: 'pequeno', href: `/api/projetos/${encodeURIComponent(slug)}/arquivos/02-trends.md`, target: '_blank' }, '02-trends.md')), md(d.trends))
          : h('div', { class: 'faixa alerta' }, 'A pesquisa de trends está ligada, mas ainda não há 02-trends.md.'));
      } else raiz.append(h('div', { class: 'faixa info' }, 'Pesquisa de trends desligada neste vídeo: os ângulos saem como apostas, sem dados de desempenho.'));
      if (d.dossie) raiz.append(h('details', { class: 'card', open: !d.trends_ativa }, h('summary', {}, 'Dossiê do tema (02-dossie.md)'), md(d.dossie)));
      const refs = (d.referencias || []).filter((r) => r.url);
      if (refs.length) {
        raiz.append(h('div', { class: 'card' }, h('h3', {}, 'Referências de estrutura'), h('ul', {}, refs.map((r) => h('li', {},
          h('a', { href: r.url, target: '_blank', rel: 'noopener' }, r.url), ' ', r.esqueleto ? h('span', { class: 'etiqueta ok' }, 'esqueleto pronto') : h('span', { class: 'etiqueta' }, 'sem esqueleto'))))));
      }
      if (!rodando) {
        raiz.append(h('div', { class: 'rodape-acoes' },
          h('button', { class: 'btn', onclick: () => confirm('Refazer a pesquisa inteira?') && executar('pesquisa').catch(falhou) }, 'Refazer a pesquisa'),
          h('span', { class: 'espaco' }),
          h('button', { class: 'btn primario', disabled: !d.dossie, onclick: () => executar('angulos').catch(falhou) }, 'Próxima etapa: ângulos →')));
      }
      return raiz;
    },
  });
}

// ---------------------------------------------------------------- 4. ângulos

function valorLegivel(v) {
  if (v == null || v === '') return null;
  if (typeof v === 'boolean') return v ? 'sim' : 'não';
  if (Array.isArray(v)) {
    if (!v.length) return null;
    return h('ul', {}, v.map((x) => h('li', {}, typeof x === 'object' ? valorLegivel(x) : String(x))));
  }
  if (typeof v === 'object') {
    return h('span', {}, Object.entries(v).filter(([, x]) => x != null && x !== '').map(([k, x], i) =>
      h('span', {}, i ? ' · ' : '', h('strong', {}, `${k.replace(/_/g, ' ')}: `), typeof x === 'object' ? JSON.stringify(x) : String(x))));
  }
  return String(v);
}

async function telaAngulos(corpo) {
  const slug = estado.projeto.slug;
  await etapaComExecucao(corpo, 'angulos', {
    titulo: 'Ângulo do vídeo',
    explicacao: 'O estrategista propõe 2 ou 3 ângulos (título, thumbnail, promessa, obrigações e estrutura) a partir das trends e do dossiê. Escolha um: depois vêm os ganchos.',
    rotuloIniciar: 'Gerar os ângulos',
    iniciar: () => executar('angulos'),
    carregar: () => api('GET', `/api/projetos/${encodeURIComponent(slug)}/angulos`),
    temDados: (d) => !!(d.angulos && (d.angulos.angulos || []).length),
    mostrar: async (d, { rodando }) => {
      const doc = d.angulos;
      const rec = doc.recomendacao || {};
      let escolhido = (d.escolhido && d.escolhido.id) || rec.angulo || doc.angulos[0].id;
      const raiz = h('div');
      if (rec.angulo) raiz.append(h('div', { class: 'faixa info' }, h('strong', {}, `Recomendado: ${rec.angulo}. `), rec.motivo || ''));
      const lista = (pares) => h('dl', {}, pares.filter(([, v]) => v != null && !vazio(v))
        .map(([k, v]) => [h('dt', {}, k), h('dd', {}, valorLegivel(v))]));
      const cards = doc.angulos.map((a) => {
        const card = h('div', {
          class: `card angulo ${a.id === escolhido ? 'selecionado' : ''}`, onclick: () => {
            escolhido = a.id;
            cards.forEach((c) => c.classList.toggle('selecionado', c.dataset.id === a.id));
          },
        },
        h('div', { class: 'linha-topo' }, h('span', { class: 'etiqueta' }, a.id), a.id === rec.angulo ? h('span', { class: 'etiqueta destaque' }, 'recomendado') : null,
          d.escolhido && d.escolhido.id === a.id ? h('span', { class: 'etiqueta ok' }, 'escolhido') : null,
          h('span', { class: 'mudo pequeno' }, [a.formato, a.duracao_recomendada].filter(Boolean).join(' · '))),
        h('h3', {}, a.titulo),
        h('p', {}, a.promessa),
        lista([['Thumbnail', a.thumbnail], ['Gancho', a.gancho_proposto], ['Títulos alternativos', a.titulos_alternativos],
          ['Obrigações', a.obrigacoes]]),
        h('details', { onclick: (e) => e.stopPropagation() }, h('summary', { class: 'pequeno', style: { marginTop: '.6rem' } }, 'Mais detalhes'),
          lista([['Técnicas', a.tecnicas], ['Gatilhos', a.gatilhos], ['Estrutura', a.estrutura], ['Aposta', a.aposta],
            ['Diferencial', a.diferencial], ['Evidências', a.evidencias], ['Riscos', a.riscos]])));
        card.dataset.id = a.id;
        return card;
      });
      raiz.append(h('div', { class: 'angulos' }, cards));
      if (rodando) return raiz;
      const ajustes = h('textarea', { rows: 2, placeholder: 'Ajustes no ângulo escolhido (opcional): trocar o título, a duração, o gancho...', value: (d.escolhido && d.escolhido.ajustes_do_usuario) || '' });
      const orientacao = h('input', { type: 'text', placeholder: 'O que mudar nos ângulos novos (opcional)' });
      raiz.append(h('div', { class: 'card' }, h('div', { class: 'grade' },
        h('div', { class: 'campo largo' }, h('label', {}, 'Ajustes do usuário'), ajustes),
        h('div', { class: 'campo largo' }, h('label', {}, 'Não gostou de nenhum?'), h('div', { class: 'linha' },
          h('div', { style: { flex: 1, minWidth: '220px' } }, orientacao),
          h('button', { class: 'btn', onclick: () => executar('angulos', { orientacao: orientacao.value }).catch(falhou) }, 'Gerar ângulos novos'))))));
      raiz.append(h('div', { class: 'rodape-acoes' }, h('span', { class: 'espaco' }),
        h('button', {
          class: 'btn primario', onclick: async () => {
            try {
              await api('POST', `/api/projetos/${encodeURIComponent(slug)}/angulos/escolher`, { id: escolhido, ajustes: ajustes.value });
              await executar('ganchos');
            } catch (e) { falhou(e); }
          },
        }, 'Escolher e gerar os ganchos →')));
      return raiz;
    },
  });
}

// ---------------------------------------------------------------- 4b. ganchos

async function telaGanchos(corpo) {
  const slug = estado.projeto.slug;
  await etapaComExecucao(corpo, 'ganchos', {
    titulo: 'Gancho do vídeo',
    explicacao: 'O redator escreve de 6 a 8 aberturas com estruturas diferentes para o ângulo escolhido. Cada uma é lida a frio por um espectador sem contexto e avaliada pelo crítico; o sistema calcula a nota e separa as 3 melhores. Escolha uma (dá para ajustar o texto): ela vira a primeira fala do roteiro, e o que ela promete o roteiro precisa cumprir.',
    rotuloIniciar: 'Gerar os ganchos',
    iniciar: () => executar('ganchos'),
    carregar: () => api('GET', `/api/projetos/${encodeURIComponent(slug)}/ganchos`),
    temDados: (d) => !!(d.ganchos && (d.ganchos.ganchos || []).length),
    mostrar: async (d, { rodando }) => {
      const doc = d.ganchos;
      const melhores = doc.melhores || [];
      const rec = doc.recomendacao || {};
      const atual = d.escolhido_do_angulo ? d.escolhido : null;
      const porId = Object.fromEntries(doc.ganchos.map((g) => [g.id, g]));
      let escolhido = (atual && porId[atual.id] && atual.id) || rec.gancho || melhores[0] || doc.ganchos[0].id;
      const raiz = h('div');
      if (!d.lista_do_angulo) raiz.append(h('div', { class: 'faixa alerta' }, 'Estes ganchos são de um ângulo anterior: gere os ganchos de novo para o ângulo escolhido.'));
      else if (!melhores.length && !rodando) raiz.append(h('div', { class: 'faixa alerta' }, 'Nenhum gancho passou nos critérios bloqueantes (clareza e credibilidade). Escolha um e ajuste o texto, ou gere ganchos novos com uma orientação.'));
      if (rec.gancho) raiz.append(h('div', { class: 'faixa info' }, h('strong', {}, `Recomendado: ${rec.gancho}. `), rec.motivo || ''));
      const texto = h('textarea', { rows: 2, 'data-auto': '1' });
      const textoDe = (id) => (atual && atual.id === id ? atual.texto : porId[id].texto);
      const cards = [];
      const selecionar = (id) => {
        escolhido = id;
        cards.forEach((c) => c.classList.toggle('selecionado', c.dataset.id === id));
        texto.value = textoDe(id);
        autoAltura(texto);
      };
      const lista = (pares) => h('dl', {}, pares.filter(([, v]) => v != null && !vazio(v))
        .map(([k, v]) => [h('dt', {}, k), h('dd', {}, valorLegivel(v))]));
      const cartao = (g) => {
        const medida = g.segundos != null ? `${g.palavras} palavras · ${g.segundos} s` : null;
        const card = h('div', { class: `card angulo ${g.id === escolhido ? 'selecionado' : ''}`, onclick: () => selecionar(g.id) },
          h('div', { class: 'linha-topo' }, h('span', { class: 'etiqueta' }, g.id),
            g.id === rec.gancho ? h('span', { class: 'etiqueta destaque' }, 'recomendado') : null,
            g.pedido_pelo_usuario ? h('span', { class: 'etiqueta usuario' }, 'pedido por você') : null,
            atual && atual.id === g.id ? h('span', { class: 'etiqueta ok' }, atual.editado_pelo_usuario ? 'escolhido (editado)' : 'escolhido') : null,
            g.elegivel === false ? h('span', { class: 'etiqueta' }, `não passou: ${(g.bloqueantes || []).join(', ')}`) : null,
            h('span', { class: 'mudo pequeno' }, [g.nota_percentual != null ? `nota ${g.nota_percentual}%` : null, g.tipo_gancho, medida].filter(Boolean).join(' · '))),
          h('h3', {}, `“${g.texto}”`),
          g.resumo ? h('p', {}, g.resumo) : null,
          lista([['Gatilhos', g.gatilhos], ['Obrigações', g.obrigacoes], ['Primeiro frame', g.visual]]),
          h('details', { onclick: (e) => e.stopPropagation() }, h('summary', { class: 'pequeno', style: { marginTop: '.6rem' } }, 'Leitura fria e notas'),
            lista([['Padrão', g.padrao], ['Referência', g.referencia], ['Leitura fria', g.leitura_fria],
              ['Confusões', (g.confusoes || []).map((c) => `“${c.trecho}”: ${c.problema}`)],
              ['Notas', (g.criterios || []).map((c) => `${c.id}: ${c.nota}${c.problema ? ` (${c.problema})` : ''}`)],
              ['Ajustes do sistema', g.ajustes_do_sistema]])));
        card.dataset.id = g.id;
        cards.push(card);
        return card;
      };
      // o gancho que o usuário pediu fica à vista mesmo quando não passou nos bloqueantes
      const pedido = doc.ganchos.find((g) => g.pedido_pelo_usuario && !melhores.includes(g.id));
      const top = [...melhores.map((id) => porId[id]).filter(Boolean), ...(pedido && melhores.length ? [pedido] : [])];
      const outros = doc.ganchos.filter((g) => !top.includes(g));
      raiz.append(h('h2', {}, top.length ? 'Os melhores' : 'Os ganchos'), h('div', { class: 'angulos' }, (top.length ? top : outros).map(cartao)));
      if (top.length && outros.length) {
        raiz.append(h('details', { class: 'card' }, h('summary', {}, `Os outros ${outros.length}`), h('div', { class: 'angulos' }, outros.map(cartao))));
      }
      if (rodando) return raiz;
      texto.value = textoDe(escolhido);
      const orientacao = h('input', { type: 'text', placeholder: 'O que mudar nos ganchos novos (opcional)' });
      raiz.append(h('div', { class: 'card' }, h('div', { class: 'grade' },
        h('div', { class: 'campo largo' }, h('label', {}, 'O gancho, como vai ser falado (ajuste se quiser)'), texto),
        h('div', { class: 'campo largo' }, h('label', {}, 'Não gostou de nenhum?'), h('div', { class: 'linha' },
          h('div', { style: { flex: 1, minWidth: '220px' } }, orientacao),
          h('button', { class: 'btn', onclick: () => executar('ganchos', { orientacao: orientacao.value }).catch(falhou) }, 'Gerar ganchos novos'))))));
      raiz.append(h('div', { class: 'rodape-acoes' }, h('span', { class: 'espaco' }),
        h('button', {
          class: 'btn primario', disabled: !d.lista_do_angulo, onclick: async () => {
            try {
              await api('POST', `/api/projetos/${encodeURIComponent(slug)}/ganchos/escolher`, { id: escolhido, texto: texto.value });
              await executar('roteiro');
            } catch (e) { falhou(e); }
          },
        }, 'Escolher e gerar o roteiro →')));
      requestAnimationFrame(() => autoAltura(texto));
      return raiz;
    },
  });
}

// ---------------------------------------------------------------- 5. roteiro

let editor = null; // {slug, versoes, nomes, sel, doc, sujo}

function corDoFalante(nome) {
  if (norm(nome) === 'narrador') return 'var(--borda)';
  const ps = (estado.brief && estado.brief.personagens) || [];
  const p = ps.find((x) => norm(x.nome) === norm(nome));
  if (p && p.cor) return p.cor;
  const i = editor.nomes.findIndex((n) => norm(n) === norm(nome));
  return PALETA[Math.max(i, 0) % PALETA.length];
}

function resumoVersao(v) {
  const a = v.avaliacao;
  const nota = a ? `${a.nota ?? '?'}%` : null;
  return [`v${v.versao}`, nota, a ? (a.aprovado ? '✓' : '✗') : null, v.final ? '· final' : null, v.origem.startsWith('editada') ? '· editada' : null]
    .filter(Boolean).join(' ');
}

async function telaRoteiro(corpo) {
  const slug = estado.projeto.slug;
  await etapaComExecucao(corpo, 'roteiro', {
    titulo: 'Roteiro',
    explicacao: 'O roteirista escreve e o crítico avalia, até 3 versões. Aqui aparece só o que é dito (narração e falas, sem as descrições de imagem). Edite à vontade: salvar cria uma versão nova e as indicações de imagem continuam no lugar.',
    rotuloIniciar: 'Gerar o roteiro',
    iniciar: () => executar('roteiro'),
    carregar: () => api('GET', `/api/projetos/${encodeURIComponent(slug)}/roteiros`),
    temDados: (d) => d.versoes.length > 0,
    mostrar: async (d, { rodando }) => {
      const anterior = editor && editor.slug === slug ? editor.versoes[editor.sel]?.versao : null;
      let sel = d.versoes.findIndex((v) => v.versao === anterior);
      if (sel < 0) sel = Math.max(d.versoes.findIndex((v) => v.final), d.versoes.length - 1);
      editor = { slug, versoes: d.versoes, nomes: d.nomes, sel, doc: clonar(d.versoes[sel].doc), sujo: false, rodando };
      const raiz = h('div', { id: 'roteiro' });
      desenharRoteiro(raiz);
      return raiz;
    },
  });
}

function trocarVersao(i, raiz) {
  if (editor.sujo && !confirm('Há alterações não salvas nesta versão. Descartar?')) return;
  editor.sel = i;
  editor.doc = clonar(editor.versoes[i].doc);
  editor.sujo = false;
  desenharRoteiro(raiz);
}

function desenharRoteiro(raiz) {
  const v = editor.versoes[editor.sel];
  const a = v.avaliacao;
  const m = v.medicao || {};
  const salvar = h('button', { class: 'btn primario', disabled: !editor.sujo, onclick: () => salvarVersao(raiz) }, 'Salvar como nova versão');
  const descartar = h('button', { class: 'btn', disabled: !editor.sujo, onclick: () => { editor.doc = clonar(v.doc); editor.sujo = false; desenharRoteiro(raiz); } }, 'Descartar alterações');
  const marcarSujo = () => {
    if (!editor.sujo) { editor.sujo = true; salvar.disabled = false; descartar.disabled = false; }
  };

  const abas = h('div', { class: 'versoes' }, editor.versoes.map((x, i) =>
    h('button', { class: `versao ${i === editor.sel ? 'ativa' : ''}`, onclick: () => trocarVersao(i, raiz) }, resumoVersao(x))));

  const numeros = h('div', { class: 'numeros' },
    m.palavras != null ? h('div', {}, h('strong', {}, m.palavras), h('span', {}, 'palavras')) : null,
    m.duracao_estimada ? h('div', {}, h('strong', {}, m.duracao_estimada), h('span', {}, `estimada · alvo ${m.duracao_alvo || '?'}`)) : null,
    a ? h('div', {}, h('strong', {}, `${a.nota ?? '?'}%`), h('span', {}, a.aprovado ? 'aprovado pelo crítico' : 'reprovado pelo crítico')) : h('div', {}, h('strong', {}, '—'), h('span', {}, 'sem avaliação')),
    h('div', {}, h('strong', { class: 'pequeno' }, v.origem), h('span', {}, v.arquivo)));

  const avaliacao = a ? h('details', { class: 'card' }, h('summary', {}, `Avaliação do crítico${a.bloqueantes.length ? ` · ${a.bloqueantes.length} bloqueante(s)` : ''}`),
    a.resumo ? md(a.resumo) : null,
    a.bloqueantes.length ? h('div', { class: 'faixa erro' }, h('strong', {}, 'Bloqueantes: '), a.bloqueantes.map((b) => (typeof b === 'object' ? b.id || JSON.stringify(b) : b)).join(', ')) : null,
    (a.obrigacoes || []).length ? h('div', {}, h('strong', {}, 'Promessas e obrigações do contrato'), h('ul', {}, a.obrigacoes.map((o) =>
      h('li', {}, `${{ sim: '✓', parcial: '~', nao: '✗' }[o.cumprida] || '?'} ${o.id}: ${o.obrigacao || ''}${o.onde ? ` (${o.onde})` : ''}`)))) : null,
    h('ul', {}, a.criterios.map((c) => h('li', { style: { marginBottom: '.5rem' } },
      h('strong', {}, `${c.id}${c.bloco ? ` (${c.bloco})` : ''}: nota ${c.nota}`), h('div', {}, c.problema),
      c.instrucao ? h('div', { class: 'mudo pequeno' }, `→ ${c.instrucao}`) : null)))) : null;

  const alertas = [].concat(m.erros || [], m.alertas || []);
  const blocos = h('div', { class: 'card' }, editor.doc.blocos.map((b) => desenharBloco(b, marcarSujo, raiz)));

  const orientacao = h('textarea', { rows: 2, placeholder: 'Orientação para o roteirista (opcional): o que mudar na próxima versão' });
  const acoes = editor.rodando ? null : h('div', {},
    h('details', { class: 'card' }, h('summary', {}, 'Pedir mais uma versão ao roteirista'),
      h('p', { class: 'mudo pequeno' }, `Parte da última versão (v${editor.versoes[editor.versoes.length - 1].versao}) e da avaliação dela; se ela foi editada por você e não tem avaliação, o crítico avalia primeiro.`),
      orientacao,
      h('div', { class: 'linha', style: { marginTop: '.6rem' } },
        h('button', { class: 'btn', onclick: () => executar('roteiro', { orientacao: orientacao.value, rodadas: 1 }).catch(falhou) }, 'Uma rodada roteirista + crítico'))),
    h('div', { class: 'rodape-acoes' }, descartar, salvar,
      a ? null : h('button', { class: 'btn', onclick: () => { if (editor.sujo) return aviso('Salve antes de avaliar.'); executar('avaliar', { versao: v.versao }).catch(falhou); } }, 'Avaliar com o crítico'),
      h('span', { class: 'espaco' }),
      h('button', { class: 'btn primario', onclick: () => usarComoFinal(v) }, `Usar v${v.versao} e ir para a próxima etapa →`)));

  trocar(raiz, abas, h('div', { class: 'card' }, numeros,
    alertas.length ? h('details', {}, h('summary', { class: 'pequeno' }, `${alertas.length} alerta(s) do medidor`), h('ul', { class: 'pequeno' }, alertas.map((x) => h('li', {}, typeof x === 'object' ? x.mensagem || valorLegivel(x) : x)))) : null),
  avaliacao, blocos, acoes);
  raiz.querySelectorAll('textarea[data-auto]').forEach(autoAltura);
}

function novaFala(modelo) {
  return { tipo: 'fala', quem: modelo ? modelo.quem : 'Narrador', direcao: '', texto: '', rehook: false, visuais: [], visual_antes: true, quebra: false };
}

function desenharBloco(b, marcarSujo, raiz) {
  const visiveis = b.itens.map((it, i) => [it, i]).filter(([it]) => it.tipo === 'fala');
  const redesenhar = () => { marcarSujo(); editor.sujo = true; desenharRoteiro(raiz); };
  return h('div', { class: 'bloco' },
    h('div', { class: 'bloco-topo' }, h('span', { class: 'bloco-id' }, b.id),
      h('input', { type: 'text', value: b.titulo, oninput: (e) => { b.titulo = e.target.value; marcarSujo(); } })),
    visiveis.map(([it, i]) => {
      const nomes = editor.nomes.some((n) => norm(n) === norm(it.quem)) ? editor.nomes : [...editor.nomes, it.quem];
      const linha = h('div', { class: `fala ${norm(it.quem) === 'narrador' ? 'narrador' : ''}` });
      linha.style.setProperty('--cor-falante', corDoFalante(it.quem));
      const quem = h('select', {
        value: nomes.find((n) => norm(n) === norm(it.quem)), onchange: (e) => {
          it.quem = e.target.value;
          linha.classList.toggle('narrador', norm(it.quem) === 'narrador');
          linha.style.setProperty('--cor-falante', corDoFalante(it.quem));
          marcarSujo();
        },
      }, nomes.map((n) => h('option', { value: n }, n)));
      const texto = h('textarea', { 'data-auto': '1', rows: 1, value: it.texto, oninput: (e) => { it.texto = e.target.value; autoAltura(e.target); marcarSujo(); } });
      const rehook = h('button', {
        type: 'button', class: `btn pequeno rehook ${it.rehook ? 'ativo' : ''}`, title: 'marca esta fala como re-hook',
        onclick: () => { it.rehook = !it.rehook; rehook.classList.toggle('ativo', it.rehook); marcarSujo(); },
      }, 'RE-HOOK');
      linha.append(quem,
        h('input', { type: 'text', value: it.direcao, placeholder: 'como dizer', oninput: (e) => { it.direcao = e.target.value; marcarSujo(); } }),
        texto,
        h('div', { class: 'acoes' }, rehook,
          h('button', { type: 'button', class: 'btn icone pequeno', title: 'nova fala abaixo', onclick: () => { b.itens.splice(i + 1, 0, novaFala(it)); redesenhar(); } }, '+'),
          h('button', {
            type: 'button', class: 'btn icone pequeno perigo', title: 'apagar esta fala', onclick: () => {
              // a indicação de imagem que estava nesta linha continua no arquivo, sozinha na linha
              if ((it.visuais || []).length) b.itens.splice(i, 1, { tipo: 'oculto', bruto: it.visuais.join(' '), quebra: it.quebra });
              else b.itens.splice(i, 1);
              redesenhar();
            },
          }, '×')));
      return linha;
    }),
    visiveis.length ? null : h('button', { class: 'btn pequeno', onclick: () => { b.itens.push(novaFala()); redesenhar(); } }, '+ fala'));
}

async function salvarVersao(raiz) {
  const v = editor.versoes[editor.sel];
  try {
    const r = await api('POST', `/api/projetos/${encodeURIComponent(editor.slug)}/roteiros`, { base: v.versao, doc: editor.doc });
    editor.sujo = false;
    aviso(`Versão v${r.versao} salva (${r.medicao && r.medicao.duracao_estimada ? `${r.medicao.palavras} palavras, ${r.medicao.duracao_estimada}` : r.arquivo}).`, 'ok');
    const d = await api('GET', `/api/projetos/${encodeURIComponent(editor.slug)}/roteiros`);
    editor.versoes = d.versoes;
    editor.nomes = d.nomes;
    editor.sel = d.versoes.findIndex((x) => x.versao === r.versao);
    editor.doc = clonar(d.versoes[editor.sel].doc);
    desenharRoteiro(raiz);
  } catch (e) { falhou(e); }
}

async function usarComoFinal(v) {
  if (editor.sujo) return aviso('Há alterações não salvas: salve como nova versão (ou descarte) antes de seguir.', 'erro');
  if (v.avaliacao && !v.avaliacao.aprovado && !confirm(`A v${v.versao} foi reprovada pelo crítico. Usar mesmo assim?`)) return;
  try {
    const r = await api('POST', `/api/projetos/${encodeURIComponent(editor.slug)}/roteiros/${v.versao}/final`);
    aviso(`06-roteiro-final.md agora é a v${v.versao}.`, 'ok');
    if (!r.proxima) { aviso('O pedido vai só até o roteiro: pronto!', 'ok'); return rota(); }
    if (r.proxima === 'producao') return irPara(editor.slug, 'producao');
    await executar(r.proxima);
  } catch (e) { falhou(e); }
}

// ---------------------------------------------------------------- próxima etapa

function botaoProxima(id, rotuloPadrao) {
  const prox = proximaAplicavel(id);
  if (!prox) return h('span', { class: 'mudo' }, 'Última etapa do pedido.');
  const custa = prox.id === 'producao';
  return h('button', {
    class: 'btn primario', onclick: () => (custa ? irPara(estado.projeto.slug, prox.id) : executar(prox.id).catch(falhou)),
  }, rotuloPadrao || `Próxima etapa: ${prox.nome.toLowerCase()} →`);
}

// ---------------------------------------------------------------- 7. direção visual

async function telaVisual(corpo) {
  const slug = estado.projeto.slug;
  await etapaComExecucao(corpo, 'visual', {
    titulo: 'Direção visual',
    explicacao: 'O diretor visual escreve a bíblia (estilo, personagens e locais com descrição fixa) e as cenas de cada bloco; o script monta os prompts e o storyboard.',
    rotuloIniciar: 'Gerar a direção visual',
    iniciar: () => executar('visual'),
    carregar: () => api('GET', `/api/projetos/${encodeURIComponent(slug)}/visual`),
    temDados: (d) => !!d.storyboard,
    mostrar: async (d, { rodando }) => h('div', {},
      h('div', { class: 'card' }, h('div', { class: 'numeros' },
        h('div', {}, h('strong', {}, d.cenas), h('span', {}, 'cenas')),
        h('div', {}, h('strong', {}, d.personagens.length), h('span', {}, 'personagens')),
        h('div', {}, h('strong', {}, d.locais.length), h('span', {}, 'locais'))),
      d.estilo ? h('p', {}, h('strong', {}, `${d.estilo.nome || 'Estilo'}: `), d.estilo.descricao || '') : null),
      h('details', { class: 'card', open: true }, h('summary', {}, 'Storyboard (08-storyboard.md)'), md(d.storyboard)),
      rodando ? null : h('div', { class: 'rodape-acoes' },
        h('button', { class: 'btn', onclick: () => confirm('Refazer a direção visual?') && executar('visual').catch(falhou) }, 'Refazer'),
        h('span', { class: 'espaco' }), botaoProxima('visual'))),
  });
}

// ---------------------------------------------------------------- 8. produção: entregáveis

function botaoCopiar(texto, rotuloBotao = 'Copiar') {
  const b = h('button', {
    class: 'btn pequeno', onclick: async () => {
      try {
        await navigator.clipboard.writeText(texto);
        b.textContent = 'Copiado';
        setTimeout(() => { b.textContent = rotuloBotao; }, 1500);
      } catch { aviso('Não consegui copiar: selecione o texto e copie à mão.', 'erro'); }
    },
  }, rotuloBotao);
  return b;
}

function dolares(v) {
  return `US$ ${Number(v || 0).toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: v && v < 0.1 ? 4 : 2 })}`;
}

// A descrição de cada plataforma, uma aba por plataforma, com o título dela quando é outro e o botão de copiar.
function descricoesPorPlataforma(p) {
  const plataformas = p.plataformas || [];
  const texto = h('div');
  const abas = h('div', { class: 'abas-plataforma' });
  const mostrarPlataforma = (i) => {
    const pl = plataformas[i];
    abas.querySelectorAll('button').forEach((b, j) => b.classList.toggle('ativa', j === i));
    trocar(texto,
      pl.titulo && pl.titulo !== p.titulo ? h('div', { class: 'linha', style: { marginBottom: '.6rem' } },
        h('strong', {}, 'Título nesta plataforma: '), pl.titulo, botaoCopiar(pl.titulo)) : null,
      h('pre', { class: 'texto-pronto' }, pl.descricao || ''),
      h('div', { class: 'linha', style: { marginTop: '.5rem' } },
        h('span', { class: 'mudo pequeno espaco' }, `${(pl.descricao || '').length} caracteres`), botaoCopiar(pl.descricao || '', 'Copiar descrição')));
  };
  plataformas.forEach((pl, i) => abas.append(h('button', { class: 'btn pequeno', onclick: () => mostrarPlataforma(i) }, pl.nome || pl.id)));
  if (plataformas.length) mostrarPlataforma(0);
  return [abas, texto];
}

function entregaveisPublicacao(p) {
  const plataformas = p.plataformas || [];
  const [abas, texto] = descricoesPorPlataforma(p);

  const tags = p.tags || {};
  const emAlta = tags.em_alta || [];
  const tema = tags.tema || [];
  const campoYoutube = p.tags_youtube || '';
  return h('div', {},
    h('div', { class: 'card' },
      h('div', { class: 'linha-topo' }, h('h2', {}, 'Título'), h('span', { class: 'espaco' }), botaoCopiar(p.titulo || '')),
      h('p', { class: 'titulo-final' }, p.titulo),
      (p.titulos_alternativos || []).length ? h('div', { class: 'mudo pequeno' }, 'Alternativas: ',
        p.titulos_alternativos.map((t, i) => h('span', {}, i ? ' · ' : '', t))) : null),
    plataformas.length ? h('div', { class: 'card' }, h('h2', {}, 'Descrição por plataforma'), abas, texto) : null,
    h('div', { class: 'card' },
      h('h2', {}, 'Tags'),
      h('h3', { class: 'subtitulo' }, 'Em alta agora'),
      emAlta.length ? h('div', { class: 'chips' }, emAlta.map((t) => h('span', { class: 'chip alta', title: t.evidencia || '' }, t.tag,
        t.evidencia ? h('small', {}, t.evidencia) : null))) :
        h('p', { class: 'mudo pequeno' }, 'Nenhuma tag em alta que combine com o vídeo nos dados coletados.'),
      h('h3', { class: 'subtitulo' }, 'Do tema'),
      tema.length ? h('div', { class: 'chips' }, tema.map((t) => h('span', { class: 'chip' }, t))) : h('p', { class: 'mudo pequeno' }, 'Nenhuma.'),
      campoYoutube ? h('div', { class: 'campo', style: { marginTop: '1rem' } },
        h('label', {}, 'Campo de tags do YouTube'),
        h('textarea', { rows: 3, readOnly: true, value: campoYoutube }),
        h('div', { class: 'linha' }, h('span', { class: `pequeno espaco ${campoYoutube.length > 500 ? 'texto-erro' : 'mudo'}` }, `${campoYoutube.length} de 500 caracteres`),
          botaoCopiar(campoYoutube, 'Copiar tags'))) : null,
      p.fonte_tags ? h('p', { class: 'mudo pequeno', style: { marginTop: '.6rem' } }, `Tags em alta: ${p.fonte_tags}. Passe o mouse numa tag para ver a evidência.`) : null,
      (p.avisos || []).length ? h('div', { class: 'faixa alerta', style: { marginTop: '.8rem' } },
        h('strong', {}, 'Para conferir antes de publicar'), h('ul', { class: 'lista-avisos' }, p.avisos.map((a) => h('li', {}, a)))) : null));
}

const SERVICOS = { llm: 'Modelos de linguagem', busca: 'Busca na web', imagem: 'Imagens', animacao: 'Animação', narracao: 'Narração' };

// O custo vem do banco: cada chamada paga (agentes, busca, imagens, narração) com o preço da tabela. O formato
// antigo (itens prontos) continua aceito.
function cartaoCusto(c) {
  if (!c) return null;
  const porEtapa = c.itens ? c.itens.map((i) => ({ rotulo: i.rotulo, detalhe: i.detalhe, usd: i.usd }))
    : (c.por_etapa || []).map((x) => ({
      rotulo: x.etapa ? nomeEtapa(x.etapa) : 'fora de uma execução',
      detalhe: x.execucoes != null ? `${x.execucoes} execuç${x.execucoes === 1 ? 'ão' : 'ões'}` : '',
      usd: x.usd,
    }));
  const porServico = (c.por_servico || []).map((x) => ({
    rotulo: SERVICOS[x.servico] || x.servico || '—',
    detalhe: [x.provedor, x.modelo, x.chamadas != null ? `${x.chamadas} chamada${x.chamadas === 1 ? '' : 's'}` : null].filter(Boolean).join(' · '),
    usd: x.usd != null ? x.usd : x.custo_usd,
  }));
  const tabela = (linhas) => h('table', { class: 'tabela-custos' },
    h('tbody', {}, linhas.map((i) => h('tr', {}, h('td', {}, i.rotulo), h('td', { class: 'mudo pequeno' }, i.detalhe), h('td', { class: 'valor' }, dolares(i.usd))))));
  const notas = [
    'Cada chamada paga aos provedores entra aqui: os tokens (ou caracteres) de cada uma vezes a tabela de preços.',
    c.tabela && c.tabela.conferido_em ? `Tabela de preços conferida em ${c.tabela.conferido_em}.` : null,
    c.sem_preco && c.sem_preco.length ? `Sem preço na tabela, fora do total: ${c.sem_preco.join(', ')}.` : null,
  ].filter(Boolean);
  return h('div', { class: 'card' },
    h('div', { class: 'linha-topo' }, h('h2', {}, 'Custo total do projeto'), h('span', { class: 'espaco' }),
      h('span', { class: 'custo-total' }, dolares(c.total_usd))),
    porEtapa.length ? [h('h3', { class: 'subtitulo' }, 'Por etapa'), tabela(porEtapa)] : null,
    porServico.length ? [h('h3', { class: 'subtitulo' }, 'Por serviço'), tabela(porServico)] : null,
    porEtapa.length || porServico.length ? null : h('p', { class: 'mudo' }, 'Nada registrado ainda.'),
    h('ul', { class: 'mudo pequeno notas-custo' }, notas.map((n) => h('li', {}, n))));
}

// A revisão visual: o revisor confere as imagens, a thumbnail e os quadros do vídeo, e o sistema refaz o que
// ele aponta (até duas rodadas). Pode vir uma revisão só ou a lista das rodadas.
function cartaoRevisao(revisao) {
  if (!revisao || (typeof revisao === 'object' && !Array.isArray(revisao) && !Object.keys(revisao).length)) return null;
  if (typeof revisao === 'string') return h('details', { class: 'card' }, h('summary', {}, 'Revisão visual'), md(revisao));
  const rodadas = Array.isArray(revisao) ? revisao : [revisao];
  if (!rodadas.length) return null;
  const itens = (l, fmt) => (l && l.length ? h('ul', {}, l.map((x) => h('li', {}, fmt(x)))) : null);
  const problemas = (r) => (r.cenas_refazer || []).length + (r.fichas_refazer || []).length + (r.internet_trocar || []).length
    + (r.thumbnail && r.thumbnail.ok === false ? 1 : 0) + (r.quadros && r.quadros.ok === false ? 1 : 0);
  const ultima = rodadas[rodadas.length - 1];
  const rodada = (r, i) => h('div', { class: 'rodada-revisao' },
    rodadas.length > 1 ? h('h3', { class: 'subtitulo' }, r.rodada ? `Rodada ${r.rodada}` : `Rodada ${i + 1}`) : null,
    r.resumo ? md(r.resumo) : null,
    (r.cenas_refazer || []).length ? [h('strong', { class: 'pequeno' }, 'Cenas refeitas'), itens(r.cenas_refazer, (x) => [h('strong', {}, `${x.id}: `), x.motivo,
      x.ajuste_prompt ? h('div', { class: 'mudo pequeno' }, `→ ${x.ajuste_prompt}`) : null])] : null,
    (r.fichas_refazer || []).length ? [h('strong', { class: 'pequeno' }, 'Fichas de personagem refeitas'), itens(r.fichas_refazer, (x) => [h('strong', {}, `${x.id}: `), x.motivo])] : null,
    (r.internet_trocar || []).length ? [h('strong', { class: 'pequeno' }, 'Imagens da internet trocadas'),
      itens(r.internet_trocar, (x) => [h('strong', {}, `${x.id}`), x.opcao != null ? ` → opção ${x.opcao}` : '', x.motivo ? `: ${x.motivo}` : ''])] : null,
    r.thumbnail ? h('p', { class: r.thumbnail.ok === false ? 'texto-erro' : 'mudo' }, r.thumbnail.ok === false ? `✗ Thumbnail: ${r.thumbnail.motivo || 'refazer'}` : '✓ Thumbnail conferida') : null,
    r.quadros ? h('div', {}, h('p', { class: r.quadros.ok === false ? 'texto-erro' : 'mudo', style: { marginBottom: '.3rem' } },
      r.quadros.ok === false ? '✗ Quadros do vídeo com problema' : '✓ Quadros do vídeo conferidos (legenda, destaque, cartelas)'),
    itens(r.quadros.problemas, (x) => (typeof x === 'object' ? valorLegivel(x) : x))) : null);
  const pendentes = problemas(ultima);
  return h('details', { class: 'card', open: pendentes > 0 },
    h('summary', {}, 'Revisão visual', pendentes ? h('span', { class: 'etiqueta erro', style: { marginLeft: '.5rem' } }, `${pendentes} apontamento${pendentes === 1 ? '' : 's'}`)
      : h('span', { class: 'etiqueta ok', style: { marginLeft: '.5rem' } }, 'sem problemas')),
    rodadas.map(rodada));
}

const STATUS_CENA = { bloqueado: 'bloqueada pelo filtro', erro: 'erro', faltando: 'sem imagem', provisorio: 'provisória', pendente: 'pendente' };
const ORIGEM_CENA = { internet: 'internet', ia: 'gerada', gerada: 'gerada', provisorio: 'provisória', ficha: 'ficha' };

// Grade das cenas com a imagem de cada uma. Marcar e pedir "refazer" enfileira a etapa refazer-cenas com o
// ajuste opcional (o diretor visual o leva para o prompt).
function gradeCenas(slug, cenas, { rodando }) {
  if (!cenas || !cenas.length) return null;
  const marcadas = new Set();
  const ajuste = h('textarea', { rows: 2, placeholder: 'O que mudar (opcional). Ex.: o rosto da Moranguete mais redondo, menos gente ao fundo, sem texto na parede' });
  const botao = h('button', {
    class: 'btn primario', disabled: true, onclick: async () => {
      botao.disabled = true;
      try { await executar('refazer-cenas', { cenas: [...marcadas], ajuste: ajuste.value.trim() }); } catch (e) { botao.disabled = false; falhou(e); }
    },
  }, 'Refazer as cenas marcadas');
  const atualizar = () => {
    botao.disabled = !marcadas.size;
    botao.textContent = marcadas.size ? `Refazer ${marcadas.size} cena${marcadas.size === 1 ? '' : 's'}` : 'Refazer as cenas marcadas';
  };
  const cards = cenas.map((c) => {
    const url = urlDoProjeto(slug, c.imagem, { recente: true });
    const problema = c.status && c.status !== 'ok';
    const card = h('div', { class: `cena ${problema ? 'com-problema' : ''}` },
      url ? h('a', { class: 'cena-imagem', href: url, target: '_blank', rel: 'noopener', title: 'abrir a imagem' }, h('img', { src: url, alt: `cena ${c.id}`, loading: 'lazy' }))
        : h('div', { class: 'cena-imagem vazia' }, h('span', { class: 'mudo pequeno' }, 'sem imagem')),
      h('div', { class: 'cena-corpo' },
        h('div', { class: 'linha' }, h('strong', {}, c.id), c.bloco ? h('span', { class: 'mudo pequeno' }, c.bloco) : null, h('span', { class: 'espaco' }),
          c.origem ? h('span', { class: 'etiqueta' }, ORIGEM_CENA[c.origem] || c.origem) : null,
          problema ? h('span', { class: 'etiqueta erro' }, STATUS_CENA[c.status] || c.status) : null),
        c.descricao ? h('div', { class: 'pequeno mudo cena-descricao', title: c.descricao }, c.descricao) : null,
        rodando ? null : h('label', { class: 'pequeno marcar-cena' }, h('input', {
          type: 'checkbox', onchange: (e) => {
            if (e.target.checked) marcadas.add(c.id); else marcadas.delete(c.id);
            card.classList.toggle('marcada', e.target.checked);
            atualizar();
          },
        }), ' refazer')));
    return card;
  });
  const comProblema = cenas.filter((c) => c.status && c.status !== 'ok').length;
  return h('details', { class: 'card', open: comProblema > 0 || undefined },
    h('summary', {}, `Cenas (${cenas.length})`, comProblema ? h('span', { class: 'etiqueta erro', style: { marginLeft: '.5rem' } }, `${comProblema} com problema`) : null),
    rodando ? null : h('p', { class: 'mudo pequeno' }, 'Marque as cenas que quer refazer e, se quiser, diga o que mudar. As da internet trocam pela próxima opção da busca; as geradas são geradas de novo (com custo). Se o vídeo já foi montado, produza de novo depois para ele usar as imagens novas: só o que mudou é refeito.'),
    h('div', { class: 'grade-cenas' }, cards),
    rodando ? null : h('div', { class: 'refazer-cenas' },
      h('div', { class: 'campo' }, h('label', {}, 'Ajuste para as cenas marcadas'), ajuste),
      h('div', { class: 'linha', style: { marginTop: '.6rem' } }, h('span', { class: 'espaco' }), botao)));
}

// ---------------------------------------------------------------- desempenho do vídeo publicado

const JANELAS = { '48h': '48 horas', '7d': '7 dias', '28d': '28 dias' };
const METRICAS = [
  ['ctr', 'CTR (%)', 'cliques ÷ impressões'],
  ['retencao_30s', 'Retenção aos 30 s (%)', 'quem ainda assistia aos 30 s'],
  ['retencao_media', 'Retenção média (%)', 'porcentagem média assistida'],
  ['viu_vs_passou', 'Viu vs. passou (%)', 'shorts: assistiu em vez de passar'],
  ['views', 'Visualizações', ''],
  ['inscritos_ganhos', 'Inscritos ganhos', ''],
];

// Registra as métricas do vídeo publicado por janela (48h, 7d, 28d), ligadas às técnicas e ao gancho do
// roteiro; o servidor refaz o resumo do que funciona no canal, que o estrategista e o roteirista leem.
async function secaoDesempenho(slug) {
  const raiz = h('section', { class: 'card' });
  const url = `/api/projetos/${encodeURIComponent(slug)}/desempenho`;
  let registros = [];
  const carregar = async () => {
    try { registros = listaDeDados(await api('GET', url), 'registros', 'desempenho', 'itens'); } catch (e) { console.error(e); registros = []; }
  };
  const desenhar = () => {
    const ultimo = registros[registros.length - 1] || {};
    const videoId = h('input', { type: 'text', value: ultimo.video_id || '', placeholder: 'ex.: DYAYD0UYvvE (o código depois de watch?v=)' });
    const janela = h('select', {}, Object.entries(JANELAS).map(([id, nome]) => h('option', { value: id, selected: id === '7d' }, nome)));
    const campos = Object.fromEntries(METRICAS.map(([id]) => [id, h('input', { type: 'number', step: 'any', min: '0' })]));
    const obs = h('input', { type: 'text', placeholder: 'observação (opcional): ex.: thumbnail trocada no dia 3' });
    const salvar = h('button', {
      class: 'btn primario', onclick: async () => {
        const id = videoId.value.trim().replace(/^.*(?:v=|youtu\.be\/|shorts\/)([\w-]{6,})\b.*$/, '$1');
        if (!id) { videoId.focus(); return aviso('Escreva o código do vídeo no YouTube.'); }
        const metricas = {};
        for (const [k, el] of Object.entries(campos)) if (el.value !== '') metricas[k] = Number(el.value);
        if (!Object.keys(metricas).length) return aviso('Preencha pelo menos uma métrica.');
        salvar.disabled = true;
        try {
          await api('POST', url, { video_id: id, janela: janela.value, metricas, obs: obs.value.trim() || null });
          aviso(`Métricas de ${JANELAS[janela.value]} registradas. O resumo do que funciona no canal foi atualizado.`, 'ok');
          await carregar();
          desenhar();
        } catch (e) { salvar.disabled = false; falhou(e); }
      },
    }, 'Registrar métricas');
    const valorMetrica = (r, k) => {
      const v = (r.metricas || r)[k];
      return v == null || v === '' ? '—' : typeof v === 'number' ? v.toLocaleString('pt-BR') : String(v);
    };
    trocar(raiz,
      h('h2', {}, 'Desempenho do vídeo publicado'),
      h('p', { class: 'mudo pequeno' }, 'Depois de publicar, copie os números do YouTube Studio para cada janela. Eles ficam ligados às técnicas e ao gancho deste roteiro, e o sistema aprende o que funciona no canal. Compare sempre a mesma janela entre vídeos.'),
      registros.length ? h('div', { class: 'rolagem' }, h('table', { class: 'tabela-desempenho' },
        h('thead', {}, h('tr', {}, h('th', {}, 'Janela'), h('th', {}, 'Vídeo'), METRICAS.map(([, nome]) => h('th', {}, nome)), h('th', {}, 'Registrado'))),
        h('tbody', {}, registros.map((r) => h('tr', { title: r.obs || '' }, h('td', {}, JANELAS[r.janela] || r.janela), h('td', {}, h('code', {}, r.video_id || '')),
          METRICAS.map(([k]) => h('td', { class: 'valor' }, valorMetrica(r, k))), h('td', { class: 'mudo pequeno' }, quando(r.registrado_em)))))))
        : h('p', { class: 'mudo pequeno' }, 'Nenhuma métrica registrada ainda.'),
      h('div', { class: 'grade', style: { marginTop: '1rem' } },
        h('div', { class: 'campo' }, h('label', {}, 'Vídeo no YouTube'), videoId),
        h('div', { class: 'campo' }, h('label', {}, 'Janela'), janela),
        METRICAS.map(([id, nome, ajuda]) => h('div', { class: 'campo' }, h('label', {}, nome), campos[id], ajuda ? h('div', { class: 'ajuda' }, ajuda) : null)),
        h('div', { class: 'campo largo' }, h('label', {}, 'Observação'), obs)),
      h('div', { class: 'linha', style: { marginTop: '.8rem' } }, h('span', { class: 'mudo pequeno espaco' }, 'Registrar de novo a mesma janela do mesmo vídeo substitui os números.'), salvar));
  };
  await carregar();
  desenhar();
  return raiz;
}

// ---------------------------------------------------------------- 8. produção

async function telaProducao(corpo) {
  const slug = estado.projeto.slug;
  const teste = h('input', { type: 'checkbox' });
  const opcoesProducao = () => h('div', { class: 'card' },
    h('label', { class: 'interruptor' }, teste, 'Modo teste: voz robótica local (espeak) e quadros provisórios, sem gastar com APIs'),
    h('p', { class: 'mudo pequeno', style: { marginTop: '.5rem' } }, 'Sem o modo teste, as imagens e a narração são cobradas nas chaves de API cadastradas. Os provedores e os modelos ficam em ',
      h('a', { href: '#/configuracoes/midia' }, 'Configurações › Imagens e voz'), '.'));
  const iniciar = () => executar('producao', { teste: teste.checked });
  await etapaComExecucao(corpo, 'producao', {
    titulo: 'Produção',
    explicacao: 'Imagens (internet e IA, com as fichas dos personagens), revisão visual, narração com as vozes do pedido, alinhamento palavra a palavra e montagem com legendas e música.',
    rotuloIniciar: 'Produzir o vídeo',
    iniciar,
    carregar: () => api('GET', `/api/projetos/${encodeURIComponent(slug)}/producao`),
    temDados: (d) => (d.videos || []).length > 0 || !!d.folha_de_contato || !!d.publicacao || !!d.thumbnail || (d.cenas || []).some((c) => c.imagem),
    mostrar: async (d, { rodando }) => {
      const videos = d.videos || [];
      return h('div', {},
        rodando ? null : listaDeAvisos(d.avisos, 'Avisos da produção'),
        videos.map((v) => {
          const url = urlDoProjeto(slug, v, { recente: true });
          return h('div', { class: 'card' },
            h('div', { class: 'linha-topo' }, h('h2', {}, 'Vídeo'), h('span', { class: 'mudo pequeno' }, v.split('/').pop()), h('span', { class: 'espaco' }),
              h('a', { class: 'btn pequeno', href: url, download: v.split('/').pop() }, 'Baixar')),
            h('video', { src: url, controls: true, preload: 'metadata' }));
        }),
        d.thumbnail ? h('div', { class: 'card' },
          h('div', { class: 'linha-topo' }, h('h2', {}, 'Thumbnail'), h('span', { class: 'espaco' }),
            h('a', { class: 'btn pequeno', href: urlDoProjeto(slug, d.thumbnail), download: `${slug}-thumbnail.jpg` }, 'Baixar')),
          h('img', { class: 'thumbnail', src: urlDoProjeto(slug, d.thumbnail), alt: 'thumbnail do vídeo' }),
          h('p', { class: 'mudo pequeno' }, 'É também o primeiro quadro do vídeo, o que as plataformas mostram como capa quando você não sobe uma thumbnail.')) : null,
        videos.length && !rodando ? h('div', { class: 'faixa info linha' },
          h('span', { class: 'espaco' }, 'Para dublar em outras línguas, trocar a música ou exportar sem a legenda na imagem, use a edição.'),
          h('a', { class: 'btn primario pequeno', href: `#/p/${encodeURIComponent(slug)}/edicao` }, 'Abrir a edição')) : null,
        cartaoRevisao(d.revisao),
        gradeCenas(slug, d.cenas, { rodando }),
        d.publicacao ? entregaveisPublicacao(d.publicacao) : rodando ? null : h('div', { class: 'card' },
          h('h2', {}, 'Título, descrições e tags'),
          h('p', { class: 'mudo' }, 'Ainda não gerados. O publicador escreve o título, a descrição de cada plataforma e as tags, buscando agora as que estão em alta.'),
          h('button', { class: 'btn primario', onclick: () => executar('publicacao').catch(falhou) }, 'Gerar título, descrições e tags')),
        cartaoCusto(d.custo),
        d.folha_de_contato ? h('details', { class: 'card' }, h('summary', {}, 'Folha de contato das imagens'),
          h('img', { class: 'contato', src: urlDoProjeto(slug, d.folha_de_contato, { recente: true }), alt: 'folha de contato' })) : null,
        d.creditos ? h('details', { class: 'card' }, h('summary', {}, 'Créditos das imagens da internet'), md(d.creditos)) : null,
        videos.length && !rodando ? await secaoDesempenho(slug) : null,
        rodando ? null : h('div', {}, opcoesProducao(), h('div', { class: 'rodape-acoes' }, h('span', { class: 'espaco' }),
          d.publicacao ? h('button', { class: 'btn', onclick: () => confirm('Escrever de novo o título, as descrições e as tags, com uma coleta nova das tags em alta?') && executar('publicacao').catch(falhou) }, 'Refazer textos de publicação') : null,
          videos.length ? h('button', { class: 'btn', onclick: () => confirm('Produzir de novo? Só o que mudou é refeito.') && iniciar().catch(falhou) }, 'Produzir de novo')
            : h('button', { class: 'btn primario', onclick: () => iniciar().catch(falhou) }, 'Produzir o vídeo'))));
    },
  });
  // sem vídeo ainda, as opções ficam acima do botão de começar (ou do "tentar de novo")
  const botao = corpo.querySelector('.card button.btn.primario');
  if (!corpo.contains(teste) && botao) botao.closest('.card').before(opcoesProducao());
}

// ---------------------------------------------------------------- 9. edição

// Depois da produção, o vídeo vira peças: o vídeo sem som e sem legendas, as faixas de voz (a narração original e
// as dublagens, cada uma com a legenda na língua dela) e as músicas da biblioteca. A prévia combina as peças aqui no
// navegador (o vídeo mudo manda, e a voz e a música seguem o tempo dele); a exportação faz a mesma combinação no
// servidor, com a mistura da montagem (a música no volume escolhido o tempo todo, como na prévia).

const MUSICAS = 'biblioteca/musicas/';
const SAIDAS_EXPORTACAO = { video: 'Vídeo (MP4)', audio: 'Faixa de áudio (MP3, com a duração exata do vídeo)' };
const STATUS_DUBLAGEM = { traduzida: 'traduzida, ainda sem voz', incompleta: 'incompleta' };

function segundosDe(textoTempo) {
  const m = /^(\d+):(\d{2})$/.exec(String(textoTempo || '').trim());
  return m ? Number(m[1]) * 60 + Number(m[2]) : 0;
}

function duracaoLegivel(s) {
  if (s == null || Number.isNaN(Number(s))) return '';
  const t = Math.round(Number(s));
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`;
}

// O <track> do navegador só lê WebVTT: a legenda .srt vira VTT para a prévia.
function vttDeSrt(srt) {
  return `WEBVTT\n\n${String(srt || '').replace(/\r/g, '').replace(/(\d{2}:\d{2}:\d{2}),(\d{3})/g, '$1.$2')}`;
}

async function telaEdicao(corpo) {
  const slug = estado.projeto.slug;
  const info = infoEtapa('edicao');
  const rodando = estado.rodando && telaDa(estado.rodando.etapa) === 'edicao' ? estado.rodando : null;
  corpo.append(h('div', { class: 'linha-topo' }, h('h1', {}, 'Edição')),
    h('p', { class: 'mudo' }, 'O vídeo pronto vira peças separadas: o vídeo sem som, as faixas de voz (a narração original e as dublagens, cada uma com a legenda na língua dela) e a música. Combine as peças, confira na prévia e exporte o vídeo ou a faixa de áudio para o YouTube.'));
  if (!info.aplicavel) {
    corpo.append(h('div', { class: 'faixa info' }, 'Esta etapa não vale para este vídeo: o pedido para antes do vídeo montado.'));
    return;
  }
  const d = await api('GET', `/api/projetos/${encodeURIComponent(slug)}/edicao`);
  if (!d.video_final) {
    corpo.append(h('div', { class: 'card' }, h('p', {}, 'A edição parte do vídeo pronto: produza o vídeo antes.'),
      h('a', { class: 'btn primario', href: `#/p/${encodeURIComponent(slug)}/producao` }, 'Ir para a produção')));
    return;
  }
  if (rodando) {
    corpo.append(painelLog(rodando, () => rota(), { titulo: `${nomeEtapa(rodando.etapa)}: o sistema está trabalhando` }));
  } else if (info.execucao && FALHAS[info.execucao.status]) {
    corpo.append(cartaoFalha(info.execucao, null));
  } else if (info.execucao) {
    corpo.append(cartaoResumo(info.execucao) || '');
  }
  corpo.append(mesaDeEdicao(slug, d, !!rodando), cartaoDublagens(slug, d, !!rodando), cartaoExportacoes(slug, d, !!rodando));
}

// A combinação: a prévia à esquerda e as peças à direita. A escolha fica guardada neste navegador.
function mesaDeEdicao(slug, d, rodando) {
  const vozes = d.vozes.filter((v) => v.audio);
  const comLegenda = d.vozes.filter((v) => v.ass && v.srt);
  const pedido = d.musica_do_pedido || {};
  const chave = `edicao:${slug}`;
  const guardado = lembrado(chave) || {};
  const temVoz = (id) => vozes.some((v) => v.id === id);
  const mix = {
    voz: 'voz' in guardado && (guardado.voz === null || temVoz(guardado.voz)) ? guardado.voz : (vozes[0] ? vozes[0].id : null),
    musica: 'musica' in guardado ? guardado.musica : (pedido.ativa ? pedido.arquivo : null),
    volume: Number.isFinite(guardado.volume) ? guardado.volume : (pedido.volume ?? 20),
    inicio: guardado.inicio || pedido.inicio || '0:00',
    legendas: 'legendas' in guardado ? guardado.legendas : null,
    saidas: Array.isArray(guardado.saidas) && guardado.saidas.length ? guardado.saidas : Object.keys(SAIDAS_EXPORTACAO),
  };
  if (mix.musica && !d.musicas.includes(mix.musica.slice(MUSICAS.length))) mix.musica = null;
  if (mix.legendas && !comLegenda.some((v) => v.id === mix.legendas)) mix.legendas = null;
  const guardar = () => lembrar(chave, mix);

  // ------------------------------------------------ prévia
  const silencioso = !!d.video; // o vídeo-base não tem som: o volume dele vale para a prévia inteira
  const video = h('video', { src: urlDoProjeto(slug, d.video ? d.video.arquivo : d.video_final, { recente: true }), controls: true,
    preload: 'metadata', playsinline: true });
  video.muted = !silencioso;
  const somVoz = new Audio();
  const somMusica = new Audio();
  somMusica.loop = true;
  const volumes = () => {
    const mestre = silencioso ? (video.muted ? 0 : video.volume) : 1;
    somVoz.volume = mestre;
    somMusica.volume = Math.min(1, mestre * (mix.voz ? mix.volume / 100 : 1));
  };
  const alvoMusica = () => {
    const t = segundosDe(mix.inicio) + video.currentTime;
    return somMusica.duration ? t % somMusica.duration : t;
  };
  const tocar = (som) => { if (som.src && !video.paused) som.play().catch(() => {}); };
  const alinhar = (forcar) => {
    if (somVoz.src) {
      if (somVoz.duration && video.currentTime >= somVoz.duration) somVoz.pause();
      else if (forcar || Math.abs(somVoz.currentTime - video.currentTime) > 0.25) somVoz.currentTime = video.currentTime;
    }
    if (somMusica.src && (forcar || Math.abs(somMusica.currentTime - alvoMusica()) > 0.4)) somMusica.currentTime = alvoMusica();
  };
  const seguir = () => { alinhar(true); tocar(somVoz); tocar(somMusica); };
  video.addEventListener('play', seguir);
  video.addEventListener('seeked', seguir);
  video.addEventListener('pause', () => { somVoz.pause(); somMusica.pause(); });
  video.addEventListener('timeupdate', () => { if (!video.paused) alinhar(false); });
  video.addEventListener('ratechange', () => { somVoz.playbackRate = video.playbackRate; somMusica.playbackRate = video.playbackRate; });
  video.addEventListener('volumechange', () => { if (!silencioso && !video.muted) video.muted = true; volumes(); });
  somVoz.addEventListener('loadedmetadata', seguir);
  somMusica.addEventListener('loadedmetadata', seguir);
  const fonte = (som, url) => {
    if ((som.getAttribute('src') || null) === url) return;
    som.pause();
    if (url) som.src = url; else som.removeAttribute('src');
    som.load();
  };
  const aplicarVoz = () => {
    const v = vozes.find((x) => x.id === mix.voz);
    fonte(somVoz, v ? urlDoProjeto(slug, v.audio, { recente: true }) : null);
    volumes();
  };
  const aplicarMusica = () => { fonte(somMusica, mix.musica ? urlDoArquivo(mix.musica) : null); volumes(); };
  let versaoLegenda = 0;
  let urlTrilha = null;
  const aplicarLegendas = async () => {
    const vez = ++versaoLegenda;
    video.querySelectorAll('track').forEach((t) => t.remove());
    if (urlTrilha) { URL.revokeObjectURL(urlTrilha); urlTrilha = null; }
    const v = comLegenda.find((x) => x.id === mix.legendas);
    if (!v) return;
    try {
      const srt = await lerTexto(urlDoProjeto(slug, v.srt, { recente: true }));
      if (srt == null || vez !== versaoLegenda) return;
      urlTrilha = URL.createObjectURL(new Blob([vttDeSrt(srt)], { type: 'text/vtt' }));
      const trilha = h('track', { kind: 'subtitles', label: v.nome, srclang: v.idioma, src: urlTrilha, default: true });
      video.append(trilha);
      trilha.track.mode = 'showing';
    } catch (e) { console.error(e); }
  };
  if (pararPrevia) pararPrevia();
  pararPrevia = () => { somVoz.pause(); somMusica.pause(); video.pause(); if (urlTrilha) URL.revokeObjectURL(urlTrilha); };

  // ------------------------------------------------ peças
  const listaVozes = h('div', { class: 'faixas-voz' });
  const desenharVozes = () => trocar(listaVozes, [...vozes, null].map((v) => {
    const id = v ? v.id : null;
    const radio = h('input', {
      type: 'radio', name: `voz-${slug}`, checked: mix.voz === id,
      onchange: () => { mix.voz = id; guardar(); aplicarVoz(); desenharVozes(); atualizarExportar(); },
    });
    const detalhe = v ? [v.voz, v.fornecedor, duracaoLegivel(v.duracao_s)].filter(Boolean).join(' · ') : 'só a música';
    return h('label', { class: `faixa-voz ${mix.voz === id ? 'escolhida' : ''}` }, radio,
      h('span', { class: 'faixa-nome' }, v ? v.nome : 'Sem voz'), h('span', { class: 'mudo pequeno' }, detalhe),
      v && v.desatualizada ? h('span', { class: 'etiqueta erro', title: v.desatualizada }, 'desatualizada') : null);
  }));
  desenharVozes();

  const selMusica = h('select', { onchange: (e) => { mix.musica = e.target.value || null; guardar(); aplicarMusica(); atualizarExportar(); } },
    h('option', { value: '' }, 'Sem música'),
    d.musicas.map((m) => h('option', { value: MUSICAS + m, selected: mix.musica === MUSICAS + m },
      m + (pedido.arquivo === MUSICAS + m ? ' (a do pedido)' : ''))));
  const enviar = h('input', {
    type: 'file', accept: 'audio/*', onchange: async (e) => {
      const f = e.target.files[0];
      if (!f) return;
      try {
        const r = await enviarArquivo('musica', f);
        if (!d.musicas.includes(r.nome)) {
          d.musicas.push(r.nome);
          if (opcoes && !opcoes.musicas.includes(r.nome)) opcoes.musicas.push(r.nome);
          selMusica.append(h('option', { value: MUSICAS + r.nome }, r.nome));
        }
        selMusica.value = MUSICAS + r.nome;
        mix.musica = MUSICAS + r.nome;
        guardar();
        aplicarMusica();
        atualizarExportar();
        aviso(`Música ${r.nome} guardada na biblioteca de músicas.`, 'ok');
      } catch (err) { falhou(err); }
    },
  });
  const rotuloVolume = h('span', { class: 'pequeno valor-volume' }, `${mix.volume}%`);
  const volume = h('input', {
    type: 'range', min: 0, max: 100, step: 1, value: mix.volume,
    oninput: (e) => { mix.volume = Number(e.target.value); rotuloVolume.textContent = `${mix.volume}%`; volumes(); },
    onchange: guardar,
  });
  const inicio = h('input', {
    type: 'text', value: mix.inicio, placeholder: '0:00', class: 'curto', onchange: (e) => {
      const v = e.target.value.trim();
      if (v && !/^\d{1,3}:\d{2}$/.test(v)) { aviso('O início da música é no formato m:ss (ex.: 1:05).', 'erro'); e.target.value = mix.inicio; return; }
      mix.inicio = v || '0:00';
      guardar();
      alinhar(true);
    },
  });

  const selLegendas = h('select', { onchange: (e) => { mix.legendas = e.target.value || null; guardar(); aplicarLegendas(); atualizarExportar(); } },
    h('option', { value: '' }, 'Sem legenda na imagem'),
    comLegenda.map((v) => h('option', { value: v.id, selected: mix.legendas === v.id }, v.nome)));

  const caixas = {};
  const saidas = Object.entries(SAIDAS_EXPORTACAO).map(([id, nome]) => {
    caixas[id] = h('input', {
      type: 'checkbox', checked: mix.saidas.includes(id),
      onchange: () => { mix.saidas = Object.keys(SAIDAS_EXPORTACAO).filter((k) => caixas[k].checked); guardar(); atualizarExportar(); },
    });
    return h('label', { class: 'pequeno' }, caixas[id], ` ${nome}`);
  });
  const nome = h('input', { type: 'text', placeholder: 'nome dos arquivos (automático: en-com-musica...)' });
  const resumo = h('p', { class: 'mudo pequeno' });
  const botao = h('button', {
    class: 'btn primario', onclick: async () => {
      botao.disabled = true;
      try {
        await executar('exportar', { voz: mix.voz, musica: mix.musica, volume: mix.volume, inicio: mix.inicio, legendas: mix.legendas,
          saidas: mix.saidas, nome: nome.value.trim() || undefined });
      } catch (e) { falhou(e); atualizarExportar(); }
    },
  }, 'Exportar esta combinação');
  const atualizarExportar = () => {
    const problema = !mix.saidas.length ? 'Escolha o que exportar.'
      : !mix.voz && !mix.musica && mix.saidas.includes('audio') ? 'Sem voz e sem música não há áudio para exportar.' : '';
    botao.disabled = rodando || !!problema;
    const vozNome = (vozes.find((v) => v.id === mix.voz) || {}).nome;
    const legNome = (comLegenda.find((v) => v.id === mix.legendas) || {}).nome;
    resumo.textContent = problema || [
      `Voz: ${vozNome || 'nenhuma'}`,
      `música: ${mix.musica ? `${mix.musica.slice(MUSICAS.length)} a ${mix.volume}%` : 'nenhuma'}`,
      `legenda na imagem: ${legNome || 'nenhuma'}`,
      rodando ? 'espere a execução em andamento terminar' : null,
    ].filter(Boolean).join(' · ');
  };
  atualizarExportar();
  aplicarVoz();
  aplicarMusica();
  aplicarLegendas();

  const previa = h('div', { class: 'previa' }, video,
    d.video ? h('div', { class: 'linha mudo pequeno' },
      h('span', { class: 'espaco' }, `Vídeo sem som e sem legendas${d.video.duracao_s ? ` · ${duracaoLegivel(d.video.duracao_s)}` : ''}`),
      h('a', { href: urlDoProjeto(slug, d.video.arquivo), download: `${slug}-sem-som.mp4` }, 'Baixar'))
      : h('div', { class: 'faixa info pequeno' }, 'Este vídeo foi montado antes da edição: a prévia mostra o vídeo final, com a legenda original na imagem e o som dele desligado. O vídeo sem som e sem legendas é montado na primeira exportação, sem custo.'),
    h('p', { class: 'mudo pequeno' }, 'Com voz, a música fica no volume escolhido do começo ao fim, na prévia e no arquivo exportado.'));
  return h('div', { class: 'card' },
    h('h2', {}, 'Combinar e exportar'),
    h('div', { class: 'mesa' }, previa,
      h('div', { class: 'controles' },
        h('div', { class: 'campo' }, h('label', {}, 'Voz'), listaVozes),
        h('div', { class: 'campo' }, h('label', {}, 'Música de fundo'), selMusica,
          h('div', { class: 'volume' }, h('span', { class: 'mudo pequeno' }, 'volume por baixo da voz'), volume, rotuloVolume),
          h('div', { class: 'linha' }, h('span', { class: 'mudo pequeno' }, 'começa em'), inicio),
          enviar, h('div', { class: 'ajuda' }, 'Outra música da biblioteca, ou carregue um arquivo (ele fica na biblioteca, para os outros vídeos também).')),
        h('div', { class: 'campo' }, h('label', {}, 'Legenda na imagem'), selLegendas,
          h('div', { class: 'ajuda' }, 'Para o YouTube, prefira sem legenda na imagem e suba o .srt de cada língua: o espectador escolhe a dele.')),
        h('div', { class: 'campo' }, h('label', {}, 'Exportar'), h('div', { class: 'saidas' }, saidas), nome, resumo,
          h('div', { class: 'linha' }, h('span', { class: 'espaco' }), botao)))));
}

// As dublagens feitas e o pedido de uma nova: as línguas, o fornecedor e a voz do narrador (os personagens usam as
// vozes deles do pedido).
function cartaoDublagens(slug, d, rodando) {
  const dublagens = d.vozes.filter((v) => !v.original);
  const feitas = new Set(dublagens.map((v) => v.id));
  const marcadas = new Set();
  const botao = h('button', { class: 'btn primario', disabled: true }, 'Dublar');
  const atualizar = () => {
    botao.disabled = rodando || !marcadas.size;
    botao.textContent = marcadas.size ? `Dublar em ${marcadas.size} língua${marcadas.size === 1 ? '' : 's'}` : 'Dublar';
  };
  const linguas = h('div', { class: 'chips' }, d.idiomas.map((i) => h('label', { class: 'chip escolha' },
    h('input', { type: 'checkbox', disabled: rodando, onchange: (e) => { if (e.target.checked) marcadas.add(i.id); else marcadas.delete(i.id); atualizar(); } }),
    i.nome, feitas.has(i.id) ? h('small', {}, 'refazer') : null)));
  let fornecedor = d.narracao.fornecedor === 'espeak' ? ((d.fornecedores.find((f) => f.pronto && f.id !== 'espeak') || {}).id || 'gemini') : d.narracao.fornecedor;
  let voz = '';
  const lugarVoz = h('div');
  const desenharVoz = () => {
    fornecedorVoz = fornecedor; // o seletor de voz lista as vozes deste fornecedor
    voz = '';
    trocar(lugarVoz, seletorVoz('', (v) => { voz = v; }, `a mesma da narração${d.narracao.voz ? ` (${d.narracao.voz})` : ''}`, d.narracao.voz));
  };
  const selFornecedor = h('select', { onchange: (e) => { fornecedor = e.target.value; desenharVoz(); } },
    d.fornecedores.filter((f) => f.id !== 'espeak').map((f) => h('option', { value: f.id, selected: f.id === fornecedor, disabled: !f.pronto && f.id !== fornecedor },
      f.nome + (f.pronto ? '' : ' (sem chave)'))));
  desenharVoz();
  const teste = h('input', { type: 'checkbox' });
  const retraduzir = h('input', { type: 'checkbox' });
  botao.addEventListener('click', async () => {
    botao.disabled = true;
    try {
      await executar('dublagem', { idiomas: [...marcadas], fornecedor: teste.checked ? undefined : fornecedor, voz: voz || undefined,
        teste: teste.checked, retraduzir: retraduzir.checked });
    } catch (e) { falhou(e); atualizar(); }
  });

  const refazer = (v) => {
    if (!confirm(`Gerar de novo a dublagem em ${v.nome}? A tradução que ainda vale é reaproveitada; os trechos que mudaram são falados de novo (com custo).`)) return;
    const espeak = v.fornecedor === 'espeak';
    executar('dublagem', { idiomas: [v.id], fornecedor: espeak ? undefined : v.fornecedor, voz: v.voz || undefined, teste: espeak }).catch(falhou);
  };
  const apagar = async (v) => {
    if (!confirm(`Apagar a dublagem em ${v.nome}, com a tradução e as legendas? As exportações já feitas ficam.`)) return;
    try { await api('DELETE', `/api/projetos/${encodeURIComponent(slug)}/dublagens/${encodeURIComponent(v.id)}`); rota(); } catch (e) { falhou(e); }
  };
  const lista = dublagens.map((v) => h('div', { class: 'item-edicao' },
    h('div', { class: 'linha' }, h('strong', {}, v.nome),
      h('span', { class: 'mudo pequeno' }, [v.voz, v.fornecedor, duracaoLegivel(v.duracao_s), v.trechos ? `${v.trechos} trechos` : null, quando(v.gerada_em)].filter(Boolean).join(' · ')),
      v.status !== 'pronta' ? h('span', { class: 'etiqueta erro' }, STATUS_DUBLAGEM[v.status] || v.status) : null,
      v.desatualizada ? h('span', { class: 'etiqueta erro', title: v.desatualizada }, 'desatualizada') : null,
      v.acelerados ? h('span', { class: 'etiqueta sugestao', title: 'trechos que não couberam no tempo da frase original e ficaram acelerados' },
        `${v.acelerados} trecho${v.acelerados === 1 ? '' : 's'} rápido${v.acelerados === 1 ? '' : 's'}`) : null,
      h('span', { class: 'espaco' }),
      v.audio ? h('a', { class: 'btn pequeno', href: urlDoProjeto(slug, v.audio, { recente: true }), download: `${slug}-${v.id}.wav` }, 'Voz (WAV)') : null,
      v.srt ? h('a', { class: 'btn pequeno', href: urlDoProjeto(slug, v.srt, { recente: true }), download: `${slug}-${v.id}.srt` }, 'Legenda (SRT)') : null,
      rodando ? null : h('button', { class: 'btn pequeno', onclick: () => refazer(v) }, 'Refazer'),
      rodando ? null : h('button', { class: 'btn pequeno perigo', onclick: () => apagar(v) }, 'Apagar')),
    v.desatualizada ? h('div', { class: 'mudo pequeno' }, `Desatualizada: ${v.desatualizada}. Refaça para acompanhar o vídeo atual.`) : null,
    listaDeAvisos(v.avisos),
    v.publicacao ? publicacaoTraduzida(slug, v) : null));

  return h('div', { class: 'card' },
    h('h2', {}, 'Dublagens'),
    h('p', { class: 'mudo pequeno' }, 'Cada frase da narração é traduzida para caber no tempo da frase original, falada com as vozes do vídeo e encaixada no lugar dela, para a fala acompanhar as imagens. A voz acelera ou desacelera um pouco quando precisa, e a frase que não cabe volta ao tradutor para uma versão mais curta. A faixa sai com a duração exata do vídeo, e a legenda, o título e a descrição na língua nova saem junto.'),
    lista.length ? lista : null,
    rodando ? null : h('div', { class: 'nova-dublagem' },
      h('div', { class: 'campo' }, h('label', {}, 'Línguas'), linguas),
      h('div', { class: 'grade' },
        h('div', { class: 'campo' }, h('label', {}, 'Fornecedor da voz'), selFornecedor),
        h('div', { class: 'campo' }, h('label', {}, 'Voz do narrador'), lugarVoz)),
      h('label', { class: 'interruptor' }, teste, 'Modo teste: voz robótica local (espeak), sem gastar com voz (a tradução usa o modelo de linguagem configurado)'),
      h('label', { class: 'interruptor' }, retraduzir, 'Traduzir de novo, sem reaproveitar a tradução que já existe'),
      h('p', { class: 'mudo pequeno' }, 'O modelo do tradutor fica em ', h('a', { href: '#/configuracoes/modelos' }, 'Configurações › Modelos das etapas'), ' (etapa Edição).'),
      h('div', { class: 'linha' }, h('span', { class: 'espaco' }), botao)));
}

// O título, as descrições e as tags na língua da dublagem: o que o YouTube Studio pede em Idiomas, ao lado da faixa
// de áudio e da legenda.
function publicacaoTraduzida(slug, v) {
  const p = v.publicacao;
  const [abas, texto] = descricoesPorPlataforma(p);
  return h('details', { class: 'publicacao-traduzida' },
    h('summary', {}, `Título e descrição em ${v.nome}`,
      p.desatualizada ? h('span', { class: 'etiqueta erro', style: { marginLeft: '.5rem' } }, 'desatualizados') : null),
    p.desatualizada ? h('div', { class: 'faixa alerta pequeno' },
      `${p.desatualizada}: refaça a dublagem para traduzir de novo (a voz e as legendas que não mudaram são reaproveitadas).`) : null,
    h('div', { class: 'linha' }, h('span', { class: 'titulo-final espaco' }, p.titulo), botaoCopiar(p.titulo, 'Copiar título')),
    (p.titulos_alternativos || []).length ? h('div', { class: 'mudo pequeno' }, 'Alternativas: ',
      p.titulos_alternativos.map((t, i) => h('span', {}, i ? ' · ' : '', t))) : null,
    (p.plataformas || []).length ? h('div', { style: { marginTop: '.6rem' } }, abas, texto) : null,
    p.tags_youtube ? h('div', { class: 'campo', style: { marginTop: '.8rem' } },
      h('label', {}, 'Campo de tags do YouTube'),
      h('textarea', { rows: 2, readOnly: true, value: p.tags_youtube }),
      h('div', { class: 'linha' }, h('span', { class: 'mudo pequeno espaco' }, 'As tags do tema, traduzidas: as tags em alta não foram coletadas nesta língua.'),
        botaoCopiar(p.tags_youtube, 'Copiar tags'))) : null,
    listaDeAvisos(p.avisos, 'Para conferir'),
    h('p', { class: 'mudo pequeno', style: { marginTop: '.6rem' } },
      p.fonte ? 'Traduzidos dos textos de publicação do vídeo original.' : 'O vídeo original ainda não tinha textos de publicação: estes foram escritos a partir da narração traduzida.',
      p.md ? [' ', h('a', { href: urlDoProjeto(slug, p.md, { recente: true }), download: `${slug}-${v.id}-publicacao.md` }, 'Baixar (.md)')] : null));
}

function cartaoExportacoes(slug, d, rodando) {
  const nomeDaFaixa = (id) => (d.vozes.find((v) => v.id === id) || {}).nome || id;
  const apagar = async (e) => {
    if (!confirm(`Apagar a exportação ${e.nome}?`)) return;
    try { await api('DELETE', `/api/projetos/${encodeURIComponent(slug)}/exportacoes/${encodeURIComponent(e.nome)}`); rota(); } catch (err) { falhou(err); }
  };
  const itens = (d.exportacoes || []).map((e) => {
    const r = e.receita || {};
    const a = e.arquivos || {};
    const url = (c) => urlDoProjeto(slug, c, { recente: true });
    return h('div', { class: 'item-edicao' },
      h('div', { class: 'linha' }, h('strong', {}, e.nome),
        h('span', { class: 'mudo pequeno' }, [duracaoLegivel(e.duracao_s), quando(e.criada_em)].filter(Boolean).join(' · ')),
        h('span', { class: 'espaco' }),
        a.video ? h('a', { class: 'btn pequeno', href: url(a.video), download: `${e.nome}.mp4` }, 'MP4') : null,
        a.audio ? h('a', { class: 'btn pequeno', href: url(a.audio), download: `${e.nome}.mp3` }, 'MP3') : null,
        a.srt ? h('a', { class: 'btn pequeno', href: url(a.srt), download: `${e.nome}.srt` }, 'SRT') : null,
        rodando ? null : h('button', { class: 'btn pequeno perigo', onclick: () => apagar(e) }, 'Apagar')),
      h('div', { class: 'mudo pequeno' }, [
        `voz: ${r.voz ? nomeDaFaixa(r.voz) : 'nenhuma'}`,
        `música: ${r.musica ? `${String(r.musica).replace(MUSICAS, '')} a ${r.volume}%` : 'nenhuma'}`,
        `legenda na imagem: ${r.legendas ? nomeDaFaixa(r.legendas) : 'nenhuma'}`,
      ].join(' · ')),
      a.video ? h('details', {}, h('summary', { class: 'pequeno' }, 'assistir'), h('video', { src: url(a.video), controls: true, preload: 'none' })) : null);
  });
  return h('div', { class: 'card' },
    h('h2', {}, 'Exportações'),
    itens.length ? itens : h('p', { class: 'mudo' }, 'Nenhuma ainda. Escolha a voz, a música e a legenda acima e exporte.'),
    h('details', { class: 'ajuda-youtube' }, h('summary', {}, 'Como publicar várias línguas num vídeo só do YouTube'),
      h('ol', { class: 'pequeno' },
        h('li', {}, 'Exporte o vídeo com a voz original, a música e sem legenda na imagem, e suba esse MP4.'),
        h('li', {}, 'Exporte cada dublagem como faixa de áudio (MP3), com a mesma música: no YouTube, a faixa de outra língua substitui o som inteiro do vídeo, e ela já sai com a duração exata dele.'),
        h('li', {}, 'No YouTube Studio, abra o vídeo, vá em Legendas e adicione cada língua: em Dublagem, envie o MP3 dela; em Legendas, o SRT; em Título e descrição, os textos traduzidos (em Dublagens, acima).'),
        h('li', {}, 'O envio de faixas de áudio ainda não está liberado para todos os canais. Sem ele, publique um vídeo por língua com o MP4 exportado (com a dublagem e, se quiser, a legenda na imagem).'))));
}

// ---------------------------------------------------------------- biblioteca

const SUBABAS_BIBLIOTECA = { personagens: 'Personagens', referencias: 'Referências' };

async function telaBiblioteca(sub, item) {
  if (!SUBABAS_BIBLIOTECA[sub]) {
    sub = 'personagens';
    item = null;
    history.replaceState(null, '', '#/biblioteca/personagens');
  }
  document.getElementById('topo-projeto').textContent = `· Biblioteca · ${SUBABAS_BIBLIOTECA[sub]}`;
  const conteudo = h('div');
  trocar(app, h('h1', {}, 'Biblioteca'),
    h('p', { class: 'mudo' }, 'O que vale para todos os vídeos: os personagens que voltam de vídeo em vídeo e as estruturas tiradas de vídeos de referência.'),
    h('nav', { class: 'subabas' }, Object.entries(SUBABAS_BIBLIOTECA).map(([id, nome]) =>
      h('a', { href: `#/biblioteca/${id}`, class: id === sub ? 'ativa' : null }, nome))),
    conteudo);
  if (sub === 'personagens' && item) await telaRascunho(conteudo, item);
  else if (sub === 'personagens') await telaPersonagens(conteudo);
  else await telaReferencias(conteudo);
}

// Execuções sem projeto (criar personagem, analisar referência): o servidor pode dizer qual está ativa; senão,
// vale a última que esta aba disparou (lembrada no navegador), para continuar acompanhando depois de recarregar.
async function estadoDaExecucao(id) {
  try { return { id, ...(await api('GET', `/api/execucoes/${id}?desde=${Number.MAX_SAFE_INTEGER}`)) }; } catch { return null; }
}

async function execucaoParaAcompanhar(chave, doServidor) {
  if (doServidor && doServidor.id && (doServidor.status == null || ativa(doServidor.status))) {
    return doServidor.status ? doServidor : (await estadoDaExecucao(doServidor.id)) || doServidor;
  }
  const id = lembrado(chave);
  if (doServidor && doServidor.id && (!id || doServidor.id >= id)) return doServidor;
  if (!id) return null;
  const e = await estadoDaExecucao(id);
  if (!e) lembrar(chave, null);
  return e;
}

function idDaExecucao(r) {
  if (r == null) return null;
  if (typeof r === 'number') return r;
  return r.id ?? r.execucao_id ?? (r.execucao && r.execucao.id) ?? null;
}

// O painel da execução avulsa (rodando), a falha da última ou o resumo dela, uma vez.
function painelAvulso(chave, exec, titulo) {
  if (!exec) return null;
  if (ativa(exec.status)) return painelLog(exec, () => rota(), { titulo });
  if (lembrado(chave) === exec.id) lembrar(chave, null); // já mostrou como terminou: da próxima vez não mostra
  if (FALHAS[exec.status]) return cartaoFalha(exec, null);
  return exec.resultado ? h('details', { class: 'card' }, h('summary', {}, `Última execução: ${quando(exec.terminada_em) || 'terminou'}`),
    listaDeAvisos(exec.avisos), md(exec.resultado), logGuardado(exec.id)) : null;
}

// ---------------------------------------------------------------- biblioteca · personagens

function cartaoPersonagem(p, { rascunho = false } = {}) {
  const img = p.imagem ? urlDoArquivo(p.imagem) : null;
  return h('a', { class: 'projeto', href: `#/biblioteca/personagens/${encodeURIComponent(p.slug)}` },
    h('div', { class: 'card cartao-personagem' },
      img ? h('div', { class: 'capa com-imagem quadrada' }, h('img', { src: img, alt: p.nome || p.slug, loading: 'lazy' }))
        : h('div', { class: 'capa quadrada' }, h('span', { class: 'mudo pequeno' }, rascunho ? 'sem imagem ainda' : 'sem imagem')),
      h('strong', {}, p.nome || p.slug),
      p.resumo ? h('div', { class: 'mudo pequeno contexto-resumo' }, p.resumo) : null,
      h('div', { class: 'linha', style: { marginTop: '.5rem' } },
        rascunho ? h('span', { class: 'etiqueta sugestao' }, 'rascunho') : h('span', { class: 'etiqueta ok' }, 'na biblioteca'),
        p.voz ? h('span', { class: 'mudo pequeno' }, `voz ${p.voz}`) : null,
        p.versoes != null ? h('span', { class: 'mudo pequeno' }, `${Array.isArray(p.versoes) ? p.versoes.length : p.versoes} versões`) : null)));
}

async function telaPersonagens(raiz) {
  const r = await api('GET', '/api/personagens');
  const salvos = listaDeDados(r, 'biblioteca', 'personagens');
  const naBiblioteca = new Set(salvos.map((p) => p.slug));
  const rascunhos = Array.isArray(r) ? [] : listaDeDados(r, 'rascunhos').filter((p) => !naBiblioteca.has(p.slug) || p.pendente);
  const nome = h('input', { type: 'text', placeholder: 'Nome (opcional): ex.: Moranguete' });
  const descricao = h('textarea', {
    rows: 5, 'data-auto': '1',
    placeholder: 'Quem é e como é: espécie ou tipo, idade, corpo, roupa, um traço marcante, o jeito de ser e de falar. Ex.: um morango jovem e vaidoso, que fala rápido e se acha a protagonista da novela.',
    oninput: (e) => autoAltura(e.target),
  });
  const criar = h('button', {
    class: 'btn primario', onclick: async () => {
      if (!descricao.value.trim()) { descricao.focus(); return aviso('Descreva o personagem.'); }
      criar.disabled = true;
      try {
        const resp = await api('POST', '/api/personagens', { descricao: descricao.value.trim(), nome: nome.value.trim() || null });
        const slug = resp && (resp.slug || resp.rascunho);
        if (slug) {
          lembrar(`personagem:${slug}`, idDaExecucao(resp));
          location.hash = `#/biblioteca/personagens/${encodeURIComponent(slug)}`;
        } else {
          lembrar('personagem:novo', idDaExecucao(resp));
          await rota();
        }
      } catch (e) { criar.disabled = false; falhou(e); }
    },
  }, 'Criar personagem →');
  const novo = await execucaoParaAcompanhar('personagem:novo', null);
  trocar(raiz,
    h('section', { class: 'card' }, h('h2', {}, 'Novo personagem'),
      h('p', { class: 'mudo pequeno' }, 'O sistema escreve a ficha (aparência desenhável, com um traço marcante, e o comportamento), gera a imagem de referência e confere o resultado. Você aprova uma versão, pede um ajuste ou outra variação. Aprovado, o personagem pode ser usado em qualquer vídeo, sempre com o mesmo rosto.'),
      h('div', { class: 'grade' },
        h('div', { class: 'campo' }, h('label', {}, 'Nome'), nome),
        h('div', { class: 'campo largo' }, h('label', {}, 'Descrição'), descricao)),
      h('div', { class: 'linha', style: { marginTop: '.8rem' } }, h('span', { class: 'espaco' }), criar)),
    painelAvulso('personagem:novo', novo, 'Criando o personagem'),
    rascunhos.length ? [h('h2', { style: { margin: '1.4rem 0 .7rem' } }, 'Em criação'),
      h('div', { class: 'lista-projetos lista-personagens' }, rascunhos.map((p) => cartaoPersonagem(p, { rascunho: true })))] : null,
    h('h2', { style: { margin: '1.4rem 0 .7rem' } }, 'Personagens salvos'),
    salvos.length ? h('div', { class: 'lista-projetos lista-personagens' }, salvos.map((p) => cartaoPersonagem(p)))
      : h('p', { class: 'mudo' }, 'Nenhum personagem salvo ainda.'));
}

function blocoFicha(titulo, valor) {
  if (vazio(valor)) return null;
  return h('div', { class: 'ficha-item' }, h('h3', { class: 'subtitulo' }, titulo),
    typeof valor === 'string' ? md(valor) : valorLegivel(valor));
}

// Um personagem em criação: a ficha, as versões geradas e as ações (aprovar, ajustar, nova variação).
async function telaRascunho(raiz, slug) {
  const url = `/api/personagens/rascunhos/${encodeURIComponent(slug)}`;
  const chave = `personagem:${slug}`;
  let r = null;
  try { r = await api('GET', url); } catch (e) { if (e.status !== 404) throw e; }
  document.getElementById('topo-projeto').textContent = `· Biblioteca · ${(r && (r.nome || (r.ficha || {}).nome)) || slug}`;
  const voltar = h('a', { href: '#/biblioteca/personagens', class: 'mudo pequeno' }, '← Personagens');
  const exec = await execucaoParaAcompanhar(chave, r && (r.rodando || r.execucao));
  const rodando = !!(exec && ativa(exec.status));
  if (!r) {
    trocar(raiz, voltar, painelAvulso(chave, exec, 'Escrevendo a ficha do personagem') ||
      h('div', { class: 'faixa info' }, 'Este personagem não tem rascunho. Crie um novo a partir da descrição.'));
    return;
  }
  const ficha = r.ficha || {};
  const nome = r.nome || ficha.nome || slug;
  const versoes = listaDeDados(r.versoes || [], 'versoes').map((v) => (typeof v === 'string' ? { versao: v } : v)).map((v) => ({
    ...v,
    versao: v.versao || v.id,
    imagem: v.imagem || v.url || (v.arquivo ? `rascunhos/personagens/${slug}/${v.arquivo}` : null),
  }));
  const aprovada = r.aprovada || r.versao_aprovada || null;
  const naBiblioteca = !!(r.na_biblioteca || r.biblioteca);

  const enviar = async (botao, caminho, corpoReq, mensagem) => {
    botao.disabled = true;
    try {
      const resp = await api('POST', `${url}/${caminho}`, corpoReq);
      const id = idDaExecucao(resp);
      if (id) lembrar(chave, id);
      if (mensagem) aviso(mensagem, 'ok');
      await rota();
    } catch (e) { botao.disabled = false; falhou(e); }
  };

  const cartaoVersao = (v) => {
    const img = urlDoArquivo(v.imagem, { recente: true });
    const ajuste = h('textarea', { rows: 2, placeholder: 'O que mudar nesta versão: ex.: cabelo mais curto, uma cicatriz no queixo, olhos mais tristes' });
    const caixaAjuste = h('div', { class: 'ajuste-versao', hidden: true }, ajuste,
      h('div', { class: 'linha', style: { marginTop: '.4rem' } }, h('span', { class: 'espaco' }),
        h('button', {
          class: 'btn primario pequeno', onclick: (ev) => {
            if (!ajuste.value.trim()) { ajuste.focus(); return aviso('Diga o que mudar.'); }
            enviar(ev.currentTarget, 'gerar', { ajuste: ajuste.value.trim(), base: v.versao }, `Gerando um ajuste da ${v.versao}.`);
          },
        }, 'Gerar o ajuste')));
    const aprovar = h('button', {
      class: `btn pequeno ${v.versao === aprovada ? '' : 'primario'}`, disabled: rodando, onclick: () => {
        const sobrescrever = naBiblioteca;
        if (sobrescrever && !confirm(`${nome} já está na biblioteca. Trocar a imagem e a ficha salvas pela ${v.versao}? Os vídeos já produzidos não mudam.`)) return;
        enviar(aprovar, 'aprovar', { versao: v.versao, sobrescrever }, `Aprovando a ${v.versao}.`);
      },
    }, v.versao === aprovada ? 'Aprovar de novo' : 'Aprovar');
    const revisao = v.revisao || v.autorrevisao;
    return h('div', { class: `card versao-personagem ${v.versao === aprovada ? 'aprovada' : ''}` },
      img ? h('a', { href: img, target: '_blank', rel: 'noopener', class: 'imagem-versao' }, h('img', { src: img, alt: `${nome} ${v.versao}`, loading: 'lazy' }))
        : h('div', { class: 'imagem-versao vazia' }, h('span', { class: 'mudo pequeno' }, 'sem imagem')),
      h('div', { class: 'linha', style: { marginTop: '.5rem' } }, h('strong', {}, v.versao),
        v.versao === aprovada ? h('span', { class: 'etiqueta ok' }, 'aprovada') : null,
        v.base ? h('span', { class: 'mudo pequeno' }, `ajuste da ${v.base}`) : null,
        h('span', { class: 'espaco' }), v.modelo ? h('span', { class: 'mudo pequeno', title: 'modelo de imagem' }, v.modelo) : null),
      v.ajuste ? h('div', { class: 'mudo pequeno' }, `“${v.ajuste}”`) : null,
      revisao ? h('div', { class: `pequeno ${revisao.ok === false ? 'texto-erro' : 'mudo'}` },
        revisao.ok === false ? `✗ revisão: ${revisao.motivo || (revisao.problemas || []).join('; ') || 'problemas'}` : `✓ revisão: ${revisao.motivo || revisao.resumo || 'ok'}`) : null,
      rodando ? null : h('div', { class: 'linha', style: { marginTop: '.5rem' } }, aprovar,
        h('button', { class: 'btn pequeno', onclick: () => { caixaAjuste.hidden = !caixaAjuste.hidden; if (!caixaAjuste.hidden) ajuste.focus(); } }, 'Ajustar…')),
      caixaAjuste);
  };

  const variacao = h('button', {
    class: 'btn', disabled: rodando, onclick: () => enviar(variacao, 'gerar', {}, 'Gerando uma variação nova a partir da ficha.'),
  }, versoes.length ? 'Nova variação' : 'Gerar a imagem');

  trocar(raiz, voltar,
    h('div', { class: 'linha-topo', style: { marginTop: '.5rem' } }, h('h2', {}, nome),
      naBiblioteca ? h('span', { class: 'etiqueta ok' }, aprovada ? `na biblioteca · ${aprovada}` : 'na biblioteca') : h('span', { class: 'etiqueta sugestao' }, 'rascunho'),
      h('span', { class: 'espaco' }), variacao),
    painelAvulso(chave, exec, 'Trabalhando no personagem'),
    versoes.length ? h('div', { class: 'versoes-personagem' }, [...versoes].reverse().map(cartaoVersao))
      : rodando ? null : h('div', { class: 'card' }, h('p', { class: 'mudo' }, 'Nenhuma imagem gerada ainda.')),
    h('details', { class: 'card', open: !versoes.length },
      h('summary', {}, 'Ficha do personagem'),
      ficha.resumo || r.resumo ? h('p', {}, ficha.resumo || r.resumo) : null,
      blocoFicha('Aparência', ficha.fisico),
      blocoFicha('Comportamento', ficha.comportamento),
      blocoFicha('Voz', ficha.voz),
      blocoFicha('Estilo', ficha.estilo),
      ficha.descricao_en ? h('div', { class: 'ficha-item' }, h('h3', { class: 'subtitulo' }, 'Descrição usada nos prompts (em inglês)'),
        h('p', { class: 'pequeno mudo' }, ficha.descricao_en)) : null));
}

// ---------------------------------------------------------------- biblioteca · referências

async function telaReferencias(raiz) {
  const r = await api('GET', '/api/referencias');
  const esqueletos = listaDeDados(r, 'esqueletos', 'referencias', 'itens');
  const exec = await execucaoParaAcompanhar('referencia', Array.isArray(r) ? null : r.rodando || r.execucao);
  const rodando = !!(exec && ativa(exec.status));
  const url = h('input', { type: 'text', placeholder: 'https://www.youtube.com/watch?v=...' });
  const analisar = h('button', {
    class: 'btn primario', disabled: rodando, onclick: async () => {
      if (!url.value.trim()) { url.focus(); return aviso('Cole o link do vídeo.'); }
      analisar.disabled = true;
      try {
        const resp = await api('POST', '/api/referencias', { url: url.value.trim() });
        lembrar('referencia', idDaExecucao(resp));
        await rota();
      } catch (e) { analisar.disabled = false; falhou(e); }
    },
  }, 'Analisar');
  url.addEventListener('keydown', (e) => { if (e.key === 'Enter') analisar.click(); });

  trocar(raiz,
    h('section', { class: 'card' }, h('h2', {}, 'Analisar um vídeo de referência'),
      h('p', { class: 'mudo pequeno' }, 'Cole o link de um vídeo que performou bem. O sistema baixa a transcrição e tira o esqueleto: gancho, blocos, viradas, re-hooks, payoff. O estrategista e o roteirista usam os esqueletos como molde, e as técnicas novas que aparecerem entram na biblioteca só se você aprovar.'),
      h('div', { class: 'linha' }, h('div', { style: { flex: '1', minWidth: '240px' } }, url), analisar)),
    painelAvulso('referencia', exec, 'Analisando o vídeo'),
    h('h2', { style: { margin: '1.4rem 0 .7rem' } }, 'Esqueletos'),
    esqueletos.length ? esqueletos.map((e) => cartaoEsqueleto(e, rodando)) : h('p', { class: 'mudo' }, 'Nenhum esqueleto ainda.'));
}

function cartaoEsqueleto(e, rodando) {
  const fonte = e.fonte || {};
  const arquivo = e.arquivo || e.id;
  const titulo = e.titulo || fonte.titulo || arquivo;
  const canal = e.canal || fonte.canal;
  const link = e.url || fonte.url;
  const sugeridas = (e.tecnicas_sugeridas || e.tecnicas_novas_sugeridas || []).map((t) => (typeof t === 'string' ? { id: t } : t));
  const aprovadas = new Set([...(e.tecnicas_aprovadas || []), ...sugeridas.filter((t) => t.aprovada).map((t) => t.id)]);
  const pendentes = sugeridas.filter((t) => !aprovadas.has(t.id));
  const aprovar = async (botao, ids) => {
    botao.disabled = true;
    try {
      await api('POST', `/api/referencias/${encodeURIComponent(arquivo)}/tecnicas`, { aprovar: ids });
      aviso(ids.length === 1 ? `Técnica ${ids[0]} na biblioteca.` : `${ids.length} técnicas na biblioteca.`, 'ok');
      await rota();
    } catch (err) { botao.disabled = false; falhou(err); }
  };
  const todas = pendentes.length > 1 && !rodando ? h('button', { class: 'btn pequeno', onclick: (ev) => aprovar(ev.currentTarget, pendentes.map((t) => t.id)) }, 'Aprovar todas') : null;
  const detectadas = e.tecnicas_detectadas || [];
  return h('section', { class: 'card esqueleto' },
    h('div', { class: 'linha-topo' }, h('h3', {}, titulo), canal ? h('span', { class: 'mudo pequeno' }, canal) : null, h('span', { class: 'espaco' }),
      link ? h('a', { class: 'pequeno', href: link, target: '_blank', rel: 'noopener' }, 'ver vídeo ↗') : null),
    h('div', { class: 'mudo pequeno' }, [fonte.duracao || e.duracao, e.formato, fonte.analisado_em || e.analisado_em ? `analisado em ${fonte.analisado_em || e.analisado_em}` : null].filter(Boolean).join(' · ')),
    e.template_abstrato ? h('p', { style: { marginTop: '.5rem' } }, typeof e.template_abstrato === 'string' ? e.template_abstrato : valorLegivel(e.template_abstrato)) : null,
    detectadas.length ? h('div', { class: 'chips', style: { marginTop: '.5rem' } }, detectadas.map((t) => h('span', { class: 'chip' }, typeof t === 'object' ? t.id || t.nome : t))) : null,
    sugeridas.length ? h('div', { class: 'tecnicas-novas' },
      h('div', { class: 'linha-topo' }, h('h3', { class: 'subtitulo' }, `Técnicas novas sugeridas (${sugeridas.length})`), h('span', { class: 'espaco' }), todas),
      sugeridas.map((t) => h('div', { class: 'tecnica' },
        h('div', { class: 'espaco' }, h('strong', {}, t.nome || t.id), t.nome ? h('code', { class: 'pequeno' }, ` ${t.id}`) : null,
          t.descricao ? h('div', { class: 'pequeno' }, t.descricao) : null,
          t.exemplo ? h('div', { class: 'pequeno mudo' }, `ex.: ${t.exemplo}`) : null),
        aprovadas.has(t.id) ? h('span', { class: 'etiqueta ok' }, 'aprovada')
          : rodando ? null : h('button', { class: 'btn pequeno', onclick: (ev) => aprovar(ev.currentTarget, [t.id]) }, 'Aprovar')))) :
      h('p', { class: 'mudo pequeno', style: { marginTop: '.5rem' } }, 'Nenhuma técnica nova: as deste vídeo já estão na biblioteca.'));
}

rota();
