from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.config import get_settings
from app.models.stac_models import STACAsset, STACSceneSummary
from app.services.cache_service import cached, stable_cache_key
from app.services.utils import bbox_around_point

try:
    from pystac_client import Client
except Exception:
    Client = None  # type: ignore

try:
    import planetary_computer as pc
except Exception:
    pc = None  # type: ignore

_SETTINGS = get_settings()
logger = logging.getLogger(__name__)


def _stac_client():
    if Client is None:
        return None
    try:
        return Client.open(_SETTINGS.stac_url)
    except Exception as exc:
        logger.warning("STAC client connection failed for %s: %s", _SETTINGS.stac_url, exc, exc_info=True)
        return None


def _latest_item(search):
    items = list(search.items())
    if not items:
        return None
    items = sorted(items, key=lambda i: getattr(i, "datetime", None) or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    return items[0]


def search_latest_scene(lat: float, lon: float, collection: str, days_back: int = 30) -> Optional[STACSceneSummary]:
    key = stable_cache_key("stac_scene", {"lat": round(float(lat), 4), "lon": round(float(lon), 4), "collection": collection, "days": days_back})
    return cached("stac", key, lambda: _search_latest_scene_uncached(lat, lon, collection, days_back))


def _search_latest_scene_uncached(lat: float, lon: float, collection: str, days_back: int = 30) -> Optional[STACSceneSummary]:
    client = _stac_client()
    if client is None:
        return None

    bbox = bbox_around_point(lat, lon, radius_km=20)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days_back)

    try:
        search = client.search(
            collections=[collection],
            bbox=bbox,
            datetime=f"{start.isoformat()}/{end.isoformat()}",
            max_items=10,
        )
        item = _latest_item(search)
        if item is None:
            return None

        if pc is not None:
            try:
                item = pc.sign(item)
            except Exception:
                pass

        assets = [
            STACAsset(name=name, href=asset.href, media_type=asset.media_type, roles=list(asset.roles or []))
            for name, asset in item.assets.items()
        ]

        props = item.properties or {}
        cloud_cover = None
        for key in ["eo:cloud_cover", "cloud_cover", "s2:cloud_cover"]:
            if key in props:
                cloud_cover = props[key]
                break

        return STACSceneSummary(
            collection=collection,
            item_id=item.id,
            datetime=item.datetime.isoformat() if getattr(item, "datetime", None) else None,
            bbox=list(item.bbox) if getattr(item, "bbox", None) else None,
            cloud_cover=cloud_cover,
            assets=assets,
            properties=props,
        )
    except Exception as exc:
        logger.warning("STAC search failed for collection=%s lat=%s lon=%s: %s", collection, lat, lon, exc, exc_info=True)
        return None


def get_recent_context(lat: float, lon: float) -> Dict[str, Any]:
    return {
        "sentinel1": search_latest_scene(lat, lon, _SETTINGS.stac_collection_s1, _SETTINGS.stac_days_back),
        "sentinel2": search_latest_scene(lat, lon, _SETTINGS.stac_collection_s2, _SETTINGS.stac_days_back),
        "sentinel3": search_latest_scene(lat, lon, _SETTINGS.stac_collection_s3, _SETTINGS.stac_days_back),
        "landsat": search_latest_scene(lat, lon, _SETTINGS.stac_collection_landsat, _SETTINGS.stac_days_back),
    }
