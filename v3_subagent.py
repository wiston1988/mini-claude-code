#!/usr/bin/env python3
"""
v3_subagent.py - Minimal Subagent Implementation

This version adds Task tool support, demonstrating how Claude Code's subagent
mechanism works with ~150 lines of new code. Key concepts:

1. AGENT_TYPES: Registry defining agent capabilities and tool access
2. Task tool: Spawns child agents with isolated message history
3. Tool filtering: Each agent type has its own tool whitelist
4. Recursive query: Subagents reuse the same query() loop

The subagent pattern enables:
- Parallel exploration (multiple read-only agents)
- Separation of concerns (explore vs implement)
- Cost optimization (use cheaper models for simple tasks)
"""

import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from anthropic import Anthropic
except Exception as exc:
    sys.stderr.write("Install with: pip install anthropic\n")
    raise exc

ANTHROPIC_BASE_URL = "https://api.moonshot.cn/anthropic"
ANTHROPIC_API_KEY = (
    "sk-HBfTVqsZ4dmrb3QvQIqoycTD0CGxUYT3QCP0Ui5ZYw9pQNqY"  # Replace with your API key
)
AGENT_MODEL = "kimi-k2-turbo-preview"

WORKDIR = Path.cwd()
MAX_TOOL_RESULT_CHARS = 100_000
TODO_STATUSES = ("pending", "in_progress", "completed")

RESET = "\x1b[0m"
PRIMARY_COLOR = "\x1b[38;2;120;200;255m"
ACCENT_COLOR = "\x1b[38;2;150;140;255m"
INFO_COLOR = "\x1b[38;2;110;110;110m"
PROMPT_COLOR = "\x1b[38;2;120;200;255m"
SUBAGENT_COLOR = "\x1b[38;2;255;180;100m"
TODO_PENDING_COLOR = "\x1b[38;2;176;176;176m"
TODO_PROGRESS_COLOR = "\x1b[38;2;120;200;255m"
TODO_COMPLETED_COLOR = "\x1b[38;2;34;139;34m"

MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
MD_CODE = re.compile(r"`([^`]+)`")
MD_HEADING = re.compile(r"^(#{1,6})\s*(.+)$", re.MULTILINE)
MD_BULLET = re.compile(r"^\s*[-\*]\s+", re.MULTILINE)


# ============================================================================
# SUBAGENT PROGRESS TRACKER - Kode-style inline progress display
# ============================================================================
class SubagentProgress:
    """
    Manages subagent output display in Kode style:
    - Shows a single progress line that updates in place
    - Collects tool calls for summary
    - Does NOT pollute main chat area
    """

    def __init__(self, agent_type: str, description: str):
        self.agent_type = agent_type
        self.description = description
        self.tool_calls: List[str] = []
        self.start_time = time.time()
        self._last_line_len = 0

    def update(self, tool_name: str, tool_arg: str | None = None) -> None:
        """Record a tool call and update the progress line."""
        display = f"{tool_name}({tool_arg})" if tool_arg else tool_name
        self.tool_calls.append(display)
        self._render_progress()

    def _render_progress(self) -> None:
        """Render the current progress line (overwrites previous)."""
        elapsed = time.time() - self.start_time
        count = len(self.tool_calls)
        last_tool = self.tool_calls[-1] if self.tool_calls else "starting..."

        # Truncate last_tool if too long
        max_tool_len = 40
        if len(last_tool) > max_tool_len:
            last_tool = last_tool[: max_tool_len - 3] + "..."

        line = f"{INFO_COLOR}  | {last_tool} (+{count} tool uses, {elapsed:.1f}s){RESET}"

        # Clear previous line and write new one
        sys.stdout.write("\r" + " " * self._last_line_len + "\r")
        sys.stdout.write(line)
        sys.stdout.flush()
        self._last_line_len = len(line) + 10  # Account for ANSI codes

    def finish(self) -> str:
        """Finalize and return summary line."""
        elapsed = time.time() - self.start_time
        count = len(self.tool_calls)

        # Clear the progress line
        sys.stdout.write("\r" + " " * self._last_line_len + "\r")
        sys.stdout.flush()

        # Return summary
        return f"{INFO_COLOR}  | completed: {count} tool calls in {elapsed:.1f}s{RESET}"


# Global reference for current subagent progress (if any)
_current_subagent_progress: SubagentProgress | None = None


