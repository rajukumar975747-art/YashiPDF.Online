'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const api = require('./indexnow');

const root = path.resolve(__dirname, '..');

test('filters URLs to unique HTTPS pages on the exact host', () => {
  assert.deepEqual(api.filterUrls([
    'https://www.yashipdf.online/a',
    'https://www.yashipdf.online/a',
    'http://www.yashipdf.online/b',
    'https://yashipdf.online/c',
    'https://www.yashipdf.online.attacker.test/d',
    'https://www.yashipdf.online:8443/e',
    'https://user@www.yashipdf.online/f',
    'https://www.yashipdf.online/g#fragment',
    'not a URL'
  ]), ['https://www.yashipdf.online/a']);
});

test('extracts sitemap URLs and maps every current entry through Vercel routing', () => {
  const sitemap = fs.readFileSync(path.join(root, 'sitemap.xml'), 'utf8');
  const config = JSON.parse(fs.readFileSync(path.join(root, 'vercel.json'), 'utf8'));
  const urls = api.extractSitemapUrls(sitemap);
  assert.equal(urls.length, 93);
  assert.equal(new Set(urls).size, 93);
  for (const url of urls) {
    const route = api.resolvePublicRoute(url, config, root);
    assert.equal(route.kind, 'file', url);
    assert.ok(route.file, `${url} did not map to a local file`);
    assert.ok(route.file.endsWith('.html'), `${url} mapped to a non-HTML file`);
  }
  const initial = api.collectInitialUrls(root);
  assert.equal(initial.items.length, 93);
  assert.deepEqual(initial.skipped, []);
});

test('resolves clean blog landing and nested article routes from Vercel rules', () => {
  const config = JSON.parse(fs.readFileSync(path.join(root, 'vercel.json'), 'utf8'));
  assert.equal(api.resolvePublicRoute('https://www.yashipdf.online/blog', config, root).file, 'blog.html');
  assert.equal(api.resolvePublicRoute('https://www.yashipdf.online/blog/how-to-archive-documents-using-pdf/a', config, root).file, 'blog/how-to-archive-documents-using-pdf/a.html');
  assert.equal(api.resolvePublicRoute('https://www.yashipdf.online/blog/best%20file%20format%20for%20digital%20documents', config, root).file, 'blog/best file format for digital documents.html');
});

test('maps a modified canonical page and its removed previous URL without duplicate notifications', () => {
  const oldUrl = 'https://www.yashipdf.online/blog/old-guide';
  const newUrl = 'https://www.yashipdf.online/blog/new-guide';
  const oldHtml = `<link rel="canonical" href="${oldUrl}">`;
  const newHtml = `<link rel="canonical" href="${newUrl}">`;
  const xml = urls => `<?xml version="1.0"?><urlset>${urls.map(url => `<url><loc>${url}</loc></url>`).join('')}</urlset>`;
  const files = new Map([
    ['base:sitemap.xml', xml([oldUrl])],
    ['head:sitemap.xml', xml([newUrl])],
    ['base:vercel.json', '{}'],
    ['head:vercel.json', JSON.stringify({
      cleanUrls: true,
      redirects: [{ source: '/blog/old-guide', destination: '/blog/new-guide', permanent: true }],
      rewrites: [{ source: '/blog/:slug', destination: '/blog/:slug.html' }]
    })],
    ['base:blog/old-guide.html', oldHtml],
    ['head:blog/new-guide.html', newHtml]
  ]);
  const result = api.collectChangedUrls({
    root,
    base: 'base',
    head: 'head',
    readText: (_, ref, file) => files.get(`${ref}:${file}`) ?? null,
    getChanges: () => [{ status: 'R', oldPath: 'blog/old-guide.html', newPath: 'blog/new-guide.html' }],
    existsSync: fullPath => fullPath.endsWith(path.join('blog', 'new-guide.html'))
  });
  assert.deepEqual(result.items.map(item => [item.url, item.kind]), [[oldUrl, 'delete'], [newUrl, 'upsert']]);
  assert.deepEqual(result.skipped, []);
});

test('reads one existing canonical tag and rejects missing or duplicate tags', () => {
  assert.deepEqual(api.canonicalUrls('<link rel="canonical" href="https://www.yashipdf.online/x">'), ['https://www.yashipdf.online/x']);
  assert.deepEqual(api.canonicalUrls('<html></html>'), []);
  assert.equal(api.canonicalUrls('<link rel="canonical" href="https://www.yashipdf.online/x"><link rel="canonical" href="https://www.yashipdf.online/y">').length, 2);
});

test('constructs the IndexNow request payload and caps batches at 10,000 URLs', () => {
  const key = 'd493aba4b2394fadb37beef58b2ec089';
  assert.deepEqual(api.buildPayload(['https://www.yashipdf.online/a', 'https://example.com/b'], key), {
    host: 'www.yashipdf.online',
    key,
    keyLocation: `https://www.yashipdf.online/${key}.txt`,
    urlList: ['https://www.yashipdf.online/a']
  });
  assert.equal(api.batch(Array.from({ length: 10001 }, (_, i) => i)).map(part => part.length).join(','), '10000,1');
  assert.throws(() => api.batch([], 10001), /Batch size/);
});

