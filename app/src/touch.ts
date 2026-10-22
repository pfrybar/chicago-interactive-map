import { MathUtils, Mesh, PerspectiveCamera, Plane, Raycaster, Spherical, Vector2, Vector3 } from 'three'
import type { MapControls } from 'three/addons/controls/MapControls.js'

/**
 * Map-mode touch gestures, like phone map apps:
 * - one finger drags the map at a constant rate: the same finger movement moves the map
 *   the same distance anywhere on the screen (set by the view distance), so dragging near
 *   the horizon of a tilted view doesn't race; momentum covers long distances
 * - two fingers pinch to zoom, twist to turn and move together to pan, all around the
 *   building or ground between them, which stays put
 * - two fingers sliding up or down together tilt the view
 * - a flick keeps the map gliding after the fingers lift, slowing like iOS scrolling
 * MapControls still handles the mouse.
 */
const GLIDE_PAN_MS = 325   // time constant of the pan glide's slowdown (iOS scroll views use about this)
const GLIDE_TURN_MS = 200  // twist and pinch glide for less time
const LIFT_MS = 60         // a finger still moving this recently when lifted starts a glide

export class TouchMap {
  private pointers = new Map<number, Vector2>()
  private ray = new Raycaster()
  private plane = new Plane(new Vector3(0, 1, 0), 0)
  private pivot = new Vector3()
  private last = new Vector2()        // one finger: where it was at the last event
  private prev: { mid: Vector2; dist: number; angle: number } | null = null
  private start: { mid: Vector2; dist: number; angle: number; p: Vector2[] } | null = null
  private tilting: boolean | null = null  // decided on the first real movement of a two-finger gesture
  enabled = true
  // Velocities while fingers move (m/ms, rad/ms, log-distance/ms), and the glide after they lift.
  private motion = { pan: new Vector3(), turn: 0, zoom: 0, t: 0 }
  private glide: { pan: Vector3; turn: number; zoom: number } | null = null

  constructor(
    private camera: PerspectiveCamera,
    private map: MapControls,
    private dom: HTMLElement,
    private pickables: () => Iterable<Mesh>,
  ) {
    // MapControls ignores touch; this class handles it.
    map.touches = { ONE: -1 as never, TWO: -1 as never }
    this.ray.firstHitOnly = true
    dom.addEventListener('pointerdown', (e) => this.down(e))
    dom.addEventListener('pointermove', (e) => this.move(e))
    dom.addEventListener('pointerup', (e) => this.up(e))
    dom.addEventListener('pointercancel', (e) => this.up(e))
  }

  private down(e: PointerEvent): void {
    if (!this.enabled || e.pointerType !== 'touch') return
    this.glide = null  // a new touch catches the gliding map
    this.motion.t = 0
    this.pointers.set(e.pointerId, new Vector2(e.clientX, e.clientY))
    this.begin()
  }

  private up(e: PointerEvent): void {
    if (!this.pointers.delete(e.pointerId)) return
    // Event timestamps, not handling time: on a busy frame the handler runs late.
    if (this.pointers.size === 0 && this.tilting !== true && e.timeStamp - this.motion.t < LIFT_MS) {
      const cap = 4e-3 * this.camera.position.distanceTo(this.map.target)  // 4 view distances per second
      const pan = this.motion.pan.clone()
      if (pan.length() > cap) pan.setLength(cap)
      this.glide = { pan, turn: MathUtils.clamp(this.motion.turn, -6e-3, 6e-3), zoom: MathUtils.clamp(this.motion.zoom, -3e-3, 3e-3) }
    }
    this.motion.t = 0
    this.begin()
  }

  /** (Re)start a gesture with the fingers now down, so adding or lifting one doesn't jump. */
  private begin(): void {
    const p = [...this.pointers.values()]
    this.prev = this.start = null
    this.tilting = null
    if (p.length === 1) {
      this.last.copy(p[0])
    } else if (p.length >= 2) {
      const g = this.shape(p[0], p[1])
      this.prev = g
      this.start = { ...g, mid: g.mid.clone(), p: [p[0].clone(), p[1].clone()] }
      this.groundPoint(g.mid, this.pivot)
    }
  }

  private move(e: PointerEvent): void {
    const pt = this.pointers.get(e.pointerId)
    if (!pt || !this.enabled) return
    pt.set(e.clientX, e.clientY)
    const p = [...this.pointers.values()]
    if (p.length === 1) {
      const d = this.screenToGround(p[0].x - this.last.x, p[0].y - this.last.y)
      this.last.copy(p[0])
      this.translate(d)
      this.sample(d, 0, 0, e.timeStamp)
    } else if (p.length >= 2 && this.prev && this.start) {
      const g = this.shape(p[0], p[1])
      if (this.tilting === null) this.tilting = this.decideTilt(p)
      if (this.tilting === true) {
        this.tilt((g.mid.y - this.prev.mid.y) / this.dom.clientHeight)
      } else if (this.tilting === false) {
        const d = this.screenToGround(g.mid.x - this.prev.mid.x, g.mid.y - this.prev.mid.y)
        this.translate(d)
        this.pivot.add(d)  // the pivot stays between the fingers
        const scale = this.prev.dist / Math.max(g.dist, 1)
        this.zoom(scale)
        this.turn(g.angle - this.prev.angle)
        this.sample(d, g.angle - this.prev.angle, Math.log(scale), e.timeStamp)
      }
      this.prev = g
    }
    this.camera.lookAt(this.map.target)
  }

