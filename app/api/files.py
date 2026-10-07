from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Feature, UploadedFile
from app.schemas import (FeatureOut, FeaturesResponse, FileOut, MeasurementOut, MeasurementsResponse,
                         MeasurementSummary, Page)
from app.services.ingest import IngestError
from app.services.processing import UploadTooLarge, process_upload

router = APIRouter(prefix="/api/files", tags=["files"])


def _get_file(session: Session, file_id: str) -> UploadedFile:
    f = session.get(UploadedFile, file_id)
    if f is None:
        raise HTTPException(404, "File not found")
    return f


def _require_completed(f: UploadedFile) -> None:
    if f.status != "COMPLETED":
        raise HTTPException(409, f"File status is {f.status}: {f.error or 'not ready'}")


@router.post("/", response_model=FileOut, status_code=status.HTTP_201_CREATED,
             summary="Upload a Shapefile (.zip) or KML and process it")
def upload_file(file: UploadFile = File(...), session: Session = Depends(get_session)):
    try:
        record = process_upload(session, file)
    except UploadTooLarge:
        raise HTTPException(413, "File too large")
    except IngestError as exc:
        raise HTTPException(400, str(exc))
    if record.status == "FAILED":
        # Persisted so the failure can be inspected via GET /api/files/{id}/
        body = FileOut.model_validate(record).model_dump(mode="json")
        return JSONResponse(status_code=422, content={"detail": record.error, **body})
    return record


@router.get("/", response_model=list[FileOut], summary="List uploaded files")
def list_files(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
               session: Session = Depends(get_session)):
    q = select(UploadedFile).order_by(UploadedFile.created_at.desc()).limit(limit).offset(offset)
    return session.scalars(q).all()


@router.get("/{file_id}/", response_model=FileOut, summary="File information")
def file_info(file_id: str, session: Session = Depends(get_session)):
    return _get_file(session, file_id)


@router.delete("/{file_id}/", status_code=204, summary="Delete a file and its features")
def delete_file(file_id: str, session: Session = Depends(get_session)):
    session.delete(_get_file(session, file_id))
    session.commit()


@router.get("/{file_id}/features/", response_model=FeaturesResponse,
            summary="Extracted features: id, geometry type, geometry (GeoJSON, source CRS), CRS, properties")
def list_features(file_id: str, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                  session: Session = Depends(get_session)):
    f = _get_file(session, file_id)
    _require_completed(f)
    rows = session.scalars(select(Feature).where(Feature.file_id == file_id)
                           .order_by(Feature.index).limit(limit).offset(offset)).all()
    feats = [FeatureOut(id=r.index, layer=r.layer, geometry_type=r.geometry_type, geometry=r.geometry,
                        crs=r.crs, properties=r.properties) for r in rows]
    return FeaturesResponse(file_id=file_id, page=Page(total=f.feature_count, limit=limit, offset=offset),
                            features=feats)


@router.get("/{file_id}/measurements/", response_model=MeasurementsResponse,
            summary="Per-feature area / length, computed in a local projected CRS")
def measurements(file_id: str, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                 geometry_type: str | None = Query(None, description="Filter, e.g. Polygon"),
                 session: Session = Depends(get_session)):
    f = _get_file(session, file_id)
    _require_completed(f)

    base = select(Feature).where(Feature.file_id == file_id)
    if geometry_type:
        base = base.where(Feature.geometry_type == geometry_type)
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = session.scalars(base.order_by(Feature.index).limit(limit).offset(offset)).all()

    counts = dict(session.execute(
        select(Feature.measurement_status, func.count()).where(Feature.file_id == file_id)
        .group_by(Feature.measurement_status)).all())
    tot_area, tot_len = session.execute(
        select(func.coalesce(func.sum(Feature.area_sq_m), 0.0), func.coalesce(func.sum(Feature.length_m), 0.0))
        .where(Feature.file_id == file_id)).one()

    items = [MeasurementOut(
        id=r.index, layer=r.layer, geometry_type=r.geometry_type, crs=r.crs, status=r.measurement_status,
        measurement_crs=r.measurement_crs, area_sq_m=r.area_sq_m,
        area_hectares=None if r.area_sq_m is None else r.area_sq_m / 10_000,
        length_m=r.length_m, geometry_valid=r.geometry_valid, message=r.message) for r in rows]
    return MeasurementsResponse(
        file_id=file_id,
        summary=MeasurementSummary(total_area_sq_m=tot_area, total_length_m=tot_len,
                                   measured=counts.get("OK", 0), not_applicable=counts.get("NOT_APPLICABLE", 0),
                                   unsupported=counts.get("UNSUPPORTED", 0), errors=counts.get("ERROR", 0)),
        page=Page(total=total, limit=limit, offset=offset), measurements=items)
