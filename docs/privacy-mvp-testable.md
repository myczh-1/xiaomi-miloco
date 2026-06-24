# Miloco 隐私保护可落地测试版方案

## 1. 结论

这版不做：

- fork Miloco 长期维护分支
- 外部插件整文件覆盖 `prompt_builder.py`

这版改成：

- **外部插件**
- **运行时注入**
- **只接管 Omni 视频编码入口**
- **MVP 只做一个匿名化模式：`skeleton_mask`**

目标仍然只有一个：

> 把送给 Omni 的视频做本地匿名化后，尽量保留动作和场景语义，同时不长期 fork 主项目。

---

## 2. 为什么改方向

### 2.1 不继续坚持 `box_mask`

`box_mask` 的优点是简单，但它有一个根本问题：

- 它保护隐私有效
- 但很可能把云端还能用的动作/姿态信息一起抹掉

如果你的本地实验已经表明：

- 纯遮挡的隐私收益和骨架化接近
- 但云端理解效果更差

那 MVP 再走 `box_mask` 没意义。

因此第一版直接收敛成：

- **`skeleton_mask`：人体区域黑化 + 骨架重绘**

### 2.2 不做 fork 的理由成立

你的诉求也成立：

- 这个功能不是主项目核心路径
- 只是你自己叠加的隐私能力
- 不值得为它长期维护一条 fork 分支

但这不意味着“整文件覆盖”就是正确外部化方案。

整文件覆盖的问题是：

- 上游一改 `prompt_builder.py`，你就要整体重拷
- 很难判断自己覆盖掉了哪些新逻辑
- 出问题时很难快速定位是上游变化还是你自己的 patch

所以外部化的正确方向不是“覆盖文件”，而是：

- **运行时 patch 单一函数**

---

## 3. 正确的拦截点仍然不变

目标函数仍然是：

- `backend/miloco/src/miloco/perception/engine/omni/prompt_builder.py:1144`

也就是：

- `_encode_video_mp4(...)`

原因没有变化：

- 已经拿到完整 `frames: list[np.ndarray]`
- 只作用于发给 Omni 的视频内容
- 改动面最小

但语义边界要说准确：

- 这里不只影响云端上传视频
- 还会影响 `meaningful_events` 复用的 clip

相关字节旁路逻辑在：

- `backend/miloco/src/miloco/perception/engine/omni/prompt_builder.py:1225`

所以这条路线的真实边界是：

- **影响 Omni 看到的 mp4**
- **影响 `meaningful_events` 复用的 mp4**
- **不影响本地采集、实时预览、tracker、Identity、Gate 的输入**

---

## 4. MVP 形态

### 4.1 外部插件，不改 Miloco 仓库文件

MVP 形态改为：

- 一个独立 Python 包，例如 `miloco_privacy_plugin`

该包不修改 Miloco 仓库源码，不复制覆盖原文件。

它只做两件事：

1. 在 Miloco 启动时被自动 import
2. 对 `prompt_builder._encode_video_mp4` 做运行时 patch

### 4.2 启动方式

外部插件建议通过以下任一方式接入：

- `sitecustomize.py`
- `.pth` 自动导入
- 显式启动包装器

MVP 优先级建议：

1. `sitecustomize.py`
2. `.pth`
3. 启动包装器

原因：

- 不改 Miloco 仓库
- 安装/卸载清晰
- 对用户来说接近“可插拔”

### 4.3 运行时 patch 的范围

只 patch 一个函数：

- `miloco.perception.engine.omni.prompt_builder._encode_video_mp4`

不 patch：

- 采集层
- WebSocket 层
- Tracker 层
- Identity 层

这样做是为了把影响范围压到最小。

---

## 5. MVP 唯一模式：`skeleton_mask`

### 5.1 模式定义

MVP 只保留一个模式：

- `skeleton_mask`

行为定义：

1. 对发给 Omni 的每帧做人体区域匿名化
2. 用纯黑或近纯黑覆盖人体区域
3. 在覆盖结果上重绘人体骨架

输出目标：

- 云端看不到真实脸、衣着、体表细节
- 云端还能看到人数、姿态、动作趋势、相对位置

### 5.2 为什么直接上姿态

不是因为姿态“更高级”，而是因为这里有明确产品判断：

- 你的目标不是单纯遮住人
- 而是 **尽量保留可供云端理解的动作结构**

如果只做 bbox 遮挡：

- 隐私保护确实简单
- 但对动作和交互理解的损伤可能过大

所以 MVP 在这里不再走“最简单”，而走“最接近最终可用价值”。

### 5.3 允许的第一版实现

MVP 仍然要克制。

第一版允许：

- MediaPipe Pose / segmentation 这一条单后端

第一版不做：

- RTMPose
- OpenPose
- 多后端切换
- 在线模型下载
- 自动 GPU 路由

原因：

- 当前目标是验证路线
- 不是做通用匿名化平台

---

## 6. 你当前 demo 暴露出的两个关键问题

### 6.1 性能不高

你当前 demo 的慢，主要来自两个现实因素：

- 按视频逐帧跑姿态/分割
- 输出视频时本地转码串行进行

但这个结论不能直接等价到 Miloco 的 Omni 路径。

Miloco 当前对 Omni 视频有两个天然约束：

- 默认 `omni_fps = 1`
  - `backend/miloco/src/miloco/perception/engine/config.py:15`
- 在 pipeline 内已经先对 Omni 视频做专门下采样
  - `backend/miloco/src/miloco/perception/engine/pipeline.py:126`

因此 MVP 的真实处理规模是：

