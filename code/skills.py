"""Session 8 skill registry + per-skill execution.

The orchestrator (flow.py) treats every node as a `Skill` object loaded
from agent_config.yaml. There is no Python class per skill — that
abstraction would have to be added at the point where a skill needs
behaviour the orchestrator can't infer from the yaml. Today every skill
either calls the gateway or (for sandbox_executor) calls sandbox.py.

What lives here:
  - Skill / SkillRegistry
  - input resolution (`n:...`, `art:...`, `USER_QUERY`, literals)
  - prompt rendering (template + inputs + optional failure report)
  - JSON parsing of the model's reply (single top-level object)
  - the MCP tool schemas exposed to tool-using skills
  - `run_skill(...)` — the dispatcher
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import yaml
from pydantic import ValidationError

import artifacts as artifacts_svc
from gateway import LLM
from schemas import AgentResult, NodeSpec

ROOT = Path(__file__).parent
AGENT_CONFIG_PATH = ROOT / "agent_config.yaml"


# ── catalogue ────────────────────────────────────────────────────────────────

class Skill:
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.prompt_path = ROOT / cfg["prompt"]
        self.description = cfg.get("description", "")
        self.tools_allowed: list[str] = cfg.get("tools_allowed", []) or []
        self.internal_successors: list[str] = cfg.get("internal_successors", []) or []
        self.critic: bool = bool(cfg.get("critic", False))
        self.provider_pin: str | None = cfg.get("provider_pin")
        # P2 #10: per-skill temperature / max_tokens come from the yaml so
        # tuning a single skill no longer requires a code edit. Defaults
        # are deliberately conservative; a skill that wants exploration
        # (Researcher) bumps temperature; a skill that wants determinism
        # (Critic, Distiller) drops it to ~0.
        self.temperature: float = float(cfg.get("temperature", 0.3))
        self.max_tokens: int = int(cfg.get("max_tokens", 2048))

    def prompt_template(self) -> str:
        if not self.prompt_path.exists():
            return f"You are the {self.name} skill. (Prompt file missing.)"
        return self.prompt_path.read_text()


class SkillRegistry:
    def __init__(self):
        cfg = yaml.safe_load(AGENT_CONFIG_PATH.read_text())
        self._skills: dict[str, Skill] = {n: Skill(n, c) for n, c in cfg.items()}

    def get(self, name: str) -> Skill:
        if name not in self._skills:
            raise KeyError(f"unknown skill: {name}")
        return self._skills[name]

    def names(self) -> list[str]:
        return list(self._skills)


# ── input resolution + prompt rendering ──────────────────────────────────────

def resolve_inputs(node_inputs: list[str], graph_nodes, query: str) -> list[dict]:
    """Materialise each input id into a dict the prompt can serialise.

    Recognised input forms:
      - "USER_QUERY"  → the original user query text
      - "n:<i>"       → the AgentResult.output of that completed node
      - "art:<sha>"   → the bytes of an artifact, decoded as utf-8 best-effort
      - any other     → passed through as a free-form string

    `graph_nodes` is the nx node-view dict from flow.Graph; we read each
    upstream node's `result` attribute (set when the orchestrator marks
    the node complete).
    """
    out = []
    for inp in node_inputs:
        if inp == "USER_QUERY":
            out.append({"id": "USER_QUERY", "kind": "query", "value": query})
        elif inp.startswith("n:") and inp in graph_nodes:
            upstream = graph_nodes[inp].get("result")
            if isinstance(upstream, AgentResult):
                out.append({"id": inp, "kind": "upstream",
                            "skill": upstream.agent_name, "output": upstream.output})
            else:
                out.append({"id": inp, "kind": "upstream-missing", "output": None})
        elif inp.startswith("art:"):
            try:
                blob = artifacts_svc.get_bytes(inp)
                text = blob.decode("utf-8", errors="replace")
                out.append({"id": inp, "kind": "artifact", "text": text[:20_000]})
            except Exception as e:
                out.append({"id": inp, "kind": "artifact-missing", "error": str(e)})
        else:
            out.append({"id": inp, "kind": "literal", "value": inp})
    return out


def _format_memory_hits(hits: list) -> str:
    """Compact rendering of FAISS-ranked MemoryItem hits for the prompt.

    Each hit is shown as one line: kind, descriptor, source, plus a 400-char
    preview of `value.chunk` when present (indexed-document chunks) or of
    `value.raw` (classifier facts). The full chunk would blow the prompt,
    but the descriptor + preview is enough for the Planner to decide
    whether memory already covers the query and for downstream skills to
    synthesise from indexed material without an extra Retriever round-trip.
    """
    if not hits:
        return ""
    lines = []
    for h in hits[:8]:  # cap to keep the prompt bounded
        kind = getattr(h, "kind", "?")
        desc = (getattr(h, "descriptor", "") or "")[:200]
        source = getattr(h, "source", "")
        val = getattr(h, "value", {}) or {}
        chunk = val.get("chunk")
        raw = val.get("raw")
        line = f"  - [{kind}] {desc}"
        if source:
            line += f"\n      source: {source}"
        if isinstance(chunk, str) and chunk.strip():
            preview = chunk[:2000].replace("\n", " ")
            more = " …" if len(chunk) > 2000 else ""
            line += f"\n      chunk: {preview}{more}"
        elif isinstance(raw, str) and raw.strip():
            raw_more = " …" if len(raw) > 2000 else ""
            line += f"\n      raw: {raw[:2000]}{raw_more}"
        lines.append(line)
    return "\n".join(lines)


def render_prompt(skill: Skill, query: str, resolved: list[dict],
                  failure_report: str | None = None,
                  memory_hits: list | None = None,
                  question: str | None = None) -> str:
    parts = [skill.prompt_template().rstrip()]
    # USER_QUERY top-line: only when the Planner wired USER_QUERY into this
    # node's inputs. Earlier versions added it unconditionally, which
    # leaked the full original query into every fan-out worker — three
    # researcher siblings spawned to "find population of A / B / C" all
    # saw the same "compare A, B, C" query and each one ended up
    # searching for all three. Per-node scoping now travels through
    # `metadata.question` (rendered as QUESTION below) and the INPUTS
    # block; USER_QUERY is present only when the Planner asked for it.
    user_query_in_inputs = any(
        isinstance(r, dict) and r.get("id") == "USER_QUERY" for r in resolved
    )
    # formatter always needs the user query regardless of how it was wired
    if user_query_in_inputs or skill.name == "formatter":
        parts += ["", f"USER_QUERY: {query}"]
    # QUESTION: the per-node sub-question the Planner attached via
    # `metadata.question`. This is how a fan-out worker learns *its*
    # slice of the user's request without seeing the whole query.
    if isinstance(question, str) and question.strip():
        parts += ["", f"QUESTION: {question.strip()}"]
    if failure_report:
        parts += ["", f"FAILURE:\n{failure_report}"]
    # Memory hits — FAISS-ranked MemoryItems from session-start memory.read.
    # Same hits flow into every skill's prompt this run (the S7 contract:
    # every cognitive role can see what the agent already knows).
    hits_block = _format_memory_hits(memory_hits or [])
    if hits_block:
        parts += ["", f"MEMORY HITS ({len(memory_hits)} from FAISS):", hits_block]
    parts += ["", "INPUTS:", json.dumps(resolved, indent=2, default=str)[:20_000]]
    return "\n".join(parts)


def _extract_from_md_table(md: str) -> tuple:
    """Fallback: extract (labels, values, metric_name) by parsing a markdown table.

    Picks the last numeric column as the primary bar-chart value so that
    'Real Value' wins over 'Nominal Value' in a typical investment table.
    """
    import re

    def to_num(s: str):
        cleaned = re.sub(r"[$,+%]", "", (s or "").strip())
        try:
            return float(cleaned)
        except ValueError:
            return None

    lines = [l for l in md.replace("\\n", "\n").strip().splitlines() if l.strip()]
    use_pipe = any("|" in l for l in lines)
    headers: list[str] = []
    rows: list[list[str]] = []
    for line in lines:
        if use_pipe:
            if not line.strip().startswith("|"):
                continue
            cells = [re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", c).strip()
                     for c in line.strip().strip("|").split("|")]
            if all(("-" in c and set(c) <= set("|-: ")) for c in cells if c):
                continue
        else:
            raw = re.split(r"\t|  +", line.strip())
            cells = [re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", c).strip()
                     for c in raw if c.strip()]
            if not cells or all(not c or set(c) <= set("-: ") for c in cells):
                continue
        if not headers:
            headers = cells
        else:
            rows.append(cells)

    if not headers or not rows:
        return [], [], "Value"

    # find rightmost column that contains numbers across all rows
    val_col, metric_name = -1, "Value"
    for col_i in range(len(headers) - 1, 0, -1):
        nums = [to_num(r[col_i]) for r in rows if col_i < len(r)]
        if any(v is not None for v in nums):
            val_col = col_i
            metric_name = headers[col_i] if col_i < len(headers) else "Value"
            break

    if val_col == -1:
        return [], [], "Value"

    labels = [r[0] for r in rows if r]
    values = [to_num(r[val_col]) if val_col < len(r) else 0.0 for r in rows]
    values = [v if v is not None else 0.0 for v in values]
    return labels, values, metric_name


def _md_to_html_table(md: str) -> str:
    """Convert a markdown table (pipe or tab-separated) to an HTML table string."""
    import re as _re

    def _strip_md(s: str) -> str:
        """Remove markdown bold/italic markers and trailing backslash-n literals."""
        return _re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", s).replace("\\n", "").strip()

    if not md:
        return ""
    text = md.replace("\\n", "\n").strip()
    lines = [l for l in text.splitlines() if l.strip()]

    # Detect delimiter: pipe table takes priority; fall back to tab-separated.
    use_pipe = any("|" in l for l in lines)

    rows: list[list[str]] = []
    for line in lines:
        if use_pipe:
            if not line.strip().startswith("|"):
                continue
            cells = [_strip_md(c) for c in line.strip().strip("|").split("|")]
            # Skip separator rows (---|--- style)
            if all(not c or (set(c) <= set("|-: ")) for c in cells):
                continue
        else:
            # Tab-separated; also handle runs of spaces (2+ spaces as delimiter)
            raw = _re.split(r"\t|  +", line.strip())
            cells = [_strip_md(c) for c in raw if c.strip()]
            if not cells:
                continue
            # Skip separator rows
            if all(not c or set(c) <= set("-: ") for c in cells):
                continue

        rows.append(cells)

    if not rows:
        return f"<pre>{md}</pre>"

    header_row = rows[0]
    data_rows = rows[1:]
    thead = "<tr>" + "".join(f"<th>{c}</th>" for c in header_row) + "</tr>"
    tbody = "\n".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>"
        for row in data_rows
    )
    return f"<table><thead>{thead}</thead><tbody>\n{tbody}\n</tbody></table>"


def _write_chart_html(parsed: dict, *, session_id: str = "", query: str = "") -> None:
    """Generate a Chart.js HTML page from data_visualizer output and write to ~/s8_output/."""
    import os
    from datetime import datetime, timezone

    data = parsed.get("data") or {}
    labels = data.get("labels") or []
    values = data.get("values") or []
    metric_name = data.get("metric_name", "Value")
    caption = parsed.get("caption", "")
    table_md = parsed.get("table", "")

    if not labels or not values:
        labels, values, metric_name = _extract_from_md_table(table_md)

    if not labels or not values:
        return

    n = min(len(labels), len(values))
    labels = [str(l) for l in labels[:n]]
    values = [float(v) if v is not None else 0.0 for v in values[:n]]

    # Horizontal bars for many items or long labels
    index_axis = "y" if (len(labels) > 5 or max((len(l) for l in labels), default=0) > 12) else "x"
    value_axis = "x" if index_axis == "y" else "y"

    max_val = max(values) if values else 0.0
    min_val = min(values) if values else 0.0
    winner_idx = values.index(max_val) if max_val != 0.0 else -1
    winner_label = labels[winner_idx] if winner_idx >= 0 else "—"

    # Ranked sorted pairs for stat cards
    ranked = sorted(zip(labels, values), key=lambda x: x[1], reverse=True)

    PALETTE = ["#4361ee", "#f4a261", "#e76f51", "#2a9d8f", "#9b5de5", "#f72585"]
    WINNER_COLOR = "#2dc96a"
    bar_colors = [
        WINNER_COLOR if i == winner_idx else PALETTE[i % len(PALETTE)]
        for i in range(n)
    ]

    # Stat cards: winner + runner-up + spread
    def _fmt(v: float) -> str:
        if abs(v) >= 1_000_000:
            return f"{v/1_000_000:.2f}M"
        if abs(v) >= 1_000:
            return f"{v:,.0f}"
        return f"{v:.3g}"

    stat_cards_html = ""
    if ranked:
        w_label, w_val = ranked[0]
        stat_cards_html += (
            f"<div class='stat winner-stat'>"
            f"<span class='stat-badge'>&#127942; Winner</span>"
            f"<div class='stat-label'>{w_label}</div>"
            f"<div class='stat-value'>{_fmt(w_val)}</div>"
            f"</div>"
        )
        if len(ranked) > 1:
            r_label, r_val = ranked[1]
            stat_cards_html += (
                f"<div class='stat'>"
                f"<span class='stat-badge'>2nd</span>"
                f"<div class='stat-label'>{r_label}</div>"
                f"<div class='stat-value'>{_fmt(r_val)}</div>"
                f"</div>"
            )
        if len(ranked) > 1:
            spread_pct = ((max_val - min_val) / abs(min_val) * 100) if min_val != 0 else 0
            stat_cards_html += (
                f"<div class='stat'>"
                f"<span class='stat-badge'>Spread</span>"
                f"<div class='stat-label'>Max vs Min</div>"
                f"<div class='stat-value'>{spread_pct:.0f}%</div>"
                f"</div>"
            )

    # Winner row index in table (0-based data row, not DOM row)
    winner_row_js = f"const winnerIdx = {winner_idx};" if winner_idx >= 0 else "const winnerIdx = -1;"

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    meta_parts = []
    if session_id:
        meta_parts.append(f"Session {session_id}")
    meta_parts.append(ts)
    meta_line = " &nbsp;·&nbsp; ".join(meta_parts)

    query_preview = ""
    if query:
        q = query[:160] + ("…" if len(query) > 160 else "")
        query_preview = f"<p class='query-preview'><strong>Query:</strong> {q}</p>"

    html_table = _md_to_html_table(table_md)

    # Tooltip + datalabel formatters
    tooltip_fmt = (
        "function(ctx){"
        f"const v=ctx.parsed['{value_axis}'];"
        "return ' '+v.toLocaleString(undefined,{maximumFractionDigits:2});}"
    )
    datalabel_fmt = (
        "function(v){"
        "if(Math.abs(v)>=1e6) return (v/1e6).toFixed(2)+'M';"
        "if(Math.abs(v)>=1e3) return v.toLocaleString(undefined,{maximumFractionDigits:0});"
        "return v.toLocaleString(undefined,{maximumFractionDigits:2});}"
    )

    out_dir = Path(os.path.expanduser("~/s8_output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "visualization.html"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{metric_name} — DataVisualizer</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0/dist/chartjs-plugin-datalabels.min.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
    max-width: 1020px; margin: 36px auto; padding: 0 28px 48px;
    background: #f2f4f8; color: #1a1f2e; line-height: 1.5;
  }}
  h1 {{ font-size: 1.5rem; font-weight: 700; margin: 0 0 2px; color: #1a1f2e; }}
  .meta {{ font-size: .75rem; color: #999; margin-bottom: 8px; letter-spacing: .02em; }}
  .query-preview {{
    font-size: .82rem; color: #666; background: #fff;
    border-left: 3px solid #4361ee; padding: 7px 12px;
    border-radius: 0 6px 6px 0; margin-bottom: 18px;
  }}
  .caption {{
    font-size: .94rem; color: #333;
    background: #fff; border-left: 4px solid {WINNER_COLOR};
    padding: 10px 16px; border-radius: 0 8px 8px 0;
    margin-bottom: 20px;
  }}
  /* stat cards */
  .stats {{ display: flex; gap: 14px; margin-bottom: 20px; flex-wrap: wrap; }}
  .stat {{
    flex: 1; min-width: 140px; background: #fff;
    border-radius: 10px; padding: 14px 18px;
    box-shadow: 0 1px 6px rgba(0,0,0,.07);
  }}
  .winner-stat {{ border-top: 3px solid {WINNER_COLOR}; }}
  .stat-badge {{
    display: inline-block; font-size: .7rem; font-weight: 700;
    letter-spacing: .06em; text-transform: uppercase;
    color: #888; margin-bottom: 6px;
  }}
  .winner-stat .stat-badge {{ color: {WINNER_COLOR}; }}
  .stat-label {{ font-size: .88rem; font-weight: 600; color: #333; margin-bottom: 4px; }}
  .stat-value {{ font-size: 1.35rem; font-weight: 700; color: #1a1f2e; }}
  /* chart card */
  .card {{
    background: #fff; border-radius: 12px;
    padding: 24px 28px; box-shadow: 0 2px 10px rgba(0,0,0,.07);
    margin-bottom: 20px;
  }}
  .card-title {{
    font-size: .88rem; font-weight: 600; color: #888;
    text-transform: uppercase; letter-spacing: .05em; margin: 0 0 16px;
  }}
  /* table */
  .table-card {{
    background: #fff; border-radius: 12px;
    box-shadow: 0 2px 10px rgba(0,0,0,.07);
    overflow: hidden; margin-bottom: 20px;
  }}
  .table-card table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
  .table-card th {{
    background: #1a1f2e; color: #e8eaf0;
    padding: 11px 16px; font-weight: 600; font-size: .82rem;
    text-transform: uppercase; letter-spacing: .04em;
  }}
  .table-card th:not(:first-child) {{ text-align: right; }}
  .table-card td {{ padding: 10px 16px; border-bottom: 1px solid #edf0f4; color: #333; }}
  .table-card td:first-child {{ font-weight: 500; }}
  .table-card td:not(:first-child) {{ text-align: right; }}
  .table-card tr:last-child td {{ border-bottom: none; }}
  .table-card tr:nth-child(even) td {{ background: #f8fafc; }}
  .table-card tr.winner-row td {{
    background: #f0fdf6 !important;
    font-weight: 700; color: #166534;
  }}
  footer {{
    font-size: .73rem; color: #bbb; text-align: center; margin-top: 12px;
  }}
</style>
</head>
<body>
<h1>Comparison Analysis</h1>
<p class="meta">{meta_line}</p>
{query_preview}
<div class="caption">{caption}</div>
<div class="stats">{stat_cards_html}</div>
<div class="card">
  <p class="card-title">{metric_name}</p>
  <canvas id="chart"></canvas>
</div>
<div class="table-card">{html_table}</div>
<footer>DataVisualizer &mdash; EAGv3 Session 8 &mdash; sandbox-verified output</footer>

<script>
Chart.register(ChartDataLabels);
{winner_row_js}
const labels = {json.dumps(labels)};
const values = {json.dumps(values)};
const colors = {json.dumps(bar_colors)};

// highlight winner row in table
if (winnerIdx >= 0) {{
  const rows = document.querySelectorAll('.table-card tbody tr');
  if (rows[winnerIdx]) rows[winnerIdx].classList.add('winner-row');
}}

new Chart(document.getElementById('chart'), {{
  type: 'bar',
  data: {{
    labels,
    datasets: [{{
      label: {json.dumps(metric_name)},
      data: values,
      backgroundColor: colors,
      borderRadius: 8,
      borderSkipped: false,
    }}]
  }},
  options: {{
    indexAxis: '{index_axis}',
    responsive: true,
    animation: {{ duration: 700, easing: 'easeOutCubic' }},
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        callbacks: {{
          label: {tooltip_fmt}
        }}
      }},
      datalabels: {{
        color: '#fff',
        font: {{ weight: 'bold', size: 12 }},
        anchor: 'end',
        align: 'start',
        offset: 6,
        formatter: {datalabel_fmt}
      }}
    }},
    scales: {{
      {index_axis}: {{
        grid: {{ display: false }},
        ticks: {{ color: '#555', font: {{ size: 12 }} }}
      }},
      {value_axis}: {{
        grid: {{ color: '#f0f0f0' }},
        ticks: {{
          color: '#888', font: {{ size: 11 }},
          callback: {datalabel_fmt}
        }},
        beginAtZero: true
      }}
    }}
  }}
}});
</script>
</body>
</html>"""

    out_path.write_text(html, encoding="utf-8")
    parsed["html_path"] = str(out_path)
    print(f"\n[data_visualizer] HTML chart → {out_path}")
    print(f"[data_visualizer] Open in browser: file://{out_path}\n")