# ============================================================================
# AGENT TYPE REGISTRY - Core of subagent mechanism
# ============================================================================
# Each agent type defines:
# - description: When to use this agent (shown in Task tool prompt)
# - tools: List of allowed tools ("*" = all tools)
# - system_prompt: Agent-specific instructions
# ============================================================================
AGENT_TYPES: Dict[str, Dict[str, Any]] = {
    "explore": {
        "description": "Fast read-only agent for exploring codebases, finding files, and searching code",
        "tools": ["bash", "read_file"],  # Read-only tools only
        "system_prompt": (
            "You are an exploration agent. Your job is to quickly search and understand code.\n"
            "Rules:\n"
            "- Only use read-only operations (bash for grep/find/ls, read_file)\n"
            "- Never modify files\n"
            "- Return a concise, structured summary of findings\n"
            "- Focus on answering the specific question asked"
        ),
    },
    "code": {
        "description": "Full-featured coding agent for implementing features, fixing bugs, and refactoring",
        "tools": "*",  # All tools available
        "system_prompt": (
            "You are a coding agent. Implement the requested changes efficiently.\n"
            "Rules:\n"
            "- Make minimal, focused changes\n"
            "- Test your changes when possible\n"
            "- Report what was changed concisely"
        ),
    },
    "plan": {
        "description": "Planning agent for designing implementation strategies before coding",
        "tools": ["bash", "read_file"],  # Read-only for analysis
        "system_prompt": (
            "You are a planning agent. Analyze the codebase and design implementation plans.\n"
            "Rules:\n"
            "- Read relevant files to understand the codebase\n"
            "- Output a numbered step-by-step plan\n"
            "- Identify key files that need changes\n"
            "- Do NOT make any changes yourself"
        ),
    },
}


def get_agent_descriptions() -> str:
    """Generate agent type descriptions for Task tool prompt."""
    lines = []
    for name, config in AGENT_TYPES.items():
        tools = config["tools"] if config["tools"] != "*" else "All tools"
        lines.append(f"- {name}: {config['description']} (Tools: {tools})")
    return "\n".join(lines)


# ============================================================================
# Utility functions (same as v2)
# ============================================================================
def clear_screen() -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\033c")
        sys.stdout.flush()


def render_banner(title: str, subtitle: str | None = None) -> None:
    print(f"{PRIMARY_COLOR}{title}{RESET}")
    if subtitle:
        print(f"{ACCENT_COLOR}{subtitle}{RESET}")
    print()


def user_prompt_label() -> str:
    return f"{ACCENT_COLOR}{RESET} {PROMPT_COLOR}User{RESET}{INFO_COLOR} >> {RESET}"


def format_markdown(text: str) -> str:
    if not text or text.lstrip().startswith("\x1b"):
        return text

    def bold_repl(m):
        return f"\x1b[1m{m.group(1)}\x1b[0m"

    def code_repl(m):
        return f"\x1b[38;2;255;214;102m{m.group(1)}\x1b[0m"

    def heading_repl(m):
        return f"\x1b[1m{m.group(2)}\x1b[0m"

    formatted = MD_BOLD.sub(bold_repl, text)
    formatted = MD_CODE.sub(code_repl, formatted)
    formatted = MD_HEADING.sub(heading_repl, formatted)
    formatted = MD_BULLET.sub("- ", formatted)
    return formatted


def safe_path(path_value: str) -> Path:
    abs_path = (WORKDIR / str(path_value or "")).resolve()
    if not abs_path.is_relative_to(WORKDIR):
        raise ValueError("Path escapes workspace")
    return abs_path


