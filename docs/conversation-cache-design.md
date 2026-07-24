# 会话上下文与缓存命中设计方案

> 版本: 0.5 | 时间: 2026-07 | 状态: 待审核
>
> v0.2 变更: 根据 v0.1 审核反馈重构——移除隐式 auto 模式、fork 支持任意分支点、
> 会话命名空间隔离与静态校验、跨 provider 安全检查。
>
> v0.3 变更: 根据 v0.2 审核反馈——`_type` 标记与 Context 序列化机制兼容性已源码验证、
> condition 分支一致性静态校验、`iter_child_steps()` 通用子 Step 遍历机制替代手写递归。
>
> v0.4 变更: 再次验证 v0.3 三项修复均成立；新发现 LargeRef 路径会导致大会话
> （>1MB）断点续传丢数据，补充运行时检测与告警机制。
>
> v0.5 变更: 针对 v0.4 三项结构性缺陷做根因修复——
> （2）`key`/`from` 支持模板化，一份 step 即可管理运行时数量不确定的 provider/persona；
> （3）二元开关 `allow_cross_provider` 升级为**一致性档位** + 工具签名校验，消除
> "一半读旧 agent / 一半读新 agent"的半状态不一致，并新增 `MessageNormalizer`
> 扩展点为真正的格式适配留口；（4）大会话不再"只告警不修复"——引入
> `ConversationStore` 旁路存储，>阈值会话自动卸载、Context 仅持内容寻址引用，
> 断点续传不再丢历史（旗舰场景可靠性闭环）。附带缓存命中可观测性
> （`LLMUsage.cached_tokens` 回写会话 meta）。

## 1. 问题分析

### 1.1 现状

当前 `LLMStep` 每次调用都是**无状态**的:

```
LLMStep.run() → _build_messages() → [system, user] → LLM → output
```

- 每次都从 `system` + `prompt` 模板重新构建消息列表
- Step 之间**无消息历史共享**——B 无法继承 A 的对话上下文
- 循环场景（如 `story_best_practice.yaml`）靠把累加文本 `{{story_v1}}` 塞进 prompt 模板来传递上下文

### 1.2 三个核心痛点

| 场景 | 现状做法 | 问题 |
|------|---------|------|
| **循环增量生成** | 每轮把完整前文 `{{story_v1}}` 拼进一个 user 消息 | 前文越长 token 越多；消息全在一个 user 消息里，提供商无法按消息前缀缓存 |
| **A→B 同上下文不同输出** | B 用相同 agent + 相同 prompt 再跑一次 | 消息确实相同能命中缓存，但无法显式管理"复用 A 的消息但不带 A 的回答" |
| **A→B 继承上下文继续回答** | 无法实现——B 只能把 A 的输出塞进 prompt | B 看到的是"用户消息里夹了 A 的回答"，而非真正的多轮对话 `[system, user, assistant(A), user(B)]` |

### 1.3 缓存命中原理

OpenAI / DeepSeek / Anthropic 等提供商的 prompt cache 按**消息数组前缀**匹配:

```
调用1: [system, user₁]                                → 缓存 [system, user₁]
调用2: [system, user₁, assistant₁, user₂]             → 命中前缀 [system, user₁]，仅 user₂ 是新 token
调用3: [system, user₁, assistant₁, user₂, assistant₂, user₃] → 命中更长前缀
```

**关键**: 消息必须是独立的消息条目（而非拼在一个 user 消息里），前缀才能稳定匹配。

---

## 2. 设计目标

| 目标 | 说明 |
|------|------|
| **简洁** | 仅引入一个概念（会话），最小化新增 API |
| **向后兼容** | 不配置 conversation 时行为完全不变 |
| **安全** | 会话命名空间隔离 + 类型校验 + 一致性档位 + 不可变存储 |
| **可靠** | 大会话断点续传不丢历史（旁路存储兜底，而非仅告警） |
| **可静态推理** | YAML 声明式语义，Step 行为不隐式依赖运行时状态 |
| **可扩展** | 模板化 key 支持动态多 provider；`MessageNormalizer` 为真适配留口；预留滑动窗口 / 摘要压缩 / 显式缓存断点 |
| **可观测** | 缓存命中 token 回写会话 meta + trace，命中与否在生产可见 |
| **易用** | YAML 中 3 行配置即可启用 |

---

## 3. 核心概念：会话（Conversation）

**会话**是一组有序的 `LLMMessage` 列表，存储在 Context 中，供 LLM Step 加载、扩展、分支。

### 3.1 存储格式（带类型标记与元数据）

会话以带类型标记的 dict 存储，与普通变量隔离。`meta` 记录创建会话的 agent
派生属性快照（provider / model / **工具签名** / **system 哈希**），供后续
continue/fork 做完整一致性校验（见 §8.2），而非仅 provider/model 二元判断:

```
Context
├── character_profile: {...}       ← 普通变量（Step 输出）
├── story_v1: "第一章..."          ← 普通变量（append 累加）
└── chat_v1: {                     ← 会话（带类型标记，< 阈值走内联冻结）
      "_type": "conversation",     ← 类型标记，防与普通变量冲突
      "messages": [
        {"role": "system", "content": "你是小说家..."},
        {"role": "user", "content": "写第1章"},
        {"role": "assistant", "content": "第一章..."},
        {"role": "user", "content": "续写第2章"},
        {"role": "assistant", "content": "第二章..."},
      ],
      "meta": {
        "provider": "mimo",            ← 创建会话的 provider
        "model": "mimo-v2.5",          ← 创建会话的 model
        "tools_sig": "a1b2c3",         ← agent.tools 排序去重后的短哈希
        "system_hash": "d4e5f6",       ← system 消息哈希（缓存前缀稳定锚）
        "cached_tokens": 0             ← 累计命中缓存 token（可观测，见 §7.9）
      }
    }
```

**大会话（> `large_object_threshold`）走旁路存储**，Context 仅持内容寻址引用:

```
└── chat_big: {                        ← 大会话引用（恒小于阈值）
      "_type": "conversation_ref",     ← 引用标记
      "store": "local",                ← 旁路存储类型
      "ref": "9f8e7d6c...",            ← 内容寻址 key（消息序列 md5）
      "size": 2097152,                 ← 原始字节大小（仅用于观测/告警）
      "meta": { ...同上... }           ← meta 内联，无需回读旁路即可校验一致性
    }
```

`conversation_ref` 恒为小对象（< 1KB），`ctx.set` 不会走 LargeRef 路径，
`snapshot()`/`restore()` 完整保留引用，恢复后按 `ref` 从 `ConversationStore`
回填完整消息历史（见 §8.1.2）。

**为什么用带标记的 dict 而非裸 list**: 见 §8.1 命名空间隔离。

### 3.2 不可变保障

会话存入 Context 时：
- **内联路径**（≤ 阈值）：经 `_deep_freeze` 冻结为 `FrozenDict`（含 `tuple` 化的 messages）。
- **旁路路径**（> 阈值）：完整 messages 经 `ConversationStore.save()` 落盘（内容寻址、原子 rename），Context 仅持 `conversation_ref`（小 dict，同样被冻结）。

`fork`/`continue` 均构造**新列表**，不修改源会话。旁路存储内容寻址（同内容同 `ref`），天然不可变、天然去重，旧版本由 `ConversationGCSweeper` 按宽限期回收。

---

## 4. YAML 语法

在 LLM Step 上新增可选的 `conversation` 配置块:

```yaml
- id: write_chapter
  type: llm
  agent: writer
  prompt: "请续写第{{chapter_num}}章"
  output: chapter
  conversation:
    mode: continue          # start | continue | fork
    key: story_chat         # 存储到 Context 的键名（支持 {{var}} 模板）
    from: story_chat        # 源会话键名（默认=key，同样支持模板）
    fork_at: last           # fork 专属：分支点（last | N），默认 last
    compat: strict          # strict | passthrough，默认 strict
```

