'use strict';

// Fixed OpenMAIC reads this same JSON (a YAML subset) from its working directory.
// No credentials are copied to process.env, requests or logs.
const fs = require('node:fs');
const path = require('node:path');
const { TextDecoder } = require('node:util');

const PROVIDERS = new Set([
  'openai', 'azure', 'anthropic', 'google', 'glm', 'qwen', 'deepseek', 'kimi',
  'minimax', 'siliconflow', 'doubao', 'openrouter', 'grok', 'tencent-hunyuan',
  'xiaomi', 'ollama', 'lemonade',
]);
const identifier = /^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/;
const modelIdentifier = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/;

function requireValid(condition) {
  if (!condition) throw new Error('invalid');
}

function exactKeys(value, keys) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === keys.length
    && keys.every((key) => Object.hasOwn(value, key));
}

function parseUnambiguousJson(raw) {
  const parsed = JSON.parse(raw);
  // JSON.parse accepts duplicate keys whereas the upstream YAML loader rejects
  // them. Tokenize already-valid JSON, so strings cannot smuggle punctuation.
  const tokens = raw.match(/"(?:[^"\\]|\\.)*"|[{}[\]:,]/gs) || [];
  const objects = [];
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index];
    if (token === '{' || token === '[') objects.push(token === '{' ? new Set() : null);
    else if (token === '}' || token === ']') objects.pop();
    else if (token.startsWith('"') && tokens[index + 1] === ':') {
      const key = JSON.parse(token);
      const keys = objects[objects.length - 1];
      requireValid(keys && !keys.has(key));
      keys.add(key);
    }
  }
  return parsed;
}

function validBaseUrl(value) {
  requireValid(typeof value === 'string' && value.length <= 512
    && !/[^\x21-\x7e]|[\\?#]/.test(value) && !value.endsWith('/'));
  const raw = /^https?:\/\/[^/?#]+(\/[^?#]*)?$/.exec(value);
  requireValid(raw);
  const url = new URL(value);
  requireValid(url.hostname && !url.username && !url.password
    && url.port !== '0' && !value.split('/')[2].endsWith(':'));
  const decodedPath = decodeURIComponent(raw[1] || '');
  requireValid(!/[\x00-\x20\x7f\\]/.test(decodedPath)
    && !decodedPath.split('/').some((part) => part === '.' || part === '..'));
}

function bootstrap() {
  const env = process.env;
  requireValid(!env.MODEL_ROUTES);
  for (const [name, value] of Object.entries(env)) {
    requireValid(!value || (!/_(API_KEY|BASE_URL|MODELS)$/.test(name)
      && !name.startsWith('ALIDOCMIND_ACCESS_KEY_')));
  }
  const filename = process.env.YFEISTAI_OPENMAIC_PROVIDER_CONFIG_FILE
    || path.join(process.cwd(), 'server-providers.yml');
  if (!fs.existsSync(filename)) {
    requireValid(process.env.YFEISTAI_OPENMAIC_MODE === 'shared'
      && process.env.YFEISTAI_OPENMAIC_PROVIDER_PROFILE_ID === 'platform-default'
      && !process.env.YFEISTAI_DATA_PLANE_ID);
    // Do not accept a dangling symlink as an unconfigured runtime.
    try { fs.lstatSync(filename); throw new Error('invalid'); }
    catch (error) { requireValid(error.code === 'ENOENT'); }
    return null;
  }
  const before = fs.lstatSync(filename);
  requireValid(before.isFile() && !before.isSymbolicLink() && before.size <= 65536);
  if (process.platform !== 'win32') {
    requireValid(process.getuid() === 1000 && process.getgid() === 1000
      && before.uid === 1000 && before.gid === 1000
      && [0o400, 0o600].includes(before.mode & 0o7777));
  }
  // Keep the original handle through read/JSON parsing; reject path replacement.
  const handle = fs.openSync(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  let config;
  try {
    const opened = fs.fstatSync(handle);
    requireValid(opened.dev === before.dev && opened.ino === before.ino
      && opened.mode === before.mode && opened.size === before.size
      && opened.uid === before.uid && opened.gid === before.gid);
    const bytes = fs.readFileSync(handle);
    requireValid(bytes.length <= 65536
      && !(bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf));
    config = parseUnambiguousJson(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
    const after = fs.lstatSync(filename);
    requireValid(after.dev === opened.dev && after.ino === opened.ino
      && after.size === opened.size && after.mtimeMs === opened.mtimeMs
      && after.mode === opened.mode && after.uid === opened.uid && after.gid === opened.gid);
  } finally {
    fs.closeSync(handle);
  }
  requireValid(exactKeys(config, ['scope', 'tenantId', 'profileId', 'defaultModel', 'providers']));
  requireValid(['shared', 'dedicated'].includes(config.scope)
    && config.scope === env.YFEISTAI_OPENMAIC_MODE
    && typeof config.profileId === 'string' && identifier.test(config.profileId)
    && config.profileId === env.YFEISTAI_OPENMAIC_PROVIDER_PROFILE_ID);
  if (config.scope === 'shared') {
    requireValid(config.tenantId === null && config.profileId === 'platform-default'
      && !env.YFEISTAI_DATA_PLANE_ID);
  } else {
    requireValid(typeof config.tenantId === 'string' && identifier.test(config.tenantId)
      && config.tenantId === env.YFEISTAI_DATA_PLANE_ID);
  }
  if (config.scope === 'shared' && config.defaultModel === null
      && exactKeys(config.providers, [])) return null;
  requireValid(typeof config.defaultModel === 'string');
  const colon = config.defaultModel.indexOf(':');
  const providerId = config.defaultModel.slice(0, colon);
  const modelId = config.defaultModel.slice(colon + 1);
  requireValid(colon > 0 && PROVIDERS.has(providerId) && modelIdentifier.test(modelId)
    && exactKeys(config.providers, [providerId]));
  const provider = config.providers[providerId];
  requireValid(exactKeys(provider, ['apiKey', 'baseUrl'])
    && typeof provider.apiKey === 'string' && provider.apiKey === provider.apiKey.trim()
    && !/^\u0085|\u0085$/.test(provider.apiKey)
    && !/[\x00-\x1f\x7f]/.test(provider.apiKey)
    && (provider.apiKey || ['ollama', 'lemonade'].includes(providerId)));
  validBaseUrl(provider.baseUrl);
  return config;
}

function readConfiguration() {
  try { return bootstrap(); }
  catch { throw new Error('YFEISTAI_PROVIDER_CONFIG_INVALID'); }
}

module.exports = { readConfiguration };