def clamp_text(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...<truncated {len(text) - limit} chars>"


def pretty_tool_line(kind: str, title: str | None, indent: int = 0) -> None:
    prefix = "  " * indent
    body = f"{kind}({title})..." if title else kind
    glow = f"{ACCENT_COLOR}\x1b[1m"
    print(f"{prefix}{glow}@ {body}{RESET}")


def pretty_sub_line(text: str, indent: int = 0) -> None:
    prefix = "  " * indent
    for line in text.splitlines() or [""]:
        print(f"{prefix}  | {format_markdown(line)}")


class Spinner:
    def __init__(self, label: str = "Waiting for model") -> None:
        self.label = label
        self.frames = ["@", "@@", "@@@", "@@", "@"]
        self.color = "\x1b[38;2;255;229;92m"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not sys.stdout.isatty() or self._thread:
            return
        self._stop.clear()

        def run():
            start = time.time()
            i = 0
            while not self._stop.is_set():
                elapsed = time.time() - start
                sys.stdout.write(
                    f"\r{self.color}{self.frames[i % len(self.frames)]} {self.label} ({elapsed:.1f}s)\x1b[0m"
                )
                sys.stdout.flush()
                i += 1
                time.sleep(0.15)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._thread:
            return
        self._stop.set()
        self._thread.join(timeout=1)
        self._thread = None
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()


def block_to_dict(block: Any) -> Dict[str, Any]:
    if isinstance(block, dict):
        return block
    result = {}
    for key in ("type", "text", "id", "name", "input"):
        if hasattr(block, key):
            result[key] = getattr(block, key)
    return result


def normalize_content_list(content: Any) -> List[Dict[str, Any]]:
    try:
        return [block_to_dict(item) for item in (content or [])]
    except Exception:
        return []


# ============================================================================
# API Client
# ============================================================================
api_key = ANTHROPIC_API_KEY
if not api_key:
    sys.stderr.write("ANTHROPIC_API_KEY not set\n")
    sys.exit(1)

client = Anthropic(api_key=api_key, base_url=ANTHROPIC_BASE_URL)


# ============================================================================
# System Prompt
# ============================================================================
SYSTEM = (
    f"You are a coding agent operating INSIDE the user's repository at {WORKDIR}.\n"
    "Follow this loop: plan briefly -> use TOOLS to act -> report results.\n\n"
    "Rules:\n"
    "- Prefer taking actions with tools over long prose\n"
    "- Use the Task tool to spawn subagents for complex subtasks\n"
    "- Use TodoWrite to track multi-step work\n"
    "- After finishing, summarize what changed\n\n"
    f"Available agent types for Task tool:\n{get_agent_descriptions()}"
)


# ============================================================================
# TodoManager (same as v2)
# ============================================================================
class TodoManager:
    def __init__(self) -> None:
        self.items: List[Dict[str, str]] = []

    def update(self, items: List[Dict[str, Any]]) -> str:
        if not isinstance(items, list):
            raise ValueError("Todo items must be a list")
        cleaned, seen_ids, in_progress = [], set(), 0
        for i, raw in enumerate(items):
            if not isinstance(raw, dict):
                raise ValueError("Each todo must be an object")
            tid = str(raw.get("id") or i + 1)
            if tid in seen_ids:
                raise ValueError(f"Duplicate todo id: {tid}")
            seen_ids.add(tid)
            content = str(raw.get("content") or "").strip()
            if not content:
                raise ValueError("Todo content cannot be empty")
            status = str(raw.get("status") or "pending").lower()
            if status not in TODO_STATUSES:
                raise ValueError(f"Invalid status: {status}")
            if status == "in_progress":
                in_progress += 1
            active_form = str(raw.get("activeForm") or "").strip()
            if not active_form:
                raise ValueError("activeForm cannot be empty")
            cleaned.append(
                {
                    "id": tid,
                    "content": content,
                    "status": status,
                    "active_form": active_form,
                }
            )
            if len(cleaned) > 20:
                raise ValueError("Max 20 todos")
        if in_progress > 1:
            raise ValueError("Only one task can be in_progress")
        self.items = cleaned
        return self.render()

    def render(self) -> str:
        if not self.items:
            return f"{TODO_PENDING_COLOR}[ ] No todos{RESET}"
        lines = []
        for t in self.items:
            mark = "[x]" if t["status"] == "completed" else "[ ]"
            if t["status"] == "completed":
                lines.append(f"{TODO_COMPLETED_COLOR}{mark} {t['content']}{RESET}")
            elif t["status"] == "in_progress":
                lines.append(f"{TODO_PROGRESS_COLOR}{mark} {t['content']}{RESET}")
            else:
                lines.append(f"{TODO_PENDING_COLOR}{mark} {t['content']}{RESET}")
        return "\n".join(lines)

    def stats(self) -> Dict[str, int]:
        return {
            "total": len(self.items),
            "completed": sum(t["status"] == "completed" for t in self.items),
            "in_progress": sum(t["status"] == "in_progress" for t in self.items),
        }


TODO_BOARD = TodoManager()


# ============================================================================
# TOOL DEFINITIONS
# ============================================================================
BASE_TOOLS = [
    {
        "name": "bash",
        "description": "Execute a shell command in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command"},
                "timeout_ms": {"type": "integer", "minimum": 1000, "maximum": 120000},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a UTF-8 text file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["overwrite", "append"]},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_text",
        "description": "Small, precise text edits (replace/insert/delete_range).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["replace", "insert", "delete_range"],
                },
                "find": {"type": "string"},
                "replace": {"type": "string"},
                "insert_after": {"type": "integer"},
                "new_text": {"type": "string"},
                "range": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 2,
                    "maxItems": 2,
                },
            },
            "required": ["path", "action"],
        },
    },
    {
        "name": "TodoWrite",
        "description": "Update the shared todo list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "content": {"type": "string"},
                            "activeForm": {"type": "string"},
                            "status": {"type": "string", "enum": list(TODO_STATUSES)},
                        },
                        "required": ["content", "activeForm", "status"],
                    },
                }
            },
            "required": ["items"],
        },
    },
]

