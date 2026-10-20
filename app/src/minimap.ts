import type { Overview } from './city'

const SIZE = 168 // CSS pixels

/** North-up 2D map of the build, with the camera's position and view direction. */
export class Minimap {
  readonly canvas: HTMLCanvasElement
  private base: HTMLCanvasElement
  private scale: number
  private ox: number
  private oy: number
  private dpr = Math.min(window.devicePixelRatio, 2)
  onPick: (x: number, y: number) => void = () => {}

  constructor(private ov: Overview, host: HTMLElement) {
    const [x0, y0, x1, y1] = ov.bounds
    this.scale = SIZE / Math.max(x1 - x0, y1 - y0)
    this.ox = x0 + (x1 - x0) / 2
    this.oy = y0 + (y1 - y0) / 2

    this.canvas = document.createElement('canvas')
    this.canvas.className = 'minimap'
    this.canvas.width = this.canvas.height = SIZE * this.dpr
    this.canvas.setAttribute('role', 'img')
    this.canvas.setAttribute('aria-label', 'Overview map. Click to jump there.')
    host.appendChild(this.canvas)

    this.base = document.createElement('canvas')
    this.base.width = this.base.height = SIZE * this.dpr
    this.drawBase()

    this.canvas.addEventListener('click', (e) => {
      const r = this.canvas.getBoundingClientRect()
      const px = ((e.clientX - r.left) / r.width) * SIZE
      const py = ((e.clientY - r.top) / r.height) * SIZE
      this.onPick(this.ox + (px - SIZE / 2) / this.scale, this.oy - (py - SIZE / 2) / this.scale)
    })
  }

  private px(x: number, y: number): [number, number] {
    return [(SIZE / 2 + (x - this.ox) * this.scale) * this.dpr, (SIZE / 2 - (y - this.oy) * this.scale) * this.dpr]
  }

  private fill(ctx: CanvasRenderingContext2D, rings: number[][][], color: string): void {
    ctx.fillStyle = color
    ctx.beginPath()
    for (const ring of rings) {
      ring.forEach(([x, y], i) => {
        const [a, b] = this.px(x, y)
        if (i === 0) ctx.moveTo(a, b)
        else ctx.lineTo(a, b)
      })
      ctx.closePath()
    }
    ctx.fill('evenodd')
  }

  private drawBase(): void {
    const ctx = this.base.getContext('2d')!
    ctx.fillStyle = '#d9d6cf'
    ctx.fillRect(0, 0, this.base.width, this.base.height)
    this.fill(ctx, this.ov.green, '#a9bf96')
    this.fill(ctx, this.ov.water, '#7fa7bd')
    this.fill(ctx, this.ov.roads, '#f4f2ee')
    this.fill(ctx, this.ov.buildings, '#8c8679')
    ctx.strokeStyle = '#1a7fae'
    ctx.lineWidth = 1.5 * this.dpr
    ctx.setLineDash([4 * this.dpr, 3 * this.dpr])
    for (const area of this.ov.areas) {
      ctx.beginPath()
      for (const ring of area.rings) {
        ring.forEach(([x, y], i) => {
          const [a, b] = this.px(x, y)
          if (i === 0) ctx.moveTo(a, b)
          else ctx.lineTo(a, b)
        })
        ctx.closePath()
      }
      ctx.stroke()
    }
  }

  /** x, y in local meters; heading in degrees clockwise from north. */
  draw(x: number, y: number, heading: number): void {
    const ctx = this.canvas.getContext('2d')!
    ctx.drawImage(this.base, 0, 0)
    const [a, b] = this.px(x, y)
    const h = (heading * Math.PI) / 180
    const d = this.dpr
    ctx.save()
    ctx.translate(a, b)
    ctx.rotate(h)
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
