# 官方 API 覆盖与解析范围

依据 2026-10-20 获取的 [UVI UVIScript 官方参考](https://lua.uvi.net/) 和 [Falcon 官方手册](https://manual.uvi.net/falcon/en/)。本机引擎为 Falcon 27.0.2。

仓库 `src/falcon_mcp/data/api_coverage.json` 提供 **1489 条**类成员/函数签名/属性/常量记录，其中 **431 条**映射到直接工具，其余映射到原生 Lua 扩展。这里包含继承成员和不同类上的同名属性，不是 1489 个独立操作或 1489 项通过的硬件测试。`falcon_api_coverage(query,offset,limit)` 可以逐项查询原始签名、归属类、官方来源和对应工具。

## 可调用路径

| API 类型 | 入口 | 运行条件 |
|---|---|---|
| Element 与引擎子树、参数和已有连接 | 查询/调参工具；`falcon_scan_project` | 已载入 0.2 桥 |
| 效果链、采样、播放范围、IR、MultiSample mapping/zone purge | `edit_effect`、`load_sample`、`set_playback`、`load_impulse`、`mapping` | 对应现有节点和有效文件；等待官方异步回调 |
| 音符、VoiceId、渐变、表达、Script Modulation | `start_voice`、`voice_control`、`play_notes` | 句柄保留原生 userdata 身份；同回调的 sample offset 由 start_voice 应用 |
| 原生事件与 MIDI | `post_event`、`post_midi`、`send_midi`、`poly_aftertouch` | 使用各 API 自己的通道编号与时间单位 |
| MIDI sequence/event API | `midi_file` | 句柄仅属于当前 Lua 实例；资产位于 `<home>/assets` |
| ScriptProcessor saveState/loadState | `script_state` | 仅脚本状态，不是 Program/Multi 文件 |
| 时间、音乐上下文、按键、CC、位置、单位转换 | `query_api` | 音频线程回调内执行 |
| UI 类、事件回调、线程、一般文件/异步操作、其他 Lua API | `write_extension` | 写入原生 Lua；随后重载脚本 |

工具表中的名字省略了 `falcon_` 前缀。`api_coverage(probe_runtime=true)` 探测当前引擎中 44 个包装函数是否存在，**不会**逐一执行有副作用的函数。

原生扩展与桥合并为一个 Lua 文件。自定义 `onInit` 与桥初始化协同运行，`onNote`、`onSave`、`onLoad`、UI 控件等在官方上下文中工作。接口不执行邮箱里的任意 Lua。更改扩展后使用 Falcon ScriptProcessor 的 reload 按钮；官方没有源码替换/重载 API。写入空字符串可清除扩展，然后重载。写入前保留旧扩展备份；语法错误或无限循环仍会影响这个 ScriptProcessor，需修正源码再重载。

## 官方没有提供的能力

- 合成结构中 Layer、Keygroup、Oscillator、Modulator 和调制连接的创建/删除；效果链 API 是例外。
- Program/Multi 加载保存、宿主/音频驱动配置、ScriptProcessor 源码读取和自动重载。
- 受保护银行内部数据、完整内部 MultiSample zone 列表，以及冻结整个引擎的原子快照。

需要这些操作时，先由用户或 GUI 工作流准备，再通过实时桥读取和修改官方暴露的状态。加载/保存 Program 按用户要求不列为交付前提。

## 工程解析的完整性

扫描从 Synth 或所选根开始，合并 parts/program/layers/keygroups/oscillators/inserts/auxs/sends/mods/eventProcessors/children，按真实引擎路径去重，读取每个参数的定义和值，以及每条调制连接的全部参数。长字符串分块取回，切片分页；采样、LoopLab、purge、映射加载状态和 zone 数量在可用时附带。

调制源按绝对路径、`$Keygroup/...` 等作用域路径和 `@MIDI` / `@VoiceParam` 的最近祖先作用域解析；BusRouter 目的地解析为路由边。原始 Source/Destination 与连接设置保留，不会把无法解析的边伪装成成功。

`complete=true` 表示本次公开扫描没有读取错误、截断或检测到的结构变化；不是插件文件的二进制逆向结果，也不是自动化期间参数的原子冻结。报告包含 `atomic=false`、`structure_verified`、错误及限制。UML 包含组成、调制和路由，可输出源码并本地绘制；裁剪会明确返回省略边数。

## 实机与测试覆盖

当前官方 Analog Poly 模板实测：42 个节点、372 个参数、11 条调制连接、12 条路由，所有关系解析成功；原生 VoiceId 跨 MCP 的控制/释放、MIDI 创建/插入/读取/保存、三种日志入口均通过。全部模块和所有签名尚未逐一实机验收，特别是不同采样器、IR、映射文件、UI 控件及 VST 宿主组合，不能用覆盖索引替代这些测试。详细记录见 [validation.md](validation.md)。
