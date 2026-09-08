<div align="center">

# Claude Context Continuity

<img src="docs/images/hero.svg" alt="概念插图：原生 Claude Code 的上下文窗口轮换到新上下文，工作区仍保持连接" width="760" />

**把 Codex 式上下文接力带到原生 Claude Code。**

对话快满了，就换个干净上下文接着做。进度留在笔记里，细节随时回原始记录查。

[English](README.md) · [Repository](https://github.com/macchen123/claude-context-continuity) · [许可证](LICENSE)

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![MIT License](https://img.shields.io/badge/License-MIT-2ea44f)](LICENSE)
[![Version 0.1.0](https://img.shields.io/badge/version-0.1.0-6f42c1)](https://github.com/macchen123/claude-context-continuity/releases/tag/v0.1.0)
[![Platform macOS and Linux](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-555555)](#platform)

<sub>概念插图，不是终端截图。</sub>

</div>

[为什么](#why) · [另一种节奏](#rhythm) · [三步开始](#quick-start) · [工作方式](#how-it-works) · [日常使用](#everyday-use) · [隐私](#privacy) · [FAQ](#faq) · [参与贡献](#contributing)

<a id="why"></a>
## 不想在长任务里反复依赖 `/compact`？

Claude Code 提供 `/compact` 来整理越来越长的对话。它能腾出空间，但长任务反复从摘要继续，也可能遇到这些麻烦：

- **细节被省略。** 某条准确约束、一次方案取舍，未必能完整留在摘要里。
- **背景又要解释一遍。** 遗漏的信息可能让后续工作偏离方向，需要纠正，甚至重做。
- **工作被打断。** 压缩和整理需要等待，恢复背景也会花时间。

我们想补上另一种选择：**像 Codex 那样，提前感知预算，适时换到干净上下文，再借助笔记和原始历史接着做，而不是只依赖一轮又一轮的摘要。** 这就是 Claude Context Continuity 的开发起点。

它保留你熟悉的原生 Claude Code，不另造一套编码助手。设计受 Codex 的 [新上下文机制](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/tools/handlers/new_context_window_spec.rs) 和 [History/Notes 工具](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/ext/history-notes/src/tools.rs) 启发，是独立社区项目，不代表官方，也不是 Codex 的完整移植。

<a id="rhythm"></a>
## 长会话的另一种节奏

| 使用 `/compact` | 使用上下文接力 |
| --- | --- |
| 通过摘要压缩当前对话，腾出空间。 | 切换到干净上下文，腾出空间。 |
| 当前上下文保留一份缩短后的记录。 | 新上下文带上简短交接，并能找到原始历史。 |
| 摘要遗漏的细节可能需要重新解释。 | 给新上下文一条直接查回早期细节的路径。 |

<a id="quick-start"></a>
## 三步开始

### 1. 准备环境并安装

**用户还需准备什么？**

| 前置条件 | 准备方式 |
| --- | --- |
| Python 3.10+ 和 pip | 安装 Python，或使用已有的 Python 环境 |
| Git | 下方 Git 地址安装方式需要；改装 Release wheel 则不需要 |
| 原生 Claude Code | 单独安装，完成登录或认证，确认 `claude` 能正常工作 |
| tmux | 通过系统包管理器安装；本包管理会话，你无需手动操作 tmux |
| 终端与 PATH | 使用 macOS/Linux 交互终端，确保 `claude`、`tmux` 及 Python 环境的命令目录在 PATH 中 |

```sh
python3 -m pip install "git+https://github.com/macchen123/claude-context-continuity.git@v0.1.0"
```

**pip 会安装什么？**

| 安装内容 | 用途 |
| --- | --- |
| `claude-context-continuity` Python 包 | 自动接力、预算观察、History 和 Notes |
| `apsw==3.53.4.0` | 提供 SQLite 3.53.4 与 FTS5，不替换系统 SQLite |
| `cclaude` 命令 | 启动带上下文接力的原生 Claude Code |
| `claude-context` 命令 | History 搜索、Notes 管理和换窗请求 |
| `/renew` 的实现 | 随包提供，启动受管 cclaude 会话时自动加载，无需另装 skill |

安装不会修改 Claude Code 全局配置；`/renew` 和相关 hooks 只通过会话本地 plugin 加载。

### 2. 设置上下文预算

```sh
# 仅为示例：请为你自己的原生宿主选择一个正数估计值。
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=200000
```

`200000` 是示例值，请按所用宿主的上下文容量设置。检测到更小的原生窗口时会采用较小值；尚无法确定预算时，原生会话照常运行，自动换窗暂不开启。

### 3. 像平常一样启动

```sh
cclaude
```

<details>
<summary>可选：为这次调用选择 no-compact 方式</summary>

如果原生宿主支持该设置，并且你有意在这次调用中采用该方式：

```sh
DISABLE_COMPACT=1 cclaude
```

这由你自行选择。本包会读取 no-compact 配置，但绝不会编辑全局设置，也不保证其他宿主如何处理 compact。设置可用时，它也会考虑 `autoCompactEnabled: false`。

</details>

<a id="how-it-works"></a>
## 工作方式

<img src="docs/images/context-flow.svg" alt="概念流程图：Session 1 留下简短交接，新上下文在同一工作区继续，原始会话与 Notes 可按需读取" width="100%" />

<p align="center"><em>概念流程，不是终端截图。</em></p>

1. **预留工作空间。** 在会话拥挤之前设置上下文估计。
2. **完成后再切换。** 工具只在当前操作结束且输入框为空时，请求 Claude Code 开启新上下文。若无法判断，它会让会话保持原样。
3. **接上原来的工作。** 新上下文得到简洁交接；同一个工作区、原始会话和工作 Notes 仍可使用。

<a id="everyday-use"></a>
## 日常使用

在通常启动交互式 `claude` 的位置改用 `cclaude` 即可。继续在同一个项目目录工作，并使用相同的原生工具与设置。

当选定预算接近时，工具会为当前工作完成后的干净切换保留空间。新上下文获得简短交接，而不是整段转录记录；工作区仍保持原位。

普通路径不需要你处理 session ID。若稍后需要某个准确的早期细节，原始会话仍可读取，Notes 也可以保存少量有用提醒。

### 用 `/renew` 提前换窗

在受管 `cclaude` 会话中输入：

```text
/renew
```

一个阶段完成，或想在达到预算阈值前换个干净上下文时，可以主动使用它。模型先整理简短交接，再调用现有控制器使用的同一个 `context-request`。它等待当前工作结算，不强制 `/clear`、不杀任务、不覆盖正在编辑的输入；请求已接受不等于换窗已经完成。

这个命令只通过会话本地 `cclaude` plugin 加载，不需要全局安装或额外配置；`cclaude` 插件命名空间由本包使用。`/renew` 是已核验原生 CLI 支持的短别名；如有其他命令占用同名，可使用 `/cclaude:renew`，不覆盖已有命令。手动调用沿用原生工具权限，可能产生正常的模型回合开销；自动预算观察不依赖调用这个命令。

<details>
<summary>进阶命令：查看会话、使用 Notes 或请求交接</summary>

以下命令均为可选。`history` 始终同时需要 `--source` 和 `--session-id`，并且选定来源的文件名必须与该 session ID 匹配。

```sh
# 从一份选定会话中读取人类指令。
claude-context history \
  --source "<session-jsonl-path>" \
  --session-id "<session-id>" \
  --instructions

# 检索该会话，或列出某个 context 已知的来源会话。
claude-context history \
  --source "<session-jsonl-path>" \
  --session-id "<session-id>" \
  --search "decision" \
  --page-size 5
claude-context history-windows --context-id "<context-id>"

# 读取或新建一条简短工作 Note。
claude-context notes list --context-id "<context-id>"
claude-context notes read handoff-1 --context-id "<context-id>"
printf '%s' '简短工作笔记。' | \
  claude-context notes write plan --context-id "<context-id>"

# 当前工作完成后，为已托管会话请求交接。
claude-context context-request \
  --context-id "$CLAUDE_CONTINUITY_ID" \
  --handoff "目标、已完成工作、剩余工作、约束和重要文件位置"
```

**v0.1.0 跨窗口 History：**`history-search` 使用本地增量 FTS5 缓存，统一搜索或浏览一个连续会话已知的窗口。整个任务期间均可调用，不只在交接时使用；它不扫描所有项目，也不保证模型必然自动找回正确细节。

```sh
# 搜索已知窗口；context ID 默认取 CLAUDE_CONTINUITY_ID。
claude-context history-search --query "决定" --role user --recent-first --page-size 5
# 浏览某个窗口的工具结果，或保留相同参数继续一页。
claude-context history-search --context-id "<context-id>" --window 0 --tool Bash
claude-context history-search --query "决定" --role user --recent-first --page-size 5 --cursor "<next_cursor>"
# 按搜索命中的准确原始记录哈希读回。
claude-context history --source "<source_path>" --session-id "<session_id>" \
  --message-id "<message_id>" --expected-sha256 "<sha256>"
```

还可按 `--session`、`--source-kind` 过滤；省略 `--query` 则有界浏览。普通未托管会话可使用 `history-search --source "<session-jsonl-path>" --session-id "<session-id>" --query "决定"`。来源变化时分页游标明确失效，需重新搜索。更新已有 Note 仍需它当前的 `--expected-sha256`。

`0.1.0` 版本锁定 `apsw==3.53.4.0`，自带 SQLite **3.53.4** 与 FTS5，不替换系统 SQLite。长词走 trigram 倒排索引，一、二字查询在 SQLite 所选缓存文本中扫描，仍保持准确匹配；无需 embedding 服务或检索专用模型。未变化来源复用缓存；变化来源由既有 History reader 解析，只更新变化的索引记录。

</details>

<a id="native-behavior"></a>
## 融入原生 Claude Code

普通路径就是在交互终端中使用 `cclaude`。它在原生 Claude Code 周围加入上下文连续性，而不替换其 prompt、模型、工具、权限或设置。

<details>
<summary>技术兼容性与 direct-native 行为</summary>

`cclaude` 会先检查本地 `claude --help` 的参数形状，再决定是否托管会话。该路由基于已观测的 Claude Code CLI `2.1.263`，并不承诺未来原生 CLI 都会提供相同的参数或工具。

只有普通交互式 `cclaude` TTY 会被自动化。print 模式、`--bg`/`--background`、`--cloud`、原生子命令、`--bare`、`--safe-mode`、非 TTY 调用和无法识别的原生参数都会原样直接交给 `claude`。如需从 TTY 显式启动同一交互路径，可用 `claude-context context-run`。

</details>

<a id="privacy"></a>
## 本地数据与隐私

默认情况下，连续性数据与包源码分离，位于：

```text
${CLAUDE_CONFIG_DIR:-$HOME/.claude}/session-continuity/
```

这个目录保存运行记录、工作笔记及可重建的 History 搜索缓存。原始对话仍由 Claude Code 保存；缓存额外保存允许检索的脱敏文本与准确定位，不保存原始转录、隐藏思考或工具调用输入，但仍应按私密会话数据保护。

工具本身不增加额外上传服务。不过，当 Claude 读取旧对话或笔记时，这些内容可能随正常请求发送给你选用的模型服务。请像保护其他 Claude Code 会话一样，保护这些本地记录。

<details>
<summary>选择本地数据目录</summary>

如有需要，请在启动命令前选择其他本地状态目录：

```sh
export CLAUDE_CONTEXT_CONTINUITY_DIR="<chosen-state-directory>"
cclaude
```

不需要额外安装 skills 或复制模板。受管 `cclaude` 会话从自己的本地 plugin 加载 `/renew`；普通 `claude` 会话不会得到这个命令。未托管会话仍可直接让模型保存简短交接，必要时使用同一套 `claude-context` History/Notes 命令。

</details>

<a id="platform"></a>
## 平台范围

| 环境 | 状态 |
| --- | --- |
| Python | 需要 3.10+；已测试 3.12 |
| macOS 与 Linux | 支持的 POSIX 基线，需具备原生 `claude` 与 `tmux` |
| WSL | 未验证 |
| 原生 Windows | 不支持 |

<a id="faq"></a>
## FAQ

### 它能替代以 compact 为中心的工作流吗？

可以。你可以用“提前留出预算 → 自动换到干净上下文 → 根据笔记和原始历史继续”的方式，替代反复依赖 `/compact` 的工作习惯。

它不会改写或移除原生 `/compact` 命令，也不保证所有任务都会效果更好或完全不丢细节。另外，`/compact` 压缩的是当前上下文，并不会因此删除原始对话记录。

### 工具无法判断工作是否完成，或无法获得可用预算时会怎样？

只有自动切换会暂停。你仍可正常使用原生 Claude Code 会话；工具不会强制 clear、接管其他终端，或重放结果不确定的输入。

### 它能一次检索每个过往 context 吗？

可以。`v0.1.0` 的 `history-search` 可统一搜索一个连续会话登记的窗口，并支持过滤、排序和分页；整个任务期间均可调用。它不搜索无关项目，也不保证模型每次都能选择正确的查询词。单源 `history --search` 与窗口目录接口仍然保留。

### 它会替代 Claude Code，或完整复刻 Codex 吗？

不会。你使用的仍是原生 Claude Code，这个工具只补充上下文接力，不重做编码助手，也不改变原有模型、工具和权限。

<a id="contributing"></a>
## 参与贡献与测试

欢迎通过 fork 和 Pull Request 参与贡献。只有仓库所有者可以更新或合并到 `main`；其他贡献者无需写权限，也可以在本地运行测试。版本更新说明统一查看 [GitHub Releases](https://github.com/macchen123/claude-context-continuity/releases)。

在自己的源码 checkout 中，使用可信的包镜像安装项目及依赖，再运行隔离测试：

```sh
python3 -m pip install -e .
CLAUDE_CONFIG_DIR="$PWD/.test-claude-config" \
PYTHONPATH=src \
python3 -m unittest discover -s tests -v
```

## 许可证

Claude Context Continuity `0.1.0` 使用 [MIT License](LICENSE) 发布。
