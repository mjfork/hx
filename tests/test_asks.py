"""hx needs-you --json and hx answer: the queue as structured cards, and a guarded one-line reply."""
import json
from pathlib import Path

import pytest

from hx import asks, drafts, sidebar
from hx.cli import main
from hx.herdr import Pane, Workspace
from tests.conftest import EMPTY_BOX, FakeHerdr, box

NOW = 1_800_000_000.0

TWO_ASKS = """**Ask 1:** Which route should the zod 4.4 fix take?
**Options:**
1. Make the stored-schema render write the old form (recommended)
2. Bump the format version from 14 to 15,
   with a migration
3. Drop the bump
**Reply with:** `1`, `2` or `3`
**Unblocks:** the zod PR

**Ask 2:** Should the same PR also update the scans?
**Reply with:** `yes` or `no`
**Unblocks:** Security Scans going green"""


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture(autouse=True)
def quiet_0(monkeypatch):
    monkeypatch.setattr(drafts, "QUIET_S", 0)


def item(queue: Path, pane: str, ask: str, ts: int = 1) -> Path:
    queue.mkdir(parents=True, exist_ok=True)
    f = queue / f"{pane.replace(':', '_')}.json"
    f.write_text(json.dumps({"pane": pane, "workspace": pane.split(":")[0], "ask": ask, "cwd": "/w", "ts": ts}))
    return f


def panes(*pairs):
    return [Pane(p, p.split(":")[0], s, "idle", "/w") for p, s in pairs]


# --- the parser ---------------------------------------------------------------------------------

def test_two_asks_with_options_and_wrapped_lines():
    got = asks.parse(TWO_ASKS)
    assert [a["n"] for a in got] == [1, 2]
    one = got[0]
    assert one["ask"] == "Which route should the zod 4.4 fix take?"
    assert [o["n"] for o in one["options"]] == [1, 2, 3]
    assert one["options"][1]["text"] == "Bump the format version from 14 to 15, with a migration"
    assert one["reply_with"] == [{"text": "1", "tap": True}, {"text": "2", "tap": True}, {"text": "3", "tap": True}]
    assert one["unblocks"] == "the zod PR"
    assert got[1]["options"] == [] and [r["text"] for r in got[1]["reply_with"]] == ["yes", "no"]


def test_a_single_unnumbered_ask_is_ask_1():
    got = asks.parse("**Ask:** Grant wiki access?\n**Reply with:** `done`\n**Unblocks:** publishing")
    assert got == [{"n": 1, "label": "", "ask": "Grant wiki access?", "options": [],
                    "reply_with": [{"text": "done", "tap": True}], "free_text": "", "unblocks": "publishing"}]


def test_an_ask_may_carry_a_label():
    got = asks.parse("**Ask 1 (tr-ht6qk):** Per page?\n**Reply with:** `1`\n\n**Ask (urgent):** Push?\n**Reply with:** `yes`")
    assert [(a["n"], a["label"]) for a in got] == [(1, "tr-ht6qk"), (2, "urgent")]


def test_a_reply_with_no_literal_is_a_free_text_ask():
    got = asks.parse("**Ask:** Which cap did you approve?\n**Options:**\n1. 0.01 kg/s\n2. 2e-3 x mean flow\n"
                     "**Reply with:** the cap, in your words\n**Unblocks:** tr-xrn2m")
    assert got[0]["reply_with"] == [] and got[0]["free_text"] == "the cap, in your words"
    assert [o["n"] for o in got[0]["options"]] == [1, 2]


@pytest.mark.parametrize("reply", ["! ~/x/rollout.sh", "  !rm -rf tmp", 'trk answer tr-ab12c --outcome "<one line>"'])
def test_shell_and_placeholder_replies_are_never_tappable(reply):
    got = asks.parse(f"**Ask:** Run it?\n**Reply with:** `{reply}`, or `2`")
    assert got[0]["reply_with"] == [{"text": reply, "tap": False}, {"text": "2", "tap": True}]


