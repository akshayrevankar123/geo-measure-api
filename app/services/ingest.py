"""Reading Shapefile (.zip) and KML uploads into plain Python feature records."""
from __future__ import annotations

import datetime as dt
import math
import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import shapely
from pyogrio import list_layers, read_info
from pyogrio.raw import read as raw_read
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from app.config import settings
from app.services.measure import describe_crs


class IngestError(Exception):
    """Raised for files that are structurally unusable (maps to HTTP 422)."""


@dataclass
class ParsedFeature:
    layer: str | None
    geometry: BaseGeometry | None
    properties: dict
    crs: str | None


@dataclass
class ParsedFile:
    kind: str
    features: list[ParsedFeature] = field(default_factory=list)


def detect_kind(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".zip":
        return "shapefile"
    if ext == ".kml":
        return "kml"
    raise IngestError("Unsupported file type. Upload a .zip containing a Shapefile, or a .kml file.")


def safe_extract(zip_path: Path, dest: Path) -> list[Path]:
    """Extract with zip-slip and zip-bomb protection; returns .shp paths found."""
    if not zipfile.is_zipfile(zip_path):
        raise IngestError("File is not a valid zip archive.")
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > settings.max_zip_members:
            raise IngestError("Zip archive contains too many files.")
        if sum(i.file_size for i in infos) > settings.max_unzipped_bytes:
            raise IngestError("Zip archive is too large when extracted.")
        root = dest.resolve()
        for info in infos:
            target = (dest / info.filename).resolve()
            if root != target and root not in target.parents:
                raise IngestError("Zip archive contains an unsafe path.")
        zf.extractall(dest)
    shps = sorted(p for p in dest.rglob("*") if p.suffix.lower() == ".shp" and "__MACOSX" not in p.parts)
    if not shps:
        raise IngestError("No .shp file found inside the zip archive.")
    return shps


def _json_safe(value):
    if value is None:
        return None
    if isinstance(value, (np.datetime64,)):
        return None if np.isnat(value) else str(value)
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _read_layer(path: Path, layer: str | None, kind: str) -> list[ParsedFeature]:
    info = read_info(path, layer=layer)
    crs = describe_crs(info.get("crs"))
    if crs is None and kind == "kml":
        crs = "EPSG:4326"  # KML is defined to be WGS84 lon/lat
    meta, _fids, geoms, field_data = raw_read(path, layer=layer, force_2d=True)
    names = list(meta["fields"])
    out: list[ParsedFeature] = []
    for i in range(len(geoms)):
        geom = shapely.from_wkb(geoms[i]) if geoms[i] is not None else None
        props = {n: _json_safe(field_data[j][i]) for j, n in enumerate(names)}
        out.append(ParsedFeature(layer=layer, geometry=geom, properties=props, crs=crs))
    return out


def parse_file(path: Path, kind: str, workdir: Path) -> ParsedFile:
    parsed = ParsedFile(kind=kind)
    try:
        if kind == "shapefile":
            targets = [(p, None) for p in safe_extract(path, workdir / "unzipped")]
        else:
            layers = [str(name) for name, _ in list_layers(path)]
            targets = [(path, name) for name in layers]
            if not targets:
                raise IngestError("KML contains no layers.")
        for p, layer in targets:
            parsed.features.extend(_read_layer(p, layer, kind))
            if len(parsed.features) > settings.max_features:
                raise IngestError(f"File has more than {settings.max_features} features.")
    except IngestError:
        raise
    except Exception as exc:  # GDAL/pyogrio errors on corrupt data
        raise IngestError(f"Could not read geospatial data: {exc}") from exc

    if not parsed.features:
        raise IngestError("File contains no features.")
    if kind == "shapefile" and any(f.crs is None for f in parsed.features):
        raise IngestError("Shapefile has no CRS (.prj missing or unreadable); refusing to guess units.")
    return parsed


def to_geojson(geom: BaseGeometry | None) -> dict | None:
    return None if geom is None else mapping(geom)
