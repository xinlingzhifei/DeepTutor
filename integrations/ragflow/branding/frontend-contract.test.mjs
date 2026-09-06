import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '../../..');
const upstream = resolve(here, '../upstream');
const pin = JSON.parse(readFileSync(resolve(here, '../UPSTREAM.json'), 'utf8'));
const require = createRequire(resolve(root, 'web/package.json'));
const ts = require('typescript');
const git = (...args) => execFileSync('git', args, {
  cwd: upstream, encoding: 'utf8', maxBuffer: 12 * 1024 * 1024,
});
const read = (path) => readFileSync(resolve(upstream, path), 'utf8');
const original = (path) => git('show', `${pin.commit}:${path}`);
const hash = (value) => createHash('sha256').update(value).digest('hex');
const same = (actual, expected, message) => assert.equal(
  hash(JSON.stringify(actual)), hash(JSON.stringify(expected)), message,
);
const locales = ['ar', 'bg', 'de', 'en', 'es', 'fr', 'id', 'it', 'ja', 'ko', 'pt-br', 'ru', 'tr', 'vi', 'zh-traditional', 'zh'];
const localePaths = locales.map((language) => `web/src/locales/${language}.ts`);
const displayChanges = new Map(Object.entries({
  'web/src/components/api-service/chat-overview-modal/backend-service-api.tsx': { 'RAGFlow API': 'yFei API' },
  'web/src/components/embed-dialog/index.tsx': { 'Powered by RAGFlow': 'Powered by yFei' },
  'web/src/layouts/components/global-navbar.tsx': { 'RAGFlow logo': 'yFei logo' },
  'web/src/layouts/components/header.tsx': { 'RAGFlow logo': 'yFei logo' },
  'web/src/pages/admin/layouts/navigation-layout.tsx': { logo: 'yFei logo' },
  'web/src/pages/admin/login.tsx': { logo: 'yFei logo', RAGFlow: 'yFei' },
  'web/src/pages/home/banner.tsx': { 'Welcome to RAGFlow': 'Welcome to yFei', RAGFlow: 'yFei' },
  'web/src/pages/login-next/index.tsx': { logo: 'yFei logo', RAGFlow: 'yFei' },
  'web/src/pages/next-search/ragflow-logo.tsx': { RAGFlow: 'yFei' },
  'web/src/pages/skills/components/upload-modal.tsx': { 'Uploading to RAGFlow...': 'Uploading to yFei...' },
  'web/src/pages/user-setting/data-source/constant/index.tsx': { 'ragflow lang:en': 'yFei lang:en' },
  'web/src/pages/user-setting/data-source/constant/jira-constant.tsx': { RAGFlow: 'yFei' },
}));
const changedSources = git('-c', 'core.autocrlf=false', 'diff', pin.commit, '--name-only', '--', 'web')
  .trim().split('\n').filter((path) => /\.tsx?$/.test(path));

function parse(path, text) {
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true);
  assert.equal(source.parseDiagnostics.length, 0, `${path}: invalid TypeScript syntax`);
  return source;
}

function nodes(source) {
  const result = [];
  function visit(node) {
    result.push(node);
    ts.forEachChild(node, visit);
  }
  visit(source);
  return result;
}

function isText(node) {
  return ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)
    || ts.isTemplateHead(node) || ts.isTemplateMiddle(node)
    || ts.isTemplateTail(node) || ts.isJsxText(node);
}

function expectedText(path, node) {
  const text = node.text;
  if (localePaths.includes(path)) {
    return text.replace(/RAGFlow/g, 'yFei').replace(/(?<=<i>')ragflow(?='<\/i>)/g, 'yFei');
  }
  const mapping = displayChanges.get(path);
  const value = ts.isJsxText(node) ? text.trim() : text;
  return mapping && Object.hasOwn(mapping, value)
    ? text.replace(value, mapping[value]) : text;
}

