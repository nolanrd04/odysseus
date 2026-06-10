# Deep Research Pipeline (`src/`)

> Parent: [`README.md`](README.md) | Architecture: [`../architecture.md`](../architecture.md)

---

## Overview

Deep research is an iterative, multi-step process where the LLM plans a research strategy, performs web searches, extracts relevant information from pages, and synthesizes a final structured report. Inspired by Alibaba's DeepResearch approach.

**Entry route:** `POST /api/research` → `routes/research_routes.py` → `src/research_handler.py`

---

## Files

### [`src/deep_research.py`](/src/deep_research.py) (~858 lines)
Core research loop implementation.

**Pipeline stages:**
```
1. THINK    — LLM plans which queries to search and what to look for
2. SEARCH   — SearXNG queries via services/search/
3. EXTRACT  — Fetches pages, extracts relevant passages (goal-directed)
4. REFLECT  — LLM evaluates coverage, plans follow-up searches if needed
5. SYNTHESIZE — LLM writes structured Markdown report from all extracted content
```

Each stage calls `llm_core.py` with a stage-specific prompt. The loop repeats THINK→SEARCH→EXTRACT→REFLECT until the LLM declares the topic sufficiently covered or a max-iteration limit is reached.

**Key functions:**
- `run_deep_research(topic, goal, session_id, ...)` — main entry point
- `think_step(context)` — generates search queries
- `extract_step(url, goal)` — pulls relevant content from a single page
- `synthesize(all_extracts, topic)` — final report generation

---

### [`src/research_handler.py`](/src/research_handler.py) (~894 lines)
Orchestrates a research session: manages state, progress streaming, and report persistence.

**What it does:**
- Creates a research session record in the DB
- Streams progress events (SSE) back to the frontend as each stage completes
- Calls `deep_research.py` for the actual research
- Saves the finished report to `data/deep_research/`
- Triggers `visual_report.py` to render HTML

---

### [`src/visual_report.py`](/src/visual_report.py) (~1869 lines)
Renders research reports from Markdown → styled HTML.

**Features:**
- Syntax highlighting for code blocks
- Citation/source linking
- Table of contents generation
- Responsive layout

Reports are saved as `.html` files in `data/deep_research/` alongside the source Markdown.

---

### [`src/goal_based_extractor.py`](/src/goal_based_extractor.py)
Extracts goal-relevant passages from raw web page content. Called by `deep_research.py` during the EXTRACT stage.

Given a research goal and raw page text, this module uses the LLM to pull out only the passages that advance the research goal — filtering noise from long pages.

---

## Configuration

| Env Var | Purpose |
|---------|---------|
| `SEARXNG_INSTANCE` | Web search engine for research queries |
| `DATA_BRAVE_API_KEY` | Brave Search API (alternative to SearXNG) |
| `TAVILY_API_KEY` | Tavily search API (alternative) |
| `SERPER_API_KEY` | Serper search API (alternative) |

Search provider priority: SearXNG → Brave → Tavily → Serper

---

## Output

Research reports are saved to `data/deep_research/` as:
- `{session_id}.md` — source Markdown
- `{session_id}.html` — rendered HTML report

Accessible via the research routes: `GET /api/research/{id}/report`

---

## Related

- Web search service → [`../services/README.md`](../services/README.md)
- Research routes → [`../routes/README.md`](../routes/README.md) → `research_routes.py`
- LLM calls → [`llm.md`](llm.md)
