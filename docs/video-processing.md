# Xiaomi Miloco 视频处理详解

## 目录

- [1. 整体架构](#1-整体架构)
- [2. 视频采集层 (MIoT SDK)](#2-视频采集层-miot-sdk)
- [3. 视频流管理层](#3-视频流管理层)
- [4. 视频转码层](#4-视频转码层)
- [5. 前端播放层](#5-前端播放层)
- [6. 视频录制功能](#6-视频录制功能)
- [7. 关键设计决策](#7-关键设计决策)
- [8. 核心文件索引](#8-核心文件索引)

---

## 1. 整体架构

视频处理流程可以分为 **5 个层次**：

```
┌─────────────────────────────────────────────────────────────────┐
│                      视频处理整体架构                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐  │
│  │  米家     │───▶│ MIoT SDK │───▶│ 视频流    │───▶│ WebSocket│  │
│  │  摄像头   │    │   层     │    │ 管理层    │    │   广播   │  │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘  │
│                                          │              │       │
│                                          ▼              ▼       │
│                                    ┌──────────┐  ┌──────────┐  │
│                                    │  感知     │  │  前端     │  │
│                                    │  引擎     │  │  播放器   │  │
│                                    └──────────┘  └──────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### 核心流程

```
米家摄像头 → PPCS/SDK → 解码后BGR帧 → H264LiveEncoder → H.264 NAL → WebSocket广播
                                    ↓
                              感知引擎（AI分析）
```

### 关键特性

- **统一H.264输出**: 无论摄像头原生是H.264还是H.265，都转码为H.264
- **多消费者共享**: `multi_reg` 让实时预览和感知引擎共享同一解码流
- **双解码路径**: WebCodecs（低延迟）+ MSE/jmuxer（兼容性）
- **帧级容错**: 丢弃首个IDR前的帧，确保干净的GOP边界

---

## 2. 视频采集层 (MIoT SDK)

**核心文件**: 
- `miot/camera_handler.py`
- `miot/client.py`

### 2.1 摄像头连接

摄像头通过小米的 MIoT SDK 连接，支持两种流：

```python
# 原始视频流 — 未解码的 H.264/H.265 数据
raw_video

# 解码后视频帧 — PyAV 解码出的 BGR ndarray
decoded_video
```

### 2.2 注册视频流回调

```python
class CameraHandler:
    """摄像头处理器"""
    
    async def register_raw_video_async(
        self,
        callback: Callable,
        channel: int,
    ):
        """注册原始视频流回调"""
        await self.miot_camera_instance.register_raw_video_async(callback, channel)
    
    async def register_decode_video_frame_stream(
        self,
        callback: Callable[[str, VideoFrame, int, int, int, int], Coroutine],
        channel: int,
    ) -> int:
        """注册解码后的视频帧回调（multi_reg，可与其他消费者共享）"""
        return await self.miot_camera_instance.register_decode_video_frame_async(
            callback, channel, multi_reg=True
        )
```

### 2.3 关键设计：multi_reg

使用 `multi_reg=True` 让 **实时预览** 和 **感知引擎** 共享同一个解码流：

```
┌─────────────────────────────────────────────────────────────┐
│                    multi_reg 共享机制                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│                    ┌──────────────┐                         │
│                    │   米家摄像头   │                         │
│                    └──────────────┘                         │
│                           │                                 │
│                           ▼                                 │
│                    ┌──────────────┐                         │
│                    │  PPCS 连接    │  （单条连接）            │
│                    └──────────────┘                         │
│                           │                                 │
│                           ▼                                 │
│                    ┌──────────────┐                         │
│                    │  PyAV 解码器  │  （单次解码）            │
│                    └──────────────┘                         │
│                           │                                 │
│              ┌────────────┼────────────┐                   │
│              ▼            ▼            ▼                   │
│       ┌──────────┐ ┌──────────┐ ┌──────────┐              │
│       │ 实时预览  │ │ 感知引擎  │ │ 录制器   │              │
│       │ (WebSocket│ │ (AI分析) │ │ (Clip)  │              │
│       └──────────┘ └──────────┘ └──────────┘              │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

优势：
- 避免对摄像头建立多条 PPCS 连接
- 减少带宽和解码开销
- 多个消费者共享同一解码结果

### 2.4 MiotProxy

```python
class MiotProxy:
    """MIoT 代理，管理所有摄像头连接"""
    
    _camera_img_managers: dict[str, CameraHandler]
    
    async def start_camera_decode_video_stream(
        self,
        camera_id: str,
        channel: int,
        callback: Callable,
    ) -> int:
        """启动解码视频流"""
        if camera_id not in self._camera_img_managers:
            return -1
        
        instance = self._camera_img_managers[camera_id]
        reg_id = await instance.register_decode_video_frame_stream(callback, channel)
        return reg_id
    
    async def stop_camera_decode_video_stream(
        self,
        camera_id: str,
        channel: int,
        reg_id: int,
    ):
        """停止解码视频流"""
        if camera_id not in self._camera_img_managers:
            return
        
        instance = self._camera_img_managers[camera_id]
        await instance.unregister_decode_video_frame_stream(channel, reg_id)
```

---

## 3. 视频流管理层

**核心文件**: `miot/ws.py`

### 3.1 MIoTVideoStreamManager

```python
class MIoTVideoStreamManager:
    """
    MIoT 视频 WebSocket 管理器
    
    职责：
    - 管理 WebSocket 连接
    - 启动/停止 SDK 视频流
    - 将 BGR 帧编码为 H.264
    - 广播给所有订阅者
    """
    
    _CAMERA_CONNECT_COUNT_MAX = 4  # 每摄像头最大连接数
    _TRANSCODE_GOP = 30            # GOP 大小（约1.2秒@25fps）
    
    # 状态管理
    _camera_connect_map: dict[str, dict[str, OrderedDict[str, WebSocket]]]
    _camera_codec: dict[str, MIoTCameraCodec]
    _camera_seen_keyframe: set[str]
    _camera_encoder: dict[str, H264LiveEncoder]
    _camera_reg_id: dict[str, int]
    _camera_recorders: dict[str, list[NalClipRecorder]]
    _camera_locks: dict[str, asyncio.Lock]
```

### 3.2 生命周期管理

```python
async def new_connection(
    self,
    websocket: WebSocket,
    user_name: str,
    token_hash: str,
    camera_id: str,
    channel: int,
) -> str:
    """
    新建视频流连接
    
    流程：
    1. 获取摄像头锁
    2. 检查是否是第一个订阅者
    3. 如果是，启动 SDK 视频流
    4. 注册 WebSocket 连接
    5. 如果是晚到者，发送 init 消息
    """
    camera_tag = f"{camera_id}.{channel}"
    
    async with self._lock_for(camera_tag):
        # 第一个订阅者触发 SDK 流
        sdk_just_started = not self._has_subscribers(camera_tag)
        if sdk_just_started:
            await self._ensure_sdk_subscription(camera_id, channel, camera_tag)
        
        # 注册连接
        user_tag = f"{user_name}.{token_hash}"
        self._camera_connect_map[camera_tag].setdefault(user_tag, OrderedDict())
        connection_id = str(self._camera_connect_id)
        self._camera_connect_id += 1
        self._camera_connect_map[camera_tag][user_tag][connection_id] = websocket
        
        # 限制连接数
        if len(self._camera_connect_map[camera_tag][user_tag]) > self._CAMERA_CONNECT_COUNT_MAX:
            _, ws = self._camera_connect_map[camera_tag][user_tag].popitem(last=False)
            await ws.close()
        
        # 晚到者：发送 init 消息
        cached_codec = self._camera_codec.get(camera_tag)
        if cached_codec is not None and not sdk_just_started:
            await websocket.send_text(self._build_init_msg(cached_codec))
    
    return connection_id


async def close_connection(
    self,
    user_name: str,
    token_hash: str,
    camera_id: str,
    channel: int,
    cid: str,
):
    """
    关闭视频流连接
    
    流程：
    1. 获取摄像头锁
    2. 移除 WebSocket 连接
    3. 如果是最后一个订阅者，停止 SDK 视频流
    """
    camera_tag = f"{camera_id}.{channel}"
    user_tag = f"{user_name}.{token_hash}"
    
    async with self._lock_for(camera_tag):
        # 移除连接
        ws = self._camera_connect_map[camera_tag][user_tag].pop(cid)
        await ws.close()
        
        # 如果没有订阅者了，停止 SDK 流
        await self._teardown_if_idle(camera_id, channel, camera_tag)
```

### 3.3 SDK 订阅管理

```python
async def _ensure_sdk_subscription(
    self,
    camera_id: str,
    channel: int,
    camera_tag: str,
) -> None:
    """
    确保 SDK 订阅（幂等）
    
    流程：
    1. 启动 SDK 视频流
    2. 创建 H264LiveEncoder
    3. 记录 reg_id
    """
    try:
        reg_id = await manager.miot_service.start_video_stream(
            camera_id=camera_id,
            channel=channel,
            callback=self.__video_stream_callback,
        )
    except Exception:
        self._camera_connect_map.pop(camera_tag, None)
        raise
    
    if reg_id < 0:
        self._camera_connect_map.pop(camera_tag, None)
        raise RuntimeError(f"Camera {camera_id} not registered with SDK")
    
    self._camera_reg_id[camera_tag] = reg_id
    self._camera_encoder[camera_tag] = H264LiveEncoder(gop=self._TRANSCODE_GOP)


async def _teardown_if_idle(
    self,
    camera_id: str,
    channel: int,
    camera_tag: str,
) -> None:
    """
    如果没有订阅者，停止 SDK 流
    """
    if self._has_subscribers(camera_tag):
        return
    
    reg_id = self._camera_reg_id.pop(camera_tag, -1)
    if reg_id >= 0:
        await manager.miot_service.stop_video_stream(camera_id, channel, reg_id)
    
    encoder = self._camera_encoder.pop(camera_tag, None)
    if encoder is not None:
        await encoder.close()
    
    self._camera_connect_map.pop(camera_tag, None)
    self._camera_codec.pop(camera_tag, None)
    self._camera_seen_keyframe.discard(camera_tag)
```

### 3.4 视频流回调

```python
async def __video_stream_callback(
    self,
    did: str,
    bgr: NDArray[np.uint8],
    ts: int,
    channel: int,
    recv_unix_ms: int,
    decoded_unix_ms: int,
) -> None:
    """
    解码后视频帧回调
    
    流程：
    1. 检查是否有订阅者
    2. 转发给录制器（如果有）
    3. 发送 init 消息（首次）
    4. 编码为 H.264
    5. 广播给所有 WebSocket 客户端
    """
    camera_tag = f"{did}.{channel}"
    
    if not self._has_subscribers(camera_tag):
        return
    
    # 转发给录制器
    for rec in list(self._camera_recorders.get(camera_tag, ())):
        try:
            await rec.feed_bgr(bgr, ts)
        except Exception as e:
            logger.error("recorder feed_bgr error %s: %s", camera_tag, e)
    
    # 发送 init 消息（首次）
    if camera_tag not in self._camera_codec:
        self._camera_codec[camera_tag] = MIoTCameraCodec.VIDEO_H264
        await self._broadcast(
            camera_tag,
            text=self._build_init_msg(MIoTCameraCodec.VIDEO_H264),
        )
    
    # 如果没有 WebSocket 客户端，跳过编码
    if not self._all_websockets(camera_tag):
        return
    
    # 编码为 H.264
    encoder = self._camera_encoder.get(camera_tag)
    if encoder is None:
        return
    
    packets = await encoder.encode(bgr, pts_ms=ts)
    
    # 时间戳净化
    _TS_SAFE_MAX = 9_000_000_000_000_000  # 9e15
    wire_ts = ts if 0 <= ts < _TS_SAFE_MAX else decoded_unix_ms
    
    # 广播
    for nal_bytes, is_keyframe in packets:
        # 等待首个关键帧
        if camera_tag not in self._camera_seen_keyframe:
            if not is_keyframe:
                continue
            self._camera_seen_keyframe.add(camera_tag)
        
        # 构建帧头
        header = struct.pack(
            ">B7xQ",
            1 if is_keyframe else 0,
            wire_ts & 0xFFFFFFFFFFFFFFFF,
        )
        await self._broadcast(camera_tag, payload=header + nal_bytes)
```

### 3.5 线路协议 (Wire Protocol)

每帧推送给浏览器的格式：

```
字节 0:    uint8  frame_type
           - 1 = 关键帧 (I/IDR)
           - 0 = P帧

字节 1-7:  7字节 padding

字节 8-15: uint64 时间戳 (大端序, 毫秒)

字节 16+:  Annex-B NAL 数据
```

首帧前会发送 JSON 初始化消息：

```json
{
    "type": "init",
    "codec": "h264",
    "container": "annexb"
}
```

### 3.6 广播机制

```python
async def _broadcast(
    self,
    camera_tag: str,
    *,
    text: str | None = None,
    payload: bytes | None = None,
) -> None:
    """
    广播给所有订阅者
    
    使用 asyncio.gather 并发发送
    """
    targets = self._all_websockets(camera_tag)
    if not targets:
        return
    
    async def _send(ws: WebSocket) -> None:
        try:
            if text is not None:
                await ws.send_text(text)
            else:
                await ws.send_bytes(payload)
        except Exception as err:
            logger.error("WebSocket send error: %s", err)
    
    await asyncio.gather(*(_send(ws) for ws in targets))
```

---

## 4. 视频转码层

**核心文件**: `miot/transcoder.py`

### 4.1 H264LiveEncoder

```python
class H264LiveEncoder:
    """
    libx264 封装，用于实时转码
    
    特性：
    - 首次调用时惰性初始化编码器
    - 分辨率变化时自动重建编码器
    - 使用 dedicated 单线程执行器
    """
    
    def __init__(self, gop: int = 30):
        self._gop = gop
        self._codec: av.codec.CodecContext | None = None
        self._width = 0
        self._height = 0
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._closed = False
        self._pts_counter = 0  # 自维护 PTS，不信摄像头侧 PTS
```

### 4.2 编码器配置

```python
def _open_encoder(self, width: int, height: int) -> None:
    """打开编码器"""
    codec = av.codec.CodecContext.create("libx264", "w")
    codec.width = width
    codec.height = height
    codec.pix_fmt = "yuv420p"
    codec.time_base = Fraction(1, 1000)  # PTS 单位：毫秒
    codec.framerate = Fraction(30, 1)
    codec.gop_size = self._gop
    codec.max_b_frames = 0
    codec.thread_count = 1  # 单线程，避免多 slice 导致浏览器硬解失败
    
    codec.options = {
        "preset": "ultrafast",      # 最快编码速度
        "tune": "zerolatency",      # 零延迟调优
        "x264-params": (
            f"keyint={self._gop}:min-keyint={self._gop}:"  # 固定 GOP
            "scenecut=0:"           # 禁用场景切换 IDR
            "bframes=0:"            # 无 B 帧
            "repeat-headers=1:"     # 每个 IDR 前重复 SPS/PPS
            "level=4.0:"            # H.264 level 限制
            "slices=1:"             # 单 slice
            "sliced-threads=0"      # 禁用 slice 线程
        ),
    }
    
    codec.open()
    self._codec = codec
```

### 4.3 编码参数说明

| 参数 | 值 | 说明 |
|------|-----|------|
| preset | ultrafast | 最快编码速度，适合实时场景 |
| tune | zerolatency | 零延迟调优，减少缓冲 |
| keyint | 30 | GOP 大小，约1.2秒@25fps |
| scenecut | 0 | 禁用场景切换 IDR，保持固定 GOP |
| bframes | 0 | 无 B 帧，减少延迟 |
| repeat-headers | 1 | 每个 IDR 前重复 SPS/PPS，兼容晚到者 |
| level | 4.0 | 支持 1080p@30 / 720p@60 |
| slices | 1 | 单 slice，兼容浏览器硬解 |
| threads | 1 | 单线程，避免并发问题 |

### 4.4 编码流程

```python
def _encode_sync(
    self,
    bgr: NDArray[np.uint8],
    pts_ms: int,
) -> list[tuple[bytes, bool]]:
    """
    编码单帧
    
    返回：[(nal_bytes, is_keyframe), ...]
    """
    if self._closed:
        return []
    
    # 检查分辨率变化
    h, w = bgr.shape[:2]
    if self._codec is None:
        self._open_encoder(w, h)
    elif w != self._width or h != self._height:
        # 分辨率变化，重建编码器
        self._rebuild_encoder(w, h)
    
    # 创建 VideoFrame
    frame = av.VideoFrame.from_ndarray(bgr, format="bgr24")
    frame = frame.reformat(format="yuv420p")
    
    # 使用自维护 PTS（不信摄像头侧 PTS）
    frame.pts = self._pts_counter * 33  # 约 30fps
    self._pts_counter += 1
    
    # 编码
    out = []
    for packet in self._codec.encode(frame):
        out.append((bytes(packet), bool(packet.is_keyframe)))
    
    return out


async def encode(
    self,
    bgr: NDArray[np.uint8],
    pts_ms: int,
) -> list[tuple[bytes, bool]]:
    """异步编码包装"""
    if self._closed:
        return []
    
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        self._executor, self._encode_sync, bgr, pts_ms
    )
```

### 4.5 PTS 自维护

```python
# 不信任摄像头侧 PTS 的原因：
# 1. SDK 把 PTS 读成 c_uint64
# 2. 摄像头在 "PTS 未知" 时发哨兵值 0xFFFFFFFFFFFFFFFF
# 3. PyAV 17 的 frame.pts setter 是 signed int64，哨兵值会抛 OverflowError
#
# 解决方案：
# 使用本地计数器 × 33ms（约 30fps）
# PTS 只供 libx264 内部码率核算，不进 wire
# 浏览器的播放时序来自 WS 帧头里的相机原始 ts

self._pts_counter = 0
frame.pts = self._pts_counter * 33
self._pts_counter += 1
```

---

## 5. 前端播放层

**核心文件**: `web/public/watch.html`

### 5.1 解码策略

```
┌─────────────────────────────────────────────────────────────┐
│                    前端解码策略                               │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  SecureContext (HTTPS/localhost)                            │
│      │                                                      │
│      ▼                                                      │
│  ┌──────────────────┐                                       │
│  │ WebCodecs        │  低延迟，支持 H.264 和 H.265         │
│  │ (VideoDecoder)   │                                       │
│  └──────────────────┘                                       │
│                                                             │
│  非 SecureContext (LAN HTTP)                                │
│      │                                                      │
│      ▼                                                      │
│  ┌──────────────────┐                                       │
│  │ MSE + jmuxer     │  兼容性好，仅支持 H.264              │
│  │ (H.264 only)     │                                       │
│  └──────────────────┘                                       │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 5.2 WebSocket 连接

```javascript
function connect() {
    // 1. 清理旧连接
    tearDown();
    
    // 2. 构建 WebSocket URL
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/api/miot/ws/video_stream`
              + `?camera_id=${encodeURIComponent(camId)}`
              + `&channel=${encodeURIComponent(channel)}`
              + `&token=${encodeURIComponent(token)}`;
    
    // 3. 创建连接
    ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    
    // 4. 注册事件
    ws.addEventListener("open", onOpen);
    ws.addEventListener("close", onClose);
    ws.addEventListener("error", onError);
    ws.addEventListener("message", onMessage);
}
```

### 5.3 消息处理

```javascript
function onMessage(ev) {
    // 身份守卫：防止旧连接的消息污染新连接
    if (ev.currentTarget !== ws) return;
    
    stats.lastWsTs = Date.now();
    
    // 文本消息：init 或 error
    if (typeof ev.data === "string") {
        const msg = JSON.parse(ev.data);
        
        if (msg.type === "init") {
            // 初始化：记录 codec，等待首帧
            codecHint = msg.codec || "h264";
            setState("已连接摄像头，等待画面…");
        } else if (msg.type === "error") {
            // 错误：摄像头不可达等
            serverWillClose = true;
            setState(msg.message, true);
        }
        return;
    }
    
    // 二进制消息：视频帧
    const buf = new Uint8Array(ev.data);
    if (buf.byteLength < 16 || !codecHint) return;
    
    // 解析帧头
    const frameType = buf[0];  // 1=关键帧, 0=P帧
    let ts = Number(new DataView(buf.buffer, buf.byteOffset + 8, 8).getBigUint64(0));
    const nalu = buf.subarray(16);
    
    // 时间戳净化
    if (!Number.isSafeInteger(ts) || ts < 0 || ts >= 9e15) ts = 0;
    
    // 惰性解码器配置
    if (!configured) {
        if (frameType !== 1) return;  // 等待 IDR
        ensureDecoder(nalu, ts);
        return;
    }
    
    // 解码
    if (useMSE) {
        // MSE + jmuxer 路径
        jmuxer.feed({ video: nalu, duration: dur });
    } else {
        // WebCodecs 路径
        decoder.decode(new EncodedVideoChunk({
            type: frameType === 1 ? "key" : "delta",
            timestamp: ts * 1000,
            data: nalu,
        }));
    }
}
```

### 5.4 WebCodecs 解码

```javascript
async function ensureDecoder(firstKeyNAL, firstKeyTs) {
    // 1. 探测 codec 支持
    const pick = await pickCodecString(firstKeyNAL);
    if (!pick) throw new Error("No usable decoder");
    
    const { codec: codecString, hwAccel } = pick;
    
    // 2. 创建 VideoDecoder
    decoder = new VideoDecoder({
        output: (frame) => {
            // 绘制到 canvas
            const canvas = $("v");
            const ctx = canvas.getContext("2d");
            
            if (canvas.width !== frame.displayWidth) {
                canvas.width = frame.displayWidth;
                canvas.height = frame.displayHeight;
            }
            
            ctx.drawImage(frame, 0, 0);
            frame.close();
            
            // 更新统计
            stats.frames++;
            stats.lastFrameLocalMs = Date.now();
        },
        error: (e) => {
            console.error("decoder error", e);
            setState("解码器错误: " + e.message, true);
            configured = false;
        },
    });
    
    // 3. 配置解码器
    decoder.configure({
        codec: codecString,
        optimizeForLatency: true,
        hardwareAcceleration: hwAccel,
    });
    
    // 4. 提交首个 IDR
    decoder.decode(new EncodedVideoChunk({
        type: "key",
        timestamp: firstKeyTs * 1000,
        data: firstKeyNAL,
    }));
    
    configured = true;
}
```

### 5.5 MSE + jmuxer 解码

```javascript
async function ensureMseDecoder(firstKeyNAL, firstKeyTs) {
    // 1. 检查 jmuxer 是否加载
    if (typeof window.JMuxer === "undefined") {
        throw new Error("MSE 解码库未加载");
    }
    
    // 2. 切换显示：canvas 隐藏，video 显示
    const canvas = $("v");
    const video = $("vmse");
    canvas.classList.add("hidden");
    video.classList.remove("hidden");
    
    // 3. 创建 jmuxer 实例
    jmuxer = new JMuxer({
        node: video,
        mode: "video",
        flushingTime: 0,  // 立即 flush，降低延迟
        fps: 25,
        onError: (data) => {
            console.warn("jmuxer error", data);
            setState("MSE 解码错误", true);
        },
    });
    
    // 4. 喂首个 IDR
    jmuxer.feed({ video: firstKeyNAL, duration: 40 });
    lastMseTs = firstKeyTs;
    
    configured = true;
}
```

### 5.6 Codec 探测

```javascript
async function pickCodecString(firstKeyNAL) {
    if (codecHint === "h264") {
        // 从 SPS 推导 codec string
        const sps = findSpsNal(firstKeyNAL, "h264");
        const derived = sps && avcCodecFromSps(sps);
        
        // 候选列表
        const candidates = derived 
            ? [derived, ...avcCodecCandidates()]
            : avcCodecCandidates();
        
        // 逐个探测
        for (const codec of candidates) {
            const r = await probeCodec(codec);
            if (r) return r;
        }
        
        return null;
    }
    
    // H.265：探测候选字符串
    for (const codec of hevcCodecCandidates()) {
        const r = await probeCodec(codec);
        if (r) return r;
    }
    
    return null;
}

async function probeCodec(codec) {
    // 尝试不同的硬件加速模式
    for (const hwAccel of ["prefer-hardware", "prefer-software", "no-preference"]) {
        try {
            const sup = await VideoDecoder.isConfigSupported({
                codec,
                hardwareAcceleration: hwAccel,
            });
            if (sup.supported) return { codec, hwAccel };
        } catch { /* keep trying */ }
    }
    return null;
}
```

### 5.7 容错机制

```javascript
// 首帧看门狗
async function _first_frame_watchdog(websocket, camera_id, channel) {
    await asyncio.sleep(15);  // 15秒超时
    
    if (miot_video_stream_manager.has_emitted_frame(camera_id, channel)) {
        return;  // 有帧，正常退出
    }
    
    // 无帧，报错
    await websocket.send_text(JSON.stringify({
        type: "error",
        reason: "camera_unreachable",
        message: "连不上摄像头（可能不在同一局域网，或摄像头离线）",
    }));
    await websocket.close();
}

// 流停滞检测
setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    
    const now = Date.now();
    const wsAge = stats.lastWsTs ? now - stats.lastWsTs : 0;
    
    // WS 15秒无消息 → 重连
    if (wsAge > 15000) {
        setState("WS 15s 无消息，重连…", true);
        connect();
        return;
    }
    
    // 解码 8秒无帧 → 重连
    if (configured && stats.lastFrameLocalMs && now - stats.lastFrameLocalMs > 8000) {
        setState("解码停滞 8s+，重连…", true);
        connect();
    }
}, 2000);
```

### 5.8 时间戳净化

```javascript
// 前后端双重校验，防止哨兵值导致解码失败

// 服务端（ws.py）
_TS_SAFE_MAX = 9_000_000_000_000_000;  // 9e15
wire_ts = ts if 0 <= ts < _TS_SAFE_MAX else decoded_unix_ms;

// 前端（watch.html）
if (!Number.isSafeInteger(ts) || ts < 0 || ts >= 9e15) ts = 0;
```

---

## 6. 视频录制功能

**核心文件**: `miot/ws.py` (NalClipRecorder)

### 6.1 NalClipRecorder

```python
class NalClipRecorder:
    """
    一次性 BGR → mp4 录制器
    
    用途：身份录入等场景的 15 秒视频录制
    
    状态机：
    WAITING_FIRST → RECORDING → DONE
    
    特性：
    - 每帧立即编码，不做缓冲
    - 使用独立的 libx264 编码器
    - ultrafast preset，~3-8ms/帧
    """
    
    def __init__(self, duration_ms: int = 15000):
        self._duration_ms = duration_ms
        self._state = "WAITING_FIRST"
        self._start_ts = None
        self._frame_count = 0
        self._out_buf = None
        self._container = None
        self._stream = None
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._result_future = asyncio.get_running_loop().create_future()
```

### 6.2 录制流程

```python
async def feed_bgr(self, bgr: NDArray, ts_ms: int) -> None:
    """
    喂入 BGR 帧
    
    流程：
    1. 如果是首帧，初始化编码器
    2. 编码当前帧
    3. 检查是否达到目标时长
    4. 如果是，触发 finalize
    """
    if self._state == "DONE":
        return
    
    loop = asyncio.get_running_loop()
    
    # 首帧：初始化编码器
    if self._state == "WAITING_FIRST":
        h, w = int(bgr.shape[0]), int(bgr.shape[1])
        await loop.run_in_executor(self._executor, self._init_encoder, w, h)
        self._start_ts = ts_ms
        self._state = "RECORDING"
    
    # 编码当前帧
    await loop.run_in_executor(self._executor, self._encode_frame_sync, bgr)
    self._frame_count += 1
    
    # 检查时长
    elapsed = ts_ms - self._start_ts
    if elapsed >= self._duration_ms:
        self._state = "DONE"
        asyncio.ensure_future(self._finalize_async())


def _init_encoder(self, width: int, height: int) -> None:
    """初始化编码器"""
    self._out_buf = io.BytesIO()
    self._container = av.open(self._out_buf, mode="w", format="mp4")
    self._stream = self._container.add_stream("h264", rate=30)
    self._stream.width = width
    self._stream.height = height
    self._stream.pix_fmt = "yuv420p"
    self._stream.options = {
        "preset": "ultrafast",
        "tune": "zerolatency",
    }


def _encode_frame_sync(self, bgr: NDArray) -> None:
    """编码单帧"""
    frame = av.VideoFrame.from_ndarray(bgr, format="bgr24")
    frame.pts = self._frame_count
    for packet in self._stream.encode(frame):
        self._container.mux(packet)


async def _finalize_async(self) -> None:
    """完成录制"""
    loop = asyncio.get_running_loop()
    mp4_bytes = await loop.run_in_executor(self._executor, self._finalize_sync)
    self._result_future.set_result(mp4_bytes)


def _finalize_sync(self) -> bytes:
    """同步完成"""
    for packet in self._stream.encode():
        self._container.mux(packet)
    self._container.close()
    return self._out_buf.getvalue()


async def wait(self, timeout: float) -> bytes:
    """等待录制完成"""
    return await asyncio.wait_for(self._result_future, timeout=timeout)
```

### 6.3 录制器集成

```python
# 在 MIoTVideoStreamManager 中集成
async def register_recorder(
    self,
    camera_id: str,
    channel: int,
    recorder: NalClipRecorder,
) -> None:
    """注册录制器"""
    camera_tag = f"{camera_id}.{channel}"
    
    async with self._lock_for(camera_tag):
        # 如果没有订阅者，启动 SDK 流
        if not self._has_subscribers(camera_tag):
            await self._ensure_sdk_subscription(camera_id, channel, camera_tag)
        
        self._camera_recorders.setdefault(camera_tag, []).append(recorder)


async def unregister_recorder(
    self,
    camera_id: str,
    channel: int,
    recorder: NalClipRecorder,
) -> None:
    """注销录制器"""
    camera_tag = f"{camera_id}.{channel}"
    
    async with self._lock_for(camera_tag):
        lst = self._camera_recorders.get(camera_tag)
        if lst is not None:
            lst.remove(recorder)
            if not lst:
                self._camera_recorders.pop(camera_tag, None)
        
        # 如果没有订阅者了，停止 SDK 流
        await self._teardown_if_idle(camera_id, channel, camera_tag)
```

---

## 7. 关键设计决策

### 7.1 统一 H.264 输出

**原因**: 浏览器兼容性
- H.264: 所有浏览器都支持
- H.265: 部分浏览器不支持（尤其是 LAN HTTP 场景）

**实现**: 使用 H264LiveEncoder 将所有视频转码为 H.264

### 7.2 多消费者共享解码

**原因**: 减少带宽和解码开销
- 实时预览需要视频流
- 感知引擎也需要视频流
- 如果各建一条 PPCS 连接，带宽翻倍

**实现**: 使用 `multi_reg=True` 让多个消费者共享同一解码流

### 7.3 双解码路径

**原因**: 兼容性和性能的平衡
- WebCodecs: 低延迟，但需要 SecureContext
- MSE/jmuxer: 兼容性好，但延迟稍高

**实现**: 自动检测环境，选择最优路径

### 7.4 帧级容错

**原因**: 确保浏览器从干净的 GOP 边界开始解码
- 如果从 P 帧开始，解码会失败
- 必须等待 IDR 帧

**实现**: 
- 服务端：丢弃首个 IDR 前的帧
- 前端：等待 IDR 才开始解码

### 7.5 时间戳净化

**原因**: 防止摄像头哨兵值导致解码失败
- 摄像头在 "PTS 未知" 时发哨兵值 0xFFFFFFFFFFFFFFFF
- PyAV 的 frame.pts setter 是 signed int64，哨兵值会抛 OverflowError

**实现**: 前后端双重校验，超出安全范围时使用服务端时间

### 7.6 单线程编码

**原因**: 
- libx264 的 codec context 不是线程安全的
- 多 slice 会导致浏览器硬解失败

**实现**: 使用 dedicated 单线程执行器

### 7.7 固定 GOP

**原因**: 
- 平衡带宽和首帧等待时间
- GOP=30，约1.2秒@25fps
- 晚到者最多等待一个 GOP 就能开始解码

**实现**: 设置 `keyint=30:min-keyint=30:scenecut=0`

---

## 8. 核心文件索引

### 视频采集层
- `miot/camera_handler.py` — 摄像头处理器
- `miot/client.py` — MIoT 客户端
- `miot/types.py` — 类型定义

### 视频流管理层
- `miot/ws.py` — WebSocket 流管理器
  - `MIoTVideoStreamManager` — 视频流管理
  - `MIoTAudioStreamManager` — 音频流管理
  - `NalClipRecorder` — 视频录制器

### 视频转码层
- `miot/transcoder.py` — H264LiveEncoder

### 感知引擎集成
- `perception/collect/camera_adapter.py` — 摄像头适配器
- `perception/collect/stream_buffer.py` — 流缓冲区
- `perception/collect/collector.py` — 多模态采集器

### 前端播放层
- `web/public/watch.html` — 实时视频播放器
- `web/src/components/LivePlayerPlaceholder.tsx` — React 组件

### 路由
- `miot/router.py` — 视频相关 API 路由
  - `GET /api/miot/watch` — watch 页面
  - `WS /api/miot/ws/video_stream` — 视频流 WebSocket
  - `WS /api/miot/ws/audio_stream` — 音频流 WebSocket

