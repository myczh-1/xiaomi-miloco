"""
RTSP device adapter — pulls video/audio from an RTSP stream (e.g. phone IP Webcam).

Designed for testing the perception pipeline without Xiaomi camera hardware.
Configure via ``settings.yaml``::

    perception:
      rtsp:
        url: "rtsp://192.168.x.x:8080/h264_pcm.sdp"
        name: "Phone Camera"
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import av

from miloco.config import get_settings
from miloco.node_monitor import NodeName
from miloco.perception.collect.adapter_base import BaseDeviceAdapter
from miloco.perception.collect.stream_buffer import (
    MultiTrackSyncBuffer,
    StreamFragment,
)
from miloco.perception.schema import (
    DecodedAudioFrame,
    DecodedVideoFrame,
    DeviceData,
)
from miloco.perception.types import PerceptionDevice

logger = logging.getLogger(__name__)

_RTSP_TRACKS = ["decoded_video", "decoded_audio"]
_DEFAULT_DID = "rtsp_0"


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _unix_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class _RtspDeviceState:
    """Per-stream state."""

    did: str
    url: str
    name: str
    sync_buffer: MultiTrackSyncBuffer
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    connected: bool = False
    epoch_delta: int | None = None


class RtspDeviceAdapter(BaseDeviceAdapter):
    """RTSP stream adapter — pulls H.264 + PCM from an RTSP URL via PyAV."""

    device_type = "rtsp"
    _node_name: NodeName | None = None  # no dedicated node_monitor node

    def __init__(
        self,
        on_window_ready: Callable[[], None] | None = None,
    ):
        self._on_window_ready = on_window_ready
        self._devices: dict[str, _RtspDeviceState] = {}

    # ---- BaseDeviceAdapter interface ----

    async def discover_devices(
        self,
        all_devices: dict | None = None,
        online_only: bool = True,
        cap: bool = True,
    ) -> dict[str, PerceptionDevice]:
        cfg = get_settings().perception.rtsp
        if not cfg or not cfg.url:
            return {}
        did = _DEFAULT_DID
        if online_only and did in self._devices and not self._devices[did].connected:
            return {}
        return {
            did: PerceptionDevice(
                did=did,
                name=cfg.name or "RTSP Camera",
                device_type="camera",
                online=True,
            )
        }

    async def connect_device(
        self, did: str, source: PerceptionDevice | None = None
    ) -> None:
        if did in self._devices:
            return

        cfg = get_settings().perception.rtsp
        if not cfg or not cfg.url:
            logger.warning("No RTSP URL configured, cannot connect")
            return

        collect_cfg = get_settings().perception.collect
        state = _RtspDeviceState(
            did=did,
            url=cfg.url,
            name=cfg.name or "RTSP Camera",
            sync_buffer=MultiTrackSyncBuffer(
                track_names=_RTSP_TRACKS,
                window_ms=collect_cfg.window_size * 1000,
                max_windows=collect_cfg.max_windows,
                on_window_ready=self._on_window_ready,
                window_settle_ms=collect_cfg.settle_ms,
                buffer_full_action=collect_cfg.full_action,
            ),
        )
        state.stop_event.clear()
        state.thread = threading.Thread(
            target=self._pull_loop,
            args=(state,),
            name=f"rtsp-pull-{did}",
            daemon=True,
        )
        self._devices[did] = state
        state.thread.start()
        logger.info("RTSP pull started for %s → %s", did, cfg.url)

    async def disconnect_device(self, did: str) -> None:
        state = self._devices.pop(did, None)
        if state is None:
            return
        state.stop_event.set()
        if state.thread and state.thread.is_alive():
            state.thread.join(timeout=5)
        state.sync_buffer.clear()
        logger.info("RTSP pull stopped for %s", did)

    def collect(self, did: str, *, drain: bool = True) -> DeviceData | None:
        state = self._devices.get(did)
        if not state:
            return None

        if drain:
            ready = state.sync_buffer.drain_ready()
            if ready is None or not any(ready.tracks.values()):
                return None
            dropped, ovf_cnt, max_depth, last_action = (
                state.sync_buffer.consume_drop_stats()
            )
            return self._build_device_data(
                state,
                ready.tracks,
                window_start_ms=ready.start_ms,
                window_end_ms=ready.end_ms,
                dropped_windows=dropped,
                overflow_count=ovf_cnt,
                max_buffer_depth=max_depth,
                last_overflow_action=last_action,
            )
        else:
            collect_ms = get_settings().perception.collect.window_size * 1000
            tracks = state.sync_buffer.peek_latest(duration_ms=collect_ms)
            if tracks is None or not any(tracks.values()):
                return None
            return self._build_device_data(state, tracks)

    def get_connected_devices(self) -> dict[str, PerceptionDevice]:
        result = {}
        for did, state in self._devices.items():
            if state.connected:
                result[did] = PerceptionDevice(
                    did=did,
                    name=state.name,
                    device_type="camera",
                    online=True,
                )
        return result


    async def shutdown(self) -> None:
        for did in list(self._devices):
            await self.disconnect_device(did)

    # ---- Pull loop (runs in daemon thread) ----

    def _pull_loop(self, state: _RtspDeviceState) -> None:
        """Background thread: open RTSP, decode frames, push into buffer."""
        reconnect_delay = 2.0
        while not state.stop_event.is_set():
            try:
                self._pull_once(state)
            except Exception as e:
                if state.stop_event.is_set():
                    break
                logger.warning(
                    "RTSP pull error for %s: %s — reconnecting in %.0fs",
                    state.did, e, reconnect_delay,
                )
                state.connected = False
                # Wait before reconnect, but check stop_event frequently
                for _ in range(int(reconnect_delay * 10)):
                    if state.stop_event.is_set():
                        return
                    time.sleep(0.1)
                reconnect_delay = min(reconnect_delay * 1.5, 30.0)
            else:
                # Clean disconnect (stream ended)
                if state.stop_event.is_set():
                    break
                state.connected = False
                reconnect_delay = 2.0

    def _pull_once(self, state: _RtspDeviceState) -> None:
        """Single connection attempt — blocks until stream ends or stop_event."""
        logger.info("Opening RTSP stream: %s", state.url)
        container = av.open(
            state.url,
            options={
                "rtsp_transport": "tcp",
                "stimeout": "5000000",  # 5s connect timeout
                "max_delay": "500000",
            },
            timeout=10,
        )
        state.connected = True
        reconnect_delay = 2.0  # reset on success
        logger.info("RTSP stream connected: %s", state.url)

        video_stream = container.streams.video[0] if container.streams.video else None
        audio_stream = container.streams.audio[0] if container.streams.audio else None

        if video_stream:
            video_stream.thread_type = "AUTO"
        if audio_stream:
            audio_stream.thread_type = "AUTO"

        try:
            for frame in container.decode(video=0 if video_stream else None,
                                          audio=0 if audio_stream else None):
                if state.stop_event.is_set():
                    break

                wall_ms = _monotonic_ms()
                if state.epoch_delta is None:
                    state.epoch_delta = _unix_ms() - wall_ms

                if isinstance(frame, av.VideoFrame):
                    self._handle_video_frame(state, frame, wall_ms)
                elif isinstance(frame, av.AudioFrame):
                    self._handle_audio_frame(state, frame, wall_ms)
        finally:
            container.close()
            state.connected = False

    def _handle_video_frame(
        self, state: _RtspDeviceState, frame: av.VideoFrame, wall_ms: int
    ) -> None:
        """Convert av.VideoFrame to BGR numpy and push to buffer."""
        try:
            bgr = frame.to_ndarray(format="bgr24")
        except Exception as e:
            logger.debug("Video frame convert error: %s", e)
            return

        stream_ts = int(frame.pts * frame.time_base * 1000) if frame.pts is not None else wall_ms
        decoded = DecodedVideoFrame(
            frame=bgr,
            stream_ts=stream_ts,
            wall_ms=wall_ms,
            unix_ms=wall_ms + (state.epoch_delta or 0),
        )
        state.sync_buffer.put("decoded_video", decoded, stream_ts=stream_ts, wall_ms=wall_ms)

    def _handle_audio_frame(
        self, state: _RtspDeviceState, frame: av.AudioFrame, wall_ms: int
    ) -> None:
        """Convert av.AudioFrame to PCM int16 numpy and push to buffer."""
        try:
            # Resample to 16kHz mono int16 for the perception engine
            frame = frame.resample(format="s16", layout="mono", rate=16000)
            pcm = frame.to_ndarray()
        except Exception as e:
            logger.debug("Audio frame convert error: %s", e)
            return

        stream_ts = int(frame.pts * frame.time_base * 1000) if frame.pts is not None else wall_ms
        decoded = DecodedAudioFrame(
            frame=pcm,
            stream_ts=stream_ts,
            wall_ms=wall_ms,
            unix_ms=wall_ms + (state.epoch_delta or 0),
        )
        state.sync_buffer.put("decoded_audio", decoded, stream_ts=stream_ts, wall_ms=wall_ms)

    # ---- DeviceData packaging ----

    def _build_device_data(
        self,
        state: _RtspDeviceState,
        tracks: dict[str, list[StreamFragment]],
        window_start_ms: int = 0,
        window_end_ms: int = 0,
        *,
        dropped_windows: int = 0,
        overflow_count: int = 0,
        max_buffer_depth: int = 0,
        last_overflow_action: str | None = None,
    ) -> DeviceData | None:
        dv_frags = tracks.get("decoded_video", [])
        da_frags = tracks.get("decoded_audio", [])
        if not dv_frags and not da_frags:
            return None

        video = [f.data for f in dv_frags]
        audio = [f.data for f in da_frags]

        return DeviceData(
            meta=PerceptionDevice(
                did=state.did,
                name=state.name,
                device_type="camera",
                room_name="RTSP",
                online=True,
            ),
            video=video,
            audio=audio,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            dropped_windows=dropped_windows,
            overflow_count=overflow_count,
            max_buffer_depth=max_buffer_depth,
            last_overflow_action=last_overflow_action,
        )
