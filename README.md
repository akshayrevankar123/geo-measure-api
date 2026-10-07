# Geospatial File Measurement API

FastAPI service that accepts a **Shapefile (.zip)** or **KML**, extracts every feature
(index, geometry type, geometry, CRS, properties) and returns **area** (polygons) and **length**
(lines) computed in a *projected metric CRS* - never in degrees.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # http://localhost:8000  (Swagger UI at /docs)
pytest                                  # run the test-suite
```

Docker: `docker build -t geo-api . && docker run -p 8000:8000 -v geo-data:/data geo-api`

Config (env vars): `GEO_DB_URL` (default `sqlite:///./data/geo.db`), `GEO_MAX_UPLOAD_MB` (50),
`GEO_MAX_UNZIPPED_MB` (500), `GEO_MAX_ZIP_MEMBERS` (200), `GEO_MAX_FEATURES` (200000).

Wheels for `shapely`, `pyproj` and `pyogrio` bundle GDAL/PROJ, so no system GDAL install is needed.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/files/` | Upload `.zip` (Shapefile) or `.kml` as multipart field `file`, process synchronously |
| GET | `/api/files/` | List uploads |
| GET | `/api/files/{id}/` | File info |
| GET | `/api/files/{id}/features/` | Extracted features (GeoJSON geometry in the source CRS, properties) |
| GET | `/api/files/{id}/measurements/` | Per-feature measurements + totals (`limit`, `offset`, `geometry_type` filter) |
| DELETE | `/api/files/{id}/` | Delete a file and its features |

```bash
curl -F file=@examples/survey.kml http://localhost:8000/api/files/
```
```json
{"id":"ca92fc5a9f1e486daf02c05c4791a984","filename":"survey.kml","file_type":"kml",
 "feature_count":3,"crs":"EPSG:4326","status":"COMPLETED","error":null,"created_at":"..."}
```

`GET /api/files/{id}/measurements/`
```json
{
  "file_id": "ca92...",
  "summary": {"total_area_sq_m": 1204684.28, "total_length_m": 11757.07,
              "measured": 2, "not_applicable": 1, "unsupported": 0, "errors": 0},
  "page": {"total": 3, "limit": 100, "offset": 0},
  "measurements": [
    {"id": 0, "geometry_type": "Polygon", "crs": "EPSG:4326", "status": "OK",
     "measurement_crs": "LAEA(lat0=12,lon0=77)", "area_sq_m": 1204684.28, "area_hectares": 120.47, "length_m": null},
    {"id": 1, "geometry_type": "LineString", "crs": "EPSG:4326", "status": "OK",
     "measurement_crs": "EPSG:32643", "area_sq_m": null, "length_m": 11757.07},
    {"id": 2, "geometry_type": "Point", "status": "NOT_APPLICABLE", "message": "Point has no area or length"}
  ]
}
```

`GET /api/files/{id}/features/` returns `{"id": 0, "geometry_type": "Polygon", "geometry": {GeoJSON}, "crs": "EPSG:4326", "properties": {...}}` per feature.

**Measurement `status`:** `OK` | `NOT_APPLICABLE` (Point/MultiPoint) | `UNSUPPORTED` (GeometryCollection, empty geometry) | `ERROR` (e.g. coordinates impossible for the CRS). One bad feature never fails the upload.

**Errors:** `400` unsupported extension - `413` too large - `422` unreadable file (corrupt zip/KML, no `.shp`, shapefile with no CRS, zip-slip). A 422 upload is still recorded as `FAILED` (see `GET /api/files/{id}/`); its `/measurements/` returns `409`. Unknown id: `404`.

## Architecture

```
app/
  main.py              FastAPI app, router, table creation on startup
  api/files.py         HTTP layer only (validation, status codes, pagination)
  services/processing.py  upload -> temp dir -> parse -> measure -> persist
  services/ingest.py   Shapefile/KML reading (pyogrio/GDAL), safe zip extraction
  services/measure.py  CRS selection + measurement (pure functions, no I/O)
  models.py / db.py    SQLAlchemy 2 (SQLite by default; any SQLAlchemy URL works)
  schemas.py           Pydantic response models
