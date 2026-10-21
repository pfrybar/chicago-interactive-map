import { Clock, Mesh, MeshBasicMaterial, Raycaster, Vector2, Vector3 } from 'three'
import viewpointFile from '../../eval/viewpoints.json'
import { City, frameMatrix, loadBuild, type Feature } from './city'
import { Controls, type Mode } from './controls'
import { headingOf, toLocal, toWorld } from './frame'
import { Minimap, areaAt } from './minimap'
import { createStage, type Stage } from './scene'

interface Viewpoint { name: string; mode: Mode; position: number[]; target: number[]; note?: string }
const viewpoints = viewpointFile.viewpoints as Viewpoint[]

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T
const params = new URLSearchParams(location.search)
const evalMode = params.has('eval')
if (evalMode) document.body.classList.add('eval')

const HEIGHT_SOURCES: Record<string, string> = {
  lidar: 'measured by USGS lidar, 2017',
  override: 'corrected after review',
  'osm:height': 'OpenStreetMap height',
  'osm:levels': 'from OpenStreetMap floor count',
  'city:stories': 'from City of Chicago floor count',
  default: 'estimate, no height data yet',
}
const HELP: Record<Mode, string> = {
  map: 'Drag to pan. Right-drag or two fingers to tilt and turn. Scroll or pinch to zoom. Click a building for details.',
  walk: 'W A S D or arrow keys to walk, hold Shift to go faster. Click the view to look around with the mouse; Esc releases it.',
}
const HELP_TOUCH = 'Use the left circle to walk. Drag anywhere else to look around.'
const HELP_TOUCH_MAP = 'Drag to move. Pinch to zoom, twist to turn, slide two fingers up or down to tilt. Tap a building for details.'

