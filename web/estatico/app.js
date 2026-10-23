// Interface web do pipeline de vídeos. Sem framework: cada tela monta o DOM com h() e fala com a API do
// servidor (web/servidor.py). O estado de verdade fica nos arquivos de projetos/<slug>/ e no SQLite; aqui
// só o que está na tela.

'use strict';

const app = document.getElementById('app');
let opcoes = null;      // /api/opcoes: campos do pedido, vozes, músicas, personagens da biblioteca...
let estado = null;      // /api/projetos/<slug>: projeto, etapas, brief, execução rodando
let polling = null;
const revisoes = {};    // slug -> revisão das sugestões em andamento (não vai para o banco até confirmar)
const avisosPendentes = {};

const ROTULOS = {
  tema: 'Tema', descricao: 'Descrição', contexto: 'Contexto', slug: 'Pasta do projeto', titulo: 'Título',
  formato: 'Formato', duracao_alvo: 'Duração', tom: 'Tom', natureza: 'Natureza', publico: 'Público', nicho: 'Nicho',
  idioma: 'Idioma', estilo_artistico: 'Estilo artístico', imagens_referencia: 'Imagens de referência',
  'visual.proporcao': 'Proporção', 'visual.segundos_por_imagem': 'Segundos por imagem',
  'visual.numero_de_imagens': 'Número de imagens', 'visual.imagens_da_internet': 'Imagens da internet',
  'visual.qualidade': 'Qualidade das imagens', 'narracao.ativa': 'Narração', 'narracao.fornecedor': 'Fornecedor da voz',
  'narracao.voz': 'Voz do narrador', 'narracao.estilo': 'Jeito do narrador',
  'narracao.palavras_por_minuto': 'Velocidade (palavras por minuto)', personagens: 'Personagens',
  'legendas.ativas': 'Legendas', 'legendas.destacar_palavra': 'Destacar a palavra dita', 'legendas.cor_destaque': 'Cor do destaque',
  'legendas.cor_texto': 'Cor das letras', 'legendas.maiusculas': 'Caixa alta', 'legendas.palavras_por_legenda': 'Palavras na tela',
  'legendas.posicao': 'Posição', 'musica.ativa': 'Música de fundo', 'musica.nome': 'Música e artista', 'musica.arquivo': 'Arquivo',
  'musica.inicio': 'Começa em', 'musica.volume': 'Volume (%)', referencias: 'Vídeos de referência (estrutura)',
  canais_referencia: 'Canais de referência', concorrentes: 'Concorrentes', restricoes: 'Restrições',
  'trends.ativa': 'Pesquisar trends', 'trends.links': 'Links sugeridos para a pesquisa', 'producao.ate': 'Até onde ir',
  'producao.cortes': 'Gerar cortes (shorts)', modo_voz: 'Vozes do vídeo',
};
const LONGOS = { descricao: 5, estilo_artistico: 3, tom: 2, 'narracao.estilo': 2, publico: 2 };
const CUSTOM = new Set(['slug', 'contexto', 'imagens_referencia', 'personagens', 'musica.arquivo', 'trends.ativa', 'trends.links']);
const OBRIGATORIOS = new Set(['titulo', 'descricao']);
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
    const det = dados && dados.detail;
    throw new Error(typeof det === 'string' ? det : det ? JSON.stringify(det) : texto || r.statusText);
  }
  return dados;
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

// ---------------------------------------------------------------- rotas

window.addEventListener('hashchange', rota);
window.addEventListener('beforeunload', (e) => { if (editor && editor.sujo) { e.preventDefault(); e.returnValue = ''; } });

async function rota() {
  pararPolling();
  try {
    if (!opcoes) opcoes = await api('GET', '/api/opcoes');
    const partes = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
    if (partes[0] === 'conexoes') {  // endereço antigo da tela de chaves
      history.replaceState(null, '', '#/configuracoes/chaves');
      partes.splice(0, 1, 'configuracoes', 'chaves');
    }
    const aba = ['contextos', 'configuracoes'].includes(partes[0]) ? partes[0] : 'videos';
    document.querySelectorAll('.abas-topo a').forEach((a) => a.classList.toggle('ativa', a.dataset.aba === aba));
    if (partes[0] === 'p' && partes[1]) await abrirProjeto(decodeURIComponent(partes[1]), partes[2]);
    else if (partes[0] === 'contextos' && partes[1]) await telaContexto(decodeURIComponent(partes[1]));
    else if (partes[0] === 'contextos') await telaContextos();
    else if (partes[0] === 'configuracoes') await telaConfiguracoes(partes[1]);
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
  const criar = async () => {
    if (!titulo.value.trim()) { titulo.focus(); return aviso('Escreva o título do vídeo.'); }
    try {
      const p = await api('POST', '/api/projetos', { titulo: titulo.value.trim() });
      irPara(p.slug, 'configurar');
    } catch (e) { falhou(e); }
  };
  titulo.addEventListener('keydown', (e) => { if (e.key === 'Enter') criar(); });
  const nomeEtapa = (id) => (opcoes.etapas.find((e) => e.id === id) || {}).nome || id;
  trocar(app,
    h('div', { class: 'card' },
      h('h1', {}, 'Novo vídeo'),
      h('p', { class: 'mudo' }, 'Comece pelo título. Na próxima tela, a descrição e o que mais você quiser definir; o resto o sistema sugere.'),
      h('div', { class: 'linha' }, h('div', { style: { flex: '1', minWidth: '240px' } }, titulo),
        h('button', { class: 'btn primario', onclick: criar }, 'Criar vídeo'))),
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
            p.no_banco ? h('span', { class: 'etiqueta' }, nomeEtapa(p.etapa)) : h('span', { class: 'etiqueta' }, 'feito pela conversa'),
            h('span', { class: 'mudo pequeno' }, quando(p.atualizado_em))))))) :
      h('p', { class: 'mudo' }, 'Nenhum projeto ainda.'),
  );
  titulo.focus();
}

