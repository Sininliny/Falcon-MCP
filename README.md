# Falcon MCP 0.2.1

面向 GAI 的本地 UVI Falcon 2027 MCP 服务器。通过 **UVIScript 实时桥**控制引擎，使用 **按需启动的开源 CLAP 模型**提供音色描述，形成“查询 → 调参 → 试听回录 → 描述 → A/B 比较”的工作流。

在 Windows、Falcon standalone **27.0.2** 上开发与实测。加载/保存 Program 按需求移出本版范围。

## 能力与边界

| 能力 | 实现 |
|---|---|
| 结构查询 | 分页浏览 Synth / Part / Program / Layer / Keygroup、振荡器、效果、事件处理器、调制器、发送和其他子元素 |
| 参数 | 实时定义、类型/范围/只读验证、批次预检查、可选旧值比较、写后读回 |
| 离线索引 | 官方文档 182 类模块、3066 个参数的名称、类型、范围和单位 |
| 调制 | 已有连接的参数，以及已有 LFO、包络、宏等节点的参数 |
| 效果链 | 插入、移除、移动，等待异步完成回调 |
| 采样 | 加载到已有采样振荡器、循环范围、purge/unpurge |
| 演奏 | 有限时长测试音符、CC、弯音、触后、Program Change、释放桥生成的声音 |
| 测试反馈 | Windows 输出回录、客观测量、中文音色特征描述、目标文本匹配、A/B 差异 |
| 模型生命周期 | 每次分析独立进程，结束/异常/超时/取消后退出；服务器不加载模型 |
| 工程解析 | 全部公开引擎节点、参数定义/值、调制连接、发送/输出路由、采样元数据；JSON 快照与分页检索 |
| UML | PlantUML、Mermaid、SVG、PNG；组合、调制、路由关系，支持聚焦子树 |
| 音频诊断 | 数值频谱、声谱图、包络、波形、LUFS、估算真峰值、DC、相位/单声道兼容性、测试音 THD |
| 日志 | 完整请求/结果、错误堆栈、耗时、模型进程输出；本地 JSONL、引擎 print 和原生日志 |
| 官方 API 覆盖 | 直接工具 + 可自动重载的原生 Lua 扩展；1489 条官方签名/属性索引可查询 |

**公开 API 的边界：**UVIScript 无法创建/删除 Layer、Keygroup、Oscillator、Modulator 和调制连线，也没有 Program 加载/保存接口。先在 Falcon 中准备结构，再由 GAI 调整已有节点；效果链编辑是例外。本项目不会声称实现不存在的接口，也不绕过音色保护。

常用引擎功能可以直接通过 MCP 调用。UI 控件、事件回调、线程/异步任务等必须在 Falcon Lua 上下文运行的 API，可用 `falcon_write_extension` 写入原生脚本，首次设置文件监视后由 Falcon 自动重载。**API 可达不代表每个接口都已逐项实机验收，也不代表全部接口都能在一次即时 JSON 调用中运行。**详细映射和边界见 [API 覆盖说明](docs/api-coverage.md)。

Standalone 和 VST 内可使用同一 Lua 桥。本机验证使用 standalone；VST 宿主组合尚未逐一实测。宿主必须持续处理音频，休眠会使桥轮询暂停。

## 安装与连接

