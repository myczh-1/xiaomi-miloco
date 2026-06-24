"""
Perception API controller.

Provides endpoints for realtime engine control, active perception queries,
perception log retrieval, and device listing.
"""

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, Query, Response

from miloco.manager import get_manager
from miloco.middleware import verify_token
from miloco.middleware.exceptions import HTTPException
from miloco.perception.schema import (
    OnDemandPerceptionRequest,
    PrivacyPreviewStatus,
    RtspDebugConfig,
    RtspDebugConfigRequest,
)
from miloco.schema.common_schema import NormalResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/perception", tags=["Perception"])

manager = get_manager()


def _require_engine_ready():
    """Dependency guard: returns 503 when perception engine is not ready."""
    status = manager.perception_service.engine_status()
    if not status.engine.ready:
        raise HTTPException(
            message=f"perception_unavailable: {status.engine.status}",
            status_code=503,
        )


@router.get(
    "/engine/status",
    summary="Get perception engine status",
    dependencies=[Depends(verify_token)],
)
async def get_engine_status():
    status = manager.perception_service.engine_status()
    return NormalResponse(code=0, message="ok", data=status)


@router.post(
    "/engine/start",
    summary="Start realtime perception engine",
    dependencies=[Depends(verify_token)],
)
async def start_engine():
    await manager.perception_service.start_engine()
    return NormalResponse(code=0, message="Perception engine started")


@router.post(
    "/engine/stop",
    summary="Stop realtime perception engine",
    dependencies=[Depends(verify_token)],
)
async def stop_engine():
    await manager.perception_service.stop_engine()
    return NormalResponse(code=0, message="Perception engine stopped")


@router.post(
    "/clear",
    summary="Clear all device stream buffers",
    dependencies=[Depends(verify_token)],
)
async def clear_buffers():
    manager.perception_service.clear_buffers()
    return NormalResponse(code=0, message="All perception buffers cleared")


@router.post(
    "/perceive",
    summary="Active perception query",
    dependencies=[Depends(verify_token), Depends(_require_engine_ready)],
)
async def on_demand_perceive(request: OnDemandPerceptionRequest):
    result = await manager.perception_service.on_demand_perceive(request)
    return NormalResponse(code=0, message="ok", data=result)


@router.get(
    "/logs",
    summary="Query perception logs",
    dependencies=[Depends(verify_token)],
)
async def query_logs(
    limit: int | None = Query(None, ge=1, le=1000, description="Max entries; omit for unlimited"),
    after: str | None = Query(None, description="ISO 8601 timestamp cursor"),
    before: str | None = Query(
        None,
        description="ISO 8601 upper-bound; combined with ``after`` allows windowed pagination",
    ),
    since: str | None = Query(None, description="Relative time, e.g. '1h', '30m', '2h30m'"),
):
    data = manager.perception_service.query_logs(
        after=after, before=before, since=since, limit=limit
    )
    return NormalResponse(code=0, message="ok", data=data)


@router.get(
    "/devices",
    summary="List perception-capable devices",
    dependencies=[Depends(verify_token)],
)
async def list_devices():
    devices = await manager.perception_service.get_devices()
    return NormalResponse(
        code=0,
        message="ok",
        data=[asdict(d) for d in devices],
    )


@router.get(
    "/debug/rtsp",
    summary="Get debug RTSP/RTMP source config and state",
    dependencies=[Depends(verify_token)],
)
async def get_rtsp_debug_config():
    data = await manager.perception_service.get_rtsp_debug_config()
    return NormalResponse(
        code=0,
        message="ok",
        data=RtspDebugConfig.model_validate(data),
    )


@router.put(
    "/debug/rtsp",
    summary="Update debug RTSP/RTMP source config",
    dependencies=[Depends(verify_token)],
)
async def update_rtsp_debug_config(request: RtspDebugConfigRequest):
    data = await manager.perception_service.update_rtsp_debug_config(
        url=request.url,
        name=request.name,
    )
    return NormalResponse(
        code=0,
        message="ok",
        data=RtspDebugConfig.model_validate(data),
    )


@router.get(
    "/debug/rtsp/frame",
    summary="Get latest debug RTSP/RTMP preview frame as JPEG",
    dependencies=[Depends(verify_token)],
)
async def get_rtsp_debug_frame():
    payload = await manager.perception_service.get_rtsp_preview_jpeg()
    if payload is None:
        raise HTTPException(message="rtsp preview not available", status_code=404)
    return Response(content=payload, media_type="image/jpeg")


@router.get(
    "/debug/privacy_preview",
    summary="Get privacy-plugin preview status",
    dependencies=[Depends(verify_token)],
)
async def get_privacy_preview_status():
    data = manager.perception_service.get_privacy_preview_status()
    return NormalResponse(
        code=0,
        message="ok",
        data=PrivacyPreviewStatus.model_validate(data),
    )


@router.get(
    "/debug/privacy_preview/{variant}",
    summary="Get latest privacy-plugin preview frame as JPEG",
    dependencies=[Depends(verify_token)],
)
async def get_privacy_preview_image(variant: str):
    payload = manager.perception_service.get_privacy_preview_image(variant)
    if payload is None:
        raise HTTPException(message="privacy preview not available", status_code=404)
    return Response(content=payload, media_type="image/jpeg")

