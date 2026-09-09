# Codex 原生上下文管理：未来探索与 Pi 迁移评估

状态：**搁置探索，不是当前实现方案或发布承诺。**

核验日期：2026-09-09。

当前交付仍是修复 `cclaude` 的原生会话自动接续：确认宿主真正建立新会话、保留后台任务与待处理输入、持续提供本地 History/Notes。本文的 CPA／Codex 路线不作为当前修复或其他工作的前置条件，不代表已获准更换宿主、部署插件或修改运行中的服务。

## 1. 为什么保留这条路线

期望实现“更换模型上下文，不重启工作现场”：后台任务、工具执行状态和输入队列继续存在；旧对话原文可按需查回，不靠反复文本摘要维持长任务。

开源宿主的价值不只是能看到代码，而是可以在必要时接入或修改执行循环、请求构造、上下文裁剪和本地状态重建。未来可以评估 Pi 等开放宿主；本次不迁移已有工作环境。

## 2. 参考项目与固定核验版本

- [pi-openai-toolkit](https://github.com/awoaCrim/pi-openai-toolkit)，核验版本 `0.14.3`，commit [`1d3972c7ae64397463e922d8fe8333279df352d8`](https://github.com/awoaCrim/pi-openai-toolkit/tree/1d3972c7ae64397463e922d8fe8333279df352d8)。
- Pi 宿主 `@earendil-works/pi-coding-agent@0.85.1`，源码 [`earendil-works/pi@v0.85.1`](https://github.com/earendil-works/pi/tree/v0.85.1)。
- Codex：本次查看 `main` 时的 commit [`20f109eadb9b45360e6ca4f1dee2e82c83a48f7a`](https://github.com/openai/codex/tree/20f109eadb9b45360e6ca4f1dee2e82c83a48f7a)。
- CPA：核验运行版本对应源码 [`CLIProxyAPI@v7.2.154`](https://github.com/router-for-me/CLIProxyAPI/tree/v7.2.154)。
- Claude Code：本机核验版本 `2.1.266`。

以上是核验快照，不应在未来直接当成最新版本。重新启动探索时，先复核最新上游和实际运行版本；实验接口可能改变。

## 3. pi-openai-toolkit 如何对齐 Pi 与 Codex

它不是让两边独立换窗，而是由一个窗口管理器维护共同边界。

1. 以 Pi session ID 作为后端 `session_id`／`thread_id`，为每个窗口生成 UUID 和序号；把边界写入 Pi 会话记录，恢复时从记录重建。
2. 注册持续可用的 `new_context`、`get_context_remaining`、`history`、`notes` 工具。
3. `new_context` 默认检查本窗口是否有成功的 Notes 写入，然后通过 Pi 的 `sendMessage` 把新边界交给原生消息循环。
4. Pi 的 `context` hook 在下一次模型请求构造之前执行 `messages.slice(boundaryIndex)`，只选择新边界之后的消息。
5. `session_before_compact` 接管原生压缩路径；在消费已安排的换窗时，返回固定的“未生成摘要”标记和 `firstKeptEntryId`。Pi 宿主据此重建活动消息集合：`agent.state.messages = sessionContext.messages`。这一步发生在原生 compaction 流程消费边界时，不等于每次 `new_context` 返回的瞬间就清空全部本地内存。
6. `before_provider_request`／`before_provider_headers` 使用同一个边界添加窗口元数据，并转换加密工具结果。新窗口首次真实用量到来前，不把旧窗口的高用量误当成新窗口用量。

Pi session ID 本身可以保持不变；关键是宿主能够控制当前模型消息集合，并让本地上下文管理与后端窗口一致。完整会话记录仍保留。

关键源码：

- [工具入口与持续查史指引](https://github.com/awoaCrim/pi-openai-toolkit/blob/1d3972c7ae64397463e922d8fe8333279df352d8/src/context-management/tools.ts#L92-L165)
- [窗口裁剪和本地 compaction 边界](https://github.com/awoaCrim/pi-openai-toolkit/blob/1d3972c7ae64397463e922d8fe8333279df352d8/src/context-management/window-manager.ts#L126-L160)
- [Pi 宿主重建活动上下文](https://github.com/earendil-works/pi/blob/v0.85.1/packages/coding-agent/src/core/agent-session.ts#L2356-L2360)
- [窗口身份与加密结果改写](https://github.com/awoaCrim/pi-openai-toolkit/blob/1d3972c7ae64397463e922d8fe8333279df352d8/src/context-management/window-request.ts#L18-L83)

## 4. 已完成的最小后端核验

使用已有授权账号和全新随机会话，执行不生成模型内容、不写 Notes、不读取已有会话正文的检查：

| 目标 | 操作 | 实际结果 |
| --- | --- | --- |
| 原始 Codex 后端 | `POST /alpha/history/v2/list_windows` | HTTP 200，返回 `encrypted_output`；未输出密文正文 |
| 原始 Codex 后端 | `POST /alpha/notes/v2/thread_hint` | HTTP 200，返回空 `text` |
| 核验时的 CPA 实例 | 相同协议的 `/v1/alpha/history/...` 与 `/v1/alpha/notes/...` | HTTP 404 |
| 核验时的 CPA 实例 | `/v1/models` | HTTP 200，模型列表包含 `gpt-6-astra` |

首次后端探测出现 400／405。核对源码后发现需要 `context` 对象、`mode/limit` 截断策略，以及两个端点均使用 POST；toolkit README 中 `thread_hint` 的 GET 写法与实现不一致。修正请求后上述原始后端检查通过，不把首次错误解释成服务不支持。

准确请求形状参考：

```json
{
  "context": {
    "session_id": "独立验证会话的 UUID",
    "current_agent_name": "/root"
  }
}
```

截断策略示例为 `{"mode":"bytes","limit":1024}`。正式契约仍以重新核验时的源码和真实运行结果为准。

这只证明端点和基本协议可用，**没有证明旧窗口已经被正确收录、模型能解读加密结果，或完整跨窗口任务已跑通**。HTTP 200 不能替代这些验收。凭证仅在内存中使用，不写入本文或验证产物。

## 5. CPA 插件具备的能力与边界

已核实 v7.2.154 提供请求前后改写、响应改写、下行 SSE 分块改写、主动终止并返回自定义响应、外发 HTTP，以及自定义模型执行适配。

普通插件的自定义路由被限制在管理和资源命名空间，不能直接任意注册 `/v1/alpha/...`。但这只是不能原样照搬 toolkit 的 URL，不能据此断言整个方案必须 fork CPA。未来应先验证插件专用 RPC／薄工具适配是否足够，再决定是否需要最小核心扩展。

另需核实：

- Anthropic ↔ Responses 转换是否保留工具配对、窗口元数据、加密参数和 `encrypted_output`／`encrypted_content`。
- 生成请求与 History/Notes 请求是否绑定到同一后端历史。多账号调度时，不能让它们各自独立选择账号。
- 主会话、子代理、恢复和分支身份是否正确隔离。
- 窗口状态只有一个权威写入者，其他层从明确边界派生，不各自独立触发切换。

参考：[CPA 插件接口](https://github.com/router-for-me/CLIProxyAPI/blob/v7.2.154/sdk/pluginapi/types.go#L925-L1046)。

## 6. 为什么当前不在 Claude Code 上推进这条路线

核验的 Claude Code 2.1.266 扩展接口中，没有发现 Pi 所需的两项等价能力：

1. 返回并替换“下一次请求使用的完整消息集合”；
2. 提交指定保留边界，让宿主重建自己的活动上下文。

可用能力包括补充上下文、替换单次工具输出、在工具批次结束后停止下一次请求，以及触发原生 `/clear`。这些不能被描述成任意宿主上下文裁剪接口。`PreCompact` 不能返回 Pi 式的 `firstKeptEntryId` 自定义裁剪结果；`MessageDisplay` 只改变显示内容；供应商协议中的 `context_management` 字段也不是插件修改宿主消息列表的入口。

因此，单独在 CPA 裁掉旧消息，不能证明 Claude Code 本地长度检查、活动消息状态和用量显示已同步重置。不能伪造 usage，也不能以服务端请求变短代替宿主验收。

**当前决策：搁置纯 CPA／Codex 上下文接管路线，继续完成 `cclaude` 原生换会话方案。** 只有后续宿主提供等价接口，或明确选择可扩展的宿主，才重新评估。

## 7. 未来最小验证与 Pi 迁移评估

先运行独立小任务，不直接迁移正在进行的工作：

1. 窗口 A 留下真实指令与已完成产物，保存必要 Notes。
2. 启动一个可独立继续的后台任务，并提交一条待处理输入。
3. 切换到窗口 B，分别检查宿主当前模型消息集合、实际发出的请求和后端 window ID，而不是只看成功提示。
4. 正常工作数步，再在两个不同阶段需要 A 中的旧信息；观察真实 History 搜索和准确读回，不把交接时的一次读取算成持续查史。
5. 确认后台任务没有重启或丢失、输入没有丢失或重复执行、模型请求没有再次带上 A 的全文。
6. 核验恢复、分支、子代理与账号调度时的身份；检查真实用量，不以显示修饰掩盖不一致。
7. 核验现有 MCP、项目指令、工具权限、模型路由及日常编辑体验是否可以迁移。

需要区分：纯函数测试、原生宿主加本机脚本模型验证、真实模型行为验证。脚本安排的 History 调用只能证明连接，不证明模型会自主识别缺失信息。远端模型或其他付费操作须在执行前明确授权。

重新选择宿主时，以以上实测和扩展接口为依据，不单凭“开源”标签或 README 的“无损”表述。尤其应将无摘要 `new_context + history + notes` 与 Remote Compaction v2 分开评估；密文存在不等于已经证明原文细节可无损恢复。
