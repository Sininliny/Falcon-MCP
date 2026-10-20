# 一次设置与自动连接（0.2.2）

目标是用户完成一次 Falcon 设置后，GAI 的日常操作、扩展发布和版本核验都通过 MCP 完成。服务器不使用 computer use，不点击用户鼠标。首次创建 Script Processor、设置监视和默认 Multi 没有公开 UVIScript API，仍需用户完成一次；这些步骤不能被工具虚报为已自动安装。

## 首次迁移

1. 保留当前工程的备份。在 Master 的 EVENTS 中添加一个 Script Processor，载入 `falcon-mcp prepare` 输出的 `falcon_bridge.lua`。不要把持久桥放在 Program 里，否则更换音色会删除桥。
2. 开启处理器的 **WATCH FOR SCRIPT CHANGES**。删除原 Program/Part 桥；同一个 Falcon 实例不能有两个桥争用同一个邮箱。
3. 保存 **Save as default Multi**。插件用户也把 Master 桥保存到 DAW 模板及需要继续工作的工程；旧工程不会因修改默认 Multi 自动加入桥。不要用新默认 Multi 覆盖需要保留的其他初始配置。
4. 保持音频引擎运行，没有播放序列/持续音符时执行 `falcon-mcp setup --verify-watch`，或调用 `falcon_setup(verify_watch=true)`。工具修改一次探测标记，等待 Falcon 回报精确构建 ID，成功才记录已验证。
5. 重启 Falcon，或重新打开刚保存的宿主模板，再执行相同验收。`startup_persistence=unverified_by_api` 始终明确表示 API 无法读取启动配置；重开后的实际连接才是持久性证据。

本仓库的本机路径是 `F:/source/code/Falcon-MCP/.runtime/bridge/falcon_bridge.lua`。安装更新后重开 Codex 的 MCP 会话以发现新增 `falcon_setup` 工具。直接命令：

```powershell
$env:FALCON_MCP_HOME = "F:/source/code/Falcon-MCP/.runtime"
F:/source/code/Falcon-MCP/.venv/Scripts/python.exe -m falcon_mcp setup --verify-watch
```

## 正常运行

MCP 启动自动生成/更新脚本；相同内容不触碰文件修改时间，避免重启客户端就重置脚本。不同 MCP 进程的生成操作用系统文件锁串行化，源码采用临时文件加原子替换，避免 Falcon 读到半份代码。

`falcon_status` 返回当前和预期构建 ID、Master 位置及当前 Lua 实例的监视验收。连接成功不等于安装完整。重开保存了旧嵌入源码的 Multi 后，如果 Falcon 仍加载旧构建，用 `falcon_setup(verify_watch=true)` 重新发布一次标记，触发监视并验证，整个过程不需要鼠标。

`falcon_write_extension` 保存版本备份、生成源码、等待自动重载并读回构建 ID。相同源码不会重复重载。返回 `applied=false` 或 `requires_reload=true` 时不能继续假定新扩展已安装。先读取 `falcon_get_logs(source="native")` 排查语法和运行时错误；缺少监视或 Master 时，返回首次设置步骤。没有无条件重试和鼠标补救循环。

每次引擎写入前先发送只读 ping 握手。新 Lua 的写入必须匹配当前会话，首次读取磁盘上的残留写入会被拒绝；首次 ping 则立即处理，不再被初始化逻辑吞掉。已经发出的未知写入继续由 reconcile 守卫阻断，不能借重载清除未知结果。

## 边界与恢复

- 加载另外一个不含 Master 桥的 Multi、Clear Multi、手动删除处理器，仍会移除桥；恢复已保存的桥 Multi/模板一次。正常替换 Program 不影响 Master 桥。
- 脚本重载会重置控件、voice/MIDI 句柄及扩展局部状态。部署前停止桥音符；检测到播放序列或跟踪声部时拒绝部署。手动演奏也应先停下。
- Falcon 关闭、引擎暂停或 DAW 让插件休眠时，Lua 轮询不能运行。宿主需持续处理；公开 API 无法从暂停的 Lua 内恢复宿主音频处理。
- 只在同一实例装一个桥。多个 Falcon 实例必须设置不同 `FALCON_MCP_HOME`，分别生成和保存各自的桥，不能复制同一凭据和邮箱脚本到多个实例。
- 删除 `connection.json` 会更换凭据；此时需更新加载的脚本。保留该文件和 runtime 路径以保持既有安装。
- 生成/扩展/部署/错误日志写入 `<home>/logs/audit-*.jsonl`。审计不输出连接凭据。

官方依据：[文件监视自动重载](https://manual.uvi.net/falcon/en/events/)、[默认 Multi 启动和 Master 保存范围](https://manual.uvi.net/falcon/en/interface/)、[Lua 异步文件操作与线程限制](https://lua.uvi.net/_lua_reference.html)。

## 本次验证范围

自动化覆盖真实 Lua 5.1 首次轮询、旧会话写入拒绝、源码发布幂等、监视成功/失败的构建验收、Program 安装拒绝、无鼠标扩展部署、未知写入阻断及 stdio 工具发现。文件监视测试使用可控引擎替身，不替代 Falcon 实机验收。

本机实查的现有桥是 0.2.1，位置为 `/uvi/Part 0/Program`。生成新版脚本后仍回报旧构建，尚未完成 Master / 监视 / 默认 Multi 的首次迁移，不能声称已实机验证新桥自动启动。当前工程没有被替换，也没有操作鼠标。
