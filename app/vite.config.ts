import fs from 'node:fs'
import path from 'node:path'
import zlib from 'node:zlib'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { defineConfig, type Plugin } from 'vite'

const BUILDS = path.resolve(__dirname, '../data/builds')
const TYPES: Record<string, string> = {
  '.json': 'application/json',
  '.glb': 'model/gltf-binary',
  '.webp': 'image/webp',
}

const BROTLI = { params: { [zlib.constants.BROTLI_PARAM_QUALITY]: 11 } }
const compressing = new Set<string>()

/** Write `file`.br in the background (once at a time per file); later requests serve it. */
function brotliLater(file: string): void {
  if (compressing.has(file)) return
  compressing.add(file)
  fs.readFile(file, (err, data) => {
    if (err) return compressing.delete(file)
    zlib.brotliCompress(data, BROTLI, (err2, out) => {
      if (err2) return compressing.delete(file)
      const tmp = `${file}.br.${process.pid}.tmp`
      fs.writeFile(tmp, out, () => fs.rename(tmp, `${file}.br`, () => compressing.delete(file)))
    })
  })
}

/**
 * Serves /builds/* straight from ../data/builds in both `vite` and `vite preview`.
 * Keeping the builds out of public/ stops `vite build` from copying a gigabyte of tiles
 * into dist/.
 *
 * - Brotli when the browser accepts it (HTTPS only, so the shared link): a quarter of the
 *   raw size, against 44% for gzip. Compressed once into `<file>.br` beside the tile
 *   (`./dev precompress`, or on first request), never per request.
 * - Otherwise gzip on the fly (the local dev server and the eval are plain HTTP).
 * - Tile URLs carry the build's id (`?v=`, see city.ts), so they can be cached for good:
 *   a rebuild changes the id. Anything without it is revalidated.
 */
function serveBuilds(): Plugin {
  const handler = (req: IncomingMessage, res: ServerResponse, next: () => void) => {
    if (!req.url?.startsWith('/builds/')) return next()
    const [pathPart, query = ''] = req.url.slice('/builds/'.length).split('?')
    const rel = decodeURIComponent(pathPart)
    const versioned = new URLSearchParams(query).has('v')
    const file = path.resolve(BUILDS, rel)
    if (!file.startsWith(BUILDS + path.sep)) {
      res.statusCode = 403
      return res.end()
    }
    fs.stat(file, (err, st) => {
      if (err || !st.isFile()) {
        res.statusCode = 404
        return res.end()
      }
      const ext = path.extname(file)
      res.setHeader('Content-Type', TYPES[ext] ?? 'application/octet-stream')
      // Unversioned files (manifest, build info) are rebuilt in place: revalidate those.
      res.setHeader('Cache-Control', versioned ? 'public, max-age=31536000, immutable' : 'no-cache')
      res.setHeader('Last-Modified', st.mtime.toUTCString())
      if (req.headers['if-modified-since'] && new Date(req.headers['if-modified-since']) >= new Date(st.mtime.toUTCString())) {
        res.statusCode = 304
        return res.end()
      }
      const accepts = String(req.headers['accept-encoding'] ?? '')
      const compressible = ext !== '.webp'
      if (compressible && /\bbr\b/.test(accepts)) {
        const br = fs.statSync(`${file}.br`, { throwIfNoEntry: false })
        if (br && br.mtimeMs >= st.mtimeMs) {
          res.setHeader('Content-Encoding', 'br')
          res.setHeader('Vary', 'Accept-Encoding')
          res.setHeader('Content-Length', br.size)
          return fs.createReadStream(`${file}.br`).pipe(res)
        }
        brotliLater(file)  // this time gzip, next time Brotli
      }
      const gzip = compressible && /\bgzip\b/.test(accepts)
      if (gzip) {
        res.setHeader('Content-Encoding', 'gzip')
        res.setHeader('Vary', 'Accept-Encoding')
        fs.createReadStream(file).pipe(zlib.createGzip({ level: 6 })).pipe(res)
      } else {
        res.setHeader('Content-Length', st.size)
        fs.createReadStream(file).pipe(res)
      }
    })
  }
  return {
    name: 'serve-builds',
    configureServer: (server) => { server.middlewares.use(handler) },
    configurePreviewServer: (server) => { server.middlewares.use(handler) },
  }
}

export default defineConfig({
  plugins: [serveBuilds()],
  server: {
    port: 5173,
    allowedHosts: ['web'], // the eval container reaches the dev server by its compose name
    fs: { allow: ['..'] }, // the app imports ../eval/viewpoints.json
  },
  preview: {
    port: 4173,
    allowedHosts: ['.trycloudflare.com'], // ./dev share
  },
  build: { target: 'es2022' },
})
