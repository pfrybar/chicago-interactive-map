// Interaction smoke test: building picking in map mode, wall collisions in walk mode.
// Usage: docker compose run --rm eval node smoke.mjs   (viewer must be running)
import { chromium } from 'playwright'

const APP_URL = process.env.APP_URL ?? 'http://localhost:5173'
const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] })
let page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
page.setDefaultNavigationTimeout(120_000)
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
await page.close()
page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
page.setDefaultNavigationTimeout(120_000)
await page.goto(`${APP_URL}/?view=state-and-madison`)
await ready()
await page.evaluate(() => {
  const { controls, stage } = window.__cimDebug
  const p = stage.camera.position.clone()
  const t = p.clone(); t.x -= 100 // face west
  controls.setView('walk', p, t)
})
// Software rendering is slow and frame time is capped, so hold Shift (fast walk)
// and keep going until the walker stops (or 60 s pass).
await page.keyboard.down('ShiftLeft')
await page.keyboard.down('KeyW')
const xs = []
for (let i = 0; i < 30; i++) {
  await page.waitForTimeout(2000)
  xs.push(await page.evaluate(() => window.__cim.camera.position[0]))
  const n = xs.length
  if (n >= 3 && Math.abs(xs[n - 1] - xs[n - 3]) < 0.05) break
}
await page.keyboard.up('KeyW')
await page.keyboard.up('ShiftLeft')
console.log('walk west x:', xs.map((x) => x.toFixed(2)).join(' → '))
if (!(xs[0] < 4)) fail.push('walking did not move the camera')
const last = xs[xs.length - 1]
if (Math.abs(last - xs[xs.length - 3]) >= 0.05) fail.push('walker did not stop at the wall')
if (last < -40) fail.push('walker went through the building')

// 3. Phone gestures in map mode: twist turns about the point between the fingers, two
// fingers sliding up tilt toward the horizon, spreading zooms in, the compass faces north.
await page.close()
const phone = await browser.newContext({ viewport: { width: 400, height: 800 }, hasTouch: true, isMobile: true })
page = await phone.newPage()
page.setDefaultNavigationTimeout(120_000)
await page.goto(`${APP_URL}/?view=loop-overview`)
await ready()
const cdp = await phone.newCDPSession(page)
// Each touch gets a timestamp `step` seconds after the last, as on a phone (~60 Hz). Software
// rendering delivers events hundreds of milliseconds apart, which would hide momentum.
let clock = Date.now() / 1000
const touch = (type, pts, step = 0.016) => cdp.send('Input.dispatchTouchEvent', {
  type, timestamp: (clock += step),
  touchPoints: pts.map(([x, y], id) => ({ x, y, id, radiusX: 4, radiusY: 4, force: 1 })),
})
// Two fingers moved in steps from `a` to `b` (each a pair of [x, y]).
async function gesture(a, b, steps = 12) {
  await touch('touchStart', a)
  for (let i = 1; i <= steps; i++) {
    const t = i / steps
    await touch('touchMove', a.map(([x, y], k) => [x + (b[k][0] - x) * t, y + (b[k][1] - y) * t]))
    await page.waitForTimeout(40)
  }
  await touch('touchEnd', [])
  await page.waitForTimeout(300)
}
const state = () => page.evaluate(() => {
  const { controls, stage } = window.__cimDebug
  const c = stage.camera, t = controls.map.target
  const off = c.position.clone().sub(t)
  return { heading: controls.heading(), dist: off.length(), polar: Math.acos(off.y / off.length()),
           compass: !document.getElementById('compass').hidden }
})
// Screen position of the world point that was under the screen point (x, y) when `mark` ran.
await page.evaluate(() => {
  window.__mark = (x, y) => {
    const { stage, controls } = window.__cimDebug
    const ray = new controls.ray.constructor()
    ray.setFromCamera({ x: (x / innerWidth) * 2 - 1, y: -(y / innerHeight) * 2 + 1 }, stage.camera)
    const hits = ray.intersectObjects([...window.__cimDebug.city.buildingMeshes, ...window.__cimDebug.city.groundMeshes], false)
    window.__marked = hits[0]?.point.clone() ?? null
  }
  window.__where = () => {
    const p = window.__marked?.clone().project(window.__cimDebug.stage.camera)
    return p ? [(p.x + 1) / 2 * innerWidth, (1 - p.y) / 2 * innerHeight] : null
  }
})
const c = [200, 450], r = 90
const ring = (deg) => [[c[0] - r * Math.cos(deg * Math.PI / 180), c[1] - r * Math.sin(deg * Math.PI / 180)],
                       [c[0] + r * Math.cos(deg * Math.PI / 180), c[1] + r * Math.sin(deg * Math.PI / 180)]]