function urls(text) {
  return text.match(/https?:\s*\/\/[^\s"'`<>]+|\bcloud\.ragflow\.io\b/g) ?? [];
}

function propertyKeys(source) {
  return nodes(source).filter((node) => ts.isPropertyAssignment(node))
    .map((node) => node.name.getText(source));
}

test('frontend changes are checked against the pinned upstream source', () => {
  assert.equal(git('rev-parse', 'HEAD').trim(), pin.commit);
  same(changedSources.sort(), [...localePaths, ...displayChanges.keys()].sort(), 'unexpected or missing changed frontend source');
});

for (const path of changedSources) {
  test(`${path}: only approved display literals change`, () => {
    const before = parse(path, original(path));
    const after = parse(path, read(path));
    const oldNodes = nodes(before);
    const newNodes = nodes(after);
    assert.equal(newNodes.length, oldNodes.length, `${path}: AST structure changed`);
    for (let index = 0; index < oldNodes.length; index++) {
      const oldNode = oldNodes[index];
      const newNode = newNodes[index];
      const position = `${path}:${after.getLineAndCharacterOfPosition(newNode.getStart(after)).line + 1}`;
      assert.equal(newNode.kind, oldNode.kind, `${position}: AST node kind changed`);
      if (isText(oldNode)) {
        same(newNode.text, expectedText(path, oldNode), `${position}: unapproved string/template/JSX change`);
        same(urls(newNode.text), urls(oldNode.text), `${position}: URL changed`);
      } else if (oldNode.getChildCount(before) === 0) {
        same(newNode.getText(after), oldNode.getText(before), `${position}: identifier or syntax token changed`);
      }
      if (ts.isImportDeclaration(oldNode) || ts.isExportDeclaration(oldNode)) {
        same(newNode.getText(after), oldNode.getText(before), `${position}: import/export changed`);
      }
    }
    same(propertyKeys(after), propertyKeys(before), `${path}: property keys changed`);
    const comments = (text) => text.match(/\/\*[\s\S]*?Copyright[\s\S]*?\*\//g) ?? [];
    same(comments(after.text), comments(before.text), `${path}: copyright/license changed`);
  });

  test(`${path}: no visible old brand in strings, template fragments or JSX`, () => {
    const source = parse(path, read(path));
    for (const node of nodes(source).filter(isText)) {
      if (ts.isImportDeclaration(node.parent) || ts.isExportDeclaration(node.parent)) continue;
      const display = node.text.replace(/https?:\s*\/\/[^\s"'`<>]+|\bcloud\.ragflow\.io\b/g, '');
      assert.ok(!/ragflow/i.test(display), `${path}:${source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1}: old visible brand`);
    }
  });
}

for (const path of localePaths) {
  test(`${path}: all translation keys preserved and brand applied`, () => {
    const before = parse(path, original(path));
    const after = parse(path, read(path));
    same(propertyKeys(after), propertyKeys(before), `${path}: translation keys changed`);
    const oldBrandCount = nodes(before).filter(isText)
      .reduce((sum, node) => sum + (node.text.match(/RAGFlow|(?<=<i>')ragflow(?='<\/i>)/g) ?? []).length, 0);
    assert.ok(oldBrandCount > 0, `${path}: expected original display branding`);
    const countNewBrand = (source) => nodes(source).filter(isText)
      .reduce((sum, node) => sum + (node.text.match(/yFei/g) ?? []).length, 0);
    assert.equal(countNewBrand(after) - countNewBrand(before), oldBrandCount, `${path}: incomplete locale branding`);
  });
}

test('package metadata and HTML change only the description and title', () => {
  const oldPackage = JSON.parse(original('web/package.json'));
  const newPackage = JSON.parse(read('web/package.json'));
  const { description: oldDescription, ...oldRest } = oldPackage;
  const { description: newDescription, ...newRest } = newPackage;
  assert.equal(newDescription, oldDescription.replace('RAGFlow', 'yFei'));
  same(newRest, oldRest, 'package dependencies, scripts or technical metadata changed');
  const normalizeLines = (text) => text.replaceAll('\r\n', '\n');
  same(normalizeLines(read('web/index.html')), normalizeLines(original('web/index.html')).replace('<title>RAGFlow</title>', '<title>yFei</title>'), 'HTML changed beyond its title, including favicon path');
  const oldConf = JSON.parse(original('web/src/conf.json'));
  same(JSON.parse(read('web/src/conf.json')), { ...oldConf, appName: 'yFei' }, 'app configuration changed beyond appName');
});

test('storage keys, OAuth values, MCP platform and prompt protocol strings remain unchanged', () => {
  const paths = [
    'web/src/app.tsx',
    'web/src/hooks/route-hook.ts',
    'web/src/hooks/use-aimlapi-authorize.ts',
    'web/src/utils/llm-cache.ts',
    'web/src/constants/common.ts',
    'web/src/pages/agent/form/agent-form/use-build-prompt-options.ts',
    'web/src/pages/user-setting/data-source/component/google-drive-token-field.tsx',
    'web/src/pages/user-setting/data-source/component/gmail-token-field.tsx',
    'web/src/pages/user-setting/data-source/component/box-token-field.tsx',
  ];
  for (const path of paths) {
    const literals = (text) => nodes(parse(path, text)).filter(isText).map((node) => [node.kind, node.text]);
    same(literals(read(path)), literals(original(path)), `${path}: technical string changed`);
  }
});
