import { MathUtils, Mesh, PerspectiveCamera, Raycaster, Vector2, Vector3 } from 'three'
import { MapControls } from 'three/addons/controls/MapControls.js'
import { TouchMap } from './touch'

export type Mode = 'map' | 'walk'

const EYE = 1.7
const WALK_SPEED = 2.2  // m/s, a brisk walk
const RUN_SPEED = 12    // m/s, more like a bike; for covering ground
const RADIUS = 0.45     // how close you can get to a wall
const LOOK_SPEED = 0.0022

/**
 * Two ways to move:
 * - map: MapControls for the mouse (drag to pan, right-drag to rotate, wheel to zoom);
 *   TouchMap for fingers (drag, pinch, twist, two-finger tilt; see touch.ts)
 * - walk: first person at eye height, collides with buildings
 */
export class Controls {
  mode: Mode = 'map'
  readonly map: MapControls
  readonly touch: TouchMap
  private yaw = 0
  private pitch = 0
  private keys = new Set<string>()
  private stick = new Vector2() // virtual joystick, -1..1
  private ray = new Raycaster()
  onModeChange: (m: Mode) => void = () => {}

  private down = new Raycaster()

  constructor(
    private camera: PerspectiveCamera,
    private dom: HTMLElement,
    private colliders: () => Iterable<Mesh>,
    private ground: () => Iterable<Mesh>,
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
    this.touch = new TouchMap(camera, map, dom, () => [...this.colliders(), ...this.ground()])
    this.ray.firstHitOnly = true
    this.down.firstHitOnly = true

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
        const gx = t.x - dir.x * 12, gz = t.z - dir.z * 12
        this.camera.position.set(gx, (this.groundAt(gx, gz) ?? 0) + EYE, gz)
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
    this.touch.enabled = mode === 'map'
    if (mode === 'map' && document.pointerLockElement) document.exitPointerLock()
    this.onModeChange(mode)
  }

  setStick(x: number, y: number): void {
    this.stick.set(x, y)
  }

  /** Compass heading of the view in radians, clockwise from north (world -z is north). */
  heading(): number {
    const f = new Vector3(0, 0, -1).applyQuaternion(this.camera.quaternion)
    return Math.atan2(f.x, -f.z)
  }

  /** Turn the map by `angle` radians clockwise on screen (the heading drops) about the point it's looking at. */
  turnBy(angle: number): void {
    if (this.mode !== 'map') return
    this.touch.turn(angle, this.map.target.clone())
    this.camera.lookAt(this.map.target)
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
      this.camera.position.set(point.x, (this.groundAt(point.x, point.z) ?? 0) + EYE, point.z)
    }
  }

  /** Height of the loaded ground under (x, z), or null where none is loaded. */
  groundAt(x: number, z: number): number | null {
    this.down.set(new Vector3(x, 400, z), new Vector3(0, -1, 0))
    this.down.far = 800
    const hit = this.down.intersectObjects([...this.ground()], false)[0]
    return hit ? hit.point.y : null
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
    this.followGround(dt)
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
      this.ray.set(this.camera.position.clone().setY(this.camera.position.y - EYE + 1.0), dir)
      this.ray.far = dist + RADIUS
      const hit = this.ray.intersectObjects(meshes, false)[0]
      if (!hit || !hit.face) break
      const n = hit.face.normal.clone().transformDirection(hit.object.matrixWorld).setY(0).normalize()
      const allowed = Math.max(0, hit.distance - RADIUS)
      const blocked = dir.clone().multiplyScalar(dist - allowed)
      move.sub(n.multiplyScalar(blocked.dot(n)))
    }
    this.camera.position.add(move)
  }

  /** Keep eye height above the ground: ease up and down slopes, stairs and bridges. */
  private followGround(dt: number): void {
    const g = this.groundAt(this.camera.position.x, this.camera.position.z)
    if (g === null) return
    const want = g + EYE
    const y = this.camera.position.y
    this.camera.position.y = Math.abs(want - y) > 6 ? want : y + (want - y) * Math.min(1, dt * 10)
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