def parse_skill_json(text: str) -> dict:
    """Skills return a single top-level JSON object. Strip markdown fences
    if the model added them despite being told not to."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[:-3]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                pass
    return {}


# ── MCP tool schemas exposed through the gateway tools= channel ──────────────

_TOOL_CATALOG = {
    "web_search": {
        "name": "web_search",
        "description": "Search the web (Tavily primary, DDG fallback). Hard-capped at 5 results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 3},
            },
            "required": ["query"],
        },
    },
    "fetch_url": {
        "name": "fetch_url",
        "description": "Fetch clean markdown from a URL via crawl4ai.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    "search_knowledge": {
        "name": "search_knowledge",
        "description": "Vector search over the agent's indexed knowledge base.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
}


def tool_payload(tool_names: list[str]) -> list[dict] | None:
    if not tool_names:
        return None
    return [_TOOL_CATALOG[n] for n in tool_names if n in _TOOL_CATALOG]


# ── per-node execution ───────────────────────────────────────────────────────

async def run_skill(skill: Skill, node_id: str, graph_nodes,
                    session_id: str, query: str,
                    failure_report: str | None,
                    *, memory_hits: list | None = None) -> tuple[AgentResult, str]:
    """Dispatch one node. Returns (result, rendered_prompt).

    `memory_hits` is the FAISS-ranked MemoryItem list captured once at
    session start by Executor.run and threaded through here so every
    skill's prompt can see the same hits. This is the S7 promise carried
    forward — Memory works in S8 because the orchestrator delivers the
    hits, not just because the FAISS index is on disk.

    sandbox_executor bypasses the gateway: it picks the `code` field out of
    its upstream coder node and runs sandbox.run_python directly. All other
    skills are LLM-backed and route through the V8 gateway with
    agent=<skill_name> so agent_routing.yaml + cost-by-agent kick in."""
    resolved = resolve_inputs(graph_nodes[node_id]["inputs"], graph_nodes, query)
    # Per-node sub-question from the Planner's `metadata.question`. Travels
    # into the rendered prompt as a QUESTION: block so a fan-out worker
    # (e.g. one of three researchers spawned to cover three cities) can
    # see *its* slice of the user's request even when USER_QUERY is not
    # in its inputs.
    node_meta = graph_nodes[node_id].get("metadata") or {}
    question = node_meta.get("question") if isinstance(node_meta, dict) else None
    rendered = render_prompt(skill, query, resolved, failure_report,
                             memory_hits=memory_hits, question=question)
    started = time.time()

    if skill.name == "sandbox_executor":
        code = ""
        for r in resolved:
            if r.get("kind") == "upstream" and isinstance(r.get("output"), dict):
                code = r["output"].get("code") or code
        if not code:
            return AgentResult(
                success=False, agent_name=skill.name,
                error="no code in upstream coder output",
                elapsed_s=time.time() - started,
            ), rendered
        from sandbox import run_python
        out = run_python(code)
        return AgentResult(
            success=(out["exit_code"] == 0 and not out["timed_out"]),
            agent_name=skill.name, output=out,
            elapsed_s=time.time() - started,
        ), rendered

    tools = tool_payload(skill.tools_allowed)
    if tools:
        # Multi-turn tool-use loop. mcp_runner opens one MCP stdio session
        # per skill invocation, dispatches each tool_call the model emits,
        # and feeds the results back until the model produces final text.
        from mcp_runner import run_with_tools
        reply = await run_with_tools(
            prompt=rendered,
            tools_payload=tools,
            agent=skill.name,
            session_id=session_id,
            provider_pin=skill.provider_pin,
            max_tokens=skill.max_tokens,
            temperature=skill.temperature,
        )
    else:
        reply = await asyncio.to_thread(
            LLM().chat,
            prompt=rendered,
            agent=skill.name,
            session=session_id,
            provider=skill.provider_pin,
            max_tokens=skill.max_tokens,
            temperature=skill.temperature,
        )
    raw_text = reply.get("text", "")
    parsed = parse_skill_json(raw_text)
    # When a tool-using skill writes prose instead of JSON, parse_skill_json
    # returns {}. Wrap the raw text as `findings` so the distiller/formatter
    # downstream can still extract from it rather than receiving empty input.
    if not parsed and raw_text.strip() and skill.tools_allowed:
        parsed = {"findings": raw_text.strip()[:10_000]}

    # data_visualizer fallback: if the LLM returned empty/malformed output, build
    # a minimal table+caption directly from the upstream sandbox stdout so the
    # downstream formatter always has something to work with.
    if skill.name == "data_visualizer" and not parsed.get("table"):
        for r in resolved:
            if r.get("kind") == "upstream" and isinstance(r.get("output"), dict):
                stdout = r["output"].get("stdout", "")
                if not stdout:
                    continue
                try:
                    sb = json.loads(stdout)
                    summary = sb.get("summary") or {}
                    if summary:
                        # Build a minimal markdown table from sandbox stdout
                        metrics = list(next(iter(summary.values())).keys()) if summary else []
                        header = "| Asset | " + " | ".join(metrics) + " |"
                        sep = "|---|" + "---|" * len(metrics)
                        rows = [
                            "| " + name + " | " + " | ".join(str(v) for v in vals.values()) + " |"
                            for name, vals in summary.items()
                        ]
                        parsed["table"] = "\n".join([header, sep] + rows)
                        parsed["caption"] = sb.get("note", "")
                        parsed["chart"] = ""
                        parsed["data"] = {}
                        print("[data_visualizer] WARNING: LLM returned empty output; "
                              "synthesised table from sandbox stdout.")
                except Exception:
                    pass
                break

    # Write an HTML Chart.js visualization whenever data_visualizer returns a table.
    # Falls back to parsing the markdown table when the LLM omits the `data` block.
    if skill.name == "data_visualizer" and parsed.get("table"):
        _write_chart_html(parsed, session_id=session_id, query=query)

    # Lift orchestrator-recognised fields out of the skill's JSON.
    # NOTES_RUNS feedback P0 #1: malformed successors used to be silently
    # dropped, which left students chasing "missing node" bugs for an hour.
    # Now: log the offending JSON + the validation error, then fail the
    # node so the failure path (and replay) surfaces it.
    raw_successors = parsed.pop("successors", []) or []
    successors: list[NodeSpec] = []
    rejected: list[str] = []
    for s in raw_successors:
        try:
            successors.append(NodeSpec.model_validate(s))
        except ValidationError as ve:
            rejected.append(f"successor={s!r}  error={ve}")
    if skill.name == "planner":
        for s in parsed.get("nodes", []) or []:
            try:
                successors.append(NodeSpec.model_validate(s))
            except ValidationError as ve:
                rejected.append(f"node={s!r}  error={ve}")

    if rejected:
        err = (
            f"{skill.name}: {len(rejected)} malformed NodeSpec(s) emitted.\n"
            + "\n".join(f"  - {line}" for line in rejected)
        )
        print(f"[skills] {err}")
        return AgentResult(
            success=False, agent_name=skill.name,
            output=parsed, successors=successors,
            elapsed_s=time.time() - started,
            provider=reply.get("provider", ""),
            error=err,
        ), rendered

    return AgentResult(
        success=True,
        agent_name=skill.name,
        output=parsed,
        successors=successors,
        elapsed_s=time.time() - started,
        provider=reply.get("provider", ""),
    ), rendered
