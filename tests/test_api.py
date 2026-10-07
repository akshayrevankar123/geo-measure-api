import io
import zipfile

import pytest
from pyproj import Geod
from shapely.geometry import LineString, Polygon

from tests.conftest import kml_bytes, line_xml, placemark, point_xml, poly_xml, shapefile_zip

GEOD = Geod(ellps="WGS84")


def upload(client, name, data):
    return client.post("/api/files/", files={"file": (name, data)})


def rel_err(a, b):
    return abs(a - b) / b


# ---------------------------------------------------------------- KML
def test_kml_end_to_end(client):
    sq = [(77.0, 12.0), (77.01, 12.0), (77.01, 12.01), (77.0, 12.01), (77.0, 12.0)]
    ln = [(77.0, 12.0), (77.05, 12.02), (77.1, 12.0)]
    kml = kml_bytes(placemark("plot", poly_xml(sq)) + placemark("road", line_xml(ln)) +
                    placemark("well", point_xml(77.0, 12.0)))
    r = upload(client, "survey.kml", kml)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "COMPLETED" and body["feature_count"] == 3 and body["crs"] == "EPSG:4326"
    fid = body["id"]
    assert client.get(f"/api/files/{fid}/").json()["filename"] == "survey.kml"

    m = client.get(f"/api/files/{fid}/measurements/").json()
    by_type = {x["geometry_type"]: x for x in m["measurements"]}
    # accuracy vs ellipsoidal ground truth
    true_area, _ = GEOD.geometry_area_perimeter(Polygon(sq))
    assert rel_err(by_type["Polygon"]["area_sq_m"], abs(true_area)) < 1e-3
    assert rel_err(by_type["LineString"]["length_m"], GEOD.geometry_length(LineString(ln))) < 1e-3
    assert by_type["LineString"]["measurement_crs"] == "EPSG:32643"  # UTM 43N
    assert by_type["Point"]["status"] == "NOT_APPLICABLE"
    assert m["summary"]["measured"] == 2 and m["summary"]["not_applicable"] == 1

    feats = client.get(f"/api/files/{fid}/features/").json()["features"]
    assert [f["geometry_type"] for f in feats] == ["Polygon", "LineString", "Point"]
    assert feats[0]["properties"]["Name"] == "plot" and feats[0]["crs"] == "EPSG:4326"
    assert feats[0]["geometry"]["type"] == "Polygon"


def test_degrees_are_not_used_high_latitude(client):
    """At 60N a 0.01x0.01 deg cell is ~half as wide as tall; naive degree maths is 2x off."""
    sq = [(10.0, 60.0), (10.01, 60.0), (10.01, 60.01), (10.0, 60.01), (10.0, 60.0)]
    fid = upload(client, "hi.kml", kml_bytes(placemark("p", poly_xml(sq)))).json()["id"]
    area = client.get(f"/api/files/{fid}/measurements/").json()["measurements"][0]["area_sq_m"]
    assert rel_err(area, abs(GEOD.geometry_area_perimeter(Polygon(sq))[0])) < 1e-3
    assert area > 1e5  # degrees^2 would give 1e-4


def test_multipolygon_and_southern_hemisphere(client):
    a = [(150.0, -33.0), (150.01, -33.0), (150.01, -33.01), (150.0, -33.01), (150.0, -33.0)]
    b = [(150.02, -33.0), (150.03, -33.0), (150.03, -33.01), (150.02, -33.01), (150.02, -33.0)]
    xml = "<MultiGeometry>" + poly_xml(a) + poly_xml(b) + "</MultiGeometry>"
    fid = upload(client, "m.kml", kml_bytes(placemark("mp", xml))).json()["id"]
    m = client.get(f"/api/files/{fid}/measurements/").json()["measurements"][0]
    assert m["geometry_type"] == "MultiPolygon"
    truth = abs(GEOD.geometry_area_perimeter(Polygon(a))[0]) + abs(GEOD.geometry_area_perimeter(Polygon(b))[0])
    assert rel_err(m["area_sq_m"], truth) < 1e-3


def test_unsupported_geometry_is_graceful(client):
    xml = ("<MultiGeometry>" + point_xml(77, 12) + line_xml([(77, 12), (77.1, 12.1)]) + "</MultiGeometry>")
    fid = upload(client, "gc.kml", kml_bytes(placemark("mixed", xml) + placemark("nogeom", ""))).json()["id"]
    m = client.get(f"/api/files/{fid}/measurements/").json()
    assert {x["status"] for x in m["measurements"]} <= {"UNSUPPORTED", "NOT_APPLICABLE"}
    assert all(x["message"] for x in m["measurements"])


