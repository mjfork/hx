from hx.herdr import Pane
import json

from hx.routing import Clear, Notify, Wake, card, route
from hx.state import AskInfo, State

IDLE = Pane("w1:p2", "w1", "s-worker", "done", "/")
BUSY = Pane("w1:p2", "w1", "s-worker", "working", "/")


def ask(id="tr-a", addressee="michael", kind="human", root="tr-p", owner="michael"):
    return {"id": id, "title": "Colour?", "addressee": addressee, "addressee_kind": kind,
            "root": root, "effective_owner": owner}


def st(**asks):
    return State(epoch="e", seq=9, asks={k: AskInfo("s-worker", 5, "Colour?", "Which?")
                                         for k in asks or {"tr-a": 1}})


def test_human_ask_goes_to_every_channel_at_once():
    acts = route([ask()], [], [], {"tr-a": ["tr-t", "tr-p"]}, st(),
                 [IDLE], {"michael": ["console", "queue"]})
    assert [(a.channel, a.pane) for a in acts] == [("console", None), ("queue", "w1:p2")]
    assert acts[0].key == "deliver:tr-a:5:console"


def test_done_keys_are_not_repeated():
    s = st(); s.done["deliver:tr-a:5:console"] = "t"
    acts = route([ask()], [], [], {"tr-a": []}, s, [IDLE], {"michael": ["console"]})
    assert acts == []


def test_queue_waits_for_a_live_pane():
    acts = route([ask()], [], [], {"tr-a": []}, st(), [], {"michael": ["queue"]})
    assert [(a.channel, a.pane) for a in acts] == [("queue", None)]


def test_agent_ask_wakes_the_claimant_on_the_nearest_ancestor():
    claims = [{"item": "tr-t", "actor": "worker", "session": "s-worker"}]
    acts = route([ask(addressee="worker", kind="agent")], [], claims,
                 {"tr-a": ["tr-t", "tr-p"]}, st(), [IDLE], {})
    assert len(acts) == 1 and isinstance(acts[0], Wake) and acts[0].pane == "w1:p2"
    assert "tr-a" in acts[0].text


def test_two_asks_for_one_pane_are_one_message():
    claims = [{"item": "tr-t", "actor": "worker", "session": "s-worker"}]
    acts = route([ask("tr-a", "worker", "agent"), ask("tr-b", "worker", "agent")], [], claims,
                 {"tr-a": ["tr-t"], "tr-b": ["tr-t"]}, st(**{"tr-a": 1, "tr-b": 1}), [IDLE], {})
    assert len(acts) == 1 and "2 asks" in acts[0].text


def test_wake_held_while_working():
    claims = [{"item": "tr-t", "actor": "worker", "session": "s-worker"}]
    assert route([ask(addressee="worker", kind="agent")], [], claims,
                 {"tr-a": ["tr-t"]}, st(), [BUSY], {}) == []


def test_agent_ask_with_no_agent_falls_to_owner_channels():
    acts = route([ask(addressee="worker", kind="agent")], [], [], {"tr-a": ["tr-t"]},
                 st(), [], {"michael": ["console"]})
    assert [(type(a), a.person) for a in acts] == [(Notify, "michael")]


def test_answer_wakes_the_asker_and_clears():
    row = {"seq": 12, "change": "ask.answered", "item": "tr-a", "root": "tr-p",
           "asker_session": "s-worker", "data": {"outcome": "Blue", "parent_claim_session": None,
                                                   "parent_claim_live": False}}
    acts = route([], [row], [], {}, st(), [IDLE], {})
    assert [type(a) for a in acts] == [Clear, Wake]
    assert acts[1].text.startswith("[hx] tr-a answered: Blue")


def test_parent_closed_clears_quietly():
    row = {"seq": 12, "change": "ask.withdrawn", "item": "tr-a", "root": "tr-p",
           "asker_session": "s-worker", "data": {"reason": "parent_closed",
                                                   "parent_claim_session": None,
                                                   "parent_claim_live": False}}
    assert [type(a) for a in route([], [row], [], {}, st(), [IDLE], {})] == [Clear]


def answered_row():
    return {"seq": 12, "change": "ask.answered", "item": "tr-a", "root": "tr-p",
            "asker_session": "s-worker", "data": {"outcome": "Blue", "parent_claim_session": None,
                                                    "parent_claim_live": False}}


def test_answer_held_while_asker_works():
    from hx.routing import still_pending
    s = st()
    acts = route([], [answered_row()], [], {}, s, [BUSY], {})
    assert [type(a) for a in acts] == [Clear]
    for a in acts:
        s.done[a.key] = "t"
    assert still_pending([answered_row()], s, [BUSY]) == [answered_row()]
    s.done["answered:e:tr-a:12"] = "t"
    assert still_pending([answered_row()], s, [BUSY]) == []


def test_answer_with_no_live_session_is_dropped():
    from hx.routing import still_pending
    assert still_pending([answered_row()], st(), []) == []


# Upstream ask UP-0009: a person's ask reads like a pane's card, and shows where it can be explained.
LONG = ("We need a name for the colour.\n\n- **Why:** the *page* has none.\n- More context that runs on. " * 3)


def asked(title="Name the colour", sections=None, root="tr-p"):
    return {**ask(root=root), "title": title, "sections": sections if sections is not None else [
        {"handle": "question", "body": LONG},
        {"handle": "options", "body": json.dumps({"options": [{"n": 1, "label": "Red"}, {"n": 2, "label": "**Blue**"},
                                                               {"n": 3, "label": "Green"}], "recommended": 2})}]}


def test_a_card_has_the_question_line_each_option_and_the_reply():
    assert card(asked()).splitlines() == [
        "**Ask:** tr-a: We need a name for the colour.", "**Options:**", "1. Red", "2. Blue (recommended)", "3. Green",
        "**Reply with:** `trk answer tr-a --option <N>` in a terminal (not in a Claude pane, which trk reads as an agent)",
        "The question may not be on this pane's screen: `trk show tr-a` has it in full."]


def test_a_card_takes_a_question_title_and_without_options_asks_for_an_outcome():
    lines = card(asked("Which colour?", [{"handle": "question", "body": LONG}])).splitlines()
    assert lines[0] == "**Ask:** tr-a: Which colour?" and "**Options:**" not in lines
    assert lines[1].startswith('**Reply with:** `trk answer tr-a --outcome "<one line>"`')


def test_a_long_first_sentence_is_cut():
    line = card(asked(sections=[{"handle": "question", "body": "word " * 200}])).splitlines()[0]
    assert line.endswith("…") and len(line) <= len("**Ask:** tr-a: ") + 240


ORCH = Pane("w2:p1", "w2", "s-orch", "done", "/")


def pinned(state, panes, agentic=lambda pane: False):
    acts = route([asked()], [], [], {}, state, panes, {"michael": ["queue"]}, agentic)
    return [(a.pane, a.label) for a in acts if isinstance(a, Notify)]


def test_an_agentic_workers_person_ask_pins_to_the_live_orchestrator():
    s = st()
    s.bindings = {"tr-p": {"orchestrator": "s-orch"}}
    assert pinned(s, [IDLE, ORCH], agentic=lambda pane: pane == "w1:p2") == [("w2:p1", "tr-a")]


def test_without_an_orchestrator_the_asker_shows_it_agentic_or_not():
    assert pinned(st(), [IDLE]) == [("w1:p2", "tr-a")]
    assert pinned(st(), [IDLE], agentic=lambda pane: True) == [("w1:p2", "tr-a")]
