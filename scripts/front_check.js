// Exécute tout le script du dashboard dans un faux DOM, contre l'API réelle d'une instance, et remonte toute erreur.
// Une fonction manquante dans index.html = page bloquée sur « Chargement… » : à lancer après toute modification du front.
//   node scripts/front_check.js <dossier de l'instance (.env)> <port> [dossier du code, défaut = le même]
const fs = require('fs'), vm = require('vm');
const [dir, port, codeDir = dir] = process.argv.slice(2);
const env = fs.readFileSync(`${dir}/.env`, 'utf8');
const get = k => (env.match(new RegExp('^' + k + '=(.*)$', 'm')) || [])[1]?.replace(/^"|"$/g, '');
const auth = 'Basic ' + Buffer.from(`${get('BW_DASHBOARD_USER') || 'admin'}:${get('BW_DASHBOARD_PASSWORD')}`).toString('base64');
const html = fs.readFileSync(`${codeDir}/bankwatch/static/index.html`, 'utf8');
const js = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).sort((a, b) => b.length - a.length)[0];
const els = {}, errors = [], calls = [];
const mkEl = id => ({ id, innerHTML: '', textContent: '', value: '', hidden: false, dataset: {}, style: {},
  classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, addEventListener() {}, querySelector: () => null,
  querySelectorAll: () => [], closest: () => null, setAttribute() {}, getContext: () => ({}), close() {}, showModal() {}, appendChild() {} });
els['f-days'] = Object.assign(mkEl('f-days'), { value: '30' });
const document = { getElementById: id => els[id] || (els[id] = mkEl(id)), querySelector: () => mkEl('q'), querySelectorAll: () => [],
  documentElement: mkEl('html'), addEventListener() {}, body: mkEl('body') };
function Chart() { this.destroy = () => {}; this.update = () => {}; }
Chart.defaults = { font: {}, plugins: { legend: { labels: {} }, tooltip: {} } }; Chart.register = () => {};
const ctx = { document, Chart, console, Intl, Date, JSON, Math, Promise, setTimeout, clearTimeout, encodeURIComponent, URLSearchParams,
  Array, Object, String, Number, Error, location: { hash: '', pathname: '/', search: '', href: '' },
  history: { replaceState() {}, pushState() {} }, localStorage: { getItem: () => null, setItem() {} },
  matchMedia: () => ({ matches: true, addEventListener() {} }), getComputedStyle: () => ({ getPropertyValue: () => '#888' }),
  navigator: {}, addEventListener() {},
  fetch: (p, o = {}) => { calls.push(p.split('?')[0]); return fetch(`http://127.0.0.1:${port}${p}`, { ...o, headers: { Authorization: auth } }); } };
ctx.window = ctx; ctx.globalThis = ctx;
process.on('unhandledRejection', e => errors.push('rejet : ' + (e && e.stack || e).toString().split('\n').slice(0, 2).join(' | ')));
try { vm.runInNewContext(js, ctx, { filename: 'index.js' }); } catch (e) { errors.push('exception : ' + e.stack.split('\n').slice(0, 3).join(' | ')); }
setTimeout(() => {
  const txt = id => String(els[id]?.textContent || els[id]?.innerHTML || '').replace(/\s+/g, ' ').slice(0, 110);
  const uniq = [...new Set(calls)];
  console.log('API appelées :', uniq.length, '·', uniq.map(c => c.replace('/api/', '')).join(' '));
  for (const id of ['sub', 'k-balance', 'k-alerts', 'k-alerts-sub']) console.log(' ', id, '→', txt(id));
  const al = els['alerts']?.innerHTML || '';
  console.log('  alertes : boutons C’est bon', (al.match(/data-resolve=/g) || []).length, '· Rouvrir', (al.match(/data-reopen=/g) || []).length);
  console.log('  transactions rendues :', (els['tx']?.innerHTML.match(/data-tx=/g) || []).length);
  // prévision : chaque vue est rendue tour à tour (ce que fait un clic sur un onglet)
  const names = [...(els['forecast-tabs']?.innerHTML || '').matchAll(/data-fview="([^"]*)"/g)].map(m => m[1]);
  console.log('  prévision : onglets', JSON.stringify(names), '· affichés :', els['forecast-tabs']?.style.display);
  for (const n of names.length ? names : [null]) {
    try {
      if (n !== null) vm.runInContext(`forecastView = ${JSON.stringify(n)}; renderForecast();`, ctx);
      const h = els['forecast']?.innerHTML || '';
      console.log(`   [${n ?? 'vue unique'}]`, txt('forecast-hint'), '· comptes', (h.match(/class="fc /g) || []).length,
        '· en manque', (h.match(/class="fc short"/g) || []).length, '·', (h.match(/→ virer[^<]*/) || [''])[0].trim());
    } catch (e) { errors.push(`vue ${n} : ` + e.stack.split('\n').slice(0, 2).join(' | ')); }
  }
  console.log(errors.length ? 'ERREURS :\n' + errors.join('\n') : 'aucune erreur JS');
  process.exit(errors.length ? 1 : 0);
}, 4000);
