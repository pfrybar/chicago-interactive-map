// Brotli-compress a build's tiles and JSON once, into <file>.br beside each file, so the
// shared link serves a quarter of the raw size without compressing per request.
// Usage: node scripts/precompress.mjs ../data/builds/<build>   (skips files already done)
import fs from 'node:fs'
import path from 'node:path'
import zlib from 'node:zlib'
import { promisify } from 'node:util'

const brotli = promisify(zlib.brotliCompress)
const root = process.argv[2]
if (!root) throw new Error('usage: precompress.mjs <build directory>')
const files = []
for (const entry of fs.readdirSync(root, { recursive: true, withFileTypes: true })) {
  const f = path.join(entry.parentPath ?? entry.path, entry.name)
  if (entry.isFile() && /\.(glb|json)$/.test(f)) files.push(f)
}
let done = 0, raw = 0, packed = 0, skipped = 0
const t0 = Date.now()
async function one(f) {
  const st = fs.statSync(f), br = fs.statSync(`${f}.br`, { throwIfNoEntry: false })
  if (br && br.mtimeMs >= st.mtimeMs) { skipped++; return }
  const out = await brotli(fs.readFileSync(f), { params: { [zlib.constants.BROTLI_PARAM_QUALITY]: 11 } })
  fs.writeFileSync(`${f}.br.tmp`, out)
  fs.renameSync(`${f}.br.tmp`, `${f}.br`)
  raw += st.size; packed += out.length
}
const queue = [...files]
await Promise.all(Array.from({ length: Number(process.env.UV_THREADPOOL_SIZE ?? 4) }, async () => {
  while (queue.length) {
    await one(queue.pop())
    if (++done % 500 === 0) console.log(`  ${done}/${files.length} files, ${((Date.now() - t0) / 1000).toFixed(0)} s`)
  }
}))
console.log(`precompressed ${files.length - skipped} of ${files.length} files: ${(raw / 1e6).toFixed(0)} MB -> ` +
            `${(packed / 1e6).toFixed(0)} MB in ${((Date.now() - t0) / 1000).toFixed(0)} s (${skipped} already done)`)