// ---------------------------------------------------------------- conexões com os provedores

const USOS = { llm: 'LLM', narracao: 'Narração', imagem: 'Imagens', trends: 'Trends' };
const PROVEDOR_DO_MODO = { 'claude-code-api': 'anthropic', anthropic: 'anthropic', openai: 'openai', gemini: 'gemini' };

const SUBABAS_CONFIG = {
  chaves: 'Chaves de API',
  modelos: 'Modelos das etapas',
  narracao: 'Narração',
};

async function telaConfiguracoes(sub) {
  if (!SUBABAS_CONFIG[sub]) {
    sub = 'chaves';
    history.replaceState(null, '', '#/configuracoes/chaves');
  }
  document.getElementById('topo-projeto').textContent = `· Configurações · ${SUBABAS_CONFIG[sub]}`;
  const dados = await api('GET', '/api/conexoes');
  const porId = Object.fromEntries(dados.provedores.map((p) => [p.id, p]));
  const temChave = (id) => !!(porId[id] && porId[id].origem);
  const chavesCard = h('section', { class: 'card' });
  const etapasCard = h('section', { class: 'card' });

  function cartaoProvedor(p) {
    if (!p.origem) p.teste = null;  // o teste antigo não diz nada sobre uma chave que não existe mais
    const status = !p.origem ? h('span', { class: 'etiqueta' }, 'sem chave')
      : p.teste && p.teste.ok ? h('span', { class: 'etiqueta ok' }, 'conectado')
        : p.teste ? h('span', { class: 'etiqueta erro' }, 'falhou') : h('span', { class: 'etiqueta' }, 'não testado');
    const campo = h('input', { type: 'password', placeholder: p.origem ? 'colar outra chave para trocar' : `colar a chave (${p.variavel})`, autocomplete: 'off' });
    const recarregar = () => telaConfiguracoes(sub).catch(falhou);
    const salvar = h('button', {
      class: 'btn primario pequeno', onclick: async () => {
        if (!campo.value.trim()) { campo.focus(); return aviso('Cole a chave.'); }
        salvar.disabled = true;
        try {
          const r = await api('PUT', `/api/conexoes/chaves/${p.id}`, { valor: campo.value.trim() });
          aviso(r.teste.ok ? `Chave salva e testada: ${r.teste.mensagem}.` : `Chave salva, mas o teste falhou: ${r.teste.mensagem}`, r.teste.ok ? 'ok' : 'erro');
          await recarregar();
        } catch (e) { salvar.disabled = false; falhou(e); }
      },
    }, p.origem ? 'Trocar e testar' : 'Salvar e testar');
    const testar = h('button', {
      class: 'btn pequeno', disabled: !p.origem, onclick: async () => {
        testar.disabled = true;
        try {
          const r = await api('POST', `/api/conexoes/${p.id}/testar`);
          aviso(r.ok ? `${p.nome}: ${r.mensagem}.` : `${p.nome}: ${r.mensagem}`, r.ok ? 'ok' : 'erro');
          await recarregar();
        } catch (e) { testar.disabled = false; falhou(e); }
      },
    }, 'Testar');
    const remover = p.origem === 'arquivo' ? h('button', {
      class: 'btn pequeno perigo', onclick: async () => {
        if (!confirm(`Apagar a chave de ${p.nome} do .env?`)) return;
        try { await api('DELETE', `/api/conexoes/chaves/${p.id}`); await recarregar(); } catch (e) { falhou(e); }
      },
    }, 'Remover') : null;
    return h('div', { class: 'provedor' },
      h('div', { class: 'linha-topo' }, h('strong', {}, p.nome), status),
      h('div', { class: 'linha', style: { marginBottom: '.5rem' } }, p.usos.map((u) => h('span', { class: 'chip' }, USOS[u] || u))),
      p.origem ? h('div', { class: 'mudo pequeno' }, h('code', {}, p.variavel), ' = ', h('code', {}, p.mascara || ''),
        p.origem === 'ambiente' ? ' · vem de uma variável de ambiente (mude onde ela é exportada)' : ' · no .env') : null,
      p.teste ? h('div', { class: `pequeno ${p.teste.ok ? 'mudo' : 'texto-erro'}`, style: { marginTop: '.3rem' } },
        `${p.teste.ok ? '✓' : '✗'} ${p.teste.mensagem} · ${quando(p.teste.quando)}`) : null,
      p.origem === 'ambiente' ? null : h('div', { class: 'linha', style: { marginTop: '.6rem' } },
        h('div', { style: { flex: '1', minWidth: '200px' } }, campo), salvar),
      h('div', { class: 'linha', style: { marginTop: '.5rem' } }, testar, remover, h('span', { class: 'espaco' }),
        h('a', { href: p.painel, target: '_blank', rel: 'noopener', class: 'pequeno' }, 'criar chave ↗')));
  }

  function linhaEtapa(e) {
    const escolha = { modo: e.modo, modelo: e.modelo };
    const gravar = async () => {
      try {
        await api('PUT', '/api/conexoes/etapas', { [e.id]: escolha });
        aviso(`${e.nome}: ${dados.modos[escolha.modo]}${escolha.modelo ? ` · ${escolha.modelo}` : ''}`, 'ok', 2500);
      } catch (err) { falhou(err); }
    };
    const modeloBox = h('div', { class: 'modelo-etapa' });
    const desenharModelo = () => {
      const provedor = PROVEDOR_DO_MODO[escolha.modo];
      if (escolha.modo === 'claude-code' || escolha.modo === 'claude-code-api') {
        trocar(modeloBox, h('select', { onchange: (ev) => { escolha.modelo = ev.target.value; gravar(); } },
          dados.modelos_claude_code.map((m) => h('option', { value: m, selected: m === (escolha.modelo || '') }, m || 'padrão do agente'))));
        return;
      }
      const lista = (porId[provedor] && porId[provedor].modelos) || [];
      const idLista = `modelos-${e.id}`;
      if (!escolha.modelo) escolha.modelo = dados.sugeridos[provedor];
      const entrada = h('input', {
        type: 'text', value: escolha.modelo, list: idLista, placeholder: dados.sugeridos[provedor],
        onchange: (ev) => { escolha.modelo = ev.target.value.trim(); gravar(); desenharModelo(); },
      });
      const comPreco = dados.precos.includes(escolha.modelo);
      trocar(modeloBox, entrada, h('datalist', { id: idLista }, lista.map((m) => h('option', { value: m }))),
        h('div', { class: `pequeno ${comPreco ? 'mudo' : 'texto-alerta'}` },
          comPreco ? 'preço na tabela: o custo entra no total' : 'sem preço em biblioteca/precos.json: o custo deste modelo fica fora do total',
          lista.length ? '' : [' · ', h('a', { href: '#/configuracoes/chaves' }, 'teste a chave'), ' para listar os modelos']));
    };
    const modo = h('select', {
      onchange: (ev) => { escolha.modo = ev.target.value; escolha.modelo = ''; desenharModelo(); gravar(); },
    }, Object.entries(dados.modos).map(([id, nome]) => {
      const provedor = PROVEDOR_DO_MODO[id];
      const semChave = provedor && !temChave(provedor);
      return h('option', { value: id, selected: id === escolha.modo, disabled: semChave && id !== escolha.modo },
        nome + (semChave ? ' (sem chave)' : ''));
    }));
    desenharModelo();
    return h('tr', {}, h('td', {}, h('strong', {}, e.nome), h('div', { class: 'mudo pequeno' }, e.agentes)),
      h('td', {}, modo), h('td', {}, modeloBox));
  }

  trocar(chavesCard, h('h2', {}, 'Chaves de API'),
    h('p', { class: 'mudo pequeno' }, 'As chaves ficam no arquivo .env da raiz (permissão 600, fora do controle de versão) e só aparecem aqui mascaradas. Salvar já testa a chave.'),
    h('div', { class: 'grade-provedores' }, dados.provedores.map(cartaoProvedor)));

  const todas = h('select', {}, h('option', { value: '' }, 'aplicar o mesmo a todas as etapas…'),
    Object.entries(dados.modos).map(([id, nome]) => h('option', { value: id, disabled: PROVEDOR_DO_MODO[id] && !temChave(PROVEDOR_DO_MODO[id]) }, nome)));
  todas.addEventListener('change', async () => {
    if (!todas.value) return;
    const mapa = Object.fromEntries(dados.etapas.map((e) => [e.id, { modo: todas.value, modelo: '' }]));
    try { await api('PUT', '/api/conexoes/etapas', mapa); await telaConfiguracoes(sub); } catch (e) { falhou(e); }
  });
  trocar(etapasCard,
    h('div', { class: 'linha-topo' }, h('h2', {}, 'Modelo de cada etapa'), h('span', { class: 'espaco' }), h('div', { style: { minWidth: '240px' } }, todas)),
    h('p', { class: 'mudo pequeno' }, 'No Claude Code, a etapa segue a skill como numa conversa. Com Claude, GPT ou Gemini pela API, ela roda no motor de agentes da interface: os mesmos agentes de .claude/agents/, as mesmas ferramentas e os mesmos arquivos, então dá para trocar de modelo de uma etapa para outra. Na produção, o modelo só escreve os textos de publicação: as imagens e a narração seguem o pedido.'),
    h('div', { class: 'rolagem' }, h('table', { class: 'tabela-etapas' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Etapa'), h('th', {}, 'Quem roda'), h('th', {}, 'Modelo'))),
      h('tbody', {}, dados.etapas.map(linhaEtapa)))));

  const narracao = h('section', { class: 'card' }, h('h2', {}, 'Narração'),
    h('p', { class: 'mudo pequeno' }, 'O fornecedor da voz é escolhido em cada vídeo, no campo "Fornecedor da voz" da configuração: Gemini TTS, OpenAI, ElevenLabs ou espeak (grátis, para testar). Uma voz de outro fornecedor vira uma voz do mesmo gênero no escolhido.'),
    h('ul', { class: 'pequeno' }, ['gemini', 'openai', 'elevenlabs'].map((id) => h('li', {}, h('strong', {}, porId[id].nome), ': ',
      temChave(id) ? (id === 'elevenlabs' ? `${(opcoes.vozes_por_fornecedor.elevenlabs || []).length} vozes da conta (teste a chave para atualizar)` : 'pronto') : 'sem chave'))));

  const semChaves = !dados.provedores.some((p) => p.origem);
  const subabas = h('nav', { class: 'subabas' }, Object.entries(SUBABAS_CONFIG).map(([id, nome]) =>
    h('a', { href: `#/configuracoes/${id}`, class: id === sub ? 'ativa' : null }, nome)));
  const conteudo = { chaves: chavesCard, modelos: etapasCard, narracao }[sub];
  trocar(app, h('h1', {}, 'Configurações'),
    h('p', { class: 'mudo' }, 'Chaves dos provedores de LLM e de voz, o modelo que roda cada etapa do pipeline e os fornecedores de narração.'),
    subabas,
    sub !== 'chaves' && semChaves ? h('div', { class: 'faixa alerta' }, 'Nenhuma chave cadastrada ainda. ',
      h('a', { href: '#/configuracoes/chaves' }, 'Cadastre em Chaves de API'), '.') : null,
    conteudo);
  if (dados.provedores.some((p) => p.id === 'elevenlabs' && p.teste && p.teste.ok)) {
    opcoes = await api('GET', '/api/opcoes');  // as vozes da ElevenLabs mudam com o teste
  }
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
      h('div', { class: 'faixa info' }, `Contexto antigo, só de texto, em ${c.caminho}. A interface não edita esse arquivo; para anexar imagens e áudios, copie-o para um contexto novo.`),
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
    h('p', { class: 'mudo' }, `Tudo é salvo enquanto você digita, em ${c.caminho.replace(/contexto\.md$/, '')}. Vale para os vídeos que escolherem este contexto, a partir da próxima vez que o pedido for confirmado.`),
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
  configurar: telaConfigurar, sugestoes: telaSugestoes, pesquisa: telaPesquisa, angulos: telaAngulos,
  roteiro: telaRoteiro, cortes: telaCortes, visual: telaVisual, producao: telaProducao,
};

async function abrirProjeto(slug, etapa) {
  estado = await api('GET', `/api/projetos/${encodeURIComponent(slug)}`);
  if (!etapa || !TELAS[etapa]) {
    etapa = estado.projeto.etapa || 'configurar';
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
    if (estado.rodando && estado.rodando.etapa === e.id) classes.push('rodando');
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
  if (!info || !info.desatualizada || !info.aplicavel || (estado.rodando && estado.rodando.etapa === id)) return null;
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
  if (!r || r.etapa === atual) return null;
  const nome = (infoEtapa(r.etapa) || {}).nome || r.etapa;
  return h('div', { class: 'faixa info linha' }, h('span', { class: 'girando' }),
    h('span', {}, `Rodando agora: ${nome} (${quando(r.iniciada_em)}).`),
    h('a', { href: `#/p/${encodeURIComponent(estado.projeto.slug)}/${r.etapa}` }, 'acompanhar'));
}

function proximaAplicavel(id) {
  const ids = estado.etapas.map((e) => e.id);
  return estado.etapas.slice(ids.indexOf(id) + 1).find((e) => e.aplicavel);
}

async function executar(etapa, extras = {}) {
  const slug = estado.projeto.slug;
  await api('POST', `/api/projetos/${encodeURIComponent(slug)}/executar`, { etapa, ...extras });
  irPara(slug, etapa === 'avaliar' ? 'roteiro' : etapa);
}

// ---------------------------------------------------------------- execuções

function linhaDeLog(l) {
  const t = l.trimStart();
  const sub = l.startsWith('    ');
  const cls = t.startsWith('▶') ? 'agente' : t.startsWith('$') ? 'cmd' : t.startsWith('✎') ? 'arquivo'
    : t.startsWith('✓') ? 'fim' : t.startsWith('✗') || t.startsWith('■') ? 'falha' : t.startsWith('›') ? (sub ? 'sub' : 'fala') : 'sub';
  return h('span', { class: cls }, l + '\n');
}

function painelLog(execucao, aoTerminar) {
  const pre = h('pre', { class: 'log' });
  const relogio = h('span', { class: 'mudo pequeno' });
  let n = 0;
  const cancelar = h('button', {
    class: 'btn perigo pequeno', onclick: async () => {
      if (!confirm('Cancelar esta execução? O que já foi gravado fica nos arquivos.')) return;
      try { await api('POST', `/api/execucoes/${execucao.id}/cancelar`); } catch (e) { falhou(e); }
    },
  }, 'Cancelar');
  const card = h('div', { class: 'card' },
    h('div', { class: 'linha-topo' }, h('span', { class: 'girando' }), h('h3', {}, 'O Claude Code está trabalhando nesta etapa'),
      relogio, h('span', { class: 'espaco' }), cancelar),
    h('p', { class: 'mudo pequeno' }, 'Pode fechar a página: a execução continua no servidor e o log fica guardado.'),
    pre);
  const passo = async () => {
    try {
      const e = await api('GET', `/api/execucoes/${execucao.id}?desde=${n}`);
      if (e.log) {
        const perto = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
        e.log.split('\n').filter(Boolean).forEach((l) => pre.append(linhaDeLog(l)));
        if (perto) pre.scrollTop = pre.scrollHeight;
        n = e.tamanho;
      }
      relogio.textContent = `começou ${quando(e.iniciada_em)}`;
      if (e.status !== 'rodando') { pararPolling(); aoTerminar(e); }
    } catch (err) { console.error(err); }
  };
  pararPolling();
  polling = setInterval(passo, 1500);
  passo();
  return card;
}

function cartaoResumo(execucao) {
  if (!execucao || !execucao.resultado) return null;
  const custo = execucao.custo_usd ? ` · US$ ${execucao.custo_usd.toFixed(2)}` : '';
  return h('details', { class: 'card' }, h('summary', {}, `Resumo da execução (${quando(execucao.terminada_em)}${custo})`),
    md(execucao.resultado), logGuardado(execucao.id));
}

function logGuardado(id) {
  const det = h('details', { style: { marginTop: '.6rem' } }, h('summary', { class: 'pequeno' }, 'Log completo'));
  det.addEventListener('toggle', async () => {
    if (!det.open || det.dataset.carregado) return;
    det.dataset.carregado = '1';
    const e = await api('GET', `/api/execucoes/${id}`);
    det.append(h('pre', { class: 'log' }, e.log.split('\n').filter(Boolean).map(linhaDeLog)));
  });
  return det;
}

// Esqueleto comum das etapas que rodam o Claude Code: acompanha a execução, mostra o erro da última ou o
// botão de começar, e entrega os dados para a tela quando há o que mostrar.
async function etapaComExecucao(corpo, id, { titulo, explicacao, rotuloIniciar, iniciar, carregar, temDados, mostrar }) {
  const info = infoEtapa(id);
  const rodando = estado.rodando && estado.rodando.etapa === id ? estado.rodando : null;
  corpo.append(h('div', { class: 'linha-topo' }, h('h1', {}, titulo)));
  if (explicacao) corpo.append(h('p', { class: 'mudo' }, explicacao));
  if (!info.aplicavel) {
    corpo.append(h('div', { class: 'faixa info' }, 'Esta etapa não vale para este vídeo, pelo pedido aprovado.'));
  }
  if (rodando) {
    corpo.append(painelLog(rodando, () => rota()));
    if (carregar && temDados) {
      const dados = await carregar();
      if (temDados(dados)) corpo.append(h('h2', { style: { marginTop: '1rem' } }, 'Até agora'), await mostrar(dados, { rodando: true }));
    }
    return;
  }
  const dados = carregar ? await carregar() : null;
  const exec = info.execucao;
  if (exec && ['erro', 'cancelada', 'interrompida'].includes(exec.status)) {
    const msg = { erro: 'A última execução terminou com erro.', cancelada: 'A última execução foi cancelada.',
      interrompida: 'A última execução foi interrompida (o servidor reiniciou).' }[exec.status];
    corpo.append(h('div', { class: 'card' }, h('div', { class: 'faixa erro' }, msg),
      exec.resultado ? md(exec.resultado) : null, logGuardado(exec.id),
      h('div', { class: 'linha', style: { marginTop: '.8rem' } },
        h('button', { class: 'btn primario', onclick: () => iniciar().catch(falhou) }, 'Tentar de novo'))));
  }
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

// Seletor de voz com as amostras de ~5 s de cada voz (biblioteca/vozes/, narrar.py --amostras): o botão ▶ toca a
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
    ouvir.title = ouvir.disabled ? 'sem amostra (gere com narrar.py --amostras)' : `ouvir ${atual()}`;
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
    }, h('option', { value: '' }, c.quem === 'decisao' ? '— o sistema sugere —' : c.padrao ? `padrão: ${c.padrao}` : '—'),
    lista.map((o) => h('option', { value: o }, o)));
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
        'Personagens salvos na biblioteca (skill criar-personagem): clique para usar. A imagem aprovada vira a referência das cenas.'), grade) : null,
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
  return caminho.startsWith(prefixo) ? `/api/projetos/${encodeURIComponent(estado.projeto.slug)}/arquivos/${caminho.slice(prefixo.length)}` : null;
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

  corpo.append(h('h1', {}, 'Configuração do vídeo'),
    h('p', { class: 'mudo' }, 'Só título e descrição são obrigatórios. O que ficar vazio, o sistema sugere na próxima etapa, e você aprova antes de começar. Tudo é salvo enquanto você digita.'));
  if (infoEtapa('sugestoes').pronta) {
    corpo.append(h('div', { class: 'faixa alerta' }, 'Este pedido já foi aprovado. Mudanças aqui só valem quando você gerar e confirmar as sugestões de novo, e as etapas que dependem delas precisam ser refeitas.'));
  }

  for (const [secao, lista] of secoes()) {
    const grade = h('div', { class: 'grade' });
    const card = h('section', { class: 'card' }, h('h2', {}, secao), grade);
    for (const c of lista) {
      if (c.chave === 'slug') {
        grade.append(h('div', { class: 'campo' }, h('label', {}, rotulo('slug')), h('input', { type: 'text', value: slug, readOnly: true }),
          h('div', { class: 'ajuda' }, `projetos/${slug}/`)));
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
    corpo.append(card);
  }

  const gerar = h('button', {
    class: 'btn primario', onclick: async () => {
      if (vazio(get('titulo')) || vazio(get('descricao'))) return aviso('Preencha o título e a descrição.', 'erro');
      gerar.disabled = true;
      try { await salvarJa(); await executar('sugestoes'); } catch (e) { gerar.disabled = false; falhou(e); }
    },
  }, 'Gerar: analisar e sugerir →');
  corpo.append(h('div', { class: 'rodape-acoes' }, h('span', { class: 'mudo pequeno espaco' }, 'Próximo passo: o sistema lê o que você preencheu e sugere o resto.'), gerar));
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
      h('div', { class: 'ajuda' }, 'Estilo, personagens ou lugares que o diretor visual deve olhar. Ficam em 00-anexos/ do projeto e não entram no vídeo.'));
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
            aviso(`Música salva em ${r.caminho}`, 'ok');
          } catch (err) { falhou(err); }
        },
      }),
      h('div', { class: 'ajuda' }, 'O sistema não baixa música: escolha um arquivo de biblioteca/musicas/ ou carregue um.'));
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
  for (const s of sug.campos || []) {
    if (!campo(s.campo)) continue;
    motivos[s.campo] = s.motivo;
    if (!(s.campo in valores) && !vazio(s.valor)) {
      const c = campo(s.campo);
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
    if (['slug', 'personagens', 'musica.arquivo', 'contexto', 'imagens_referencia'].includes(c.chave)) continue;
    const origem = rev.origens[c.chave];
    if (origem === 'sugestao' || (c.quem === 'decisao' && vazio(rev.valores[c.chave]))) decididos.push(c);
    else if (origem === 'usuario') doUsuario.push(c);
    else padroes.push(c);
  }
  const linhaCampo = (c) => {
    const etq = etiquetaOrigem(rev.origens[c.chave], c);
    const w = widgetCampo(c, get, (v) => { mudar(c.chave)(v); etq.replaceWith(etiquetaOrigem('usuario')); }, {
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

  raiz.append(h('section', { class: 'card' }, h('h2', {}, `Sugestões (${decididos.length})`), h('div', { class: 'grade' }, decididos.map(linhaCampo))));
  raiz.append(h('details', { class: 'card' }, h('summary', {}, `O que você definiu (${doUsuario.length})`), h('div', { class: 'grade' }, doUsuario.map(linhaCampo))));
  raiz.append(h('details', { class: 'card' }, h('summary', {}, `Padrões do sistema (${padroes.length})`),
    h('p', { class: 'mudo pequeno' }, 'Vazios ficam com o padrão indicado. Preencha só o que quiser mudar.'), h('div', { class: 'grade' }, padroes.map(linhaCampo))));

  const confirmar = h('button', {
    class: 'btn primario', onclick: async () => {
      if (infoEtapa('pesquisa').pronta && !confirm('A pesquisa já foi feita com o pedido anterior. Confirmar regrava o pedido e o brief; as etapas seguintes precisam ser refeitas. Seguir?')) return;
      confirmar.disabled = true;
      try {
        const corpoReq = {
          valores: Object.fromEntries(Object.entries(rev.valores).filter(([, v]) => !vazio(v) || v === false)),
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
        aviso('Pedido aprovado: projetos/' + slug + '/00-pedido.yaml e 00-brief.json gravados.', 'ok');
        await executar('pesquisa');
      } catch (e) {
        confirmar.disabled = false;
        raiz.append(h('div', { class: 'card' }, h('div', { class: 'faixa erro' }, 'O pedido não foi gravado:'), h('pre', { class: 'saida' }, e.message)));
        falhou(new Error('O pedido não foi gravado; veja o motivo no fim da página.'));
      }
    },
  }, 'Confirmar e iniciar a pesquisa →');
  raiz.append(h('div', { class: 'rodape-acoes' },
    h('a', { class: 'btn', href: `#/p/${encodeURIComponent(slug)}/configurar` }, '← Voltar à configuração'),
    h('button', { class: 'btn', onclick: () => { delete revisoes[slug]; executar('sugestoes').catch(falhou); } }, 'Sugerir de novo'),
    h('span', { class: 'espaco' }), confirmar));
  return raiz;
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
    explicacao: 'O estrategista propõe ângulos (título, thumbnail, promessa e estrutura) a partir das trends e do dossiê. Escolha um para o roteiro.',
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
        lista([['Thumbnail', a.thumbnail], ['Gancho', a.gancho_proposto], ['Títulos alternativos', a.titulos_alternativos]]),
        h('details', { onclick: (e) => e.stopPropagation() }, h('summary', { class: 'pequeno', style: { marginTop: '.6rem' } }, 'Mais detalhes'),
          lista([['Técnicas', a.tecnicas], ['Estrutura', a.estrutura], ['Aposta', a.aposta], ['Diferencial', a.diferencial],
            ['Evidências', a.evidencias], ['Riscos', a.riscos]])));
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
              await executar('roteiro');
            } catch (e) { falhou(e); }
          },
        }, 'Escolher e gerar o roteiro →')));
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