**字段说明**:

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `mode` | str | — | 会话模式（见 §5） |
| `key` | str | step id | 当前会话存入 Context 的键名。支持 `{{var}}` 模板渲染（运行期解析），见 §4.1 |
| `from` | str | =key | 源会话键名（continue/fork 从此加载历史）。同样支持模板 |
| `fork_at` | str\|int | `"last"` | fork 专属：分支点。`"last"` 操作末尾轮；`N`（整数）从第 N 个 user 消息处分支（0-indexed） |
| `compat` | str | `"strict"` | 一致性档位（见 §8.2）：`strict` 校验 provider/model **与工具签名** 全部一致，否则 raise；`passthrough` 仅告警放行，**不做任何格式转换**（显式声明"自行承担兼容风险"） |

不配置 `conversation` → 当前无状态行为，零改动。

### 4.1 模板化 key：动态多 provider/persona

`key`/`from` 在运行期经现有模板引擎（`resolve_template`）渲染，与 prompt 模板同一通路。
这使**一份 step 模板**即可管理"运行期数量不确定"的 provider/persona 列表，无需手写 2×P 份
几乎相同的 start/continue 分支:

```yaml
- id: gen_all_providers
  type: loop
  iter: "{{providers}}"          # ["openai", "deepseek", "mimo"] —— 运行期才确定
  as: provider
  output_mode: append
  output: story
  step:
    type: condition
    when: "has('chat_{{provider}}')"   # has() 内同样支持模板（见 §7.4）
    then:
      - id: cont
        type: llm
        agent: writer
        prompt: "续写"
        conversation:
          mode: continue
          key: "chat_{{provider}}"      # 运行期渲染为 chat_openai / chat_deepseek / ...
          from: "chat_{{provider}}"
    else:
      - id: start
        type: llm
        agent: writer
        prompt: "创作第1章"
        conversation:
          mode: start
          key: "chat_{{provider}}"
```

**渲染语义**：`key`/`from` 在 `run()` 进入会话流程时渲染一次，渲染结果作为 Context 实际键名。
循环变量 `{{provider}}` 必须存在于 Context（由 loop `as` 注入），缺失抛 `KeyError`——
这是配置错误而非"会话不存在"，应尽早暴露。

**静态校验影响**（见 §7.8）：
- 校验 A（key vs output 重名）：含 `{{` 的模板 key 跳过字面碰撞检查，改由运行时 `_type`
  标记兜底（模板 key 与静态 output 名同名的概率极低，且会立即触发运行时报错）。
- 校验 B（condition 分支一致性）：then/else 两侧使用同一模板字符串即视为等价
  （字面比较仍成立）。

---

## 5. 三种模式

### 5.1 模式总览

| 模式 | 会话不存在时 | 会话已存在时 | 典型场景 |
|------|------------|------------|---------|
| `start` | 创建新会话 | **覆盖重建**（显式覆盖旧会话） | 显式开启新对话 |
| `continue` | **报错** | 扩展（追加 user + 获取 response） | A→B 继承上下文+回答 |
| `fork` | **报错** | 分支（从指定点回退，重新提问） | A→B 同上下文不同输出 |

### 5.2 移除 `auto` 模式

v0.1 的 `auto` 模式让 Step 行为隐式依赖运行时 `ctx.has(key)` 状态——同一 Step 在重试、条件分支、子流程复用等场景下语义会改变（第一次 start，重试变 continue），违背声明式原则。

**替代方案**: 移除 `auto`，提供 `has()` 表达式函数供 ConditionStep 显式判断:

```yaml
# 循环场景：用 condition + has() 显式分支
- id: gen_v1
  type: loop
  iter: "[1, 2, 3]"
  as: chapter_num
  output_mode: append
  output: story_v1
  step:
    type: condition
    when: "has('chat_v1')"
    then:
      - id: write_continue
        type: llm
        agent: writer_ardent
        prompt: "续写第{{chapter_num}}章"
        output: story_v1
        conversation:
          mode: continue
          from: chat_v1
          key: chat_v1
    else:
      - id: write_start
        type: llm
        agent: writer_ardent
        prompt: "创作第1章"
        output: story_v1
        conversation:
          mode: start
          key: chat_v1
```

**`has()` 函数**: 在 `eval_expression` 中新增 `has('key')` 支持，返回 `ctx.has(key)` 的布尔值。实现方式为在变量替换前预处理 `has(...)` 模式（类似现有的 `null`/`true`/`false` 替换），不引入新的求值通路。

> **关于循环中 step 配置重复**: 上述写法在 then/else 中重复了 agent/prompt/output 配置。这是显式分支的代价——换取静态可推理性。未来可通过 YAML 锚点（`&ref` / `*ref`）或 Step 模板继承机制消除重复，当前不引入额外复杂度。

### 5.3 各模式消息构建逻辑

三种模式统一在保存时记录**完整 meta 快照**（provider / model / tools_sig /
system_hash / cached_tokens），continue/fork 统一在加载时做**完整一致性校验**
（见 §8.2），而非仅 provider/model 二元判断。

**`start`**:
```
messages = [system, user(prompt)]
→ LLM → response
→ 保存 {messages: [system, user, assistant(response)],
        meta: {provider, model, tools_sig, system_hash, cached_tokens}} 到 key
```

若 `key` 已存在则覆盖（用户显式选择 start 即表示要重新开始）。

**`continue`**:
```
loaded, meta = load_and_validate(ctx, from, agent, compat)   # 加载 + 完整校验
messages = loaded + [user(prompt)]
→ LLM → response
→ 累计 cached_tokens += resp.usage.cached_tokens（见 §7.9）
→ 保存 {messages: loaded + [user, assistant(response)], meta: 新快照} 到 key
```

**`fork`**:
```
loaded, meta = load_and_validate(ctx, from, agent, compat)   # 加载 + 完整校验
base = fork_at_user(loaded, fork_at, prompt or None)
→ LLM → response
→ 保存 {messages: base + [assistant(response)], meta: 新快照} 到 key
```

**`load_and_validate` 内部**（continue/fork 共用）:
```
raw = ctx.get(from)
if raw._type == "conversation_ref":     # 旁路存储（见 §8.1.2）
    messages = store.load(raw.ref)
else:
    messages = raw.messages
check_compatibility(meta, agent, compat)  # provider/model + tools_sig + system_hash
```

### 5.4 fork_at 详解

`fork_at` 控制从会话的哪个位置分支。以 user 消息为分支锚点（因为"重新提问"的语义锚点天然是 user 消息）:

```
会话: [system, user₀, asst₀, user₁, asst₁, user₂, asst₂]

fork_at: "last" (默认)
  → 截断到最后一个 user: [system, user₀, asst₀, user₁, asst₁, user₂]
  → 去掉 asst₂，保留 user₂

fork_at: 1
  → 截断到第 2 个 user（0-indexed）: [system, user₀, asst₀, user₁]
  → 去掉 asst₁ 及之后所有消息，保留 user₁

fork_at: 0
  → 截断到第 1 个 user: [system, user₀]
  → 去掉 asst₀ 及之后所有消息，保留 user₀
```

无论 `fork_at` 为何值，如果提供了非空 `prompt`，则替换分支点的 user 消息为新 prompt。

### 5.5 模式选择速查

```
需要循环增量生成？             → condition + has() 显式分支 start/continue
B 要继承 A 的上下文+回答？      → continue
B 要 A 的上下文但不要 A 的回答？ → fork (fork_at: last)
要从对话中间某一轮重新分支？     → fork (fork_at: N)
需要显式开启新对话？            → start
不需要会话管理？                → 不配置 conversation（当前行为）
```

---

## 6. 场景覆盖

### 6.1 循环场景 + 缓存命中（story_best_practice.yaml 改造）

