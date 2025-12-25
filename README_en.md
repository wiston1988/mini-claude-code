# Mini Claude Code

English | [中文](./README.md)

> Production-ready: [Kode - Open Source Agent CLI](https://github.com/shareAI-lab/Kode)

## Overview

This repository is a **learning-focused** recreation of Claude Code's core mechanisms. Through three progressive versions (~900 lines of Python total), it demonstrates the key design patterns behind AI coding agents:

| Version | Lines | Core Theme | Learning Objective |
|---------|-------|------------|-------------------|
| v1 | ~400 | Model as Agent | Understand the tool loop |
| v2 | +170 | Structured Planning | Master Todo tool & system reminders |
| v3 | +120 | Subagent Mechanism | Learn Task tool & context isolation |

## Quick Start

```bash
# Install dependencies
pip install anthropic

# Configure API (edit ANTHROPIC_API_KEY in the code)
# Supports Anthropic API or compatible endpoints (e.g., Moonshot Kimi)

# Run any version
python v1_basic_agent.py
python v2_todo_agent.py
python v3_subagent.py
```

## File Structure

```
mini-claude-code/
├── v1_basic_agent.py      # Baseline: 4 core tools + main loop
├── v2_todo_agent.py       # Todo version: +TodoManager + System Reminder
├── v3_subagent.py         # Subagent version: +Task tool + Agent Registry
├── articles/              # Tutorial articles (Chinese)
│   ├── v1文章.md
│   ├── v2文章.md
│   └── v3文章.md
└── demo/                  # AI-generated game demos
```

---

## V1: Minimal Model-as-Agent Implementation

**File**: `v1_basic_agent.py` (~400 lines)

**Core Idea**: Code supplies tools; the model is the sole decision-maker.

### 1.1 System Prompt

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

This prompt establishes behavioral boundaries:
- Explicit working directory prevents the model from hallucinating file paths
- "Tools first" mandate prevents verbose explanations without action
- Summary requirement ensures user can verify results

### 1.2 Four Core Tools

| Tool | Purpose | Key Parameters |
|------|---------|----------------|
| `bash` | Execute shell commands | command, timeout_ms |
| `read_file` | Read text files | path, start_line, end_line |
| `write_file` | Create/overwrite files | path, content, mode |
| `edit_text` | Precise text edits | path, action(replace/insert/delete_range) |

Each tool includes safety checks:
- **Path sandboxing**: `safe_path()` ensures all operations stay within workspace
- **Command filtering**: Blocks `rm -rf /`, `sudo`, and other dangerous commands
- **Output clamping**: Truncates results exceeding 100KB

### 1.3 Main Loop Logic

```python
def query(messages):
    while True:
        response = client.messages.create(model, system, messages, tools)

        # Process text output
        for block in response.content:
            if block.type == "text":
                print(block.text)
            if block.type == "tool_use":
                tool_uses.append(block)

        # If model requests tools, execute and continue
        if response.stop_reason == "tool_use":
            results = [dispatch_tool(tu) for tu in tool_uses]
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": results})
            continue

        # Otherwise return final result
        return messages
```

This loop is the agent's heart: the model keeps calling tools until the task completes with plain text.

---

## V2: Structured Planning and System Reminders

**File**: `v2_todo_agent.py` (~570 lines, +170 new)

**Core Idea**: Use the Todo tool to keep the model anchored in a structured workflow.

### 2.1 TodoManager Class

```python
class TodoManager:
    def __init__(self):
        self.items = []  # Max 20 items

    def update(self, items):
        # Validation rules:
        # - Each item must have content, status, activeForm
        # - Status must be pending/in_progress/completed
        # - Only one item can be in_progress at a time
        # - IDs must be unique
```

These constraints force the model to follow conventions instead of arbitrary output.

### 2.2 TodoWrite Tool

```python
{
    "name": "TodoWrite",
    "input_schema": {
        "items": [{
            "content": "Task description",
            "status": "pending | in_progress | completed",
            "activeForm": "Present tense (e.g., Reading files)"
        }]
    }
}
```

The model calls this tool to create and update task lists. The CLI renders colored status:
- `pending` → Gray `[ ]`
- `in_progress` → Blue `[ ]`
- `completed` → Green `[x]` (with strikethrough)

### 2.3 System Reminder Mechanism

```python
INITIAL_REMINDER = '<reminder>Use Todo tool for multi-step tasks</reminder>'
NAG_REMINDER = '<reminder>10+ turns without Todo update, please resume planning</reminder>'

# In main loop:
if rounds_without_todo > 10:
    inject_reminder(NAG_REMINDER)
```

This mechanism ensures the model doesn't forget to use Todo during long conversations.

---

## V3: Subagent Mechanism

**File**: `v3_subagent.py` (~900 lines, +120 new)

**Core Idea**: Divide and conquer + context isolation.

### 3.1 Agent Type Registry

```python
AGENT_TYPES = {
    "explore": {
        "description": "Read-only agent for exploring codebases",
        "tools": ["bash", "read_file"],  # Tool whitelist
        "system_prompt": "You are an exploration agent, read-only..."
    },
    "code": {
        "tools": "*",  # All tools
        "system_prompt": "You are a coding agent, implement features..."
    },
    "plan": {
        "tools": ["bash", "read_file"],
        "system_prompt": "You are a planning agent, analyze and output plans..."
    }
}
```

Each agent type has its own tool permissions and system prompt.

### 3.2 Task Tool Definition

```python
{
    "name": "Task",
    "input_schema": {
        "description": "Short description (3-5 words)",
        "prompt": "Detailed instructions",
        "subagent_type": "explore | code | plan"
    }
}
```

The main agent dispatches subtasks by calling the Task tool.

### 3.3 Subagent Execution Core (run_task)

```python
def run_task(inp, depth=0):
    agent_type = inp["subagent_type"]
    agent_config = AGENT_TYPES[agent_type]

    # 1. Build agent-specific system prompt
    sub_system = f"You are a {agent_type} subagent...\n{agent_config['system_prompt']}"

    # 2. Filter available tools
    sub_tools = get_tools_for_agent(agent_type)

    # 3. Create isolated message history (KEY!)
    sub_messages = [{"role": "user", "content": prompt}]

    # 4. Recursively call the same query loop (silent mode)
    result_messages = query(sub_messages, sub_system, sub_tools, silent=True)

    # 5. Return only final text to parent agent
    return extract_final_text(result_messages)
```

**Core Design Principles**:
- **Message isolation**: Subagent cannot see parent conversation, and won't pollute it
- **Tool filtering**: Explore agent can only read, not write
- **Recursive reuse**: Subagent uses the same query() function
- **Result abstraction**: Subagent may call 20 tools but returns only a summary

### 3.4 Silent Mode and Progress Display

```python
class SubagentProgress:
    def update(self, tool_name, tool_arg):
        # Overwrite same line, don't pollute main chat
        line = f"  | {tool_name}({tool_arg}) (+{count} tools, {elapsed}s)"
        sys.stdout.write("\r" + line)
```

Subagent execution looks like:
```
@ Task(explore: Explore codebase)...
  | Read(README.md) (+3 tool uses, 2.1s)   <- Real-time refresh
  | completed: 8 tool calls in 15.2s       <- Final summary
```

---

## Comparison with Claude Code / Kode

| Feature | Claude Code / Kode | Mini Claude Code |
|---------|-------------------|------------------|
| Agent Registry | AgentConfig + YAML files | Python dict |
| Task Parameters | +model, +resume, +run_in_background | Only 3 basic params |
| Tool Filtering | Whitelist + Blacklist | Whitelist only |
| Background Execution | Async + TaskOutput | Omitted (sync) |
| Resume | Transcript storage/restore | Omitted |
| forkContext | Can pass parent context | Omitted |

Omitted features are advanced capabilities that don't affect understanding of core mechanisms.

---

## Tutorial Articles

For detailed design rationale and code walkthrough, see the `articles/` directory:

1. **v1文章.md**: No secrets in Claude Code! 400-line Model-as-Agent implementation
2. **v2文章.md**: Todo tool for model self-discipline
3. **v3文章.md**: Unveiling Subagent mechanism in 120 lines

---

## Related Resources

- **Production Implementation**: [Kode - Open Source Agent CLI](https://github.com/shareAI-lab/Kode)
- **X/Twitter**: [@baicai003](https://x.com/baicai003)

## License

MIT
