import { Vector3 } from 'three'

/**
 * Two coordinate conventions meet here:
 * - local map frame (pipeline, viewpoints, minimap): x east, y north, z up, meters from State & Madison
 * - three.js world: x east, y up, z south (glTF convention)
 */
export type Local = [number, number, number]

export function toWorld([x, y, z]: Local | number[], out = new Vector3()): Vector3 {
  return out.set(x, z ?? 0, -y)
}

export function toLocal(v: Vector3): Local {
  return [v.x, -v.z, v.y]
}

/** Compass heading in degrees (0 = north, 90 = east) of a world-space direction. */
export function headingOf(dir: Vector3): number {
  const deg = (Math.atan2(dir.x, -dir.z) * 180) / Math.PI
  return (deg + 360) % 360
}