```yaml
- id: gen_v1
  type: loop
  iter: "[1, 2, 3]"
  as: chapter_num
  output_mode: append
  output: story_v1
  step:
    type: condition
    when: "has('chat_v1')"
    then:
      - id: write_v1_cont
        type: llm
        agent: writer_ardent
        prompt: "续写第{{chapter_num}}章，只输出本章内容"
        output: story_v1
        conversation:
          mode: continue
          from: chat_v1
          key: chat_v1
    else:
      - id: write_v1_start
        type: llm
        agent: writer_ardent
        prompt: "为以下主角创作第1章：{{character_profile.name}}"
        output: story_v1
        conversation:
          mode: start
          key: chat_v1
```

**缓存效果**:

```
迭代1: [system, user("创作第1章")]
       → 缓存 [system, user₁]
迭代2: [system, user₁, assistant₁, user("续写第2章")]
       → 命中 [system, user₁, assistant₁]，仅 user₂ 新增
迭代3: [system, user₁, assistant₁, user₂, assistant₂, user("续写第3章")]
       → 命中更长前缀，仅 user₃ 新增
```

### 6.2 A→B 同上下文不同输出（不含 A 的回答）

```yaml
steps:
  - id: step_a
    type: llm
    agent: writer
    prompt: "用一句话描述春天"
    output: answer_a
    conversation:
      mode: start
      key: chat

  - id: step_b
    type: llm
    agent: writer
    prompt: ""               # 空 prompt = 同输入重跑
    output: answer_b
    conversation:
      mode: fork             # 去掉 A 的回答，保留 [system, user]
      from: chat
      key: chat_b
    temperature_override: 0.9
```

B 的消息 = `[system, user]`（与 A 完全相同），但温度不同 → 不同输出。输入前缀命中缓存。

### 6.3 A→B 继承上下文+回答，B 继续回答

```yaml
steps:
  - id: step_a
    type: llm
    agent: assistant
    prompt: "解释什么是递归"
    output: answer_a
    conversation:
      mode: start
      key: chat

  - id: step_b
    type: llm
    agent: assistant
    prompt: "给一个 Python 递归的例子"
    output: answer_b
    conversation:
      mode: continue
      from: chat
      key: chat
```

B 的消息 = `[system, user("解释递归"), assistant(A的回答), user("给个Python例子")]`，真正的多轮对话。

### 6.4 从对话中间分支

```yaml
steps:
  # 三轮对话后，从第 1 轮重新分支
  - id: step_a1
    type: llm
    agent: assistant
    prompt: "问题1"
    output: a1
    conversation:
      mode: start
      key: chat

  - id: step_a2
    type: llm
    agent: assistant
    prompt: "问题2"
    output: a2
    conversation:
      mode: continue
      from: chat
      key: chat

  - id: step_a3
    type: llm
    agent: assistant
    prompt: "问题3"
    output: a3
    conversation:
      mode: continue
      from: chat
      key: chat

  # 从第 1 轮（user₀ 的位置）重新分支，提出不同问题
  - id: step_b
    type: llm
    agent: assistant
    prompt: "替代问题2"
    output: b_answer
    conversation:
      mode: fork
      from: chat
      fork_at: 1            # 从第 2 个 user 消息处分支
      key: chat_b
```

B 的消息 = `[system, user("问题1"), assistant(A1的回答), user("替代问题2")]`，从第 1 轮后重新分支。

### 6.5 并行双版本

```yaml
- id: generate
  type: parallel
  max_concurrency: 2
  branches:
    - id: gen_v1
      type: loop
      step:
        type: condition
        when: "has('chat_v1')"
        # ... continue / start 分支（各自 key: chat_v1）

    - id: gen_v2
      type: loop
      step:
        type: condition
        when: "has('chat_v2')"
        # ... continue / start 分支（各自 key: chat_v2）
```

两个版本各自独立会话（`chat_v1` / `chat_v2`），互不干扰。

---

## 7. 实现方案

### 7.1 新增模块: `agentkit/core/conversation.py`

会话序列化、操作、校验与旁路存储的工具集，不依赖其他 agentkit 子模块
（仅 `llm.base` 数据类 + `config.get_default`）:

```python
CONVERSATION_TYPE = "conversation"
CONVERSATION_REF_TYPE = "conversation_ref"

# ---- 序列化 ----
def messages_to_dicts(messages: list[LLMMessage]) -> list[dict]: ...
def dicts_to_messages(dicts: list[dict]) -> list[LLMMessage]: ...

# ---- agent 派生属性快照（用于一致性校验）----
def _tools_signature(tools: list[str]) -> str:
    """工具名排序去重后取短 md5，作为 tools 一致性指纹。"""
    # 例如 ["search","calc"] -> md5("calc,search")[:8]
    ...

def _hash_system(system_text: str) -> str:
    """system 消息短哈希，缓存前缀稳定锚 + 一致性校验双用。"""
    ...

# ---- 会话封装/解析 ----
def pack_conversation(messages, agent) -> dict:
    """封装为带完整 meta 快照的内联存储格式。"""
    return {
        "_type": CONVERSATION_TYPE,
        "messages": messages_to_dicts(messages),
        "meta": {
            "provider": agent.provider or "",
            "model": agent.model or "",
            "tools_sig": _tools_signature(agent.tools),
            "system_hash": _hash_system(agent.system or ""),
            "cached_tokens": 0,
        },
    }

def unpack_conversation(raw, key) -> tuple[list[LLMMessage], dict]:
    """解析内联存储格式，返回 (messages, meta)。
    类型不匹配时抛 ConversationTypeError（命名空间冲突检测）。"""
    if not isinstance(raw, Mapping) or raw.get("_type") != CONVERSATION_TYPE:
        raise ConversationTypeError(key, raw)
    return dicts_to_messages(raw["messages"]), dict(raw.get("meta", {}))

# ---- 旁路存储（大会话自动卸载，见 §8.1.2）----
class ConversationStore:
    """内容寻址会话存储协议。save 返回 ref key（内容 md5），load 回填字节。
    默认 LocalConversationStore：原子 rename 落盘 + mtime GC，镜像 ArtifactStore。
    可经 set_conversation_store() 替换为 S3/内存实现，保持 agentkit 依赖轻量。"""
    def save(self, data: bytes) -> str: ...
    def load(self, ref: str) -> bytes: ...
    def exists(self, ref: str) -> bool: ...

# ---- 会话操作（均返回新列表，不修改输入） ----
def fork_at_user(messages, fork_at="last", new_prompt=None) -> list[LLMMessage]: ...  # 同 v0.4

# ---- 完整一致性校验（替代 v0.4 的 provider 二元判断）----
def check_compatibility(conv_meta, agent, compat) -> None:
    """校验会话 meta 与当前 agent 的全部派生属性一致性。

    检查项（按严重度）:
      - tools_sig 不一致: 工具集已变 → 历史中的 tool_calls 与新 schema 错位，
        属正确性风险。strict 档 raise；passthrough 档 warning。
      - provider/model 不一致: 跨 provider 消息结构约束不同（Anthropic 连续
        同角色、tool_calls 结构、多模态格式），属正确性风险。同上。
      - system_hash 不一致: system 已变 → 缓存前缀断裂（性能问题，非正确性）。
        strict 档 warning（system_override 显式覆盖时静默）；passthrough 档 debug。

    strict（默认）: tools_sig / provider / model 任一不一致即 raise，杜绝
      "一半读旧 agent system / 一半读新 agent tools" 的半状态。
    passthrough: 仅告警放行，不做任何格式转换（显式声明自行承担风险）。
    """
    ...

# ---- 加载（内联 + 旁路统一入口）----
def load_and_validate(ctx, from_key, agent, compat) -> tuple[list[LLMMessage], dict]:
    """加载会话：内联直接取 messages，旁路 ref 从 ConversationStore 回填。
    加载后做 check_compatibility。"""
    raw = ctx.get(from_key)
    if isinstance(raw, Mapping) and raw.get("_type") == CONVERSATION_REF_TYPE:
        store = get_conversation_store()
        messages = dicts_to_messages(json.loads(store.load(raw["ref"]).decode()))
        return messages, dict(raw.get("meta", {}))
    messages, meta = unpack_conversation(raw, from_key)
    check_compatibility(meta, agent, compat)
    return messages, meta

# ---- 保存（自动选择内联/旁路）----
def save_conversation(ctx, key, messages, agent, *, cached_tokens_delta=0) -> None:
    """保存会话：> large_object_threshold 走旁路存储（Context 仅持 ref），
    否则内联冻结。旁路路径绕过 Context LargeRef，断点续传不丢历史。"""
    conv = pack_conversation(messages, agent)   # 内联格式
    conv["meta"]["cached_tokens"] = cached_tokens_delta  # 由调用方累计传入
    blob = json.dumps(conv["messages"], ensure_ascii=False).encode("utf-8")
    if sys.getsizeof(blob) > int(get_default("large_object_threshold")):
        store = get_conversation_store()
        ref = store.save(blob)
        ctx.set(key, {
            "_type": CONVERSATION_REF_TYPE,
            "store": "local",
            "ref": ref,
            "size": len(blob),
            "meta": conv["meta"],   # meta 内联，校验无需回读旁路
        })
    else:
        ctx.set(key, conv)

# ---- 格式适配扩展点（见 §8.2）----
class MessageNormalizer:
    """消息格式适配协议（默认 identity，不做转换）。
    provider 可注册专属 normalizer（如 Anthropic 连续同角色合并、
    tool_calls 结构转换、多模态 content part 重排）。
    strict/passthrough 均不自动调用——需显式配置 normalizer 才生效，
    避免"隐式改消息"破坏缓存前缀稳定性。"""
    def normalize(self, messages: list[LLMMessage], conv_meta: dict,
                 agent) -> list[LLMMessage]:
        return messages
```

