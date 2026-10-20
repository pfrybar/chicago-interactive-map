import {
  BufferAttribute, BufferGeometry, Color, Material, Matrix4, Mesh, MeshLambertMaterial, MeshStandardMaterial,
  Object3D, PerspectiveCamera, WebGLRenderer,
} from 'three'
import { TilesRenderer } from '3d-tiles-renderer'
import { LAND } from './scene'
import { acceleratedRaycast, computeBoundsTree, disposeBoundsTree } from 'three-mesh-bvh'

BufferGeometry.prototype.computeBoundsTree = computeBoundsTree
BufferGeometry.prototype.disposeBoundsTree = disposeBoundsTree
Mesh.prototype.raycast = acceleratedRaycast

export interface Feature {
  id: string
  h: number
  minH?: number
  src: string
  name?: string
  year?: number
  osm?: string
  /** Height and source before lidar replaced it. */
  prev?: [number, string]
  /** 2 when the roof shape comes from the lidar (roofer LoD 2.2). */
  lod?: number
}

export interface Area { number: number; name: string; center: [number, number]; rings: number[][][] }

export interface Overview {
  bounds: [number, number, number, number]
  image: string
  metersPerPixel: number
  size: [number, number]
  areas: Area[]
}

export interface Attribution { text: string; license: string; url: string }

export interface BuildInfo {
  name: string
  base: string
  overview: Overview
  attribution: Attribution[]
  frame: { enuToEcef: number[] }
  report: { areas: { number: number; name: string }[]; buildings: number }
}

// One shared material per ground layer / buildings, replacing the defaults in the tiles.
// Colors are linear (three.js working space), so they look lighter on screen than the numbers suggest.
const MATERIALS: Record<string, Material> = {
  building: new MeshStandardMaterial({ color: new Color(0.60, 0.58, 0.55), roughness: 0.85, metalness: 0 }),
  land: new MeshLambertMaterial({ color: LAND }),
  green: new MeshLambertMaterial({ color: new Color(0.22, 0.33, 0.15) }),
  sand: new MeshLambertMaterial({ color: new Color(0.66, 0.58, 0.40) }),
  water: new MeshStandardMaterial({ color: new Color(0.035, 0.11, 0.17), roughness: 0.08, metalness: 0 }),
  road: new MeshLambertMaterial({ color: new Color(0.19, 0.185, 0.18) }),
  path: new MeshLambertMaterial({ color: new Color(0.55, 0.54, 0.51) }),
  wall: new MeshLambertMaterial({ color: new Color(0.42, 0.40, 0.37) }),
}

export async function loadBuild(): Promise<BuildInfo> {
  const manifest = await (await fetch('builds/manifest.json')).json()
  const name: string = manifest.current
  const base = `builds/${name}/`
  const [overview, attribution, frame, report] = await Promise.all(
    ['overview.json', 'attribution.json', 'frame.json', 'report.json'].map(async (f) => (await fetch(base + f)).json()),
  )
  return { name, base, overview, attribution, frame, report }
}

export class City {
  readonly tiles: TilesRenderer
  /** Building meshes currently loaded; used for picking and walk collisions. */
  readonly buildingMeshes = new Set<Mesh>()
  /** Ground meshes currently loaded; walk mode stands on them. */
  readonly groundMeshes = new Set<Mesh>()
  ready = false

