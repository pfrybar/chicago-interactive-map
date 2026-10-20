"""The tile grid: square tiles anchored at the origin, in a quadtree of levels.

Level 0 tiles are `tile_size` meters (500 m) and hold full detail. Each level up
doubles the size and holds simpler content for viewing from farther away.
"""
import math
from dataclasses import dataclass

from shapely.geometry import box

from .config import load_config

BLOCK_LEVEL = 2  # pipeline work unit: one level-2 tile (2 km) with its 16 leaves

# The grid starts 32 km southwest of State & Madison, so one level-7 tile (64 km)
# covers all of Chicago and the quadtree has a single root. (A grid anchored at
# the origin would split the Loop between four trees that never merge.)
GRID_ORIGIN = (-32000.0, -32000.0)


@dataclass(frozen=True, order=True)
class Key:
    level: int
    i: int
    j: int

    @property
    def size(self) -> float:
        return load_config()["tile_size"] * 2 ** self.level

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        s = self.size
        ox, oy = GRID_ORIGIN
        return ox + self.i * s, oy + self.j * s, ox + (self.i + 1) * s, oy + (self.j + 1) * s

    def polygon(self):
        return box(*self.bounds)

    def parent(self) -> "Key":
        return Key(self.level + 1, self.i // 2, self.j // 2)

    def ancestor(self, level: int) -> "Key":
        k = self
        while k.level < level:
            k = k.parent()
        return k

    def children(self) -> list["Key"]:
        return [Key(self.level - 1, 2 * self.i + a, 2 * self.j + b) for a in (0, 1) for b in (0, 1)]

    def leaves(self) -> list["Key"]:
        n = 2 ** self.level
        return [Key(0, self.i * n + a, self.j * n + b) for a in range(n) for b in range(n)]

    @property
    def name(self) -> str:
        return f"{self.level}/{self.i}_{self.j}"


def index(x, y, level: int = 0):
    """Tile column and row containing (x, y); works on scalars or numpy arrays."""
    s = load_config()["tile_size"] * 2 ** level
    ox, oy = GRID_ORIGIN
    if isinstance(x, (int, float)):
        return math.floor((x - ox) / s), math.floor((y - oy) / s)
    import numpy as np
    return np.floor((x - ox) / s).astype(int), np.floor((y - oy) / s).astype(int)


def leaf_at(x: float, y: float) -> Key:
    return Key(0, *index(x, y))


def leaves_covering(geom, margin_tiles: int = 0) -> set[Key]:
    """Leaf tiles touching `geom` grown by `margin_tiles` tiles."""
    s = load_config()["tile_size"]
    grown = geom.buffer(margin_tiles * s) if margin_tiles else geom
    x0, y0, x1, y1 = grown.bounds
    (i0, j0), (i1, j1) = index(x0, y0), index(x1, y1)
    out = set()
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            k = Key(0, i, j)
            if grown.intersects(k.polygon()):
                out.add(k)
    return out