### 7.2 修改 `LLMStep`

新增构造参数:

```python
def __init__(
    self,
    ...,
    conversation_mode: str | None = None,       # "start" | "continue" | "fork"
    conversation_key: str | None = None,        # 支持 {{var}} 模板
    conversation_from: str | None = None,        # 支持 {{var}} 模板
    conversation_fork_at: str | int = "last",
    conversation_compat: str = "strict",         # "strict" | "passthrough"（替代 allow_cross_provider）
):
```

修改 `run()` 流程:

```
1. 解析 Agent 配置                              (不变)
2. _build_messages_with_conversation()          (新增: 渲染 key/from + 按 mode 加载/构建消息)
3. 构建工具 schema                               (不变；strict 档下与已加载会话的
   tools_sig 一致——由 load_and_validate 保证，杜绝半状态)
4. 取 LLM 客户端                                 (不变)
5. Function Call 循环 → content + usage         (不变；usage.cached_tokens 透出供 §7.9)
   └─ 记录 fc_end = len(messages)              (新增: 标记契约链前的消息长度)
6. 输出契约保障链 → (value, error)              (不变)
7. on_exhausted 决策                             (不变)
8. 写入 output                                   (不变)
9. save_conversation(ctx, key, messages, agent,
                     cached_tokens_delta=usage.cached_tokens)  (新增: 自动选择内联/旁路)
```

**消息构建逻辑** (`_build_messages_with_conversation`):

```python
def _build_messages_with_conversation(self, agent, ctx):
    mode = self.conversation_mode
    if mode is None:
        return self._build_messages(agent, ctx)  # 当前行为

    # 渲染模板化 key/from（与 prompt 同一模板引擎，见 §4.1）
    key = self._render_str(self.conversation_key, ctx)
    from_key = self._render_str(self.conversation_from or key, ctx)
    self._conv_resolved_key = key        # 暂存供 save_conversation 复用

    if mode == "start":
        return self._build_messages(agent, ctx)  # 后续 save 时覆盖

    if mode == "continue":
        loaded, meta = load_and_validate(ctx, from_key, agent,
                                         self.conversation_compat)
        user_msg = self._build_user_message(agent, ctx)
        return loaded + [user_msg]

    if mode == "fork":
        loaded, meta = load_and_validate(ctx, from_key, agent,
                                         self.conversation_compat)
        prompt = self._render_prompt(agent, ctx)
        return fork_at_user(loaded, self.conversation_fork_at, prompt or None)
```

### 7.3 修改 YAML Loader

```python
if step_type == "llm":
    conv = step_dict.get("conversation")
    if conv:
        conv_mode = conv.get("mode")
        conv_key = conv.get("key")
        conv_from = conv.get("from")
        conv_fork_at = conv.get("fork_at", "last")
        conv_compat = conv.get("compat", "strict")   # 替代 allow_cross_provider
        # 校验 compat 合法取值
        if conv_compat not in ("strict", "passthrough"):
            raise ValueError(f"conversation.compat 仅支持 strict|passthrough，得到 {conv_compat!r}")
    else:
        conv_mode = conv_key = conv_from = None
        conv_fork_at = "last"
        conv_compat = "strict"
    return step_cls(
        ...,
        conversation_mode=conv_mode,
        conversation_key=conv_key,
        conversation_from=conv_from,
        conversation_fork_at=conv_fork_at,
        conversation_compat=conv_compat,
    )
```

### 7.4 新增表达式函数 `has()`（支持模板参数）

在 `core/template.py` 的 `eval_expression` 中，在变量替换前预处理 `has('key')` /
`has("key")` 模式。参数本身支持 `{{var}}` 模板，渲染后再判断——配合 §4.1 模板化 key
即可实现动态多 provider 的 start/continue 分支:

```python
_HAS_PATTERN = re.compile(r'\bhas\(\s*(["\'])([^"\']+)\1\s*\)')

def eval_expression(expr: str, ctx: "Context") -> Any:
    # 在 _VAR_PATTERN.sub 之前，先替换 has('...') 为 True/False
    def _has_repl(m: re.Match) -> str:
        # 参数本身可能含 {{var}} 模板，用 resolve_template 渲染（与 prompt 同通路）
        key = resolve_template(m.group(2), ctx)
        return "True" if ctx.has(key) else "False"
    expr = _HAS_PATTERN.sub(_has_repl, expr)
    # ... 后续不变
```

这样 `when: "has('chat_v1')"` 与 `when: "has('chat_{{provider}}')"` 均可用。
参数内引用的变量必须存在（由 loop `as` 等注入），缺失抛 `KeyError`——属配置错误。

### 7.5 system 消息处理（与 tools_sig 一致性联动）

| 模式 | system 来源 |
|------|-----------|
| `start` | 当前 agent.system（或 `system_override`）—— 与现状一致，记入 `system_hash` |
| `continue` / `fork` | 已加载会话中的 system 消息；设了 `system_override` 则替换会话中的 system |

在 `continue`/`fork` 中，**不会**追加新的 system 消息——会话已有 system，保持前缀稳定以命中缓存。

**与 §8.2 一致性校验的联动**（修复"一半读旧 agent system、一半读新 agent tools"的半状态）:
- `strict` 档：`load_and_validate` 校验 `tools_sig` 与 `system_hash`。当前 agent 的
  tools 与会话记录不一致 → raise（工具集已变，tool_calls 历史与新 schema 错位）；
  system 不一致但未设 `system_override` → warning（缓存前缀将断裂，但保留会话 system
  以最大化命中）。
- 工具 schema 始终从**当前 agent** 构建（`_build_tools_schema` 不变）。strict 档下
  当前 agent.tools 已被校验与会话一致，故"会话 system(旧) + 当前 tools(新)"的错位
  在源头消除——二者本就是同一 agent 的派生属性。
- `system_override` 显式覆盖时静默（用户明确要换 system），仅记入新 `system_hash`。

### 7.6 Function Call 与会话的交互

