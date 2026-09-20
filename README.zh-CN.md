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

自动监测会在每个新窗口继续运行。原生历史或换窗确认还没到齐时，工具会等待，条件满足后自行继续。剩余空间不足时可能暂缓下一次模型请求，但这不代表任务完成，也不会关闭自动监测。

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

### 5. 自动换窗为什么在等待？
宿主可能正在等一条原生历史写完、工具结果落盘，或清屏与交接的确认到达。它会持续检查，条件满足后自行继续；确认迟到不会永久关闭自动换窗，结果未知的命令也不会被盲目重发。

History/Notes 始终可用。已核验的剩余空间足够时，工作可以继续；接近上限时，会暂缓下一次模型请求，已完成结果和已提交输入仍留在原生历史中。用 `claude-context tui-status --context-id <context-id>` 查看当前阶段和等待原因。请求被接受不等于新窗口已经打开。

`PostToolBatch hook stopped continuation` 配合“正在自动切换上下文”表示旧回合正在让位给新窗口，不代表任务完成。子代理消息和后台任务通知仍可在 History 中读取；它们和交接都不提供新的用户授权。

### 6. 终端中断后，怎样继续？
按原生方式恢复即可：用 `cclaude --resume <session-id>` 启动，或在受管的 `cclaude` 会话内使用 `/resume`。自动监测会重新绑定选中的原生会话和实际用量。若终端仍在、只是监测进程退出，真实原生 hook 会恢复这个监测进程，不需要另跑修复命令。

`tui-attach` 只重新连接已有终端。`tui-status` 会检查实际控制器、独占锁、tmux 会话和控制 socket；连上终端本身不代表监测已经正常。历史不可读或投递尚未确认时会明确说明，不靠重跑已完成工作或接管其他会话来“修复”。

若清屏已确认而交接尚未投递，恢复不会再次清屏：新窗口尚未开始对话时，只继续那一次待发交接；新对话已经开始时，只恢复监测，不再插入旧交接。被延后的输入会继续保留，无法确认投递结果的消息仍不会重发。

交互式 `cclaude --resume <session-id>`（或 `-r`）会先检查该受管会话是否仍在运行。只有一个可确认的存活实例时，直接连接它，不另开原生进程或控制器，也不升级已经运行的旧进程。若命令还带有新提示、模型或权限等启动选项，则明确拒绝，避免静默忽略选项或重复投递输入。

发现多个存活实例、身份无法核验或上次启动结果不明时，不自动挑选、终止或重开。当前目录有存活实例时，`--continue`、无 ID 的 `--resume` 或会话名称也不会被猜测成某个实例；请改用完整 session ID 或 `tui-attach`。显式 `--fork-session` 仍按原生语义创建独立会话。

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
