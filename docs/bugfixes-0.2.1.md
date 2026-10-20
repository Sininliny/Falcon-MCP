# 0.2.1 缺陷检查与修复

本轮以 0.2.0 为基线，新增 21 项回归测试，总计 **63 项通过**。新增测试先复现了浮点守卫、声部、MIDI、工程解析、THD、模型清理和回执恢复中的失败，再验证修复。

## 已修复

| 问题 | 修复后的行为 |
|---|---|
| 原样回传刚读出的浮点 `expected` 也被拒绝 | UVI JSON 约 14 位有效数字的舍入现在允许最多 `1e-13` 相对误差；整数/布尔/字符串仍严格比较，无绝对误差下限，不掩盖接近零的编辑 |
| setter 成功但读回失败会丢失已执行结果 | 返回每项已执行状态、读回错误和 partial；setter 自身抛错时注明 outcome_unknown |
| 创建声部后设置 sample offset 失败，声部未登记 | 先登记原生 VoiceId，失败时释放；释放本身失败则保留追踪供 panic 处理 |
| playNote 返回 nil 使序列卡在 playing | 明确记录错误，释放当前序列已生成声部，结束 playing 状态 |
| 256 声部上限连 NoteOff/Controller 都阻断 | 仅新 NoteOn 受创建限额约束，已有声音仍可释放 |
| Lua 地址/getTime 在重载后可能重复 | 引擎实例改用首次有效请求的随机 host UUID，避免工程扫描及句柄错误复用 |
| MIDI 句柄重载后从 1 开始，旧句柄指向新对象 | 句柄包含实例 nonce；旧实例句柄明确过期 |
| MIDI 轨号超过当前文件范围仍调用原生 API | 调用前按 sequence.numTracks 检查并拒绝 |
| 单条调制连接的读取错误没有影响 complete | 嵌套错误也标记工程不完整，保留部分快照 |
| 不可序列化的调制 Source 使整个图解析崩溃 | 原始错误元数据保留，输出未解析关系而不丢失整个报告；路由目的地也检查类型 |
| 子节点分页游标不前进时无限循环 | 检测停滞并保存 complete=false 的部分报告 |
| 启动模型时连续取消会遗留进程 | 创建和清理由独立任务持有；多次取消仍等待退出再释放调用锁 |
| 模型管道异常跳过生命周期字段清理 | 排空错误单独记录，finally 清除进程状态并记录释放 |
| 模型启动日志填满管道后，kill 后的 wait 仍可能挂起 | 创建阶段取消也启动 stdout/stderr 排空，先排空再等待进程关闭 |
| 录音清理再次收到取消时线程仍在运行 | 两种录音工具均等待音频线程关闭后才返回、释放锁 |
| 重载导致写入回执丢失，reconcile 仍解除阻断 | 仅匹配原请求 ID 的回执自动解除；缺失时保持 unknown，检查状态后可显式 acknowledge_unknown |
| status 在读取共享状态之前返回旧的阻断信息 | ping 后返回最新共享状态 |
| 短低频片段重复计算重叠频谱区域，给出假 THD | 分辨率不足、无可测高次谐波或基频太弱时返回 null 并说明原因 |
| 审计缺少桥请求 ID，实机验收可能使用旧 Lua | 增加 bridge_dispatch 请求 ID 与 reconcile 审计；status 提示版本不匹配，smoke 强制匹配桥版本 |

## 实机验收

环境仍为 Windows、Falcon standalone 27.0.2、48 kHz，保留原 ASIO 配置和当前音色。

- 真实 stdio MCP 流程通过，42 个工具；完整工程仍为 42 个节点、372 个参数、11 条调制连接及 12 条路由，`complete=true`，UML 无截断/省略关系。
- 发声、changeVolume、releaseVoice、MIDI 文件创建/插入/读取/保存/释放和日志入口通过。
- 用极短客户端超时和 **Gain 同值写回**制造真实超时，匹配回执后 `resolved=true,outcome=completed`。
- 第二次同值写回后重载 Lua，丢失真实回执；reconcile 返回 `resolved=false,outcome=unknown`，写入仍被阻止。读取 Gain 与测试前完全相同，再显式确认解除阻断。
- 重载后新实例 nonce 与旧实例不同；创建新 MIDI 对象后，旧句柄仍被拒绝；track=2 对单轨序列在进入原生 API 前被拒绝。
- 本地 CPU CLAP 正常推理通过，返回前 `model_loaded=false,worker_pid=null`；重复取消和管道失败场景由实际临时 Python 子进程回归测试覆盖。

本机报告位于 `.runtime/acceptance-0.2.1.json`、`.runtime/reconcile-0.2.1.json`、`.runtime/model-0.2.1.json`，完整审计位于 `.runtime/logs/`；均不提交版本库。

## 升级

安装 0.2.1 后执行 `falcon-mcp prepare`，在 Falcon ScriptProcessor 重载生成的 Lua。当前本机已重载最终版本。已有 voice/MIDI 句柄随重载失效，需要重新获取。

如遇回执丢失，先读取受影响参数确认现状，再调用 `falcon_reconcile(acknowledge_unknown=true)`；这不是成功确认或自动重试。公开 API 的边界及未逐项实测的模块范围仍见 [api-coverage.md](api-coverage.md)。
