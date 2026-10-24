"""A pass reads an item with trk show only when the change feed names it, or once its copy is
ten minutes old. With 29 live claims, reading every item each pass took 18 s (2026-10-16)."""
from hx.herdr import Pane
from hx.reconcile import reconcile
from hx.showcache import ShowCache
from hx.tracker import Trk
from tests.conftest import FakeHerdr

W = Pane("w1:p2", "w1", "s-worker", "idle", "/", "w1:t1")
T = 1_800_000_000.0


class CountingTrk(Trk):
    def __init__(self, home):
        super().__init__(home=str(home))
        self.shown: list[str] = []

    def show(self, item_id):
        self.shown.append(item_id)
        return super().show(item_id)


def go(trk, tmp_path, herdr, cache, now):
    return reconcile(trk, herdr, tmp_path / "state.json", tmp_path / "inbox.log", tmp_path / "q",
                     {"michael": ["console"]}, cache=cache, now=now)


def test_a_quiet_second_pass_shows_nothing(trk_home, tree, tmp_path):
    trk, cache = CountingTrk(trk_home), ShowCache()
    go(trk, tmp_path, FakeHerdr([W]), cache, T)
    assert trk.shown
    trk.shown.clear()
    go(trk, tmp_path, FakeHerdr([W]), cache, T + 30)
    assert trk.shown == []


def test_an_item_the_feed_names_is_read_again(trk_home, sh, tree, tmp_path):
    trk, cache = CountingTrk(trk_home), ShowCache()
    go(trk, tmp_path, FakeHerdr([W]), cache, T)
    sh("criterion", "add", tree["ticket"], "c2", "--as", "worker", "--session", "s-worker")
    trk.shown.clear()
    h = FakeHerdr([W])
    go(trk, tmp_path, h, cache, T + 30)
    assert trk.shown == [tree["ticket"]]
    assert ("report_pane", "w1:p2", f"{tree['ticket']} ac 0/2") in h.calls


def test_a_copy_is_read_again_after_ten_minutes(trk_home, tree, tmp_path):
    trk, cache = CountingTrk(trk_home), ShowCache()
    go(trk, tmp_path, FakeHerdr([W]), cache, T)
    trk.shown.clear()
    go(trk, tmp_path, FakeHerdr([W]), cache, T + 600)
    assert sorted(trk.shown) == sorted([tree["ticket"], tree["program"]])


def test_ask_ancestors_come_from_the_cache(trk_home, sh, tree, tmp_path):
    sh("ask", tree["ticket"], "Colour?", "--to", "product", "--question", "-",
       "--as", "worker", "--session", "s-worker", stdin="Which?")
    trk, cache = CountingTrk(trk_home), ShowCache()
    go(trk, tmp_path, FakeHerdr([W]), cache, T)
    trk.shown.clear()
    go(trk, tmp_path, FakeHerdr([W]), cache, T + 30)
    assert trk.shown == []


def test_a_new_epoch_empties_the_cache():
    c = ShowCache()
    c.items["tr-a"] = ({"id": "tr-a"}, T)
    c.clear()
    assert c.items == {}


def test_without_a_cache_every_pass_reads_fresh(trk_home, tree, tmp_path):
    trk = CountingTrk(trk_home)
    reconcile(trk, FakeHerdr([W]), tmp_path / "state.json", tmp_path / "inbox.log", tmp_path / "q",
              {"michael": ["console"]})
    trk.shown.clear()
    reconcile(trk, FakeHerdr([W]), tmp_path / "state.json", tmp_path / "inbox.log", tmp_path / "q",
              {"michael": ["console"]})
    assert trk.shown


def test_the_watcher_keeps_one_cache_across_passes(monkeypatch, tmp_path):
    import pytest

    from hx import cli
    seen, sleeps = [], []
    monkeypatch.setenv("HX_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "reconcile", lambda *a, **kw: seen.append(kw.get("cache")) or [])

    def sleep(_):
        sleeps.append(1)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(cli.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        cli._watch(0.0, 0.0)
    assert len(seen) == 2 and seen[0] is not None and seen[0] is seen[1]
