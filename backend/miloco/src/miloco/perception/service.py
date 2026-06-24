"""
Perception service layer.

Orchestrates the realtime engine, active perception queries,
perception log retrieval, and device management.

Active perception uses the same pipeline as realtime — data is collected
from the realtime stream buffers via collector.collect_batch(),
ensuring a unified data path.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import cv2

from miloco.database.perception_repo import PerceptionLogRepo
from miloco.middleware.exceptions import BusinessException
from miloco.perception.collect.collector import MultimodalCollector
from miloco.perception.processor import PipelineProcessor
from miloco.perception.runner import PerceptionRunner
from miloco.perception.schema import (
    OnDemandPerceptionRequest,
    OnDemandPerceptionResultItem,
    PerceptionEngineStatus,
)
from miloco.perception.types import PerceptionDevice
from miloco.utils.agent_config import update_shared_config
from miloco.utils.time_utils import ms_to_iso_local, now_ms

logger = logging.getLogger(__name__)


class PerceptionService:
    """Service for all perception operations."""

    def __init__(
        self,
        collector: MultimodalCollector,
        pipeline: PipelineProcessor,
        perception_runner: PerceptionRunner,
        log_repo: PerceptionLogRepo,
    ):
        self._collector = collector
        self._pipeline = pipeline
        self._engine = perception_runner
        self._log_repo = log_repo

    # ---- Realtime engine lifecycle ----

    async def start_engine(self) -> None:
        await self._engine.start()

    async def stop_engine(self) -> None:
        await self._engine.stop()

    def engine_status(self) -> PerceptionEngineStatus:
        return self._engine.status()

    @property
    def tier_u_pool(self):
        """暴露 PerceptionEngine 内部的 TierUPool(陌生人池)给 router 用。

        实际穿层封装放在 ``PipelineProcessor.tier_u_pool`` property,本层只透传。
        engine 禁用 / 池启动失败时返 None。
        """
        return self._pipeline.tier_u_pool

    @property
    def deep_sort_config(self):
        """暴露 yaml-resolved DeepSortConfigDC 给 router 视频注册路径用。

        穿层封装放在 ``PipelineProcessor.deep_sort_config``,本层透传。
        engine 未初始化时返代码默认值(``DeepSortConfigDC()``)。
        """
        return self._pipeline.deep_sort_config

    def get_active_confirmed_track_keys(self) -> list[tuple[str, int]]:
        """暴露当前所有 cam 上 confirmed track 的 ``(cam_id, track_id)`` 列表。

        给 router pool_fetch 用: 跟 confirmed track 实时 emb 做去重 (case b)。
        engine 未初始化时返空列表。
        """
        return self._pipeline.get_active_confirmed_track_keys()

    def get_reid_extractor(self):
        """从任一活动的 DeepSortTracker 借 HumanReID 实例,给身份库注册时
        ``add_tier_a_samples_batch`` 做 .npy 兜底抽取用。
        所有 device 的 tracker 共用同一份 ReID ONNX 模型,任选一个即可;
        无活动 tracker → None,库就跳过兜底(行为退回旧版,不报错)。
        """
        return self._pipeline.get_reid_extractor()

    # ---- Buffer management ----

    def clear_buffers(self) -> None:
        """Clear all device stream buffers.

        Discards all buffered data without disconnecting devices.
        New frames arriving after this call start from a clean state,
        allowing the realtime pipeline to process only fresh data.
        """
        self._collector.clear_all_buffers()
        logger.info("All perception buffers cleared")

    # ---- Active perception ----

    async def on_demand_perceive(
        self, request: OnDemandPerceptionRequest
    ) -> OnDemandPerceptionResultItem | None:
        """On-demand perception: batch-collects requested devices and runs
        a single fusion inference via pipeline.

        If the realtime engine is running, data comes from its existing stream
        subscriptions. If not running, the collector may have no data.
        """
        active_sources = self._collector.get_all_active_sources()

        valid_dids: list[str] = []
        for did in request.sources:
            if did not in active_sources:
                logger.warning(
                    "[service](device=%s) 未激活感知(skipped)", did
                )
                continue
            valid_dids.append(did)

        if not valid_dids:
            raise BusinessException(
                "No valid active perception sources found. "
                "Ensure the perception engine is running and devices are online.",
                code=2011,
            )

        # Single batch inference call — collector assembles batch, processor infers
        result = await self._pipeline.process_on_demand(valid_dids, request.query)

        if not result:
            raise BusinessException(
                "Failed to perform on-demand perception.",
                code=2012,
            )

        # Map inference results back to API response items
        return OnDemandPerceptionResultItem(
            answer=result.answer,
            timestamp=ms_to_iso_local(now_ms()),
        )

    # ---- Perception logs ----

    def query_logs(
        self,
        after: str | None = None,
        before: str | None = None,
        since: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """Query perception logs.

        Args:
            after: ISO 8601 timestamp cursor — return entries after this time.
            before: ISO 8601 upper bound — return entries before this time.
            since: Relative time string like "1h", "30m", "2h30m".
            limit: Max entries to return. None means no limit.

        Returns:
            Dict with logs, count, and total_inferences.
        """
        from miloco.utils.time_utils import parse_iso_ms, since_to_ms

        after_ms: int | None = None
        before_ms: int | None = None
        since_ms: int | None = None

        if after:
            after_ms = parse_iso_ms(after, "after")

        if before:
            before_ms = parse_iso_ms(before, "before")

        if since and after_ms is None:
            since_ms = since_to_ms(since)

        logs, count = self._log_repo.query(
            after_ms=after_ms, before_ms=before_ms, since_ms=since_ms, limit=limit
        )

        return {
            "logs": logs,
            "count": count,
            "total_inferences": self._log_repo.get_today_inference_count(),
        }

    def cleanup_logs(self, keep_days: int) -> int:
        """清理过期感知日志。"""
        return self._log_repo.delete_before_days(keep_days)

    # ---- Device management ----

    async def get_devices(self, online_only: bool = True) -> list[PerceptionDevice]:
        """List all perception-capable devices across all adapter types.

        Args:
            online_only: If True (default), only return online devices.
                         If False, return all discovered devices.
        """
        devices: list[PerceptionDevice] = []

        for adapter in self._collector._adapters.values():
            try:
                # cap=False：列设备全集（含超出投喂上限的相机），用于 rule target
                # 校验等「枚举可选设备」语义，不受 MAX_ENABLED_CAMERAS 投喂上限收窄。
                discovered = await adapter.discover_devices(
                    online_only=online_only, cap=False
                )
                for did, source in discovered.items():
                    devices.append(source)
            except Exception as e:
                logger.error(
                    "[collect](adapter=%s) 发现设备失败 | %s",
                    adapter.device_type,
                    e,
                )

        return devices

    async def get_rtsp_debug_config(self) -> dict[str, object]:
        adapter = self._collector.get_adapter("rtsp")
        if adapter is None:
            return {
                "did": "rtsp_0",
                "enabled": False,
                "url": None,
                "name": "RTSP Camera",
                "connected": False,
                "last_error": "rtsp adapter unavailable",
                "has_preview": False,
                "last_frame_wall_ms": 0,
            }
        getter = getattr(adapter, "get_debug_status", None)
        if callable(getter):
            return getter()
        return {
            "did": "rtsp_0",
            "enabled": False,
            "url": None,
            "name": "RTSP Camera",
            "connected": False,
            "last_error": "rtsp debug status unavailable",
            "has_preview": False,
            "last_frame_wall_ms": 0,
        }

    async def update_rtsp_debug_config(self, *, url: str | None, name: str) -> dict[str, object]:
        norm_url = (url or "").strip() or None
        norm_name = name.strip() or "RTSP Camera"
        update_shared_config(perception={"rtsp": {"url": norm_url, "name": norm_name}})
        adapter = self._collector.get_adapter("rtsp")
        if adapter is not None:
            await adapter.sync_devices()
        return await self.get_rtsp_debug_config()

    async def get_rtsp_preview_jpeg(self, did: str = "rtsp_0") -> bytes | None:
        adapter = self._collector.get_adapter("rtsp")
        if adapter is None:
            return None
        peek = getattr(adapter, "peek_latest_frame", None)
        if not callable(peek):
            return None
        frame = peek(did)
        if frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return None
        return buf.tobytes()

    def get_privacy_preview_status(self) -> dict[str, object]:
        status: dict[str, object] = {
            "plugin_installed": False,
            "plugin_enabled": False,
            "debug_enabled": False,
            "patched": False,
            "has_preview": False,
            "message": "",
            "timestamp_ms": 0,
            "frame_count": 0,
            "width": 0,
            "height": 0,
        }
        try:
            from miloco_privacy_plugin.config import load_config

            cfg = load_config()
            status["plugin_installed"] = True
            status["plugin_enabled"] = bool(cfg.enabled)
            status["debug_enabled"] = bool(cfg.debug)
        except Exception as e:  # noqa: BLE001
            status["message"] = f"plugin not installed: {e}"
            return status

        try:
            import miloco.perception.engine.omni.prompt_builder as pb

            status["patched"] = bool(
                getattr(pb._encode_video_mp4, "__miloco_privacy_patched__", False)
            )
        except Exception as e:  # noqa: BLE001
            status["message"] = f"patch status unavailable: {e}"

        try:
            from miloco_privacy_plugin.debug_state import get_preview_meta

            meta = get_preview_meta()
        except Exception as e:  # noqa: BLE001
            if not status["message"]:
                status["message"] = f"preview unavailable: {e}"
            return status

        if isinstance(meta, Mapping):
            status.update(
                {
                    "has_preview": bool(meta.get("has_preview")),
                    "timestamp_ms": int(meta.get("timestamp_ms", 0) or 0),
                    "frame_count": int(meta.get("frame_count", 0) or 0),
                    "width": int(meta.get("width", 0) or 0),
                    "height": int(meta.get("height", 0) or 0),
                }
            )
        return status

    def get_privacy_preview_image(self, variant: str) -> bytes | None:
        try:
            from miloco_privacy_plugin.debug_state import get_preview_image
        except Exception:  # noqa: BLE001
            return None
        if variant not in {"original", "processed"}:
            return None
        return get_preview_image(variant)
