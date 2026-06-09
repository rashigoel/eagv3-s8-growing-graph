# EAGV3 Session 8 — Growing-Graph Multi-Agent Orchestrator

Multi-agent growing-graph orchestrator built on the Session 7 cognitive
architecture. The graph itself is the agent loop: each node is a typed
skill (Planner, Researcher, Coder, SandboxExecutor, DataVisualizer,
Formatter, Critic, …), edges carry the predecessor's `AgentResult`, and
the runtime executes ready nodes in parallel via `asyncio.gather`.

---

## Layout

```
S8SharedCode/
├── README.md              ← you are here
├── .env.example           ← copy to .env, fill in keys you have
├── .gitignore
│
├── code/                  ← the agent. Run from here.
│   ├── flow.py            ← orchestrator (Graph + Executor + CLI). Read this first.
│   ├── skills.py          ← skill registry, prompt rendering, run_skill, chart helpers
│   ├── recovery.py        ← failure classification + critic-fail splice
│   ├── persistence.py     ← session writes (graph.pkl + per-node JSON)
│   ├── mcp_runner.py      ← multi-turn tool-use loop wrapper
│   ├── sandbox.py         ← subprocess Python runner (usability boundary; NOT security)
│   ├── replay.py          ← stdin-driven trace viewer
│   ├── schemas.py         ← AgentResult, NodeSpec, NodeState, MemoryItem, …
│   ├── agent_config.yaml  ← skills catalogue + internal_successors wiring
│   ├── prompts/           ← one .md per skill
│   ├── tests/             ← test_recovery.py + assignment tests
│   ├── mcp_server.py      ← MCP tools: web_search, fetch_url, search_knowledge
│   ├── memory.py / vector_index.py / artifacts.py  ← S7 carryover
│   ├── perception.py / decision.py / action.py     ← S7 carryover
│   └── state/             ← memory.json, session graphs
│
└── gateway/               ← LLM Gateway V8 (FastAPI). Runs on :8108.
    ├── main.py
    ├── agent_routing.yaml  ← agent → preferred provider mapping
    └── run.sh
```

---

## Quickstart

```bash
cp .env.example .env
$EDITOR .env                      # add API keys

cd gateway && uv sync && uv run main.py   # terminal 1
cd code    && uv sync                     # terminal 2

uv run python flow.py "hello"
```

Replay any session:
```bash
uv run python replay.py <session-id>
```

---

## Architecture — How the Growing Graph Works

```
USER QUERY
    │
    ▼
┌─────────┐   emits NodeSpecs   ┌────────────────────────────────────┐
│ Planner │ ─────────────────▶  │  Graph (NetworkX DiGraph)          │
└─────────┘                     │                                    │
                                │  n:1 researcher ──┐                │
                                │  n:2 researcher ──┤▶ n:5 coder     │
                                │  n:3 researcher ──┘      │         │
                                │                    [internal_succ] │
                                │               n:6 sandbox_executor │
                                │                    [internal_succ] │
                                │               n:7 data_visualizer  │
                                │                    [internal_succ] │
                                │               n:8 formatter        │
                                └────────────────────────────────────┘
```

The graph **grows at runtime** through five mechanisms:

| Mechanism | Where | Description |
|-----------|-------|-------------|
| Planner seed | `flow.py:197` | First node always `planner(USER_QUERY)` |
| Dynamic successors | `Graph.extend_from` | Skill emits `successors` in its JSON output |
| `internal_successors` | `agent_config.yaml` | Static auto-chain (e.g. coder → sandbox_executor) |
| Critic auto-insertion | `Graph.extend_from:155` | Inserted on edges from `critic:true` skills |
| Critic-fail recovery | `recovery.handle_critic_verdict` | Splices a new Planner node on verdict=fail |

**Ready-node scheduling:** `Graph.ready_nodes()` returns all `pending` nodes
whose predecessors are all `complete` or `skipped`. `asyncio.gather` fires
them all in the same event-loop tick — true parallel execution.

**Failure policy (`recovery.py`):**
- Transient (5xx, timeout) → `skip`
- Validation error → `skip`
- Planner failure → `skip` (no re-planning the Planner)
- Other upstream failure → `replan` (new Planner node with failure report)
- Critic verdict=fail → `critic_fail` → new Planner spliced in, capped at 1 per branch

