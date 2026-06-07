# EAGV3 Session 8 — Growing-Graph Multi-Agent Orchestrator

Multi-agent growing-graph orchestrator built on the Session 7 cognitive
architecture. The graph itself is the agent loop: each node is a typed
skill (Planner, Researcher, Coder, SandboxExecutor, DataVisualizer,
Formatter, …), edges carry the predecessor's `AgentResult`, and the runtime
executes ready nodes in parallel via `asyncio.gather`.

---

## Layout

```
S8SharedCode/
├── README.md          ← you are here
├── ASSIGNMENT.md      ← full spec
├── .env.example       ← copy to .env, fill in keys you have
├── .gitignore
│
├── code/              ← the agent. Run from here.
│   ├── flow.py        ← orchestrator (Graph + Executor + CLI). Read this first.
│   ├── skills.py      ← skill registry, prompt rendering, run_skill
│   ├── recovery.py    ← failure classification + critic-fail splice
│   ├── persistence.py ← session writes (graph.json + per-node JSON)
│   ├── mcp_runner.py  ← multi-turn tool-use loop wrapper
│   ├── sandbox.py     ← subprocess Python runner (usability boundary; NOT security)
│   ├── replay.py      ← stdin-driven trace viewer
│   ├── schemas.py     ← AgentResult, NodeSpec, NodeState, MemoryItem, …
│   ├── agent_config.yaml  ← skills catalogue + internal_successors wiring
│   ├── prompts/       ← one .md per skill
│   ├── tests/         ← test_recovery.py + assignment tests
│   ├── mcp_server.py  ← MCP tools: web_search, fetch_url, search_knowledge, …
│   ├── memory.py / vector_index.py / artifacts.py  ← S7 carryover (don't touch)
│   ├── perception.py / decision.py / action.py     ← S7 carryover (don't touch)
│   └── sandbox/papers/  ← five arxiv abstracts for indexed-corpus queries
│
└── gateway/           ← LLM Gateway V8 (FastAPI). Runs on :8108.
    ├── main.py
    ├── client.py
    ├── providers.py / router.py / embedders.py / db.py / cache.py
    ├── agent_routing.yaml  ← agent → preferred provider mapping
    ├── pyproject.toml
    └── run.sh
```

---

## Quickstart

