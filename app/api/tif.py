from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.services.analysis_service import export_point_tif

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/export")
async def export(
    lat: float = Query(...),
    lon: float = Query(...),
    layer: Literal["flood", "turbidity", "chlorophyll", "water_quality", "soil_moisture", "drought", "glacier", "jrc_occurrence"] = Query("flood"),
    buffer_km: float = Query(5.0, ge=0.1, le=50.0),
    scale_m: int = Query(30, ge=10, le=1000),
):
    try:
        path = await export_point_tif(lat=lat, lon=lon, layer=layer, buffer_km=buffer_km, scale_m=scale_m)
        return FileResponse(path, media_type="image/tiff", filename=f"{layer}_{round(lat,4)}_{round(lon,4)}.tif")
    except RuntimeError as e:
        logger.warning("GeoTIFF export unavailable: %s", e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("GeoTIFF export failed")
        raise HTTPException(status_code=500, detail=f"GeoTIFF export failed: {e}")
