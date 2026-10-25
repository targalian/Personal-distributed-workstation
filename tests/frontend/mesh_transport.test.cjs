/* Behavioral contracts for shared browser authentication and request transport. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
require('../../lan_mesh/web/static/mesh_transport.js');

function storage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return { getItem: key => data.get(key) || null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key) };
}
const json = value => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });

test('concurrent requests wait for one bootstrap and retain caller headers', async () => {
  let release;
  const gate = new Promise(resolve => release = resolve);
  let bootstraps = 0;
  const seen = [];
  const client = MeshTransport.create({ storage: storage(), fetch: async (url, opts) => {
    if (url.includes('auth-token')) { bootstraps++; await gate; return json({ auth_enabled: true, mesh_token: 'mesh', role: 'boss' }); }
    seen.push(opts.headers);
    return json({ ok: true });
  }});
  const opts = { headers: new Headers({ 'X-Custom': 'kept' }) };
  const requests = Array.from({ length: 8 }, () => client.request('/api/tasks', opts));
  assert.equal(bootstraps, 1);
  assert.equal(seen.length, 0);
  release();
  await Promise.all(requests);
  assert.equal(seen.length, 8);
  assert.ok(seen.every(headers => headers.get('Authorization') === 'Bearer mesh' && headers.get('X-Custom') === 'kept'));
  assert.equal(opts.headers.has('Authorization'), false);
});

test('an old bootstrap cannot overwrite a newly logged-in identity', async () => {
  let release;
  const gate = new Promise(resolve => release = resolve);
  const seen = [];
  const client = MeshTransport.create({ storage: storage({ lan_mesh_user_token: 'old' }), fetch: async (url, opts) => {
    const token = opts.headers.get('Authorization');
    seen.push(token);
    if (token === 'Bearer old') { await gate; return json({ role: 'boss' }); }
    return json({ role: 'viewer', auth_enabled: true, mesh_token: 'mesh' });
  }});
  const previous = client.ensure();
  client.setUserToken('new');
  await client.ensure();
  release(); await previous;
  assert.equal(client.getRole(), 'viewer');
  await client.request('/api/tasks');
  assert.equal(seen.at(-1), 'Bearer new');
});

test('a failed bootstrap can retry and business HTTP errors remain raw', async () => {
  let attempts = 0;
  const client = MeshTransport.create({ storage: storage(), fetch: async url => {
    if (url.includes('auth-token')) {
      if (++attempts === 1) throw new Error('offline');
      return json({ role: 'boss' });
    }
    return new Response('missing', { status: 404 });
  }});
  await client.ensure();
  assert.equal((await client.request('/api/missing')).status, 404);
  assert.equal(attempts, 2);
});

test('FormData boundaries and AbortSignal pass through unchanged', async () => {
  let captured;
  const client = MeshTransport.create({ storage: storage(), fetch: async (url, opts) => {
    if (url.includes('auth-token')) return json({ role: 'boss' });
    captured = opts; return json({ ok: true });
  }});
  const form = new FormData(); form.append('file', 'data');
  const controller = new AbortController();
  await client.request('/shared', { method: 'POST', body: form, signal: controller.signal });
  assert.equal(captured.body, form);
  assert.equal(captured.signal, controller.signal);
  assert.equal(captured.headers.has('Content-Type'), false);
});

test('synchronous fetch failures also release bootstrap ownership for retry', async () => {
  let calls = 0;
  const client = MeshTransport.create({ storage: storage(), fetch: () => {
    if (++calls === 1) throw new Error('synchronous transport failure');
    return Promise.resolve(json({ role: 'boss' }));
  }});
  await client.ensure();
  await client.ensure();
  assert.equal(calls, 2);
  assert.equal(client.getRole(), 'boss');
});
