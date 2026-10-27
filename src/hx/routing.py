"""Who hears about an ask (spec Section 1, ADR 0002 point 10). Pure: snapshots in, actions out."""
import json
import re
from dataclasses import dataclass
from typing import Callable

from hx.herdr import Pane
from hx.state import State


@dataclass(frozen=True)
class Notify:
    key: str
    person: str
    channel: str
    ask: str
    text: str
    pane: str | None
    card: str = ""     # the queue item's text: the ask in a pane's card shape (card())
    label: str = ""    # what the list names the card by: its ask, not the pinned pane's tab


def header(kind: str, ref: str) -> str:
    """What every line hx types into a pane starts with (upstream ask UP-0003): a session's rules read a line
    that opens `[<kind>] id:` as automation, never as a person's answer, whatever free text follows."""
    return f"[{kind}] id:{'-'.join(ref.split()) or '-'} from:hx"


@dataclass(frozen=True)
class Wake:
    key: str
    pane: str
    text: str
    needs_you: str | None = None   # the answered ask: clear the pane's needs-you item if it names it
    kind: str = "hx"               # the header's word: ask, answered, withdrawn, reply, comment, hold, resume, done
    ref: str = ""                  # the header's id: the ask, hold or comment it is about; the key when empty

    @property
    def line(self) -> str:
        """The text as typed: the header, then the text."""
        return f"{header(self.kind, self.ref or self.key)} {self.text}"


@dataclass(frozen=True)
class Clear:
    key: str
    ask: str


def _pane_of(session: str | None, panes: list[Pane]) -> Pane | None:
    return next((p for p in panes if session and p.session == session), None)


def _orchestrator(root: str, state: State) -> str | None:
    return (state.bindings.get(root) or {}).get("orchestrator")


CARD_LINE = 240       # the question line's length on a card
_MARKS = re.compile(r"[*_`#>]+")


def _flat(text: str) -> str:
    return " ".join(_MARKS.sub("", text or "").split())


def _first_sentence(text: str) -> str:
    flat = _flat(text)
    line = re.split(r"(?<=[.?!])\s", flat, maxsplit=1)[0]
    return line if len(line) <= CARD_LINE else line[:CARD_LINE - 1].rstrip() + "…"


def _options(body: str | None) -> tuple[list[tuple[int, str]], int | None]:
    """trk's `options` section, `{"options": [{n, label}...], "recommended": N|None}`; nothing when absent or bad."""
    try:
        d = json.loads(body or "")
        opts = [(int(o["n"]), _flat(str(o["label"]))) for o in d["options"]]
    except (ValueError, TypeError, KeyError):
        return [], None
    rec = d.get("recommended")
    return opts, rec if isinstance(rec, int) else None


def card(a: dict) -> str:
    """An ask waiting on a person, as a pane writes its own card (upstream ask UP-0009): the one-line question (the
    title when it is a question, else the question's first sentence), one numbered line per option, then the
    shortest answer trk takes. It says how to answer in full, since the pane it is pinned to may not show it."""
    body = {s.get("handle"): s.get("body") for s in a.get("sections") or []}
    title = _flat(a.get("title") or "")
    line = title if title.endswith("?") else (_first_sentence(body.get("question") or "") or title)
    opts, rec = _options(body.get("options"))
    out = [f"**Ask:** {a['id']}: {line}"]
    if opts:
        out += ["**Options:**"] + [f"{n}. {label}{' (recommended)' if n == rec else ''}" for n, label in opts]
        reply = f"`trk answer {a['id']} --option <N>`"
    else:
        reply = f"`trk answer {a['id']} --outcome \"<one line>\"`"
    out.append(f"**Reply with:** {reply} in a terminal (not in a Claude pane, which trk reads as an agent)")
    out.append(f"The question may not be on this pane's screen: `trk show {a['id']}` has it in full.")
    return "\n".join(out)


def _pin(a: dict, info, state: State, panes, agentic: Callable[[str], bool]) -> Pane | None:
    """Where a person's ask shows (UP-0009): the root's live orchestrator, which can show and explain it; else the
    asker's pane unless it is agentic (an agentic worker never shows a person its asks); else the asker's pane
    anyway, since the card carries the whole ask."""
    orch = _pane_of(_orchestrator(a["root"], state), panes)
    asker = _pane_of(info.session if info else None, panes)
    if orch:
        return orch
    if asker and not agentic(asker.pane_id):
        return asker
    return asker