  /** Smoothed velocity of the current gesture, from the movement since the last event. */
  private sample(pan: Vector3, turn: number, zoom: number, now: number): void {
    const dt = now - this.motion.t
    const m = this.motion
    if (!m.t || dt > 100) {  // first sample, or after a pause: start over
      m.pan.copy(pan).divideScalar(16); m.turn = turn / 16; m.zoom = zoom / 16
    } else if (dt > 0) {
      const k = 0.4
      m.pan.multiplyScalar(1 - k).addScaledVector(pan, k / dt)
      m.turn = m.turn * (1 - k) + (turn / dt) * k
      m.zoom = m.zoom * (1 - k) + (zoom / dt) * k
    }
    m.t = now
  }

  /** Advance a glide by `ms` milliseconds; called every frame in map mode. */
  update(ms: number): void {
    const g = this.glide
    if (!g || !this.enabled) return
    ms = Math.min(ms, 50)
    this.translate(g.pan.clone().multiplyScalar(ms))
    this.turn(g.turn * ms, this.map.target.clone())
    if (g.zoom) {
      this.pivot.copy(this.map.target)
      this.zoom(Math.exp(g.zoom * ms))
    }
    g.pan.multiplyScalar(Math.exp(-ms / GLIDE_PAN_MS))
    g.turn *= Math.exp(-ms / GLIDE_TURN_MS)
    g.zoom *= Math.exp(-ms / GLIDE_TURN_MS)
    const still = g.pan.length() * 1000 < 0.02 * this.camera.position.distanceTo(this.map.target)
    if (still && Math.abs(g.turn) < 2e-5 && Math.abs(g.zoom) < 2e-5) this.glide = null
    this.camera.lookAt(this.map.target)
  }

  /** Two fingers sliding the same way vertically, without much pinch or twist, mean tilt.
   *  Judged on the midpoint: each finger's movement arrives as a separate event. */
  private decideTilt(p: Vector2[]): boolean | null {
    const s = this.start!
    const g = this.shape(p[0], p[1])
    // Wait until both fingers have moved: with only one moved, the pair looks twisted.
    const m0 = p[0].distanceTo(s.p[0]), m1 = p[1].distanceTo(s.p[1])
    if (Math.min(m0, m1) < 5 && Math.max(m0, m1) < 40) return null
    const pinch = Math.abs(g.dist - s.dist) / s.dist, twist = Math.abs(g.angle - s.angle)
    if (pinch > 0.1 || twist > 0.1) return false
    const dm = g.mid.clone().sub(s.mid)
    const d0y = p[0].y - s.p[0].y, d1y = p[1].y - s.p[1].y
    return Math.abs(dm.y) > 2 * Math.abs(dm.x) && d0y * d1y > 0
  }

  private shape(a: Vector2, b: Vector2) {
    return { mid: a.clone().add(b).multiplyScalar(0.5), dist: a.distanceTo(b), angle: Math.atan2(b.y - a.y, b.x - a.x) }
  }

  private ndc(p: Vector2): Vector2 {
    const r = this.dom.getBoundingClientRect()
    return new Vector2(((p.x - r.left) / r.width) * 2 - 1, -((p.y - r.top) / r.height) * 2 + 1)
  }

  /** The building or ground under a screen point, or the ground plane at the target's height. */
  private groundPoint(p: Vector2, out: Vector3): Vector3 {
    this.ray.setFromCamera(this.ndc(p), this.camera)
    const hit = this.ray.intersectObjects([...this.pickables()], false)[0]
    if (hit) return out.copy(hit.point)
    this.plane.constant = -this.map.target.y
    return this.ray.ray.intersectPlane(this.plane, out) ?? out.copy(this.map.target)
  }

  /**
   * The ground movement for a finger movement of (dx, dy) pixels: the map follows the
   * finger at the scale of the screen center, the same everywhere on the screen.
   */
  private screenToGround(dx: number, dy: number): Vector3 {
    const dist = this.camera.position.distanceTo(this.map.target)
    const perPixel = (2 * dist * Math.tan(MathUtils.degToRad(this.camera.fov) / 2)) / this.dom.clientHeight
    const right = new Vector3(1, 0, 0).applyQuaternion(this.camera.quaternion).setY(0).normalize()
    const ahead = new Vector3(0, 1, 0).applyQuaternion(this.camera.quaternion).setY(0)  // screen-up, on the ground
    if (ahead.lengthSq() < 1e-8) ahead.set(-right.z, 0, right.x)
    ahead.normalize()
    return right.multiplyScalar(-dx * perPixel).addScaledVector(ahead, dy * perPixel)
  }

  private translate(d: Vector3): void {
    d.y = 0
    this.camera.position.add(d)
    this.map.target.add(d)
  }

  private zoom(scale: number): void {
    const dist = this.camera.position.distanceTo(this.map.target) * scale
    if (dist < this.map.minDistance || dist > this.map.maxDistance) return
    for (const v of [this.camera.position, this.map.target]) v.sub(this.pivot).multiplyScalar(scale).add(this.pivot)
  }

  /** Turn the view about a vertical axis through the pivot. Twisting fingers clockwise turns the map clockwise. */
  turn(angle: number, about = this.pivot): void {
    if (!angle) return
    const up = new Vector3(0, 1, 0)
    for (const v of [this.camera.position, this.map.target]) v.sub(about).applyAxisAngle(up, angle).add(about)
  }

  /** Tilt about the target: sliding up looks toward the horizon. `amount` is a fraction of the screen height. */
  private tilt(amount: number): void {
    const s = new Spherical().setFromVector3(this.camera.position.clone().sub(this.map.target))
    s.phi = MathUtils.clamp(s.phi - amount * Math.PI, 0.01, this.map.maxPolarAngle)
    this.camera.position.copy(this.map.target).add(new Vector3().setFromSpherical(s))
  }
}