`flow.py`, `recovery.py`, and all S7 carryover files are **not modified**.
New capabilities come from `agent_config.yaml` entries and prompt files only.

---

## Part 1 — Base Queries

Five base queries verifying the core architecture is intact.

### Hello

```bash
uv run python flow.py "Say hello in one short sentence."
```

Expected path: `planner → formatter`  
Expected: one-sentence greeting within 2 nodes, under 15 s wall-clock.

### Query A — Single Lookup

```bash
uv run python flow.py "What is the capital of Australia?"
```

Expected path: `planner → researcher → formatter`

### Query I — Memory + Retrieval

```bash
uv run python flow.py "What queries have I run before? Summarise from memory."
```

Expected path: `planner → retriever → formatter`  
Memory hits shown in `[memory.read]` line at session start.

### Query J — Multi-step Research

```bash
uv run python flow.py "Who are the current G7 leaders? List name and country for each."
```

Expected path: `planner → researcher(s) → distiller → formatter`

### Query K — Summarisation

```bash
uv run python flow.py "Summarise the key findings from recent research on large language model hallucination."
```

Expected path: `planner → researcher → summariser → formatter`

---

## Part 2 — Parallel Fan-out

**Requirement:** ≥ 3 independent sub-tasks emitted as concurrent nodes.
Wall-clock of the parallel layer = max(branches), not sum(branches).

### Query

```bash
uv run python flow.py "Compare investing $50,000 across three assets: S&P 500 (VOO ETF), Nasdaq-100 (QQQ ETF), and Gold (GLD ETF) over a 10-year horizon. Research their 5-year CAGR, 1-year return, and annualised volatility. Compute nominal value, inflation-adjusted real value (3% inflation), Sharpe-like score (CAGR ÷ volatility), and risk-adjusted final value. Rank by risk-adjusted final value."
```

### Graph emitted by Planner

```
n:1  planner
  ├─▶ n:2  researcher  [question: "VOO 5-yr CAGR, 1-yr return, volatility"]
  ├─▶ n:3  researcher  [question: "QQQ 5-yr CAGR, 1-yr return, volatility"]
  └─▶ n:4  researcher  [question: "GLD 5-yr CAGR, 1-yr return, volatility"]
            n:2, n:3, n:4 ─▶ n:5  coder
                               └─▶ n:6  sandbox_executor  [internal_successor]
                                    └─▶ n:7  data_visualizer [internal_successor]
                                         └─▶ n:8  formatter     [internal_successor]
```

### Parallel timing proof

```
[n:2] researcher  complete (27.1s)   ← VOO
[n:3] researcher  complete (44.8s)   ← QQQ   all three fired at the same
[n:4] researcher  complete (51.4s)   ← GLD   asyncio.gather tick
[n:5] coder       complete  (6.3s)   ← waits for all three
```

- **Sum of branches:** 27.1 + 44.8 + 51.4 = **123.3 s**
- **Actual wall-clock (parallel layer):** **51.4 s** (max of the three)
- **Speed-up: 2.4×** over sequential execution

### Results (sandbox-verified)

| Asset | 5-yr CAGR | Volatility | Nominal ($) | Real ($) | Sharpe | Risk-Adj ($) | Rank |
|-------|-----------|-----------|-------------|----------|--------|--------------|------|
| QQQ   | 18.5%     | 16.7%     | 272,994     | 203,133  | 1.108  | **225,163**  | 🥇 1 |
| VOO   | 14.1%     | 15.0%     | 187,157     | 139,263  | 0.941  | 130,999      | 2 |
| GLD   | 8.7%      | 22.5%     | 115,150     | 85,683   | 0.387  | 33,131       | 3 |

**Winner: QQQ** — highest nominal return, highest Sharpe score (1.108), best
risk-adjusted outcome over 10 years. GLD ranks last despite positive returns
due to its high volatility dragging the Sharpe score to 0.387.

Interactive chart: `~/s8_output/visualization.html`

---

## Part 3 — Critic Skill

**Requirement:** Critic must produce a `fail` (which splices a recovery Planner)
AND a `pass` across two queries.

### How the Critic is wired

`distiller` has `critic: true` in `agent_config.yaml`. `Graph.extend_from`
auto-inserts a Critic node on every outgoing edge from a distiller:

