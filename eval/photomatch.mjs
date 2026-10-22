// Render the map from a street photo's camera pose (written by `python -m cim photomatch-pose`).
// Usage: NAME=mapillary_123 node photomatch.mjs   (see ./dev photomatch)
import { readFile } from 'node:fs/promises'
import { chromium } from 'playwright'

const APP_URL = process.env.APP_URL ?? 'http://localhost:5173'
const name = process.env.NAME
const dir = new URL('../data/work/photomatch/', import.meta.url)
const pose = JSON.parse(await readFile(new URL(`${name}.json`, dir)))
const width = 1280, height = Math.round((width * pose.height) / pose.width)
const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] })
const page = await browser.newPage({ viewport: { width, height } })
page.setDefaultNavigationTimeout(120_000)
await page.goto(`${APP_URL}/?view=state-and-madison&eval=1`)
await page.waitForFunction(() => window.__cim?.ready, null, { timeout: 180_000, polling: 500 })
await page.evaluate((p) => {
  const { controls, stage } = window.__cimDebug
  const V = stage.camera.position.constructor
  const h = (p.heading * Math.PI) / 180, t = (p.pitch * Math.PI) / 180
  // Local (east, north, up) to the viewer's world (east, up, -north); walk mode settles at eye height.
  const from = new V(p.x, 60, -p.y)
  const to = from.clone().add(new V(Math.sin(h) * Math.cos(t), Math.sin(t), -Math.cos(h) * Math.cos(t)).multiplyScalar(100))
  controls.setView('walk', from, to)
  stage.camera.fov = p.vfov
  stage.camera.updateProjectionMatrix()
}, pose)
await page.waitForTimeout(1500)  // let the move register before waiting for the new tiles
await page.waitForFunction(() => window.__cim?.ready, null, { timeout: 180_000, polling: 500 })
await page.screenshot({ path: new URL(`${name}_render.png`, dir).pathname, timeout: 180_000 })
await browser.close()
console.log(`rendered ${name}`)