def _human(a: dict, person: str, state: State, panes, humans,
           agentic: Callable[[str], bool] = lambda pane: False) -> list[Notify]:
    info = state.asks.get(a["id"])
    gen = info.gen if info else 0
    text = f"{a['id']} {a['title']}: {info.question if info else ''}".strip()
    pin = _pin(a, info, state, panes, agentic)
    return [Notify(f"deliver:{a['id']}:{gen}:{ch}", person, ch, a["id"], text,
                   pin.pane_id if (ch == "queue" and pin) else None, card(a), a["id"])
            for ch in humans.get(person, ["console"])]


def _closer_pane(row: dict, state: State, panes) -> Pane | None:
    """The first live pane of: the asker, the parent's live claimant, the orchestrator."""
    data = row["data"]
    live_parent = data.get("parent_claim_session") if data.get("parent_claim_live") else None
    for s in (row.get("asker_session"), live_parent, _orchestrator(row["root"], state)):
        if p := _pane_of(s, panes):
            return p
    return None


def still_pending(closed: list[dict], state: State, panes) -> list[dict]:
    """Closed rows whose wake is still owed: not done, not quiet, and someone live to wake."""
    return [r for r in closed
            if r["data"].get("reason") != "parent_closed"
            and f"answered:{state.epoch}:{r['item']}:{r['seq']}" not in state.done
            and _closer_pane(r, state, panes) is not None]


def route(open_asks, closed, claims, ancestors, state: State, panes, humans,
          agentic: Callable[[str], bool] = lambda pane: False) -> list:
    acts: list = []
    wakes: dict[str, list[tuple[str, str]]] = {}   # pane -> [(key, line)]
    for a in open_asks:
        if a["addressee_kind"] == "human":
            acts += _human(a, a["addressee"], state, panes, humans, agentic)
            continue
        gen = state.asks[a["id"]].gen if a["id"] in state.asks else 0
        sessions = [next((c["session"] for c in claims
                          if c["item"] == anc and c["actor"] == a["addressee"]), None)
                    for anc in ancestors.get(a["id"], [])]
        candidates = [s for s in sessions if s] + [_orchestrator(a["root"], state)]
        pane = next((p for s in candidates if (p := _pane_of(s, panes))), None)
        if pane is None:
            acts += _human(a, a["effective_owner"], state, panes, humans, agentic)
        elif pane.wakeable:
            wakes.setdefault(pane.pane_id, []).append(
                (f"wake:{a['id']}:{gen}:{pane.session}", f"{a['id']} \"{a['title']}\""))
    for row in closed:
        ask, data = row["item"], row["data"]
        acts.append(Clear(f"clear:{state.epoch}:{ask}:{row['seq']}", ask))
        if data.get("reason") == "parent_closed":
            continue
        p = _closer_pane(row, state, panes)
        if p and p.wakeable:                    # a busy pane holds the wake (still_pending)
            verb = "answered" if row["change"] == "ask.answered" else "withdrawn"
            why = data.get("outcome") or data.get("reason") or data.get("resolution")
            tail = " Run trk show %s." % ask if verb == "answered" else ""
            acts.append(Wake(f"answered:{state.epoch}:{ask}:{row['seq']}", p.pane_id,
                             f"[hx] {ask} {verb}: {why}.{tail}",
                             # only the asker's own pane: a fallback pane's item is another ask
                             needs_you=ask if verb == "answered" and p.session == row.get("asker_session") else None,
                             kind=verb, ref=ask))
    for pane_id, items in wakes.items():
        todo = [(k, line) for k, line in items if k not in state.done]
        if todo:
            head = f"[hx] {len(todo)} asks for you: " if len(todo) > 1 else "[hx] ask for you: "
            acts.append(Wake("+".join(k for k, _ in todo), pane_id,
                             head + " · ".join(l for _, l in todo) + ". Run trk show <id>.",
                             kind="ask", ref=",".join(k.split(":")[1] for k, _ in todo)))
    # A queue item is re-offered every pass: needs-you drops it when its pane dies, and
    # Channels.notify writes it only when the file is missing.
    return [a for a in acts if a.key not in state.done
            or (isinstance(a, Notify) and a.channel == "queue")]