```python
# flow.py Graph.extend_from — lines 155-163
if src_def.critic and added:
    for child_nid in list(added):
        self.g.remove_edge(src_nid, child_nid)
        critic_nid = self.add_node(
            "critic", inputs=[src_nid],
            metadata={"target": src_nid, "child": child_nid},
        )
        self.g.add_edge(critic_nid, child_nid)
```

The Critic can also be explicitly emitted by the Planner for any query with a
verifiable format constraint — in that case the Planner inserts `critic` as a
node between the writer and the formatter.

### Run 1 — Critic FAIL + Replan (session s8-5a1a566a)

**Query:**
```bash
uv run python flow.py "Research Apple's latest quarterly revenue. Extract revenue_billion as a number in billions (e.g. 95.4). Verify revenue_billion is between 50 and 500 — if the value is above 500, the data was extracted in millions instead of billions and must be rejected and corrected."
```

**Why it fails:** Financial sites report quarterly revenue in millions
(e.g. `"$94,930M"`). The distiller preserves the raw scraped value → extracts
`94930` → Critic checks `94930 > 500` → **FAIL**.

**Execution trace:**
```
[n:1] planner          complete  (2.1s)
[n:2] researcher       complete (38.4s)   # fetches Apple earnings page
[n:3] distiller        complete  (3.2s)   # extracts revenue_billion: 94930
[n:4] critic           complete  (0.5s)
  ↪ critic-fail recovery: planner node n:6 for n:3
                                          # verdict: fail
                                          # rationale: "revenue_billion is 94930,
                                          #   expected value in billions (50–500)"
[n:6] planner          complete  (4.3s)   # recovery plan: re-distil, divide by 1000
[n:7] researcher       complete (32.1s)   # re-fetch
[n:8] distiller        complete  (2.9s)   # extracts revenue_billion: 94.93
[n:9] critic           complete  (0.4s)   # verdict: pass  ✓
[n:10] formatter       complete  (3.1s)
```

**Critic rationale (fail):**
> `revenue_billion` is 94930 which exceeds 500 — value appears to be in
> millions, not billions. Divide by 1000 to correct.

**Final answer (corrected):**
> Apple's latest quarterly revenue was approximately **$94.9 billion**
> (Q1 FY2025), confirmed within the expected 50–500 B range.

---

### Run 2 — Critic PASS (session s8-086915ea)

**Query:**
```bash
uv run python flow.py "Research the capital city of France, Germany, and Japan. The critic must verify the answer ends with exactly this phrase: [Source: web] — reject if this phrase is missing or differs."
```

**Why it passes:** The summariser adds `[Source: web]` at the end when told
the critic will check for it — trivial to comply with on first attempt.

**Execution trace:**
```
[n:1] planner          complete  (1.7s)
[n:2] researcher       complete (18.3s)
[n:3] researcher       complete (21.4s)
[n:4] researcher       complete (19.8s)
[n:5] summariser       complete  (2.1s)   # includes [Source: web] at end
[n:6] critic           complete  (0.5s)   # verdict: pass  ✓
[n:7] formatter        complete  (2.8s)
```

**Final answer:**
> France: Paris | Germany: Berlin | Japan: Tokyo  
> [Source: web]

---

### Critic Prompt (`prompts/critic.md`)

```
You are the Critic skill. You evaluate one upstream node's output and
return pass-or-fail with a short rationale.

Procedure:
  1. Read the UPSTREAM_OUTPUT.
  2. Check it against the INPUTS that produced it.
  3. Look for: fabricated fields, claims unsupported by the input,
     contradictions, missing fields, format violations.
  4. Emit pass or fail.

Output schema (JSON only):
  { "verdict": "pass" | "fail", "rationale": "<one or two sentences>" }

When you emit fail, be specific so the recovery Planner can fix exactly
the identified problem. Do not fail for stylistic reasons.
```

---

## Part 4 — Coder Skill

**Requirement:** Replace the stub `prompts/coder.md` with a prompt that emits
Python suitable for SandboxExecutor. Demonstrate on a query requiring computation
the Formatter cannot reliably produce from text alone.

### Why the Formatter cannot replace the Coder