@pytest.mark.parametrize("text", [
    "",
    "Some words before any ask\n**Ask:** x?\n**Reply with:** `1`",       # stray text first
    "**Ask:** x?\n**Unblocks:** y",                                          # no Reply with
    "**Ask:** x?\n**Reply with:**",                                          # an empty Reply with
    "**Ask:** x?\n**Options (2):**\n1. a\n**Reply with:** `1`",              # a label on another field
    "**Ask:** x?\n**Reply with 2:** `1`",                                     # a number on another field
    "**Ask:** x?\n**Options:**\n1. a\n3. b\n**Reply with:** `1`",            # options skip a number
    "**Ask:** x?\n**Colour:** red\n**Reply with:** `1`",                     # an unknown field
    "**Ask 1:** x?\n**Reply with:** `1`\n**Ask 1:** y?\n**Reply with:** `2`",  # a repeated number
])
def test_anything_off_the_format_is_none(text):
    assert asks.parse(text) is None


# --- the cards ------------------------------------------------------------------------------------

def test_cards_parse_each_live_item_and_skip_the_rest(tmp_path, monkeypatch):
    q, cache = tmp_path / "q", tmp_path / "cache"
    monkeypatch.setenv("CACHE_STATE_DIR", str(cache))
    item(q, "w1:p1", TWO_ASKS, ts=7)
    item(q, "w9:p1", TWO_ASKS)                                         # its pane is closed
    (q / "hx_tr-ab12c.json").write_text(json.dumps({"pane": "w1:p2", "ask": "tr-ab12c"}))   # the trk card
    (q / ".list-cache").write_text("[]")
    h = FakeHerdr(panes(("w1:p1", "s-a"), ("w1:p2", "s-b")), workspaces=[Workspace("w1", "zod fix")])
    cards = asks.cards(h, q, cache, [], NOW)
    assert len(cards) == 1
    c = cards[0]
    assert (c["pane"], c["workspace"], c["space"], c["session"], c["ts"]) == ("w1:p1", "w1", "zod fix", "s-a", 7)
    assert c["item"] == "w1_p1.json" and c["sha"] == asks.sha(TWO_ASKS)
    assert c["raw"] == TWO_ASKS and len(c["asks"]) == 2 and c["trk_ask"] is None
    assert c["cache"] == {"left_s": None, "text": "?"}


def test_a_card_names_the_open_trk_ask_it_relays(tmp_path):
    q = tmp_path / "q"
    item(q, "w1:p1", "**Ask:** tr-ab12c Approve the merge?\n**Reply with:** `yes`")
    h = FakeHerdr(panes(("w1:p1", "s-a")))
    cards = asks.cards(h, q, tmp_path / "cache", [{"id": "tr-ab12c"}], NOW)
    assert cards[0]["trk_ask"] == "tr-ab12c"
    assert asks.cards(h, q, tmp_path / "cache", [], NOW)[0]["trk_ask"] is None    # closed in trk


