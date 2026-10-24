from hx.cli import main, watch_once
from tests.conftest import FakeHerdr


def test_watch_runs_on_head_change_and_on_tick(tmp_path):
    head = tmp_path / "feed.head"
    head.write_text("1")
    calls = []
    mt, lr = watch_once(None, head, lambda: calls.append(1), now=100.0, last_run=100.0, tick=30)
    assert calls == [1]
    mt, lr = watch_once(mt, head, lambda: calls.append(2), now=101.0, last_run=lr, tick=30)
    assert calls == [1]
    mt, lr = watch_once(mt, head, lambda: calls.append(3), now=200.0, last_run=lr, tick=30)
    assert calls == [1, 3]


def test_reconcile_dry_run_prints_actions(trk_home, sh, tree, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HX_CONFIG", str(tmp_path / "none.toml"))
    monkeypatch.setattr("hx.cli.Herdr", lambda: FakeHerdr([]))
    sh("ask", tree["ticket"], "Colour?", "--to", "product", "--question", "-",
       "--as", "worker", "--session", "s-worker", stdin="Which colour?")
    assert main(["reconcile", "--dry-run"]) == 0
    assert "Notify deliver:" in capsys.readouterr().out


def test_start_passes_flags_and_parent(monkeypatch, tmp_path, capsys):
    from hx import cli
    got = {}
    monkeypatch.setenv("HX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TRK_SESSION", "s-boss")
    monkeypatch.setattr(cli, "start", lambda *a, **kw: got.update(kw) or "ok")
    assert cli.main(["start", "tr-r", "--model", "claude-opus-5-5", "--effort", "high",
                     "--reason", "kickoff framing", "--dry-run"]) == 0
    assert got == {"model": "claude-opus-5-5", "effort": "high", "reason": "kickoff framing",
                   "parent": "s-boss", "dry_run": True}


def test_start_herdr_error_is_one_line(monkeypatch, tmp_path, capsys):
    from hx import cli
    from hx.herdr import HerdrError
    monkeypatch.setenv("HX_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("TRK_SESSION", raising=False)

    def boom(*a, **kw):
        raise HerdrError("herdr down")
    monkeypatch.setattr(cli, "start", boom)
    assert cli.main(["start", "tr-r"]) == 1
    assert capsys.readouterr().err.strip() == "hx start: herdr down"


def test_watch_queue_refreshes_when_an_item_comes_or_goes(tmp_path):
    from hx.cli import watch_queue
    q = tmp_path / "queue"
    q.mkdir()
    calls = []
    last = watch_queue(None, q, lambda: calls.append(1))
    assert calls == []                                   # the first look only records the items
    (q / ".list-cache").write_text("x")                  # herdr-jump's own files never count
    assert watch_queue(last, q, lambda: calls.append(2)) == last and calls == []
    (q / "w1_p1.json").write_text("{}")
    last = watch_queue(last, q, lambda: calls.append(3))
    (q / "w1_p1.json").unlink()                          # the prompt hook took it
    last = watch_queue(last, q, lambda: calls.append(4))
    assert calls == [3, 4]


def test_watch_queue_of_a_missing_folder_never_refreshes(tmp_path):
    from hx.cli import watch_queue
    calls = []
    last = watch_queue(None, tmp_path / "none", lambda: calls.append(1))
    assert watch_queue(last, tmp_path / "none", lambda: calls.append(1)) == last and calls == []