- Function Call 循环中的 `[assistant(tool_calls), tool(result)]` 消息是合法的对话内容，**一并保存**到会话
- 输出契约重试时追加的 `[assistant(坏输出), user(修复提示)]` **不保存**——仅保存最终回答
- 保存内容: `messages[:fc_end] + [assistant(最终content)]`，经 `save_conversation()` 落库（自动选内联/旁路）
- `cached_tokens` 从本轮 LLM 调用 `resp.usage.cached_tokens` 透出，随 `save_conversation(..., cached_tokens_delta=...)` 累计进会话 meta（见 §7.9）

### 7.7 `iter_child_steps()` 通用子 Step 遍历（解决审核问题 7）

v0.2 的 `_validate_conversation_keys` 静态校验依赖手写递归遍历已知的 Step 类型（loop body / parallel branches / condition then/else），对未来新增的组合型 Step（switch/retry/subworkflow 等）会隐性失效——开发者忘了同步更新校验函数，冲突检测悄悄失效而非报错。

**方案**: 在 `BaseStep` 上新增 `iter_child_steps()` 方法，封装"如何遍历子 Step"的知识到各 Step 类型自身:

```python
# steps/base.py — BaseStep 新增
class BaseStep(ABC):
    def iter_child_steps(self) -> list[BaseStep]:
        """返回直接子 Step 列表。叶子 Step 返回空列表。

        组合型 Step（Loop / Parallel / Condition 等）重写此方法，
        返回其持有的子 Step。静态校验、可视化等通用遍历逻辑
        只需调用此方法递归，无需按类型 case-by-case 硬编码。
        """
        return []
```

```python
# 各组合 Step 重写
class LoopStep(BaseStep):
    def iter_child_steps(self):
        return [self.body] if self.body else []

class ParallelStep(BaseStep):
    def iter_child_steps(self):
        return list(self.branches)

class ConditionStep(BaseStep):
    def iter_child_steps(self):
        return list(self.then_steps) + list(self.else_steps)
```

**通用递归遍历**:

```python
def walk_all_steps(steps: list[BaseStep]) -> list[BaseStep]:
    """深度优先遍历 Step 树（含所有嵌套层级）。"""
    result = []
    for step in steps:
        result.append(step)
        result.extend(walk_all_steps(step.iter_child_steps()))
    return result
```

新增组合型 Step 类型时，只需重写 `iter_child_steps()`，所有依赖此方法的通用逻辑（静态校验、可视化、trace 聚合等）自动获得对新类型的支持，无需逐个更新。

### 7.8 YAML 静态校验

基于 `walk_all_steps` 通用遍历，实现两类校验:

```python
def _validate_conversation_keys(steps: list[BaseStep]) -> None:
    """校验 A: conversation.key 不与任何 Step 的 output 重名。

    含 {{ 的模板 key 跳过字面碰撞检查（运行期才确定，且与静态 output 名同名的
    概率极低），改由运行时 _type 标记兜底（unpack_conversation 失败即报错）。"""
    all_steps = walk_all_steps(steps)
    output_keys: set[str] = set()
    conv_keys: set[str] = set()
    for step in all_steps:
        for port in step.outputs:
            output_keys.add(port.name)
        if hasattr(step, 'conversation_key') and step.conversation_key:
            k = step.conversation_key
            if "{{" in k:          # 模板 key：跳过字面碰撞检查
                continue
            conv_keys.add(k)
    collisions = output_keys & conv_keys
    if collisions:
        raise ValueError(
            f"conversation.key 与 Step output 重名: {collisions}。"
            f"请使用不同的键名避免数据覆盖。"
        )

def _validate_condition_branch_consistency(steps: list[BaseStep]) -> None:
    """校验 B: condition 分支中 conversation 配置的一致性约束。

    当 then/else 两侧均含 conversation 配置时，校验:
    - conversation.key 必须相同（操作同一会话）
    - output 必须相同（写入同一变量）

    这约束了"start/continue 分支必须保持等价"这个语义不变量，
    防止只改一个分支导致首轮与后续轮次行为不一致。
    prompt 和 agent 允许不同（start 是"创作第1章"，continue 是"续写"）。
    """
    for step in walk_all_steps(steps):
        if not isinstance(step, ConditionStep):
            continue
        then_convs = [s for s in step.then_steps
                      if hasattr(s, 'conversation_key') and s.conversation_key]
        else_convs = [s for s in step.else_steps
                      if hasattr(s, 'conversation_key') and s.conversation_key]
        if not then_convs or not else_convs:
            continue  # 只有一侧有 conversation，不约束
        then_keys = {s.conversation_key for s in then_convs}
        else_keys = {s.conversation_key for s in else_convs}
        if then_keys != else_keys:
            raise ValueError(
                f"ConditionStep {step.id!r} 的 then/else 分支 "
                f"conversation.key 不一致: then={then_keys}, else={else_keys}。"
                f"分支两侧必须操作同一会话。"
            )
        then_outs = {s.output for s in then_convs if s.output}
        else_outs = {s.output for s in else_convs if s.output}
        if then_outs and else_outs and then_outs != else_outs:
            raise ValueError(
                f"ConditionStep {step.id!r} 的 then/else 分支 "
                f"output 不一致: then={then_outs}, else={else_outs}。"
                f"分支两侧必须写入同一 output 变量。"
            )
```

**校验 B 解决的问题（审核意见 6）**: `condition + has()` 模式中 then/else 分支各自定义完整 agent/prompt/output，两者在语义上必须等价（"start 第一章"和"continue 续写"是同一件事的两种入口）。但系统此前没有机制约束这一点——只改 then 分支的 agent 或 output 而忘了同步 else 分支，workflow 会在首轮和后续轮次表现出不同行为且不报错。

校验 B 约束 `conversation.key` 和 `output` 必须一致，而允许 `prompt`/`agent`/`temperature` 不同（因为 start 和 continue 的提示词本就不同）。这在不限制分支灵活性的前提下，锁定了"数据流一致性"这个最关键的不变量。**模板化 key** 下 then/else 两侧使用同一模板字符串（如 `"chat_{{provider}}"`），字面比较天然成立，无需特殊处理。

### 7.9 缓存命中可观测性（解决"无 usage.cache_* 验证"缺口）

v0.4 全文未读取任何缓存命中学段，缓存收益在生产里悄悄归零无人发现。v0.5 利用
`LLMUsage.cached_tokens`（`llm/base.py` 已有字段）回写会话 meta，使命中与否可观测:

```python
# LLMStep.run 末尾（FC 循环与契约链均累计 token，cached_tokens 同步累计）
final_usage = self._token_usage_total          # 已有 scratch
cached_delta = self._cached_tokens_total       # 新增 scratch：Σ resp.usage.cached_tokens
save_conversation(ctx, key, final_messages, agent,
                  cached_tokens_delta=cached_delta)
```

- `cached_tokens` 累计进会话 `meta`，跨 continue/fork 轮次叠加，可在 trace 与
  `conversation.meta` 中读取，供日志/告警判断"缓存是否真命中"。
- `on_llm_call` 钩子已携带 `resp.usage`，`TokenAccountingHooks` 可据此统计命中率
  （`cached_tokens / prompt_tokens`），无需新增 hook 通路。
- **不覆盖 Anthropic**：`cached_tokens` 仅对自动前缀缓存的 provider（OpenAI/DeepSeek）
  有效；Anthropic 显式 `cache_control` 仍属 §10 扩展项（需 provider client 注入字段）。

---

## 8. 安全性

### 8.1 命名空间隔离（解决审核问题 3）

**三层防护**:

1. **类型标记**: 会话存储为 `{"_type": "conversation", "messages": [...], "meta": {...}}`，与普通变量（str/dict/list）结构不同。加载时 `unpack_conversation` 校验 `_type` 字段，不匹配则抛 `ConversationTypeError`，错误信息明确提示"可能是与普通变量重名冲突"。

