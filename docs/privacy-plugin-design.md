# Miloco 隐私保护插件设计文档

## 目录

- [1. 概述](#1-概述)
- [2. 设计目标](#2-设计目标)
- [3. 架构设计](#3-架构设计)
- [4. 拦截点分析](#4-拦截点分析)
- [5. 插件结构](#5-插件结构)
- [6. 核心模块设计](#6-核心模块设计)
- [7. 安装与使用](#7-安装与使用)
- [8. 配置说明](#8-配置说明)
- [9. 性能考量](#9-性能考量)
- [10. 已知限制](#10-已知限制)
- [11. 后续扩展](#11-后续扩展)

---

## 1. 概述

### 1.1 背景

Miloco 项目当前的感知引擎会将视频片段上传到云端大模型（MiMo）进行分析，存在隐私风险：

- 每 4 秒上传一次视频片段到云端
- 包含家庭内部画面、声音
- 可能包含敏感场景（卧室、浴室等）

### 1.2 解决方案

本插件采用**本地预处理 + 云端推理**的混合架构：

- 在视频发送给云端之前，进行本地隐私化处理
- 处理方式：人物骨架化 + 黑边/轮廓处理
- 大模型只能看到抽象化的骨架图，看不到真实面貌和衣着

### 1.3 设计原则

| 原则 | 说明 |
|------|------|
| **非侵入** | 不修改原项目代码，通过文件覆盖实现 |
| **可逆** | 卸载插件后项目恢复原状 |
| **可配置** | 支持多种隐私处理模式 |
| **高性能** | 处理延迟 < 30ms/帧 |
| **易安装** | 一键安装脚本 |

---

## 2. 设计目标

### 2.1 核心目标

```
┌─────────────────────────────────────────────────────────────────┐
│                    设计目标                                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ✅ 隐私保护：云端看不到原始视频                                  │
│  ✅ 功能保留：动作识别、姿态分析正常工作                          │
│  ✅ 非侵入：不修改原项目代码                                      │
│  ✅ 易使用：一键安装、简单配置                                    │
│                                                                 │
│  ⚠️ 功能损失（可接受）：                                         │
│     - 身份识别不可用（无面部信息）                                │
│     - 衣着识别不可用（无外观信息）                                │
│     - 文字识别不可用（模糊处理）                                  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 处理效果

```
原始图像：                         处理后：
┌─────────────────┐               ┌─────────────────┐
│                 │               │                 │
│    ┌─────┐      │               │      ●          │
│    │ 😊  │      │               │     /|\         │
│    └──┬──┘      │      →        │      |          │
│      /\        │               │     / \         │
│     /  \       │               │                 │
│                 │               │  [黑边轮廓]     │
│  清晰人脸+衣着  │               │  纯骨架线条     │
└─────────────────┘               └─────────────────┘

云端看到的：
- ✅ 有人、站姿、动作
- ❌ 不知道长什么样、穿什么
```

---

## 3. 架构设计

### 3.1 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    插件架构                                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ 原始 Miloco 项目                                          │  │
│  │                                                          │  │
│  │  摄像头 → camera_handler → camera_adapter → pipeline    │  │
│  │                                       │                  │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                       │                         │
│                                       ▼                         │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ 插件拦截层                                                │  │
│  │                                                          │  │
│  │  prompt_builder.py (覆盖)                                │  │
│  │       │                                                  │  │
│  │       ▼                                                  │  │
│  │  ┌────────────────────────────────────────────────────┐  │  │
│  │  │ privacy_filter.py (新增)                           │  │  │
│  │  │  - 人体检测                                        │  │  │
│  │  │  - 姿势估计                                        │  │  │
│  │  │  - 黑边/轮廓处理                                   │  │  │
│  │  │  - 骨架绘制                                        │  │  │
│  │  └────────────────────────────────────────────────────┘  │  │
│  │                                                          │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                       │                         │
│                                       ▼                         │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ 云端大模型                                                │  │
│  │ （只看到骨架图）                                          │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 数据流

```
┌─────────────────────────────────────────────────────────────────┐
│                    数据流对比                                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  原始流程：                                                      │
│  ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐     │
│  │ 摄像头  │ →  │ 感知    │ →  │ 编码    │ →  │ 云端    │     │
│  │ 原始帧  │    │ 引擎    │    │ mp4     │    │ 大模型  │     │
│  └─────────┘    └─────────┘    └─────────┘    └─────────┘     │
│                                                                 │
│  插件流程：                                                      │
│  ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐     │
│  │ 摄像头  │ →  │ 感知    │ →  │ 隐私    │ →  │ 编码    │ → 云端│
│  │ 原始帧  │    │ 引擎    │    │ 处理    │    │ mp4     │    │
│  └─────────┘    └─────────┘    └─────────┘    └─────────┘     │
│                                 ↑                               │
│                                 │                               │
│                          插件注入点                              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. 拦截点分析

### 4.1 候选拦截点

| 位置 | 文件 | 函数 | 优点 | 缺点 |
|------|------|------|------|------|
| ① SDK 层 | `camera_handler.py` | `register_decode_video_frame_stream` | 最早拦截 | 影响所有下游 |
| ② 采集层 | `camera_adapter.py` | `_make_decoded_video_callback` | 影响感知引擎 | 影响本地预览 |
| ③ WebSocket | `ws.py` | `__video_stream_callback` | 影响实时预览 | 不影响感知 |
| ④ **Prompt 层** | `prompt_builder.py` | `_encode_video_mp4` | **只影响云端** | 最精准 |

### 4.2 推荐拦截点：④ prompt_builder.py

```
选择理由：
├── 只影响发送给云端的视频，不影响本地实时预览
├── 函数签名简单，输入是 numpy 数组，方便处理
├── 是云端推理前的最后一步，改动范围最小
└── 其他所有功能（本地预览、Gate、Identity）不受影响

拦截位置：
miloco/perception/engine/omni/prompt_builder.py
└── _encode_video_mp4(frames, audio_clip, sample_rate, fps)
    └── 在编码前插入处理逻辑
```

### 4.3 拦截点代码位置

```python
# 文件：miloco/perception/engine/omni/prompt_builder.py
# 函数：_encode_video_mp4
# 行号：约 1144-1230

def _encode_video_mp4(
    frames: list[NDArray[np.uint8]],      # ← 拦截点：处理这些帧
    audio_clip: NDArray[np.int16],
    sample_rate: int,
    fps: int,
) -> str | None:
    """Encode BGR frames + PCM audio into mp4 using PyAV."""
    
    # ===== 插件注入位置 =====
    # 在这里对 frames 进行隐私处理
    # frames = [privacy_filter(frame) for frame in frames]
    # ========================
    
    # 原始编码逻辑继续...
    container = av.open(tmp_path, "w")
    # ...
```

---

## 5. 插件结构

### 5.1 目录结构

```
miloco-privacy-plugin/
│
├── README.md                           # 使用说明
├── LICENSE                             # 许可证
├── install.sh                          # 安装脚本
├── uninstall.sh                        # 卸载脚本
├── config.example.yaml                 # 配置示例
│
├── patches/                            # 补丁文件目录
│   ├── __init__.py                     # 包初始化
│   │
│   ├── prompt_builder.py               # 覆盖原文件
│   │   └── _encode_video_mp4()         # 修改此函数
│   │
│   └── privacy/                        # 隐私处理模块
│       ├── __init__.py                 # 模块入口
│       ├── filter.py                   # 核心过滤逻辑
│       ├── pose_estimator.py           # 姿势估计
│       ├── skeleton_renderer.py        # 骨架绘制
│       └── utils.py                    # 工具函数
│
└── models/                             # 预训练模型（可选）
    ├── yolov8n.onnx                    # 人体检测模型
    └── rtmpose-m.onnx                  # 姿势估计模型
```

### 5.2 文件说明

| 文件 | 作用 | 是否必须 |
|------|------|---------|
| `prompt_builder.py` | 覆盖原文件，注入处理逻辑 | ✅ 必须 |
| `privacy/filter.py` | 核心隐私处理逻辑 | ✅ 必须 |
| `privacy/pose_estimator.py` | 姿势估计封装 | ✅ 必须 |
| `privacy/skeleton_renderer.py` | 骨架绘制 | ✅ 必须 |
| `models/*.onnx` | 预训练模型 | 可选（可在线下载） |

---

## 6. 核心模块设计

### 6.1 隐私过滤器 (privacy/filter.py)

```python
"""
隐私过滤器 - 核心模块

功能：
1. 人体检测
2. 姿势估计
3. 人物区域处理（黑边/轮廓）
4. 骨架绘制

输入：原始 BGR 帧 (numpy array)
输出：处理后的 BGR 帧 (numpy array)
"""

class PrivacyFilter:
    """隐私过滤器"""
    
    def __init__(self, config: dict):
        """
        初始化过滤器
        
        配置项：
        - mode: 处理模式
          - "skeleton_black": 骨架 + 黑背景（最严格）
          - "skeleton_outline": 骨架 + 轮廓（平衡）
          - "skeleton_blur": 骨架 + 模糊（最宽松）
        - pose_model: 姿势估计模型路径
        - detection_model: 人体检测模型路径
        - skeleton_color: 骨架颜色
        - skeleton_thickness: 骨架线条粗细
        - background_color: 背景颜色（黑边模式）
        """
        self.mode = config.get("mode", "skeleton_black")
        self.pose_estimator = PoseEstimator(config)
        self.skeleton_renderer = SkeletonRenderer(config)
    
    def process_frame(self, frame: np.ndarray) -> np.ndarray:
        """
        处理单帧
        
        流程：
        1. 人体检测 → 获取人体边界框
        2. 姿势估计 → 获取骨骼关键点
        3. 人物区域处理 → 黑边/轮廓/模糊
        4. 骨架绘制 → 在处理后的图像上绘制骨架
        
        输入：原始 BGR 帧
        输出：处理后的 BGR 帧
        """
        # Step 1: 人体检测
        persons = self.detect_persons(frame)
        
        # Step 2: 姿势估计
        poses = self.estimate_poses(frame, persons)
        
        # Step 3: 人物区域处理
        processed = self.process_person_regions(frame, persons)
        
        # Step 4: 骨架绘制
        result = self.draw_skeletons(processed, poses)
        
        return result
    
    def process_frames(self, frames: list[np.ndarray]) -> list[np.ndarray]:
        """批量处理多帧"""
        return [self.process_frame(frame) for frame in frames]
```

### 6.2 姿势估计器 (privacy/pose_estimator.py)

```python
"""
姿势估计器

支持的后端：
- MediaPipe Pose（轻量，适合 CPU）
- RTMPose（高精度，需要 GPU）
- OpenPose（经典，较慢）
"""

class PoseEstimator:
    """姿势估计器"""
    
    # COCO 17 关键点定义
    KEYPOINTS = [
        "nose",           # 0: 鼻子
        "left_eye",       # 1: 左眼
        "right_eye",      # 2: 右眼
        "left_ear",       # 3: 左耳
        "right_ear",      # 4: 右耳
        "left_shoulder",  # 5: 左肩
        "right_shoulder", # 6: 右肩
        "left_elbow",     # 7: 左肘
        "right_elbow",    # 8: 右肘
        "left_wrist",     # 9: 左腕
        "right_wrist",    # 10: 右腕
        "left_hip",       # 11: 左髋
        "right_hip",      # 12: 右髋
        "left_knee",      # 13: 左膝
        "right_knee",     # 14: 右膝
        "left_ankle",     # 15: 左踝
        "right_ankle",    # 16: 右踝
    ]
    
    # 骨架连接定义
    SKELETON = [
        (0, 1), (0, 2),          # 鼻子 → 眼睛
        (1, 3), (2, 4),          # 眼睛 → 耳朵
        (5, 6),                  # 肩膀连线
        (5, 7), (7, 9),          # 左臂
        (6, 8), (8, 10),         # 右臂
        (5, 11), (6, 12),        # 躯干
        (11, 12),                # 髋部连线
        (11, 13), (13, 15),      # 左腿
        (12, 14), (14, 16),      # 右腿
    ]
    
    def __init__(self, config: dict):
        """
        初始化估计器
        
        配置项：
        - backend: "mediapipe" | "rtmpose" | "openpose"
        - model_path: 模型文件路径
        - confidence_threshold: 置信度阈值
        - device: "cpu" | "cuda" | "mps"
        """
        self.backend = config.get("backend", "mediapipe")
        self.confidence_threshold = config.get("confidence_threshold", 0.5)
        self._load_model(config)
    
    def estimate(self, frame: np.ndarray) -> list[Pose]:
        """
        估计单帧的姿势
        
        返回：Pose 列表，每个 Pose 包含 17 个关键点
        """
        poses = self._detect(frame)
        return [p for p in poses if p.confidence > self.confidence_threshold]
```

### 6.3 骨架渲染器 (privacy/skeleton_renderer.py)

```python
"""
骨架渲染器

功能：
- 在图像上绘制骨架
- 支持多种样式（线条、圆形、颜色）
- 支持多人不同颜色
"""

class SkeletonRenderer:
    """骨架渲染器"""
    
    # 默认颜色板（多人区分）
    COLORS = [
        (255, 0, 0),    # 红
        (0, 255, 0),    # 绿
        (0, 0, 255),    # 蓝
        (255, 255, 0),  # 黄
        (255, 0, 255),  # 洋红
        (0, 255, 255),  # 青
    ]
    
    def __init__(self, config: dict):
        """
        初始化渲染器
        
        配置项：
        - line_thickness: 线条粗细（默认 2）
        - joint_radius: 关键点半径（默认 3）
        - draw_joints: 是否绘制关键点（默认 True）
        - draw_limbs: 是否绘制肢体（默认 True）
        - color_mode: "per_person" | "single"
        - single_color: 单色模式的颜色
        """
        self.line_thickness = config.get("line_thickness", 2)
        self.joint_radius = config.get("joint_radius", 3)
        self.draw_joints = config.get("draw_joints", True)
        self.draw_limbs = config.get("draw_limbs", True)
    
    def draw(
        self,
        image: np.ndarray,
        poses: list[Pose],
        color: tuple[int, int, int] | None = None,
    ) -> np.ndarray:
        """
        在图像上绘制骨架
        
        输入：
        - image: 背景图像
        - poses: 姿势列表
        - color: 指定颜色（None 则自动分配）
        
        输出：绘制了骨架的图像
        """
        result = image.copy()
        
        for i, pose in enumerate(poses):
            c = color or self.COLORS[i % len(self.COLORS)]
            
            # 绘制肢体
            if self.draw_limbs:
                for (p1, p2) in PoseEstimator.SKELETON:
                    if pose.keypoints[p1].confidence > 0 and \
                       pose.keypoints[p2].confidence > 0:
                        cv2.line(
                            result,
                            pose.keypoints[p1].xy,
                            pose.keypoints[p2].xy,
                            c,
                            self.line_thickness,
                        )
            
            # 绘制关键点
            if self.draw_joints:
                for kp in pose.keypoints:
                    if kp.confidence > 0:
                        cv2.circle(
                            result,
                            kp.xy,
                            self.joint_radius,
                            c,
                            -1,
                        )
        
        return result
```

### 6.4 人物区域处理

```python
"""
人物区域处理模式

模式：
1. skeleton_black: 人物区域全黑 + 骨架
2. skeleton_outline: 人物轮廓化 + 骨架
3. skeleton_blur: 人物区域模糊 + 骨架
"""

def process_person_region_black(
    frame: np.ndarray,
    bboxes: list[BBox],
) -> np.ndarray:
    """
    模式 1：人物区域全黑
    
    处理：
    - 人物区域填充黑色
    - 背景保留
    - 后续叠加骨架
    """
    result = np.zeros_like(frame)
    
    # 保留背景（非人物区域）
    mask = np.ones(frame.shape[:2], dtype=bool)
    for bbox in bboxes:
        x1, y1, x2, y2 = bbox
        mask[y1:y2, x1:x2] = False
    
    result[mask] = frame[mask]
    
    return result


def process_person_region_outline(
    frame: np.ndarray,
    bboxes: list[BBox],
) -> np.ndarray:
    """
    模式 2：人物轮廓化
    
    处理：
    - 人物区域边缘检测
    - 内部填充纯色
    - 后续叠加骨架
    """
    result = frame.copy()
    
    for bbox in bboxes:
        x1, y1, x2, y2 = bbox
        roi = frame[y1:y2, x1:x2]
        
        # 边缘检测
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        
        # 填充纯色
        result[y1:y2, x1:x2] = 0
        result[y1:y2, x1:x2][edges > 0] = 255
    
    return result


def process_person_region_blur(
    frame: np.ndarray,
    bboxes: list[BBox],
    blur_strength: int = 51,
) -> np.ndarray:
    """
    模式 3：人物区域模糊
    
    处理：
    - 人物区域高斯模糊
    - 背景保留
    - 后续叠加骨架
    """
    result = frame.copy()
    
    for bbox in bboxes:
        x1, y1, x2, y2 = bbox
        roi = frame[y1:y2, x1:x2]
        
        # 高斯模糊
        blurred = cv2.GaussianBlur(roi, (blur_strength, blur_strength), 0)
        result[y1:y2, x1:x2] = blurred
    
    return result
```

---

## 7. 安装与使用

### 7.1 安装步骤

```bash
# 1. 克隆原项目
git clone https://github.com/XiaoMi/xiaomi-miloco.git
cd xiaomi-miloco

# 2. 克隆插件
git clone https://github.com/your-org/miloco-privacy-plugin.git

# 3. 运行安装脚本
cd miloco-privacy-plugin
./install.sh ../xiaomi-miloco

# 安装脚本会：
# - 复制 patches/prompt_builder.py 覆盖原文件
# - 复制 patches/privacy/ 到正确位置
# - 下载必要的模型文件（可选）
# - 生成配置文件
```

### 7.2 安装脚本 (install.sh)

```bash
#!/bin/bash
# Miloco 隐私保护插件安装脚本

set -e

MILOCO_DIR="${1:-.}"
PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=========================================="
echo "Miloco 隐私保护插件安装"
echo "=========================================="
echo ""
echo "Miloco 目录: $MILOCO_DIR"
echo "插件目录: $PLUGIN_DIR"
echo ""

# 检查 Miloco 目录
if [ ! -f "$MILOCO_DIR/miloco/perception/engine/omni/prompt_builder.py" ]; then
    echo "错误：未找到 Miloco 项目"
    echo "请指定正确的 Miloco 目录：./install.sh /path/to/miloco"
    exit 1
fi

# 备份原文件
echo "备份原文件..."
BACKUP_DIR="$PLUGIN_DIR/backups/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"
cp "$MILOCO_DIR/miloco/perception/engine/omni/prompt_builder.py" "$BACKUP_DIR/"
echo "备份完成：$BACKUP_DIR"

# 复制补丁文件
echo "安装补丁文件..."
cp "$PLUGIN_DIR/patches/prompt_builder.py" \
   "$MILOCO_DIR/miloco/perception/engine/omni/"

# 复制隐私处理模块
echo "安装隐私处理模块..."
mkdir -p "$MILOCO_DIR/miloco/perception/engine/privacy"
cp -r "$PLUGIN_DIR/patches/privacy/"* \
      "$MILOCO_DIR/miloco/perception/engine/privacy/"

# 生成配置文件
if [ ! -f "$MILOCO_DIR/privacy_config.yaml" ]; then
    echo "生成配置文件..."
    cp "$PLUGIN_DIR/config.example.yaml" "$MILOCO_DIR/privacy_config.yaml"
fi

# 下载模型（可选）
read -p "是否下载姿势估计模型？(y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "下载模型文件..."
    # TODO: 添加模型下载逻辑
fi

echo ""
echo "=========================================="
echo "安装完成！"
echo "=========================================="
echo ""
echo "配置文件：$MILOCO_DIR/privacy_config.yaml"
echo ""
echo "启动 Miloco："
echo "  cd $MILOCO_DIR"
echo "  python -m miloco.main"
echo ""
```

### 7.3 卸载脚本 (uninstall.sh)

```bash
#!/bin/bash
# Miloco 隐私保护插件卸载脚本

set -e

MILOCO_DIR="${1:-.}"
PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=========================================="
echo "Miloco 隐私保护插件卸载"
echo "=========================================="

# 查找最新备份
LATEST_BACKUP=$(ls -td "$PLUGIN_DIR/backups"/*/ 2>/dev/null | head -1)

if [ -z "$LATEST_BACKUP" ]; then
    echo "错误：未找到备份文件"
    exit 1
fi

echo "恢复备份：$LATEST_BACKUP"

# 恢复原文件
cp "$LATEST_BACKUP/prompt_builder.py" \
   "$MILOCO_DIR/miloco/perception/engine/omni/"

# 删除隐私处理模块
rm -rf "$MILOCO_DIR/miloco/perception/engine/privacy"

echo ""
echo "=========================================="
echo "卸载完成！"
echo "=========================================="
```

### 7.4 使用方式

```bash
# 安装插件后，正常使用 Miloco 即可
# 插件会自动生效，无需额外配置

cd xiaomi-miloco
python -m miloco.main
```

---

## 8. 配置说明

### 8.1 配置文件 (privacy_config.yaml)

```yaml
# Miloco 隐私保护插件配置

# 是否启用插件
enabled: true

# 隐私处理模式
# - skeleton_black: 骨架 + 黑背景（最严格）
# - skeleton_outline: 骨架 + 轮廓（平衡）
# - skeleton_blur: 骨架 + 模糊（最宽松）
mode: skeleton_black

# 姿势估计配置
pose:
  # 后端选择
  # - mediapipe: 轻量，适合 CPU
  # - rtmpose: 高精度，需要 GPU
  # - openpose: 经典，较慢
  backend: mediapipe
  
  # 模型文件路径（可选，有默认值）
  model_path: null
  
  # 置信度阈值
  confidence_threshold: 0.5
  
  # 推理设备
  # - cpu: 使用 CPU
  # - cuda: 使用 NVIDIA GPU
  # - mps: 使用 Apple Silicon GPU
  device: cpu

# 骨架渲染配置
skeleton:
  # 线条粗细（像素）
  line_thickness: 2
  
  # 关键点半径（像素）
  joint_radius: 3
  
  # 是否绘制关键点
  draw_joints: true
  
  # 是否绘制肢体
  draw_limbs: true
  
  # 颜色模式
  # - per_person: 每人不同颜色
  # - single: 统一颜色
  color_mode: per_person
  
  # 单色模式的颜色（BGR）
  single_color: [0, 255, 0]

# 模糊模式配置（仅 mode=skeleton_blur 时生效）
blur:
  # 模糊强度（奇数）
  strength: 51

# 轮廓模式配置（仅 mode=skeleton_outline 时生效）
outline:
  # 边缘检测阈值
  canny_low: 50
  canny_high: 150

# 性能配置
performance:
  # 是否启用 GPU 加速
  use_gpu: false
  
  # 批处理大小
  batch_size: 1
  
  # 是否跳帧处理（每 N 帧处理一次）
  skip_frames: 0

# 调试配置
debug:
  # 是否保存处理后的图像
  save_images: false
  
  # 保存目录
  save_dir: ./debug_images
  
  # 是否显示性能统计
  show_stats: false
```

### 8.2 环境变量

```bash
# 可以通过环境变量覆盖配置

# 启用/禁用插件
export PRIVACY_ENABLED=true

# 处理模式
export PRIVACY_MODE=skeleton_black

# 姿势估计后端
export PRIVACY_POSE_BACKEND=mediapipe

# 推理设备
export PRIVACY_DEVICE=cpu

# 调试模式
export PRIVACY_DEBUG=false
```

---

## 9. 性能考量

### 9.1 性能基准

| 模型 | 设备 | 单帧延迟 | 帧率 |
|------|------|---------|------|
| MediaPipe Pose | CPU (M1) | ~15ms | ~65 fps |
| MediaPipe Pose | CPU (Intel i7) | ~25ms | ~40 fps |
| RTMPose (small) | GPU (RTX 3060) | ~5ms | ~200 fps |
| RTMPose (medium) | GPU (RTX 3060) | ~8ms | ~125 fps |
| OpenPose (COCO) | CPU | ~100ms | ~10 fps |
| OpenPose (COCO) | GPU | ~30ms | ~33 fps |

### 9.2 推荐配置

```
场景：实时监控（30 fps 要求）
├── CPU 设备：MediaPipe Pose
├── GPU 设备：RTMPose (small)
└── 边缘设备：MediaPipe Pose (lite)

场景：离线分析（精度优先）
├── GPU 设备：RTMPose (medium)
└── CPU 设备：OpenPose (轻量版)
```

### 9.3 性能优化建议

```
1. 跳帧处理
   - 如果不需要每帧都处理，可以设置 skip_frames
   - 例如 skip_frames=2 表示每 3 帧处理一次

2. 分辨率缩放
   - 在处理前将图像缩放到较小尺寸
   - 例如 640x480 → 320x240

3. GPU 加速
   - 如果有 NVIDIA GPU，使用 CUDA
   - 如果是 Apple Silicon，使用 MPS

4. 批处理
   - 如果有多帧需要处理，使用批处理
   - 可以利用 GPU 并行性
```

---

## 10. 已知限制

### 10.1 功能限制

| 功能 | 原状态 | 插件后状态 | 原因 |
|------|--------|-----------|------|
| 动作识别 | ✅ | ✅ | 骨架足够 |
| 跌倒检测 | ✅ | ✅ | 骨架足够 |
| 身份识别 | ✅ | ❌ | 无面部信息 |
| 人脸表情 | ✅ | ❌ | 无面部信息 |
| 衣着识别 | ✅ | ❌ | 无外观信息 |
| 文字识别 | ✅ | ❌ | 模糊处理 |
| 手势识别 | ✅ | ⚠️ | 部分可用 |

### 10.2 技术限制

```
1. 遮挡问题
   - 当人物相互遮挡时，骨架可能不准确
   - 解决：使用多视角或深度摄像头

2. 边缘情况
   - 人物部分出画时，骨架不完整
   - 解决：增加边界检测逻辑

3. 复杂姿态
   - 非常规姿态（如瑜伽）可能识别不准
   - 解决：使用更高精度的模型

4. 光照影响
   - 极端光照下检测效果下降
   - 解决：增加图像预处理
```

### 10.3 兼容性限制

```
1. 项目更新
   - Miloco 更新后可能需要重新安装插件
   - 解决：安装脚本自动备份和恢复

2. Python 版本
   - 需要 Python 3.8+
   - 某些模型需要特定 Python 版本

3. 依赖冲突
   - 插件依赖可能与原项目冲突
   - 解决：使用虚拟环境
```

---

## 11. 后续扩展

### 11.1 计划功能

```
1. 更多处理模式
   - 卡通化：将人物转为卡通风格
   - 素描化：将人物转为素描风格
   - 热力图：只显示人物热力图

2. 选择性处理
   - 按区域配置：不同房间不同处理方式
   - 按时间配置：不同时间段不同处理方式
   - 按人物配置：已知人物可选择不处理

3. 身份保留
   - 可选保留面部特征（加密存储）
   - 用于需要身份识别的场景

4. 本地模型
   - 集成本地 VLM
   - 完全离线运行
```

### 11.2 扩展接口

```python
# 插件支持自定义处理函数

class CustomPrivacyFilter(PrivacyFilter):
    """自定义隐私过滤器"""
    
    def process_frame(self, frame: np.ndarray) -> np.ndarray:
        """自定义处理逻辑"""
        # 用户可以在这里实现自己的处理逻辑
        return frame

# 在配置中指定自定义过滤器
# privacy_config.yaml:
# custom_filter: path.to.CustomPrivacyFilter
```

---

## 附录

### A. 关键点定义（COCO 17 点）

```
索引	名称	            位置
0	nose	            鼻子
1	left_eye	        左眼
2	right_eye	        右眼
3	left_ear	        左耳
4	right_ear	        右耳
5	left_shoulder	    左肩
6	right_shoulder	    右肩
7	left_elbow	        左肘
8	right_elbow	        右肘
9	left_wrist	        左腕
10	right_wrist	        右腕
11	left_hip	        左髋
12	right_hip	        右髋
13	left_knee	        左膝
14	right_knee	        右膝
15	left_ankle	        左踝
16	right_ankle	        右踝
```

### B. 骨架连接定义

```
连接	关键点 1	关键点 2	部位
0	    0	        1	        鼻子 → 左眼
1	    0	        2	        鼻子 → 右眼
2	    1	        3	        左眼 → 左耳
3	    2	        4	        右眼 → 右耳
4	    5	        6	        左肩 → 右肩
5	    5	        7	        左肩 → 左肘
6	    7	        9	        左肘 → 左腕
7	    6	        8	        右肩 → 右肘
8	    8	        10	        右肘 → 右腕
9	    5	        11	        左肩 → 左髋
10	    6	        12	        右肩 → 右髋
11	    11	        12	        左髋 → 右髋
12	    11	        13	        左髋 → 左膝
13	    13	        15	        左膝 → 左踝
14	    12	        14	        右髋 → 右膝
15	    14	        16	        右膝 → 右踝
```

### C. 常见问题

**Q: 安装后如何验证插件是否生效？**
A: 启动 Miloco 后，查看日志中是否有 "Privacy filter initialized" 字样。

**Q: 插件会影响本地实时预览吗？**
A: 不会。插件只影响发送给云端的视频，本地预览保持原样。

**Q: 如何临时禁用插件？**
A: 设置环境变量 `PRIVACY_ENABLED=false` 或修改配置文件 `enabled: false`。

**Q: 支持哪些摄像头？**
A: 插件不改变摄像头接入方式，支持原项目支持的所有摄像头。

**Q: 处理延迟会影响实时性吗？**
A: 使用 MediaPipe 时延迟约 15ms，对 30fps 流影响很小。

---

## 版本历史

| 版本 | 日期 | 说明 |
|------|------|------|
| 0.1.0 | - | 初始设计文档 |

---

## 许可证

本插件遵循与 Miloco 项目相同的许可证。