test('reads the public verification key exactly from the root key file', () => {
  assert.equal(api.readKey(root), 'd493aba4b2394fadb37beef58b2ec089');
});

test('submits URL batches of at most 10,000 entries', async () => {
  const sentLengths = [];
  const urls = Array.from({ length: 10001 }, (_, i) => `https://www.yashipdf.online/page-${i}`);
  const result = await api.submitUrls(urls, {
    key: 'd493aba4b2394fadb37beef58b2ec089',
    fetchImpl: async (_, init) => {
      sentLengths.push(JSON.parse(init.body).urlList.length);
      return response(200);
    }
  });
  assert.equal(result.submitted, 10001);
  assert.deepEqual(sentLengths, [10000, 1]);
});

function response(status, retryAfter) {
  return { status, headers: { get(name) { return name.toLowerCase() === 'retry-after' ? retryAfter || null : null; } }, async text() { return ''; } };
}

test('accepts HTTP 200 and 202 responses', async () => {
  for (const status of [200, 202]) {
    let requestUrl;
    let requestInit;
    const result = await api.postBatch(['https://www.yashipdf.online/a'], {
      key: 'd493aba4b2394fadb37beef58b2ec089', fetchImpl: async (url, init) => { requestUrl = url; requestInit = init; return response(status); }
    });
    assert.equal(result.ok, true);
    assert.equal(result.status, status);
    assert.equal(requestUrl, 'https://api.indexnow.org/indexnow');
    assert.deepEqual(JSON.parse(requestInit.body), {
      host: 'www.yashipdf.online',
      key: 'd493aba4b2394fadb37beef58b2ec089',
      keyLocation: 'https://www.yashipdf.online/d493aba4b2394fadb37beef58b2ec089.txt',
      urlList: ['https://www.yashipdf.online/a']
    });
  }
});

test('does not retry permanent HTTP 400, 403, or 422 responses', async () => {
  for (const status of [400, 403, 422]) {
    let calls = 0;
    const result = await api.postBatch(['https://www.yashipdf.online/a'], {
      key: 'd493aba4b2394fadb37beef58b2ec089', fetchImpl: async () => { calls++; return response(status); }
    });
    assert.equal(result.ok, false);
    assert.equal(result.status, status);
    assert.equal(calls, 1);
  }
});

test('retries HTTP 429 and 5xx responses with backoff', async () => {
  for (const firstStatus of [429, 503]) {
    let calls = 0;
    const delays = [];
    const result = await api.postBatch(['https://www.yashipdf.online/a'], {
      key: 'd493aba4b2394fadb37beef58b2ec089',
      fetchImpl: async () => (++calls === 1 ? response(firstStatus, '0') : response(200)),
      sleepImpl: async ms => delays.push(ms)
    });
    assert.equal(result.ok, true);
    assert.equal(calls, 2);
    assert.equal(delays.length, 1);
  }
});

test('retries network errors and reports exhausted temporary failures', async () => {
  let calls = 0;
  const delays = [];
  const result = await api.postBatch(['https://www.yashipdf.online/a'], {
    key: 'd493aba4b2394fadb37beef58b2ec089',
    maxAttempts: 3,
    fetchImpl: async () => { calls++; throw new Error('mock network down'); },
    sleepImpl: async ms => delays.push(ms)
  });
  assert.equal(result.ok, false);
  assert.equal(calls, 3);
  assert.equal(delays.length, 2);
});

test('requires the expected deployed page content before an upsert is ready', async () => {
  const html = '<!doctype html>\r\n<h1>New version</h1>\r\n';
  const ok = await api.waitForResource({ kind: 'upsert', url: 'https://www.yashipdf.online/x', expectedText: html }, {
    fetchImpl: async () => ({ status: 200, async text() { return html.replace(/\r\n/g, '\n'); } }), timeoutMs: 100, intervalMs: 1
  });
  assert.equal(ok, true);
  const notReady = await api.waitForResource({ kind: 'upsert', url: 'https://www.yashipdf.online/x', expectedText: html }, {
    fetchImpl: async () => ({ status: 200, async text() { return '<h1>Old version</h1>'; } }), timeoutMs: 0, intervalMs: 1
  });
  assert.equal(notReady, false);
});

test('recognizes deletion only after a gone or redirect response', async () => {
  const gone = await api.waitForResource({ kind: 'delete', url: 'https://www.yashipdf.online/old' }, {
    fetchImpl: async () => ({ status: 410, async text() { return ''; } }), timeoutMs: 100, intervalMs: 1
  });
  assert.equal(gone, true);
});