2. **YAML 静态校验**: 编译期检测 `conversation.key` 是否与任何 Step 的 `output` 重名，重名则编译失败。在配置阶段就拦截冲突，而非运行时才报错。校验遍历基于 `iter_child_steps()` 通用机制（见 §7.7），不依赖手写类型枚举。

3. **运行时类型校验**: 即使静态校验遗漏（如动态构建 Workflow），加载会话时也会校验类型标记，给出清晰错误:

```python
class ConversationTypeError(ValueError):
    def __init__(self, key, actual):
        super().__init__(
            f"Context key '{key}' 不是有效会话数据 "
            f"(实际类型: {type(actual).__name__})。"
            f"可能是与普通 Step output 变量重名冲突。"
        )
```

### 8.1.1 `_type` 标记与 Context 序列化机制兼容性验证（解决审核问题 5）

v0.2 留了问号：`_type: "conversation"` 是否与 Context 现有的 `_to_jsonable` / `_from_jsonable` / `_deep_freeze` 冲突。以下为源码级验证结论。

**`_deep_freeze`（冻结）— 无冲突**:

源码（`context.py:310-344`）对 dict 只做 `FrozenDict({k: _deep_freeze(v) for k, v in obj.items()})`，递归冻结 value，**不检查 `_type` 字段**。`{"_type": "conversation", ...}` 被正确冻结为 `FrozenDict`，`_type` 的值 `"conversation"` 是 str，原样返回。

**`_to_jsonable`（序列化）— 无冲突**:

源码（`context.py:354-381`）对 Mapping 做 `{_json_key(k): _to_jsonable(v) for k, v in obj.items()}`，递归处理。`_type` 的值 `"conversation"` 是 str 原样返回。整个会话 dict 被正确序列化为 `{"_type": "conversation", "messages": [...], "meta": {...}}`。

**`_from_jsonable`（反序列化）— 无冲突，但需理解其分支逻辑**:

源码（`context.py:384-420`）对 dict 检查 `_type` 字段，有三个特殊处理分支:

```python
_type = obj.get("_type")
if _type == "set" and "_items" in obj:      # → 还原为 set
if _type == "bytes" and "_data" in obj:     # → 还原为 bytes
if _type is not None and "_repr" in obj:    # → 保留为 dict（不可恢复类型）
# 否则：普通 dict，递归处理 value
```

对 `{"_type": "conversation", "messages": [...], "meta": {...}}`:
1. `_type = "conversation"` — 不等于 `"set"`，不等于 `"bytes"` → 跳过前两个分支
2. `_type is not None`（True）`and "_repr" in obj` → False（会话数据不含 `_repr` 键）→ 跳过第三个分支
3. 进入"普通 dict：递归处理 value"分支 → 正确还原

**结论: `_type: "conversation"` 与 Context 序列化机制完全兼容，断点续传不丢会话历史。**

**已知限制（非本次引入）**: `_from_jsonable` 的 docstring 已记录——若用户数据恰好同时含有 `_type` + `_items`/`_data`/`_repr` 键名组合，会被误判。会话消息格式为 `{"role": ..., "content": ...}`，不含这些键名，无冲突风险。多模态 content part（如 `{"type": "image_url", ...}`）使用 `type` 而非 `_type`，亦无冲突。

### 8.1.2 LargeRef 路径：大会话断点续传根因修复（v0.5，替代 v0.4 告警方案）

上述验证只覆盖了"小对象"路径（`_deep_freeze` → `FrozenDict` 存储）。Context 对超过
`large_object_threshold`（默认 1MB）的对象走 **LargeRef** 路径:

```
ctx.set(key, value)
  → _sizeof(value) > 1MB?
    → True:  存为 LargeRef（仅持引用 + 摘要）
    → False: 存为 _deep_freeze(value)（FrozenDict）
```

**v0.4 暴露的问题链路**（仅告警、不修复）:

1. `ctx.set("chat_v1", conversation)` 时，若会话 >1MB → 存为 `LargeRef`
2. `snapshot()` 对 LargeRef 只记 `{"_evicted_big": True, ...}` — **不含实际消息数据**
3. `restore()` 恢复为占位 dict → `unpack_conversation` 校验 `_type` 失败 → 抛 `ConversationTypeError`
4. 后续 `continue`/`fork` 加载此 key → 会话历史静默丢失

**影响场景**: 循环增量生成（旗舰场景）中，长篇小说多轮迭代后恰好越过 1MB；此时触发
断点续传，会话历史丢失。v0.4 只打一行 `logger.warning`，"该丢的还是丢"——稳定性头号缺口。

**v0.5 方案：旁路存储（ConversationStore），绕过 LargeRef 而非告警**

根因思路（采纳用户修订建议"把大会话单独存到不经 LargeRef 摘要化的旁路存储，靠 hash
引用回填"）：在 `ctx.set` **之前**把 > 阈值的会话卸载到独立内容寻址存储，Context 只持
恒小于 1KB 的 `conversation_ref`。`ctx.set` 看到的是小对象 → 不走 LargeRef →
`snapshot()`/`restore()` 完整保留引用 → 恢复后按 `ref` 回填完整消息历史。

```
save_conversation(ctx, key, messages, agent):
  blob = json.dumps(messages)
  if size(blob) > threshold:
      ref  = ConversationStore.save(blob)        # 内容寻址落盘（原子 rename）
      ctx.set(key, {"_type":"conversation_ref", "ref":ref, "meta":..., "size":len(blob)})
      # ↑ 小对象：_deep_freeze 存 FrozenDict，snapshot 完整保留
  else:
      ctx.set(key, pack_conversation(messages, agent))   # 内联冻结（原行为）

load_and_validate(ctx, from_key, agent, compat):
  raw = ctx.get(from_key)
  if raw._type == "conversation_ref":                  # 旁路：从 store 回填
      messages = json.loads(ConversationStore.load(raw.ref))
  else:                                                 # 内联：直接取
      messages = unpack_conversation(raw).messages
  check_compatibility(raw.meta, agent, compat)
  return messages, meta
```

**LocalConversationStore 实现**（镜像 `runtime/artifact.py` 的五步写序 + GCSweeper）:

```python
class LocalConversationStore:
    """内容寻址本地会话存储。同内容同 ref（去重），原子 rename 落盘。
    目录布局: {base_dir}/conversations/{ref}（与 artifacts 平级，便于统一 GC）。
    写序：写 .tmp → fsync → os.replace(.tmp → {ref}) → 返回 ref。"""
    def save(self, data: bytes) -> str:
        ref = hashlib.md5(data).hexdigest()
        path = os.path.join(self._base, "conversations", ref)
        if os.path.exists(path):       # 内容寻址去重
            return ref
        tmp = path + ".tmp"
        with open(tmp, "wb") as f: f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)          # 原子
        return ref
    def load(self, ref: str) -> bytes:
        with open(os.path.join(self._base, "conversations", ref), "rb") as f:
            return f.read()
```

**GC（ConversationGCSweeper，复用 GCSweeper 模式）**: 扫描 `conversations/` 下文件，
对 mtime 超过宽限期（默认 24h，与 artifact 一致）且不被任何 checkpoint 引用的文件删除。
checkpoint 引用集合从 `output/runs/*/checkpoint.json` 的 `store.conversation_ref` 字段
收集（一次扫描即可，对账逻辑同 `GCSweeper._collect_referenced_ids`）。

**为什么不改动 `context.py`**:
- Context 的 LargeRef 是通用大对象机制（为产物/大输出避免全量拷贝），会话只是其消费者之一。
- 旁路存储完全在 conversation 模块内闭环：`save_conversation` 在 `ctx.set` 前卸载，
  `load_and_validate` 在 `ctx.get` 后回填——Context 全程只看到小对象，LargeRef 路径
  对会话不再触发。满足 v0.2 起的"不改动 context.py"约束。
