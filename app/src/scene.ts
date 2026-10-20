import {
  ACESFilmicToneMapping, Color, DirectionalLight, FogExp2, HemisphereLight, Mesh, MeshLambertMaterial,
  PCFShadowMap, PerspectiveCamera, PlaneGeometry, PMREMGenerator, Scene, SRGBColorSpace, Vector3,
  WebGLRenderer,
} from 'three'
import { Sky } from 'three/addons/objects/Sky.js'

/** Fixed afternoon sun (southwest, 32° up): good modeling light for the street grid. */
const SUN_AZIMUTH = 225
const SUN_ELEVATION = 32
const SHADOW_SPAN = 700
const FOG_DENSITY = 0.00014
/** Ground color, shared with the land layer so the area edge doesn't show. */
export const LAND = new Color(0.40, 0.39, 0.37)

export interface Stage {
  renderer: WebGLRenderer
  scene: Scene
  camera: PerspectiveCamera
  sun: DirectionalLight
  sunDir: Vector3
  setShadows(on: boolean): void
  /** Per-frame updates that follow the camera: sky dome position and haze. */
  followCamera(): void
  /** Center the shadow map on what the camera looks at, sized to roughly what's on screen. */
  followShadows(focus: Vector3, span: number): void
}

export function createStage(canvas: HTMLCanvasElement): Stage {
  const mobile = matchMedia('(pointer: coarse)').matches
  const renderer = new WebGLRenderer({ canvas, antialias: true, logarithmicDepthBuffer: true, powerPreference: 'high-performance' })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, mobile ? 1.5 : 2))
  renderer.outputColorSpace = SRGBColorSpace
  renderer.toneMapping = ACESFilmicToneMapping
  renderer.toneMappingExposure = 0.62
  renderer.shadowMap.type = PCFShadowMap

  const scene = new Scene()
  const camera = new PerspectiveCamera(55, 1, 0.5, 150000)

  const az = (SUN_AZIMUTH * Math.PI) / 180
  const el = (SUN_ELEVATION * Math.PI) / 180
  // World axes: x east, y up, z south.
  const sunDir = new Vector3(Math.sin(az) * Math.cos(el), Math.sin(el), -Math.cos(az) * Math.cos(el)).normalize()

  const sky = new Sky()
  sky.scale.setScalar(100000)
  sky.renderOrder = -20 // first: the sky writes no depth, so anything drawn before it would be painted over
  const u = sky.material.uniforms
  u.turbidity.value = 4
  u.rayleigh.value = 1.2
  u.mieCoefficient.value = 0.004
  u.mieDirectionalG.value = 0.8
  u.sunPosition.value.copy(sunDir)
  scene.add(sky)

  // Image-based light from the sky, so materials pick up blue fill from above.
  const pmrem = new PMREMGenerator(renderer)
  const envScene = new Scene()
  const envSky = new Sky()
  envSky.scale.setScalar(1000)
  envSky.material.uniforms = u
  envScene.add(envSky)
  scene.environment = pmrem.fromScene(envScene, 0, 1, 2000).texture
  scene.environmentIntensity = 0.25

  const sun = new DirectionalLight(new Color(1.0, 0.94, 0.85), 2.6)
  sun.shadow.mapSize.set(mobile ? 1024 : 2048, mobile ? 1024 : 2048)
  const sc = sun.shadow.camera
  sc.left = -SHADOW_SPAN / 2; sc.right = SHADOW_SPAN / 2; sc.top = SHADOW_SPAN / 2; sc.bottom = -SHADOW_SPAN / 2
  sc.near = 1; sc.far = 3000
  sun.shadow.bias = -0.0002
  sun.shadow.normalBias = 0.6
  scene.add(sun, sun.target)
  scene.add(new HemisphereLight(0xcfdcec, 0x8a8478, 0.45))

  scene.fog = new FogExp2(new Color(0.55, 0.64, 0.75), FOG_DENSITY)

  // Plain ground under and beyond the tiles, so the horizon never shows a void.
  // Drawn first without writing depth, so it can never z-fight with the tiles above it
  // (GPUs with 16-bit depth, common on phones, can't separate a few centimeters far away).
  const base = new Mesh(new PlaneGeometry(300000, 300000), new MeshLambertMaterial({ color: LAND, depthWrite: false }))
  base.rotation.x = -Math.PI / 2
  base.position.y = -0.05
  base.renderOrder = -10
  base.receiveShadow = true
  scene.add(base)

  const fog = scene.fog as FogExp2
  const stage: Stage = {
    renderer, scene, camera, sun, sunDir,
    followCamera() {
      // The sky dome must contain the camera wherever it goes.
      sky.position.copy(camera.position)
      // Thin the haze with altitude, so an overview of the whole city stays clear.
      fog.density = FOG_DENSITY / (1 + Math.max(camera.position.y, 0) / 600)
    },
    setShadows(on) {
      renderer.shadowMap.enabled = on
      sun.castShadow = on
      // Materials compile differently with and without shadows.
      scene.traverse((o) => {
        const m = (o as Mesh).material
        if (m) for (const mat of Array.isArray(m) ? m : [m]) mat.needsUpdate = true
      })
    },
    followShadows(focus, span) {
      // Quantize size and position so shadows don't shimmer while moving.
      const size = Math.min(Math.max(Math.pow(2, Math.ceil(Math.log2(span))), 256), 8192)
      if (size !== sc.right * 2) {
        sc.left = -size / 2; sc.right = size / 2; sc.top = size / 2; sc.bottom = -size / 2
        sc.far = size + 3000
        sc.updateProjectionMatrix()
      }
      const snap = size / 64
      const fx = Math.round(focus.x / snap) * snap
      const fz = Math.round(focus.z / snap) * snap
      sun.target.position.set(fx, 0, fz)
      const back = 1500 + size / 2
      sun.position.set(fx + sunDir.x * back, sunDir.y * back, fz + sunDir.z * back)
    },
  }
  return stage
}
