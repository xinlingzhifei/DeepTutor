'use strict';
const fs = require('node:fs');
const path = require('node:path');
const bundled = path.join(__dirname, 'provider-reader.cjs');
const source = path.join(__dirname, '../overlay/lib/yfeistai/provider-reader.cjs');
try {
  const { readConfiguration } = require(fs.existsSync(bundled) ? bundled : source);
  const config = readConfiguration();
  if (process.env.DEFAULT_MODEL && (!config || process.env.DEFAULT_MODEL !== config.defaultModel)) {
    throw new Error('invalid');
  }
  if (config) process.env.DEFAULT_MODEL = config.defaultModel;
} catch {
  process.stderr.write('YFEISTAI_PROVIDER_CONFIG_INVALID\n');
  process.exit(1);
}
