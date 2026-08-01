import { existsSync, readFileSync, statSync } from 'node:fs'
import { registerHooks } from 'node:module'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const distRoot = path.join(process.cwd(), 'dist', 'node-tests')
const modulesRoot = path.join(process.cwd(), 'node_modules')

// A small number of legacy tests use the CommonJS global only to walk back to
// the Web root. Keep that compatibility while the test output itself is ESM.
globalThis.__dirname = process.cwd()

function existingModuleUrl(candidate) {
  const pathName = fileURLToPath(candidate)
  if (existsSync(pathName) && statSync(pathName).isFile()) return candidate.href
  if (existsSync(`${pathName}.js`)) return pathToFileURL(`${pathName}.js`).href
  if (existsSync(path.join(pathName, 'index.js'))) {
    return pathToFileURL(path.join(pathName, 'index.js')).href
  }
  return null
}

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith('@/')) {
      const resolved = existingModuleUrl(pathToFileURL(path.join(distRoot, specifier.slice(2))))
      if (resolved) return { url: resolved, shortCircuit: true }
    }

    if (context.parentURL && (specifier.startsWith('./') || specifier.startsWith('../'))) {
      const resolved = existingModuleUrl(new URL(specifier, context.parentURL))
      if (resolved) return { url: resolved, shortCircuit: true }
    }

    try {
      return nextResolve(specifier, context)
    } catch (error) {
      if (!specifier.startsWith('node:') && !specifier.startsWith('file:')) {
        const resolved = existingModuleUrl(pathToFileURL(path.join(modulesRoot, specifier)))
        if (resolved) return { url: resolved, shortCircuit: true }
      }
      throw error
    }
  },
  load(url, context, nextLoad) {
    if (url.endsWith('.json')) {
      const json = readFileSync(fileURLToPath(url), 'utf8')
      JSON.parse(json)
      return {
        format: 'module',
        source: `export default ${json};`,
        shortCircuit: true,
      }
    }
    return nextLoad(url, context)
  },
})
