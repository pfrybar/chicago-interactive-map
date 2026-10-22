// Bytes and requests a phone-sized page downloads to load a view (all /builds/ traffic).
// Usage: VIEWS=loop-overview,state-and-madison [BR=1] node netstats.mjs
// BR=1 asks for Brotli, which Chrome only offers on HTTPS (the shared link). Each view loads
// twice in the same browser: the second load shows what the cache saves.
import { chromium } from 'playwright'
const APP_URL = process.env.APP_URL ?? 'http://localhost:5173'
const views = (process.env.VIEWS ?? 'loop-overview').split(',')
const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] })
for (const view of views) {
  const ctx = await browser.newContext({ viewport: { width: 400, height: 800 }, hasTouch: true, isMobile: true })
  const page = await ctx.newPage()
  const cdp = await ctx.newCDPSession(page)
  await cdp.send('Network.enable')
  if (process.env.BR) await page.setExtraHTTPHeaders({ 'Accept-Encoding': 'gzip, deflate, br' })
  let urls = new Map(), stats = { requests: 0, bytes: 0, revalidated: 0 }
  cdp.on('Network.responseReceived', (e) => {
    if (!e.response.url.includes('/builds/')) return
    urls.set(e.requestId, e.response)
    if (e.response.status === 304) stats.revalidated++
  })
  cdp.on('Network.loadingFinished', (e) => {
    if (!urls.has(e.requestId)) return
    stats.requests++
    stats.bytes += e.encodedDataLength
  })
  const t0 = Date.now()
  await page.goto(`${APP_URL}/?view=${view}&eval=1`)
  await page.waitForFunction(() => window.__cim?.ready, null, { timeout: 180000, polling: 500 })
  const encodings = new Set([...urls.values()].map((r) => r.headers['content-encoding'] ?? r.headers['Content-Encoding'] ?? 'none'))
  console.log(`${view.padEnd(20)} ${stats.requests} requests, ${(stats.bytes / 1e6).toFixed(1)} MB transferred, ` +
              `encoding ${[...encodings].join('/')}, ${((Date.now() - t0) / 1000).toFixed(0)} s to ready`)
  urls = new Map(); stats = { requests: 0, bytes: 0, revalidated: 0 }
  await page.goto(`${APP_URL}/?view=${view}&eval=1`)
  await page.waitForFunction(() => window.__cim?.ready, null, { timeout: 180000, polling: 500 })
  console.log(`${'  reload'.padEnd(20)} ${stats.requests} requests, ${(stats.bytes / 1e6).toFixed(2)} MB transferred, ` +
              `${stats.revalidated} revalidated`)
  await ctx.close()
}
await browser.close()
