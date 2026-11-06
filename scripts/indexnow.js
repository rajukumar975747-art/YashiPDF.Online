#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');

const HOST = 'www.yashipdf.online';
const ORIGIN = `https://${HOST}`;
const ENDPOINT = 'https://api.indexnow.org/indexnow';
const MAX_URLS_PER_REQUEST = 10000;
const DEFAULT_TIMEOUT_MS = 15 * 60 * 1000;
const DEFAULT_POLL_MS = 15000;
const DEFAULT_RETRIES = 5;
const DEFAULT_ROOT = path.resolve(__dirname, '..');

function normalizeUrl(value, host = HOST) {
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    const url = new URL(value.trim());
    if (url.protocol !== 'https:' || url.hostname !== host || url.port || url.username || url.password || url.hash) return null;
    return url.href;
  } catch {
    return null;
  }
}

function filterUrls(values, host = HOST) {
  const result = [];
  const seen = new Set();
  for (const value of values || []) {
    const url = normalizeUrl(value, host);
    if (url && !seen.has(url)) {
      seen.add(url);
      result.push(url);
    }
  }
  return result;
}

function xmlDecode(value) {
  return value
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replace(/&#x([\da-f]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)));
}

function extractSitemapUrls(xml, host = HOST) {
  if (typeof xml !== 'string' || !/<urlset\b/i.test(xml) || !/<\/urlset\s*>/i.test(xml)) {
    throw new Error('Sitemap is missing a complete urlset element.');
  }
  const values = [...xml.matchAll(/<loc\b[^>]*>([\s\S]*?)<\/loc\s*>/gi)].map(match => xmlDecode(match[1].trim()));
  return filterUrls(values, host);
}

function canonicalUrls(html) {
  const found = [];
  for (const match of String(html).matchAll(/<link\b[^>]*>/gi)) {
    const tag = match[0];
    const rel = tag.match(/\brel\s*=\s*(["'])(.*?)\1/i)?.[2] || '';
    if (!rel.toLowerCase().split(/\s+/).includes('canonical')) continue;
    const href = tag.match(/\bhref\s*=\s*(["'])(.*?)\1/i)?.[2];
    if (href) found.push(xmlDecode(href));
  }
  return found;
}

function compileVercelSource(source) {
  let pattern = '^';
  const names = [];
  for (let i = 0; i < source.length;) {
    if (source[i] !== ':') {
      pattern += source[i].replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      i++;
      continue;
    }
    i++;
    const nameMatch = source.slice(i).match(/^[A-Za-z_][A-Za-z\d_]*/);
    if (!nameMatch) {
      pattern += ':';
      continue;
    }
    const name = nameMatch[0];
    names.push(name);
    i += name.length;
    let customPattern = null;
    if (source[i] === '(') {
      const close = source.indexOf(')', i + 1);
      if (close < 0) throw new Error(`Invalid Vercel route pattern: ${source}`);
      customPattern = source.slice(i + 1, close);
      i = close + 1;
    }
    const modifier = source[i] === '*' || source[i] === '+' ? source[i++] : '';
    pattern += customPattern ? `(${customPattern})` : (modifier ? '(.*)' : '([^/]+)');
  }
  return { regex: new RegExp(`${pattern}$`), names };
}

function matchVercelRule(rule, pathname) {
  if (!rule || typeof rule.source !== 'string') return null;
  try {
    const compiled = compileVercelSource(rule.source);
    const match = pathname.match(compiled.regex);
    if (!match) return null;
    const params = Object.fromEntries(compiled.names.map((name, i) => [name, match[i + 1]]));
    return { params, match };
  } catch {
    return null;
  }
}

function interpolateDestination(destination, params) {
  return destination.replace(/:([A-Za-z_][A-Za-z\d_]*)/g, (_, name) => params[name] ?? '');
}

function safeLocalPath(root, pathname) {
  let decoded;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return null;
  }
  if (decoded.includes('\0') || decoded.includes('\\')) return null;
  const relative = decoded.replace(/^\/+/, '');
  const absolute = path.resolve(root, relative);
  if (absolute !== root && !absolute.startsWith(`${root}${path.sep}`)) return null;
  return { relative, absolute };
}

function resolvePathToFile(root, pathname, existsSync = fs.existsSync) {
  const safe = safeLocalPath(root, pathname);
  if (!safe) return null;
  const hasExtension = path.posix.extname(safe.relative) !== '';
  const candidates = [];
  if (!safe.relative) candidates.push('index.html');
  else if (hasExtension) candidates.push(safe.relative);
  else candidates.push(`${safe.relative}.html`, path.posix.join(safe.relative, 'index.html'), safe.relative);
  for (const candidate of candidates) {
    const fullPath = path.resolve(root, candidate);
    if ((fullPath === root || fullPath.startsWith(`${root}${path.sep}`)) && existsSync(fullPath)) return candidate.replace(/\\/g, '/');
  }
  return null;
}

function resolvePublicRoute(value, config, root = DEFAULT_ROOT, existsSync = fs.existsSync) {
  const url = normalizeUrl(value);
  if (!url) return { kind: 'invalid', file: null };
  const pathname = new URL(url).pathname;
  for (const rule of config.redirects || []) {
    const match = matchVercelRule(rule, pathname);
    if (match) return { kind: 'redirect', destination: interpolateDestination(rule.destination || '/', match.params), file: null };
  }
  let resolvedPath = pathname;
  for (const rule of config.rewrites || []) {
    const match = matchVercelRule(rule, pathname);
    if (match) {
      resolvedPath = interpolateDestination(rule.destination || pathname, match.params);
      break;
    }
  }
  return { kind: 'file', file: resolvePathToFile(root, resolvedPath, existsSync), path: resolvedPath };
}

function parseNameStatusZ(output) {
  const tokens = output.split('\0');
  const changes = [];
  for (let i = 0; i < tokens.length;) {
    const status = tokens[i++];
    if (!status) continue;
    if (status.startsWith('R') || status.startsWith('C')) {
      const oldPath = tokens[i++];
      const newPath = tokens[i++];
      if (oldPath && newPath) changes.push({ status: 'R', oldPath, newPath });
    } else {
      const file = tokens[i++];
      if (file) changes.push({ status: status[0], path: file });
    }
  }
  return changes;
}

function normalizeRepoPath(value) {
  const normalized = value.replace(/\\/g, '/');
  if (normalized.startsWith('/') || normalized.split('/').includes('..') || normalized.includes('\0')) return null;
  return normalized;
}

function isHtmlPath(value) {
  return Boolean(normalizeRepoPath(value) && /\.html?$/i.test(value));
}

function readGitText(root, ref, file) {
  try {
    return execFileSync('git', ['show', `${ref}:${file}`], { cwd: root, encoding: 'utf8', maxBuffer: 20 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
  } catch {
    return null;
  }
}

function gitChangedFiles(root, base, head) {
  const output = execFileSync('git', ['diff', '--name-status', '-z', '--find-renames', base, head, '--'], { cwd: root, encoding: 'utf8', maxBuffer: 20 * 1024 * 1024 });
  return parseNameStatusZ(output);
}

function collectChangedUrls({ root = DEFAULT_ROOT, base, head, readText = readGitText, getChanges = gitChangedFiles, existsSync = fs.existsSync } = {}) {
  if (!base || !head || /^0+$/.test(base)) return { items: [], skipped: ['The push event has no usable base commit; refusing to notify a full site.'] };
  const changes = getChanges(root, base, head);
  const htmlChanges = [];
  for (const change of changes) {
    if (change.status === 'R') {
      if (isHtmlPath(change.oldPath)) htmlChanges.push({ status: 'D', path: change.oldPath });
      if (isHtmlPath(change.newPath)) htmlChanges.push({ status: 'A', path: change.newPath });
    } else if (isHtmlPath(change.path)) htmlChanges.push(change);
  }
  if (!htmlChanges.length) return { items: [], skipped: [] };

  const oldSitemapText = readText(root, base, 'sitemap.xml');
  const newSitemapText = readText(root, head, 'sitemap.xml');
  const oldUrls = new Set(oldSitemapText ? extractSitemapUrls(oldSitemapText) : []);
  const newUrls = new Set(newSitemapText ? extractSitemapUrls(newSitemapText) : []);
  const configText = readText(root, head, 'vercel.json');
  if (!newSitemapText || !configText) throw new Error('Could not read the head revision sitemap.xml or vercel.json.');
  const config = JSON.parse(configText);
  const byUrl = new Map();
  const skipped = [];

  function add(url, kind, expectedText) {
    const normalized = normalizeUrl(url);
    if (!normalized) return;
    const existing = byUrl.get(normalized);
    if (existing?.kind === 'upsert' || kind === 'upsert') byUrl.set(normalized, { url: normalized, kind: kind === 'upsert' ? kind : existing.kind, expectedText: kind === 'upsert' ? expectedText : existing.expectedText });
    else byUrl.set(normalized, { url: normalized, kind });
  }

  for (const change of htmlChanges) {
    const currentText = change.status === 'D' ? null : readText(root, head, change.path);
    if (currentText !== null && currentText !== undefined) {
      const canonicals = canonicalUrls(currentText);
      if (canonicals.length !== 1) {
        skipped.push(`${change.path}: expected exactly one canonical tag, found ${canonicals.length}.`);
      } else {
        const [canonical] = canonicals;
        const url = normalizeUrl(canonical);
        const route = url && resolvePublicRoute(url, config, root, existsSync);
        if (!url || !newUrls.has(url) || route?.kind !== 'file' || route.file !== change.path) {
          skipped.push(`${change.path}: canonical URL is invalid, absent from the current sitemap, redirected, or does not map back to that file.`);
        } else {
          add(url, 'upsert', currentText);
        }
      }
    }

    if (change.status === 'D' || (change.status === 'M' && currentText)) {
      const previousText = readText(root, base, change.path);
      if (!previousText) continue;
      const previousCanonicals = canonicalUrls(previousText);
      if (previousCanonicals.length !== 1) continue;
      const oldUrl = normalizeUrl(previousCanonicals[0]);
      if (!oldUrl || !oldUrls.has(oldUrl) || newUrls.has(oldUrl)) continue;
      const currentRoute = resolvePublicRoute(oldUrl, config, root, existsSync);
      if (currentRoute.kind === 'file' && currentRoute.file) continue;
      add(oldUrl, 'delete');
    }
  }
  return { items: [...byUrl.values()], skipped };
}

function buildPayload(urlList, key, host = HOST) {
  if (host !== HOST) throw new Error(`IndexNow submissions are restricted to ${HOST}.`);
  const urls = filterUrls(urlList, host);
  if (!/^[a-f\d]{32,128}$/i.test(key)) throw new Error('The IndexNow key must be a 32–128 character hexadecimal string.');
  return {
    host,
    key,
    keyLocation: `https://${host}/${key}.txt`,
    urlList: urls
  };
}

function batch(values, size = MAX_URLS_PER_REQUEST) {
  if (!Number.isInteger(size) || size < 1 || size > MAX_URLS_PER_REQUEST) throw new Error(`Batch size must be between 1 and ${MAX_URLS_PER_REQUEST}.`);
  const result = [];
  for (let i = 0; i < values.length; i += size) result.push(values.slice(i, i + size));
  return result;
}

function retryDelay(response, attempt, maxDelayMs = 60000) {
  const retryAfter = response?.headers?.get?.('retry-after');
  if (retryAfter) {
    const seconds = Number(retryAfter);
    if (Number.isFinite(seconds)) return Math.min(maxDelayMs, Math.max(0, seconds * 1000));
    const date = Date.parse(retryAfter);
    if (Number.isFinite(date)) return Math.min(maxDelayMs, Math.max(0, date - Date.now()));
  }
  return Math.min(maxDelayMs, 1000 * (2 ** attempt));
}

async function postBatch(urlList, { key, fetchImpl = globalThis.fetch, sleepImpl = ms => new Promise(resolve => setTimeout(resolve, ms)), maxAttempts = DEFAULT_RETRIES, endpoint = ENDPOINT, onAttempt = () => {} } = {}) {
  if (!Array.isArray(urlList) || urlList.length < 1 || urlList.length > MAX_URLS_PER_REQUEST) throw new Error(`Each IndexNow request must contain 1–${MAX_URLS_PER_REQUEST} URLs.`);
  const payload = buildPayload(urlList, key);
  if (!payload.urlList.length) throw new Error('The IndexNow request contains no valid URLs.');
  let lastMessage = 'Unknown request failure';
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    onAttempt(attempt + 1);
    let response;
    try {
      response = await fetchImpl(endpoint, {
        method: 'POST',
        headers: { 'content-type': 'application/json; charset=utf-8' },
        body: JSON.stringify(payload)
      });
    } catch (error) {
      lastMessage = `Network error: ${error.message}`;
      if (attempt + 1 >= maxAttempts) break;
      await sleepImpl(retryDelay(null, attempt));
      continue;
    }
    if (response.status === 200 || response.status === 202) return { ok: true, status: response.status, attempts: attempt + 1 };
    if (response.status === 429 || response.status >= 500) {
      lastMessage = `Temporary IndexNow response ${response.status}`;
      if (attempt + 1 >= maxAttempts) break;
      await sleepImpl(retryDelay(response, attempt));
      continue;
    }
    const reasons = {
      400: 'Bad request; review the JSON payload and URL list.',
      403: 'IndexNow rejected the key or could not verify the key file.',
      422: 'IndexNow rejected one or more URLs for host verification.'
    };
    return { ok: false, status: response.status, attempts: attempt + 1, message: reasons[response.status] || `Permanent IndexNow response ${response.status}` };
  }
  return { ok: false, status: null, attempts: maxAttempts, message: lastMessage };
}

async function submitUrls(urls, options = {}) {
  const valid = filterUrls(urls);
  const batches = batch(valid, MAX_URLS_PER_REQUEST);
  const results = [];
  for (const urlBatch of batches) {
    results.push(await postBatch(urlBatch, options));
    if (!results.at(-1).ok) break;
  }
  return { submitted: valid.length, batches: results };
}

function readKey(root = DEFAULT_ROOT) {
  const files = fs.readdirSync(root).filter(name => /^[a-f\d]{32,128}\.txt$/i.test(name));
  if (files.length !== 1) throw new Error(`Expected exactly one root IndexNow key file; found ${files.length}.`);
  const key = files[0].slice(0, -4);
  const contents = fs.readFileSync(path.join(root, files[0]), 'utf8');
  if (contents !== key) throw new Error('IndexNow key file content must exactly match its filename key, with no extra whitespace.');
  return key;
}

function normalizePublishedContent(value) {
  return String(value).replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n');
}

function contentMatches(expected, actual) {
  return crypto.createHash('sha256').update(normalizePublishedContent(expected)).digest('hex') ===
    crypto.createHash('sha256').update(normalizePublishedContent(actual)).digest('hex');
}

async function fetchText(url, fetchImpl) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetchImpl(url, { method: 'GET', cache: 'no-store', redirect: 'manual', signal: controller.signal });
    const text = await response.text();
    return { status: response.status, text };
  } finally {
    clearTimeout(timer);
  }
}

async function waitForResource(item, { fetchImpl = globalThis.fetch, sleepImpl = ms => new Promise(resolve => setTimeout(resolve, ms)), timeoutMs = DEFAULT_TIMEOUT_MS, intervalMs = DEFAULT_POLL_MS } = {}) {
  const end = Date.now() + timeoutMs;
  while (Date.now() <= end) {
    try {
      const response = await fetchText(item.url, fetchImpl);
      if (item.kind === 'key' && response.status === 200 && response.text === item.expectedText) return true;
      if (item.kind === 'upsert' && response.status === 200 && contentMatches(item.expectedText, response.text)) return true;
      if (item.kind === 'delete' && ([301, 302, 303, 307, 308, 404, 410].includes(response.status))) return true;
    } catch {
      // Deployment may still be in progress or the site may be temporarily unreachable.
    }
    if (Date.now() + intervalMs > end) break;
    await sleepImpl(intervalMs);
  }
  return false;
}

async function waitForDeployment(items, { key, fetchImpl = globalThis.fetch, sleepImpl, timeoutMs, intervalMs, concurrency = 10 } = {}) {
  const keyOk = await waitForResource({ kind: 'key', url: `https://${HOST}/${key}.txt`, expectedText: key }, { fetchImpl, sleepImpl, timeoutMs, intervalMs });
  if (!keyOk) return { ok: false, failed: ['key verification file'] };
  const failed = [];
  let index = 0;
  async function worker() {
    while (index < items.length) {
      const item = items[index++];
      const ok = await waitForResource(item, { fetchImpl, sleepImpl, timeoutMs, intervalMs });
      if (!ok) failed.push(item.url);
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, worker));
  return { ok: failed.length === 0, failed };
}

function readCurrentSitemap(root = DEFAULT_ROOT) {
  return fs.readFileSync(path.join(root, 'sitemap.xml'), 'utf8');
}

function collectInitialUrls(root = DEFAULT_ROOT) {
  const urls = extractSitemapUrls(readCurrentSitemap(root));
  const config = JSON.parse(fs.readFileSync(path.join(root, 'vercel.json'), 'utf8'));
  const items = [];
  const skipped = [];
  for (const url of urls) {
    const route = resolvePublicRoute(url, config, root);
    if (route.kind !== 'file' || !route.file || !route.file.toLowerCase().endsWith('.html')) {
      skipped.push(`${url}: sitemap route does not map to an existing local HTML page.`);
      continue;
    }
    const html = fs.readFileSync(path.join(root, route.file), 'utf8');
    const canonicals = canonicalUrls(html).map(value => normalizeUrl(value));
    if (canonicals.length !== 1 || canonicals[0] !== url) {
      skipped.push(`${url}: mapped page canonical does not match the sitemap URL.`);
      continue;
    }
    items.push({ url, kind: 'upsert', expectedText: html });
  }
  return { items, skipped };
}

function parseArgs(args) {
  const result = { command: args[0] };
  for (let i = 1; i < args.length; i++) {
    if (args[i].startsWith('--')) result[args[i].slice(2)] = args[i + 1] && !args[i + 1].startsWith('--') ? args[++i] : true;
  }
  return result;
}

async function runCli(args = process.argv.slice(2), root = DEFAULT_ROOT) {
  const parsed = parseArgs(args);
  if (!['notify', 'sitemap'].includes(parsed.command)) {
    console.error('Usage: node scripts/indexnow.js notify --base <sha> --head <sha> [--wait] | sitemap [--wait]');
    return 2;
  }
  try {
    const key = readKey(root);
    let items;
    let skipped = [];
    if (parsed.command === 'notify') {
      const result = collectChangedUrls({ root, base: parsed.base, head: parsed.head });
      items = result.items;
      skipped = result.skipped;
    } else {
      const result = collectInitialUrls(root);
      items = result.items;
      skipped = result.skipped;
    }
    for (const reason of skipped) console.warn(`Skipped: ${reason}`);
    if (parsed.command === 'sitemap' && skipped.length) {
      console.error('Initial sitemap submission stopped because one or more sitemap URLs could not be verified locally. No IndexNow request was sent.');
      return 1;
    }
    if (!items.length) {
      console.log('No eligible page URLs to submit.');
      return skipped.length ? 1 : 0;
    }
    console.log(`Prepared ${items.length} ${parsed.command === 'sitemap' ? 'existing sitemap' : 'changed page'} URL(s).`);
    const wait = parsed.wait === true || parsed.wait === 'true';
    if (wait) {
      console.log('Waiting for the production key file and changed page state to match this deployment...');
      const readiness = await waitForDeployment(items, { key });
      if (!readiness.ok) {
        console.error(`Production readiness check timed out for ${readiness.failed.length} resource(s). No IndexNow request was sent.`);
        return 1;
      }
    }
    const result = await submitUrls(items.map(item => item.url), { key });
    for (const batchResult of result.batches) {
      if (batchResult.ok) console.log(`IndexNow accepted a batch (HTTP ${batchResult.status}) after ${batchResult.attempts} attempt(s).`);
      else console.error(`IndexNow batch failed${batchResult.status ? ` (HTTP ${batchResult.status})` : ''}: ${batchResult.message}`);
    }
    return result.batches.every(item => item.ok) ? 0 : 1;
  } catch (error) {
    console.error(`IndexNow workflow failed: ${error.message}`);
    return 1;
  }
}

if (require.main === module) {
  runCli().then(code => { process.exitCode = code; });
}

module.exports = {
  HOST, ORIGIN, ENDPOINT, MAX_URLS_PER_REQUEST,
  normalizeUrl, filterUrls, extractSitemapUrls, canonicalUrls,
  compileVercelSource, matchVercelRule, resolvePathToFile, resolvePublicRoute,
  parseNameStatusZ, isHtmlPath, collectChangedUrls,
  buildPayload, batch, retryDelay, postBatch, submitUrls,
  readKey, normalizePublishedContent, contentMatches,
  waitForResource, waitForDeployment, collectInitialUrls, runCli
};