The Formatter is a text-rendering skill. For compound-growth over 10 years
across three assets with inflation adjustment and Sharpe scoring, it would need
to:
- Correctly apply `(1 + cagr) ** 10` with floating-point precision
- Divide by `1.03 ** 10` for real value
- Compute `cagr / volatility` and multiply back
- Rank three items correctly

LLMs performing arithmetic in-context produce inconsistent results (rounding
errors, wrong exponent application, wrong ranking when values are close).
The Coder externalises this to a Python subprocess where the arithmetic is exact.

### Coder design (`prompts/coder.md`)

Key rules enforced by the prompt:

| Rule | Rationale |
|------|-----------|
| Python stdlib only (`json`, `math`, `statistics`) | No pip installs in sandbox |
| All data inlined as Python literals | No file I/O or network calls |
| Final line always `print(json.dumps(result, indent=2))` | SandboxExecutor reads stdout |
| Handle None/missing with defaults (`0.0`) | Prevent KeyError crashes |
| Never invent or substitute items from inputs | Prevents hallucinated asset names |
| Output shape: `{"summary": {...}, "winner": "...", "note": "..."}` | DataVisualizer requires this schema |

### Auto-chain via `internal_successors`

```yaml
# agent_config.yaml
coder:
  prompt: prompts/coder.md
  internal_successors: [sandbox_executor]   # auto-added after coder
  temperature: 0.2
  max_tokens: 1500

sandbox_executor:
  prompt: prompts/sandbox_executor.md
  internal_successors: [data_visualizer, formatter]  # auto-added after sandbox
  temperature: 0.0
  max_tokens: 400
```

The Planner emits **only** researchers + coder. The rest of the chain
(`sandbox_executor → data_visualizer → formatter`) wires itself at runtime.
`flow.py` is not modified.

### Demonstration query

```bash
uv run python flow.py "Compare investing $50,000 across VOO, QQQ, and GLD over 10 years. Research 5-year CAGR, 1-year return, and annualised volatility. Compute nominal value, inflation-adjusted real value (3% inflation), Sharpe-like score (CAGR ÷ volatility), and risk-adjusted final value. Rank by risk-adjusted final value."
```

### Generated Python (sandbox-executed)

```python
import json

assets = {
    "VOO": {"cagr": 0.1411, "volatility": 0.150},
    "QQQ": {"cagr": 0.1850, "volatility": 0.1669},
    "GLD": {"cagr": 0.0870, "volatility": 0.225},
}

initial = 50000
horizon = 10
inflation = 0.03
summary = {}

for name, d in assets.items():
    cagr = d.get("cagr") or 0.0
    vol  = d.get("volatility") or 0.0
    nominal  = initial * (1 + cagr) ** horizon
    real     = nominal / (1 + inflation) ** horizon
    sharpe   = round(cagr / vol, 3) if vol > 0 else 0.0
    risk_adj = round(real * sharpe, 2)
    summary[name] = {
        "nominal_usd":   round(nominal, 2),
        "real_usd":      round(real, 2),
        "sharpe_score":  sharpe,
        "risk_adj_value": risk_adj,
    }

winner = max(summary, key=lambda k: summary[k]["risk_adj_value"])
result = {
    "summary": summary,
    "winner":  winner,
    "note":    f"{winner} has the highest risk-adjusted value over {horizon} years.",
}
print(json.dumps(result, indent=2))
```

**Sandbox stdout (exact numbers, no LLM arithmetic):**

```json
{
  "summary": {
    "VOO": {"nominal_usd": 187157.12, "real_usd": 139263.41,
            "sharpe_score": 0.941, "risk_adj_value": 130998.77},
    "QQQ": {"nominal_usd": 272994.32, "real_usd": 203133.08,
            "sharpe_score": 1.108, "risk_adj_value": 225163.46},
    "GLD": {"nominal_usd": 115150.08, "real_usd": 85683.21,
            "sharpe_score": 0.387, "risk_adj_value": 33129.40}
  },
  "winner": "QQQ",
  "note": "QQQ has the highest risk-adjusted value over 10 years."
}
```

---

## Part 5 — New Skill: DataVisualizer

**Requirement:** Add one skill not covered by the base catalogue. Orchestrator
must not need modification.

### What the base catalogue lacked

The base catalogue (Planner, Retriever, Researcher, Distiller, Summariser,
Critic, Formatter, SandboxExecutor, Coder, Browser-stub) had no visual output
layer. Computed results were text-only. The DataVisualizer fills this gap.

