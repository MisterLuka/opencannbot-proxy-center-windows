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
  - `glm-5.2`
  - `qwen3.7-max`
  - `deepseek-v4-flash-0731`（显示名：DeepSeek-V4-Flash-0731）
- 页面提供“模型配置”，可添加、修改显示名称和删除自定义模型
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
模型：glm-5.2
用途：Claude Code

实例 2
端口：8767
模型：qwen3.7-max
用途：桌面助手
```

两个应用分别填写不同的 Base URL：

```text
http://127.0.0.1:8766
http://127.0.0.1:8767
```

每个实例的模型由代理层固定，不依赖客户端是否正确保存模型设置。

## 模型配置

打开代理中心左侧的“模型配置”页面即可维护模型列表。内置模型包含 GLM 5.2、Qwen 3.7 Max 和 DeepSeek-V4-Flash-0731；自定义模型时填写网关实际接受的模型 ID，模型显示名称仅用于页面展示。

模型必须同时获得当前 VK 的网关授权，页面保存配置不会绕过网关权限。新增或切换模型后，请在实例配置中选择该模型并重启实例。

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

## 数据位置

```text
%USERPROFILE%\.cannbot\proxy-center\config.json
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