需要 Python 3.11+，建议 3.12。本地模型需要 PyTorch 2.6+。服务器只使用 stdio，不监听网络端口。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[capture,feedback]"
$env:FALCON_MCP_HOME = "$PWD\.runtime"
.venv\Scripts\python.exe -m falcon_mcp prepare
.venv\Scripts\python.exe -m falcon_mcp download-model
```

`download-model` 只下载权重到磁盘，不载入模型、不占用显存。默认模型 `laion/clap-htsat-unfused` 使用 Apache 2.0 许可，权重约 615 MB。推理默认离线，不上传音频。

首次在 Falcon 的 **Master → EVENTS → + → Script Processor → Default** 添加一个处理器，使用文件夹按钮载入 `prepare` 输出的 `falcon_bridge.lua`。然后只做一次：

1. 在该处理器开启 **WATCH FOR SCRIPT CHANGES**。
2. 从主菜单 **Save as default Multi** 保存；使用 DAW 时也保存到宿主模板/工程。默认 Multi 会在新启动的 standalone/plugin 实例载入。
3. 删除旧的 Program 桥，整个实例只保留一个桥。在空闲、没有持续音符时运行下面的验收命令。

```powershell
$env:FALCON_MCP_HOME = "$PWD\.runtime"
.venv\Scripts\python.exe -m falcon_mcp setup --verify-watch
```

只有读到对应构建且自动重载探测成功，才返回 `installation.ready_without_gui=true`。重启 Falcon / 重开宿主模板后再执行一次，确认宿主确实保存了安装；公开 API 无法直接读取默认 Multi/宿主模板配置。详细操作和恢复边界见 [一次设置与自动连接](docs/one-time-setup.md)。

Master 桥不会随着替换 Program 消失。生成的事件经过下游 MIDI 路由；跨 Part 使用 `Synth.parts[1].program...`，`Program` 等快捷根取决于脚本上下文。Program/Part 桥仍可用于临时隔离试听，但不视为持久安装。

保持 Falcon 音频引擎运行，然后检查：

```powershell
.venv\Scripts\python.exe -m falcon_mcp status
```

客户端配置如下，按实际位置修改绝对路径。MCP 进程和 `prepare` 必须使用相同的 `FALCON_MCP_HOME`：

```json
{
  "mcpServers": {
    "falcon": {
      "command": "F:/source/code/Falcon-MCP/.venv/Scripts/python.exe",
      "args": ["-m", "falcon_mcp", "serve"],
      "env": {
        "FALCON_MCP_HOME": "F:/source/code/Falcon-MCP/.runtime",
        "FALCON_MODEL_DEVICE": "cpu"
      }
    }
  }
}
```

本机开发时将模型依赖独立在 `.audio-venv` 中，复用已有 CUDA PyTorch。使用该环境时，配置再加入 `"FALCON_AUDIO_PYTHON": "F:/source/code/Falcon-MCP/.audio-venv/Scripts/python.exe"`。独立环境可选，普通全新安装默认使用服务器自己的 Python。

MCP 启动会自动准备桥文件，相同内容不改写文件。启用监视后，源码更新和 `falcon_write_extension` 由 Falcon 自动重载；扩展调用等待精确构建 ID 回执，`applied=true` 才表示已生效。更新后可调用 `falcon_setup(verify_watch=true)` 验证。禁止用反复 computer use / 鼠标操作维持连接。重载会重置脚本控件与 voice/MIDI 句柄，应在空闲时执行；新桥通过握手会话拒绝重放残留写入。

## 按需音色反馈

CLAP 是音频/文本匹配模型，**不是自由生成式音频问答模型**。本项目用五组候选描述评估亮度、起音/延音、质感、动态变化、空间感，返回中文描述、每组前三名和相似度差距；候选接近时标明不确定。词库位于 `src/falcon_mcp/data/timbre_descriptors.json`，可扩展。

基础测量包括峰值、RMS、峰均比、削波比例、频谱重心、85% 滚降、平坦度、过零率；`falcon_diagnose_audio` 另提供带门控的综合 LUFS 等诊断。模型相似度不是概率或音质评分。

可传入英文 `targets`，例如 `A warm dark plucked synthesizer bass with a short decay.`。比较前后必须保持音符、力度、增益、目标文本和录音条件相同。模型不会反推出 Falcon 参数值，也不自动修改音色。

```powershell
# 将 <=30 秒 WAV/FLAC/AIFF/OGG 放入 .runtime/audio
.venv\Scripts\python.exe -m falcon_mcp describe my-test.wav
```

**内存策略：**服务器初始化和参数操作不导入 `torch` / `transformers`。描述调用才启动子进程，加载模型、推理、返回结果并退出；超时/取消则终止并等待退出。进程退出释放模型 RAM/VRAM，磁盘权重缓存保留。静音直接返回“无法可靠判断”，不启动模型。长片段按不超过 10 秒的窗口覆盖分析，避免随机截取。

## MCP 工具

共 **43 个工具**。新增功能已纳入相同的 stdio 服务，无需另启动服务。

| 工具 | 用途 |
|---|---|
| `falcon_status` / `falcon_reconcile` | 连接、能力、模型进程；超时后检查引擎回执 |
| `falcon_setup` | 一次安装检查、自动重载验收；返回具体恢复步骤，不操作鼠标 |
| `falcon_search_catalog` | 离线参数元数据 |
| `falcon_list_children` | 分页结构查询，返回可用 selector |
| `falcon_list_parameters` / `falcon_get_parameters` | 实时参数定义/读取 |
| `falcon_set_parameters` | 预检查、修改、读回 |
| `falcon_get_connections` / `falcon_set_connection_parameters` | 已有调制连接 |
| `falcon_edit_effect` | 插入、移除、移动效果 |
| `falcon_load_sample` / `falcon_set_playback` / `falcon_purge_samples` | 已有采样振荡器管理 |
| `falcon_set_output` | 输出总线 |
| `falcon_play_notes` / `falcon_send_midi` / `falcon_panic` | 测试演奏，panic 只释放桥生成的声音 |
| `falcon_list_audio_devices` / `falcon_record_test` | 输出回录设备、就绪后触发测试音符 |
| `falcon_measure_audio` | 不加载模型的客观测量 |
| `falcon_describe_timbre` | 本地音色描述与目标匹配 |
| `falcon_compare_audio` | 前后测量和目标匹配差异 |
| `falcon_record_audio` | 回录正在加工的输出，不额外触发音符，可直接生成诊断图 |
| `falcon_scan_project` / `falcon_query_project` | 全公开工程快照、参数/连接/结构分页检索 |
| `falcon_export_uml` | 根据工程快照导出 UML 源码与本地图片 |
| `falcon_diagnose_audio` / `falcon_spectrum` | 完整诊断报告及图表 / 可配置 FFT 数值频谱 |
| `falcon_get_sample_info` / `falcon_mapping` / `falcon_load_impulse` | 切片/采样元数据、MultiSample 映射/分区 purge、混响 IR |
| `falcon_query_api` | 节拍/速度/采样率/按键与 CC 状态/位置/单位转换 |
| `falcon_start_voice` / `falcon_voice_control` | 定向声部、渐变/音量/音高/声像、Script Modulation、释放 |
| `falcon_poly_aftertouch` / `falcon_post_midi` / `falcon_post_event` | 复音触后、原生 MIDI 与 UVIScript 事件 |
| `falcon_midi_file` / `falcon_script_state` | MIDI 文件创建/编辑/保存；当前 ScriptProcessor 状态 |
| `falcon_write_extension` / `falcon_api_coverage` | 原生脚本扩展；官方接口映射与运行时函数探测 |
| `falcon_get_logs` | 分页读取服务审计、引擎 print、Falcon 原生日志 |

资源 `falcon://capabilities` 提供能力说明，提示模板 `refine_timbre` 提供迭代流程。工具包含输入 schema、结构化输出和读写注解。

