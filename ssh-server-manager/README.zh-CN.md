# SSH Server Manager

**面向人类与 AI 智能体的本地优先 SSH 主机与凭据管理器。**
一个 CLI（`serverctl`）加一个仅监听本机回环的 Web 界面，用来添加、导入、测试、连接你的服务器——所有密码和密钥口令都存放在操作系统原生凭据保险库中，绝不落盘为明文。

[English](README.md) · [文档](docs/) · [安全模型](docs/security.md)

> 🤖 **在用 AI 智能体？** 一句话即可把 skill 部署到本机所有智能体——依赖安装、目录链接、健康体检一步到位。见[智能体部署指南](docs/ai-agents.md)。

---

## 为什么做这个

大多数 SSH 管理工具让你在「方便」和「数据自主」之间二选一：

- **云同步客户端**（订阅制 GUI）把你的凭据放在别人的基础设施上；
- **裸写 `~/.ssh/config`** 虽然可控，但完全不管密码，也没有保险库，AI 编程智能体很容易从中泄露秘密；
- **`sshpass` 一类包装** 把密码放进文件、环境变量或 shell 历史。

SSH Server Manager 从架构上避开这些问题：

| 特性 | 实现方式 |
|---|---|
| 秘密永不以明文落盘 | 通过 `keyring` 使用 macOS 钥匙串 / Windows 凭据管理器 / Linux Secret Service；不安全后端直接报错拒绝 |
| 绝不改写你的 `~/.ssh/config` | 单独渲染一份托管配置，与原配置并存加载 |
| Web UI 无法从网络访问 | 仅绑定 127.0.0.1，一次性启动令牌，CSRF + Origin 校验，严格 CSP |
| 查看已存秘密需要重新认证 | WebAuthn 通行密钥（Touch ID / Windows Hello）或 Argon2id 主密码；授权单次有效、30 秒过期 |
| AI 智能体可安全驱动 | 所有命令支持 `--json`；SSH 认证通过 AskPass 完成，秘密不会出现在参数、环境变量、日志或模型上下文中 |
| 主机密钥始终校验 | 代码与规范双重保证，绝不弱化 `StrictHostKeyChecking` |

## 快速开始

```bash
git clone https://github.com/xiayh0107/servers-connect.git
cd servers-connect/ssh-server-manager
./scripts/bootstrap          # Windows 用 scripts\bootstrap.cmd
./scripts/serverctl doctor   # 检查 ssh、保险库后端和依赖
```

添加服务器并连接：

```bash
./scripts/serverctl credential add-password work-password   # 本地隐藏输入，存入系统保险库
./scripts/serverctl server add web1 --hostname web1.example.com --username deploy --credential work-password
./scripts/serverctl server test web1
./scripts/serverctl connect web1
```

或者把已有配置一键导入：

```bash
./scripts/serverctl server import          # 仅预览
./scripts/serverctl server import --apply
```

想用图形界面？`./scripts/serverctl ui` 会用一次性令牌 URL 在浏览器中打开本地管理界面。

## 功能

- **连接档案** —— 别名、主机、端口、用户、备注、有序 ProxyJump 跳板链（带环路检测）。
- **三种凭据类型** —— 保险库密码、私钥（可选保险库口令）、ssh-agent/OpenSSH 默认。凭据可被多台服务器复用，被引用时禁止删除。
- **OpenSSH 导入** —— 解析 `~/.ssh/config`（跟随 `Include`），用 `ssh -G` 解析每个字面量别名，先预览后应用。
- **托管配置渲染** —— 原子写入、仅本用户可读的 `~/.ssh/ssh-server-manager.conf`；原配置始终最后加载，不影响既有默认值。
- **连接测试** —— `server test` 报告延迟和分类错误码（认证失败 / 主机密钥不受信 / 超时 / DNS 失败等），并记录历史。
- **远程执行** —— `serverctl exec 别名 -- 命令 参数`；`--shell` 跑管道和复合命令，`--stdin`/`--stdin-binary` 流式传输文件，`--reuse N` 复用连接（macOS/Linux），`--json` 输出机器可读结果。
- **本地 Web UI** —— 管理服务器与凭据、测试连接、导入配置；查看已存秘密前需通行密钥或主密码重新认证。
- **诊断** —— `serverctl doctor` 检查 ssh 可用性与版本、保险库后端安全性、数据库与配置路径、Python 依赖。

## 平台支持

| | macOS | Linux | Windows |
|---|---|---|---|
| CLI + Web UI | ✅ | ✅ | ✅ |
| 凭据保险库 | 钥匙串 | Secret Service（gnome-keyring / KWallet / KeePassXC） | 凭据管理器 |
| 秘密查看重认证 | Touch ID 通行密钥或主密码 | 通行密钥或主密码 | Windows Hello 通行密钥或主密码 |
| 连接复用（`--reuse`） | ✅ | ✅ | —（ControlMaster 依赖 Unix 套接字，会打印警告） |

细节与无桌面服务器场景见 [docs/platforms.md](docs/platforms.md)。CI 在三个平台上跑完整测试。

## 面向 AI 智能体

本项目同时以 Agent Skill（[SKILL.md](SKILL.md)）形式发布，Claude Code、Codex 等智能体可以安全地管理服务器：智能体获得结构化 JSON 输出和连接错误分类，而 AskPass 架构从设计上保证秘密不进入模型上下文。

一句话部署 skill：

```bash
curl -fsSL https://raw.githubusercontent.com/xiayh0107/servers-connect/main/install.sh | sh
```

自动把 skill 链接进检测到的所有智能体（`~/.claude/skills`、`~/.codex/skills` 等）、安装依赖并运行 `doctor` 体检。Windows 用 `install.ps1`。详见 [docs/ai-agents.md](docs/ai-agents.md)。

## 文档

安装（[docs/installation.md](docs/installation.md)）、快速上手（[docs/quickstart.md](docs/quickstart.md)）、CLI 参考（[docs/cli.md](docs/cli.md)）、Web UI（[docs/web-ui.md](docs/web-ui.md)）、安全模型（[docs/security.md](docs/security.md)）、平台说明（[docs/platforms.md](docs/platforms.md)）、AI 集成（[docs/ai-agents.md](docs/ai-agents.md)）、常见问题（[docs/faq.md](docs/faq.md)）。

## 许可证

[MIT](../LICENSE)
