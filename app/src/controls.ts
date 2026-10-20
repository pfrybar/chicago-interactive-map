import { MathUtils, Mesh, PerspectiveCamera, Raycaster, Vector2, Vector3 } from 'three'
import { MapControls } from 'three/addons/controls/MapControls.js'

export type Mode = 'map' | 'walk'

const EYE = 1.7
const WALK_SPEED = 2.2  // m/s, a brisk walk
const RUN_SPEED = 12    // m/s, more like a bike; for covering ground
const RADIUS = 0.45     // how close you can get to a wall
const LOOK_SPEED = 0.0022

/**
 * Two ways to move:
 * - map: MapControls (drag to pan, right-drag/two fingers to rotate, wheel/pinch to zoom)
 * - walk: first person at eye height, collides with buildings
 */
export class Controls {
  mode: Mode = 'map'
  readonly map: MapControls
  private yaw = 0
  private pitch = 0
  private keys = new Set<string>()
  private stick = new Vector2() // virtual joystick, -1..1
  private ray = new Raycaster()
  onModeChange: (m: Mode) => void = () => {}

  constructor(
    private camera: PerspectiveCamera,
    private dom: HTMLElement,
    private colliders: () => Iterable<Mesh>,
  ) {
    const map = new MapControls(camera, dom)
    map.enableDamping = true
    map.dampingFactor = 0.08
    map.screenSpacePanning = false
    map.maxPolarAngle = MathUtils.degToRad(84)
    map.minDistance = 15
    map.maxDistance = 9000
    map.zoomToCursor = true
    this.map = map
    this.ray.firstHitOnly = true

    addEventListener('keydown', (e) => {
      if (e.target instanceof HTMLInputElement) return
      this.keys.add(e.code)
    })
    addEventListener('keyup', (e) => this.keys.delete(e.code))
    addEventListener('blur', () => this.keys.clear())

    dom.addEventListener('click', () => {
      if (this.mode === 'walk' && !matchMedia('(pointer: coarse)').matches && document.pointerLockElement !== dom) {
        dom.requestPointerLock?.()
      }
    })
    addEventListener('mousemove', (e) => {
      if (this.mode === 'walk' && document.pointerLockElement === dom) this.look(e.movementX, e.movementY)
    })
    this.setupTouchLook()
  }

  /** Point the camera from `position` at `target` (world coordinates) in the given mode. */
  setView(mode: Mode, position: Vector3, target: Vector3): void {
    this.camera.position.copy(position)
    if (mode === 'walk') {
      const d = target.clone().sub(position)
      this.yaw = Math.atan2(-d.x, -d.z)
      this.pitch = Math.atan2(d.y, Math.hypot(d.x, d.z))
      this.applyLook()
    } else {
      this.map.target.copy(target)
      this.camera.lookAt(target)
    }
    this.setMode(mode, false)
  }

  setMode(mode: Mode, carryPosition = true): void {
    if (mode === this.mode && carryPosition) return
    if (carryPosition) {
      if (mode === 'walk') {
        // Drop in at the spot the map was looking at, facing the same way.
        const t = this.map.target
        const dir = t.clone().sub(this.camera.position).setY(0).normalize()
        this.camera.position.set(t.x - dir.x * 12, EYE, t.z - dir.z * 12)
        this.yaw = Math.atan2(-dir.x, -dir.z)
        this.pitch = MathUtils.degToRad(8)
        this.applyLook()
      } else {
        // Rise up behind the walker.
        const fwd = new Vector3(0, 0, -1).applyQuaternion(this.camera.quaternion).setY(0).normalize()
        const t = this.camera.position.clone().addScaledVector(fwd, 60).setY(0)
        this.camera.position.copy(t).addScaledVector(fwd, -260).setY(180)
        this.map.target.copy(t)
        this.camera.lookAt(t)
      }
    }
    this.mode = mode
    this.map.enabled = mode === 'map'
    if (mode === 'map' && document.pointerLockElement) document.exitPointerLock()
    this.onModeChange(mode)
  }

  setStick(x: number, y: number): void {
    this.stick.set(x, y)
  }

