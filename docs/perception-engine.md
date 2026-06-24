# Xiaomi Miloco 感知引擎详解

## 目录

- [1. 整体架构](#1-整体架构)
- [2. 数据采集层 (Collector)](#2-数据采集层-collector)
- [3. 门控层 (Gate)](#3-门控层-gate)
- [4. 身份识别层 (Identity)](#4-身份识别层-identity)
- [5. 多模态AI层 (Omni)](#5-多模态ai层-omni)
- [6. 结果输出层](#6-结果输出层)
- [7. 多摄像头并行处理](#7-多摄像头并行处理)
- [8. 关键设计决策](#8-关键设计决策)
- [9. 核心文件索引](#9-核心文件索引)

---

## 1. 整体架构

感知引擎是 Miloco 的核心，负责从摄像头视频/音频中提取有意义的信息。整体流程分为 **5 个阶段**：

```
┌─────────────────────────────────────────────────────────────────┐
│                    感知引擎整体架构                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐  │
│  │  摄像头   │───▶│  采集层   │───▶│  门控层   │───▶│ 身份识别 │  │
│  │ 视频/音频 │    │ Collector│    │   Gate   │    │ Identity │  │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘  │
│                                                       │        │
│                                                       ▼        │
│                                                  ┌──────────┐  │
│                                                  │ Omni AI  │  │
│                                                  │ 多模态分析│  │
│                                                  └──────────┘  │
│                                                       │        │
│                                                       ▼        │
│                                                  ┌──────────┐  │
│                                                  │ 结果输出  │  │
│                                                  │ 事件触发  │  │
│                                                  └──────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### 核心流程

```python
# 单次感知周期
InputSlice → Gate → Identity → Omni → PipelineResult

# 批量感知周期（多摄像头）
BatchedSnapshot → [per-device Gate → Identity → Omni] → BatchPipelineResult
```

### 关键特性

- **门控优化**: 先做廉价的帧差分，再做昂贵的AI分析
- **多摄像头并行**: 同房间/跨房间摄像头并发处理
- **流式处理**: 语音和建议早出，减少端到端延迟
- **身份融合**: Fused 模式将身份识别和场景理解合并调用

---

## 2. 数据采集层 (Collector)

**核心文件**: 
- `perception/collect/collector.py`
- `perception/collect/camera_adapter.py`
- `perception/collect/stream_buffer.py`

### 2.1 时间窗口聚合

使用 `MultiTrackSyncBuffer` 进行多轨时间窗口聚合：

```python
class MultiTrackSyncBuffer:
    """
    多轨时间窗口聚合缓冲区
    
    功能：
    - 按固定时间窗口（如5秒）聚合视频帧和音频帧
    - 窗口"就绪"条件：所有轨道都有数据，或窗口过期
    - 处理音视频启动延迟（音频可能比视频早到几百ms）
    """
    
    def __init__(self, track_names: list[str], window_ms: int = 5000):
        self._track_names = track_names  # ["decoded_video", "decoded_audio"]
        self._window_ms = window_ms
        self._windows: dict[int, dict[str, list]] = {}
```

### 2.2 摄像头适配器

```python
class CameraDeviceAdapter(BaseDeviceAdapter):
    """
    摄像头设备适配器
    
    职责：
    - 订阅解码后的视频帧和音频帧
    - 管理每个摄像头的 MultiTrackSyncBuffer
    - 处理设备上线/下线
    """
    
    _CAMERA_TRACKS = ["decoded_video", "decoded_audio"]
    
    async def _make_decoded_video_callback(self, did: str):
        """创建视频帧回调"""
        def on_frame(frame: VideoFrame, ts: int, ...):
            # 将帧放入对应摄像头的 sync_buffer
            state = self._devices[did]
            fragment = StreamFragment(
                track="decoded_video",
                data=DecodedVideoFrame(frame=frame.to_ndarray(format="bgr24"), ts=ts),
                timestamp_ms=ts
            )
            state.sync_buffer.push(fragment)
        return on_frame
```

### 2.3 采集器

```python
class MultimodalCollector:
    """
    多模态数据采集器
    
    职责：
    - 管理所有摄像头的 CameraDeviceAdapter
    - 从各 sync_buffer 取出就绪的窗口
    - 组装成 BatchedSnapshot
    """
    
    def collect_batch(self, drain=True) -> BatchedSnapshot:
        """
        采集就绪的窗口批次
        
        返回：
        - BatchedSnapshot: 按房间分组的设备快照
        - 包含：视频帧列表、音频片段、设备元数据
        """
        snapshots = []
        for did, adapter in self._adapters.items():
            window = adapter.sync_buffer.drain_ready()
            if window:
                snapshots.append(DeviceSnapshot(
                    device=adapter.device_info,
                    frames=window["decoded_video"],
                    audio_clip=window["decoded_audio"],
                    start_timestamp=window.start_ts,
                    end_timestamp=window.end_ts,
                    fps=adapter.fps,
                ))
        
        return BatchedSnapshot(snapshots=snapshots)
```

### 2.4 关键数据结构

```python
@dataclass
class DeviceSnapshot:
    """单设备单窗口快照"""
    device: PerceptionDevice       # 设备信息（did、名称、房间）
    frames: list[NDArray]          # BGR 视频帧列表
    audio_clip: AudioClip | None   # 音频片段
    start_timestamp: int           # 窗口起始时间（ms）
    end_timestamp: int             # 窗口结束时间（ms）
    fps: int                       # 帧率

@dataclass
class BatchedSnapshot:
    """多设备批次快照"""
    snapshots: list[DeviceSnapshot]
    
    def by_room(self) -> dict[str, list[DeviceSnapshot]]:
        """按房间分组"""
        result = {}
        for s in self.snapshots:
            room = s.device.room_name or "unknown"
            result.setdefault(room, []).append(s)
        return result
    
    @property
    def empty(self) -> bool:
        return len(self.snapshots) == 0
```

---

## 3. 门控层 (Gate)

**核心文件**: 
- `perception/engine/gate/gate.py`
- `perception/engine/gate/visual_gate.py`
- `perception/engine/gate/audio_gate.py`
- `perception/engine/gate/speech_vad.py`

门控层是**性能优化的关键**——只有检测到变化时才进入后续昂贵的AI分析。

### 3.1 视觉门控 (Visual Gate)

使用**帧差分法**检测画面变化：

```python
def evaluate_visual(
    frames: list[NDArray],
    config: GateConfig,
    input_fps: int = 1,
    prev_frame: NDArray | None = None,
) -> VisualEvalResult:
    """
    视觉门控评估
    
    算法：
    1. 预处理：缩放到 448×448 灰度图
    2. 帧内差分：相邻帧之间的像素变化比例
    3. 跨窗口差分：与上一窗口最后一帧的变化
    4. 变化判定：变化比例 > threshold → 通过
    
    返回：
    - changed: 是否有显著变化
    - max_score: 最大变化分数
    - intra_max: 帧内最大变化
    - cross_max: 跨窗口最大变化
    """
    
    # 预处理
    def _preprocess(frame: NDArray) -> NDArray:
        small = cv2.resize(frame, (448, 448))
        return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
    
    # 像素级差异计算
    def _diff_processed(gray_a: NDArray, gray_b: NDArray, pixel_threshold=25) -> float:
        diff = cv2.absdiff(gray_a, gray_b)
        changed_pixels = np.sum(diff > pixel_threshold)
        return changed_pixels / diff.size
    
    # 帧内差分（相邻帧）
    intra_scores = []
    for i in range(1, len(frames)):
        score = _diff_processed(
            _preprocess(frames[i-1]),
            _preprocess(frames[i])
        )
        intra_scores.append(score)
    
    # 跨窗口差分（与上一窗口最后一帧）
    cross_score = 0.0
    if prev_frame is not None:
        cross_score = _diff_processed(
            _preprocess(prev_frame),
            _preprocess(frames[-1])
        )
    
    max_score = max(max(intra_scores, default=0), cross_score)
    changed = max_score > config.change_threshold  # 默认 0.005
    
    return VisualEvalResult(
        changed=changed,
        max_score=max_score,
        intra_max=max(intra_scores, default=0),
        cross_max=cross_score,
        last_checked=_preprocess(frames[-1]),
    )
```

### 3.2 音频门控 (Audio Gate)

```python
def evaluate_audio(
    audio_clip: AudioClip | None,
    config: GateConfig,
) -> tuple[bool, float]:
    """
    音频门控评估
    
    算法：
    1. 计算音频能量（RMS）
    2. 能量 > threshold → 音频活跃
    
    返回：
    - audio_active: 音频是否活跃
    - audio_energy: 音频能量值
    """
    if audio_clip is None or len(audio_clip.samples) == 0:
        return False, 0.0
    
    # 计算 RMS 能量
    samples = audio_clip.samples.astype(np.float32) / 32768.0
    energy = np.sqrt(np.mean(samples ** 2))
    
    audio_active = energy > config.audio_energy_threshold  # 默认 0.01
    return audio_active, energy
```

### 3.3 语音活动检测 (VAD)

使用 Silero VAD 模型检测人声：

```python
def evaluate_speech(
    audio_clip: AudioClip | None,
    config: GateConfig,
) -> tuple[bool, float]:
    """
    语音活动检测
    
    使用 Silero ONNX 模型：
    1. 将音频切分为 512 样本的块
    2. 逐块送入 VAD 模型
    3. 语音概率 > threshold → 有人声
    
    返回：
    - speech_active: 是否有人声
    - speech_prob: 语音概率
    """
    if audio_clip is None:
        return False, 0.0
    
    # 仅在音频过能量 gate 时跑 VAD
    # 没过 gate 音频本就不喂，无需判人声
    ...
```

### 3.4 Hold 滞回机制

避免频繁开关——视觉变化停止后，仍保持一段时间的"活跃"状态：

```python
def run_gate(input_slice, config, input_fps, prev_frame, 
             last_visual_pass_ts, last_audio_pass_ts):
    """
    门控主函数
    
    Hold 机制：
    - 视觉变化停止后，如果音频仍活跃，继续保持"通过"状态
    - hold_duration_sec 内（默认30秒），即使视觉未通过，只要音频活跃就继续处理
    - 避免人在静止时（如看电视）频繁中断感知
    """
    
    visual = evaluate_visual(input_slice.frames, config, input_fps, prev_frame)
    audio_active, audio_energy = evaluate_audio(input_slice.audio_clip, config)
    speech_active, speech_prob = evaluate_speech(input_slice.audio_clip, config)
    
    now = time.monotonic()
    
    # 基本通过条件
    any_pass = visual.changed or audio_active
    
    # Hold 滞回条件
    hold_active = False
    if last_visual_pass_ts is not None:
        time_since_visual_pass = now - last_visual_pass_ts
        hold_active = (
            time_since_visual_pass < config.hold_duration_sec  # 默认 30 秒
            and (audio_active or speech_active)
        )
    
    # 更新时间戳
    new_last_visual_pass_ts = now if visual.changed else last_visual_pass_ts
    new_last_audio_pass_ts = now if audio_active else last_audio_pass_ts
    
    # 最终判定
    if not any_pass and not hold_active:
        return None, timing, ...  # 跳过
    
    # 构建 GatePacket
    packet = GatePacket(
        frames=input_slice.frames,
        audio_clip=input_slice.audio_clip if (audio_active or speech_active) else None,
        fps=input_fps,
        trigger=GateTrigger(
            visual_changed=visual.changed,
            audio_active=audio_active,
            speech_active=speech_active,
            hold_active=hold_active,
        ),
    )
    
    return packet, timing, ...
```

### 3.5 门控配置

```python
@dataclass
class GateConfig:
    """门控配置"""
    change_threshold: float = 0.005        # 视觉变化阈值（0.5%）
    pixel_threshold: int = 25              # 像素差异阈值
    audio_energy_threshold: float = 0.01   # 音频能量阈值
    speech_threshold: float = 0.5          # 语音概率阈值
    hold_duration_sec: float = 30.0        # Hold 滞回时长（秒）
```

### 3.6 门控输出

```python
@dataclass
class GateTiming:
    """门控性能指标"""
    total_ms: float        # 总耗时
    video_ms: float        # 视觉门控耗时
    audio_ms: float        # 音频门控耗时
    vad_ms: float          # VAD 耗时
    video_pass: bool       # 视觉是否通过
    audio_pass: bool       # 音频是否通过
    hold_pass: bool        # 滞回是否激活
    video_score: float     # 视觉变化分数
    audio_energy: float    # 音频能量
    speech_prob: float     # 语音概率

@dataclass
class GatePacket:
    """门控输出数据包"""
    frames: list[NDArray]           # 视频帧
    audio_clip: AudioClip | None    # 音频片段（仅在音频活跃时包含）
    fps: int                        # 帧率
    trigger: GateTrigger            # 触发原因
    
@dataclass
class GateTrigger:
    """触发原因"""
    visual_changed: bool    # 视觉变化
    audio_active: bool      # 音频活跃
    speech_active: bool     # 语音活动
    hold_active: bool       # Hold 滞回
```

---

## 4. 身份识别层 (Identity)

**核心文件**: 
- `perception/engine/identity/identity.py`
- `perception/engine/identity/engine.py`
- `perception/engine/identity/tracking_service.py`
- `perception/engine/identity/library.py`
- `perception/engine/identity/dispatcher.py`

身份识别分为两步：**跟踪** + **识别**

### 4.1 目标跟踪 (Tracking)

使用 **DeepSORT** 算法进行多目标跟踪：

```python
class TrackingService:
    """
    目标跟踪服务
    
    使用 DeepSORT 算法：
    1. YOLO 检测人体边界框
    2. ReID 特征提取
    3. 匈牙利匹配 + 卡尔曼滤波
    4. 输出：每个 track 的 bbox、track_id、运动状态
    """
    
    def analyze(
        self,
        frames: list[NDArray],
        fps: int,
    ) -> TrackingResponse:
        """
        分析视频帧序列，返回跟踪结果
        """
        objects = []
        for frame in frames:
            # 1. YOLO 人体检测
            detections = self._detector.detect(frame)
            
            # 2. DeepSORT 跟踪
            tracks = self._tracker.update(detections, frame)
            
            for track in tracks:
                objects.append(TrackedObject(
                    track_id=track.track_id,
                    bbox=track.bbox,
                    confidence=track.confidence,
                    face_id=None,  # 后续由 IdentityEngine 填充
                ))
        
        return TrackingResponse(object_info=objects)
```

### 4.2 跟踪输出

```python
@dataclass
class TrackedObject:
    """跟踪对象"""
    track_id: int                       # 跟踪ID
    bbox: tuple[int, int, int, int]     # 边界框 (x1, y1, x2, y2)
    confidence: float                   # 置信度
    face_id: str | None                 # 人脸ID（后续填充）

@dataclass
class TrackingResponse:
    """跟踪响应"""
    object_info: list[TrackedObject]
```

### 4.3 身份识别引擎 (Identity Engine)

```python
class IdentityEngine:
    """
    身份识别系统编排器
    
    三级身份库：
    - tier_a: 用户主动录入的已知人员（高质量样本）
    - tier_c: 系统自动采集的陌生人样本（在线累积）
    - tier_u: 陌生人临时池（单次会话）
    """
    
    def __init__(self, config, library, tier_u_pool):
        self._config = config
        self._library = library      # IdentityLibrary (tier_a + tier_c)
        self._tier_u_pool = tier_u_pool  # 陌生人临时池
        self._dispatcher = None      # 异步识别调度器
```

### 4.4 识别流程

```python
async def process(
    self,
    objects: list[dict],
    latest_frame: NDArray,
    camera_tag: str,
) -> dict[int, str]:
    """
    处理跟踪结果，返回 track_id → person_id 映射
    
    流程：
    1. 对每个 track 裁剪人体区域
    2. 提取 ReID 特征向量
    3. 与 tier_a/tier_c 库做相似度匹配
    4. 匹配成功 → 返回 person_id
    5. 匹配失败 → 加入 tier_u 陌生人池
    6. 异步触发 omni 识别（可选）
    """
    result = {}
    
    for obj in objects:
        track_id = obj["track_id"]
        bbox = obj["bbox"]
        
        # 1. 裁剪人体区域
        crop = self._crop_person(latest_frame, bbox)
        if crop is None:
            result[track_id] = "unknown"
            continue
        
        # 2. 提取 ReID 特征
        embedding = self._extract_embedding(crop)
        if embedding is None:
            result[track_id] = "unknown"
            continue
        
        # 3. 与 tier_a 匹配（已知人员）
        match = self._library.match_tier_a(embedding, threshold=0.7)
        if match:
            result[track_id] = match.person_id
            continue
        
        # 4. 与 tier_c 匹配（自动采集的陌生人）
        match = self._library.match_tier_c(embedding, threshold=0.6)
        if match:
            result[track_id] = match.person_id
            continue
        
        # 5. 加入 tier_u 陌生人池
        stranger_id = self._tier_u_pool.push(
            embedding=embedding,
            crop=crop,
            camera_tag=camera_tag,
        )
        result[track_id] = stranger_id
        
        # 6. 异步触发 omni 识别（可选）
        if self._config.async_identify:
            self._dispatcher.submit(track_id, crop, embedding)
    
    return result
```

### 4.5 身份库 (Identity Library)

```python
class IdentityLibrary:
    """
    身份库管理
    
    三级结构：
    - tier_a: 用户主动录入
        - 高质量样本（清晰正面照、多角度）
        - 持久化存储
        - 用户可编辑/删除
    
    - tier_c: 系统自动采集
        - 在线累积的陌生人样本
        - 需要 omni 校验后入库
        - 自动清理低质量样本
    
    - tier_u: 陌生人临时池
        - 单次会话的陌生人
        - 用于跨窗口追踪
        - 会话结束清理
    """
    
    def match_tier_a(
        self,
        embedding: NDArray,
        threshold: float = 0.7,
    ) -> MatchResult | None:
        """与 tier_a 库匹配"""
        for person in self._tier_a.values():
            for sample in person.samples:
                similarity = cosine_similarity(embedding, sample.embedding)
                if similarity > threshold:
                    return MatchResult(
                        person_id=person.person_id,
                        similarity=similarity,
                    )
        return None
```

### 4.6 Gallery 复合图

用于 omni 模型识别时提供参考：

```python
def build_body_composite_png(
    samples: list[NDArray],
    target_height: int = 256,
) -> bytes:
    """
    将多张人体样本水平拼接成一张复合图
    
    用途：
    - 发送给 omni 模型作为身份参考
    - 减少 API 调用次数
    - 提供多角度信息
    """
    # 缩放到统一高度
    resized = []
    for img in samples:
        h, w = img.shape[:2]
        scale = target_height / h
        new_w = int(w * scale)
        resized.append(cv2.resize(img, (new_w, target_height)))
    
    # 水平拼接
    composite = np.hstack(resized)
    
    # 编码为 PNG
    _, png = cv2.imencode('.png', composite)
    return png.tobytes()


def build_face_composite_png(
    samples: list[NDArray],
    target_height: int = 256,
) -> bytes:
    """
    将多张人脸样本水平拼接
    
    用途：
    - 人脸比全身更具辨识度
    - 与 body composite 配合使用
    """
    # 类似 body composite 的处理
    ...
```

### 4.7 Fused 模式

将身份识别和场景理解合并到同一次 omni 调用：

```python
async def run_omni_fused(
    packets: list[IdentityPacket],
    context: OmniContext,
    config: OmniConfig,
    identity_engine: IdentityEngine,
) -> OmniOutput:
    """
    Fused 主调用：身份识别和场景理解合并
    
    优势：
    - 减少一次独立的 omni 调用
    - 模型可以同时看到场景和参考图
    - 识别准确率更高
    
    流程：
    1. 构建包含 gallery 参考图的 prompt
    2. 一次性发送给 omni 模型
    3. 模型同时输出：
       - 场景描述 (caption)
       - 语音内容 (speeches)
       - 建议动作 (suggestions)
       - 身份分配 (identity_assignments)
    """
    # 1. 获取待识别的 candidates
    candidates = identity_engine.take_pending()
    
    # 2. 获取 gallery 快照
    gallery_snapshot = identity_engine.get_gallery_snapshot(
        [c.person_id for c in candidates]
    )
    
    # 3. 构建 fused payload
    payload = build_fused_payload(
        packets=packets,
        context=context,
        candidates=candidates,
        gallery_snapshot=gallery_snapshot,
    )
    
    # 4. 调用 omni
    response = await call_omni_messages(payload["messages"], config)
    
    # 5. 解析响应
    output = parse_omni_response(response)
    identity_assignments = parse_identity_assignments(response)
    
    # 6. 回传身份识别结果
    await identity_engine.deliver_fused_response(identity_assignments)
    
    return output
```

### 4.8 身份识别输出

```python
@dataclass
class IdentityTarget:
    """身份识别目标"""
    track_id: int
    bbox: tuple[int, int, int, int]
    person_id: str
    confidence: float
    motion_state: MotionState

@dataclass
class IdentityPacket:
    """身份识别输出数据包"""
    frames: list[NDArray]
    audio_clip: AudioClip | None
    fps: int
    targets: list[IdentityTarget]
    scene_motion: MotionState
    frame_info: FrameInfo
```

---

## 5. 多模态AI层 (Omni)

**核心文件**: 
- `perception/engine/omni/omni.py`
- `perception/engine/omni/prompt_builder.py`
- `perception/engine/omni/omni_client.py`
- `perception/engine/omni/response_parser.py`
- `perception/engine/omni/constants.py`

### 5.1 Prompt 构建

```python
def build_prompt(
    identity_packet: IdentityPacket,
    context: OmniContext,
    label_lookup: dict[str, str] | None = None,
) -> dict:
    """
    构建发送给 MiMo 模型的 prompt
    
    返回：
    - system_prompt: 系统提示词
    - user_content: 用户内容（文本）
    - video_base64: 编码后的视频片段
    - video_fps: 视频帧率
    - crops: 人体裁剪图列表
    """
    
    # 系统提示词
    system_prompt = "\n\n".join([
        _ROLE,           # 角色定义
        _OUTPUT_MODE_JSON,  # 输出格式
        _COMMONSENSE,    # 常识规则
        home_profile,    # 家庭档案
    ])
    
    # 用户内容
    user_content = _build_user_content(identity_packet, context, label_lookup)
    
    # 视频编码
    video_base64 = _encode_video(identity_packet.frames, identity_packet.fps)
    
    # 人体裁剪
    crops = _extract_crops(identity_packet)
    
    return {
        "system_prompt": system_prompt,
        "user_content": user_content,
        "video_base64": video_base64,
        "video_fps": identity_packet.fps,
        "crops": crops,
    }
```

### 5.2 系统提示词

```python
_ROLE = """你是一个家庭AI管家，通过摄像头观察家庭环境。
你需要理解画面内容，识别家庭成员，提供有用的建议。
请用 JSON 格式回复。"""

_COMMONSENSE = """安全规则：
- 检测儿童危险行为（玩刀具、攀爬高处、触摸电器等）
- 检测老人跌倒或行动不便
- 检测异常情况（陌生人闯入、火灾烟雾等）
- 检测宠物异常行为

礼貌规则：
- 尊重隐私，不描述敏感内容
- 提供有帮助的建议
- 语气温和友善
- 避免过度打扰

常识判断：
- 区分正常活动和异常事件
- 理解日常行为模式
- 识别潜在风险"""

_PRINCIPLE = """分析原则：
1. 优先关注安全相关事件
2. 识别家庭成员并提供个性化服务
3. 理解上下文，提供有意义的回应
4. 区分正常活动和异常事件
5. 考虑时间、地点、人物关系"""
```

### 5.3 用户内容构建

```python
def _build_user_content(
    packet: IdentityPacket,
    context: OmniContext,
    label_lookup: dict[str, str] | None,
) -> str:
    """
    构建用户内容
    
    包含：
    - 设备信息（房间、摄像头名称）
    - 时间窗口
    - 已识别人员（如果有）
    - 用户查询（如果是主动查询）
    """
    parts = []
    
    # 设备信息
    parts.append(f"房间: {context.room_name}")
    parts.append(f"摄像头: {context.device_name}")
    parts.append(f"时间: {context.time_window}")
    
    # 已识别人员
    if packet.targets:
        parts.append("\n已识别人员:")
        for target in packet.targets:
            name = label_lookup.get(target.person_id, target.person_id)
            parts.append(f"- {name} (置信度: {target.confidence:.2f})")
    
    # 用户查询
    if context.query:
        parts.append(f"\n用户查询: {context.query}")
    
    # 上一轮对话
    if context.last_caption:
        parts.append(f"\n上一轮描述: {context.last_caption}")
    
    return "\n".join(parts)
```

### 5.4 视频编码

```python
def _encode_video(frames: list[NDArray], fps: int) -> str:
    """
    将视频帧编码为 mp4 base64
    
    使用 PyAV 进行 H.264 编码：
    - preset: ultrafast
    - tune: zerolatency
    - GOP: 30 帧
    """
    output = io.BytesIO()
    container = av.open(output, mode='w', format='mp4')
    stream = container.add_stream('h264', rate=fps)
    stream.width = frames[0].shape[1]
    stream.height = frames[0].shape[0]
    stream.pix_fmt = 'yuv420p'
    stream.options = {
        'preset': 'ultrafast',
        'tune': 'zerolatency',
    }
    
    for i, frame in enumerate(frames):
        av_frame = av.VideoFrame.from_ndarray(frame, format='bgr24')
        av_frame.pts = i
        for packet in stream.encode(av_frame):
            container.mux(packet)
    
    # Flush
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    
    return base64.b64encode(output.getvalue()).decode('utf-8')
```

### 5.5 模型调用

```python
async def call_omni(
    messages: list[dict],
    config: OmniConfig,
    type: str = "realtime",
) -> dict:
    """
    调用 MiMo 多模态模型
    
    参数：
    - model: "MiMo-v2.5" 或 "MiMo-v2.5-pro"
    - messages: 包含文本、图片、视频、音频
    - temperature: 0.1（低随机性）
    - max_tokens: 1024
    - stream: 是否流式响应
    
    错误处理：
    - 超时: 30 秒
    - 重试: 429/5xx 自动重试 3 次
    - 降级: 失败时返回 skipped
    """
    client = httpx.AsyncClient(timeout=30.0)
    
    request = {
        "model": config.model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 1024,
        "stream": config.stream,
    }
    
    for attempt in range(3):
        try:
            response = await client.post(
                f"{config.base_url}/chat/completions",
                json=request,
                headers={"Authorization": f"Bearer {config.api_key}"},
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 500, 502, 503):
                await asyncio.sleep(2 ** attempt)
                continue
            raise
        except httpx.TimeoutException:
            if attempt < 2:
                continue
            raise
```

### 5.6 流式处理

```python
async def run_omni_stream(
    packet: IdentityPacket,
    context: OmniContext,
    config: OmniConfig,
    on_early_speeches: Callable | None = None,
    on_early_suggestions: Callable | None = None,
) -> OmniOutput:
    """
    流式处理模式
    
    优势：
    - 语音内容早出（减少 500-1000ms 延迟）
    - 建议早出（用户更快看到结果）
    - 更好的用户体验
    """
    # 构建 prompt
    prompt = build_stream_prompt(packet, context)
    
    # 流式调用
    buffer = ""
    speeches = []
    suggestions = []
    
    async for chunk in call_omni_stream(prompt, config):
        buffer += chunk
        
        # 检测复读
        if _has_loopback_tail(buffer):
            break
        
        # 提取早出字段
        early_speeches = extract_speeches(buffer)
        if early_speeches and on_early_speeches:
            await on_early_speeches(early_speeches)
            speeches.extend(early_speeches)
        
        early_suggestions = extract_suggestions(buffer)
        if early_suggestions and on_early_suggestions:
            await on_early_suggestions(early_suggestions)
            suggestions.extend(early_suggestions)
    
    # 解析完整响应
    output = parse_omni_response(buffer)
    output.speeches = speeches
    output.suggestions = suggestions
    
    return output
```

### 5.7 响应解析

```python
def parse_omni_response(response: dict) -> OmniOutput:
    """
    解析 omni 模型响应
    
    JSON 格式：
    {
        "caption": "场景描述",
        "speeches": [
            {"content": "语音内容", "start_ms": 0, "end_ms": 1000}
        ],
        "suggestions": [
            {"type": "action", "content": "建议内容", "priority": 1}
        ],
        "matched_rules": [
            {"rule_name": "规则名称", "confidence": 0.9}
        ]
    }
    """
    content = response["choices"][0]["message"]["content"]
    
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # 尝试提取 JSON
        data = _extract_json(content)
    
    return OmniOutput(
        caption=data.get("caption", ""),
        speeches=_parse_speeches(data.get("speeches", [])),
        suggestions=_parse_suggestions(data.get("suggestions", [])),
        matched_rules=_parse_matched_rules(data.get("matched_rules", [])),
        skipped=False,
        raw_response=data,
    )
```

### 5.8 Omni 输出

```python
@dataclass
class OmniOutput:
    """Omni 模型输出"""
    caption: str                         # 场景描述
    speeches: list[Speech]               # 语音内容
    suggestions: list[Suggestion]        # 建议
    matched_rules: list[MatchedRule]     # 触发的规则
    skipped: bool                        # 是否跳过（无变化）
    raw_response: dict                   # 原始响应

@dataclass
class Speech:
    """语音内容"""
    content: str           # 语音文本
    start_ms: int          # 开始时间
    end_ms: int            # 结束时间
    speaker: str | None    # 说话人

@dataclass
class Suggestion:
    """建议"""
    type: str              # 类型（action/remind/alert）
    content: str           # 建议内容
    priority: int          # 优先级
    device_id: str | None  # 关联设备

@dataclass
class MatchedRule:
    """触发的规则"""
    rule_name: str         # 规则名称
    rule_id: str           # 规则ID
    confidence: float      # 置信度
```

---

## 6. 结果输出层

**核心文件**: 
- `perception/processor.py`
- `perception/runner.py`
- `perception/service.py`

### 6.1 PipelineProcessor

```python
class PipelineProcessor:
    """
    数据流管线处理器
    
    职责：
    - 采集就绪的窗口
    - 运行完整管线（Gate → Identity → Omni）
    - 存储结果到数据库
    - 触发后处理钩子（事件、通知等）
    - 发布 SSE 事件
    """
    
    def __init__(
        self,
        collector: MultimodalCollector,
        perception_engine_proxy: PerceptionEngineProxy,
        log_repo: PerceptionLogRepo,
    ):
        self._collector = collector
        self._perception_engine_proxy = perception_engine_proxy
        self._log_repo = log_repo
        self._sse_subscribers: list[asyncio.Queue] = []
```

### 6.2 实时感知流程

```python
async def process_realtime(self) -> RealtimePerceptionResult | None:
    """
    实时感知管线
    
    返回：
    - RealtimePerceptionResult: 成功处理的结果
    - False: 数据已消费但推理跳过
    - None: 无数据可处理
    """
    # 1. 采集批次
    batch = self._collector.collect_batch(drain=True)
    if batch.empty:
        return None
    
    # 2. 运行管线
    result = await self._perception_engine_proxy.realtime_perceive(batch)
    
    # 3. 存储结果
    await self._store_result(result)
    
    # 4. 触发后处理
    await self._postprocess(result)
    
    # 5. 发布 SSE 事件
    self._publish("perception", result.to_dict())
    
    return result
```

### 6.3 后处理钩子

```python
async def _postprocess(self, result: RealtimePerceptionResult):
    """
    后处理钩子
    
    包括：
    - 事件分类和存储
    - 规则匹配和执行
    - 通知发送
    - 快照保存
    """
    # 事件分类
    events = self._classify_events(result)
    
    # 规则匹配
    matched_rules = self._match_rules(result)
    
    # 执行动作
    for rule in matched_rules:
        await self._execute_rule(rule)
    
    # 保存快照
    if result.has_meaningful_event:
        await self._save_snapshot(result)
    
    # 发送通知
    if result.should_notify:
        await self._send_notification(result)
```

### 6.4 PerceptionRunner

```python
class PerceptionRunner:
    """
    后台运行循环
    
    职责：
    - 调度感知周期
    - 管理设备同步
    - 处理生命周期
    """
    
    async def start(self):
        """启动感知引擎"""
        # 启动推理线程池
        executor = ThreadPoolExecutor(max_workers=2)
        self._processor.set_inference_executor(executor)
        
        # 启动感知循环
        self._perception_task = asyncio.create_task(self._perception_loop())
        
        # 启动设备同步循环
        self._sync_task = asyncio.create_task(self._sync_devices_loop())
    
    async def _perception_loop(self):
        """
        感知主循环
        
        触发条件：
        1. 窗口就绪事件（早期触发）
        2. 采集间隔超时（兜底触发）
        """
        while self._is_running:
            # 等待触发
            await self._wait_for_trigger()
            
            # 运行感知周期
            try:
                result = await self._processor.process_realtime()
            except Exception as e:
                logger.error("Perception cycle failed: %s", e)
                continue
            
            # 记录性能指标
            if result:
                self._record_metrics(result)
    
    async def _wait_for_trigger(self):
        """
        等待触发
        
        使用 asyncio.wait 实现双重触发：
        1. 窗口就绪事件
        2. 采集间隔超时
        """
        try:
            await asyncio.wait_for(
                self._window_ready_event.wait(),
                timeout=self._collect_interval,
            )
        except asyncio.TimeoutError:
            pass  # 超时触发
    
    async def _sync_devices_loop(self):
        """
        设备同步循环
        
        职责：
        - 定期检查设备状态
        - 新设备上线 → 建立连接
        - 设备离线 → 清理资源
        """
        while self._is_running:
            try:
                await self._collector.sync_devices()
            except Exception as e:
                logger.error("Device sync failed: %s", e)
            
            await asyncio.sleep(self._sync_interval)
```

### 6.5 性能监控

```python
# 每个周期记录详细性能指标
timing = {
    "gate_ms": 5.2,           # 门控耗时
    "gate_video_ms": 3.1,     # 视觉门控
    "gate_audio_ms": 2.1,     # 音频门控
    "gate_vad_ms": 1.5,       # VAD
    "identity_ms": 15.3,      # 身份识别
    "omni_ms": 1250.5,        # Omni AI 调用
    "total_ms": 1270.8,       # 总耗时
}

# 门控统计
gate_stats = {
    "video_pass": True,       # 视觉是否通过
    "audio_pass": False,      # 音频是否通过
    "hold_pass": False,       # 滞回是否激活
    "video_score": 0.012,     # 视觉变化分数
    "audio_energy": 0.005,    # 音频能量
}
```

---

## 7. 多摄像头并行处理

**核心文件**: `perception/engine/pipeline.py`

### 7.1 批量管线

```python
async def run_batch_pipeline(
    batch: BatchedSnapshot,
    contexts: dict[str, OmniContext],
    config: PerceptionConfig,
    get_tracking_service: Callable | None = None,
    get_identity_engine: Callable | None = None,
) -> BatchPipelineResult:
    """
    多摄像头并行处理
    
    设计：
    - 按房间分组
    - 同房间内各摄像头并发处理
    - 不同房间也并发处理
    - 结果合并
    
    并发安全保证：
    - 每个摄像头独立的 IdentityEngine
    - 每个摄像头独立的 SortTracker
    - DeviceContext 通过 ContextVar 隔离
    """
    # 按房间分组
    by_room = batch.by_room()
    
    # 并发处理所有房间
    room_results = await asyncio.gather(
        *(_run_room(room_name, snapshots) 
          for room_name, snapshots in by_room.items()),
        return_exceptions=True,
    )
    
    return BatchPipelineResult(rooms=room_results)
```

### 7.2 单房间处理

```python
async def _run_room(
    room_name: str,
    snapshots: list[DeviceSnapshot],
) -> RoomPipelineResult:
    """
    单房间内的多摄像头并发处理
    
    流程：
    1. 为每个摄像头创建独立的处理协程
    2. 使用 asyncio.gather 并发执行
    3. 合并结果
    """
    device_results = await asyncio.gather(
        *(_run_device(snapshot, room_name) for snapshot in snapshots),
        return_exceptions=True,
    )
    
    return RoomPipelineResult(
        room_name=room_name,
        device_results=device_results,
    )
```

### 7.3 单设备处理

```python
async def _run_device(
    snapshot: DeviceSnapshot,
    room_name: str,
) -> DevicePipelineResult:
    """
    单设备完整管线
    
    关键点：
    - 每个设备有独立的 IdentityEngine
    - DeviceContext 通过 ContextVar 隔离
    - 失败不连累其他设备
    """
    did = snapshot.device.did
    
    # 1. 门控
    gate_packet, gate_timing = run_gate(snapshot, config.gate)
    if gate_packet is None:
        return DevicePipelineResult(skipped=True)
    
    # 2. 身份识别
    identity_engine = get_identity_engine(did, room_name)
    identity_packet = await run_identity(
        gate_packet, config.identity, identity_engine
    )
    
    # 3. Omni AI
    device_ctx = set_device_context(DeviceContext(
        device_id=did,
        room_name=room_name,
    ))
    try:
        omni_output = await run_omni(identity_packet, context, config.omni)
    finally:
        reset_device_context(device_ctx)
    
    return DevicePipelineResult(
        device_id=did,
        gate_packet=gate_packet,
        identity_packet=identity_packet,
        omni_output=omni_output,
    )
```

---

## 8. 关键设计决策

### 8.1 门控优先

**原因**: Omni AI 调用成本高（~1-2秒/次），需要避免无效调用

**实现**: 
- 视觉门控：帧差分法，O(n) 复杂度
- 音频门控：RMS 能量计算，O(1) 复杂度
- 只有检测到变化时才进入后续分析

### 8.2 多层级身份库

**原因**: 
- tier_a: 用户主动录入，质量最高
- tier_c: 系统自动采集，覆盖面广
- tier_u: 临时池，支持跨窗口追踪

**实现**:
- tier_a: 持久化存储，用户可编辑
- tier_c: 在线累积，自动清理
- tier_u: 会话级，内存存储

### 8.3 Fused 模式

**原因**: 减少 omni 调用次数，提高识别准确率

**实现**:
- 将 gallery 参考图和场景视频一起发送
- 模型同时输出场景描述和身份分配
- 减少一次独立的识别调用

### 8.4 流式处理

**原因**: 减少端到端延迟

**实现**:
- 边接收边解析
- 语音和建议早出
- 使用回调通知上层

### 8.5 并发安全

**原因**: 多摄像头并行处理，避免状态污染

**实现**:
- 每个摄像头独立的 IdentityEngine
- DeviceContext 通过 ContextVar 隔离
- 工厂函数按 did 创建独立实例

### 8.6 容错设计

**原因**: 单设备失败不应影响其他设备

**实现**:
- per-device try/except
- 失败设备返回 skipped
- 健康设备正常处理

---

## 9. 核心文件索引

### 数据采集层
- `perception/collect/collector.py` — 多模态数据采集器
- `perception/collect/camera_adapter.py` — 摄像头设备适配器
- `perception/collect/stream_buffer.py` — 多轨时间窗口聚合缓冲区
- `perception/collect/adapter_base.py` — 设备适配器基类

### 门控层
- `perception/engine/gate/gate.py` — 门控主函数
- `perception/engine/gate/visual_gate.py` — 视觉门控
- `perception/engine/gate/audio_gate.py` — 音频门控
- `perception/engine/gate/speech_vad.py` — 语音活动检测

### 身份识别层
- `perception/engine/identity/identity.py` — 身份识别入口
- `perception/engine/identity/engine.py` — IdentityEngine 核心
- `perception/engine/identity/tracking_service.py` — 跟踪服务
- `perception/engine/identity/library.py` — 身份库管理
- `perception/engine/identity/dispatcher.py` — 异步识别调度
- `perception/engine/identity/gallery_composite.py` — Gallery 复合图
- `perception/engine/identity/tracker/` — DeepSORT 跟踪器

### 多模态AI层
- `perception/engine/omni/omni.py` — Omni 主函数
- `perception/engine/omni/prompt_builder.py` — Prompt 构建
- `perception/engine/omni/omni_client.py` — 模型调用客户端
- `perception/engine/omni/response_parser.py` — 响应解析
- `perception/engine/omni/constants.py` — 常量定义

### 结果输出层
- `perception/processor.py` — PipelineProcessor
- `perception/runner.py` — PerceptionRunner
- `perception/service.py` — 感知服务
- `perception/schema.py` — 数据结构定义
- `perception/types.py` — 类型定义

### 配置
- `perception/engine/config.py` — 感知引擎配置
- `perception/engine/types.py` — 引擎类型定义