You need: Python 3.11+, [uv](https://docs.astral.sh/uv/), Ollama
(`brew install ollama` then `ollama pull nomic-embed-text`), and at least
one provider API key from `.env.example`.

```bash
# 1. Secrets
cp .env.example .env
$EDITOR .env                  # add the keys you have

# 2. Install
cd gateway && uv sync && cd ..
cd code    && uv sync && cd ..

# 3. Start the gateway (one terminal)
cd gateway && uv run main.py

# 4. Run the agent (another terminal)
cd code
uv run python flow.py "hello"
```

Walk a session with:

```bash
uv run python replay.py <sid>
```

---

## Assignment Implementation (Parts 2–5)

### Part 2 — Coder Skill

**File:** `code/prompts/coder.md`

The Coder skill receives structured research data from upstream Researcher
nodes and emits a self-contained Python script. It does **not** compute
results itself — it only writes code.

Key design decisions:
- Output schema: `{"code": "<Python source>", "rationale": "<one sentence>"}` — no other fields
- All upstream data is inlined as Python literals; no file I/O or network calls
- Only Python stdlib allowed (`json`, `math`, `statistics`, `datetime`, `re`)
- Final line of generated script is always `print(json.dumps(result, indent=2))`
- Hard rule added: never rename or substitute items from inputs; use only asset names given in research

**Registration in `agent_config.yaml`:**

```yaml
coder:
  prompt: prompts/coder.md
  internal_successors: [sandbox_executor]
  temperature: 0.2
  max_tokens: 1500
```

The `internal_successors: [sandbox_executor]` means the orchestrator
auto-adds a SandboxExecutor node after every Coder node completes —
the Planner never needs to emit it.

---

### Part 3 — SandboxExecutor Skill

**File:** `code/prompts/sandbox_executor.md`

Runs the Python script from the upstream Coder node inside `sandbox.run_python()`
(subprocess, stdlib-only, 30 s timeout). Returns `stdout`, `stderr`,
`exit_code`, and any written files.

```yaml
sandbox_executor:
  prompt: prompts/sandbox_executor.md
  internal_successors: [data_visualizer]
  temperature: 0.0
  max_tokens: 400
```

The verified stdout (actual computed numbers) is what flows downstream to
DataVisualizer — not the Coder's raw code.

---

### Part 4 — DataVisualizer Skill

**File:** `code/prompts/data_visualizer.md`  
**Helper code:** `code/skills.py` (`_write_chart_html`, `_extract_from_md_table`, `_md_to_html_table`)

Reads SandboxExecutor's verified stdout and renders:
1. Markdown comparison table
2. ASCII bar chart (terminal)
3. Chart.js HTML page written to `~/s8_output/visualization.html`

Output schema:

```json
{
  "table":   "<markdown table>",
  "chart":   "<ASCII bar chart>",
  "caption": "<one sentence winner summary>",
  "data": {
    "labels":      ["VOO", "QQQ", "GLD"],
    "values":      [130999.78, 225162.81, 33130.65],
    "metric_name": "Risk-Adjusted Value ($)"
  }
}
```

`skills.py` writes the HTML file whenever `data_visualizer` completes and
returns a `table`. If the LLM omits the `data` block, `_extract_from_md_table`
parses the markdown table as a fallback to extract labels and values.

```yaml
data_visualizer:
  prompt: prompts/data_visualizer.md
  tools_allowed: []
  internal_successors: [formatter]
  temperature: 0.1
  max_tokens: 2000
```

---

### Part 5 — Full Auto-Chain via `internal_successors`

The entire computation pipeline is wired purely through `agent_config.yaml`.
`flow.py` is **not modified**.

```
Planner emits:  researcher(VOO) ─┐
                researcher(QQQ) ─┤→ coder
                researcher(GLD) ─┘
                                    ↓ [internal_successors]
                                  sandbox_executor
                                    ↓ [internal_successors]
                                  data_visualizer
                                    ↓ [internal_successors]
                                  formatter
```

The Planner prompt (`prompts/planner.md`) has a hard rule:
> For computation queries, emit ONLY researchers and coder.
> sandbox_executor, data_visualizer, and formatter are all auto-added.

The formatter auto-added by `data_visualizer`'s `internal_successors` has
`inputs=[data_visualizer_node_id]` — no USER_QUERY. `render_prompt` in
`skills.py` always injects USER_QUERY for formatter regardless of its
declared inputs so it can phrase the final answer against the user's ask.

If the Planner also emits an early formatter (running in parallel with the
sandbox), `flow.py` line 264 overwrites `formatter_answer` each time a
formatter completes — the late auto-chained formatter (which reads verified
data) always wins.

---

## Session Run — s8-a125f36e

**Query:**
> Compare investing $50,000 across three assets: S&P 500 (VOO ETF),
> Nasdaq-100 (QQQ ETF), and Gold (GLD ETF) over a 10-year horizon.
> Research their 5-year CAGR, 1-year return, and annualised volatility.
> Compute nominal value, inflation-adjusted real value (3% inflation),
> Sharpe-like score (CAGR ÷ volatility), and risk-adjusted final value.
> Rank by risk-adjusted final value.

**8-node graph:**

| Node | Skill            | Provider | Elapsed | Inputs       |
|------|------------------|----------|---------|--------------|
| n:1  | planner          | gemini   | —       | USER_QUERY   |
| n:2  | researcher (VOO) | gemini   | 27 s    | (scoped)     |
| n:3  | researcher (QQQ) | gemini   | 45 s    | (scoped)     |
| n:4  | researcher (GLD) | gemini   | 80 s    | (scoped)     |
| n:5  | coder            | gemini   | 3.1 s   | n:2, n:3, n:4|
| n:6  | sandbox_executor | —        | 0.06 s  | n:5          |
| n:7  | data_visualizer  | ollama   | 26 s    | n:6          |
| n:8  | formatter        | gemini   | —       | n:7          |

**Results (verified by sandbox stdout):**

| Asset | Nominal Value ($) | Real Value at 3% infl ($) | Sharpe Score | Risk-Adj Value ($) |
|-------|-------------------|---------------------------|--------------|---------------------|
| VOO   | 187,157           | 139,263                   | 0.941        | 130,999             |
| QQQ   | 272,994           | 203,133                   | 1.108        | **225,163**         |
| GLD   | 115,150           | 85,683                    | 0.387        | 33,131              |

**Winner: QQQ** — highest nominal return, highest real return, and highest
Sharpe score (1.108), delivering the best risk-return trade-off over 10 years.

Interactive chart written to: `~/s8_output/visualization.html`

**Data used by coder:**
- VOO: CAGR 14.11%, volatility 15%
- QQQ: CAGR 18.50%, volatility 16.69%
- GLD: CAGR 8.70%, volatility 22.50%

---

## Architecture — How the Growing Graph Works

The Planner reads the user query and emits a small DAG of skill nodes.
Each ready node fires through the gateway in parallel with its ready
siblings. When a skill's yaml entry has `internal_successors`, the
orchestrator appends those automatically.

Critic nodes get auto-inserted on edges out of skills tagged `critic: true`
in `agent_config.yaml` (currently Distiller). A verdict=fail from a Critic
splices a recovery Planner into the graph, capped at one re-plan per branch.

Failure handling is in `recovery.py`. Transient gateway errors don't
re-plan (the gateway already retries); validation errors don't re-plan
(prompt bug); upstream-failures do.

---

## Files Changed for This Assignment

| File | Change |
|------|--------|
| `code/prompts/coder.md` | New — Coder skill prompt with strict output schema |
| `code/prompts/sandbox_executor.md` | New — SandboxExecutor skill prompt |
| `code/prompts/data_visualizer.md` | New — DataVisualizer skill prompt with `data` block |
| `code/prompts/formatter.md` | Updated — added `html_path` rendering rule |
| `code/prompts/planner.md` | Updated — coder pipeline hard rules + computation example |
| `code/agent_config.yaml` | Updated — added coder, sandbox_executor, data_visualizer entries with `internal_successors` |
| `code/skills.py` | Updated — `_write_chart_html`, `_extract_from_md_table`, `_md_to_html_table`, formatter USER_QUERY injection |

`flow.py`, `recovery.py`, and all S7 carryover files are **unchanged**.

---

## Troubleshooting

| Symptom | First place to look |
|---------|---------------------|
| `[gateway] launching … failed to start within 45s` | `cd gateway && uv run main.py` in another terminal; check for missing API key or port :8108 conflict |
| `httpx.HTTPStatusError: 503 Service Unavailable` | All providers in cooldown. Add another key to `.env` or wait. |
| `sandbox_executor` reports `no code in upstream coder output` | Coder prompt not emitting correct JSON shape. Check `prompts/coder.md` output schema. |
| Formatter answer is short or wrong | Run `replay.py <sid>` — inspect `prompt_sent` on each node. |
| Wrong asset names in coder output | Researcher returned insufficient data; coder defaulted. Check researcher `findings` field in session nodes. |

---

## What NOT to Touch

- `recovery.py` — do not modify under any circumstances
- `perception.py`, `decision.py`, `action.py`, `memory.py`, `vector_index.py`, `artifacts.py`, `mcp_server.py` — S7 carryover, byte-identical
- `gateway/` — treat as a service you call