# ============================================================================
# TASK TOOL - The subagent spawning mechanism
# ============================================================================
TASK_TOOL = {
    "name": "Task",
    "description": (
        "Launch a subagent to handle a specific task autonomously.\n\n"
        f"Available agent types:\n{get_agent_descriptions()}\n\n"
        "The subagent runs in isolation with its own message history, "
        "executes tools as needed, and returns its final response."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Short task description (3-5 words)",
            },
            "prompt": {
                "type": "string",
                "description": "Detailed instructions for the subagent",
            },
            "subagent_type": {
                "type": "string",
                "description": f"Agent type: {', '.join(AGENT_TYPES.keys())}",
                "enum": list(AGENT_TYPES.keys()),
            },
        },
        "required": ["description", "prompt", "subagent_type"],
    },
}

# Full tool list for main agent (includes Task)
ALL_TOOLS = BASE_TOOLS + [TASK_TOOL]


def get_tools_for_agent(agent_type: str) -> List[Dict[str, Any]]:
    """Filter tools based on agent type configuration."""
    if agent_type not in AGENT_TYPES:
        return BASE_TOOLS  # Fallback

    allowed = AGENT_TYPES[agent_type]["tools"]
    if allowed == "*":
        return BASE_TOOLS  # Subagents don't get Task tool (no recursion in demo)

    return [t for t in BASE_TOOLS if t["name"] in allowed]


