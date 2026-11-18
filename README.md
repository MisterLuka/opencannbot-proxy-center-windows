# OpenCANNBot Proxy Center v6

Windows 原生多实例代理中心，使用 Python 标准库运行，不需要 Flask、WSL 或 Git Bash。

## 功能

- 最多创建 3 个独立代理实例
- 每个实例独立保存：
  - 监听端口
  - 固定模型路由
  - VK
  - PID
  - 运行目录
  - 日志
- 支持模型：
  - `glm-5.3`
  - `glm-5.3-flash`
  - `deepseek-v4-flash`（显示名：DeepSeek-V4-Flash）
  - `deepseek-v4-pro`（显示名：DeepSeek-V4-Pro）
- 模型列表自动从网关同步：启动时自动拉取当前 VK 可用的模型，也可在“模型配置”页面手动同步，无需再手工填写模型 ID
- 页面提供“模型配置”，可查看网关同步模型、添加自定义模型、清理网关未列出的模型
- 同一应用即使提交其他模型名，也会被实例端口固定路由到所选模型
- 快速切换模型后自动停止、启动并执行健康检查
- 专用重启等待页，显示每个执行阶段和实时日志
- 静默启动，不常驻 CMD 或 PowerShell 窗口
- 页面“完全退出”会停止全部由代理中心管理的实例并退出页面进程
- 只监听 `127.0.0.1`

## 安装

最简安装（自动下载关联项目）：

```powershell
.\install.ps1
```

