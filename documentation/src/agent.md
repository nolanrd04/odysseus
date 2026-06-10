# Agent Loop, Tools & MCP (`src/`)

> Parent: [`README.md`](README.md) | Architecture: [`../architecture.md`](../architecture.md)

---

## Overview

In agent mode, the LLM is given a set of tool schemas and may request tool execution. The agent loop runs multiple rounds: send messages → get tool calls → execute tools → feed results back → repeat until the LLM responds with final text.

---

## Core Files

### [`src/agent_loop.py`](/src/agent_loop.py) (~2291 lines)
The multi-round agent orchestration engine.

**How it works:**
1. Sends system prompt (with tool list) + conversation history to LLM via `llm_core.py`
2. Parses tool call blocks from the LLM response
3. Dispatches each tool to `tool_execution.py`
4. Appends tool results as assistant/tool messages
5. Sends the updated conversation back to the LLM
6. Repeats until: no tool calls in response, or max iterations reached

**Key functions:**
- `run_agent_loop(session_id, user_message, ...)` — main entry point
- `parse_tool_calls(response)` — extracts tool call JSON from LLM output
- `build_tool_system_prompt(tools)` — constructs the system prompt with available tools

**System prompts** for tool use are defined inline in this file. Edit here to change how the agent reasons about tools.

---

### [`src/tool_schemas.py`](/src/tool_schemas.py) (~1228 lines)
OpenAI-style function call schemas for all tools. This is the **contract** between the LLM and the tool system — what the LLM sees when deciding which tool to call.

**Structure of each schema:**
```python
{
    "name": "tool_name",
    "description": "What this tool does (seen by the LLM)",
    "parameters": {
        "type": "object",
        "properties": { ... },
        "required": [...]
    }
}
```

> **When adding a tool:** add the schema here AND the implementation in `tool_implementations.py`. Both must stay in sync.

---

### [`src/tool_implementations.py`](/src/tool_implementations.py) (~4136 lines)
Implementations of all 40+ tools available to the agent.

**Tool categories:**
| Category | Tools |
|----------|-------|
| Shell & Python | `run_shell_command`, `run_python_code` |
| File system | `read_file`, `write_file`, `list_directory`, `search_files` |
| Web | `web_search`, `fetch_url`, `scrape_page` |
| Email | `send_email`, `read_email`, `search_email` |
| Calendar | `create_event`, `list_events`, `update_event` |
| Memory | `remember`, `recall`, `forget` |
| Image | `generate_image`, `describe_image` |
| Documents | `read_document`, `edit_document` |
| Tasks | `create_task`, `list_tasks` |
| System | `get_current_time`, `get_system_info` |

Each tool function receives the parsed arguments from the LLM and returns a string result that is fed back into the conversation.

---

### [`src/tool_execution.py`](/src/tool_execution.py) (~1010 lines)
Safety layer between `agent_loop.py` and `tool_implementations.py`.

**What it does:**
- Validates tool names against the registered schema list (rejects unknown tools)
- Enforces file path confinement (tools cannot access paths outside allowed directories)
- Applies per-tool timeout limits
- Sandboxes shell command execution
- Logs all tool calls for audit

**Security-critical.** Any new tool that performs shell execution or file I/O must be reviewed here.

---

### [`src/task_scheduler.py`](/src/task_scheduler.py) (~2236 lines)
Cron-style scheduled task execution. Tasks are stored in the database (`tasks` table in `core/database.py`) and run by the agent loop on a schedule.

**Key concepts:**
- Tasks have a cron expression, a prompt, and an assigned model
- On schedule, the task scheduler starts an agent loop with the task's prompt
- Task output is persisted and viewable in the UI
- Webhooks can trigger tasks on-demand

Related routes: [`../routes/README.md`](../routes/README.md) → `task_routes.py`

---

### [`src/builtin_actions.py`](/src/builtin_actions.py) (~2231 lines)
Higher-level pre-built actions that the agent can invoke. These are more complex than atomic tools — each action orchestrates multiple steps:

- **Document editing** — opens a document, applies AI edits, saves
- **Image generation** — calls the diffusion model, saves to gallery
- **Email triage** — reads inbox, categorizes, drafts replies
- **Calendar sync** — pulls CalDAV events, reconciles with local DB

---

### [`src/mcp_manager.py`](/src/mcp_manager.py) (~439 lines)
MCP (Model Context Protocol) server lifecycle manager.

**What it does:**
- Starts and stops MCP server processes
- Discovers tools exposed by each MCP server
- Registers MCP tools into the agent's tool list at runtime
- Proxies tool calls from the agent to the correct MCP server

**MCP server configs** are stored in the `mcp_servers` DB table and managed via `/api/mcp/*` routes.

---

### [`src/builtin_mcp.py`](/src/builtin_mcp.py)
Auto-registers built-in MCP servers at startup. Currently includes:
- **Browser MCP** — Playwright-based browser automation

---

### [`mcp_servers/`](/mcp_servers/)
Built-in MCP server implementations (run in-process or as subprocesses):
- Memory MCP server
- RAG MCP server
- Email MCP server
- Image generation MCP server

---

## Adding a New Tool

1. **Define the schema** in `src/tool_schemas.py` — name, description, parameters
2. **Implement the function** in `src/tool_implementations.py` — same name, receives parsed args dict
3. **Register it** in `src/tool_execution.py` if it needs special sandboxing or path constraints
4. **Test** with a tool-use test in `tests/`

---

## Related

- LLM calls within the agent → [`llm.md`](llm.md)
- MCP route management → [`../routes/README.md`](../routes/README.md) → `mcp_routes.py`
- Task route endpoints → [`../routes/README.md`](../routes/README.md) → `task_routes.py`
- Shell execution service → [`../services/README.md`](../services/README.md)
