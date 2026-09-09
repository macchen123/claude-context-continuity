<div align="center">

# Claude Context Continuity

<img src="docs/images/hero.svg" alt="概念插图：原生 Claude Code 的上下文窗口轮换到新上下文，工作区仍保持连接" width="760" />

**把 Codex 式上下文接力带到原生 Claude Code。**

对话快满了，就换个干净上下文接着做。进度留在笔记里，细节随时回原始记录查。

[English](README.md) · [Repository](https://github.com/macchen123/claude-context-continuity) · [许可证](LICENSE)

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![MIT License](https://img.shields.io/badge/License-MIT-2ea44f)](LICENSE)
[![Platform macOS and Linux](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-555555)](#platform)
[![LINUX DO](https://img.shields.io/badge/LINUX%20DO-社区认可-0d9488)](https://linux.do)

<sub>概念插图，不是终端截图。</sub>

</div>

[为什么](#why) · [工作方式](#how-it-works) · [三步开始](#quick-start) · [日常使用](#everyday-use) · [定时任务修复](#cron-compat) · [进阶功能](#advanced) · [隐私](#privacy) · [FAQ](#faq) · [参与贡献](#contributing) · [致谢](#acknowledgments)

<a id="why"></a>
## 为什么开发这个工具？

Claude Code 原生自带 `/compact` 来压缩长会话。但面对大型长任务时，反复依赖摘要压缩往往会遇到以下问题：

- **关键细节被吞**：最初强调的硬性约束、边界条件、架构细节，很容易在两三轮压缩后被省略。
- **越跑越偏（错上加错）**：一旦关键上下文丢失，模型后半段容易放飞自我，开发出的东西偏离预期，需要花大量时间纠偏甚至推倒重来。
- **打断思路**：压缩等待耗时，反复重新向模型交代背景更耗时。

**我们的思路：借鉴 Codex 的上下文管理方案。**  
监控 token 预算，快到上限时平滑切换到一个干净的新窗口；同时将工作进度自动整理交接，旧会话的原始历史全部留在本地索引中，新会话随时能精确查回——**用“换窗接力”代替“有损压缩”**。

它完全基于原生 Claude Code，不另造轮子，不替换原有的模型与工具体系。设计受 Codex 的 [新上下文机制](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/tools/handlers/new_context_window_spec.rs) 与 [History/Notes 工具](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/ext/history-notes/src/tools.rs) 启发，属于独立的社区开源项目。

---

<a id="how-it-works"></a>
## 工作方式

<img src="docs/images/context-flow.svg" alt="概念流程图：Session 1 留下简短交接，新上下文在同一工作区继续，原始会话与 Notes 可按需读取" width="100%" />

<p align="center"><em>概念流程，不是终端截图。</em></p>

1. **提前准备**：对话快满时，提前留好交接空间，不等上下文耗尽才停下来。
2. **换窗不停工**：后台任务继续跑，换窗时发来的消息和还没提交的草稿也会保留，不用重新来过。
3. **细节随时找**：新窗口带上简短交接，旧对话原文留在本地。需要哪段细节，再回去查哪段，不只依赖摘要。

| 使用 `/compact` 压缩 | 使用上下文接力换窗 |
| --- | --- |
| 通过摘要压缩当前会话，腾出空间 | 切换到干净的新窗口，重置上下文空间 |
| 摘要有损，容易丢失早期关键约束 | 新窗口保留必要交接，旧会话原始历史随时可查 |
| 反复压缩后容易越写越偏 | 始终处于低负载上下文，输出更精准稳定 |

---

<a id="quick-start"></a>
## 三步开始

### 1. 安装环境

前置要求：Python 3.10+、Git、`tmux`，以及原生 Claude Code **2.1.266 或更新版本**。可以用 `claude --version` 查看当前版本。

```sh
python3 -m pip install "git+https://github.com/macchen123/claude-context-continuity.git"
```

*说明：安装不会修改 Claude Code 的全局配置，所有 hook 与增强命令均通过会话本地的插件机制动态加载。*

### 2. 设置上下文预算

根据你所用宿主或模型的上下文大小设置环境变量（示例为 200k）：

```sh
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=200000
```

### 3. 启动

用 `cclaude` 代替平时的 `claude` 即可：

```sh
cclaude
```

*(可选：如果你想在这次会话中完全禁用原生 compact，可加参数 `DISABLE_COMPACT=1 cclaude`)*

---

<a id="everyday-use"></a>
## 日常使用

平常怎么用 Claude Code，现在依然怎么用。

对话快满时，工具会自动换个新窗口，接着做当前的事。后台任务照常运行，换窗时发来的消息会留到新窗口处理，也不会把尚未提交的草稿发出去。

如果暂时不能自动换窗，终端会提示原因；原来的工作和历史查询仍可继续。

### 主动换窗：`/renew`

如果你阶段性完成了一个大功能，想主动换个干净窗口轻装上阵，直接在会话中输入：

```text
/renew
```

模型会先梳理一份简要交接笔记，然后安全换到新窗口。如果同名命令被其他插件占用，可以使用完整命令 `/cclaude:renew`。

---

<a id="cron-compat"></a>
## 官方定时任务（CronCreate）兼容修复

在官方 Claude Code 中存在一个已知 bug：**在终端里执行 `/clear` 后，新创建的持久定时任务（`CronCreate(durable=true)`）会丢失会话绑定，导致任务虽写入磁盘却不会被调度触发。**

由于换窗机制涉及会话切换，为了避免该 bug 影响定时任务，本工具默认开启了一个轻量的绑定兼容层：
- 自动修正新定时任务的调度归属，确保其正常触发。
- **绝不篡改官方二进制**，任务仍保存在原生的 `.claude/scheduled_tasks.json` 中，原生的 `CronList` / `CronDelete` 完全不受影响。

**控制与回滚命令**：
- **关闭此修复**（若后续官方已修复该 bug）：
  ```sh
  CCLAUDE_DURABLE_CRON_COMPAT=off cclaude
  ```
- **查看修复状态**：
  ```sh
  claude-context cron-compat status --context-id "$CLAUDE_CONTINUITY_ID"
  ```
- **一键回滚撤销**（恢复任务原貌）：
  ```sh
  claude-context cron-compat restore --context-id "<context-id>"
  ```

---

<a id="advanced"></a>
## 进阶功能：跨窗口历史检索与工作笔记

切换到新窗口后，如果需要查找旧会话里的具体细节，可以使用内置工具：

```sh
# 跨所有历史窗口搜索（基于本地 SQLite FTS5 全文索引，支持中英文检索）
claude-context history-search --query "选型决定" --recent-first

# 查看命中消息的精准原话
claude-context history --source "<路径>" --session-id "<id>" --message-id "<id>" --expected-sha256 "<hash>"

# 跨窗口工作笔记（Notes）管理
claude-context notes list
claude-context notes read plan
printf '%s' '阶段计划备忘' | claude-context notes write plan
```

---

<a id="privacy"></a>
## 本地数据与隐私

所有会话接力数据和历史索引保存在本地目录：

```text
${CLAUDE_CONFIG_DIR:-$HOME/.claude}/session-continuity/
```

- **纯本地保存**：本工具不向任何外部第三方服务器上传数据。
- **敏感信息脱敏**：写入本地索引时会自动过滤环境变量中的 API Key 与 Token。
- **模型交互**：当你在新窗口主动检索历史并送入上下文时，数据走你原有配置的模型接口，安全性与原生 Claude Code 完全一致。

---

<a id="platform"></a>
## 平台支持

| 环境 | 支持状态 |
| --- | --- |
| Python | 3.10+（已测试 3.12） |
| macOS & Linux | 完全支持（需系统自带或安装 `tmux`） |
| WSL | 基本可用（未单独做深度集成测试） |
| Windows 原生 | 暂不支持 |

---

<a id="faq"></a>
## 常见问题 (FAQ)

### 1. 它会覆盖或破坏原生的 `/compact` 吗？
不会。原生的 `/compact` 依然完好可用。如果你想手动 compact，随时可以执行，两者完全互不冲突。

### 2. 如果任务执行到一半，工具会强行换窗打断吗？
不会为了换窗终止或重跑后台任务。工具会等当前这一步工具调用结束，再换到新窗口，后台任务不用全部等完。换窗时发来的消息会留到新窗口处理，草稿也会保留。

### 3. 换到新窗口后，还能找到旧会话的细节吗？
可以。同一任务链下的所有历史窗口都会在本地建立 SQLite FTS5 全文索引，通过 `claude-context history-search` 可以跨窗口检索任何一轮对话的原始细节。

### 4. 它改变了原生 Claude Code 的模型或工具能力吗？
没有。你使用的仍然是原生 Claude Code，模型、系统提示词、MCP 工具以及权限系统均原封不动。

---

<a id="contributing"></a>
## 参与贡献与本地测试

欢迎通过 Issue 和 Pull Request 参与贡献。

从源码本地测试：
```sh
python3 -m pip install -e .
CLAUDE_CONFIG_DIR="$PWD/.test-claude-config" \
PYTHONPATH=src \
python3 -m unittest discover -s tests -v
```

---

<a id="acknowledgments"></a>
## 致谢

感谢 [LINUX DO](https://linux.do) 社区的支持与认可。

## 许可证

Claude Context Continuity 使用 [MIT License](LICENSE) 发布。