  constructor(build: BuildInfo, camera: PerspectiveCamera, renderer: WebGLRenderer, frameMatrix: Matrix4) {
    const tiles = new TilesRenderer(build.base + 'tileset.json')
    this.tiles = tiles
    // Screen-space error in pixels before a tile is swapped for its detailed children.
    // With 10 m error on 1 km tiles, full detail loads within roughly 1.3 km of the camera.
    tiles.errorTarget = matchMedia('(pointer: coarse)').matches ? 10 : 6
    tiles.lruCache.minSize = 300
    tiles.lruCache.maxSize = 500
    tiles.setCamera(camera)
    tiles.setResolutionFromRenderer(camera, renderer)

    // Undo the tileset's Earth-centered placement so tiles land in local meters, y up.
    tiles.group.matrixAutoUpdate = false
    tiles.group.matrix.copy(frameMatrix)
    tiles.group.matrixWorldNeedsUpdate = true

    tiles.addEventListener('load-model', ({ scene }: { scene: Object3D }) => {
      scene.traverse((o) => {
        const mesh = o as Mesh
        if (!mesh.isMesh) return
        const old = mesh.material as Material
        const key = old.name === 'building' || mesh.name === 'buildings' ? 'building' : old.name
        mesh.material = MATERIALS[key] ?? MATERIALS.land
        old.dispose()
        if (key === 'building') {
          mesh.castShadow = true
          mesh.receiveShadow = true
          mesh.geometry.computeBoundsTree()
          this.buildingMeshes.add(mesh)
        } else {
          mesh.receiveShadow = true
          if (key !== 'water' && key !== 'wall') {
            mesh.geometry.computeBoundsTree()
            this.groundMeshes.add(mesh)
          }
        }
      })
    })
    tiles.addEventListener('dispose-model', ({ scene }: { scene: Object3D }) => {
      scene.traverse((o) => {
        const mesh = o as Mesh
        if (!mesh.isMesh) return
        if (this.buildingMeshes.delete(mesh) || this.groundMeshes.delete(mesh)) mesh.geometry.disposeBoundsTree()
      })
    })
    tiles.addEventListener('tiles-load-end', () => { this.ready = true })
    tiles.addEventListener('tiles-load-start', () => { this.ready = false })
  }

  /** The building feature under a raycast hit, if any. */
  static featureAt(mesh: Mesh, faceA: number): { feature: Feature; index: number } | null {
    const ids = mesh.geometry.getAttribute('_feature_id_0') as BufferAttribute | undefined
    const features = mesh.userData.features as Feature[] | undefined
    if (!ids || !features) return null
    const index = Math.round(ids.getX(faceA))
    return features[index] ? { feature: features[index], index } : null
  }

  /** A copy of one building's triangles, for highlighting. */
  static extractFeature(mesh: Mesh, index: number): BufferGeometry {
    const src = mesh.geometry
    const ids = src.getAttribute('_feature_id_0') as BufferAttribute
    const pos = src.getAttribute('position') as BufferAttribute
    const nrm = src.getAttribute('normal') as BufferAttribute
    const idx = src.getIndex()!
    const outPos: number[] = []
    const outNrm: number[] = []
    for (let i = 0; i < idx.count; i += 3) {
      const a = idx.getX(i)
      if (Math.round(ids.getX(a)) !== index) continue
      for (let k = 0; k < 3; k++) {
        const v = idx.getX(i + k)
        outPos.push(pos.getX(v), pos.getY(v), pos.getZ(v))
        outNrm.push(nrm.getX(v), nrm.getY(v), nrm.getZ(v))
      }
    }
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(new Float32Array(outPos), 3))
    g.setAttribute('normal', new BufferAttribute(new Float32Array(outNrm), 3))
    return g
  }

  update(): void {
    this.tiles.update()
  }

  /** Only meshes that are actually shown: hidden coarser or finer tiles stay loaded but don't count. */
  static shown(meshes: Iterable<Mesh>): Mesh[] {
    const out: Mesh[] = []
    for (const m of meshes) {
      let o: Object3D | null = m
      while (o && o.visible) o = o.parent
      if (!o) out.push(m)
    }
    return out
  }
}

/** Matrix taking the tileset's frame (Earth-centered) to local meters with y up. */
export function frameMatrix(enuToEcef: number[]): Matrix4 {
  const toEcef = new Matrix4().fromArray(enuToEcef)
  const zUpToYUp = new Matrix4().makeRotationX(-Math.PI / 2)
  return zUpToYUp.multiply(toEcef.invert())
}