# ============================================================================
# TOOL EXECUTORS
# ============================================================================
def run_bash(inp: Dict[str, Any]) -> str:
    cmd = str(inp.get("command") or "")
    if not cmd:
        raise ValueError("missing command")
    if any(x in cmd for x in ["rm -rf /", "shutdown", "reboot", "sudo "]):
        raise ValueError("blocked dangerous command")
    timeout = int(inp.get("timeout_ms") or 30000) / 1000.0
    proc = subprocess.run(
        cmd,
        cwd=str(WORKDIR),
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return clamp_text("\n".join([proc.stdout, proc.stderr]).strip() or "(no output)")


def run_read(inp: Dict[str, Any]) -> str:
    fp = safe_path(inp.get("path"))
    text = fp.read_text("utf-8")
    lines = text.split("\n")
    start = max(0, int(inp.get("start_line") or 1) - 1)
    end = int(inp.get("end_line") or len(lines))
    if end < 0:
        end = len(lines)
    return clamp_text("\n".join(lines[start:end]))


def run_write(inp: Dict[str, Any]) -> str:
    fp = safe_path(inp.get("path"))
    fp.parent.mkdir(parents=True, exist_ok=True)
    content = inp.get("content") or ""
    if inp.get("mode") == "append" and fp.exists():
        with fp.open("a") as f:
            f.write(content)
    else:
        fp.write_text(content)
    return f"wrote {len(content.encode())} bytes to {fp.relative_to(WORKDIR)}"


def run_edit(inp: Dict[str, Any]) -> str:
    fp = safe_path(inp.get("path"))
    text = fp.read_text("utf-8")
    action = inp.get("action")

    if action == "replace":
        find = str(inp.get("find") or "")
        if not find:
            raise ValueError("missing find")
        result = text.replace(find, str(inp.get("replace") or ""))
        fp.write_text(result)
        return f"replaced in {fp.name}"

    if action == "insert":
        line_num = int(
            inp.get("insert_after") if inp.get("insert_after") is not None else -1
        )
        rows = text.split("\n")
        idx = max(-1, min(len(rows) - 1, line_num))
        rows.insert(idx + 1, str(inp.get("new_text") or ""))
        fp.write_text("\n".join(rows))
        return f"inserted after line {line_num}"

    if action == "delete_range":
        rng = inp.get("range") or []
        if len(rng) != 2:
            raise ValueError("invalid range")
        rows = text.split("\n")
        fp.write_text("\n".join(rows[: rng[0]] + rows[rng[1] :]))
        return f"deleted lines {rng[0]}-{rng[1]}"

    raise ValueError(f"unknown action: {action}")


def run_todo_update(inp: Dict[str, Any]) -> str:
    view = TODO_BOARD.update(inp.get("items") or [])
    stats = TODO_BOARD.stats()
    return view + f"\n\n({stats['completed']}/{stats['total']} completed)"


# ============================================================================
# SUBAGENT EXECUTION - The core of Task tool
# ============================================================================
def run_task(inp: Dict[str, Any], depth: int = 0) -> str:
    """
    Execute a subagent task. This is the heart of the subagent mechanism.

    Key concepts:
    1. Create isolated message history (subagent doesn't see parent conversation)
    2. Use agent-specific system prompt
    3. Filter available tools based on agent type
    4. Run the same query() loop recursively (in silent mode)
    5. Extract and return only the final text response

    Kode-style display:
    - Subagent output does NOT appear in main chat
    - Instead, a single progress line updates in place
    - Only final summary is shown
    """
    global _current_subagent_progress

    agent_type = inp.get("subagent_type")
    prompt = inp.get("prompt")
    description = inp.get("description", "subtask")

    if agent_type not in AGENT_TYPES:
        raise ValueError(
            f"Unknown agent type: {agent_type}. Available: {list(AGENT_TYPES.keys())}"
        )

    agent_config = AGENT_TYPES[agent_type]

    # 1. Get agent-specific system prompt
    sub_system = (
        f"You are a {agent_type} subagent operating in {WORKDIR}.\n"
        f"{agent_config['system_prompt']}\n\n"
        "Complete the task and provide a clear, concise summary."
    )

    # 2. Get filtered tools for this agent type
    sub_tools = get_tools_for_agent(agent_type)

    # 3. Create isolated message history (fresh start)
    sub_messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]

    # 4. Initialize progress tracker (Kode-style inline display)
    progress = SubagentProgress(agent_type, description)
    _current_subagent_progress = progress

    # 5. Run query loop in SILENT mode (depth > 0 triggers silent)
    try:
        result_messages = query(
            messages=sub_messages,
            system_prompt=sub_system,
            available_tools=sub_tools,
            depth=depth + 1,
            silent=True,  # Key: suppress output to main chat
        )
    finally:
        # 6. Finalize progress display
        summary = progress.finish()
        _current_subagent_progress = None
        print(summary)

    # 7. Extract final assistant response
    final_text = ""
    for msg in reversed(result_messages):
        if msg.get("role") == "assistant":
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    final_text = block.get("text", "")
                    break
            if final_text:
                break

    return final_text or "(subagent returned no text)"


