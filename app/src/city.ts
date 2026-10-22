import {
  BufferAttribute, BufferGeometry, Color, InstancedMesh, Material, Matrix4, Mesh, MeshLambertMaterial, MeshStandardMaterial,
  Object3D, PerspectiveCamera, Vector3, WebGLRenderer,
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
  report: { areas: { number: number; name: string }[]; buildings: number; created_at: string }
}

/** Meters from the camera within which street-level tiles show every tree. */
const TREE_NEAR = 500

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
  rail: new MeshLambertMaterial({ color: new Color(0.26, 0.23, 0.20) }),         // railbed gravel
  steel: new MeshStandardMaterial({ color: new Color(0.06, 0.08, 0.07), roughness: 0.7, metalness: 0.3 }), // CTA structure
  rails: new MeshStandardMaterial({ color: new Color(0.35, 0.33, 0.31), roughness: 0.35, metalness: 0.8 }),
  ballast: new MeshLambertMaterial({ color: new Color(0.20, 0.18, 0.16) }),
  platform: new MeshLambertMaterial({ color: new Color(0.45, 0.44, 0.42) }),
  canopy: new MeshStandardMaterial({ color: new Color(0.08, 0.10, 0.09), roughness: 0.6, metalness: 0.2 }),
  foliage: new MeshLambertMaterial({ color: new Color(0.11, 0.20, 0.07) }),
  bark: new MeshLambertMaterial({ color: new Color(0.12, 0.08, 0.05) }),
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
  /** Street-level tiles' tree sets: full near the camera, thinned farther away. */
  private treeSets = new Map<Object3D, { full: Mesh[]; far: Mesh[]; center: Vector3 }>()
  private tmp = new Vector3()
  ready = false

  constructor(build: BuildInfo, camera: PerspectiveCamera, renderer: WebGLRenderer, frameMatrix: Matrix4) {
    const tiles = new TilesRenderer(build.base + 'tileset.json')
    this.tiles = tiles
    // Every tile URL carries the build's id, so the server can let browsers cache tiles
    // for good (a rebuild changes the id). See serveBuilds in vite.config.ts.
    const version = build.report.created_at ?? build.name
    tiles.registerPlugin({
      name: 'BUILD_VERSION',
      preprocessURL: (url: string) => {
        const u = new URL(url, location.href)
        u.searchParams.set('v', version)
        return u.toString()
      },
    })
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
        let key = old.name === 'building' || mesh.name === 'buildings' ? 'building' : old.name
        if (mesh.name === 'foliage_far') key = 'foliage'
        if (['foliage', 'bark', 'foliage_far'].includes(mesh.name)) this.addTreeMesh(scene, mesh)
        mesh.material = MATERIALS[key] ?? MATERIALS.land
        old.dispose()
        if (key === 'foliage' || key === 'bark') {
          mesh.receiveShadow = true  // trees don't cast: there are too many of them
          return
        }
        if (['steel', 'rails', 'platform', 'canopy'].includes(key)) {
          mesh.castShadow = true
          mesh.receiveShadow = true
          return
        }
        if (key === 'building') {
          mesh.castShadow = true
          mesh.receiveShadow = true
          mesh.geometry.computeBoundsTree()
          this.buildingMeshes.add(mesh)
        } else {
          mesh.receiveShadow = true
          if (!['water', 'wall', 'steel', 'rails', 'ballast', 'platform', 'canopy', 'foliage', 'bark'].includes(key)) {
            mesh.geometry.computeBoundsTree()
            this.groundMeshes.add(mesh)
          }
        }
      })
    })
    tiles.addEventListener('dispose-model', ({ scene }: { scene: Object3D }) => {
      this.treeSets.delete(scene)
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

  private addTreeMesh(tile: Object3D, mesh: Mesh): void {
    let set = this.treeSets.get(tile)
    if (!set) {
      set = { full: [], far: [], center: new Vector3() }
      this.treeSets.set(tile, set)
    }
    if (mesh.name === 'foliage_far') {
      set.far.push(mesh)
    } else {
      set.full.push(mesh)
      const inst = mesh as InstancedMesh
      if (inst.isInstancedMesh && set.full.length === 1) {
        inst.computeBoundingSphere()
        set.center.copy(inst.boundingSphere!.center)  // in the mesh's own space
      }
    }
    mesh.visible = mesh.name === 'foliage_far'
  }

  update(camera: PerspectiveCamera): void {
    this.tiles.update()
    // Every tree only for street-level tiles near the camera; the thinned set elsewhere.
    for (const set of this.treeSets.values()) {
      if (!set.full.length) continue
      const near = camera.position.distanceTo(this.tmp.copy(set.center).applyMatrix4(set.full[0].matrixWorld)) < TREE_NEAR
      for (const m of set.full) m.visible = near
      for (const m of set.far) m.visible = !near
    }
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
