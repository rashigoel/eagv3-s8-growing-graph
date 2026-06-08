"""Parallel timing visualiser for Session 8 runs.

Shows a Gantt-style bar chart of node elapsed times, identifies parallel
batches, and reports wall-clock vs serial-equivalent speedup.

Usage:
    uv run python timing.py                 # most recent session
    uv run python timing.py <session_id>    # specific session
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from persistence import SessionStore, list_sessions
from schemas import NodeState


BAR_WIDTH = 30   # max █ blocks for the longest node


def _bar(elapsed: float, max_elapsed: float) -> str:
    if max_elapsed == 0:
        return ""
    blocks = max(1, round(elapsed / max_elapsed * BAR_WIDTH))
    return "█" * blocks


def _fmt(s: float) -> str:
    return f"{s:.1f}s"


def _parallel_batches(states: list[NodeState]) -> dict[str, list[str]]:
    """Group node_ids that overlapped in time into batches.
    Returns {batch_key: [node_id, ...]} where batch_key is the earliest
    started_at in the batch (as a string, for stable ordering)."""
    # Sort by started_at
    sorted_states = sorted(states, key=lambda s: s.started_at or 0)
    batches: list[list[NodeState]] = []
    for st in sorted_states:
        placed = False
        for batch in batches:
            # Overlaps if this node started before the latest end in the batch
            batch_end = max((b.completed_at or 0) for b in batch)
            if (st.started_at or 0) < batch_end:
                batch.append(st)
                placed = True
                break
        if not placed:
            batches.append([st])
    return batches


def show_timing(session_id: str) -> int:
    store = SessionStore(session_id)
    states = store.read_all_nodes()
    if not states:
        print(f"timing: no nodes under state/sessions/{session_id}/", file=sys.stderr)
        return 2

    query = store.read_query() or ""
    print()
    print(f"session  {session_id}")
    print(f"query    {query[:120]}{'…' if len(query) > 120 else ''}")
    print()

    # Build timing data
    max_elapsed = max((s.result.elapsed_s for s in states if s.result and s.result.elapsed_s), default=1)
    session_start = min((s.started_at for s in states if s.started_at), default=0)
    session_end   = max((s.completed_at for s in states if s.completed_at), default=0)
    total_wall    = session_end - session_start
    total_serial  = sum(s.result.elapsed_s for s in states if s.result and s.result.elapsed_s)

    # Detect parallel batches
    batches = _parallel_batches(states)

    # Print header
    col_node    = 6
    col_skill   = 20
    col_bar     = BAR_WIDTH + 2
    col_elapsed = 8
    sep = "─" * (col_node + col_skill + col_bar + col_elapsed + 8)
    print(sep)
    print(f"{'Node':<{col_node}} {'Skill':<{col_skill}} {'Elapsed':>{col_elapsed}}   {'Timeline'}")
    print(sep)

    node_to_batch: dict[str, int] = {}
    for i, batch in enumerate(batches):
        for st in batch:
            node_to_batch[st.node_id] = i

    prev_batch = -1
    for st in states:
        nid     = st.node_id
        skill   = st.skill
        elapsed = st.result.elapsed_s if st.result and st.result.elapsed_s else 0
        bar     = _bar(elapsed, max_elapsed)
        batch_i = node_to_batch.get(nid, -1)
        batch   = batches[batch_i] if batch_i >= 0 else []

        # Print batch separator with parallel annotation
        if batch_i != prev_batch:
            if len(batch) > 1:
                # Compute parallel layer stats
                b_starts  = [b.started_at or 0 for b in batch]
                b_ends    = [b.completed_at or 0 for b in batch]
                b_indivs  = [b.result.elapsed_s for b in batch if b.result and b.result.elapsed_s]
                wall      = max(b_ends) - min(b_starts)
                serial    = sum(b_indivs)
                speedup   = serial / wall if wall > 0 else 1
                print(f"\n  ┌─ parallel batch ({len(batch)} nodes) "
                      f"wall={_fmt(wall)}  serial={_fmt(serial)}  speedup={speedup:.2f}×")
            elif prev_batch >= 0:
                print()
            prev_batch = batch_i

        # Side marker for parallel nodes
        if len(batch) > 1:
            is_last = (batch[-1].node_id == nid)
            marker  = "  └─ " if is_last else "  ├─ "
        else:
            marker  = "     "

        print(f"{marker}{nid:<{col_node}} {skill:<{col_skill}} {_fmt(elapsed):>{col_elapsed}}   {bar}")

    print()
    print(sep)
    print(f"  total wall-clock      : {_fmt(total_wall)}")
    print(f"  serial equivalent     : {_fmt(total_serial)}")
    print(f"  overall speedup       : {total_serial/total_wall:.2f}× (parallel layers shorten the critical path)")
    print(sep)
    print()
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        sessions = list_sessions()
        if not sessions:
            print("timing: no sessions under state/sessions/", file=sys.stderr)
            return 2
        session_id = sessions[-1]
        print(f"(no session id given — using most recent: {session_id})")
    else:
        session_id = args[0]
    return show_timing(session_id)


if __name__ == "__main__":
    sys.exit(main())
