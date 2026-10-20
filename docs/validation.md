# 验收记录

日期：2026-10-20。平台：Windows，Falcon standalone 27.0.2，Python 3.12.9，MCP Python SDK 1.30.0，RTX 3070 Ti Laptop 8 GB。

**最新 0.2.1：63 项回归测试通过，已重新完成 Falcon 实机验收。**新增修复、真实超时/重载恢复及升级步骤见 [bugfixes-0.2.1.md](bugfixes-0.2.1.md)。下文保留 0.2.0 的初次验收记录。

## 自动化检查

- `pytest -q`：**42 项通过**（0.2，含真实 stdio 客户端与 Lua 5.1）。
- `ruff check src tests scripts`：通过。
- Python 编译检查：通过。
- 构建 `falcon_mcp-0.2.0-py3-none-any.whl`：通过；核对 wheel 内两份 Lua、API 覆盖索引、参数索引、描述词库与源码一致。
- MCP 通过真正的 stdio 客户端初始化、列工具、调用、结构化结果、资源读取及无效输入拒绝测试，不只是直接调用 Python 函数。
- Lua 分发器使用 Lupa 的 **Lua 5.1** 执行测试，覆盖参数批次预检查、旧值检查、类型与范围、无界范围标记、selector 注入拒绝、异步回调和 panic 后取消的序列不复活。
- 桥测试覆盖 token/ID、不确定写入的阻断与恢复、进程重启后的状态保留、两个客户端的系统文件锁。
- 音频测试覆盖正弦波频谱/RMS、反相立体声、路径越界、时长上限、静音不启动模型、工作进程失败与超时终止。
- 在新进程中导入服务器并创建工具后，`torch` 和 `transformers` 均不在 `sys.modules`。
- 0.2 新增：完整工程扫描的失败标记、调制作用域解析、UML 本地图片/源码、标签转义、报告路径限制；已知正弦 PSD 能量/频率、反相立体声、谐波 THD、LUFS、静音与诊断文件格式。
- 日志包含无效输入，凭据脱敏，轮询日志不会嵌套复制历史；模型启动中取消、超时、失败均验证进程退出。

Lua 测试使用小型引擎替身，不能代替下面的 Falcon 实机结果。

## Falcon 实机

使用官方 `01 - Analog - Poly` 模板添加桥，没有加载用户音色。实际验证：

| 项目 | 结果 |
|---|---|
| 桥启动、ping | 读取到 48000 Hz / 120 BPM 和真实 Program 上下文 |
| 引擎浏览 | 读取 Part、Keygroup、MinBlepGenerator 振荡器与调制器 |
| 参数定义 | 读取 Falcon 当前值、类型、范围和只读标志 |
| 参数写回 | 振荡器 Gain 改至 0.1；读回 0.10000000149012，容差验证成功 |
| 无效参数 | `NoSuchParameter` 被明确拒绝 |
| 效果链 | 在 Program 添加 Gain 效果，确认完成，再移除测试效果 |
| 参数恢复 | 恢复测试前 Gain / Waveform 值 |
| 调制查询 | 读取 AmpEnvMod 的已有连接、Source、Ratio、Bypass 等定义 |
| 测试音符 | MCP 调度 MIDI 48、60、67，力度 80，时长 700 ms，起音间隔 1 s |
| 输出回录 | 5 秒、48 kHz、双声道 WAV，非静音，无削波 |
| 本地模型 | CLAP 在 CPU 和 CUDA 均完成推理，默认离线读取本地缓存 |
| 推理后状态 | `model_loaded=false`、`worker_pid=null`，序列 `playing=false` / `error=""` |
| 0.2 MCP 工具发现 | 实际 stdio 客户端发现 42 个带 schema 的工具 |
| 全公开工程解析 | 42 个节点、372 个参数、11 条调制连接；`complete=true`，无结构变化 |
| 连线解析 | 11 条调制边、12 条路由边全部解析，无未识别来源 |
| UML | PlantUML / Mermaid / SVG / PNG；42 个节点，无截断，省略外部边为 0；PNG 已目视检查 |
| 原生 VoiceId | 实际返回 userdata；使用不透明句柄完成发声、changeVolume、releaseVoice |
| MIDI 文件 | 1 轨创建、插入 channel=0 的 NoteOn、分页读取、保存唯一文件、释放句柄 |
| 日志 | server/engine/native 三入口读取成功，无效 note=128 被拒绝并审计 |
| 运行时 API 探测 | 44 个包装函数在本机 Falcon 中均存在；这是存在性检测 |
| 原生扩展通道 | 真实 MCP 写入最小 onInit 扩展、GUI 重载、确认 print 中 beat2ms(1)=500 且桥继续响应，然后清除扩展并恢复标准桥 |
| 频谱/声谱图等 | 用已有真实双声道录音生成全部图表与 JSON；调用不加载模型 |

模型官方 checkpoint：`laion/clap-htsat-unfused`，本机缓存 revision `8fa0f1c6d0433df6e97c127f64b2a1d6c0dcda8a`。

最终 CUDA 回录测试的测量：峰值约 **-26.87 dBFS**，`silent=false`，`model_released=true`。模型返回：

> 亮度倾向温暖圆润；起音与延音倾向短促弹拨；质感倾向锯齿蜂鸣；动态变化倾向稳定；空间感倾向干燥贴近。

这是候选描述匹配结果，不作为音色识别正确率的验收。可审阅的实测结构化示例见 [live-feedback-example.json](live-feedback-example.json)。包含本机完整路径和录音的原始报告在被 Git 忽略的 `.runtime/live-smoke.json` 与 `.runtime/audio/`。

## 实测发现与处理