selector 示例：`Synth.parts[1].program.layers[1].keygroups[1].oscillators[1]`、`Program.inserts[2]`、`Program.layers[1].keygroups[1].mods[2]`。索引 **1 起始**，`.program` 是单个引用。`children` 按内部名称排序，结构变化后需重新查询索引。

写入示例：

```json
{
  "selector": "Program.layers[1].keygroups[1].oscillators[1]",
  "changes": [{"name": "Gain", "value": 0.25, "expected": 0.5}]
}
```

使用 API 内部名与单位：Gain 通常为线性倍率、Pan 为 -1..1、时间可能为秒。实时定义优先于文档快照；UVI 的 `min > max` 表示无界数字参数。

整批预检查不是引擎事务或 Undo 块，运行期失败仍可能部分写入。写入超时/取消后会保存未确定状态并阻止后续写入；调用 `falcon_reconcile` 检查回执后再决定下一步，不能盲目重试。多个 MCP 进程通过操作系统文件锁共享同一个邮箱。

0.2.1 起，reconcile 必须匹配原请求 ID 的回执才会自动解除阻断；脚本重载导致回执丢失时，返回 `resolved=false,outcome="unknown"` 并继续阻断。先读取受影响参数确认当前状态，再显式调用 `falcon_reconcile(acknowledge_unknown=true)`。这个操作仅确认已核查未知结果，不重放原命令、不声称原命令成功。0.2.2 起，每次写入前先进行只读握手，新 Lua 拒绝旧会话写入；握手失败不会声称写入已发出。存在未确认写入时，源码更新会推迟，保留回执以便核查。

## 解析工程、生成 UML 与诊断

按顺序调用以下工具；`report_id` 使用上一步返回的值：

```json
{"tool":"falcon_scan_project","arguments":{"root":"Synth","verify_structure":true}}
{"tool":"falcon_query_project","arguments":{"report_id":"<返回值>","section":"connections","limit":20}}
{"tool":"falcon_export_uml","arguments":{"report_id":"<返回值>","include_parameters":true}}
{"tool":"falcon_diagnose_audio","arguments":{"path":"my-test.wav"}}
```

这是调用顺序示意；实际 MCP 调用由客户端传入工具名和 arguments。JSON、`.puml`、`.mmd`、SVG 与 PNG 保存在 `<home>/reports`，不发送到外部渲染服务。大工程用 `root_path` 聚焦子树，返回值明确说明截断和未显示的外部连线。解析得到的是**公开实时状态快照**，不包含官方未暴露的脚本源码、受保护音色内部数据、MultiSample 各内部 zone 列表及宿主/驱动配置。扫描不是原子操作；错误、限额或结构变化会使 `complete=false`，可读取已完成的部分。

`falcon_diagnose_audio` 输出每通道 Welch PSD（dBFS/Hz）、声谱图、10 ms 包络、峰值波形概览、立体声向量图和完整数值 JSON。LUFS 仅对至少 400 ms 的单/双声道计算；真峰值为 4 倍 FIR 过采样估算，不是认证仪表。提供 `fundamental_hz` 可计算稳态单音谐波/THD，不用于复音或持续调制音色。所有这些分析均不加载模型。

