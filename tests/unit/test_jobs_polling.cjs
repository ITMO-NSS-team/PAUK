const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createJobsPoller } = require('../../pauk/admin/static/jobs.js');

function fixture(initialActive = false, initialPage = '2') {
  const timers = new Map();
  let next = 0;
  let listener;
  let responseActive = initialActive;
  let focused = null;
  let responsePage = "2";
  const focusedElement = {};
  const detail = { dataset: { jobDetails: 'one' }, open: true };
  const incomingDetail = { dataset: { jobDetails: 'one' }, open: false };
  const active = { contains: (x) => x === focusedElement && focused === 'active', replaceChildren(...nodes) { this.nodes = nodes; } };
  const history = { contains: (x) => x === focusedElement && focused === 'history', querySelectorAll: () => [detail], replaceChildren(...nodes) {
    this.nodes = nodes;
  } };
  const incoming = { childNodes: ['active'] };
  const incomingHistory = { childNodes: ['history'], querySelectorAll: () => [incomingDetail] };
  const banner = { contains: (x) => x === focusedElement && focused === 'banner', replaceChildren(...nodes) { this.nodes = nodes; } };
  const incomingBanner = { childNodes: ['graph warning'] };
  const marker = { dataset: { active: String(initialActive), page: initialPage } };
  const box = { children: [], replaceChildren(...items) { this.children = items; },
    append(...items) { this.children.push(...items); }, hidden: true };
  const elements = { 'jobs-fragment': marker, 'jobs-active': active, 'jobs-history': history,
    'jobs-refresh-status': box, 'jobs-graph-banner': banner };
  const document = { hidden: false, activeElement: null, getElementById: (id) => elements[id],
    addEventListener: (_, fn) => { listener = fn; }, createTextNode: (s) => s,
    createElement: () => ({}) };
  const env = { document, AbortController, location: { search: '?kind=collect&page=2&ignored=x' },
    setTimeout: (fn, delay) => { timers.set(++next, { fn, delay }); return next; },
    clearTimeout: (id) => timers.delete(id),
    DOMParser: class { parseFromString() { return { getElementById: (id) => ({
      'jobs-fragment': { dataset: { active: String(responseActive), page: responsePage } },
      'jobs-active': incoming, 'jobs-history': incomingHistory, 'jobs-graph-banner': incomingBanner,
    })[id] }; } },
    fetch: async () => ({ ok: true, status: 200, redirected: false, text: async () => 'fragment' }),
  };
  return { env, timers, box, detail, incomingDetail, active, history, banner, incomingBanner,
    setPage: (value) => { responsePage = value; },
    visibility: (hidden) => { document.hidden = hidden; listener(); },
    setActive: (value) => { responseActive = value; },
    focus: (value) => { focused = value; document.activeElement = value ? focusedElement : null; } };
}

test('initial interval follows active state', () => {
  for (const [active, delay] of [[true, 15000], [false, 60000]]) {
    const f = fixture(active); createJobsPoller(f.env);
    assert.equal([...f.timers.values()][0].delay, delay);
  }
});

test('request completes before the next timer and preserves details', async () => {
  const f = fixture(true); let finish; let calls = 0; let url;
  f.env.fetch = (value) => { calls++; url = value; return new Promise((resolve) => { finish = resolve; }); };
  const poller = createJobsPoller(f.env);
  const first = poller.poll(); await poller.poll(); assert.equal(calls, 1);
  finish({ ok: true, status: 200, redirected: false, text: async () => 'fragment' });
  await first;
  assert.equal(url, '/jobs/fragment?kind=collect&page=2');
  assert.equal(f.incomingDetail.open, true);
  assert.deepEqual(f.history.nodes, ['history']);
  assert.equal([...f.timers.values()][0].delay, 15000);
});

test('hidden tab stops timers and resumes immediately', () => {
  const f = fixture(); createJobsPoller(f.env); f.visibility(true);
  assert.equal(f.timers.size, 0); f.visibility(false);
  assert.equal([...f.timers.values()][0].delay, 0);
});

test('HTTP errors back off to the cap and preserve content', async () => {
  const f = fixture(true); f.env.fetch = async () => ({ ok: false, status: 503 });
  const poller = createJobsPoller(f.env);
  for (const expected of [30000, 60000, 120000, 120000]) {
    await poller.poll(); assert.equal([...f.timers.values()][0].delay, expected);
  }
  assert.equal(f.history.nodes, undefined); assert.equal(f.box.hidden, false);
});

