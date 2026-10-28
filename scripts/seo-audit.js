#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(process.argv[2] || path.join(__dirname, '..'));
const ignoredDirectories = new Set(['.git', 'node_modules', '.vercel', 'dist', 'build', 'coverage', 'out']);

function decodeEntities(value) {
  const named = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', ndash: '–', mdash: '—', rsquo: '’', lsquo: '‘', rdquo: '”', ldquo: '“', hellip: '…' };
  return value.replace(/&(#(?:x[\da-f]+|\d+)|[a-z][\da-z]+);/gi, (whole, entity) => {
    if (entity[0] === '#') {
      const hex = entity[1]?.toLowerCase() === 'x';
      const number = Number.parseInt(entity.slice(hex ? 2 : 1), hex ? 16 : 10);
      try { return Number.isFinite(number) ? String.fromCodePoint(number) : whole; } catch { return whole; }
    }
    return named[entity.toLowerCase()] ?? whole;
  });
}

function parseAttributes(source) {
  const attributes = Object.create(null);
  const body = source.replace(/^\s*[^\s/>]+/, '');
  const pattern = /([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
  let match;
  while ((match = pattern.exec(body))) {
    attributes[match[1].toLowerCase()] = decodeEntities(match[2] ?? match[3] ?? match[4] ?? '');
  }
  return attributes;
}

function parseHtml(source) {
  const result = { titles: [], descriptions: [], canonicals: [], h1: [], ids: [] };
  let inHead = false;
  let titleText = null;
  let headingText = null;
  let cursor = 0;

  function addText(text) {
    if (titleText !== null) titleText += text;
    if (headingText !== null) headingText += text;
  }

  while (cursor < source.length) {
    const opening = source.indexOf('<', cursor);
    if (opening < 0) { addText(source.slice(cursor)); break; }
    if (opening > cursor) addText(source.slice(cursor, opening));
    if (source.startsWith('<!--', opening)) {
      const commentEnd = source.indexOf('-->', opening + 4);
      cursor = commentEnd < 0 ? source.length : commentEnd + 3;
      continue;
    }

    let end = opening + 1;
    let quote = null;
    for (; end < source.length; end++) {
      const character = source[end];
      if (quote) { if (character === quote) quote = null; }
      else if (character === '"' || character === "'") quote = character;
      else if (character === '>') break;
    }
    if (end >= source.length) break;
    const rawTag = source.slice(opening + 1, end);
    const closing = /^\s*\//.test(rawTag);
    const tagMatch = rawTag.match(/^\s*\/?\s*([a-z][\w:-]*)/i);
    if (!tagMatch) { cursor = end + 1; continue; }
    const tag = tagMatch[1].toLowerCase();

    if (closing) {
      if (tag === 'title' && titleText !== null) {
        result.titles.push(decodeEntities(titleText).replace(/\s+/g, ' ').trim());
        titleText = null;
      } else if (tag === 'h1' && headingText !== null) {
        result.h1.push(decodeEntities(headingText).replace(/\s+/g, ' ').trim());
        headingText = null;
      } else if (tag === 'head') inHead = false;
      cursor = end + 1;
      continue;
    }

    const attributes = parseAttributes(rawTag);
    if (attributes.id) result.ids.push(attributes.id);
    if (tag === 'head') inHead = true;
    if (tag === 'title') titleText = '';
    if (tag === 'h1') headingText = '';
    if (inHead && tag === 'meta' && attributes.name?.toLowerCase() === 'description') {
      result.descriptions.push(attributes.content || '');
    }
    if (inHead && tag === 'link' && attributes.rel?.toLowerCase().split(/\s+/).includes('canonical')) {
      result.canonicals.push(attributes.href || '');
    }

    cursor = end + 1;
    // Ignore raw script/style contents so markup-looking strings are not counted as document elements.
    if ((tag === 'script' || tag === 'style') && !/\/\s*$/.test(rawTag)) {
      const closingTag = new RegExp(`<\\/\\s*${tag}\\s*>`, 'ig');
      closingTag.lastIndex = cursor;
      const close = closingTag.exec(source);
      if (!close) break;
      cursor = closingTag.lastIndex;
    }
  }
  return result;
}

function collectHtml(directory, collected = []) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    if (entry.isDirectory() && ignoredDirectories.has(entry.name)) continue;
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) collectHtml(absolute, collected);
    else if (entry.isFile() && entry.name.toLowerCase().endsWith('.html')) collected.push(absolute);
  }
  return collected;
}

const errors = [];
const warnings = [];
const files = collectHtml(root).sort();
for (const file of files) {
  const relative = path.relative(root, file).split(path.sep).join('/');
  const source = fs.readFileSync(file, 'utf8');
  const page = parseHtml(source);
  if (page.titles.length !== 1 || !page.titles[0]) errors.push(`${relative}: expected exactly one non-empty <title>, found ${page.titles.length}`);
  if (page.descriptions.length !== 1 || !page.descriptions[0].trim()) errors.push(`${relative}: expected exactly one non-empty meta description in <head>, found ${page.descriptions.length}`);
  if (page.canonicals.length !== 1) errors.push(`${relative}: expected exactly one canonical link in <head>, found ${page.canonicals.length}`);
  else {
    try {
      const canonical = new URL(page.canonicals[0]);
      if (!['https:', 'http:'].includes(canonical.protocol)) throw new Error('non-HTTP URL');
    } catch { errors.push(`${relative}: canonical is not an absolute HTTP(S) URL (${page.canonicals[0]})`); }
  }
  if (page.titles.length === 1 && Array.from(page.titles[0]).length > 70) {
    warnings.push(`${relative}: title is ${Array.from(page.titles[0]).length} characters (>70): ${page.titles[0]}`);
  }
  if (page.h1.length > 1) warnings.push(`${relative}: ${page.h1.length} source H1 elements`);
  if (page.h1.some(heading => !heading)) warnings.push(`${relative}: empty source H1 element`);
  const duplicates = [...new Set(page.ids.filter((id, index) => page.ids.indexOf(id) !== index))];
  if (duplicates.length) warnings.push(`${relative}: duplicate IDs: ${duplicates.join(', ')}`);
}

console.log(`SEO source audit: ${files.length} HTML files`);
console.log(`Errors: ${errors.length}; warnings: ${warnings.length}`);
if (errors.length) { console.log('\nErrors:'); errors.forEach(message => console.log(`- ${message}`)); }
if (warnings.length) { console.log('\nWarnings:'); warnings.forEach(message => console.log(`- ${message}`)); }
if (!errors.length && !warnings.length) console.log('All checked source metadata and H1 counts pass.');
console.log('Limit: this scans source HTML only; it does not execute JavaScript or verify rendered DOM/runtime routes.');
process.exitCode = errors.length ? 1 : 0;
