"""Which Space belongs to which root, what the sidebar says, which claims to release, and the
session map (spec Section 2 and the 2026-10-12 amendments). Pure: snapshots in, a plan out."""
import time
from dataclasses import dataclass, field

from hx.herdr import Pane, Workspace

BUSY = ("working", "blocked")
REFRESH_S = 600     # herdr forgets tokens when it restarts: re-report at least this often
ABSENT_S = 120      # a pane that still exists but shows no session releases its claims after this
MISSING_S = 60      # a workspace missing from the list is gone only after this


@dataclass(frozen=True)
class Report:
    key: str
    kind: str            # "ws" (token hx) or "pane" (token hx_sub)
    target: str
    value: str | None    # None clears the token


@dataclass(frozen=True)
class Close:
    key: str
    root: str
    workspace: str


@dataclass(frozen=True)
class Release:
    key: str
    session: str


@dataclass(frozen=True)
class MapRow:
    key: str
    session: str
    item: str
    role: str
    row: dict


@dataclass(frozen=True)
class Note:
    key: str
    text: str


@dataclass
class Snapshot:
    claims: list[dict]          # trk claims "claims"
    items: dict[str, dict]      # id -> trk show "item": every claimed item, its root, every bound root
    asks: list[dict]            # trk waiting --all "asks"
    panes: list[Pane]
    workspaces: list[Workspace]
    now: float                  # epoch seconds


@dataclass
class Plan:
    bindings: dict
    effects: list = field(default_factory=list)
    seen: dict = field(default_factory=dict)
    forget: list = field(default_factory=list)
    absent: dict = field(default_factory=dict)


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def root_of(item: dict) -> str:
    return item["root"] or item["id"]


def ticket_of(jira_url: str | None) -> str | None:
    return jira_url.rstrip("/").rsplit("/", 1)[-1] if jira_url else None


def _root_orchestrator(root: str, claims: list[dict], by_session: dict, workspace: str | None = None):
    for c in claims:
        p = by_session.get(c["session"])
        if c["item"] == root and c["actor"] == "orchestrator" and p and workspace in (None, p.workspace_id):
            return c["session"]
    return None


def _gone(root: str, b: dict, live_ws: dict, snap: Snapshot, state, effects: list) -> bool:
    """Whether a binding's workspace is gone: missing from the list for MISSING_S. Without a list,
    nothing is judged."""
    if not live_ws or b["workspace"] in live_ws:
        b["missing_since"] = None
        return False
    b["missing_since"] = b.get("missing_since") or snap.now
    if snap.now - b["missing_since"] < MISSING_S:
        return False
    key = f"gone:{root}:{b['opened_at']}"
    if b["origin"] == "hx" and key not in state.done:
        effects.append(Note(key, f"GONE  {root}  its Space {b['workspace']} closed outside hx; "
                                 f"`hx start {root}` reopens it"))
    return True