async function start() {
  const canvas = $<HTMLCanvasElement>('view')
  let stage: Stage
  try {
    stage = createStage(canvas)
  } catch {
    showError('This map needs WebGL 2. Try a current version of Chrome, Safari or Firefox.')
    return
  }
  const { renderer, scene, camera } = stage
  const build = await loadBuild().catch(() => null)
  if (!build) {
    showError('No map build found. Run "make build" first, then reload this page.')
    return
  }

  const city = new City(build, camera, renderer, frameMatrix(build.frame.enuToEcef))
  scene.add(city.tiles.group)
  const controls = new Controls(camera, canvas, () => City.shown(city.buildingMeshes), () => City.shown(city.groundMeshes))
  // For debugging from the browser console or the eval harness.
  ;(window as any).__cimDebug = { stage, city, controls }

  // Panel text
  const areaCount = build.report.areas.length
  const areaText = areaCount === 1 ? build.report.areas[0].name : `${areaCount} community areas`
  $('build-info').textContent = `${areaText} · ${build.report.buildings.toLocaleString()} buildings`
  $('attribution').innerHTML = build.attribution
    .map((a) => `<a href="${a.url}" target="_blank" rel="noopener">${a.text}</a>`).join(' · ')

  // Modes
  const touch = matchMedia('(pointer: coarse)').matches
  controls.onModeChange = (m) => {
    $('mode-map').setAttribute('aria-pressed', String(m === 'map'))
    $('mode-walk').setAttribute('aria-pressed', String(m === 'walk'))
    $('help').textContent = touch ? (m === 'walk' ? HELP_TOUCH : HELP_TOUCH_MAP) : HELP[m]
    $('stick').hidden = !(m === 'walk' && touch) || evalMode
    document.body.classList.toggle('walking', m === 'walk')
  }
  $('mode-map').addEventListener('click', () => controls.setMode('map'))
  $('mode-walk').addEventListener('click', () => controls.setMode('walk'))
  setupJoystick(controls)

  // Shadows
  const shadowBox = $<HTMLInputElement>('shadows')
  // Off by default on phones and tablets; ?shadows=0|1 overrides.
  shadowBox.checked = params.has('shadows') ? params.get('shadows') === '1' : !touch
  stage.setShadows(shadowBox.checked)
  shadowBox.addEventListener('change', () => stage.setShadows(shadowBox.checked))

  // Initial view: ?view=<name> from eval/viewpoints.json, else the first one.
  const vp = viewpoints.find((v) => v.name === params.get('view')) ?? viewpoints[0]
  controls.setView(vp.mode, toWorld(vp.position), toWorld(vp.target))
  controls.onModeChange(controls.mode)

  // Minimap and community areas
  const minimap = new Minimap(build.overview, build.base, $('minimap-host'))
  minimap.onPick = (x, y) => controls.jumpTo(toWorld([x, y, 0]))
  const select = $<HTMLSelectElement>('area-select')
  for (const a of [...build.overview.areas].sort((p, q) => p.name.localeCompare(q.name))) {
    select.add(new Option(`${a.name} (${a.number})`, String(a.number)))
  }
  select.addEventListener('change', () => {
    const a = build.overview.areas.find((x) => String(x.number) === select.value)
    if (a) controls.jumpTo(toWorld([a.center[0], a.center[1], 0]))
    select.value = ''
    select.blur() // so W A S D go to walking, not the list
  })
  let currentArea: ReturnType<typeof areaAt> = null

  // Picking
  const highlightMat = new MeshBasicMaterial({ color: 0x41b6e6, transparent: true, opacity: 0.55, depthWrite: false })
  highlightMat.polygonOffset = true
  highlightMat.polygonOffsetFactor = -2
  highlightMat.polygonOffsetUnits = -2
  let highlight: Mesh | null = null
  const raycaster = new Raycaster()
  raycaster.firstHitOnly = true
  const down = new Vector2()
  canvas.addEventListener('pointerdown', (e) => down.set(e.clientX, e.clientY))
  canvas.addEventListener('pointerup', (e) => {
    if (controls.mode !== 'map' || down.distanceTo(new Vector2(e.clientX, e.clientY)) > 5) return
    const ndc = new Vector2((e.clientX / innerWidth) * 2 - 1, -(e.clientY / innerHeight) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    const hit = raycaster.intersectObjects(City.shown(city.buildingMeshes), false)[0]
    clearHighlight()
    if (!hit?.face) return showCard(null)
    const found = City.featureAt(hit.object as Mesh, hit.face.a)
    if (!found) return showCard(null)
    highlight = new Mesh(City.extractFeature(hit.object as Mesh, found.index), highlightMat)
    hit.object.add(highlight)
    showCard(found.feature)
  })
  $('card-close').addEventListener('click', () => { clearHighlight(); showCard(null) })
  function clearHighlight() {
    if (!highlight) return
    highlight.removeFromParent()
    highlight.geometry.dispose()
    highlight = null
  }

  // Resize
  const resize = () => {
    renderer.setSize(innerWidth, innerHeight, false)
    camera.aspect = innerWidth / innerHeight
    camera.updateProjectionMatrix()
    city.tiles.setResolutionFromRenderer(camera, renderer)
  }
  addEventListener('resize', resize)
  resize()

  // Loop
  const clock = new Clock()
  const fwd = new Vector3()
  const focus = new Vector3()
  let frames = 0, fpsTime = 0, fps = 0, readyFrames = 0
  const compass = setupCompass(controls, evalMode)
  renderer.setAnimationLoop(() => {
    const dt = Math.min(clock.getDelta(), 0.1)
    controls.update(dt)
    compass(dt)
    camera.updateMatrixWorld()
    city.update(camera)
    controls.focus(focus)
    stage.followCamera()
    stage.followShadows(focus, Math.max(camera.position.distanceTo(focus) * 1.6, 300))
    renderer.render(scene, camera)

    const p = toLocal(camera.position)
    camera.getWorldDirection(fwd)
    const fl = toLocal(focus)
    const area = areaAt(build.overview.areas, fl[0], fl[1])
    if (area !== currentArea) {
      currentArea = area
      $('where').innerHTML = area ? `${escapeHtml(area.name)}<small>area ${area.number}</small>` : ''
    }
    const span = controls.mode === 'walk' ? 700 : Math.min(Math.max(p[2] * 4, 1200), 16000)
    minimap.draw(p[0], p[1], headingOf(fwd), span, area)

    const progress = city.tiles.loadProgress
    $('loading').hidden = progress >= 1
    $('loading-pct').textContent = `${Math.round(progress * 100)}%`
    readyFrames = city.ready && progress >= 1 ? readyFrames + 1 : 0

    frames++; fpsTime += dt
    if (fpsTime >= 1) { fps = frames / fpsTime; frames = 0; fpsTime = 0 }
    ;(window as any).__cim = {
      ready: readyFrames > 30,
      fps,
      render: { ...renderer.info.render },
      memory: { ...renderer.info.memory },
      tiles: { ...(city.tiles as unknown as { stats: object }).stats },
      camera: { position: p, mode: controls.mode },
    }
  })
}

function showCard(f: Feature | null) {
  const card = $('card')
  card.hidden = !f
  if (!f) return
  $('card-name').textContent = f.name ?? 'Unnamed building'
  const rows: [string, string][] = [
    ['Height', `${f.h.toFixed(0)} m / ${(f.h / 0.3048).toFixed(0)} ft <small>${HEIGHT_SOURCES[f.src] ?? f.src}</small>`],
  ]
  if (f.prev) rows.push(['Before lidar', `${f.prev[0].toFixed(0)} m <small>${HEIGHT_SOURCES[f.prev[1]] ?? f.prev[1]}</small>`])
  rows.push(['Roof', f.lod === 2 ? 'Shape from lidar <small>roofer LoD 2.2</small>' : 'Flat block'])
  if (f.minH) rows.push(['Starts at', `${f.minH.toFixed(0)} m above ground`])
  if (f.year) rows.push(['Built', String(f.year)])
  const osm = f.osm ? `<a href="https://www.openstreetmap.org/${f.osm[0] === 'w' ? 'way' : 'relation'}/${f.osm.slice(1)}" target="_blank" rel="noopener">OpenStreetMap ${f.osm}</a>` : ''
  rows.push(['ID', `${escapeHtml(f.id)}${osm ? '<br>' + osm : ''}`])
  $('card-rows').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')
}

function setupJoystick(controls: Controls) {
  const stick = $('stick')
  const knob = $('knob')
  let id: number | null = null
  const move = (e: PointerEvent) => {
    const r = stick.getBoundingClientRect()
    const R = r.width / 2
    let dx = e.clientX - (r.left + R), dy = e.clientY - (r.top + R)
    const len = Math.hypot(dx, dy)
    if (len > R) { dx *= R / len; dy *= R / len }
    knob.style.transform = `translate(${dx}px, ${dy}px)`
    controls.setStick(dx / R, -dy / R)
  }
  stick.addEventListener('pointerdown', (e) => { id = e.pointerId; stick.setPointerCapture(id); move(e); e.stopPropagation() })
  stick.addEventListener('pointermove', (e) => { if (e.pointerId === id) move(e) })
  const end = (e: PointerEvent) => {
    if (e.pointerId !== id) return
    id = null
    knob.style.transform = ''
    controls.setStick(0, 0)
  }
  stick.addEventListener('pointerup', end)
  stick.addEventListener('pointercancel', end)
}

function escapeHtml(s: string) {
  return s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`)
}

function showError(msg: string) {
  const el = $('error')
  el.textContent = msg
  el.hidden = false
}

start()

/** The compass: points north, shows only when the map is turned. Tap to face north, drag to turn. */
function setupCompass(controls: Controls, evalMode: boolean): (dt: number) => void {
  const button = $<HTMLButtonElement>('compass')
  const needle = document.getElementById('compass-needle')!
  let toNorth = false, drag: { id: number; x: number; moved: boolean } | null = null
  button.addEventListener('pointerdown', (e) => {
    drag = { id: e.pointerId, x: e.clientX, moved: false }
    button.setPointerCapture(e.pointerId)
    e.stopPropagation()
  })
  button.addEventListener('pointermove', (e) => {
    if (!drag || e.pointerId !== drag.id) return
    const dx = e.clientX - drag.x
    if (Math.abs(dx) > 2) drag.moved = true
    controls.turnBy(dx * 0.012)
    drag.x = e.clientX
    toNorth = false
  })
  const end = (e: PointerEvent) => {
    if (!drag || e.pointerId !== drag.id) return
    if (!drag.moved) toNorth = true
    drag = null
  }
  button.addEventListener('pointerup', end)
  button.addEventListener('pointercancel', end)
  button.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') toNorth = true })
  return (dt) => {
    const h = controls.heading()
    if (toNorth) {
      const step = Math.sign(h) * Math.min(Math.abs(h), Math.max(Math.abs(h), 0.3) * dt * 6)
      controls.turnBy(step)
      if (Math.abs(controls.heading()) < 0.002) toNorth = false
    }
    needle.setAttribute('transform', `rotate(${(-h * 180) / Math.PI} 20 20)`)
    button.hidden = evalMode || controls.mode !== 'map' || (Math.abs(h) < 0.01 && !drag)
  }
}
