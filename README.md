# OpenCANNBot Proxy Center v6

在 Windows 上用浏览器管理最多 3 个 OpenCANNBot 代理实例。每个实例使用独立端口和 VK，并将客户端请求固定路由到所选模型。

## 功能

- 在控制面板查看实例状态、复制连接地址、启停实例和查看运行日志。
- 每个实例独立保存 VK、运行目录和 PID。控制面板和受管理的实例均监听 `127.0.0.1`。
- 在首页快速切换模型。后台任务页显示启动、重启、健康检查进度和实时日志。
- 自动从网关同步可用模型，也可在「模型配置」中添加模型和管理显示名称。

## 安装

需要 Windows 和 Python 3.8 或更高版本。确保终端中可以运行 `py -3` 或 `python`。代理中心使用 Python 标准库。

[下载本项目 ZIP](https://github.com/MisterLuka/opencannbot-proxy-center-windows/archive/refs/heads/main.zip) 并解压，在项目目录运行以下命令。也可在 PowerShell 中运行 `.\install.ps1`。

```powershell
.\install.cmd
```

默认安装目录为 `%LOCALAPPDATA%\OpenCANNBotProxyCenter`。安装器会在该目录下的 `opencannbot` 子目录准备关联项目，已有有效仓库时直接复用。有 Git 时使用 `git clone`，没有 Git 时下载 ZIP。[OpenCANNBot 上游项目](https://github.com/BadFatCat0919/opencannbot)

使用现成的 OpenCANNBot 仓库时，将 `-RepoDir` 指向包含 `cannbot-claude-proxy.py` 的目录。可用 `-InstallDir` 选择程序安装目录。

```powershell
.\install.ps1 -RepoDir "C:\Projects\opencannbot" -InstallDir "C:\Tools\OpenCANNBotProxyCenter"
```

## 静默启动

安装完成后，双击安装目录中的 `start-proxy-center.vbs`。也可运行同目录的 `start-proxy-center.cmd`。控制面板地址为 [http://127.0.0.1:8788](http://127.0.0.1:8788)。

### 首次配置

1. 全新安装会创建「默认代理」。在「代理实例」打开该实例的「配置」，填入有网关权限的 `CANNBOT VK`。
2. 如果目标模型还未出现在下拉列表，先点击「仅保存」，再前往「模型配置」点击「从网关同步模型」。
3. 在实例配置中选择固定模型，点击「保存并重启」，等待健康检查通过。
4. 在客户端填写实例卡片上的 Base URL，使用该实例的 VK 作为 API Key。默认实例的地址为 `http://127.0.0.1:8766`。

安装时已登记关联仓库。移动或更换仓库后，在「全局设置」中更新目录，再分别重启需要使用新仓库的实例。

页面打不开时，在安装目录运行 `.\proxy-center.cmd` 查看启动输出。「完全退出」会停止代理中心管理的全部实例并关闭控制面板服务。

## 多实例示例

| 用途 | Base URL | 固定模型 |
| --- | --- | --- |
| Claude Code | `http://127.0.0.1:8766` | `glm-5.3` |
| 桌面助手 | `http://127.0.0.1:8767` | `glm-5.3-flash` |

实例的建议端口为 `8766`、`8767`、`8768`。各客户端使用对应实例的 Base URL 和 VK。代理会将请求中的模型替换为该实例配置的固定模型。

在首页的「快速切换模型」中选择模型，再点击「应用并重启」或「应用并启动」即可生效。

## 模型配置

代理中心每次启动时会尝试同步一次。也可以在「模型配置」点击「从网关同步模型」。同步会收集各实例的 VK 及可用的旧版全局 VK（`%USERPROFILE%\.cannbot\vk`），去重后逐个查询并合并返回的已启用模型。每个实例只能调用其 VK 已获授权的模型。

| 同步结果 | 模型列表处理 |
| --- | --- |
| 全部 VK 查询成功 | 加入或更新网关模型。移除网关未返回且未被实例使用的非内置模型，包括符合条件的手动条目。 |
| 部分 VK 查询成功 | 合并成功返回的模型，保留现有条目。 |
| 同步失败 | 保留现有列表，并在「模型配置」显示失败时间和原因。 |

已同步的模型会出现在实例配置和首页的模型下拉列表里。内置模型始终可选，分别为 `glm-5.3`、`glm-5.3-flash`、`deepseek-v4-flash` 和 `deepseek-v4-pro`。

手动添加模型时，使用网关实际接受的模型 ID。若删除了仍在网关列表中的同步模型，下次同步会重新加入它。

「清理未列出的模型」用于确认并删除网关未列出、当前也没有实例使用的手动条目。执行前需要有一次覆盖全部 VK 的同步结果。缓存超过 24 小时时，页面会提示先重新同步。

网关地址从关联仓库的 `cannbot-claude-proxy.py` 中读取 `GATEWAY_URL`。模型列表与默认认证使用同源地址。上游定义了同源的 `AUTH_URL` 时，优先使用该认证地址。

## Claude Code 接入

每个实例都会生成 `claude-code.ps1` 和 `claude-code.cmd`。启动脚本会为 Claude Code 设置实例的 Base URL、VK 和模型。以默认实例为例，可在 PowerShell 中运行以下命令。

```powershell
& "$env:USERPROFILE\.cannbot\proxy-center\instances\proxy-default\claude-code.ps1"
```

其他实例的脚本位于 `%USERPROFILE%\.cannbot\proxy-center\instances\<实例ID>\`。支持 Anthropic Messages API 的其他客户端也可以使用实例地址和 VK。

## 通过 cc-switch 管理 Claude Code / Codex / OpenCode

在 CC Switch 中选择客户端并添加自定义供应商。API Key 填该实例的 VK，模型选择对应实例的固定模型。以下示例使用默认实例的端口 `8766`。[CC Switch 项目](https://github.com/farion1231/cc-switch)

| 客户端 | 供应商配置 |
| --- | --- |
| Claude Code | 选择 Anthropic Messages，Base URL 填 `http://127.0.0.1:8766`，保存后启用供应商。 |
| Codex | 在供应商高级选项中把上游格式设为 Anthropic Messages，Base URL 填 `http://127.0.0.1:8766`，并为 Codex 启用本地路由以转换请求格式。 |
| OpenCode | 使用 `@ai-sdk/anthropic`，Base URL 填 `http://127.0.0.1:8766/v1`。添加供应商后，在 OpenCode 中选择该供应商的模型。 |

使用 CC Switch 本地路由时，为它选用与面板和各实例不同的监听端口。[添加供应商](https://github.com/farion1231/cc-switch/blob/main/docs/user-manual/zh/2-providers/2.1-add.md) · [本地路由](https://github.com/farion1231/cc-switch/blob/main/docs/user-manual/zh/4-proxy/4.1-service.md)

## 数据位置

本地数据位于 `%USERPROFILE%\.cannbot\proxy-center`。

| 相对路径 | 内容 |
| --- | --- |
| `config.json` | 关联仓库路径、实例配置和模型列表 |
| `models-cache.json` | 最近的网关同步结果 |
| `panel.log` | 控制面板日志 |
| `instances\<实例ID>\vk` | 实例 VK |
| `instances\<实例ID>\pid.json` | 进程记录 |
| `instances\<实例ID>\proxy.log` | 实例运行日志 |
| `instances\<实例ID>\runtime-home\` | 实例独立的运行目录 |

各实例的运行目录相互隔离，使上游代理分别读取自己的凭据文件。

## 从旧版升级

在新版源码目录运行 `.\install.ps1 -Overwrite`。原安装使用自定义目录或外部仓库时，通过 `-InstallDir` 或 `-RepoDir` 传入原路径。安装器会尝试停止旧进程并替换程序文件，保留 `%USERPROFILE%\.cannbot\proxy-center` 下的数据。

如果还没有 v6 配置，程序会尝试读取 `%USERPROFILE%\.cannbot\claude-panel.json`，并将旧版配置和可用的全局 VK 迁移到默认实例。

## 卸载

在源码目录运行以下命令可卸载默认目录中的程序，并保留实例配置、VK 和日志。

```powershell
.\uninstall.ps1
```

自定义安装目录通过 `-InstallDir` 指定。需要同时清除代理中心的本地数据时，使用 `-RemoveConfig`。

```powershell
.\uninstall.ps1 -InstallDir "C:\Tools\OpenCANNBotProxyCenter" -RemoveConfig
```

卸载会删除整个安装目录，包括自动下载到其中的关联仓库。位于安装目录之外的关联仓库会保留。

## 特别感谢

本项目的核心代理能力来自关联项目 [OpenCANNBot](https://github.com/BadFatCat0919/opencannbot)，Proxy Center 在其之上提供 Windows 原生的多实例管理。感谢 OpenCANNBot 项目的所有贡献者。

## 许可证

本项目基于 [MIT License](LICENSE) 发布。
