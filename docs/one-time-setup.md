# 一次设置与持久启动器（0.2.3）

0.2.2 的文件监视方案有一个错误假设：本机 Falcon 27.0.2 在保存/重开默认 Multi 后不会恢复 **WATCH FOR SCRIPT CHANGES** 开关。0.2.3 改为把稳定启动器保存到 Master；它自己读取并部署新版运行时，**不需要文件监视、逐次点击 reload 或 computer use**。

## 首次安装

1. 备份当前 Multi。运行 `falcon-mcp prepare`，在 **Master** 的一个 Script Processor 中载入输出的 `falcon_bridge.lua`。删除原 Program/Part 桥；整个实例只保留一个桥。
2. 将 Master 桥保存到 **Save as default Multi**。插件用户也保存到宿主模板/现有工程；旧工程不会因修改默认 Multi 自动加入桥。保留原来的默认配置备份。
3. 保持引擎运行，无持续音符时运行 `falcon-mcp setup --verify-reload`，或调用 `falcon_setup(verify_watch=true)`。`verify_watch` / `--verify-watch` 是兼容旧客户端的参数名，实际验证持久启动器部署，不是检查 Watch 开关。
4. 重启 Falcon 或重开宿主模板，再执行相同验收。`persistent_bootstrap=true`、`current_build_loaded=true`、`ready_without_gui=true` 才表示当前运行时完整就绪；`deployment_verified_this_instance=true` 表示本实例还通过了精确构建切换测试。

本机执行示例：

```powershell
$env:FALCON_MCP_HOME = "F:/source/code/Falcon-MCP/.runtime"
F:/source/code/Falcon-MCP/.venv/Scripts/python.exe -m falcon_mcp setup --verify-reload
```

## 加载过程

- `falcon_bridge.lua` 是稳定启动器，保存到 Multi 中；不内嵌连接凭据或可变业务代码。
- `falcon_runtime.lua` 包含引擎工具和用户扩展。MCP 启动、扩展发布时生成此文件，以原子替换发布；相同内容不改写。
- `deployment.json` 最后发布，包含构建 ID。启动器每 250 ms 用官方异步文件 API 读取；源码文件的构建标记必须匹配 manifest，文件总量不超过 256 KiB。
- 编译通过 `loadState` 触发的 **onLoad 非实时回调**执行，使用本机实测可用的 Lua 5.1 `loadstring` / `setfenv`。不会在音频回调里编译，也不会执行请求邮箱传入的代码。
- 每个运行时使用独立 Lua 环境；成功后才切换回调并停止旧桥循环。语法/顶层初始化错误保留上一运行时，`falcon_status.installation.bootstrap_error` 报告原因。修正版的新构建会重新部署，不无限重试同一个错误版本。
- MIDI 通过固定 `onEvent` 入口转交用户 `onEvent` 或具体 MIDI 回调；未定义时原样转发。`onTransport`、`onSave`、`onLoad` 保留委托；内部部署状态不传给用户的 `onLoad`。
- `bootstrap-epoch.json` 保存启动计数，组合运行时代数和主机随机请求 ID，避免重启/更新后旧 voice/MIDI 句柄误指新对象。不要删除该文件以复用旧句柄。

启动器自身的协议升级仍可能需要一次重新载入并保存 Master；普通核心功能与扩展更新只替换运行时，不需要改默认 Multi。状态中的 `startup_persistence=unverified_by_api` 表示公开 API 无法读取宿主/默认配置；实际重启验收单独记录，不能虚报成 API 查询结果。

## 原生扩展生命周期

`falcon_write_extension` 保存版本备份、发布可信本地源码，并等待精确构建回执；`applied=true` 才表示已运行。`runtime_eval="trusted_deployment_file_only"` 明确标记动态编译的来源，不提供任意请求字符串求值接口。

部署会使旧 voice/MIDI 句柄失效，应先停止桥音符。控件属于原生 ScriptProcessor，并不会因替换 Lua 环境自动删除；扩展应通过 `FALCON_BOOTSTRAP.shared` 复用控件和资源，在 `onUnload()` 释放自己的资源，并让后台循环在 `FALCON_BOOTSTRAP.active=false` 时退出。`onUnload` 是本项目生命周期约定，不是新增的 UVI 原生回调。不能假定热更新会重置整个 ScriptProcessor 的控件。

未知引擎写入仍由 reconcile 守卫阻断，部署不能绕过。Falcon 关闭、引擎暂停或宿主让插件休眠时，启动器与桥轮询都无法运行。加载不含 Master 桥的其他 Multi、Clear Multi、删除处理器也会移除桥，需要恢复保存的模板一次。不同 Falcon 实例必须使用不同 `FALCON_MCP_HOME`，不能争用一个邮箱。

## 本机安装与验收

- 默认 Multi：`C:/Users/Sinin/Documents/UVI/Falcon/Default Multi.uvim`。
- 独立持久模板：`F:/source/code/Falcon-MCP/.runtime/templates/Falcon MCP Persistent.uvim`。
- 原工程及设置备份：`.runtime/installation-backup/`，保留迁移前 Multi；Program/Layer/Keygroup 结构不变，实例只有一个 Master 桥。
- 实机先证明 Watch 开关重启后关闭，再在 **Watch 关闭**时验证持久启动器部署成功。
- 退出并重新启动 Falcon 后，不再输入任何界面操作，默认 Master 桥自动连接，启动计数从 1 增至 2，构建切换成功，`ready_without_gui=true`。
- 验收详情：`.runtime/acceptance-bootstrap-after-restart.json`；部署状态 `.runtime/bridge/bootstrap-status.json`；审计 `.runtime/logs/audit-*.jsonl`。运行时、工程、凭据和本机报告不随 Git 提交。

参考：[Falcon 默认 Multi](https://manual.uvi.net/falcon/en/interface/)、[Lua 主块/onLoad/onSave 的非实时约束与模块环境](https://lua.uvi.net/_lua_reference.html)、[状态读写回调](https://lua.uvi.net/_async_intro.html)。持久启动器已在本机 standalone 验收；VST/各 DAW 组合需分别验证。