未提供 `-RepoDir` 时，安装器会自动获取关联项目 [BadFatCat0919/opencannbot](https://github.com/BadFatCat0919/opencannbot)：优先使用 `git clone`，本机没有 Git 时自动回退为 zip 下载。下载位置为安装目录内的 `opencannbot\` 子目录；如该目录已存在有效仓库则直接复用，不会重复下载。

所有参数均可选：

```powershell
.\install.ps1 `
  -InstallDir "C:\Tools\OpenCANNBotProxyCenter"
```

使用已有的 opencannbot 仓库（不下载、不修改你的 Git 仓库）：

```powershell
.\install.ps1 `
  -RepoDir "C:\Projects\opencannbot" `
  -InstallDir "C:\Tools\OpenCANNBotProxyCenter"
```

也可以使用 CMD（参数同样全部可选）：

```cmd
install.cmd
install.cmd -InstallDir "C:\Tools\OpenCANNBotProxyCenter"
install.cmd -RepoDir "C:\Projects\opencannbot" -Overwrite
```

省略 `-InstallDir` 时，默认安装到：

```text
%LOCALAPPDATA%\OpenCANNBotProxyCenter
```

覆盖已有安装时使用 `-Overwrite`。该操作会停止旧代理进程并覆盖程序文件，但会保留 `%USERPROFILE%\.cannbot\proxy-center` 下的配置、VK 和日志：

```powershell
.\install.ps1 `
  -InstallDir "C:\Tools\OpenCANNBotProxyCenter" `
  -Overwrite
```

## 静默启动

双击：

```text
C:\Tools\OpenCANNBotProxyCenter\start-proxy-center.vbs
```

或者：

```powershell
wscript.exe "C:\Tools\OpenCANNBotProxyCenter\start-proxy-center.vbs"
```

浏览器打开：

```text
http://127.0.0.1:8788
```

## 多实例示例

```text
实例 1
端口：8766
模型：glm-5.3
用途：Claude Code

实例 2
端口：8767
模型：glm-5.3-flash
用途：桌面助手
```

两个应用分别填写不同的 Base URL：

```text
http://127.0.0.1:8766
http://127.0.0.1:8767
```

每个实例的模型由代理层固定，不依赖客户端是否正确保存模型设置。

## 模型配置

打开代理中心左侧的“模型配置”页面即可维护模型列表。模型列表会从 OpenCANNBot 网关自动同步，不需要再手工维护模型名称：

1. 代理中心每次启动时会在后台自动同步一次；也可以点击“从网关同步模型”立即刷新。
2. 同步会使用实例中已配置的 VK（没有实例 VK 时回退到全局 VK `%USERPROFILE%\.cannbot\vk`）调用网关的模型列表接口，只加入状态为启用的模型，并自动写入模型显示名称。
3. 如果多个实例配置了不同 VK，会合并所有 VK 能看到的模型；只要有任一 VK 查询失败，本次同步就视为不完整，不会删除网关未返回的模型。
4. 同步得到的模型会立即出现在实例配置和首页“快速切换模型”的下拉列表中，选择后重启实例即可生效。
5. 网关未提供的模型（包括历史手动添加的模型）会在同步成功后自动从列表移除；仍被实例使用的模型会保留，避免实例配置失效。
6. 被实例使用的失效模型会在页面标记“网关列表未包含”，切换实例模型后可点击“清理未列出的模型”手动清掉。清理操作有独立确认页且只删除确认页列出的模型；同步不完整或缓存超过 24 小时时页面会给出提示/拦截。
7. 手动删除的“网关同步”模型如果仍在网关列表中，下次同步时会重新出现；要长期隐藏某个模型，需要网关侧停用它。
8. 同步失败（例如离线、VK 无权限）时保留现有模型列表，并把最近一次失败时间和原因显示在“模型配置”页面顶部。

内置模型（GLM 5.3、GLM 5.3 Flash、DeepSeek-V4-Flash、DeepSeek-V4-Pro）作为网关不可用时的后备项保留，不可删除。

模型必须同时获得当前 VK 的网关授权，页面保存配置不会绕过网关权限。同步失败时（例如离线、VK 无权限）会保留现有模型列表，并在页面提示失败原因。

代理中心从关联仓库的 `cannbot-claude-proxy.py` 读取 `GATEWAY_URL`。模型列表接口使用该网关的同源地址，认证优先采用同源的 `AUTH_URL`，否则使用默认认证路径。请在“全局设置”中选择有效的 OpenCANNBot 仓库后同步模型。

## Claude Code 接入

每个实例会生成独立启动脚本：

```text
%USERPROFILE%\.cannbot\proxy-center\instances\<实例ID>\claude-code.ps1
%USERPROFILE%\.cannbot\proxy-center\instances\<实例ID>\claude-code.cmd
```

PowerShell 示例：

```powershell
& "$HOME\.cannbot\proxy-center\instances\proxy-default\claude-code.ps1"
```

其他支持 Anthropic Messages API 和自定义 Base URL 的应用，也可以直接使用实例地址和对应 VK。

## 通过 cc-switch 管理 Claude Code / Codex / OpenCode

[cc-switch](https://github.com/farion1231/cc-switch) 是一个开源桌面应用，用于统一管理 Claude Code、Codex、OpenCode、Gemini CLI 等工具的 API 供应商配置，支持一键切换、系统托盘快速切换和用量统计。

Proxy Center 的每个实例都是一个标准的 Anthropic Messages API 端点，可以直接作为 cc-switch 的自定义供应商接入：

1. 在实例配置页查看实例端点（`http://127.0.0.1:<端口>`）和 VK
2. 打开 cc-switch，点击右上角 **+** 添加供应商
3. 创建自定义配置：
   - 名称：如 `OpenCANNBot 实例 1`
   - Base URL（端点）：`http://127.0.0.1:8766`
   - API Key：填写该实例的 VK
4. 保存后在供应商列表中点击「启用」即可切换

各应用的接入方式：

| 应用 | 接入方式 |
|------|----------|
| Claude Code | 直接接入，实例原生提供 Anthropic Messages API；cc-switch 对 Claude Code 支持热切换，无需重启 |
| Codex | Codex 原生走 OpenAI Responses 协议，需在 cc-switch 中开启本地代理（格式转换）后接入 |
| OpenCode | 添加 Anthropic 类型自定义供应商（npm 填 `@ai-sdk/anthropic`），Base URL 和 API Key 同上 |

说明：

- 实例的模型由端口固定路由，cc-switch 供应商配置里的模型名不影响实际路由，生效的始终是实例所选模型
- 除 Claude Code 外，切换供应商后需重启对应 CLI 终端才会生效（以 cc-switch 文档为准）
- 若同时使用 cc-switch 的本地代理，注意其监听端口不要与实例端口（默认 8766–8768）或控制面板端口 8788 冲突

## 数据位置

```text
%USERPROFILE%\.cannbot\proxy-center\config.json
%USERPROFILE%\.cannbot\proxy-center\models-cache.json
%USERPROFILE%\.cannbot\proxy-center\panel.log
%USERPROFILE%\.cannbot\proxy-center\instances\<实例ID>\vk
%USERPROFILE%\.cannbot\proxy-center\instances\<实例ID>\pid.json
%USERPROFILE%\.cannbot\proxy-center\instances\<实例ID>\proxy.log
```

不同实例使用隔离的运行 HOME，避免代理源码中的 `~/.cannbot/vk` 在多个进程之间互相覆盖。

## 从旧版升级

直接使用相同安装目录执行 v6 的 `install.ps1` 即可覆盖程序文件。

首次运行时，如果尚未存在 v6 配置，程序会尝试迁移旧版：

```text
%USERPROFILE%\.cannbot\claude-panel.json
%USERPROFILE%\.cannbot\vk
```

迁移后会生成一个默认实例。

## 卸载

保留配置、VK 和日志：

```powershell
.\uninstall.ps1 "C:\Tools\OpenCANNBotProxyCenter"
```

连同代理中心的所有实例配置、VK 和日志一起删除：

```powershell
.\uninstall.ps1 "C:\Tools\OpenCANNBotProxyCenter" -RemoveConfig
```

卸载会删除整个安装目录，包括安装时自动下载到其中的 `opencannbot\` 仓库。使用 `-RepoDir` 指定的外部仓库不受影响。

## 特别感谢

本项目的核心代理能力来自关联项目 [OpenCANNBot](https://github.com/BadFatCat0919/opencannbot)，Proxy Center 在其之上提供 Windows 原生的多实例管理。感谢 OpenCANNBot 项目的所有贡献者：

**仓库地址：<https://github.com/BadFatCat0919/opencannbot>**

## 许可证

本项目基于 [MIT License](LICENSE) 发布。