def test_a_pane_held_on_a_dialog_gets_a_blocked_card_first(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    q = tmp_path / "q"
    item(q, "w1:p1", TWO_ASKS)
    item(q, "w1:p3", TWO_ASKS)
    ps = panes(("w1:p1", "s-a")) + [Pane("w1:p2", "w1", "s-b", "blocked", "/w"),
                                    Pane("w1:p3", "w1", "s-c", "blocked", "/w")]   # has a card already
    cards = asks.cards(FakeHerdr(ps, workspaces=[Workspace("w1", "zod fix")]), q, tmp_path / "cache", [], NOW)
    assert [(c["pane"], c["kind"]) for c in cards] == [("w1:p2", "blocked"), ("w1:p1", "ask"), ("w1:p3", "ask")]
    c = cards[0]
    assert (c["space"], c["session"], c["sha"], c["asks"], c["item"]) == ("zod fix", "s-b", None, None, None)
    assert c["raw"] == asks.DIALOG and c["cache"] == {"left_s": None, "text": "?"}


def test_hx_answer_refuses_a_dialog(tmp_path, monkeypatch):
    q = tmp_path / "q"
    q.mkdir()
    h = FakeHerdr([Pane("w1:p2", "w1", "s-b", "blocked", "/w")])
    assert asks.answer(h, q, "w1:p2", "", "s-b", "1", 0) == "refused: the ask is gone (answered, resolved or dismissed)"
    assert h.calls == []


def test_an_unparsed_item_keeps_its_raw_text(tmp_path):
    q = tmp_path / "q"
    item(q, "w1:p1", "please look at the PR")
    c = asks.cards(FakeHerdr(panes(("w1:p1", "s-a"))), q, tmp_path / "cache", [], NOW)[0]
    assert c["asks"] is None and c["raw"] == "please look at the PR"


def test_hx_needs_you_json_prints_the_cards(tmp_path, monkeypatch, capsys):
    import hx.cli

    class Waiting:
        def __init__(self, **kw):
            pass

        def waiting(self):
            return {"asks": []}
    q = tmp_path / "q"
    item(q, "w1:p1", TWO_ASKS)
    monkeypatch.setattr(hx.cli, "QUEUE_DIR", q)
    monkeypatch.setattr(hx.cli, "Herdr", lambda: FakeHerdr(panes(("w1:p1", "s-a"))))
    monkeypatch.setattr(hx.cli, "Trk", Waiting)
    assert main(["needs-you", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == 1 and [c["pane"] for c in out["cards"]] == ["w1:p1"]


def test_needs_you_takes_line_or_json_not_both(capsys):
    with pytest.raises(SystemExit):
        main(["needs-you"])
    with pytest.raises(SystemExit):
        main(["needs-you", "--line", "--json"])


# --- the answer ---------------------------------------------------------------------------------

def go(h, q, pane="w1:p1", digest=None, session="s-a", text="2", wait=45):
    c = Clock()
    return asks.answer(h, q, pane, digest or asks.sha(TWO_ASKS), session, text, wait, clock=c, sleep=c.sleep)


def test_a_current_ask_is_answered(tmp_path):
    q = tmp_path / "q"
    item(q, "w1:p1", TWO_ASKS)
    h = FakeHerdr(panes(("w1:p1", "s-a")))
    assert go(h, q) == "sent" and h.wakes == [("w1:p1", "2")]


def test_a_shell_reply_is_refused_before_anything_is_read(tmp_path):
    q = tmp_path / "q"
    item(q, "w1:p1", TWO_ASKS)
    h = FakeHerdr(panes(("w1:p1", "s-a")))
    assert go(h, q, text="  ! rm -rf ~").startswith("refused: a reply that starts with ! runs a shell")
    assert h.wakes == []


def test_an_empty_reply_is_refused(tmp_path):
    q = tmp_path / "q"
    item(q, "w1:p1", TWO_ASKS)
    assert go(FakeHerdr(panes(("w1:p1", "s-a"))), q, text="  ").startswith("refused: the reply is empty")


def test_a_gone_ask_is_refused(tmp_path):
    h = FakeHerdr(panes(("w1:p1", "s-a")))
    assert go(h, tmp_path / "q") == "refused: the ask is gone (answered, resolved or dismissed)"
    assert h.wakes == []


def test_a_changed_ask_is_refused(tmp_path):
    q = tmp_path / "q"
    item(q, "w1:p1", TWO_ASKS + "\nmore")
    h = FakeHerdr(panes(("w1:p1", "s-a")))
    assert go(h, q) == "refused: the ask changed since the card was drawn: reload"
    assert h.wakes == []


def test_a_pane_now_running_another_session_is_refused(tmp_path):
    q = tmp_path / "q"
    item(q, "w1:p1", TWO_ASKS)
    h = FakeHerdr(panes(("w1:p1", "s-new")))
    assert go(h, q) == "refused: the pane runs another session now"
    assert h.wakes == []


def test_an_ask_that_changes_while_the_box_is_busy_is_never_typed(tmp_path, monkeypatch):
    """send waits for a quiet box; the ask is checked again right before typing."""
    monkeypatch.setattr(drafts, "QUIET_S", 4)
    q = tmp_path / "q"
    f = item(q, "w1:p1", TWO_ASKS)

    class Racing(FakeHerdr):
        n = 0

        def read(self, pane_id):
            self.n += 1
            if self.n == 2:
                f.write_text(json.dumps({"pane": "w1:p1", "ask": "**Ask:** new?\n**Reply with:** `ok`"}))
            return EMPTY_BOX
    h = Racing(panes(("w1:p1", "s-a")))
    assert go(h, q) == "refused: the ask changed since the card was drawn: reload"
    assert h.wakes == []


def test_a_draft_is_never_typed_over(tmp_path):
    q = tmp_path / "q"
    item(q, "w1:p1", TWO_ASKS)
    h = FakeHerdr(panes(("w1:p1", "s-a")), screens={"w1:p1": box("half a thought")})
    assert go(h, q, wait=5) == "refused: there is text in the input box"
    assert h.wakes == []


def test_hx_answer_prints_the_result_and_exits_by_it(tmp_path, monkeypatch, capsys):
    import hx.cli
    q = tmp_path / "q"
    item(q, "w1:p1", TWO_ASKS)
    h = FakeHerdr(panes(("w1:p1", "s-a")))
    monkeypatch.setattr(hx.cli, "QUEUE_DIR", q)
    monkeypatch.setattr(hx.cli, "Herdr", lambda: h)
    assert main(["answer", "w1:p1", "--sha", "nope", "--session", "s-a", "2"]) == 1
    assert "refused  w1:p1  the ask changed" in capsys.readouterr().out
    assert main(["answer", "w1:p1", "--sha", asks.sha(TWO_ASKS), "--session", "s-a", "2"]) == 0
    assert capsys.readouterr().out.strip() == "sent  w1:p1"


# --- review fixes ---------------------------------------------------------------------------------

def test_a_second_answer_to_the_same_ask_is_refused(tmp_path, monkeypatch):
    """A double tap or a retry: the item lingers until herdr-jump's hook runs, so hx remembers."""
    monkeypatch.setenv("HX_STATE_DIR", str(tmp_path / "state"))
    q = tmp_path / "q"
    item(q, "w1:p1", TWO_ASKS)
    h = FakeHerdr(panes(("w1:p1", "s-a")))
    assert go(h, q) == "sent"
    assert go(h, q, text="3") == "refused: this ask was answered already"
    assert h.wakes == [("w1:p1", "2")]


@pytest.mark.parametrize("text", ["x\x7f! rm -rf ~", "\x15! rm", "ok\x1b[D", "a​!b", "/clear", "  /exit"])
def test_control_characters_and_slash_commands_are_refused(tmp_path, text):
    q = tmp_path / "q"
    item(q, "w1:p1", TWO_ASKS)
    h = FakeHerdr(panes(("w1:p1", "s-a")))
    assert go(h, q, text=text).startswith("refused: ")
    assert h.wakes == []


def test_a_multi_line_reply_is_typed_as_one_safe_line(tmp_path):
    q = tmp_path / "q"
    item(q, "w1:p1", TWO_ASKS)
    h = FakeHerdr(panes(("w1:p1", "s-a")))
    assert go(h, q, text="ok\n! rm") == "sent" and h.wakes == [("w1:p1", "ok ! rm")]


def test_a_slash_reply_is_never_tappable():
    got = asks.parse("**Ask:** Reset?\n**Reply with:** `/clear` or `no`")
    assert got[0]["reply_with"] == [{"text": "/clear", "tap": False}, {"text": "no", "tap": True}]


def test_prose_around_the_replies_makes_none_of_them_tappable():
    got = asks.parse("**Ask:** Apply?\n**Reply with:** `go` to apply it to `config.toml`, or `no`")
    assert [r["tap"] for r in got[0]["reply_with"]] == [False, False, False]
    plain = asks.parse("**Ask:** Pick?\n**Reply with:** `1`, `2`, or `3`")
    assert [r["tap"] for r in plain[0]["reply_with"]] == [True, True, True]


def test_a_reply_two_asks_share_is_tappable_in_neither():
    text = ("**Ask 1:** a?\n**Reply with:** `1` or `2`\n\n**Ask 2:** b?\n**Reply with:** `1` or `2`\n\n"
            "**Ask 3:** c?\n**Reply with:** `yes` or `no`")
    got = asks.parse(text)
    assert [[r["tap"] for r in a["reply_with"]] for a in got] == [[False, False], [False, False], [True, True]]


@pytest.mark.parametrize("text", [
    "**Ask:** x?\n**Options:**\n1. a\n   ```\n2. rm build\n   ```\n**Reply with:** `1`",   # a code fence
    "**Ask:** x?\n**Reply with:** `1`\n**Unblocks:** y\n\n**Possible next steps**\n1. do y",  # another section
    "**Ask:** x?\n**Reply with:** `1`\n## Another heading",
])
def test_text_past_the_format_is_none(text):
    assert asks.parse(text) is None


def test_one_bad_item_or_ask_never_breaks_the_cards(tmp_path):
    q = tmp_path / "q"
    q.mkdir()
    (q / "w1_p1.json").write_text('{"pane": "w1:p1", "ask": "**Ask:** x?\\n**Reply with:** `1`", "ts": NaN}')
    (q / "w1_p2.json").write_text(json.dumps({"pane": "w1:p2", "ask": 5, "ts": "soon"}))
    h = FakeHerdr(panes(("w1:p1", "s-a"), ("w1:p2", "s-b")))
    cards = asks.cards(h, q, tmp_path / "cache", [{"title": "no id"}], NOW)
    assert [c["ts"] for c in cards] == [None, None]
    json.dumps(cards, allow_nan=False)


def test_needs_you_json_reports_any_herdr_failure(tmp_path, monkeypatch, capsys):
    import hx.cli

    class Bad(FakeHerdr):
        def panes(self):
            raise ValueError("bad json from herdr")

    class Waiting:
        def __init__(self, **kw):
            pass

        def waiting(self):
            return {"asks": []}
    monkeypatch.setattr(hx.cli, "QUEUE_DIR", tmp_path / "q")
    monkeypatch.setattr(hx.cli, "Herdr", lambda: Bad([]))
    monkeypatch.setattr(hx.cli, "Trk", Waiting)
    assert main(["needs-you", "--json"]) == 1
    assert "bad json from herdr" in capsys.readouterr().err


def test_a_pane_without_a_session_can_be_answered_with_an_empty_session(tmp_path):
    q = tmp_path / "q"
    item(q, "w1:p1", TWO_ASKS)
    h = FakeHerdr([Pane("w1:p1", "w1", None, "idle", "/w")])
    assert go(h, q, session="") == "sent"


def test_the_same_text_asked_again_later_is_a_new_ask(tmp_path, monkeypatch):
    """The answered-once marker keys on the item's time too: a word-for-word repeat can be answered."""
    monkeypatch.setenv("HX_STATE_DIR", str(tmp_path / "state"))
    q = tmp_path / "q"
    item(q, "w1:p1", TWO_ASKS, ts=1)
    h = FakeHerdr(panes(("w1:p1", "s-a")))
    assert go(h, q) == "sent"
    item(q, "w1:p1", TWO_ASKS, ts=2)                  # the session asked the same thing again
    assert go(h, q, text="3") == "sent"
    assert go(h, q, text="1") == "refused: this ask was answered already"
    assert h.wakes == [("w1:p1", "2"), ("w1:p1", "3")]
