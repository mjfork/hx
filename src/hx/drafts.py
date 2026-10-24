"""Hold a wake while a person has text in the pane's input box, or had it in the last QUIET_S seconds.

`herdr pane run` types after whatever is in the box, so a wake on a half-typed message would join
it and send it. draft() says how the box is found. Dim text there is Claude's placeholder
suggestion, not a draft. A screen with no box (a dialog, a menu, a pane too short to show it, a
failed read) holds the wake too, and after UNREADABLE_S one line in the inbox says so.
"""
import re
import unicodedata

QUIET_S = 30
QUICK_S = 2.0        # the watcher's pass interval while a pane counts down QUIET_S (cli._tick)
STALE_S = 15         # a longer gap between sightings restarts the quiet clock: hx did not see the box
UNREADABLE_S = 600
MIN_RULE = 20
MAX_FOOTER = 10      # the box sits at the bottom: at most this many status lines below it
SELECTED = re.compile(r"^\s*❯\s*\d+\.\s")       # Claude's chosen menu option
NAV = re.compile(r"Esc to cancel|Enter to select")
# Only Claude's own menu forms are known: a chosen option (`❯ 1.`), or a navigation cue. A dialog
# drawn some other way under a lookalike box is not detected.
SGR = re.compile(r"\x1b\[([0-9;:]*)m")
OTHER_ESC = re.compile(r"\x1b(\[[0-9;?]*[A-Za-ln-z]|\][^\x07]*\x07)")


def _split(line: str) -> tuple[str, str]:
    """(all the text, the text that is not dim)."""
    plain, typed, dim, at = [], [], False, 0
    line = OTHER_ESC.sub("", line)
    for m in SGR.finditer(line + "\x1b[m"):
        chunk = line[at:m.start()]
        plain.append(chunk)
        if not dim:
            typed.append(chunk)
        at = m.end()
        # a colon group (4:2, 38:2::r:g:b) is one parameter with its own arguments, never dim
        params = [int(p) if p.isdigit() else 0 if p == "" else -1 for p in m.group(1).split(";")]
        i = 0
        while i < len(params):
            p = params[i]
            if p in (38, 48, 58):              # extended colour: skip its arguments
                i += 2 if params[i + 1:i + 2] == [5] else 4 if params[i + 1:i + 2] == [2] else 0
            elif p == 0:
                dim = False
            elif p == 2:
                dim = True
            elif p == 22:
                dim = False
            i += 1
    return "".join(plain), "".join(typed)


def _cells(s: str) -> int:
    """Terminal columns: wide characters and emoji forms take two; combining and enclosing marks,
    selectors, emoji skin tones, emoji tags and whatever a zero-width joiner joins take none."""
    n, joined = 0, False
    for k, c in enumerate(s):
        o = ord(c)
        if joined or unicodedata.category(c) in ("Mn", "Me") or o == 0x200D or 0xFE00 <= o <= 0xFE0F \
                or 0x1F3FB <= o <= 0x1F3FF or 0xE0020 <= o <= 0xE007F:
            joined = o == 0x200D
            continue
        emoji = s[k + 1:k + 2] == "\uFE0F"           # U+FE0F asks for the two-cell emoji form
        n += 2 if emoji or unicodedata.east_asian_width(c) in "WF" else 1
    return n


def _rule(plain: str) -> bool:
    """A box edge: a line of ─ at least MIN_RULE cells wide, which may carry the session name
    (`──── director ─`). A short rule typed in a draft is not one."""
    s = plain.strip()
    return _cells(s) >= MIN_RULE and s.startswith("─" * 3) and s.endswith("─")


def draft(screen: str) -> str | None:
    """The text in the input box ("" when empty), or None when the screen shows no box.

    Found from the bottom: the box's edges are the widest rules on screen, at least as wide as
    every other line. The bottom edge is the last of them, and the top edge the nearest one above
    it with a `❯` line under it. A rule typed in a draft is narrower, so it stays inside the box. When a short pane cuts the box off, the last
    full rule has the `❯` line under it and the box runs to the bottom of the screen.
    """
    lines = [_split(l) for l in screen.replace("\r\n", "\n").replace("\r", "").split("\n")]
    plain = [l for l, _ in lines]
    is_rule = [_rule(l) for l in plain]
    widest = max((_cells(l.rstrip()) for l, r in zip(plain, is_rule) if not r), default=0)
    edge = max((_cells(plain[i].strip()) for i, r in enumerate(is_rule) if r), default=0)
    if edge == 0 or edge < widest:
        return None
    full = [i for i, r in enumerate(is_rule) if r and _cells(plain[i].strip()) == edge]
    prompt = lambda i: i + 1 < len(plain) and plain[i + 1].lstrip().startswith("❯")  # noqa: E731
    end = full[-1]
    top = next((i for i in reversed(full[:-1]) if prompt(i)), None)    # a whole box first
    if top is None:
        if not prompt(end):
            return None
        top, end = end, len(lines)                                     # cut off at the bottom
    footer = [l for l in plain[end + 1:] if l.strip()]
    if len(footer) > MAX_FOOTER or any(NAV.search(l) or SELECTED.search(l)
                                       for l in plain[top + 1:end] + footer):
        return None
    first = lines[top + 1][1].lstrip().removeprefix("❯")
    words = " ".join([first, *(t for _, t in lines[top + 2:end])]).replace("\u00a0", " ").split()
    return " ".join(words)


def clear(herdr, pane: str, state, now: float, session: str | None) -> tuple[bool, str | None]:
    """(may hx type into `pane` now, an inbox line to log). Updates state.drafts for the pane;
    a new session in the pane starts its record over."""
    rec = state.drafts.get(pane)
    if rec is None or rec.get("session") != session:
        rec = state.drafts[pane] = {"session": session, "empty_since": None,
                                    "unreadable_since": None, "logged": False}
    try:
        text = draft(herdr.read(pane))
    except Exception:                          # any failed read holds this wake, not the pass
        text = None
    seen, rec["seen_at"] = rec.get("seen_at"), now
    if text is None:
        rec["empty_since"] = None
        rec["unreadable_since"] = rec["unreadable_since"] or now
        if now - rec["unreadable_since"] >= UNREADABLE_S and not rec["logged"]:
            rec["logged"] = True
            return False, (f"HELD  {pane}  hx cannot see the input box of this pane for "
                           f"{UNREADABLE_S // 60} minutes, so its wakes wait. A dialog or menu "
                           "is open, or the pane is too short to show the box.")
        return False, None
    rec["unreadable_since"], rec["logged"] = None, False
    if text:
        rec["empty_since"] = None
        return False, None
    if rec["empty_since"] is None or seen is None or now - seen > STALE_S:
        rec["empty_since"] = now
    return now - rec["empty_since"] >= QUIET_S, None


def keep_held(state, held: set[str]) -> None:
    """Only a pane hx still holds a wake for keeps its record: a sent wake, or one no longer
    owed, leaves the next wake to watch the box afresh."""
    state.drafts = {k: v for k, v in state.drafts.items() if k in held}


def counting(state) -> bool:
    """Some pane shows an empty box and waits out QUIET_S: the watcher must pass every QUICK_S."""
    return any(r.get("empty_since") is not None for r in state.drafts.values())
