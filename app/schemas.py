from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    filename: str
    file_type: str
    feature_count: int
    crs: str | None
    status: str
    error: str | None = None
    created_at: datetime


class FeatureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int  # feature index within the file (0-based)
    layer: str | None
    geometry_type: str | None
    geometry: dict[str, Any] | None
    crs: str | None
    properties: dict[str, Any]


class MeasurementOut(BaseModel):
    id: int
    layer: str | None
    geometry_type: str | None
    crs: str | None
    status: str  # OK | NOT_APPLICABLE | UNSUPPORTED | ERROR
    measurement_crs: str | None
    area_sq_m: float | None = None
    area_hectares: float | None = None
    length_m: float | None = None
    geometry_valid: bool | None = None
    message: str | None = None


class MeasurementSummary(BaseModel):
    total_area_sq_m: float
    total_length_m: float
    measured: int
    not_applicable: int
    unsupported: int
    errors: int


class Page(BaseModel):
    total: int
    limit: int
    offset: int


class FeaturesResponse(BaseModel):
    file_id: str
    page: Page
    features: list[FeatureOut]


class MeasurementsResponse(BaseModel):
    file_id: str
    summary: MeasurementSummary
    page: Page
    measurements: list[MeasurementOut]
