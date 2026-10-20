// Screenshot every viewpoint in viewpoints.json and record render stats.
// Usage (from repo root): make eval   — the app must be running (make app).
// Options: VIEWS=riverwalk,grant-park  WIDTH=1280 HEIGHT=800  TIMEOUT=120000
//          UI=1     keep the panels and minimap in the screenshot (files end in -ui)
//          PHONE=1  phone-sized touch viewport (files end in -phone)
import { chromium } from 'playwright'
import { mkdir, readFile, writeFile } from 'node:fs/promises'

const APP_URL = process.env.APP_URL ?? 'http://localhost:5173'
const PHONE = process.env.PHONE === '1'
const UI = process.env.UI === '1'
const WIDTH = Number(process.env.WIDTH ?? (PHONE ? 390 : 1280))
const HEIGHT = Number(process.env.HEIGHT ?? (PHONE ? 844 : 800))
const suffix = (UI ? '-ui' : '') + (PHONE ? '-phone' : '')
const only = process.env.VIEWS?.split(',')
const { viewpoints } = JSON.parse(await readFile(new URL('./viewpoints.json', import.meta.url)))
// Budgets per viewpoint. Software rendering makes fps meaningless here, so the
// checks use counts that don't depend on the machine.
const BUDGET = { drawCalls: 1500, triangles: 3_000_000 }
const TIMEOUT = Number(process.env.TIMEOUT ?? 120_000)

const out = new URL('./out/', import.meta.url)
await mkdir(out, { recursive: true })
const browser = await chromium.launch({
  args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
})
const page = await browser.newPage({
  viewport: { width: WIDTH, height: HEIGHT },
  ...(PHONE ? { isMobile: true, hasTouch: true, deviceScaleFactor: 2 } : {}),
})
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })

const results = []
for (const vp of viewpoints) {
  if (only && !only.includes(vp.name)) continue
  const t0 = Date.now()
  await page.goto(`${APP_URL}/?view=${vp.name}${UI ? '' : '&eval=1'}`)
  let timedOut = false
  try {
    await page.waitForFunction(() => window.__cim?.ready, null, { timeout: TIMEOUT, polling: 500 })
  } catch {
    timedOut = true
  }
  const stats = await page.evaluate(() => window.__cim ?? null)
  if (!stats) {
    errors.push(`${vp.name}: app never started`)
    await page.screenshot({ path: new URL(`${vp.name}${suffix}.png`, out).pathname })
    results.push({ name: vp.name, timedOut: true, overBudget: true })
    continue
  }
  await page.screenshot({ path: new URL(`${vp.name}${suffix}.png`, out).pathname })
  const r = {
    name: vp.name,
    seconds: Math.round((Date.now() - t0) / 100) / 10,
    drawCalls: stats.render.calls,
    triangles: stats.render.triangles,
    geometries: stats.memory.geometries,
    timedOut,
    tiles: stats.tiles,
    overBudget: timedOut || stats.render.calls > BUDGET.drawCalls || stats.render.triangles > BUDGET.triangles,
  }
  results.push(r)
  console.log(`${timedOut ? 'TIME' : r.overBudget ? 'OVER' : 'ok  '} ${vp.name.padEnd(20)} ${String(r.drawCalls).padStart(5)} draws ${String(r.triangles).padStart(9)} tris  ${r.seconds}s`)
}
await browser.close()
await writeFile(new URL('summary.json', out), JSON.stringify({ app: APP_URL, results, errors }, null, 2))
if (errors.length) console.log(`${errors.length} page errors; see eval/out/summary.json`)
process.exit(results.some((r) => r.overBudget) ? 1 : 0)
