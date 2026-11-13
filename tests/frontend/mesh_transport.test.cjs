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

test('logout clears cached boss mesh credentials before an anonymous request', async () => {
  const saved = storage({ lan_mesh_user_token: 'boss-user', lan_mesh_token: 'boss-mesh' });
  let businessToken;
  const client = MeshTransport.create({ storage: saved, fetch: async (url, opts) => {
    const token = opts.headers.get('Authorization');
    if (url.includes('auth-token')) return json(token === 'Bearer boss-user'
      ? { auth_enabled: true, role: 'boss', mesh_token: 'boss-mesh' }
      : { auth_enabled: true, role: '', mesh_token: '', users: [{ role: 'boss' }] });
    businessToken = token;
    return new Response('unauthorized', { status: 401 });
  }});
  await client.ensure();
  client.setUserToken('');
  assert.equal(saved.getItem('lan_mesh_token'), null);
  assert.equal((await client.request('/api/tasks')).status, 401);
  assert.equal(businessToken, null);
  assert.equal(client.getRole(), '');
});

test('invalid credentials cannot silently reuse a previous boss mesh token', async () => {
  const saved = storage({ lan_mesh_user_token: 'revoked', lan_mesh_token: 'old-boss' });
  const client = MeshTransport.create({ storage: saved, fetch: async () =>
    json({ auth_enabled: true, mesh_token: '', role: '', users: [{ role: 'boss' }] }) });
  await client.ensure();
  assert.equal(client.getMeshToken(), '');
  assert.equal(saved.getItem('lan_mesh_token'), null);
});

test('viewer permission errors remain raw and do not upgrade its identity', async () => {
  const client = MeshTransport.create({ storage: storage({ lan_mesh_user_token: 'viewer' }), fetch: async url =>
    url.includes('auth-token') ? json({ auth_enabled: true, role: 'viewer', mesh_token: '' })
      : new Response('forbidden', { status: 403 }) });
  assert.equal((await client.request('/api/station/activate-secretary', { method: 'POST' })).status, 403);
  assert.equal(client.getRole(), 'viewer');
  assert.deepEqual(client.webSocketProtocols(), ['lan-mesh', 'token.viewer']);
});