- 每个 4 秒窗口只处理少量帧
- 不是原始视频逐帧全量处理

所以姿态方案是否“慢到不可接受”，必须在 Miloco 真实链路里量，不应直接由 demo 推断。

### 6.2 有时会漏出人物

这才是更严重的问题。

对隐私功能来说，性能差是体验问题，漏人是功能失败。

MVP 必须把“漏出人物”的容错策略前置进设计，而不是事后优化。

---

## 7. `skeleton_mask` 的最低隐私要求

### 7.1 不允许“失败即回原图”

MVP 明确禁止这种策略：

- 姿态/分割失败时直接返回原始帧

因为这会把隐私功能变成“有时生效，有时裸奔”。

对隐私场景，这不可接受。

### 7.2 失败降级顺序

`skeleton_mask` 必须采用保守降级：

1. **优先：分割黑化 + 骨架重绘**
2. **次优：仅分割黑化**
3. **再退：整个人体大框黑化**
4. **最后兜底：整帧黑化**

但绝不允许：

- 失败 → 原图输出

### 7.3 设计原则

这里要明确优先级：

- **隐私安全 > 语义保留 > 视觉好看**

也就是说：

- 宁可黑多一点
- 也不能漏出脸、身体轮廓、衣着细节

---

## 8. 外部插件的实现方式

### 8.1 包结构建议

建议插件仓库结构：

```text
miloco-privacy-plugin/
├── pyproject.toml
├── README.md
├── sitecustomize.py
└── miloco_privacy_plugin/
    ├── __init__.py
    ├── bootstrap.py
    ├── patch_prompt_builder.py
    ├── privacy_filter.py
    ├── mediapipe_pose.py
    └── config.py
```

### 8.2 patch 逻辑职责

`patch_prompt_builder.py` 只负责：

1. import `miloco.perception.engine.omni.prompt_builder`
2. 校验目标函数是否存在
3. 校验签名是否符合预期
4. 保存原函数引用
5. 用包装函数替换 `_encode_video_mp4`

包装函数职责：

1. 读取插件配置
2. 对 `frames` 做 `skeleton_mask`
3. 失败时走保守降级
4. 再调用原始编码路径

### 8.3 版本校验

因为这是运行时 patch，所以必须加版本/签名校验。

至少要校验：

- 模块路径存在
- 函数名存在
- 参数列表仍是 `(frames, audio_clip, sample_rate, fps)`

如果校验失败：

- 插件不生效
- 打明确日志
- 不阻断 Miloco 启动

### 8.4 维护成本说明

这是兼容机制，必须明确代价。

维护成本：

- 上游函数签名变更时需要跟进
- 上游语义变更时需要复审 patch 是否仍正确

长期影响：

- 比 fork 轻
- 比整文件覆盖稳
- 但不是零维护

这个点在真正开始实现前需要你确认接受。

---

## 9. MVP 配置收敛

第一版只暴露必要配置。

建议插件配置：

```yaml
enabled: true
mode: skeleton_mask
backend: mediapipe
mask_threshold: 0.5
fail_safe: blackout
debug_dump: false
```

MVP 只支持：

- `enabled`
- `mode=skeleton_mask`
- `mask_threshold`
- `fail_safe`
- `debug_dump`

MVP 不支持：

- 多模型自动切换
- 多种渲染风格
- 复杂颜色配置
- 在线下载模型

---

## 10. 验证方式

### 10.1 先做离线验证

先不要直接上 Miloco 线上运行。

先验证：

1. 原始 clip
2. `skeleton_mask` clip
3. 两者给 Omni 的返回差异

建议固定 3 类样本：

1. 单人静止
2. 单人明显动作
3. 双人交互

重点看：

- `caption`
- `speeches`
- `suggestions`
- `matched_rules`

### 10.2 再做 Miloco 链路验证

当离线结果可接受后，再接 Miloco 真实路径，检查：

- Omni 请求是否成功
- 编码是否稳定
- `meaningful_events` clip 是否仍可复用
- 本地预览是否未受影响

### 10.3 性能指标

MVP 不看“单帧纯算法耗时”，看链路增量：

- 每个 4 秒窗口额外耗时
- p50 / p95
- 匿名化前后总 Omni 调用耗时差

第一版目标：

- **额外开销控制在可接受范围**

这里先不写死数字，避免在未实测前给出伪精确目标。

---

## 11. 当前不直接实施的内容

以下内容不是 MVP：

- 外部整文件覆盖安装器
- fork Miloco 做内置功能
- 多后端姿态模型体系
- 自动模型下载
- 多模式匿名化产品化

这样收敛的原因只有一个：

- 先证明“姿态匿名化 + 外部运行时 patch”这条路在 Miloco 上是可用的

---

## 12. 计划执行顺序

### 阶段 A：改造你的 demo 为“可嵌入 filter”

目标：

- 从命令行视频脚本，抽成可复用帧处理模块
- 保留 `skeleton_mask`
- 加上失败保守降级逻辑

### 阶段 B：做外部 patch 包

目标：

- 独立安装
- 启动自动 patch `_encode_video_mp4`
- 不改 Miloco 仓库源码

### 阶段 C：做最小验证

目标：

- 选 3 组固定 clip
- 对比匿名化前后 Omni 返回
- 记录性能与漏遮挡情况

### 阶段 D：再决定是否继续

只有当下面两点同时成立时，才值得继续：

1. 匿名化后 Omni 仍有业务价值
2. 漏遮挡问题能通过保守降级控制住

否则就应及时止损，而不是继续堆模型和配置。