# ---------------------------------------------------------------- Shapefile
def test_shapefile_geographic(client):
    sq = Polygon([(77.0, 12.0), (77.01, 12.0), (77.01, 12.01), (77.0, 12.01)])
    r = upload(client, "s.zip", shapefile_zip([sq], "EPSG:4326"))
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    m = client.get(f"/api/files/{fid}/measurements/").json()["measurements"][0]
    assert rel_err(m["area_sq_m"], abs(GEOD.geometry_area_perimeter(sq)[0])) < 1e-3


def test_shapefile_projected_utm(client):
    sq = Polygon([(500000, 1000000), (501000, 1000000), (501000, 1001000), (500000, 1001000)])
    fid = upload(client, "u.zip", shapefile_zip([sq], "EPSG:32643")).json()["id"]
    body = client.get(f"/api/files/{fid}/").json()
    assert body["crs"] == "EPSG:32643"
    m = client.get(f"/api/files/{fid}/measurements/").json()["measurements"][0]
    assert rel_err(m["area_sq_m"], 1_000_000) < 2e-3  # 1 km^2 give or take UTM scale factor


def test_web_mercator_is_not_trusted(client):
    """EPSG:3857 is 'projected' but area is hugely inflated away from the equator."""
    from pyproj import Transformer
    t = Transformer.from_crs(4326, 3857, always_xy=True)
    ring = [t.transform(x, y) for x, y in [(10, 60), (10.01, 60), (10.01, 60.01), (10, 60.01)]]
    sq = Polygon(ring)
    fid = upload(client, "wm.zip", shapefile_zip([sq], "EPSG:3857")).json()["id"]
    area = client.get(f"/api/files/{fid}/measurements/").json()["measurements"][0]["area_sq_m"]
    truth = abs(GEOD.geometry_area_perimeter(Polygon([(10, 60), (10.01, 60), (10.01, 60.01), (10, 60.01)]))[0])
    assert rel_err(area, truth) < 1e-3 and sq.area > 3 * truth


def test_invalid_bowtie_polygon_flagged(client):
    bow = Polygon([(77.0, 12.0), (77.01, 12.01), (77.01, 12.0), (77.0, 12.01)])
    fid = upload(client, "b.zip", shapefile_zip([bow], "EPSG:4326")).json()["id"]
    m = client.get(f"/api/files/{fid}/measurements/").json()["measurements"][0]
    assert m["status"] == "OK" and m["geometry_valid"] is False and m["area_sq_m"] > 0


def test_shapefile_without_crs_fails_cleanly(client):
    r = upload(client, "nocrs.zip", shapefile_zip([Polygon([(0, 0), (1, 0), (1, 1)])], None))
    assert r.status_code == 422
    fid = r.json()["id"]
    assert client.get(f"/api/files/{fid}/").json()["status"] == "FAILED"
    assert client.get(f"/api/files/{fid}/measurements/").status_code == 409


# ---------------------------------------------------------------- errors / API behaviour
def test_unsupported_extension(client):
    assert upload(client, "x.geojson", b"{}").status_code == 400


def test_corrupt_inputs(client):
    assert upload(client, "bad.zip", b"not a zip").status_code == 422
    assert upload(client, "bad.kml", b"<kml><oops").status_code == 422
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "no shapefile here")
    assert upload(client, "empty.zip", buf.getvalue()).status_code == 422


def test_zip_slip_rejected(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.shp", "x")
    r = upload(client, "evil.zip", buf.getvalue())
    assert r.status_code == 422 and "unsafe" in r.json()["detail"]


def test_not_found_and_pagination_and_delete(client):
    assert client.get("/api/files/nope/").status_code == 404
    polys = [Polygon([(77 + i * 0.1, 12), (77.01 + i * 0.1, 12), (77.01 + i * 0.1, 12.01)]) for i in range(5)]
    fid = upload(client, "p.zip", shapefile_zip(polys, "EPSG:4326")).json()["id"]
    page = client.get(f"/api/files/{fid}/measurements/?limit=2&offset=3").json()
    assert [m["id"] for m in page["measurements"]] == [3, 4] and page["page"]["total"] == 5
    assert page["summary"]["measured"] == 5
    assert client.delete(f"/api/files/{fid}/").status_code == 204
    assert client.get(f"/api/files/{fid}/").status_code == 404


def test_file_listing(client):
    assert client.get("/api/files/").status_code == 200