  /** Where the camera is aimed on the ground (world). */
  focus(out = new Vector3()): Vector3 {
    if (this.mode === 'map') return out.copy(this.map.target)
    const fwd = new Vector3(0, 0, -1).applyQuaternion(this.camera.quaternion).setY(0).normalize()
    return out.copy(this.camera.position).addScaledVector(fwd, 150).setY(0)
  }

  /** Move to a ground point (world), keeping the current mode. */
  jumpTo(point: Vector3): void {
    if (this.mode === 'map') {
      const offset = this.camera.position.clone().sub(this.map.target)
      this.map.target.copy(point).setY(0)
      this.camera.position.copy(this.map.target).add(offset)
    } else {
      this.camera.position.set(point.x, EYE, point.z)
    }
  }

  update(dt: number): void {
    if (this.mode === 'map') {
      this.map.update()
      return
    }
    const k = this.keys
    let fwd = this.stick.y, side = this.stick.x
    if (k.has('KeyW') || k.has('ArrowUp')) fwd += 1
    if (k.has('KeyS') || k.has('ArrowDown')) fwd -= 1
    if (k.has('KeyD')) side += 1
    if (k.has('KeyA')) side -= 1
    if (k.has('ArrowLeft')) this.yaw += 1.6 * dt
    if (k.has('ArrowRight')) this.yaw -= 1.6 * dt
    this.applyLook()
    const len = Math.hypot(fwd, side)
    if (len < 0.01) return
    const speed = (k.has('ShiftLeft') || k.has('ShiftRight') ? RUN_SPEED : WALK_SPEED) * Math.min(len, 1)
    const f = new Vector3(-Math.sin(this.yaw), 0, -Math.cos(this.yaw))
    const r = new Vector3(Math.cos(this.yaw), 0, -Math.sin(this.yaw))
    const move = f.multiplyScalar(fwd / len).addScaledVector(r, side / len).multiplyScalar(speed * dt)
    this.moveWithCollisions(move)
  }

  private moveWithCollisions(move: Vector3): void {
    const meshes = [...this.colliders()]
    // Two tries: if blocked, slide along the wall instead of stopping dead.
    for (let i = 0; i < 2 && move.lengthSq() > 1e-8; i++) {
      const dist = move.length()
      const dir = move.clone().divideScalar(dist)
      this.ray.set(this.camera.position.clone().setY(1.0), dir)
      this.ray.far = dist + RADIUS
      const hit = this.ray.intersectObjects(meshes, false)[0]
      if (!hit || !hit.face) break
      const n = hit.face.normal.clone().transformDirection(hit.object.matrixWorld).setY(0).normalize()
      const allowed = Math.max(0, hit.distance - RADIUS)
      const blocked = dir.clone().multiplyScalar(dist - allowed)
      move.sub(n.multiplyScalar(blocked.dot(n)))
    }
    this.camera.position.add(move)
    this.camera.position.y = EYE
  }

  private look(dx: number, dy: number): void {
    this.yaw -= dx * LOOK_SPEED
    this.pitch = MathUtils.clamp(this.pitch - dy * LOOK_SPEED, -1.4, 1.4)
    this.applyLook()
  }

  private applyLook(): void {
    this.camera.rotation.set(this.pitch, this.yaw, 0, 'YXZ')
  }

  /** Touch: drag anywhere outside the joystick to look around in walk mode. */
  private setupTouchLook(): void {
    let last: { id: number; x: number; y: number } | null = null
    this.dom.addEventListener('pointerdown', (e) => {
      if (this.mode !== 'walk' || e.pointerType === 'mouse') return
      last = { id: e.pointerId, x: e.clientX, y: e.clientY }
    })
    this.dom.addEventListener('pointermove', (e) => {
      if (!last || e.pointerId !== last.id) return
      this.look((e.clientX - last.x) * 1.6, (e.clientY - last.y) * 1.6)
      last = { id: e.pointerId, x: e.clientX, y: e.clientY }
    })
    const end = (e: PointerEvent) => { if (last && e.pointerId === last.id) last = null }
    this.dom.addEventListener('pointerup', end)
    this.dom.addEventListener('pointercancel', end)
  }
}
