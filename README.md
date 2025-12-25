# 手搓迷你 Claude Code

[English](./README_en.md) | 中文

> 生产级实现：[Kode - 开源 Agent CLI、 AI 编码 & AI 运维工具](https://github.com/shareAI-lab/Kode)

<img width="360" height="360" alt="image" src="https://github.com/user-attachments/assets/9813fca0-a6dd-4813-972e-f9bf6d62add8" />

关注我们在 X 上：https://x.com/baicai003

<img height="450" alt="image" src="https://github.com/user-attachments/assets/0e1e31f8-064f-4908-92ce-121e2eb8d453" />

---

## 项目简介

本仓库是一个**教学导向**的 Claude Code 核心机制复现项目。通过三个递进版本（约 900 行 Python），完整演示 AI 编程代理的关键设计模式：

| 版本 | 代码量 | 核心主题 | 学习目标 |
|------|--------|----------|----------|
| v1 | ~400 行 | Model as Agent | 理解工具循环的本质 |
| v2 | +170 行 | 结构化规划 | 掌握 Todo 工具与系统提醒 |
| v3 | +150 行 | 子代理机制 | 学会 Task 工具与上下文隔离 |

## 快速开始

```bash
# 安装依赖
pip install anthropic

# 配置 API（修改代码中的 ANTHROPIC_API_KEY）
# 支持 Anthropic 官方 API 或兼容接口（如 Moonshot Kimi）

# 运行任意版本
python v1_basic_agent.py
python v2_todo_agent.py
python v3_subagent.py
```

## 核心文件说明

```
mini-claude-code/
├── v1_basic_agent.py      # 基础版：4 个核心工具 + 主循环
├── v2_todo_agent.py       # 待办版：+TodoManager + System Reminder
├── v3_subagent.py         # 子代理版：+Task 工具 + Agent Registry
├── articles/              # 配套教学文章
│   ├── v1文章.md
│   ├── v2文章.md
│   └── v3文章.md
└── demo/                  # AI 生成的游戏示例
```

---

## V1：Model as Agent 的最小实现

**文件**：`v1_basic_agent.py`（约 400 行）

**核心思想**：代码只提供工具，模型才是唯一的决策主体。

### 1.1 系统提示词（System Prompt）

```python
SYSTEM = (
    f"You are a coding agent operating INSIDE the user's repository at {WORKDIR}.\n"
    "Follow this loop strictly: plan briefly -> use TOOLS to act -> report results.\n"
    "Rules:\n"
    "- Prefer taking actions with tools over long prose.\n"
    "- Never invent file paths. Read directories first if unsure.\n"
    "- After finishing, summarize what changed."
)
```

这段提示词定义了模型的行为边界：
- 明确工作目录，防止模型"幻想"不存在的文件
- 强制"工具优先"，避免长篇大论而不行动
- 要求结束时总结，便于用户确认结果

### 1.2 四个核心工具

| 工具名 | 功能 | 关键参数 |
|--------|------|----------|
| `bash` | 执行 Shell 命令 | command, timeout_ms |
| `read_file` | 读取文本文件 | path, start_line, end_line |
| `write_file` | 创建/覆盖文件 | path, content, mode |
| `edit_text` | 精确文本编辑 | path, action(replace/insert/delete_range) |

每个工具都包含安全检查：
- **路径沙箱**：`safe_path()` 确保所有操作在工作目录内
- **命令过滤**：禁止 `rm -rf /`、`sudo` 等危险命令
- **输出限制**：超过 100KB 自动截断

### 1.3 主循环逻辑

```python
def query(messages):
    while True:
        response = client.messages.create(model, system, messages, tools)

        # 处理文本输出
        for block in response.content:
            if block.type == "text":
                print(block.text)
            if block.type == "tool_use":
                tool_uses.append(block)

        # 如果模型请求工具，执行后继续循环
        if response.stop_reason == "tool_use":
            results = [dispatch_tool(tu) for tu in tool_uses]
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": results})
            continue

        # 否则返回最终结果
        return messages
```

这个循环是 Agent 的核心：模型持续调用工具，直到任务完成返回纯文本。

---

## V2：结构化规划与系统提醒

**文件**：`v2_todo_agent.py`（约 570 行，新增 ~170 行）

**核心思想**：用 Todo 工具把模型"拴"在结构化工作流里。

### 2.1 TodoManager 类

```python
class TodoManager:
    def __init__(self):
        self.items = []  # 最多 20 条

    def update(self, items):
        # 校验规则：
        # - 每条必须有 content, status, activeForm
        # - status 只能是 pending/in_progress/completed
        # - 同时只能有一个 in_progress 状态
        # - ID 不能重复
```

这些约束迫使模型遵循规范，而不是随意输出。

### 2.2 TodoWrite 工具

```python
{
    "name": "TodoWrite",
    "input_schema": {
        "items": [{
            "content": "任务描述",
            "status": "pending | in_progress | completed",
            "activeForm": "进行时描述（如：正在读取文件）"
        }]
    }
}
```

模型通过调用此工具来创建和更新任务列表，CLI 会实时渲染彩色状态：
- `pending` → 灰色 `[ ]`
- `in_progress` → 蓝色 `[ ]`
- `completed` → 绿色 `[x]`（带删除线）

### 2.3 系统提醒机制

```python
INITIAL_REMINDER = '<reminder>请使用 Todo 工具管理多步骤任务</reminder>'
NAG_REMINDER = '<reminder>已超过 10 轮未更新 Todo，请恢复规划</reminder>'

# 在主循环中：
if rounds_without_todo > 10:
    inject_reminder(NAG_REMINDER)
```

这套机制确保模型在长对话中不会忘记使用 Todo 工具。

---

## V3：子代理机制（Subagent）

**文件**：`v3_subagent.py`（约 900 行，新增 ~150 行）

**核心思想**：分而治之 + 上下文隔离。

### 3.1 Agent 类型注册表

```python
AGENT_TYPES = {
    "explore": {
        "description": "只读代理，用于探索代码库",
        "tools": ["bash", "read_file"],  # 工具白名单
        "system_prompt": "你是探索代理，只能读取不能修改..."
    },
    "code": {
        "tools": "*",  # 所有工具
        "system_prompt": "你是编码代理，负责实现功能..."
    },
    "plan": {
        "tools": ["bash", "read_file"],
        "system_prompt": "你是规划代理，分析代码并输出计划..."
    }
}
```

每种代理类型有独立的工具权限和系统提示词。

### 3.2 Task 工具定义

```python
{
    "name": "Task",
    "input_schema": {
        "description": "简短描述（3-5 词）",
        "prompt": "详细指令",
        "subagent_type": "explore | code | plan"
    }
}
```

主代理通过调用 Task 工具来派发子任务。

### 3.3 子代理执行核心（run_task）

```python
def run_task(inp, depth=0):
    agent_type = inp["subagent_type"]
    agent_config = AGENT_TYPES[agent_type]

    # 1. 构造专属系统提示词
    sub_system = f"You are a {agent_type} subagent...\n{agent_config['system_prompt']}"

    # 2. 过滤可用工具
    sub_tools = get_tools_for_agent(agent_type)

    # 3. 创建隔离的消息历史（关键！）
    sub_messages = [{"role": "user", "content": prompt}]

    # 4. 递归调用同一个 query 循环（静默模式）
    result_messages = query(sub_messages, sub_system, sub_tools, silent=True)

    # 5. 只返回最终文本给主代理
    return extract_final_text(result_messages)
```

**核心设计**：
- **消息隔离**：子代理看不到主对话，也不会污染主对话
- **工具过滤**：explore 代理只能读不能写
- **递归复用**：子代理用同一个 query() 函数
- **结果抽象**：子代理可能调用了 20 次工具，但只返回一段总结

### 3.4 静默模式与进度显示

```python
class SubagentProgress:
    def update(self, tool_name, tool_arg):
        # 覆盖更新同一行，不污染主聊天区
        line = f"  | {tool_name}({tool_arg}) (+{count} tools, {elapsed}s)"
        sys.stdout.write("\r" + line)
```

子代理执行时的效果：
```
@ Task(explore: 探索代码库)...
  | Read(README.md) (+3 tool uses, 2.1s)   <- 实时刷新
  | completed: 8 tool calls in 15.2s       <- 最终摘要
```

---

## 与 Claude Code / Kode 的对比

| 机制 | Claude Code / Kode | Mini Claude Code |
|------|-------------------|------------------|
| Agent Registry | AgentConfig + YAML 文件 | Python dict |
| Task 参数 | +model, +resume, +run_in_background | 仅基础 3 参数 |
| 工具过滤 | 白名单 + 黑名单 | 白名单 |
| 后台执行 | 支持异步 + TaskOutput | 省略（同步） |
| Resume | transcript 存储恢复 | 省略 |
| forkContext | 可传父对话上下文 | 省略 |

省略的功能都是高级特性，不影响理解核心机制。

---

## 配套教学文章

详细的设计思路和代码解析请参阅 `articles/` 目录：

1. **v1文章.md**：Claude Code 没有秘密！Model as Agent 的 400 行实现
2. **v2文章.md**：让模型自我约束的 Todo 工具
3. **v3文章.md**：用 150 行代码揭开 Subagent 的神秘面纱

---

## 相关资源

- **生产级实现**：[Kode - 开源 Agent CLI](https://github.com/shareAI-lab/Kode)
- **X/Twitter**：[@baicai003](https://x.com/baicai003)

## License

MIT