- 可扩展：`set_conversation_store()` 可替换为 S3/内存实现，跨运行复用对话历史（§10）。

**与扩展点的衔接**: 滑动窗口 / 摘要压缩（§10）仍为后续优化，用于**主动控制**会话增长
（降低 token 成本）；旁路存储解决的是**被动兜底**（无论多大都不丢历史）。二者正交、可叠加。

**可靠性保证（旗舰场景闭环）**:
- 长篇循环多轮后会话 > 1MB → 自动走旁路，Context 持 ref
- 触发断点续传 → snapshot 保留 ref（小对象，不走 LargeRef 摘要化）
- resume → restore 恢复 ref → `load_and_validate` 从 store 回填完整 messages
- 历史不再丢失；同时 `cached_tokens` meta 跨 resume 保留，命中率可观测

### 8.2 跨 Provider/Agent 一致性档位（v0.5，替代 v0.4 二元开关）

**v0.4 缺陷**: `allow_cross_provider` 只是二元开关——`true` 仅放行 + warning，
**不做任何格式转换**，真正的不兼容风险（Anthropic 连续同角色、tool_calls 结构、多模态格式）
原样丢给用户；同一 provider 换 agent 时还有"一半读旧 agent system、一半读新 agent tools"
的半状态不一致，工具 schema 按"当前新 agent"重建而 system 沿用"会话旧 agent"。

**v0.5 方案**: 二元开关升级为**一致性档位** + 完整派生属性校验 + 格式适配扩展点:

| 档位 | provider/model 不一致 | tools_sig 不一致 | system_hash 不一致 | 格式转换 |
|------|----------------------|------------------|-------------------|---------|
| `strict`（默认） | raise（正确性风险） | raise（tool_calls 错位） | warning（性能，保留会话 system） | 不做 |
| `passthrough` | warning 放行 | warning 放行 | debug | **不做**（显式声明自行承担） |

```python
def check_compatibility(conv_meta, agent, compat):
    sig = _tools_signature(agent.tools)
    sys_h = _hash_system(agent.system or "")
    pm_mismatch = (conv_meta.get("provider","") != (agent.provider or "")
                   or conv_meta.get("model","") != (agent.model or ""))
    tool_mismatch = conv_meta.get("tools_sig","") != sig
    sys_mismatch = conv_meta.get("system_hash","") != sys_h

    # 正确性风险（tools / provider / model）
    if (tool_mismatch or pm_mismatch):
        if compat == "strict":
            raise ConversationCompatError(
                f"会话与当前 agent 不一致: provider/model="
                f"{conv_meta.get('provider')}/{conv_meta.get('model')} → "
                f"{agent.provider}/{agent.model}, tools_sig 变更={tool_mismatch}。"
                f"continue/fork 会致 tool_calls 历史与 schema 错位。"
                f"如确需切换，设置 conversation.compat: passthrough 并自行承担风险。")
        logger.warning("passthrough: 会话与 agent 不一致，未做格式转换")

    # 性能风险（system）
    if sys_mismatch and not self_system_override:
        logger.warning("会话 system 与当前 agent 不一致，缓存前缀将断裂")

class ConversationCompatError(ValueError): ...
```

**关键改进**:
- **半状态根因消除**: `strict` 档校验 `tools_sig`，强制当前 agent.tools 与会话记录一致。
  tool schema 仍从当前 agent 构建，但已被校验与会话一致——"会话 system(旧) +
  当前 tools(新)"的错位在源头消除（§7.5 联动）。
- **诚实命名**: `passthrough` 明确表示"放行但不适配"，不再用 `allow_cross_provider: true`
  暗示"已处理兼容"。真正的格式适配需显式配置 `MessageNormalizer`（见下）。
- **正确性 vs 性能分离**: tools/provider 不一致是**正确性**风险（raise）；
  system 不一致是**性能**风险（warning，缓存前缀断裂但请求仍合法）。

**`MessageNormalizer` 扩展点**（为真正的格式适配留口，当前不实现）:

```python
# provider 可注册专属 normalizer，把会话消息转成目标 provider 可接受的结构
class AnthropicNormalizer(MessageNormalizer):
    def normalize(self, messages, conv_meta, agent):
        # 1. 连续同 role 消息合并（Anthropic 硬约束）
        # 2. tool_calls 结构转 Anthropic 格式
        # 3. 多模态 content part 重排
        ...
```

`strict`/`passthrough` **均不自动调用** normalizer——需显式配置才生效，避免"隐式改消息"
破坏缓存前缀稳定性。normalizer 注册后，可在加载会话后、调用 LLM 前插入一次转换，
既保留缓存前缀稳定（会话内消息不变）又解决跨 provider 结构约束。这是 §10"显式缓存断点"
之外、真正消除跨 provider 风险的演进路径。

### 8.3 fork 任意分支点（解决审核问题 2）

v0.1 的 fork 写死为"操作最后一轮"，API 形状（`strip_last_assistant` + `replace_last_user`）从根上不支持树状分支。

v0.2 的 `fork_at` 参数:
- `"last"`（默认）: 等效 v0.1 行为，操作末尾轮
- `N`（整数）: 从第 N 个 user 消息处分支（0-indexed），支持从对话中间任意位置 fork

实现基于 user 消息索引而非原始消息索引，语义清晰（"从第几轮提问处分支"），且天然适配含 tool_calls 的复杂消息序列。

### 8.4 移除隐式状态依赖（解决审核问题 1）

v0.1 的 `auto` 模式让 Step 行为隐式依赖 `ctx.has(key)`，同一 Step 在不同执行路径下语义不同。

v0.2 移除 `auto`，改为:
- 三种显式模式: `start` / `continue` / `fork`，行为静态可推理
- `has()` 表达式函数: 供 ConditionStep 显式判断会话是否存在
- 循环场景用 `condition + has()` 显式分支，虽然配置稍长，但行为完全可预测

### 8.5 不可变保障

- 会话存入 Context 时经 `_deep_freeze` 冻结（≤阈值时内联，>阈值时为 `conversation_ref` 小对象）
- `fork`/`continue` 均构造新列表，不修改源会话；旁路存储内容寻址（同内容同 ref），天然不可变
- 检查点兼容: 内联会话以 JSON 友好的 dict 存储；旁路 `conversation_ref` 恒 <1KB，
  `snapshot()`/`restore()` 完整保留引用，恢复后按 `ref` 从 `ConversationStore` 回填（见 §8.1.2）

### 8.6 错误处理

| 情况 | 行为 |
|------|------|
| `continue`/`fork` 但源会话不存在 | 抛 `ValueError`，明确提示 key 名 |
| `continue`/`fork` 但 key 存在却非会话类型 | 抛 `ConversationTypeError`，提示可能重名冲突 |
| provider/model 或 tools_sig 不匹配且 `compat: strict` | 抛 `ConversationCompatError`，提示改设 `compat: passthrough`（见 §8.2） |
| `fork_at` 超出 user 消息范围 | 抛 `ValueError`，提示有效范围 |
| Step 执行失败（`on_exhausted=raise`） | 不保存会话（异常在保存前抛出） |
| Step 输出契约失败但 `on_exhausted=skip/default` | 保存会话（含 LLM 原始输出），output 写入默认值 |
| `conversation.key` 与 Step output 重名 | 编译期 `ValueError`（静态校验拦截；模板 key 跳过字面检查，运行时兜底） |
| 会话 >阈值 | 自动走 `ConversationStore` 旁路存储，Context 仅持 `conversation_ref`，断点续传不丢历史（见 §8.1.2） |

---

## 9. 向后兼容

- 不配置 `conversation` → `conversation_mode=None` → `run()` 完全走现有路径
- 现有 YAML 文件零改动即可运行
- 现有测试不受影响
- 新增字段在 `LLMStep.__init__` 中均有默认值
- `has()` 函数仅在表达式中出现 `has(...)` 模式时生效，不影响现有表达式

---

