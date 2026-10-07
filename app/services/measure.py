"""CRS selection + measurement calculation.

Rule: never measure in degrees. Every geometry is reprojected from its source CRS
(geographic *or* projected - e.g. EPSG:3857 is projected but badly distorted) into a
local metric CRS chosen from where the feature actually is:

* Area   -> Lambert Azimuthal Equal-Area centred on the feature (exact area preservation
            at any size, so no UTM-zone-edge or multi-zone problems).
* Length -> UTM zone of the feature centroid (conformal, <0.1 % scale error inside the
            zone), or UPS poles beyond 84N / 80S.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import shapely
from pyproj import CRS, Transformer
from shapely.geometry.base import BaseGeometry
from shapely import transform as _shp_transform
from shapely.validation import make_valid

WGS84 = CRS.from_epsg(4326)

AREA_TYPES = {"Polygon", "MultiPolygon"}
LENGTH_TYPES = {"LineString", "MultiLineString"}
NO_MEASURE_TYPES = {"Point", "MultiPoint"}


@dataclass
class Measurement:
    status: str  # OK | NOT_APPLICABLE | UNSUPPORTED | ERROR
    measurement_crs: str | None = None
    area_sq_m: float | None = None
    length_m: float | None = None
    geometry_valid: bool | None = None
    message: str | None = None


# ---------------------------------------------------------------- CRS helpers
def describe_crs(crs: CRS | str | None) -> str | None:
    """'EPSG:4326' when the CRS maps to an EPSG code, else WKT-derived name / string."""
    if crs is None:
        return None
    crs = CRS.from_user_input(crs)
    epsg = crs.to_epsg(min_confidence=70)
    if epsg:
        return f"EPSG:{epsg}"
    return crs.to_string() if len(crs.to_string()) < 200 else (crs.name or "CUSTOM")


def utm_epsg(lon: float, lat: float) -> int:
    if lat >= 84:
        return 32661  # UPS North
    if lat < -80:
        return 32761  # UPS South
    lon = ((lon + 180) % 360) - 180
    zone = min(int((lon + 180) // 6) + 1, 60)
    return (32600 if lat >= 0 else 32700) + zone


@lru_cache(maxsize=512)
def _transformer(src: str, dst: str) -> Transformer:
    return Transformer.from_crs(CRS.from_user_input(src), CRS.from_user_input(dst), always_xy=True)


def _laea(lon: float, lat: float) -> str:
    # Any centre is exactly equal-area; rounding to 1 degree lets transformers be cached.
    return (f"+proj=laea +lat_0={round(lat)} +lon_0={round(lon)} +x_0=0 +y_0=0 "
            "+datum=WGS84 +units=m +no_defs")


def _centroid_lonlat(geom: BaseGeometry, src_crs: str) -> tuple[float, float]:
    c = geom.representative_point() if geom.geom_type.endswith("Polygon") else geom.centroid
    lon, lat = _transformer(src_crs, "EPSG:4326").transform(c.x, c.y)
    if not (math.isfinite(lon) and math.isfinite(lat)) or abs(lat) > 90:
        raise ValueError("coordinates are outside the valid range for the source CRS")
    return lon, lat


def _project_coords(geom: BaseGeometry, t: Transformer) -> BaseGeometry:
    def fn(coords):
        x, y = t.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])
    return _shp_transform(geom, fn)


def _project(geom: BaseGeometry, src_crs: str, dst_crs: str) -> BaseGeometry:
    out = _project_coords(geom, _transformer(src_crs, dst_crs))
    minx, miny, maxx, maxy = out.bounds
    if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)):
        raise ValueError("reprojection produced non-finite coordinates")
    return out


# ---------------------------------------------------------------- main entry
def measure(geom: BaseGeometry | None, src_crs: str | None) -> Measurement:
    """Never raises: failures are returned as status=ERROR so one bad feature
    can't take down a whole upload."""
    if geom is None or geom.is_empty:
        return Measurement("UNSUPPORTED", message="feature has no geometry")
    gtype = geom.geom_type
    if gtype in NO_MEASURE_TYPES:
        return Measurement("NOT_APPLICABLE", message=f"{gtype} has no area or length")
    if gtype not in AREA_TYPES | LENGTH_TYPES:
        return Measurement("UNSUPPORTED", message=f"measurement not supported for {gtype}")
    if not src_crs:
        return Measurement("ERROR", message="source CRS unknown; cannot measure safely")

    try:
        geom = shapely.force_2d(geom)
        valid = bool(geom.is_valid)
        if not valid:
            geom = make_valid(geom)  # e.g. bow-tie polygons: avoid signed-area nonsense
        lon, lat = _centroid_lonlat(geom, src_crs)

        if gtype in AREA_TYPES:
            dst = _laea(lon, lat)
            area = _project(geom, src_crs, dst).area
            return Measurement("OK", measurement_crs=f"LAEA(lat0={round(lat)},lon0={round(lon)})",
                               area_sq_m=area, geometry_valid=valid,
                               message=None if valid else "invalid geometry repaired before measuring")
        dst = f"EPSG:{utm_epsg(lon, lat)}"
        length = _project(geom, src_crs, dst).length
        return Measurement("OK", measurement_crs=dst, length_m=length, geometry_valid=valid,
                           message=None if valid else "invalid geometry repaired before measuring")
    except Exception as exc:  # noqa: BLE001 - deliberate: isolate per-feature failures
        return Measurement("ERROR", message=f"measurement failed: {exc}")