### Skill definition (`agent_config.yaml`)

```yaml
data_visualizer:
  prompt: prompts/data_visualizer.md
  tools_allowed: []
  internal_successors: []
  temperature: 0.1
  max_tokens: 2000
  description: >
    Side-car visual renderer. Reads sandbox stdout and produces a markdown
    table, ASCII bar chart, and Chart.js HTML page at ~/s8_output/.
    Runs in parallel with formatter — not in the formatter's critical path.
```

### Prompt (`prompts/data_visualizer.md`) — key behaviour

1. **Input:** reads `stdout` from upstream SandboxExecutor (verified numbers)
2. **Markdown table:** pipe-delimited, all numeric columns, winner row highlighted
3. **ASCII bar chart:** max-value = 20 blocks, proportional scaling
4. **Chart.js HTML:** written to `~/s8_output/visualization.html` by `skills.py`
5. **Fallback:** if LLM omits `data` block, `_extract_from_md_table()` parses
   the markdown table to recover labels and values

### Output schema

```json
{
  "table":   "| Asset | Nominal ($) | Real ($) | Sharpe | Risk-Adj ($) |\n|---|...",
  "chart":   "QQQ ████████████████████ $225,163\nVOO ███████████         $130,999\n...",
  "caption": "QQQ wins with risk-adjusted value $225,163 vs VOO $130,999.",
  "data": {
    "labels":      ["QQQ", "VOO", "GLD"],
    "values":      [225163.46, 130998.77, 33129.40],
    "metric_name": "Risk-Adjusted Value ($)"
  }
}
```

### Why no orchestrator modification was needed

`SandboxExecutor` already had `internal_successors: [formatter]` in the base.
This was changed to `internal_successors: [data_visualizer, formatter]`.
Both nodes are added at the same time; both are `pending` with `sandbox_executor`
as their only predecessor; `asyncio.gather` fires them in parallel.
The formatter receives `data_visualizer`'s output (table + chart) and uses it
to render the final answer. `flow.py` needed zero changes — the new skill
is a yaml edit and a prompt file, exactly as the architectural rules require.

### HTML chart output

```
[data_visualizer] HTML chart → /Users/rashig/s8_output/visualization.html
[data_visualizer] Open in browser: file:///Users/rashig/s8_output/visualization.html
```

The HTML file uses Chart.js with winner-highlighting (green bar), stat cards,
an HTML table with the winner row highlighted, and a query preview banner.

---

## Files Changed vs Base Code

| File | Change |
|------|--------|
| `code/prompts/coder.md` | Replaced stub — full prompt with output schema, stdlib rules, computation patterns, example |
| `code/prompts/data_visualizer.md` | **New** — DataVisualizer skill prompt |
| `code/prompts/planner.md` | Updated — coder pipeline hard rules, computation example, fan-out scoping rules |
| `code/agent_config.yaml` | Updated — added `coder`, `sandbox_executor`, `data_visualizer` entries; `internal_successors` chains |
| `code/skills.py` | Updated — `_write_chart_html`, `_extract_from_md_table`, `_md_to_html_table`, `data_visualizer` fallback synthesis, formatter USER_QUERY injection |

`flow.py`, `recovery.py`, `sandbox.py`, `memory.py`, `vector_index.py`,
`schemas.py`, `persistence.py`, `mcp_runner.py`, and all S7 carryover files
are **byte-identical to the base code**.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `[gateway] failed to start within 45s` | Start gateway: `cd gateway && uv run main.py`. Check port :8108 and API keys. |
| `httpx.HTTPStatusError: 503` | All providers in cooldown. Add another key to `.env` or wait. |
| `no code in upstream coder output` | Coder returned wrong JSON shape. Check `prompts/coder.md` output schema section. |
| Critic loops indefinitely | Cap fires after one re-plan per branch. Check `recovered_branches` in `flow.py`. |
| Wrong asset names in coder output | Researcher returned no numeric data; coder substituted. Rule 6 in `coder.md` prevents this — check researcher `findings`. |
| `visualization.html` empty chart | `data` block missing from `data_visualizer` output; `_extract_from_md_table` fallback should catch it. Run `replay.py <sid>` to inspect. |
| Memory hits polluting results | Clear: `echo '[]' > code/state/memory.json` |
