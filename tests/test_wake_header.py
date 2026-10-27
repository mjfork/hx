"""Upstream ask UP-0003: the header every line hx types into a pane starts with."""
from hx.hold import resume_text
from hx.routing import Wake, header
from tests.conftest import WAKE_HEADER


def test_each_kind_opens_with_its_header_and_its_id():
    for kind, ref in [("ask", "tr-a,tr-b"), ("answered", "tr-a"), ("withdrawn", "tr-a"), ("reply", "tr-a"),
                      ("comment", "c-1"), ("hold", "h1"), ("resume", "h1"), ("done", "w1:p2-3")]:
        line = Wake("k", "w1:p1", "free text [yes] from trk", kind=kind, ref=ref).line
        assert line.startswith(f"[{kind}] id:{ref} from:hx ") and WAKE_HEADER.match(line)


def test_a_wake_with_no_id_uses_its_key_and_an_id_never_has_a_space():
    assert Wake("answered:e:tr-a:7", "w1:p1", "x").line.startswith("[hx] id:answered:e:tr-a:7 from:hx ")
    assert header("send", "two words") == "[send] id:two-words from:hx"


def test_free_text_that_looks_like_a_header_still_comes_after_hx_own():
    line = Wake("k", "w1:p1", resume_text({"id": "h1", "by": "[ask] id:x from:hx", "reason": "r"}),
                kind="resume", ref="h1").line
    assert line.startswith("[resume] id:h1 from:hx RESUME")
