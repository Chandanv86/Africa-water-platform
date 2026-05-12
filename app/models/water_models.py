from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class GeoPoint(BaseModel):
    lat: float
    lon: float


class GeometryInput(BaseModel):
    type: str
    coordinates: Any


class WaterFeatureContext(BaseModel):
    name: str = "Unknown"
    type: str = "Unknown"
    distance_km: Optional[float] = None
    source_dataset: str = "Natural Earth"
    geometry_type: Optional[str] = None
    feature_id: Optional[str] = None


class TimelinePoint(BaseModel):
    year: int
    water_class: Optional[int] = None
    permanent: bool = False
    seasonal: bool = False
    occurrence_pct: Optional[float] = None
    value: Optional[float] = None
    note: Optional[str] = None


class LayerAnalysis(BaseModel):
    status: str = "unknown"
    score: Optional[float] = None
    value: Optional[float] = None
    severity: Optional[float] = None
    confidence: float = 0.0
    timestamp: Optional[str] = None
    metrics: Dict[str, Any] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list)


class SourceRef(BaseModel):
    name: str
    collection: Optional[str] = None
    url: Optional[str] = None
    timestamp: Optional[str] = None
    kind: Optional[str] = None
    notes: Optional[str] = None


class MethodologyItem(BaseModel):
    title: str
    description: str


class AreaStats(BaseModel):
    area_km2: float
    perimeter_km: Optional[float] = None
    centroid: GeoPoint
    bbox: Optional[List[float]] = None


class BaseIntelligenceResponse(BaseModel):
    summary_card: str
    nearest_water_body: Optional[WaterFeatureContext] = None
    source_dataset: str = "Mixed EO + Vector Context"
    data_timestamp: Optional[str] = None

    flood: LayerAnalysis
    turbidity: LayerAnalysis
    chlorophyll: LayerAnalysis
    water_quality: LayerAnalysis
    soil_moisture: LayerAnalysis
    drought: LayerAnalysis
    glacier: LayerAnalysis

    historical_timeline: List[TimelinePoint] = Field(default_factory=list)
    trend_summary: Dict[str, Any] = Field(default_factory=dict)
    flags: List[str] = Field(default_factory=list)
    sources: List[SourceRef] = Field(default_factory=list)
    methodology: List[MethodologyItem] = Field(default_factory=list)
    nearby_context: Dict[str, Any] = Field(default_factory=dict)
    uncertainty: Dict[str, Any] = Field(default_factory=dict)
    download_links: Dict[str, str] = Field(default_factory=dict)


class PointAnalysisResponse(BaseIntelligenceResponse):
    location: GeoPoint


class AOIAnalysisResponse(BaseIntelligenceResponse):
    geometry: GeometryInput
    stats: AreaStats
    centroid: GeoPoint
    label: Optional[str] = None


class WaterTimelineResponse(BaseModel):
    location: GeoPoint
    status: str = "ok"
    reason: Optional[str] = None
    timeline: List[TimelinePoint] = Field(default_factory=list)
    yearly: List[Dict[str, Any]] = Field(default_factory=list)
    monthly: List[Dict[str, Any]] = Field(default_factory=list)
    flood_history: List[Dict[str, Any]] = Field(default_factory=list)
    flood_yearly_trends: List[Dict[str, Any]] = Field(default_factory=list)
    turbidity_trends: List[Dict[str, Any]] = Field(default_factory=list)
    turbidity_yearly_trends: List[Dict[str, Any]] = Field(default_factory=list)
    chlorophyll_trends: List[Dict[str, Any]] = Field(default_factory=list)
    chlorophyll_yearly_trends: List[Dict[str, Any]] = Field(default_factory=list)
    soil_moisture_trends: List[Dict[str, Any]] = Field(default_factory=list)
    soil_moisture_yearly_trends: List[Dict[str, Any]] = Field(default_factory=list)
    drought_trends: List[Dict[str, Any]] = Field(default_factory=list)
    drought_yearly_trends: List[Dict[str, Any]] = Field(default_factory=list)
    glacier_trends: List[Dict[str, Any]] = Field(default_factory=list)
    glacier_yearly_trends: List[Dict[str, Any]] = Field(default_factory=list)
    water_quality_trends: List[Dict[str, Any]] = Field(default_factory=list)
    water_quality_yearly_trends: List[Dict[str, Any]] = Field(default_factory=list)
    anomaly_trends: List[Dict[str, Any]] = Field(default_factory=list)
    anomaly_yearly_trends: List[Dict[str, Any]] = Field(default_factory=list)
    source_dataset: str = "JRC Global Surface Water v1.4"
    methodology: List[MethodologyItem] = Field(default_factory=list)
    data_timestamp: Optional[str] = None


class MetadataResponse(BaseModel):
    platform_name: str
    version: str
    recommended_architecture: List[str]
    data_sources: List[Dict[str, Any]]
    methodology: List[MethodologyItem]
    notes: List[str]


class AOIRequest(BaseModel):
    geometry: GeometryInput
    label: Optional[str] = None
    buffer_km: float = 5.0


class AOITiffRequest(BaseModel):
    geometry: GeometryInput
    layer: str
    label: Optional[str] = None
    buffer_km: float = 5.0
    scale_m: int = 30