## 10. 扩展性

预留的扩展方向（当前不实现，但设计不阻碍）:

| 扩展 | 方式 | 场景 |
|------|------|------|
| 滑动窗口 | `conversation.max_messages: 20` | 长对话控制 token 成本 |
| 摘要压缩 | `conversation.summarize_after: 10` | 超过 N 轮自动摘要旧消息 |
| 显式缓存断点 | `conversation.cache_breakpoints: true` | Anthropic 式显式 cache_control 标记 |
| 会话持久化 | conversation store 接口 | 跨运行复用对话历史 |
| YAML 锚点消除重复 | `&ref` / `*ref` 引用 step 配置 | 消除 condition 分支中的配置重复 |

---

## 11. 改动范围

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `agentkit/core/conversation.py` | **新增** | 会话序列化、操作、校验工具 |
| `agentkit/core/template.py` | 修改 | `eval_expression` 新增 `has()` 预处理 |
| `agentkit/steps/llm_step.py` | 修改 | 新增 conversation 参数 + 消息构建/保存逻辑 |
| `agentkit/yaml/loader.py` | 修改 | 解析 `conversation` 配置块 + 静态校验 |
| `agentkit/tests/test_llm_step.py` | 修改 | 新增会话模式测试 |
| `agentkit/tests/test_condition_step.py` | 修改 | 新增 `has()` 表达式测试 |
| `examples/story_best_practice.yaml` | 可选 | 改造为 conversation 版本（示例） |

**不改动**: `context.py` / `base.py` / `loop_step.py` / `parallel_step.py` / `llm/base.py` / `llm/openai.py` 等核心模块。

---

## 12. 审核反馈与修订记录

### v0.1 → v0.2 变更

| 审核问题 | 是否客观存在 | 修订措施 |
|---------|------------|---------|
| **1. auto 模式隐式状态依赖** | 是。auto 让 Step 行为依赖运行时 `ctx.has(key)`，重试/条件分支/子流程复用下语义会变 | 移除 `auto` 模式；新增 `has()` 表达式函数供 ConditionStep 显式判断；循环场景用 `condition + has()` 显式分支 |
| **2. fork 写死操作最后一轮** | 是。`strip_last_assistant` + `replace_last_user` 的 API 形状从根上不支持中间分支 | 新增 `fork_at` 参数（`"last"` \| `N`），按 user 消息索引分支，支持任意位置 |
| **3. 命名空间无隔离** | 是。conversation.key 与普通 output 共用 Context，无类型区分，冲突时报错信息不相关 | 三层防护: 类型标记 `_type: "conversation"` + YAML 静态校验（编译期检测重名）+ 运行时类型校验（`ConversationTypeError`） |
| **4. 跨 provider 被当作性能问题** | 是。不同 provider 消息结构约束不同，跨 provider 是正确性风险而非仅缓存未命中 | 会话 meta 存储 provider/model；continue/fork 时校验一致性，默认不匹配则 raise；`allow_cross_provider: true` 可显式放行 |

### v0.2 待审核要点

1. **移除 auto 后循环场景的 UX**: 用 `condition + has()` 显式分支导致 step 配置重复（agent/prompt/output 在 then/else 各写一遍），是否可接受？未来可用 YAML 锚点消除
2. **fork_at 用 user 消息索引**: 是否比"轮次号"或"原始消息索引"更直观？user 索引对含 tool_calls 的复杂序列更健壮
3. **provider 校验默认 raise**: 是否过于严格？还是应该默认 warning + `allow_cross_provider: true` 放行？
4. **类型标记 `_type: "conversation"`**: 是否与 Context 现有的 `_to_jsonable` / `_from_jsonable` 类型标记机制冲突？（需检查 `_from_jsonable` 是否会误还原）

### v0.3 → v0.4 再次验证结论

对 v0.2 审核反馈的三项修复逐一进行了源码级二次验证:

| 审核问题 | v0.3 修复 | 二次验证结论 | 源码依据 |
|---------|----------|------------|---------|
| **5. `_type` 与序列化兼容性** | §8.1.1 源码级验证 | ✅ **成立**。`_from_jsonable` 三个分支（`set`+`_items`、`bytes`+`_data`、任意+`_repr`）均不匹配 `{"_type": "conversation", "messages": [...], "meta": {...}}` 格式。会话不含 `_items`/`_data`/`_repr` 键，落入"普通 dict 递归处理"分支 | `context.py:384-420` |
| **6. condition 分支一致性** | §7.8 校验 B | ✅ **成立**。`_validate_condition_branch_consistency` 正确约束 `conversation.key` 和 `output` 一致，允许 `prompt`/`agent` 不同。`walk_all_steps` 递归发现所有层级的 ConditionStep | `condition_step.py:90-91`（`then_steps`/`else_steps` 属性名匹配） |
| **7. 静态校验手写递归** | §7.7 `iter_child_steps()` | ✅ **成立**。BaseStep 新增默认空列表方法，LoopStep(`self.body`)、ParallelStep(`self.branches`)、ConditionStep(`self.then_steps`+`self.else_steps`) 各自重写。`walk_all_steps` 通用递归替代 case-by-case 硬编码 | `loop_step.py:137`、`parallel_step.py:110`、`condition_step.py:90-91` 属性名全部匹配 |

**v0.4 新发现问题**: 上述验证只覆盖了小对象路径。Context 对 >1MB 对象走 LargeRef 路径，`snapshot()` 只存摘要不存数据，导致大会话断点续传后变为占位 dict，`unpack_conversation` 校验失败。已在 §8.1.2 补充运行时检测+告警方案（不改动 `context.py`，在 conversation 模块 `_save_conversation` 中检测大小并 warning）。

### v0.4 → v0.5 根因修复（针对 v0.4 三项结构性缺陷）

用户指出 v0.4 存在三项结构性缺陷（①Anthropic 缓存收益缺口不在本次修复范围），
要求在易用性、可拓展性、稳定性前提下做根因修复:

| 缺陷 | v0.4 现状（根因） | v0.5 修复 | 章节 |
|------|-----------------|----------|------|
| **②静态 key 撑不住动态多 provider** | `key`/`from` 为静态 str，唯一多会话例子是手写 2×P 份 step；无机制处理"运行期数量不确定的 provider/persona" | `key`/`from` 支持 `{{var}}` 模板渲染（与 prompt 同通路）；`has()` 参数同样支持模板；一份 step 即可管理运行期动态列表 | §4.1 / §7.4 |
| **③跨 provider 校验是二元开关** | `allow_cross_provider` 仅放行 + warning，不做格式转换；同一 provider 换 agent 有"一半读旧 system / 一半读新 tools"半状态 | 升级为 `strict`/`passthrough` 一致性档位；新增 `tools_sig` + `system_hash` 校验，strict 档 raise 杜绝半状态；`MessageNormalizer` 扩展点为真适配留口（当前不自动调用） | §8.2 / §7.5 |
| **④大会话断点续传丢历史** | v0.4 仅在 `_save_conversation` 打一行 `logger.warning`，"该丢的还是丢"；旗舰场景长篇循环恰好最易越过 1MB | `ConversationStore` 旁路存储：>阈值会话在 `ctx.set` 前卸载到内容寻址存储，Context 仅持 <1KB 的 `conversation_ref`，绕过 LargeRef 摘要化，断点续传完整回填历史 | §8.1.2 / §7.1 |

**附加改进（稳定性/可观测性）**:
- 缓存命中可观测：`LLMUsage.cached_tokens` 累计回写会话 `meta`，跨轮叠加，
  trace 可读，消除"缓存收益悄悄归零无人发现"（§7.9）。Anthropic 显式
  `cache_control` 仍属 §10 扩展项，不在本次范围（缺陷①已确认不修）。
- 静态校验对模板 key 适配：含 `{{` 的 key 跳过字面碰撞检查，改由运行时 `_type` 兜底（§7.8）。