const s0 = await state()
await page.evaluate(([x, y]) => window.__mark(x, y), c)
await gesture(ring(0), ring(40))
const s1 = await state()
const at = await page.evaluate(() => window.__where())
const turned = ((s1.heading - s0.heading) * 180) / Math.PI
const drift = at ? Math.hypot(at[0] - c[0], at[1] - c[1]) : Infinity
console.log(`twist 40°: heading ${turned.toFixed(1)}°, pivot moved ${drift.toFixed(1)} px, compass ${s1.compass}`)
// Twisting clockwise turns the map clockwise with the fingers, so the view's heading drops.
if (Math.abs(turned + 40) > 8) fail.push('twisting two fingers clockwise did not turn the map clockwise by about the twist angle')
if (drift > 25) fail.push('the point between the fingers did not stay put while turning')
if (!s1.compass) fail.push('the compass did not appear after turning')
await gesture([[150, 600], [250, 600]], [[150, 420], [250, 420]])
const s2 = await state()
console.log(`tilt: polar ${(s1.polar * 57.3).toFixed(1)}° -> ${(s2.polar * 57.3).toFixed(1)}°`)
if (!(s2.polar > s1.polar + 0.1)) fail.push('sliding two fingers up did not tilt toward the horizon')
// Fully tilted, a one-finger drag along the top edge (near the horizon) must not fling
// the map out of the city.
const before = await page.evaluate(() => window.__cimDebug.controls.map.target.clone())
// (320, 250) is clear of the panel and the compass; at full tilt the horizon is near y = 310.
await touch('touchStart', [[320, 250]])
for (let i = 1; i <= 10; i++) { await touch('touchMove', [[320, 250 + 9 * i]]); await page.waitForTimeout(40) }
await touch('touchEnd', [])
await page.waitForTimeout(300)
const flung = await page.evaluate((b) => window.__cimDebug.controls.map.target.distanceTo(b), before)
console.log(`drag near the horizon: target moved ${flung.toFixed(0)} m (view distance ${s2.dist.toFixed(0)} m)`)
if (flung > 4 * s2.dist) fail.push('dragging near the horizon flung the map')
if (flung < 20) fail.push('dragging near the horizon did not move the map at all')
await gesture([[170, 450], [230, 450]], [[110, 450], [290, 450]])
const s3 = await state()
console.log(`pinch out: distance ${s2.dist.toFixed(0)} -> ${s3.dist.toFixed(0)} m`)
if (!(s3.dist < s2.dist * 0.7)) fail.push('spreading two fingers did not zoom in')
// Momentum: a quick flick keeps the map gliding after the finger lifts; a drag that
// stops before lifting doesn't; touching the screen catches a glide.
const target = () => page.evaluate(() => window.__cimDebug.controls.map.target.clone())
const away = (a, b) => Math.hypot(a.x - b.x, a.z - b.z)
async function drag(points, holdMs = 0) {
  await touch('touchStart', [points[0]])
  for (const pt of points.slice(1)) await touch('touchMove', [pt])
  await touch('touchEnd', [], 0.016 + holdMs / 1000)
}
let t0 = await target()
await drag([[200, 560], [200, 530], [200, 500], [200, 470], [200, 440]])
const lifted = await target()
await page.waitForTimeout(2000)
const glided = away(await target(), lifted), dragged = away(lifted, t0)
console.log(`flick: dragged ${dragged.toFixed(0)} m, then glided ${glided.toFixed(0)} m`)
if (!(glided > 0.2 * dragged)) fail.push('a flick did not keep the map moving after release')
t0 = await target()
await drag([[200, 560], [200, 530], [200, 500], [200, 470], [200, 440]], 250)
const held = await target()
await page.waitForTimeout(1500)
const crept = away(await target(), held)
console.log(`drag then hold: moved ${crept.toFixed(1)} m after release`)
if (crept > 1) fail.push('a drag that stopped before lifting still glided')
await drag([[200, 560], [200, 520], [200, 480], [200, 440]])
await page.waitForTimeout(150)
await touch('touchStart', [[200, 500]], 0.15)
const caught = await target()
await page.waitForTimeout(800)
const slid = away(await target(), caught)
await touch('touchEnd', [])
console.log(`touch during a glide: moved ${slid.toFixed(1)} m while held`)
if (slid > 1) fail.push('touching the screen did not stop a glide')

const box = await page.locator('#compass').boundingBox()
await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2)
await page.waitForTimeout(2500)
const s4 = await state()
console.log(`compass tap: heading ${(s4.heading * 57.3).toFixed(1)}°, compass shown ${s4.compass}`)
if (Math.abs(s4.heading) > 0.02) fail.push('tapping the compass did not turn the map to north')

await browser.close()
console.log(fail.length ? `FAILED: ${fail.join('; ')}` : 'ok')
process.exit(fail.length ? 1 : 0)