test('expired session stops polling and leaves job content intact', async () => {
  for (const response of [
    { ok: false, status: 401, redirected: false },
    { ok: false, status: 403, redirected: false },
    { ok: true, status: 200, redirected: true },
  ]) {
    const f = fixture(); let calls = 0; let bodyRead = false;
    f.env.fetch = async () => { calls++; return { ...response, text: async () => {
      bodyRead = true; return '<html>login</html>';
    } }; };
    const poller = createJobsPoller(f.env); await poller.poll();
    assert.equal(f.timers.size, 0); assert.equal(f.box.hidden, false);
    assert.equal(f.box.children[2].href, '/login?next=%2Fjobs');
    assert.equal(f.box.children[2].textContent, 'Войти снова');
    assert.equal(f.history.nodes, undefined); assert.equal(bodyRead, false);
    f.visibility(true); f.visibility(false); await poller.poll();
    assert.equal(calls, 1); assert.equal(f.timers.size, 0);
  }
});

test('does not replace content while a job control has focus', async () => {
  const f = fixture(); f.focus('active'); const poller = createJobsPoller(f.env);
  await poller.poll(); assert.equal(f.active.nodes, undefined);
  assert.deepEqual(f.history.nodes, ['history']);
});

test('a hung request is aborted, backs off, and recovers without overlap', async () => {
  const f = fixture(true); let calls = 0; let signal;
  f.env.fetch = (_, options) => {
    calls++; signal = options.signal;
    return new Promise((resolve, reject) => {
      signal.addEventListener('abort', () => reject(new Error('aborted')));
    });
  };
  const poller = createJobsPoller(f.env);
  const pending = poller.poll(); await poller.poll();
  assert.equal(calls, 1);
  const [id, timeout] = [...f.timers.entries()][0];
  assert.equal(timeout.delay, 10000); f.timers.delete(id); timeout.fn();
  await pending;
  assert.equal(signal.aborted, true);
  assert.equal(f.history.nodes, undefined);
  assert.equal([...f.timers.values()][0].delay, 30000);
  f.env.fetch = async () => ({ ok: true, status: 200, redirected: false, text: async () => 'fragment' });
  await poller.poll();
  assert.deepEqual(f.history.nodes, ['history']);
  assert.equal(f.box.hidden, true);
  assert.equal(f.timers.size, 1);
  assert.equal([...f.timers.values()][0].delay, 15000);
});

test('graph warning follows the fragment', async () => {
  const f = fixture(); const poller = createJobsPoller(f.env);
  await poller.poll(); assert.deepEqual(f.banner.nodes, ['graph warning']);
  f.incomingBanner.childNodes = [];
  await poller.poll(); assert.deepEqual(f.banner.nodes, []);
});

test('focused block alone is deferred and updates after blur', async () => {
  for (const block of ['active', 'history', 'banner']) {
    const f = fixture(); f.focus(block); const poller = createJobsPoller(f.env);
    await poller.poll();
    for (const name of ['active', 'history', 'banner']) {
      assert.equal(f[name].nodes === undefined, name === block, `${block} focus, ${name}`);
    }
    f.focus(null); await poller.poll();
    assert.notEqual(f[block].nodes, undefined);
  }
});

test('clamped page updates the URL only when history is replaced', async () => {
  const f = fixture(); f.setPage('1'); f.focus('history');
  f.env.location.pathname = '/jobs'; f.env.location.hash = '#history';
  f.env.history = {state: null, replaceState(state, title, url) { this.url = url; }};
  const poller = createJobsPoller(f.env); await poller.poll();
  assert.equal(f.env.history.url, undefined);
  f.focus(null); await poller.poll();
  assert.equal(f.env.history.url, '/jobs?kind=collect&page=1&ignored=x#history');
});

test('initial full page normalizes an invalid page in the address', () => {
  const f = fixture(false, '1');
  f.env.location.pathname = '/jobs'; f.env.location.hash = '#history';
  f.env.history = {state: null, replaceState(state, title, url) { this.url = url; }};
  createJobsPoller(f.env);
  assert.equal(f.env.history.url, '/jobs?kind=collect&page=1&ignored=x#history');
});
