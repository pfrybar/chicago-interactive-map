// Interaction smoke test: building picking in map mode, wall collisions in walk mode.
// Usage: docker compose run --rm eval node smoke.mjs   (viewer must be running)
import { chromium } from 'playwright'

const APP_URL = process.env.APP_URL ?? 'http://localhost:5173'
const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] })
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
const ready = () => page.waitForFunction(() => window.__cim?.ready, null, { timeout: 120_000, polling: 500 })
const fail = []

// 1. Clicking Willis Tower in map mode opens the building card.
await page.goto(`${APP_URL}/?view=willis-tower`)
await ready()
await page.mouse.click(690, 300)
await page.waitForTimeout(500)
const card = await page.evaluate(() => ({
  shown: !document.getElementById('card').hidden,
  name: document.getElementById('card-name').textContent,
  rows: document.getElementById('card-rows').innerText,
}))
console.log('pick:', JSON.stringify(card))
if (!card.shown) fail.push('clicking a building did not open the card')

// 2. Walking west from State & Madison stops at the building wall.
await page.goto(`${APP_URL}/?view=state-and-madison`)
await ready()
await page.evaluate(() => {
  const { controls, stage } = window.__cimDebug
  const p = stage.camera.position.clone()
  const t = p.clone(); t.x -= 100 // face west
  controls.setView('walk', p, t)
})
// Software rendering is slow and frame time is capped, so hold Shift (fast walk).
await page.keyboard.down('ShiftLeft')
await page.keyboard.down('KeyW')
const xs = []
for (let i = 0; i < 6; i++) {
  await page.waitForTimeout(2500)
  xs.push(await page.evaluate(() => window.__cim.camera.position[0]))
}
await page.keyboard.up('KeyW')
await page.keyboard.up('ShiftLeft')
console.log('walk west x:', xs.map((x) => x.toFixed(2)).join(' → '))
if (!(xs[0] < 4)) fail.push('walking did not move the camera')
if (Math.abs(xs[5] - xs[4]) > 0.05) fail.push('walker did not stop at the wall')
if (xs[5] < -40) fail.push('walker went through the building')

await browser.close()
console.log(fail.length ? `FAILED: ${fail.join('; ')}` : 'ok')
process.exit(fail.length ? 1 : 0)
