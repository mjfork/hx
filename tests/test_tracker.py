import json
from pathlib import Path

import pytest

from hx.tracker import Trk, TrkError


def test_changes_validates_against_contract(trk_home):
    reply = Trk(home=str(trk_home)).changes(0)
    assert reply["schema"] == 1
    assert reply["epoch"]
    assert [r["change"] for r in reply["changes"]][:1] == ["actor.added"]


def test_waiting_lists_an_open_ask(trk_home, sh, tree):
    sh("ask", tree["ticket"], "Colour?", "--to", "product", "--question", "-",
       "--as", "worker", "--session", "s-worker", stdin="Which colour?")
    asks = Trk(home=str(trk_home)).waiting()["asks"]
    assert [(a["addressee"], a["asker"]) for a in asks] == [("michael", "worker")]


def test_failed_command_raises(trk_home):
    with pytest.raises(TrkError, match="show"):
        Trk(home=str(trk_home)).show("tr-nope0")


def test_reply_that_breaks_the_contract_raises(trk_home, tmp_path):
    fake = tmp_path / "fake-trk"
    fake.write_text('#!/bin/sh\necho \'{"schema": 2, "claims": []}\'\n')
    fake.chmod(0o755)
    with pytest.raises(TrkError, match="contract"):
        Trk(home=str(trk_home), bin=str(fake)).claims()


def _fake_waiting(trk_home, sh, tree, tmp_path, break_it):
    """A trk whose `waiting` reply is the real one for two asks, the first edited by `break_it`."""
    for q in ("Colour?", "Size?"):
        sh("ask", tree["ticket"], q, "--to", "product", "--question", "-",
           "--as", "worker", "--session", "s-worker", stdin=q)
    reply = Trk(home=str(trk_home)).waiting()
    reply.pop("invalid", None)
    break_it(reply)
    (tmp_path / "reply.json").write_text(json.dumps(reply))
    fake = tmp_path / "fake-trk"
    fake.write_text(f'#!/bin/sh\ncat {tmp_path / "reply.json"}\n')
    fake.chmod(0o755)
    return Trk(home=str(trk_home), bin=str(fake)), [a["id"] for a in reply["asks"]]


def test_an_ask_that_breaks_the_contract_is_left_out_and_named(trk_home, sh, tree, tmp_path):
    def short_sha(reply):
        reply["asks"][0]["red_read"] = {"sha": "3b1da7854"}   # the shape UP-0019 saw
    trk, ids = _fake_waiting(trk_home, sh, tree, tmp_path, short_sha)
    reply = trk.waiting()
    assert [a["id"] for a in reply["asks"]] == ids[1:]
    assert [b["id"] for b in reply["invalid"]] == ids[:1] and reply["invalid"][0]["error"]


def test_a_break_outside_the_asks_still_raises(trk_home, sh, tree, tmp_path):
    def bad_schema(reply):
        reply["schema"] = 2
    trk, _ = _fake_waiting(trk_home, sh, tree, tmp_path, bad_schema)
    with pytest.raises(TrkError, match="contract"):
        trk.waiting()


def test_default_contract_ships_with_hx(monkeypatch):
    import hx
    from hx.tracker import Trk
    monkeypatch.delenv("HX_TRK_CONTRACT", raising=False)
    shipped = Path(hx.__file__).parent / "contract"
    assert Trk().contract_dir == str(shipped)
    assert {"changes", "show", "waiting", "claims"} <= {p.name.removesuffix(".schema.json")
                                                        for p in shipped.glob("*.schema.json")}


def test_shipped_contract_matches_the_live_trk_checkout():
    """When trk's checkout is on this machine, the shipped copy must be current: refresh it with
    scripts/sync-contract.sh."""
    import hx
    live = Path.home() / "projects/beads-alternative/contract"
    if not live.is_dir():
        pytest.skip("no trk checkout here")
    shipped = Path(hx.__file__).parent / "contract"
    assert {p.name: p.read_text() for p in live.glob("*.schema.json")} == \
           {p.name: p.read_text() for p in shipped.glob("*.schema.json")}


def test_a_missing_contract_is_a_contract_break(trk_home, tmp_path):
    """A [trk] contract path that moved away must raise the error the watcher alerts on."""
    t = Trk(contract_dir=str(tmp_path / "gone"))
    with pytest.raises(TrkError, match="breaks contract"):
        t.waiting()
