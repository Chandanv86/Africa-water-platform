from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.models.water_models import AOIAnalysisResponse, AOIRequest, AOITiffRequest
from app.services.analysis_service import analyze_aoi, export_aoi_tif

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/analyze", response_model=AOIAnalysisResponse)
async def analyze(request: AOIRequest):
    try:
        return await analyze_aoi(request.geometry.model_dump(), label=request.label, buffer_km=request.buffer_km)
    except ValueError as e:
        logger.warning("AOI validation failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("AOI analysis failed")
        raise HTTPException(status_code=500, detail=f"AOI analysis failed: {e}")


@router.post("/tif")
async def tif(request: AOITiffRequest):
    try:
        path = await export_aoi_tif(
            geometry=request.geometry.model_dump(),
            layer=request.layer,
            buffer_km=request.buffer_km,
            scale_m=request.scale_m,
            label=request.label,
        )
        return FileResponse(path, media_type="image/tiff", filename=f"{request.layer}_{request.label or 'aoi'}.tif")
    except ValueError as e:
        logger.warning("AOI GeoTIFF validation failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        logger.warning("AOI GeoTIFF export unavailable: %s", e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("AOI GeoTIFF export failed")
        raise HTTPException(status_code=500, detail=f"AOI GeoTIFF export failed: {e}")