// ---------------------------------------------------------------- 6. cortes

function leituraDoRoteiro(doc) {
  return h('div', { class: 'leitura' }, doc.blocos.map((b) => h('div', { class: 'bloco' },
    h('div', { class: 'bloco-topo' }, h('span', { class: 'bloco-id' }, b.id), h('strong', {}, b.titulo)),
    b.itens.filter((i) => i.tipo === 'fala').map((i) => h('p', { class: 'fala-lida' },
      norm(i.quem) !== 'narrador' ? h('span', { class: 'quem' }, `${i.quem}${i.direcao ? ` (${i.direcao})` : ''}:`) : null,
      i.rehook ? h('span', { class: 'etiqueta sugestao' }, 're-hook') : null, ' ', i.texto)))));
}

function botaoProxima(id, rotuloPadrao) {
  const prox = proximaAplicavel(id);
  if (!prox) return h('span', { class: 'mudo' }, 'Última etapa do pedido.');
  const custa = prox.id === 'producao';
  return h('button', {
    class: 'btn primario', onclick: () => (custa ? irPara(estado.projeto.slug, prox.id) : executar(prox.id).catch(falhou)),
  }, rotuloPadrao || `Próxima etapa: ${prox.nome.toLowerCase()} →`);
}

async function telaCortes(corpo) {
  const slug = estado.projeto.slug;
  await etapaComExecucao(corpo, 'cortes', {
    titulo: 'Cortes para shorts',
    explicacao: 'O cortador tira shorts autônomos do roteiro longo, cada um com gancho próprio, e o crítico avalia cada um.',
    rotuloIniciar: 'Gerar os cortes',
    iniciar: () => executar('cortes'),
    carregar: () => api('GET', `/api/projetos/${encodeURIComponent(slug)}/cortes`),
    temDados: (d) => !!d.indice || d.cortes.length > 0,
    mostrar: async (d, { rodando }) => h('div', {},
      d.indice ? h('div', { class: 'card' }, md(d.indice)) : null,
      d.cortes.map((c) => h('details', { class: 'card' }, h('summary', {}, c.arquivo, ' ',
        c.avaliacao ? h('span', { class: `etiqueta ${c.avaliacao.aprovado ? 'ok' : 'erro'}` }, `${c.avaliacao.nota ?? '?'}%`) : null),
      leituraDoRoteiro(c.doc))),
      rodando ? null : h('div', { class: 'rodape-acoes' },
        h('button', { class: 'btn', onclick: () => confirm('Refazer os cortes?') && executar('cortes').catch(falhou) }, 'Refazer os cortes'),
        h('span', { class: 'espaco' }), botaoProxima('cortes'))),
  });
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

// ---------------------------------------------------------------- 8. produção

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

function entregaveisPublicacao(p) {
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

function cartaoCusto(c) {
  if (!c) return null;
  const notas = [
    'Claude Code: o custo que cada execução informa (num plano de assinatura, é o equivalente em API, não uma cobrança à parte).',
    c.tabela && c.tabela.conferido_em ? `Imagens e narração: tokens de cada chamada vezes a tabela de biblioteca/precos.json, conferida em ${c.tabela.conferido_em}.` : null,
    !c.registro_apis ? 'Nenhuma chamada paga de imagem ou narração registrada neste projeto (modo teste, ou produzido antes do registro de custos existir).' : null,
    c.sem_preco && c.sem_preco.length ? `Sem preço na tabela, fora do total: ${c.sem_preco.join(', ')}.` : null,
    c.fora_do_banco ? 'Projeto feito pela conversa: o custo do Claude Code não foi registrado.' : null,
  ].filter(Boolean);
  return h('div', { class: 'card' },
    h('div', { class: 'linha-topo' }, h('h2', {}, 'Custo total de produção'), h('span', { class: 'espaco' }),
      h('span', { class: 'custo-total' }, dolares(c.total_usd))),
    c.itens.length ? h('table', { class: 'tabela-custos' },
      h('tbody', {}, c.itens.map((i) => h('tr', {}, h('td', {}, i.rotulo), h('td', { class: 'mudo pequeno' }, i.detalhe), h('td', { class: 'valor' }, dolares(i.usd)))))) :
      h('p', { class: 'mudo' }, 'Nada registrado ainda.'),
    h('ul', { class: 'mudo pequeno notas-custo' }, notas.map((n) => h('li', {}, n))));
}

async function telaProducao(corpo) {
  const slug = estado.projeto.slug;
  const teste = h('input', { type: 'checkbox' });
  const opcoesProducao = () => h('div', { class: 'card' },
    h('label', { class: 'interruptor' }, teste, 'Modo teste: voz robótica local (espeak) e quadros provisórios, sem gastar com APIs'),
    h('p', { class: 'mudo pequeno', style: { marginTop: '.5rem' } }, 'Sem o modo teste, as imagens (Gemini) e a narração (Gemini TTS) são cobradas na chave do .env.'));
  const iniciar = () => executar('producao', { teste: teste.checked });
  await etapaComExecucao(corpo, 'producao', {
    titulo: 'Produção',
    explicacao: 'Imagens (internet e IA, com as fichas dos personagens), narração com as vozes do pedido, alinhamento palavra a palavra e montagem com legendas e música.',
    rotuloIniciar: 'Produzir o vídeo',
    iniciar,
    carregar: () => api('GET', `/api/projetos/${encodeURIComponent(slug)}/producao`),
    temDados: (d) => d.videos.length > 0 || !!d.folha_de_contato || !!d.publicacao || !!d.thumbnail,
    mostrar: async (d, { rodando }) => h('div', {},
      d.videos.map((v) => {
        const url = `/api/projetos/${encodeURIComponent(slug)}/arquivos/${v}`;
        return h('div', { class: 'card' },
          h('div', { class: 'linha-topo' }, h('h2', {}, 'Vídeo'), h('span', { class: 'mudo pequeno' }, v), h('span', { class: 'espaco' }),
            h('a', { class: 'btn pequeno', href: url, download: v.split('/').pop() }, 'Baixar')),
          h('video', { src: url, controls: true, preload: 'metadata' }));
      }),
      d.thumbnail ? h('div', { class: 'card' },
        h('div', { class: 'linha-topo' }, h('h2', {}, 'Thumbnail'), h('span', { class: 'mudo pequeno' }, '10-video/thumbnail.jpg'),
          h('span', { class: 'espaco' }),
          h('a', { class: 'btn pequeno', href: `/api/projetos/${encodeURIComponent(slug)}/arquivos/${d.thumbnail}`, download: `${slug}-thumbnail.jpg` }, 'Baixar')),
        h('img', { class: 'thumbnail', src: `/api/projetos/${encodeURIComponent(slug)}/arquivos/${d.thumbnail}`, alt: 'thumbnail do vídeo' }),
        h('p', { class: 'mudo pequeno' }, 'É também o primeiro quadro do vídeo, o que as plataformas mostram como capa quando você não sobe uma thumbnail.')) : null,
      d.publicacao ? entregaveisPublicacao(d.publicacao) : rodando ? null : h('div', { class: 'card' },
        h('h2', {}, 'Título, descrições e tags'),
        h('p', { class: 'mudo' }, 'Ainda não gerados. O publicador escreve o título, a descrição de cada plataforma e as tags, buscando agora as que estão em alta.'),
        h('button', { class: 'btn primario', onclick: () => executar('publicacao').catch(falhou) }, 'Gerar título, descrições e tags')),
      cartaoCusto(d.custo),
      d.folha_de_contato ? h('details', { class: 'card' }, h('summary', {}, 'Folha de contato das imagens'),
        h('img', { class: 'contato', src: `/api/projetos/${encodeURIComponent(slug)}/arquivos/${d.folha_de_contato}`, alt: 'folha de contato' })) : null,
      d.creditos ? h('details', { class: 'card' }, h('summary', {}, 'Créditos das imagens da internet'), md(d.creditos)) : null,
      rodando ? null : h('div', {}, opcoesProducao(), h('div', { class: 'rodape-acoes' }, h('span', { class: 'espaco' }),
        d.publicacao ? h('button', { class: 'btn', onclick: () => confirm('Escrever de novo o título, as descrições e as tags, com uma coleta nova das tags em alta?') && executar('publicacao').catch(falhou) }, 'Refazer textos de publicação') : null,
        h('button', { class: 'btn', onclick: () => confirm('Produzir de novo? Só o que mudou é refeito.') && iniciar().catch(falhou) }, 'Produzir de novo')))),
  });
  // sem vídeo ainda, as opções ficam acima do botão de começar (ou do "tentar de novo")
  const botao = corpo.querySelector('.card button.btn.primario');
  if (!corpo.contains(teste) && botao) botao.closest('.card').before(opcoesProducao());
}

rota();
