from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class STACAsset(BaseModel):
    name: str
    href: str
    media_type: Optional[str] = None
    roles: List[str] = Field(default_factory=list)


class STACSceneSummary(BaseModel):
    collection: str
    item_id: str
    datetime: Optional[str] = None
    bbox: Optional[List[float]] = None
    cloud_cover: Optional[float] = None
    assets: List[STACAsset] = Field(default_factory=list)
    properties: Dict[str, Any] = Field(default_factory=dict)
