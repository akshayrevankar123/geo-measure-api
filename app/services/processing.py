"""Orchestrates: save upload -> parse -> measure -> persist."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Feature, UploadedFile
from app.services.ingest import IngestError, detect_kind, parse_file, to_geojson
from app.services.measure import measure


class UploadTooLarge(Exception):
    pass


def _save_stream(upload: UploadFile, dest: Path) -> None:
    written = 0
    with dest.open("wb") as out:
        while chunk := upload.file.read(1024 * 1024):
            written += len(chunk)
            if written > settings.max_upload_bytes:
                raise UploadTooLarge()
            out.write(chunk)


def process_upload(session: Session, upload: UploadFile) -> UploadedFile:
    """Returns the persisted UploadedFile (status COMPLETED or FAILED).
    Raises IngestError for unsupported types (nothing persisted) and UploadTooLarge."""
    filename = Path(upload.filename or "upload").name
    kind = detect_kind(filename)  # may raise IngestError before any work is done

    record = UploadedFile(filename=filename, file_type=kind, status="PROCESSING")
    workdir = Path(tempfile.mkdtemp(prefix="geo-"))
    try:
        src = workdir / ("upload" + Path(filename).suffix.lower())
        _save_stream(upload, src)

        try:
            parsed = parse_file(src, kind, workdir)
        except IngestError as exc:
            record.status, record.error = "FAILED", str(exc)
            session.add(record)
            session.commit()
            return record

        crs_values = {f.crs for f in parsed.features}
        record.crs = crs_values.pop() if len(crs_values) == 1 else "MIXED"
        record.feature_count = len(parsed.features)

        rows = []
        for idx, f in enumerate(parsed.features):
            m = measure(f.geometry, f.crs)
            rows.append(Feature(
                index=idx, layer=f.layer, crs=f.crs,
                geometry_type=f.geometry.geom_type if f.geometry is not None else None,
                geometry=to_geojson(f.geometry), properties=f.properties,
                measurement_status=m.status, measurement_crs=m.measurement_crs,
                area_sq_m=m.area_sq_m, length_m=m.length_m,
                geometry_valid=m.geometry_valid, message=m.message,
            ))
        record.features = rows
        record.status = "COMPLETED"
        session.add(record)
        session.commit()
        return record
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