def decide(snap: Snapshot, state, cfg) -> Plan:
    by_session = {p.session: p for p in snap.panes if p.session}
    by_pane = {p.pane_id: p for p in snap.panes}
    live_ws = {w.workspace_id: w.label for w in snap.workspaces}
    claims = [c for c in snap.claims if c["item"] in snap.items]
    claimants = {c["session"] for c in claims}
    by_root: dict[str, list[dict]] = {}
    for c in claims:
        by_root.setdefault(root_of(snap.items[c["item"]]), []).append(c)
    bindings = {r: dict(b) for r, b in state.bindings.items()}
    effects: list = []
    forget: list = []

    # 1. A Space whose workspace is gone.
    for root, b in list(bindings.items()):
        if _gone(root, b, live_ws, snap, state, effects):
            del bindings[root]

    # 2. A claimed root with no Space adopts the workspace of a claimant in herdr. No pane, no Space.
    for root, cl in by_root.items():
        if root in bindings:
            continue
        orch = _root_orchestrator(root, cl, by_session)
        first = orch or next((c["session"] for c in cl if c["session"] in by_session), None)
        if first is None:
            continue
        bindings[root] = {"workspace": by_session[first].workspace_id, "origin": "adopted",
                          "orchestrator": orch, "role": None, "pane": None, "tab": None,
                          "opened_at": iso(snap.now), "idle_since": None, "missing_since": None}

    # 3 and 4. Keep, drop or close each Space. Without panes herdr is not answering: judge nothing.
    for root, b in (list(bindings.items()) if snap.panes else []):
        if root not in snap.items or b["workspace"] not in live_ws:
            continue
        closed = snap.items[root]["status"] != "open"
        ws_panes = [p for p in snap.panes if p.workspace_id == b["workspace"]]
        if b["origin"] == "adopted":
            mine = [c for c in by_root.get(root, [])
                    if c["session"] in by_session and by_session[c["session"]].workspace_id == b["workspace"]]
            if closed or not mine:
                del bindings[root]
            else:
                b["orchestrator"] = _root_orchestrator(root, mine, by_session, b["workspace"])
            continue
        tab1 = by_pane.get(b["pane"])
        if tab1 and tab1.workspace_id == b["workspace"] and tab1.session:
            b["orchestrator"] = tab1.session
            if b.get("launch") and not b.get("launched"):
                b["launched"] = tab1.session     # the session hx started: only it carries the launch values
        elif not tab1:
            b["orchestrator"] = _root_orchestrator(root, by_root.get(root, []), by_session,
                                                   b["workspace"]) or b["orchestrator"]
        shared = any(o != root and ob["workspace"] == b["workspace"] for o, ob in bindings.items())
        held = any(p.status in BUSY or p.session in claimants for p in ws_panes)
        if not closed or held or shared:
            b["idle_since"] = None
            continue
        b["idle_since"] = b["idle_since"] or snap.now
        key = f"close:{root}:{b['opened_at']}"
        if snap.now - b["idle_since"] < cfg.close_delay_s or key in state.done:
            continue
        del bindings[root]
        if not live_ws[b["workspace"]].startswith(f"{root} "):
            effects.append(Note(f"mislabel:{key}", f"NOTE  {root}  not closing {b['workspace']}: its label "
                                                   f"is {live_ws[b['workspace']]!r}, not this root's"))
            continue
        effects.append(Close(key, root, b["workspace"]))
        forget += [f"ws:{b['workspace']}"] + [f"pane:{p.pane_id}" for p in ws_panes]

    # 5. Sidebar tokens: one per bound workspace, one per claimant pane anywhere in herdr.
    closing = {e.workspace for e in effects if isinstance(e, Close)}
    per_ws: dict[str, list[str]] = {}
    for root, b in sorted(bindings.items()):
        it = snap.items.get(root)
        if it is None:
            continue
        if it["status"] != "open":
            text = f"{root} · closed"
        else:
            workers = sum(1 for c in by_root.get(root, []) if c["actor"] == "worker")
            asks = sum(1 for a in snap.asks if a["root"] == root)
            text = f"{root} · {it['stage'] or it['kind']} · {workers}w" + (f" · ⚑{asks}" if asks else "")
        per_ws.setdefault(b["workspace"], []).append(text)
    want = {f"ws:{ws}": "  ".join(v) for ws, v in per_ws.items() if ws in live_ws and ws not in closing}
    for c in claims:
        p = by_session.get(c["session"])
        if p and p.workspace_id not in closing:
            crit = [x for x in snap.items[c["item"]]["criteria"] if not x["retired"]]
            met = sum(1 for x in crit if x["status"] == "pass")
            want[f"pane:{p.pane_id}"] = f"{c['item']} ac {met}/{len(crit)}" if crit else c["item"]
    for key, value in want.items():
        old = state.reported.get(key)
        if not old or old[0] != value or snap.now - old[1] >= REFRESH_S:
            kind, target = key.split(":", 1)
            effects.append(Report(f"report:{key}", kind, target, value))
    for key in state.reported:
        if key in want or key in forget:
            continue
        kind, target = key.split(":", 1)
        if target in (live_ws if kind == "ws" else by_pane):
            effects.append(Report(f"report:{key}", kind, target, None))
        else:
            forget.append(key)

    # 6. Claims of a session that left herdr: its pane closed, or the pane now runs another
    #    session, or the pane has shown no session for ABSENT_S. Without panes, judge nothing.
    absent: dict[str, float] = {}
    if snap.panes:
        seen = {s: p.pane_id for s, p in by_session.items()}
        for session, pane_id in state.seen.items():
            if session in by_session:
                continue
            pane = by_pane.get(pane_id)
            if pane is not None and pane.session is None:
                since = state.absent.get(session, snap.now)
                if snap.now - since < ABSENT_S:
                    seen[session], absent[session] = pane_id, since
                    continue
            if session in claimants:
                effects.append(Release(f"release:{session}", session))
    else:
        seen, absent = dict(state.seen), dict(state.absent)

    # 7. The session map: one current (item, role) per claimant in herdr, plus each hx orchestrator.
    current: dict[str, tuple] = {}
    for root, b in bindings.items():
        if b["origin"] == "hx" and b["orchestrator"] in by_session:
            launch = (b.get("launch") or {}) if b.get("launched") == b["orchestrator"] else {}
            current[b["orchestrator"]] = (root, root, b["role"], launch.get("model") or cfg.models.get(b["role"]),
                                          -1, launch)
    hx_orch = {b["orchestrator"]: b for b in bindings.values() if b["origin"] == "hx" and b["orchestrator"]}
    for c in claims:
        if c["session"] not in by_session:
            continue
        depth = snap.items[c["item"]].get("depth") or 0
        if c["session"] in hx_orch:
            role = hx_orch[c["session"]]["role"]
            hb = hx_orch[c["session"]]
            launch = (hb.get("launch") or {}) if hb.get("launched") == c["session"] else {}
            model = launch.get("model") or cfg.models.get(role)
        else:
            role, model, launch = ("orchestrator" if c["actor"] == "orchestrator" else "worker"), None, {}
        if c["session"] not in current or depth > current[c["session"]][4]:
            current[c["session"]] = (root_of(snap.items[c["item"]]), c["item"], role, model, depth, launch)
    for session, (root, it, role, model, _, launch) in sorted(current.items()):
        if state.sessions.get(session) != [it, role]:
            row = {"at": iso(snap.now), "session": session, "root": root, "item": it, "role": role,
                   "ticket": ticket_of(snap.items[it].get("jira_url")), "model": model,
                   "pane": by_session[session].pane_id,
                   # routing-1 fields (amendment 6): known only for sessions hx started
                   "parent": launch.get("parent"), "effort": launch.get("effort"),
                   "reason": launch.get("reason")}
            effects.append(MapRow(f"map:{session}:{it}:{role}", session, it, role, row))

    return Plan(bindings, effects, seen, forget, absent)
