from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.aoi import router as aoi_router
from app.api.health import router as health_router
from app.api.metadata import router as metadata_router
from app.api.tif import router as tif_router
from app.api.water import router as water_router
from app.config import get_settings

settings = get_settings()

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
for noisy_logger in [
    "googleapiclient.discovery",
    "urllib3.connectionpool",
    "urllib3.util.retry",
    "pystac_client.stac_api_io",
]:
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Africa-scale geospatial intelligence platform for advanced water monitoring.",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Root endpoints (easy local access)
app.include_router(health_router, prefix="/health", tags=["health"])
app.include_router(metadata_router, prefix="/metadata", tags=["metadata"])
app.include_router(water_router, prefix="/water", tags=["water"])
app.include_router(aoi_router, prefix="/aoi", tags=["aoi"])
app.include_router(tif_router, prefix="/tif", tags=["tif"])

# Backward-compatible API prefix used by earlier frontend builds
api_v1 = APIRouter(prefix=settings.api_v1_prefix)
api_v1.include_router(health_router, prefix="/health", tags=["health-v1"])
api_v1.include_router(metadata_router, prefix="/metadata", tags=["metadata-v1"])
api_v1.include_router(water_router, prefix="/water", tags=["water-v1"])
api_v1.include_router(aoi_router, prefix="/aoi", tags=["aoi-v1"])
api_v1.include_router(tif_router, prefix="/tif", tags=["tif-v1"])
app.include_router(api_v1)


@app.get("/")
async def root():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return {
        "message": "Africa Water Intelligence Platform is running",
        "docs": "/docs",
        "health": "/health",
        "metadata": "/metadata",
    }