```

**File-processing flow**: stream upload to a temp dir (size-capped) -> detect type by extension ->
for `.zip`: safe-extract (zip-slip, member-count and unzipped-size guards) and read every `.shp`;
for `.kml`: read every layer (folder) -> each feature becomes a record (2D geometry, JSON-safe properties,
layer CRS; KML is always EPSG:4326) -> measure -> store file + features in one transaction -> temp dir deleted.

**Measurement flow**: per feature, skip Points/unsupported types -> repair invalid geometry with `make_valid`
(flagged `geometry_valid: false`) -> pick a metric CRS from the feature's location -> reproject -> `.area` / `.length`.

**CRS handling**
- Source CRS is read from the file (`.prj` for shapefiles) and reported as `EPSG:xxxx` when identifiable.
- Always reprojected, even if the source is already projected: EPSG:3857 is "projected" yet inflates area ~4x at 60 N, and a state-plane CRS may be in feet.
- **Area** -> Lambert Azimuthal Equal-Area centred on the feature (rounded to 1 degree so transformers are cached). Exactly area-preserving for any feature size.
- **Length** -> UTM zone of the feature centroid (EPSG:326xx / 327xx), UPS beyond 84 N / 80 S.
- Transformers use `always_xy=True` to avoid lat/lon axis-order bugs.
- Tests check results against ellipsoidal ground truth (`pyproj.Geod`) within 0.1 %.

## Design decisions

- **FastAPI over Django/DRF**: small, API-only service; automatic OpenAPI docs and Pydantic validation, no ORM/admin needed.
- **pyogrio + shapely + pyproj instead of GeoPandas**: same GDAL under the hood, far lighter install, and I only needed rows + WKB, not dataframes.
- **Measure at upload time, store results**: measurement is deterministic, so GET endpoints are cheap, paginated SQL reads and totals are SQL aggregates. Trade-off: a later change to the algorithm needs re-processing.
- **Synchronous processing**: simplest correct thing for files up to tens of MB (the sync endpoint runs in FastAPI's threadpool). The `status` field is already there for a queue-based version (see future scope).
- **Per-feature CRS choice (LAEA/UTM) vs. one CRS per file**: one UTM zone fails for files spanning zones; per-feature is more accurate at tiny cost. Alternative considered: pure geodesic (`pyproj.Geod`) - most accurate, but the brief asks for projected measurement, so I use geodesics only as the test oracle.
- **Shapefile without a CRS is rejected** rather than guessed; guessing units silently gives wrong numbers.
- **SQLite + JSON columns** for zero-setup; swap `GEO_DB_URL` for Postgres. Geometry is stored as GeoJSON in the source CRS.
- **Invalid polygons are repaired for measurement**, not rejected - real survey data often has bow-ties/slivers.

## Learnings

- "Projected" does not mean "safe to measure in" (Web Mercator).
- GDAL/PROJ axis order: pass `always_xy=True` everywhere.
- KML polygon syntax is `outerBoundaryIs`; a misspelt tag silently yields an *empty* polygon rather than an error - the API reports that as `UNSUPPORTED: feature has no geometry`.
- Testing against a geodesic oracle catches CRS mistakes far better than hand-computed expectations.

## Future scope

- Background processing (Celery/RQ) with `PROCESSING` polling and webhooks for large files.
- More formats: GeoJSON, KMZ, GeoPackage, multi-file shapefile upload; PostGIS storage for spatial queries.
- Perimeter, centroid, bbox, and unit options (acres, km, feet).
- Authentication, per-user quotas, file retention/cleanup job.
- Antimeridian-crossing and Norway/Svalbard UTM special-zone handling; GeometryCollection support by summing members.
- Auth'd streaming of very large files, CI pipeline and a Postgres integration test.
