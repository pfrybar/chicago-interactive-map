import { MathUtils, Mesh, PerspectiveCamera, Plane, Raycaster, Spherical, Vector2, Vector3 } from 'three'
import type { MapControls } from 'three/addons/controls/MapControls.js'

/**
 * Map-mode touch gestures, like phone map apps:
 * - one finger drags the map (the ground under the finger stays under it)
 * - two fingers pinch to zoom, twist to turn and move together to pan, all around the
 *   building or ground between them, which stays put
 * - two fingers sliding up or down together tilt the view
 * MapControls still handles the mouse.
 */
const REACH = 3  // how far a drag can grab the ground, in camera-to-target distances

export class TouchMap {
  private pointers = new Map<number, Vector2>()
  private ray = new Raycaster()
  private plane = new Plane(new Vector3(0, 1, 0), 0)
  private pivot = new Vector3()
  private grab = new Vector3()        // one finger: the ground point being dragged
  private prev: { mid: Vector2; dist: number; angle: number } | null = null
  private start: { mid: Vector2; dist: number; angle: number; p: Vector2[] } | null = null
  private tilting: boolean | null = null  // decided on the first real movement of a two-finger gesture
  enabled = true

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
    this.pointers.set(e.pointerId, new Vector2(e.clientX, e.clientY))
    this.begin()
  }

  private up(e: PointerEvent): void {
    if (!this.pointers.delete(e.pointerId)) return
    this.begin()
  }

  /** (Re)start a gesture with the fingers now down, so adding or lifting one doesn't jump. */
  private begin(): void {
    const p = [...this.pointers.values()]
    this.prev = this.start = null
    this.tilting = null
    if (p.length === 1) {
      this.groundPoint(p[0], this.grab)
      this.plane.constant = -this.grab.y
      this.onPlane(p[0], this.grab)  // far hits (a tower on the horizon) are capped the same way
    } else if (p.length >= 2) {
      const g = this.shape(p[0], p[1])
      this.prev = g
      this.start = { ...g, mid: g.mid.clone(), p: [p[0].clone(), p[1].clone()] }
      this.groundPoint(g.mid, this.pivot)
      this.plane.constant = -this.pivot.y
    }
  }

  private move(e: PointerEvent): void {
    const pt = this.pointers.get(e.pointerId)
    if (!pt || !this.enabled) return
    pt.set(e.clientX, e.clientY)
    const p = [...this.pointers.values()]
    if (p.length === 1) {
      this.translate(this.grab.clone().sub(this.onPlane(p[0], new Vector3())))
    } else if (p.length >= 2 && this.prev && this.start) {
      const g = this.shape(p[0], p[1])
      if (this.tilting === null) this.tilting = this.decideTilt(p)
      if (this.tilting === true) {
        this.tilt((g.mid.y - this.prev.mid.y) / this.dom.clientHeight)
      } else if (this.tilting === false) {
        const a = this.onPlane(this.prev.mid, new Vector3())
        this.translate(a.sub(this.onPlane(g.mid, new Vector3())))
        this.onPlane(g.mid, this.pivot)
        this.zoom(this.prev.dist / Math.max(g.dist, 1))
        this.turn(g.angle - this.prev.angle)
      }
      this.prev = g
    }
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
   * Where a screen point meets the gesture's horizontal plane, no farther than REACH
   * times the camera's distance from its target. Near the horizon of a tilted view a
   * few pixels span kilometers, and above it there's no ground at all: dragging there
   * flung the map out of the city. Beyond the limit, points sit on it in the same direction.
   */
  private onPlane(p: Vector2, out: Vector3): Vector3 {
    this.ray.setFromCamera(this.ndc(p), this.camera)
    const cam = this.camera.position
    const reach = REACH * cam.distanceTo(this.map.target)
    const dir = this.ray.ray.direction
    const flat = Math.hypot(dir.x, dir.z) || 1e-9
    const hit = this.ray.ray.intersectPlane(this.plane, out)
    if (hit && Math.hypot(hit.x - cam.x, hit.z - cam.z) <= reach) return hit
    // Past the limit (and above the horizon) the distance keeps growing, gently: one
    // `reach` per radian of view angle, so dragging in the sky still moves the map.
    const height = Math.max(cam.y + this.plane.constant, 1)
    const below = Math.asin(MathUtils.clamp(-dir.y, -1, 1))
    const d = reach * (1 + Math.max(0, Math.atan(height / reach) - below))
    return out.set(cam.x + (dir.x / flat) * d, -this.plane.constant, cam.z + (dir.z / flat) * d)
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