1. Falcon 原配置为 M-Audio ASIO，默认 Realtek loopback 无法录到输出。临时改为 Windows Audio / Realtek 共享输出后完成非静音闭环；验收结束恢复原 ASIO 配置。
2. Conda NumPy/MKL 与现有 CUDA PyTorch 的 OpenMP 运行时冲突，在独立 `.audio-venv` 使用 pip NumPy/SciPy 修复，未通过容忍重复 OpenMP 绕过错误。
3. UVI `SignalConnection.Ratio` 使用 `min=1,max=0` 表示无界；参数验证按此处理。
4. Lua 5.1 不能跨 `pcall` 的 C 边界 yield。异步完成等待位于受保护分发之外，回调内部不等待。
5. 旧 mailbox 指令在脚本重载时不会重放，避免重复演奏/修改。
6. 空 Lua 对象可被 saveData 编码成 `[]`，VoiceId userdata 字段会被静默丢弃；前者在桥中规范化，后者保留在 Lua 中并通过实例专属句柄控制。
7. MultiSample 的通用 purge/unpurge 可能无报错但不生效；改用 `purgeZones` / `unpurgeZones`，错误入口明确拒绝。
8. 连接参数为 false 时原来的 Lua `and/or` 表达式会丢值，已保留布尔 false。
9. 一个客户端完成 reconcile 后另一个仍保留旧的本地阻断标记；现在每次以共享磁盘状态为准。
10. 畸形 JSON 数组回执会导致 `.get` 异常；现在忽略无效形状，继续有界轮询。
11. 批量 setter 运行期失败时曾丢失前面成功项的结果；现在返回已执行项、错误和 partial 状态。
12. 模型创建阶段取消可能遗留进程，录音取消可能遗留线程；现等待创建完成后清理模型进程，并用停止信号结束录音。
13. 原生 MidiEvent 使用 channel=0..15，postEvent 的 delta 是毫秒；已修正接口和测试。
14. `getTime` 在脚本顶层可能报 RenderContext 线程错误；初始化移入音频回调，print 日志对不可用时间做保护。
15. `setSampleOffset` 必须在音符开始渲染前同一回调使用；start_voice 提供 `sample_offset_ms`，后续调用说明限制。
16. 读取日志的完整结果如果再次写入审计，会递归膨胀；改为记录本次日志读取的请求引用，原始记录仍完整留存。

## 未声称完成的验证

- 没有逐一验证全部 182 类模块、3066 个参数和全部效果。
- 1489 条官方 API 索引记录的直接/扩展映射不是 1489 项独立硬件测试。最小 onInit 扩展通道已实机验证，所有 UI 类/回调未逐项实机验证。
- 调制连接编辑、样本加载/播放循环、purge/unpurge、输出路由、CC/弯音等有实现，尚未全部逐项做实机验收。
- 没有对 VST/各 DAW 组合、macOS、Linux 做 Falcon 实机验收。
- 没有训练或微调 CLAP，也未对音色描述准确度做标注集评估。
- 没有 Program 创建/保存 API、合成节点创建 API、VST 离线宿主或进程隔离音频捕获。

## 重现

预先在 Falcon 加载桥，确保输出可被所选 loopback 回录：

```powershell
$env:FALCON_MCP_HOME = "$PWD\.runtime"
$env:FALCON_AUDIO_PYTHON = "$PWD\.audio-venv\Scripts\python.exe"
$env:FALCON_MODEL_DEVICE = "cuda"  # 无 GPU 时设 cpu
.venv\Scripts\python.exe scripts/smoke_live.py --record --describe
# 不改变当前驱动；复用 audio 目录中的真实录音验收 0.2
.venv\Scripts\python.exe scripts/smoke_v2.py --audio test-581a8fe716f1475493d2cda81aa2baf3.wav
```

该脚本会拒绝把静音录音当作通过。模型生命周期与测量测试可离开 Falcon 单独运行。

0.2 实机验收汇总写入 `.runtime/acceptance-v2.json`，完整工程及图片在 `.runtime/reports/`，完整审计在 `.runtime/logs/`。这些本机文件被 Git 忽略，避免将工程细节、路径和音频随源码分发。真实录音的诊断值：综合响度 **-38.3897 LUFS**，估算真峰值 **-26.8658 dBFS**，左右相关性 **0.2714**；没有削波/DC/反相关诊断提示。这些量值只描述该录音，不是音色质量评分。

## 0.2.2 一次设置与无鼠标部署（2026-10-20）

- 完整回归 **75 passed**；Ruff 检查通过。新增真实 Lua 5.1 首次轮询/拒绝旧会话、幂等源码发布、自动监视成功/失败、Program 桥拒绝持久安装、扩展部署及失联修复、未知写入守卫和 stdio 的 43 工具发现。
- 本机 Python 环境更新为 0.2.2；wheel 构建及 `setup.py`/Lua 包含检查通过。
- 真实 `scripts/smoke_setup.py` 通过 MCP stdio 读取本机 Falcon，确认 43 个工具、模型未加载、GUI 操作 0 次，报告存于 `.runtime/acceptance-setup.json`。
- 本机现有 Falcon 仍运行 Program 下的 0.2.1 桥。新版诊断正确返回 `current_build_loaded=false,master_scope=false,watch_verified_this_instance=false,ready_without_gui=false`。没有替换工程或用鼠标重载；不能把软件测试通过当成 Master/默认 Multi 的自动启动实机验收。
- 完成 [一次迁移](one-time-setup.md) 后，需真实执行 `falcon_setup(verify_watch=true)`，再重开 Falcon/DAW 模板重复验收，才能确认本机安装持续可用。
