"""Generate a self-contained Markdown report for one persisted session.

Reads `state/sessions/<sid>/` (query, graph, per-node JSON, browser
artifacts) plus the gateway's per-session cost rollup, and writes
`state/sessions/<sid>/report.md`. The report has eight sections that
map 1:1 onto the rubric:

    1. Original user goal
    2. Planner DAG
    3. Browser path chosen
    4. Browser actions taken
    5. Screenshots
    6. Extracted data
    7. Final comparison table (formatter output)
    8. Turn count and cost summary

This script is read-only with respect to existing project code: it
imports SessionStore + schemas only. No edits to flow.py, skills.py,
persistence.py, or the orchestrator. Run it after a flow run:

    uv run python report.py <session_id>
    uv run python report.py                  # lists sessions

The cost block calls the gateway at http://localhost:8109; if the
gateway is offline the cost section degrades to "(gateway offline)"
without failing the report.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from persistence import SessionStore, list_sessions
from schemas import NodeState

ROOT = Path(__file__).parent
SESSIONS_ROOT = ROOT / "state" / "sessions"
GATEWAY_URL = "http://localhost:8109"
MAX_CONTENT_CHARS = 4000  # truncate per-node raw content in §6
MAX_PROMPT_CHARS = 1500


# ── helpers ──────────────────────────────────────────────────────────────────

def _md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[:n] + f"\n\n_... [truncated, {len(s) - n} more chars]_"


def _fetch_cost(session_id: str) -> dict | None:
    try:
        import httpx
        r = httpx.get(
            f"{GATEWAY_URL}/v1/cost/by_agent",
            params={"session": session_id},
            timeout=5.0,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _read_graph(store: SessionStore):
    try:
        return store.read_graph()
    except Exception as e:
        print(f"[report] WARNING: could not read graph.json: {e}", file=sys.stderr)
        return None


def _is_browser(st: NodeState) -> bool:
    return st.skill == "browser"


def _is_formatter(st: NodeState) -> bool:
    return st.skill == "formatter"


def _browser_artifacts_root(session_id: str) -> Path:
    return SESSIONS_ROOT / session_id / "browser"


def _list_browser_dirs(session_id: str) -> list[Path]:
    root = _browser_artifacts_root(session_id)
    if not root.exists():
        return []
    return sorted([p for p in root.iterdir() if p.is_dir()])


def _screenshots_for_node(node_idx: int, browser_dirs: list[Path]) -> list[Path]:
    """Best-effort match: the n-th browser node maps to the n-th
    `browser_<ts>` subdirectory (they're created in order at runtime).
    Returns all turn_*.png under that subdir's a11y/ and vision/ folders."""
    if node_idx >= len(browser_dirs):
        return []
    bdir = browser_dirs[node_idx]
    out: list[Path] = []
    for layer in ("a11y", "vision"):
        sub = bdir / layer
        if sub.exists():
            out.extend(sorted(sub.glob("turn_*.png")))
            out.extend(sorted(sub.glob("turn_*.jpg")))
    return out


# ── section renderers ────────────────────────────────────────────────────────

def _section_header(sid: str, query: str, n_nodes: int) -> str:
    return (
        f"# Session report — `{sid}`\n\n"
        f"**Nodes:** {n_nodes}  |  **Query length:** {len(query)} chars\n\n"
        "---\n"
    )


def _section_1_goal(query: str) -> str:
    return (
        "## 1. Original user goal\n\n"
        "```text\n"
        f"{query.strip() or '(empty)'}\n"
        "```\n"
    )


def _section_2_dag(graph) -> str:
    if graph is None:
        return "## 2. Planner DAG\n\n_(graph.json missing or unreadable)_\n"
    lines = ["## 2. Planner DAG\n", "```mermaid", "graph TD"]
    for nid, d in graph.nodes(data=True):
        skill = d.get("skill", "?")
        status = d.get("status", "?")
        safe = nid.replace(":", "_")
        lines.append(f'    {safe}["{nid}<br/>{skill}<br/>({status})"]')
    for u, v in graph.edges():
        lines.append(f"    {u.replace(':','_')} --> {v.replace(':','_')}")
    lines.append("```\n")
    lines.append("**Adjacency** (text fallback):\n")
    lines.append("| node | skill | status | predecessors |")
    lines.append("|---|---|---|---|")
    for nid, d in graph.nodes(data=True):
        preds = ", ".join(graph.predecessors(nid)) or "—"
        lines.append(
            f"| `{nid}` | {d.get('skill','?')} | "
            f"{d.get('status','?')} | {preds} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def _section_3_browser_paths(states: list[NodeState]) -> str:
    rows = ["## 3. Browser path chosen\n"]
    rows.append("| node | url | path | turns | error_code | success |")
    rows.append("|---|---|---|---|---|---|")
    found = False
    for st in states:
        if not _is_browser(st):
            continue
        found = True
        out = (st.result.output if st.result else {}) or {}
        url = (out.get("url") or "").strip() or "—"
        path = out.get("path") or "—"
        turns = out.get("turns", 0)
        err = (st.result.error_code if st.result else None) or "—"
        ok = "yes" if (st.result and st.result.success) else "no"
        rows.append(
            f"| `{st.node_id}` | `{_md_escape(url)[:80]}` | `{path}` | "
            f"{turns} | `{err}` | {ok} |"
        )
    if not found:
        rows.append("| _(no browser nodes in this session)_ |  |  |  |  |  |")
    rows.append("")
    return "\n".join(rows) + "\n"


def _section_4_actions(states: list[NodeState]) -> str:
    parts = ["## 4. Browser actions taken\n"]
    any_browser = False
    for st in states:
        if not _is_browser(st):
            continue
        any_browser = True
        out = (st.result.output if st.result else {}) or {}
        url = out.get("url") or "—"
        path = out.get("path") or "—"
        actions = out.get("actions") or []
        parts.append(f"### `{st.node_id}` — {url}  (path = `{path}`)\n")
        if not actions:
            parts.append("_(no per-turn actions recorded — Layer 1 extract path or no interaction needed)_\n")
            continue
        parts.append("| turn | actions | outcome |")
        parts.append("|---|---|---|")
        for a in actions:
            turn = a.get("turn", "—")
            outcome = a.get("outcome", "—")
            try:
                acts_str = json.dumps(a.get("actions") or [], ensure_ascii=False)
            except (TypeError, ValueError):
                acts_str = str(a.get("actions"))
            if len(acts_str) > 200:
                acts_str = acts_str[:200] + "…"
            parts.append(f"| {turn} | `{_md_escape(acts_str)}` | {_md_escape(str(outcome))} |")
        parts.append("")
    if not any_browser:
        parts.append("_(no browser nodes in this session)_\n")
    return "\n".join(parts) + "\n"


def _section_5_screenshots(session_id: str, states: list[NodeState]) -> str:
    parts = ["## 5. Screenshots / page-state logs\n"]
    bdirs = _list_browser_dirs(session_id)
    if not bdirs:
        parts.append("_(no browser/ artifact directory for this session)_\n")
        return "\n".join(parts) + "\n"
    browser_states = [st for st in states if _is_browser(st)]
    for idx, st in enumerate(browser_states):
        shots = _screenshots_for_node(idx, bdirs)
        out = (st.result.output if st.result else {}) or {}
        url = out.get("url") or "—"
        path = out.get("path") or "—"
        parts.append(f"### `{st.node_id}` — {url}  (path = `{path}`)\n")
        if not shots:
            parts.append("_(no screenshots — Layer 1 extract path uses no Playwright)_\n")
            continue
        for s in shots:
            rel = s.relative_to(SESSIONS_ROOT / session_id).as_posix()
            parts.append(f"![{s.name}]({rel})  ")
            parts.append(f"_{s.name}_\n")
    if len(browser_states) < len(bdirs):
        leftover = bdirs[len(browser_states):]
        parts.append("\n_Additional unmatched artifact directories (likely from recovery branches):_\n")
        for d in leftover:
            parts.append(f"- `{d.relative_to(SESSIONS_ROOT / session_id).as_posix()}/`")
    return "\n".join(parts) + "\n"


def _section_6_extracted(states: list[NodeState]) -> str:
    parts = ["## 6. Extracted data (per node)\n"]
    for st in states:
        if not _is_browser(st):
            continue
        out = (st.result.output if st.result else {}) or {}
        url = out.get("url") or "—"
        final_url = out.get("final_url") or url
        content = out.get("content") or ""
        parts.append(f"### `{st.node_id}` — `{final_url}`\n")
        if content:
            parts.append("```text")
            parts.append(_truncate(content, MAX_CONTENT_CHARS))
            parts.append("```")
        else:
            parts.append("_(no extracted content; this node may have been an interaction-only goal)_")
        parts.append("")

    distillers = [st for st in states if st.skill == "distiller"]
    if distillers:
        parts.append("### Distiller outputs (structured records)\n")
        for st in distillers:
            out = (st.result.output if st.result else {}) or {}
            try:
                blob = json.dumps(out, indent=2, ensure_ascii=False)
            except (TypeError, ValueError):
                blob = str(out)
            parts.append(f"#### `{st.node_id}`")
            parts.append("```json")
            parts.append(_truncate(blob, MAX_CONTENT_CHARS))
            parts.append("```")
            parts.append("")
    return "\n".join(parts) + "\n"


def _section_7_final_table(states: list[NodeState]) -> str:
    parts = ["## 7. Final comparison table (formatter output)\n"]
    formatters = [st for st in states if _is_formatter(st)]
    if not formatters:
        parts.append("_(no formatter node in this session)_\n")
        return "\n".join(parts) + "\n"
    last = formatters[-1]
    out = (last.result.output if last.result else {}) or {}
    final = out.get("final_answer")
    if isinstance(final, str) and final.strip():
        parts.append(final)
    else:
        try:
            blob = json.dumps(out, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            blob = str(out)
        parts.append("_(no `final_answer` field; raw formatter output below)_\n")
        parts.append("```json")
        parts.append(_truncate(blob, MAX_CONTENT_CHARS))
        parts.append("```")
    parts.append("")
    return "\n".join(parts) + "\n"


def _section_8_summary(states: list[NodeState], cost: dict | None) -> str:
    parts = ["## 8. Turn count and cost summary\n"]

    by_skill_nodes: dict[str, int] = defaultdict(int)
    by_skill_turns: dict[str, int] = defaultdict(int)
    by_skill_elapsed: dict[str, float] = defaultdict(float)
    by_skill_provider: dict[str, set] = defaultdict(set)
    total_elapsed = 0.0

    for st in states:
        by_skill_nodes[st.skill] += 1
        if st.result:
            by_skill_elapsed[st.skill] += st.result.elapsed_s or 0.0
            total_elapsed += st.result.elapsed_s or 0.0
            if st.result.provider:
                by_skill_provider[st.skill].add(st.result.provider)
            if _is_browser(st):
                by_skill_turns[st.skill] += int((st.result.output or {}).get("turns", 0) or 0)

    parts.append("| skill | nodes | total_turns | total_elapsed_s | providers |")
    parts.append("|---|---|---|---|---|")
    for skill in sorted(by_skill_nodes):
        provs = ", ".join(sorted(by_skill_provider[skill])) or "—"
        parts.append(
            f"| `{skill}` | {by_skill_nodes[skill]} | "
            f"{by_skill_turns[skill] or '—'} | "
            f"{by_skill_elapsed[skill]:.1f} | {provs} |"
        )
    parts.append(f"\n**Total wall-clock (sum of node elapsed_s):** {total_elapsed:.1f} s\n")

    parts.append("### Cost (USD) — from gateway `/v1/cost/by_agent?session=<sid>`\n")
    if cost is None:
        parts.append("_(gateway offline or unreachable; skip)_\n")
        return "\n".join(parts) + "\n"
    if not cost:
        parts.append("_(gateway returned no cost rows for this session)_\n")
        return "\n".join(parts) + "\n"
    parts.append("| agent | calls | in_tokens | out_tokens | dollars |")
    parts.append("|---|---|---|---|---|")
    grand_dollars = 0.0
    for agent in sorted(cost):
        rows = cost.get(agent) or []
        n = len(rows)
        in_tok = sum((r.get("in_tok") or 0) for r in rows)
        out_tok = sum((r.get("out_tok") or 0) for r in rows)
        dollars = sum((r.get("dollars") or 0.0) for r in rows)
        grand_dollars += dollars
        parts.append(f"| `{agent}` | {n} | {in_tok} | {out_tok} | ${dollars:.4f} |")
    parts.append(f"\n**Total: ${grand_dollars:.4f}**\n")
    return "\n".join(parts) + "\n"


# ── driver ───────────────────────────────────────────────────────────────────

def build_report(session_id: str) -> Path:
    session_dir = SESSIONS_ROOT / session_id
    if not session_dir.exists():
        raise FileNotFoundError(f"no such session directory: {session_dir}")

    store = SessionStore(session_id)
    states = store.read_all_nodes()
    query = store.read_query() or ""
    graph = _read_graph(store)
    cost = _fetch_cost(session_id)

    sections = [
        _section_header(session_id, query, len(states)),
        _section_1_goal(query),
        _section_2_dag(graph),
        _section_3_browser_paths(states),
        _section_4_actions(states),
        _section_5_screenshots(session_id, states),
        _section_6_extracted(states),
        _section_7_final_table(states),
        _section_8_summary(states, cost),
    ]
    text = "\n".join(sections).rstrip() + "\n"
    out_path = session_dir / "report.md"
    out_path.write_text(text, encoding="utf-8")
    return out_path


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in {"-h", "--help", "help"}:
        sessions = list_sessions()
        print("usage: uv run python report.py <session_id>")
        print("       uv run python report.py --all")
        if sessions:
            print("\navailable sessions (most recent first):")
            for s in sessions:
                print(f"  {s}")
        else:
            print("\n(no sessions yet under state/sessions/)")
        return 0

    if args[0] == "--all":
        sessions = list_sessions()
        if not sessions:
            print("report: no sessions to render", file=sys.stderr)
            return 2
        rc = 0
        for sid in sessions:
            try:
                out = build_report(sid)
                print(f"[report] wrote {out}")
            except Exception as e:
                print(f"[report] FAILED for {sid}: {type(e).__name__}: {e}", file=sys.stderr)
                rc = 1
        return rc

    sid = args[0]
    try:
        out = build_report(sid)
    except FileNotFoundError as e:
        print(f"report: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"report: FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"[report] wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
