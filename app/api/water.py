from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from app.models.water_models import PointAnalysisResponse, WaterTimelineResponse
from app.services.analysis_service import inspect_point

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/inspect", response_model=PointAnalysisResponse)
async def inspect(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
    buffer_km: float = Query(5.0, ge=0.1, le=50.0),
):
    try:
        return await inspect_point(lat=lat, lon=lon, buffer_km=buffer_km)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Point inspection failed")
        raise HTTPException(status_code=500, detail=f"Inspection failed: {e}")


@router.get("/timeline", response_model=WaterTimelineResponse)
async def timeline(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
):
    try:
        hist = await inspect_point(lat=lat, lon=lon)
        trends = hist.trend_summary or {}
        return WaterTimelineResponse(
            location=hist.location,
            status=trends.get("status", "ok" if hist.historical_timeline else "unavailable"),
            reason=trends.get("reason"),
            timeline=hist.historical_timeline,
            yearly=trends.get("yearly", []),
            monthly=trends.get("monthly", []),
            flood_history=trends.get("flood_history", []),
            flood_yearly_trends=trends.get("flood_yearly_trends", []),
            turbidity_trends=trends.get("turbidity_trends", []),
            turbidity_yearly_trends=trends.get("turbidity_yearly_trends", []),
            chlorophyll_trends=trends.get("chlorophyll_trends", []),
            chlorophyll_yearly_trends=trends.get("chlorophyll_yearly_trends", []),
            soil_moisture_trends=trends.get("soil_moisture_trends", []),
            soil_moisture_yearly_trends=trends.get("soil_moisture_yearly_trends", []),
            drought_trends=trends.get("drought_trends", []),
            drought_yearly_trends=trends.get("drought_yearly_trends", []),
            glacier_trends=trends.get("glacier_trends", []),
            glacier_yearly_trends=trends.get("glacier_yearly_trends", []),
            water_quality_trends=trends.get("water_quality_trends", []),
            water_quality_yearly_trends=trends.get("water_quality_yearly_trends", []),
            anomaly_trends=trends.get("anomaly_trends", []),
            anomaly_yearly_trends=trends.get("anomaly_yearly_trends", []),
            source_dataset="JRC Global Surface Water v1.4",
            methodology=hist.methodology,
            data_timestamp=hist.data_timestamp,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Timeline request failed")
        raise HTTPException(status_code=500, detail=f"Timeline failed: {e}")