当片段太短、频率分辨率不足、基频能量不足或 Nyquist 以下没有高次谐波时，THD 返回 `available=false,thd_db=null` 并说明原因，避免返回误导性的数值。0.2.1 修复记录见 [bugfixes-0.2.1.md](docs/bugfixes-0.2.1.md)。

`falcon_start_voice` 返回字符串声部句柄，原生 VoiceId 保留在 Falcon 中；用 `falcon_voice_control` 控制或释放，重载后句柄失效。`sample_offset_ms` 在发声同一音频回调内应用；后续单独调用 `setSampleOffset` 不能移动已经播放的声音。普通演奏 channel 为 1..16，原生 `MidiEvent` channel 为 0..15，`post_event.delta` 单位为毫秒。

## 完整日志

`<home>/logs/audit-*.jsonl` 按进程/会话追加，包含 UTC 时间、trace ID、完整 MCP 参数/结果（包括输入被拒绝）、桥操作与耗时、异常堆栈、模型 stdout/stderr/退出状态。凭据字段自动脱敏；读取日志的结果只记录引用，避免日志自身递归膨胀。没有自动删除历史记录。

`falcon_get_logs(source="server")` 读取这些审计记录；`source="engine"` 读取桥捕获的最近 256 条 print，offset 为 sequence 游标，缺口用 `gap` 标明；`source="native"` 读取 Falcon 的 `ScriptProcessor.log`，offset 为行号。原生日志只包含 Falcon 自身实际写入的内容；它不能提供引擎没有公开的内部调试信息。

## 回录与配置

回录只打开 **Windows WASAPI 输出 loopback**，不使用麦克风。默认设备可能与 Falcon 输出不同，使用 `falcon_list_audio_devices` 选择 `device_index`。ASIO 和独占模式通常绕过共享混音器，可能录到静音；可让 Falcon 临时使用共享 Windows Audio 输出，或由 DAW 导出 WAV 再分析。回录包含该端点的其他应用声音，应使用隔离输出。本版没有进程隔离捕获或 VST 离线渲染宿主。

| 环境变量 | 默认值 |
|---|---|
| `FALCON_MCP_HOME` | 用户目录下 `.falcon-mcp` |
| `FALCON_AUDIO_ROOT` | `<home>/audio`；所有音频路径限制在此目录内 |
| `FALCON_MODEL_CACHE` | `<home>/models` |
| `FALCON_AUDIO_PYTHON` | 当前 Python，可指定独立模型环境 |
| `FALCON_MODEL_DEVICE` | `cpu`；支持 `cuda` / `auto` |
| `FALCON_MODEL_ID` | `laion/clap-htsat-unfused`，替代值必须兼容 CLAP 架构 |
| `FALCON_MODEL_OFFLINE` | `1`；设 `0` 才允许推理时下载缺失文件 |
| `FALCON_BRIDGE_TIMEOUT` / `FALCON_MODEL_TIMEOUT` | 15 / 180 秒 |
| `FALCON_NATIVE_LOG` | Windows 默认 `%APPDATA%/Falcon/ScriptProcessor.log`，其他宿主可显式指定 |

桥仅通过本地 JSON 文件通信，使用随机 token、请求 ID 和原子请求写入。token 用于连接分隔，不能隔离本机同用户恶意进程。运行目录含 token，不应提交或共享。Conda/PyTorch 出现 `OMP Error #15` 时应使用干净模型环境，不建议强行容忍重复 OpenMP。

## 验证

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev,capture]"
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests scripts
.venv\Scripts\python.exe scripts/smoke_live.py --record --describe
# 0.2：已载入新版桥，复用 audio 目录中已有的真实录音
.venv\Scripts\python.exe scripts/smoke_v2.py --audio my-test.wav
```

最后一条需要预先载入桥，会演奏音符、生成唯一录音文件并调用模型，不加载或保存 Program。报告写入 `<FALCON_MCP_HOME>/live-smoke.json`。验收情况见 [docs/validation.md](docs/validation.md)。

## 官方依据

- [最新 Falcon 手册](https://manual.uvi.net/falcon/en/)、[UVIScript API](https://lua.uvi.net/)
- [参数表](https://lua.uvi.net/_elements.html)、[异步操作](https://lua.uvi.net/_async_intro.html)、[Lua 沙盒](https://lua.uvi.net/_lua_reference.html)
- [UVI 结构创建限制说明](https://lua.uvi.net/_agents_md.html)
- [CLAP 模型与许可](https://huggingface.co/laion/clap-htsat-unfused)、[官方代码](https://github.com/LAION-AI/CLAP)

项目遵循仓库 Apache 2.0 许可，不分发 Falcon、付费音色或模型权重。参数索引仅含 API 名称与数值元数据，不复制手册正文。
