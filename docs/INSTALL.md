# 安装、更新和恢复

## 一次性安装

1. 使用支持本地插件的 Windows Codex 桌面版，登录并加载原生模型列表。
2. 安装 CPython 3.13 x64。无需管理员权限运行本项目。
3. 下载并保留源码目录，运行 `py -3.13 scripts/install.py --check`。
4. 检查显示的引擎、运行环境和 Codex 主目录，再运行不带 `--check` 的安装命令。
5. 重开桌面客户端，进入模型路由面板，配置自己的提供商并启用路由。

脚本创建 `%LOCALAPPDATA%\CodexRouter\runtimes\1.2.0`，从 Python 包索引安装
`requirements.lock.txt` 中固定版本的二进制 wheels，再调用引擎的 `marketplace/add`、
`plugin/install` 和 `plugin/read`。只有读取结果确认已安装、已启用才报告成功。
它不会请求订阅凭据，也不会在安装阶段更改模型路由字段。

默认引擎发现位置为 `%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe`。
如果存在多份引擎，选择最近更新的一份；需要指定版本时使用 `--codex`。

## 离线依赖与手动安装

在同平台的联网环境准备依赖：

```powershell
py -3.13 -m pip download --only-binary=:all: -r requirements.lock.txt -d wheels
py -3.13 scripts/install.py --wheelhouse wheels
```

`--runtime-only` 只准备依赖，不注册插件。随后可使用桌面版支持的本地 marketplace
安装路径，或兼容的 `codex plugin marketplace add .` 注册本仓库，再在 Codex UI 安装。
仓库 marketplace 位于 `.agents/plugins/marketplace.json`，公开插件 ID 为
`codex-router@codex-model-router`。

安装器的 `--codex-home`、`--runtime-dir` 用于隔离验收或受管理部署。
使用自定义运行环境时，在启动 Codex 前设置 `CODEX_ROUTER_RUNTIME`，使插件启动器使用相同目录。
普通安装不要指定这两个参数。

## 更新

先等待当前任务结束。关闭路由并停用旧插件，再更新源码、重新运行安装器，重开客户端后启用。
不同版本使用不同运行环境，不在正在运行的 Python 环境内替换依赖。
运行数据继续位于原目录；不要复制其他 Windows 账户的 DPAPI 密文。

## 恢复与卸载

面板中关闭“启用路由”，然后重开客户端，使新进程恢复原生连接。
接着在 Codex 插件管理页卸载插件。

面板无法打开时，从已下载的源码目录运行：

```powershell
& "$env:LOCALAPPDATA\CodexRouter\runtimes\1.2.0\Scripts\python.exe" main.py restore
```

该命令按接入记录仅恢复两个路由字段。配置冲突时会报错，不覆盖他人修改。
不要先删除运行数据：其中的 `integration.json` 和 `backups/` 用于恢复。
安装器不删除用户运行环境和数据；清理请在恢复、卸载和退出客户端后自行进行。

## 常见问题

- **插件运行环境缺失**：重新运行安装脚本；启动器不会在 MCP 握手时联网安装依赖。
- **没有原生模型缓存**：在客户端登录并打开一次模型菜单。
- **地址冲突**：端口 18789 已被其他程序占用时，插件明确失败；不自动关闭其他程序。
- **新模型未显示**：保存后重开客户端。模型目录由原客户端缓存。
- **第三方失败**：确认协议、模型 ID、SSE 和工具调用支持，再使用面板连接测试。
- **长会话压缩失败**：Chat Completions 路径需要新会话；不会伪造压缩结果。
