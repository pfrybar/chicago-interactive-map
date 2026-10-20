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

/**
 * Serves /builds/* straight from ../data/builds in both `vite` and `vite preview`,
 * gzipped when the browser accepts it (tiles shrink to about a third). Keeping the
 * builds out of public/ stops `vite build` from copying a gigabyte of tiles into dist/.
 */
function serveBuilds(): Plugin {
  const handler = (req: IncomingMessage, res: ServerResponse, next: () => void) => {
    if (!req.url?.startsWith('/builds/')) return next()
    const rel = decodeURIComponent(req.url.slice('/builds/'.length).split('?')[0])
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
      // Builds are rebuilt in place during development, so let browsers revalidate.
      res.setHeader('Cache-Control', 'no-cache')
      res.setHeader('Last-Modified', st.mtime.toUTCString())
      if (req.headers['if-modified-since'] && new Date(req.headers['if-modified-since']) >= new Date(st.mtime.toUTCString())) {
        res.statusCode = 304
        return res.end()
      }
      const gzip = ext !== '.webp' && /\bgzip\b/.test(String(req.headers['accept-encoding'] ?? ''))
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
