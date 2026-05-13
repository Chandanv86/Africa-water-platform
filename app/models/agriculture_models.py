"""Pydantic v2 schemas for the agriculture / food-security analytics module.

Every extractor returns a standardised record that includes datatype, source,
year, value, unit, confidence, coverage, status, method, timestamp, and notes.
Specialised sub-schemas exist for data types that carry richer structure
(cropland transitions, phenology profiles, food-security classifications).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.models.water_models import (
    AreaStats,
    GeoPoint,
    GeometryInput,
    MethodologyItem,
    SourceRef,
)


# ---------------------------------------------------------------------------
# Canonical yearly record — every data type maps to this
# ---------------------------------------------------------------------------

class YearlyRecord(BaseModel):
    """One metric for one year for one data type."""

    datatype: str = Field(
        ...,
        description="Identifier: cropland_extent | cropland_conversion | phenology | food_security",
    )
    description: str = Field(..., description="Human-readable label")
    source: str = Field(..., description="Primary data source")
    year: int
    value: Optional[float] = Field(
        None,
        description="Metric value. None when unavailable — NEVER zero as a substitute for missing.",
    )
    unit: str = Field(..., description="km2 | km2/yr | ndvi | ipc_phase")
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    coverage: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="Fraction of AOI with valid observations",
    )
    status: str = Field(
        "unavailable",
        description="ok | partial | unavailable | insufficient_data",
    )
    method: str = Field("", description="Exact processing recipe used")
    timestamp: str = Field("", description="ISO date of data vintage")
    notes: List[str] = Field(default_factory=list)


class YearOverYearChange(BaseModel):
    """Change in a yearly metric compared with the previous available year."""

    datatype: str
    year: int
    previous_year: int
    value_current: Optional[float] = None
    value_previous: Optional[float] = None
    absolute_change: Optional[float] = None
    percent_change: Optional[float] = None
    unit: str = ""
    status: str = Field("unavailable", description="ok | unavailable")
    notes: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Cropland gain / loss / net — keeps gain and loss SEPARATE
# ---------------------------------------------------------------------------

class CroplandTransition(BaseModel):
    """Year-over-year cropland change between *previous_year* and *year*."""

    year: int
    previous_year: int
    gain_km2: Optional[float] = Field(
        None, description="Area converted TO cropland (non-crop → crop)"
    )
    loss_km2: Optional[float] = Field(
        None, description="Area converted FROM cropland (crop → non-crop)"
    )
    net_change_km2: Optional[float] = Field(
        None, description="gain - loss"
    )
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    coverage: float = Field(0.0, ge=0.0, le=1.0)
    status: str = Field(
        "unavailable",
        description="ok | partial | low_confidence | unavailable",
    )
    method: str = ""
    notes: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Monthly phenology profile — cloud-masked composites only
# ---------------------------------------------------------------------------

class PhenologyRecord(BaseModel):
    """Yearly crop phenology derived from cloud-masked Sentinel-2."""

    year: int
    monthly_ndvi: List[Optional[float]] = Field(
        default_factory=lambda: [None] * 12,
        description="12 values (index 0 = Jan). None for months lacking valid scenes.",
    )
    monthly_evi: List[Optional[float]] = Field(
        default_factory=lambda: [None] * 12,
    )
    season_start_month: Optional[int] = Field(
        None, ge=1, le=12, description="First month NDVI > 0.3"
    )
    peak_month: Optional[int] = Field(None, ge=1, le=12)
    season_end_month: Optional[int] = Field(
        None, ge=1, le=12, description="Last month NDVI > 0.3"
    )
    peak_ndvi: Optional[float] = None
    amplitude: Optional[float] = Field(
        None, description="peak NDVI − trough NDVI"
    )
    valid_month_count: int = Field(
        0,
        description="Months with ≥ 1 valid composite pixel over the AOI",
    )
    scene_count: int = 0
    status: str = Field(
        "unavailable",
        description="ok (≥6 months) | partial (3-5) | insufficient_data (<3) | unavailable",
    )
    method: str = ""
    notes: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Food security — categorical IPC classification, NEVER continuous
# ---------------------------------------------------------------------------

IPC_LABELS: Dict[int, str] = {
    1: "Minimal",
    2: "Stressed",
    3: "Crisis",
    4: "Emergency",
    5: "Famine",
}


class FoodSecurityRecord(BaseModel):
    """FEWS NET IPC-phase classification for one year.

    The *phase* field is an integer 1-5 following the IPC scale.
    It is NEVER converted to a continuous score.
    """

    year: int
    phase: Optional[int] = Field(
        None,
        ge=1,
        le=5,
        description="IPC phase 1-5.  None when unavailable.",
    )
    phase_label: Optional[str] = Field(
        None,
        description="Human label: Minimal / Stressed / Crisis / Emergency / Famine",
    )
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    source_region: Optional[str] = Field(
        None, description="FEWS NET admin-zone name used for the lookup"
    )
    lookup_method: str = Field(
        "centroid-nearest zone",
        description="How the FEWS NET zone was selected — this is an approximation, not full AOI coverage",
    )
    status: str = Field(
        "unavailable",
        description="ok | unavailable",
    )
    notes: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level response
# ---------------------------------------------------------------------------

class AgricultureAnalysisResponse(BaseModel):
    """Full agriculture / food-security analytics for an AOI over a year range."""

    geometry: GeometryInput
    stats: AreaStats
    centroid: GeoPoint
    label: Optional[str] = None
    year_range: List[int] = Field(
        default_factory=list, description="Years included in the analysis"
    )

    cropland_extent: List[YearlyRecord] = Field(default_factory=list)
    cropland_conversion: List[CroplandTransition] = Field(default_factory=list)
    phenology: List[PhenologyRecord] = Field(default_factory=list)
    food_security: List[FoodSecurityRecord] = Field(default_factory=list)
    yearly_changes: List[YearOverYearChange] = Field(default_factory=list)

    summary: Dict[str, Any] = Field(
        default_factory=dict,
        description="Latest values + trend direction",
    )
    methodology: List[MethodologyItem] = Field(default_factory=list)
    sources: List[SourceRef] = Field(default_factory=list)
    status: str = Field("unavailable", description="ok | partial | unavailable")
    notes: List[str] = Field(default_factory=list)