# ============================================================================
# TOOL DISPATCHER
# ============================================================================
def dispatch_tool(
    tool_use: Dict[str, Any], depth: int = 0, silent: bool = False
) -> Dict[str, Any]:
    """
    Dispatch tool calls including Task for subagent spawning.

    In silent mode (for subagents):
    - No output to main chat
    - Updates progress tracker instead
    """
    name = (
        tool_use.get("name")
        if isinstance(tool_use, dict)
        else getattr(tool_use, "name", None)
    )
    inp = (
        tool_use.get("input", {})
        if isinstance(tool_use, dict)
        else getattr(tool_use, "input", {})
    )
    tool_id = (
        tool_use.get("id")
        if isinstance(tool_use, dict)
        else getattr(tool_use, "id", None)
    )

    def output(tool_name: str, tool_arg: str | None, result: str) -> None:
        """Output handler: either print or update progress."""
        if silent and _current_subagent_progress:
            # Silent mode: update progress line
            _current_subagent_progress.update(tool_name, tool_arg)
        else:
            # Normal mode: print to chat
            pretty_tool_line(tool_name, tool_arg, depth)
            pretty_sub_line(clamp_text(result, 500), depth)

    try:
        if name == "bash":
            result = run_bash(inp)
            output("Bash", inp.get("command"), result)
            return {"type": "tool_result", "tool_use_id": tool_id, "content": result}

        if name == "read_file":
            result = run_read(inp)
            output("Read", inp.get("path"), result)
            return {"type": "tool_result", "tool_use_id": tool_id, "content": result}

        if name == "write_file":
            result = run_write(inp)
            output("Write", inp.get("path"), result)
            return {"type": "tool_result", "tool_use_id": tool_id, "content": result}

        if name == "edit_text":
            result = run_edit(inp)
            output("Edit", f"{inp.get('action')} {inp.get('path')}", result)
            return {"type": "tool_result", "tool_use_id": tool_id, "content": result}

        if name == "TodoWrite":
            result = run_todo_update(inp)
            output("TodoWrite", None, result)
            return {"type": "tool_result", "tool_use_id": tool_id, "content": result}

        # ===== TASK TOOL: Subagent spawning =====
        if name == "Task":
            # Task tool always prints its header (shows subagent spawn)
            pretty_tool_line(
                "Task", f"{inp.get('subagent_type')}: {inp.get('description')}", depth
            )
            result = run_task(inp, depth)
            return {"type": "tool_result", "tool_use_id": tool_id, "content": result}

        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": f"unknown tool: {name}",
            "is_error": True,
        }

    except Exception as e:
        if silent and _current_subagent_progress:
            _current_subagent_progress.update(name, f"ERROR: {e}")
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": str(e),
            "is_error": True,
        }


# ============================================================================
# QUERY LOOP - Now supports subagent context
# ============================================================================
def query(
    messages: List[Dict[str, Any]],
    system_prompt: Optional[str] = None,
    available_tools: Optional[List[Dict[str, Any]]] = None,
    depth: int = 0,
    silent: bool = False,
) -> List[Dict[str, Any]]:
    """
    Core query loop. Extended to support:
    - Custom system_prompt (for subagents)
    - Custom available_tools (filtered per agent type)
    - depth tracking (for visual indentation)
    - silent mode (for subagent execution - no output to main chat)
    """
    system = system_prompt or SYSTEM
    tools = available_tools or ALL_TOOLS

    while True:
        # In silent mode, use progress tracker instead of spinner
        spinner = None
        if not silent:
            spinner = Spinner(f"{'  ' * depth}Agent thinking")
            spinner.start()

        try:
            response = client.messages.create(
                model=AGENT_MODEL,
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=16000,
            )
        finally:
            if spinner:
                spinner.stop()

        tool_uses = []
        for block in getattr(response, "content", []) or []:
            block_type = (
                getattr(block, "type", None)
                if not isinstance(block, dict)
                else block.get("type")
            )
            if block_type == "text":
                # Only print text in non-silent mode
                if not silent:
                    text = (
                        getattr(block, "text", "")
                        if not isinstance(block, dict)
                        else block.get("text", "")
                    )
                    prefix = "  " * depth
                    for line in text.split("\n"):
                        print(f"{prefix}{format_markdown(line)}")
            if block_type == "tool_use":
                tool_uses.append(block)

        if getattr(response, "stop_reason", None) == "tool_use":
            # Pass silent flag to tool dispatcher
            results = [dispatch_tool(tu, depth, silent) for tu in tool_uses]
            messages.append(
                {
                    "role": "assistant",
                    "content": normalize_content_list(response.content),
                }
            )
            messages.append({"role": "user", "content": results})
            continue

        messages.append(
            {"role": "assistant", "content": normalize_content_list(response.content)}
        )
        return messages


# ============================================================================
# MAIN LOOP
# ============================================================================
def main() -> None:
    clear_screen()
    render_banner("Tiny Kode Agent", "v3 with subagent support")
    print(f"{INFO_COLOR}Workspace: {WORKDIR}{RESET}")
    print(f"{INFO_COLOR}Agent types: {', '.join(AGENT_TYPES.keys())}{RESET}")
    print(f'{INFO_COLOR}Type "exit" to quit.{RESET}\n')

    history: List[Dict[str, Any]] = []

    while True:
        try:
            line = input(user_prompt_label())
        except EOFError:
            break

        if not line or line.strip().lower() in {"q", "quit", "exit"}:
            break

        print()
        history.append({"role": "user", "content": [{"type": "text", "text": line}]})

        try:
            query(history)
        except Exception as e:
            print(f"{ACCENT_COLOR}Error: {e}{RESET}")

        print()


if __name__ == "__main__":
    main()
