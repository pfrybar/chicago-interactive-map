"""The local map frame: meters east/north/up from State & Madison."""
from functools import lru_cache

import numpy as np
from pyproj import CRS

from .config import load_config

WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)


class LocalFrame:
    """Flat local frame (transverse Mercator centered on the origin).

    x = east, y = north, z = up, all in meters. Over a city-sized area the
    projection's scale error is far below a centimeter per meter, and ground
    curvature is ignored (about 30 m of drop at the city edges, which only
    matters when overlaying globe-based data; see AGENTS.md).
    """

    def __init__(self, lat: float, lon: float, height: float):
        self.lat, self.lon, self.height = lat, lon, height
        self.proj4 = (
            f"+proj=tmerc +lat_0={lat} +lon_0={lon} +k=1 +x_0=0 +y_0=0 "
            "+ellps=WGS84 +units=m +no_defs"
        )
        self.crs = CRS.from_proj4(self.proj4)

    def enu_to_ecef(self) -> np.ndarray:
        """4x4 matrix taking local east/north/up meters to Earth-centered (ECEF) meters."""
        lat, lon = np.radians(self.lat), np.radians(self.lon)
        n = WGS84_A / np.sqrt(1 - WGS84_E2 * np.sin(lat) ** 2)
        origin = np.array([
            (n + self.height) * np.cos(lat) * np.cos(lon),
            (n + self.height) * np.cos(lat) * np.sin(lon),
            (n * (1 - WGS84_E2) + self.height) * np.sin(lat),
        ])
        east = np.array([-np.sin(lon), np.cos(lon), 0.0])
        north = np.array([-np.sin(lat) * np.cos(lon), -np.sin(lat) * np.sin(lon), np.cos(lat)])
        up = np.array([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)])
        m = np.eye(4)
        m[:3, 0], m[:3, 1], m[:3, 2], m[:3, 3] = east, north, up, origin
        return m

    def to_dict(self) -> dict:
        return {
            "origin": {"lat": self.lat, "lon": self.lon, "height": self.height},
            "proj4": self.proj4,
            "axes": "x east, y north, z up (meters); glTF content is y-up per the 3D Tiles spec",
            # Column-major, as 3D Tiles and three.js expect.
            "enuToEcef": self.enu_to_ecef().flatten(order="F").tolist(),
        }


@lru_cache
def local_frame() -> LocalFrame:
    o = load_config()["origin"]
    return LocalFrame(o["lat"], o["lon"], o["height"])
