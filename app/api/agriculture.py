"""Agriculture / food-security API route."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from app.models.agriculture_models import AgricultureAnalysisResponse
from app.models.water_models import AOIRequest
from app.services.agriculture_service import analyze_agriculture_aoi

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/analyze", response_model=AgricultureAnalysisResponse)
async def analyze_agriculture(
    request: AOIRequest,
    year_start: int = Query(default=2022, ge=2015, le=2026, description="First year (inclusive)"),
    year_end: int = Query(default=2025, ge=2015, le=2026, description="Last year (inclusive)"),
):
    """Run agriculture and food-security analytics for a polygon AOI.

    Returns yearly cropland extent, cropland conversion, crop phenology,
    and FEWS NET food-security classification.
    """
    if year_start > year_end:
        raise HTTPException(status_code=400, detail="year_start must be <= year_end")

    try:
        return await analyze_agriculture_aoi(
            geometry=request.geometry.model_dump(),
            label=request.label,
            year_start=year_start,
            year_end=year_end,
        )
    except ValueError as e:
        logger.warning("Agriculture AOI validation failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Agriculture analysis failed")
        raise HTTPException(status_code=500, detail=f"Agriculture analysis failed: {e}")
