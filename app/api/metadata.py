from __future__ import annotations

from fastapi import APIRouter
from app.config import get_settings
from app.models.water_models import MetadataResponse, MethodologyItem

router = APIRouter()


@router.get("", response_model=MetadataResponse)
async def metadata():
    settings = get_settings()
    return MetadataResponse(
        platform_name=settings.app_name,
        version=settings.app_version,
        recommended_architecture=[
            "FastAPI backend",
            "Leaflet + leaflet-draw frontend",
            "Earth Engine for historical and analytical layers",
            "STAC/Planetary Computer/Copernicus for recent imagery",
            "GeoTIFF export for raster delivery",
            "TTL cache for repeat requests",
            "Redis + PostGIS for production scaling",
        ],
        data_sources=[
            {
                "name": "JRC Global Surface Water v1.4",
                "role": "Historical permanent/seasonal water and yearly history",
                "coverage": "1984 to 2021",
            },
            {
                "name": "Sentinel-1 GRD",
                "role": "Flood detection, SAR change analysis",
                "coverage": "2014 to latest",
            },
            {
                "name": "Sentinel-2 SR Harmonized",
                "role": "Turbidity and sediment plumes",
                "coverage": "2017 to latest",
            },
            {
                "name": "Sentinel-3 OLCI",
                "role": "Chlorophyll and algal bloom proxy",
                "coverage": "2016 to latest",
            },
            {
                "name": "SMAP",
                "role": "Surface/root-zone moisture proxy",
                "coverage": "2015 to latest",
            },
            {
                "name": "CHIRPS / TerraClimate",
                "role": "Drought indices and climate anomalies",
                "coverage": "1981 to latest",
            },
        ],
        methodology=[
            MethodologyItem(
                title="Important note",
                description="Flood, turbidity, chlorophyll, soil moisture, drought, and glacier layers are remote-sensing proxies, not in-situ measurements.",
            ),
            MethodologyItem(
                title="UI note",
                description="The frontend uses Leaflet Draw so the user can draw a quadrilateral or polygon AOI.",
            ),
        ],
        notes=[
            "For high-stakes or regulated use, calibrate each proxy per basin or region.",
            "Add Redis, Celery/RQ, PostGIS, and object storage for full production deployment.",
        ],
    )
