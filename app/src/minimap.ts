import type { Area, Overview } from './city'

const SIZE = 168 // CSS pixels

/** North-up map centered on the camera, drawn from the build's minimap image. */
export class Minimap {
  readonly canvas: HTMLCanvasElement
  private img = new Image()
  private dpr = Math.min(window.devicePixelRatio, 2)
  private span = 2000 // meters across the minimap
  private cx = 0
  private cy = 0
  onPick: (x: number, y: number) => void = () => {}

  constructor(private ov: Overview, base: string, host: HTMLElement) {
    this.img.src = base + ov.image
    this.canvas = document.createElement('canvas')
    this.canvas.className = 'minimap'
    this.canvas.width = this.canvas.height = SIZE * this.dpr
    this.canvas.setAttribute('role', 'img')
    this.canvas.setAttribute('aria-label', 'Overview map. Click to jump there.')
    host.appendChild(this.canvas)
    this.canvas.addEventListener('click', (e) => {
      const r = this.canvas.getBoundingClientRect()
      const fx = (e.clientX - r.left) / r.width - 0.5
      const fy = (e.clientY - r.top) / r.height - 0.5
      this.onPick(this.cx + fx * this.span, this.cy - fy * this.span)
    })
  }

  /** x, y in local meters; heading in degrees clockwise from north; span = meters across. */
  draw(x: number, y: number, heading: number, span: number, current: Area | null): void {
    this.cx = x; this.cy = y; this.span = span
    const ctx = this.canvas.getContext('2d')!
    const W = this.canvas.width
    const [x0, , , y1] = this.ov.bounds
    const mpp = this.ov.metersPerPixel
    ctx.fillStyle = '#cdc9c1'
    ctx.fillRect(0, 0, W, W)
    if (this.img.complete && this.img.naturalWidth) {
      ctx.imageSmoothingEnabled = true
      ctx.drawImage(this.img, (x - span / 2 - x0) / mpp, (y1 - (y + span / 2)) / mpp, span / mpp, span / mpp, 0, 0, W, W)
    }
    const toPx = (px: number, py: number): [number, number] =>
      [((px - x) / span + 0.5) * W, ((y - py) / span + 0.5) * W]

    if (current) {
      ctx.strokeStyle = '#1a7fae'
      ctx.lineWidth = 1.5 * this.dpr
      ctx.setLineDash([4 * this.dpr, 3 * this.dpr])
      ctx.beginPath()
      for (const ring of current.rings) {
        ring.forEach(([rx, ry], i) => {
          const [a, b] = toPx(rx, ry)
          if (i === 0) ctx.moveTo(a, b)
          else ctx.lineTo(a, b)
        })
        ctx.closePath()
      }
      ctx.stroke()
      ctx.setLineDash([])
    }

    const d = this.dpr
    ctx.save()
    ctx.translate(W / 2, W / 2)
    ctx.rotate((heading * Math.PI) / 180)
    ctx.fillStyle = 'rgba(26, 127, 174, 0.28)'
    ctx.beginPath()
    ctx.moveTo(0, 0)
    ctx.arc(0, 0, 26 * d, -Math.PI / 2 - 0.5, -Math.PI / 2 + 0.5)
    ctx.closePath()
    ctx.fill()
    ctx.fillStyle = '#1a7fae'
    ctx.strokeStyle = '#fff'
    ctx.lineWidth = 1.5 * d
    ctx.beginPath()
    ctx.arc(0, 0, 4.5 * d, 0, Math.PI * 2)
    ctx.fill()
    ctx.stroke()
    ctx.restore()
  }
}

/** The community area containing (x, y), if any. */
export function areaAt(areas: Area[], x: number, y: number): Area | null {
  for (const a of areas) {
    let inside = false
    for (const ring of a.rings) {
      for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const [xi, yi] = ring[i], [xj, yj] = ring[j]
        if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside
      }
    }
    if (inside) return a
  }
  return null
}
