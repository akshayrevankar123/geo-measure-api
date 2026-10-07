import io
import os
import tempfile
import zipfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["GEO_DB_URL"] = f"sqlite:///{_tmp}/test.db"

import numpy as np  # noqa: E402
import pytest  # noqa: E402
import shapely  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pyogrio.raw import write as raw_write  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


def kml_bytes(placemarks: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2">'
            f"<Document><name>t</name>{placemarks}</Document></kml>").encode()


def placemark(name, geom_xml):
    return f"<Placemark><name>{name}</name>{geom_xml}</Placemark>"


def poly_xml(coords):
    s = " ".join(f"{x},{y},0" for x, y in coords)
    return f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{s}</coordinates></LinearRing></outerBoundaryIs></Polygon>"


def line_xml(coords):
    s = " ".join(f"{x},{y},0" for x, y in coords)
    return f"<LineString><coordinates>{s}</coordinates></LineString>"


def point_xml(x, y):
    return f"<Point><coordinates>{x},{y},0</coordinates></Point>"


def shapefile_zip(geoms, crs: str | None, geometry_type="Polygon", names=None) -> bytes:
    d = Path(tempfile.mkdtemp())
    wkb = np.array([shapely.to_wkb(g) for g in geoms], dtype=object)
    names = names or [f"f{i}" for i in range(len(geoms))]
    raw_write(str(d / "layer.shp"), geometry=wkb, field_data=[np.array(names, dtype=object)],
              fields=["name"], geometry_type=geometry_type, crs=crs, driver="ESRI Shapefile")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in d.iterdir():
            zf.write(p, p.name)
    return buf.getvalue()
