import json
import os
import socket
import threading
from pathlib import Path

import pytest

from tests.conftest import FakeHerdr
from hx import sidebar
from hx.herdr import Herdr, HerdrError, Pane

NOW = 1_800_000_000.0
HOUR_MS = 3_600_000


def pane(pid: str) -> Pane:
    return Pane(pid, pid.split(":")[0], "s-" + pid, "idle", "/w")


def queue_item(queue: Path, name: str, pane_id: str, ask: str) -> None:
    queue.mkdir(parents=True, exist_ok=True)
    (queue / name).write_text(json.dumps({"pane": pane_id, "workspace": pane_id.split(":")[0],
                                          "ask": ask, "cwd": "/w", "ts": "1"}))


def cache_file(cache: Path, pane_id: str, left_s: float, working: bool = False,
               ttl_ms: int = HOUR_MS) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"{pane_id}.json").write_text(json.dumps(
        {"pane": pane_id, "expiresAt": (NOW + left_s) * 1000, "ttlMs": ttl_ms, "isWorking": working}))


def waiting(ask: str, base: float | None) -> dict:
    row = {"id": ask}
    if base is not None:
        row["urgency"] = {"score": base, "base": base, "held_up": 1, "minutes": 0, "cache_warm": True}
    return row


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "queue", tmp_path / "cache"


# --- the cache column ---------------------------------------------------------------------------

@pytest.mark.parametrize("left, working, text", [
    (52 * 60, False, "52m"),
    (9 * 60 + 1, False, "10m"),        # whole minutes, rounded up: never shows less than is left
    (9 * 60, False, "9m!"),            # under 10 minutes is marked
    (30, False, "1m!"),
    (0, False, "cold"),
    (-5, False, "cold"),
    (40 * 60, True, "busy"),           # a working session answers nothing yet
    (None, False, "?"),                # no cache-timer file for the pane
])
def test_cache_text(left, working, text):
    assert sidebar.cache_text(left, working) == text


@pytest.mark.parametrize("left, factor", [
    (3600, 1.0),          # fresh: no bump
    (1800, 3.25),         # half way: 1 + 9 * 0.25
    (0, 1.0),             # cold: the bump is gone
    (None, 1.0),          # unknown
])
def test_factor_bumps_as_the_cache_nears_its_end(left, factor):
    assert sidebar.factor(left, HOUR_MS) == pytest.approx(factor)


def test_factor_follows_the_panes_own_ttl():
    assert sidebar.factor(150, 300_000) == pytest.approx(3.25)     # a 5-minute cache, half gone


def test_cache_left_reads_cache_timer_file(dirs):
    _, cache = dirs
    cache_file(cache, "w1:p1", 600, working=True)
    assert sidebar.cache_left("w1:p1", cache, NOW) == (600, True, HOUR_MS)


@pytest.mark.parametrize("content", ["not json", "[]", '{"expiresAt": "soon"}', '{"isWorking": true}'])
def test_cache_left_of_a_bad_file_is_unknown(dirs, content):
    _, cache = dirs
    cache.mkdir(parents=True)
    (cache / "w1:p1.json").write_text(content)
    assert sidebar.cache_left("w1:p1", cache, NOW)[0] is None


# --- rows ---------------------------------------------------------------------------------------

def test_hx_item_row_takes_the_ask_from_its_file_name_and_its_base_from_trk(dirs):
    queue, cache = dirs
    queue_item(queue, "hx_tr-aaaaa.json", "w1:p1", "**Ask:** something")
    cache_file(cache, "w1:p1", 1800)
    rows = sidebar.rows(queue, cache, [waiting("tr-aaaaa", 4.0)], {"w1:p1"}, NOW)
    assert rows == {"w1:p1": sidebar.Row("w1:p1", "tr-aaaaa", "30m", 13.0, 1800)}


def test_pane_item_row_takes_the_first_open_ask_its_text_names(dirs):
    queue, cache = dirs
    queue_item(queue, "w1_p2.json", "w1:p2", "**Ask:** about tr-zzzzz (closed) and tr-bbbbb now")
    rows = sidebar.rows(queue, cache, [waiting("tr-bbbbb", 2.0)], {"w1:p2"}, NOW)
    assert rows["w1:p2"].ask == "tr-bbbbb"
    assert rows["w1:p2"].score == 2.0            # no cache file: no bump


def test_ask_id_must_stand_alone_in_the_text(dirs):
    queue, cache = dirs
    queue_item(queue, "w1_p2.json", "w1:p2", "see xtr-bbbbb and tr-bbbbb-old")
    assert sidebar.rows(queue, cache, [waiting("tr-bbbbb", 2.0)], {"w1:p2"}, NOW)["w1:p2"].ask is None


def test_row_without_a_trk_ask_is_unscored(dirs):
    queue, cache = dirs
    queue_item(queue, "w1_p3.json", "w1:p3", "**Ask:** merge it?")
    row = sidebar.rows(queue, cache, [], {"w1:p3"}, NOW)["w1:p3"]
    assert (row.ask, row.score) == (None, None)


def test_a_working_panes_own_ask_is_left_out_and_its_file_kept(dirs):
    """A background task or timed wake sets a pane going without the prompt hook that takes its item off."""
    queue, cache = dirs
    queue_item(queue, "w1_p3.json", "w1:p3", "**Ask:** merge it?")
    assert sidebar.rows(queue, cache, [], {"w1:p3"}, NOW, busy={"w1:p3"}) == {}
    assert (queue / "w1_p3.json").exists()
    assert "w1:p3" in sidebar.rows(queue, cache, [], {"w1:p3"}, NOW)          # idle again: it shows


def test_a_trk_ask_on_a_working_pane_still_shows(dirs):
    queue, cache = dirs
    queue_item(queue, "hx_tr-aaaaa.json", "w1:p1", "x")
    assert sidebar.rows(queue, cache, [waiting("tr-aaaaa", 4.0)], {"w1:p1"}, NOW, busy={"w1:p1"})["w1:p1"].ask \
        == "tr-aaaaa"


def test_ask_without_an_urgency_key_is_unscored(dirs):
    """Before trk's urgency score ships (tr-qwwsc), every row is unscored."""
    queue, cache = dirs
    queue_item(queue, "hx_tr-aaaaa.json", "w1:p1", "x")
    assert sidebar.rows(queue, cache, [waiting("tr-aaaaa", None)], {"w1:p1"}, NOW)["w1:p1"].score is None


@pytest.mark.parametrize("urgency", [None, "high", {"base": "4"}, {"base": None}, {"base": float("nan")}, {"base": -1}])
def test_bad_urgency_is_unscored(dirs, urgency):
    queue, cache = dirs
    queue_item(queue, "hx_tr-aaaaa.json", "w1:p1", "x")
    rows = sidebar.rows(queue, cache, [{"id": "tr-aaaaa", "urgency": urgency}], {"w1:p1"}, NOW)
    assert rows["w1:p1"].score is None


def test_item_of_a_closed_pane_is_not_shown(dirs):
    queue, cache = dirs
    queue_item(queue, "w9_p1.json", "w9:p1", "**Ask:** x")
    assert sidebar.rows(queue, cache, [], {"w1:p1"}, NOW) == {}


def test_two_items_on_one_pane_keep_the_higher_score(dirs):
    queue, cache = dirs
    queue_item(queue, "hx_tr-aaaaa.json", "w1:p1", "x")
    queue_item(queue, "w1_p1.json", "w1:p1", "**Ask:** tr-bbbbb?")
    rows = sidebar.rows(queue, cache, [waiting("tr-aaaaa", 1.0), waiting("tr-bbbbb", 5.0)], {"w1:p1"}, NOW)
    assert rows["w1:p1"].ask == "tr-bbbbb"


def test_unreadable_and_odd_queue_files_are_skipped(dirs):
    queue, cache = dirs
    queue.mkdir(parents=True)
    (queue / "w1_p1.json").write_text("{nope")
    (queue / "w1_p2.json").write_text('["list"]')
    (queue / "w1_p3.json").write_text('{"ask": "no pane"}')
    (queue / ".hx_tr-aaaaa.tmp").write_text('{"pane": "w1:p4", "ask": "x"}')
    assert sidebar.rows(queue, cache, [], {"w1:p1", "w1:p2", "w1:p3", "w1:p4"}, NOW) == {}


def test_no_queue_folder_means_no_rows(dirs):
    queue, cache = dirs
    assert sidebar.rows(queue, cache, [], {"w1:p1"}, NOW) == {}


# --- a pane held on a dialog -------------------------------------------------------------------

def blocked_pane(pid: str) -> Pane:
    return Pane(pid, pid.split(":")[0], "s-" + pid, "blocked", "/w")


def mode_file(folder: Path, pane_id: str, mode: str, driver: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{pane_id}.json").write_text(json.dumps({"mode": mode, "driver": driver}))


def test_a_blocked_pane_with_no_item_gets_a_dialog_row_above_every_ask(dirs, tmp_path):
    queue, cache = dirs
    cache_file(cache, "w1:p2", 300)
    queue_item(queue, "w1_p1.json", "w1:p1", "tr-aaaaa")
    panes = [pane("w1:p1"), blocked_pane("w1:p2")]
    out = sidebar.rows(queue, cache, [waiting("tr-aaaaa", 1e9)], {"w1:p1", "w1:p2"}, NOW,
                       sidebar.blocked(panes, tmp_path / "mode"))
    assert out["w1:p2"] == sidebar.Row("w1:p2", None, "5m!", None, 300, dialog=True)
    toks = {p: sidebar.tokens(r) for p, r in out.items()}
    assert toks["w1:p2"]["hx_item"] == "⚑ dialog" and toks["w1:p2"]["hx_score"] == "open"
    assert toks["w1:p2"]["hx_sort"] > toks["w1:p1"]["hx_sort"]


def test_the_dialog_row_goes_when_the_pane_is_no_longer_blocked(dirs, tmp_path):
    queue, cache = dirs
    assert sidebar.rows(queue, cache, [], {"w1:p2"}, NOW, sidebar.blocked([pane("w1:p2")], tmp_path)) == {}


def test_a_blocked_pane_with_an_item_shows_one_row(dirs, tmp_path):
    queue, cache = dirs
    queue_item(queue, "w1_p2.json", "w1:p2", "tr-aaaaa")
    out = sidebar.rows(queue, cache, [], {"w1:p2"}, NOW, sidebar.blocked([blocked_pane("w1:p2")], tmp_path))
    assert list(out) == ["w1:p2"] and not out["w1:p2"].dialog and out["w1:p2"].ask is None


def test_a_blocked_pane_a_live_driver_runs_is_left_to_the_driver(tmp_path):
    mode = tmp_path / "mode"
    mode_file(mode, "w1:p2", "agentic", "w1:p1")
    mode_file(mode, "w1:p3", "agentic", "w9:p1")           # its driver is gone
    mode_file(mode, "w1:p4", "async", "w1:p1")             # not agentic: the person drives it
    mode_file(mode, "w1:p5", "agentic", "w1:p5")           # drives itself
    panes = [pane("w1:p1")] + [blocked_pane(f"w1:p{n}") for n in (2, 3, 4, 5)]
    assert sidebar.blocked(panes, mode) == {"w1:p3", "w1:p4", "w1:p5"}


@pytest.mark.parametrize("content", ["", "nope", "[]", '{"mode": "agentic", "driver": 3}'])
def test_a_bad_mode_file_means_no_driver(tmp_path, content):
    (tmp_path / "w1:p2.json").write_text(content)
    assert sidebar.blocked([pane("w1:p1"), blocked_pane("w1:p2")], tmp_path) == {"w1:p2"}


def test_a_closed_blocked_pane_has_no_row(dirs):
    queue, cache = dirs
    assert sidebar.rows(queue, cache, [], {"w1:p1"}, NOW, {"w9:p9"}) == {}


def test_refresh_shows_a_dialog_row(dirs, tmp_path, monkeypatch):
    from hx import reconcile
    queue, cache = dirs
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    h = FakeHerdr([pane("w1:p1"), blocked_pane("w1:p2")])
    assert reconcile.refresh_sidebar(Waiting([]), h, tmp_path / "state.json", queue, NOW) == []
    reported = [c for c in h.sidebar if c[0] == "report_tokens"]
    assert [c[1] for c in reported] == ["w1:p2"] and reported[0][2]["hx_item"] == "⚑ dialog"
    assert json.loads((tmp_path / "sidebar.json").read_text())["count"] == 1


# --- tokens and sort ----------------------------------------------------------------------------

def test_tokens_of_a_scored_row():
    assert sidebar.tokens(sidebar.Row("w1:p1", "tr-aaaaa", "9m!", 12.54, 540)) == {
        "hx_item": "tr-aaaaa", "hx_cache": "9m!", "hx_score": "▲ 12.5",
        "hx_sort": "000125", "hx_tie": "00540"}


def test_tokens_of_an_unscored_row_sort_last_and_cold_after_warm():
    t = sidebar.tokens(sidebar.Row("w1:p1", None, "cold", None, 0))
    assert t == {"hx_item": "⚑", "hx_cache": "cold", "hx_score": "–", "hx_sort": "000000", "hx_tie": "99999"}


def test_sort_token_is_capped_and_never_ties_a_scored_row_with_an_unscored_one():
    assert sidebar.tokens(sidebar.Row("p", "a", "1m!", 1e9, 60))["hx_sort"] == "999998"
    assert sidebar.tokens(sidebar.Row("p", "a", "1m!", 0.01, 60))["hx_sort"] == "000001"


def test_busy_and_unknown_cache_sort_after_warm():
    assert sidebar.tokens(sidebar.Row("p", "a", "busy", None, None))["hx_tie"] == "99999"


def test_rows_sort_as_the_panel_will():
    rows = [sidebar.Row("a", "tr-1", "52m", 4.0, 3120), sidebar.Row("b", "tr-2", "cold", 38.0, 0),
            sidebar.Row("c", None, "9m!", None, 540), sidebar.Row("d", None, "40m", None, 2400)]
    toks = {r.pane: sidebar.tokens(r) for r in rows}
    order = sorted(toks, key=lambda p: (-int(toks[p]["hx_sort"]), toks[p]["hx_tie"]))
    assert order == ["b", "a", "c", "d"]


# --- publish ------------------------------------------------------------------------------------

def test_publish_sets_the_view_reports_tokens_and_clears_old_live_rows():
    h = FakeHerdr([])
    row = sidebar.Row("w1:p1", "tr-aaaaa", "30m", 13.0, 1800)
    shown, errors = sidebar.publish(h, {"w1:p1": row}, before=["w1:p1", "w1:p9"], live={"w1:p1", "w1:p9"})
    assert (shown, errors) == (["w1:p1"], [])
    assert h.sidebar == [
        ("view_set", sidebar.VIEW),
        ("report_tokens", "w1:p1", sidebar.tokens(row), sidebar.TTL_MS),
        ("clear_tokens", "w1:p9", sidebar.NAMES)]


def test_publish_never_clears_a_closed_pane():
    """Its tokens went with it; herdr would refuse the call on every pass (review finding)."""
    h = FakeHerdr([])
    shown, errors = sidebar.publish(h, {}, before=["w1:p9"], live={"w1:p1"})
    assert (shown, errors) == ([], []) and h.sidebar == [("view_set", sidebar.VIEW)]


def test_one_failing_pane_never_stops_the_others_or_the_view():
    h = FakeHerdr([])
    rows = {p: sidebar.Row(p, "tr-a", "30m", 1.0, 1800) for p in ("w1:p1", "w1:p2")}
    real = h.report_tokens

    def flaky(pane, tokens, ttl):
        if pane == "w1:p1":
            raise RuntimeError("pane gone")
        real(pane, tokens, ttl)
    h.report_tokens = flaky
    h.clear_tokens = lambda pane, names: (_ for _ in ()).throw(RuntimeError("clear failed"))
    shown, errors = sidebar.publish(h, rows, before=["w1:p3"], live={"w1:p1", "w1:p2", "w1:p3"})
    assert shown == ["w1:p2"]
    assert errors == ["w1:p1: RuntimeError: pane gone", "w1:p3: RuntimeError: clear failed"]
    assert [c[0] for c in h.sidebar] == ["view_set", "report_tokens"]


def test_a_failed_view_still_reports_the_rows():
    h = FakeHerdr([])
    h.view_set = lambda view: (_ for _ in ()).throw(RuntimeError("no plugin"))
    shown, errors = sidebar.publish(h, {"w1:p1": sidebar.Row("w1:p1", None, "?", None, None)}, [], {"w1:p1"})
    assert shown == ["w1:p1"] and errors == ["view: RuntimeError: no plugin"]


def test_view_filters_on_the_sort_token_and_names_the_panel():
    assert sidebar.VIEW["source"] == "plugin:hx"
    assert sidebar.VIEW["label"] == "needs you"
    assert sidebar.VIEW["filter"] == {"op": "exists", "field": {"token": "hx_sort"}}
    assert [s["field"]["token"] for s in sidebar.VIEW["sort"]] == ["hx_sort", "hx_tie"]


def test_publish_with_no_rows_still_sets_the_view():
    h = FakeHerdr([])
    assert sidebar.publish(h, {}, before=[], live=set()) == ([], [])
    assert h.sidebar == [("view_set", sidebar.VIEW)]


def test_ttl_outlives_a_slow_pass_but_not_a_dead_watcher():
    """The service passes every 30 s (--tick) when nothing changes; one slow herdr or trk call
    must not empty the panel (review finding), and a stopped watcher must, within minutes."""
    assert 4 * 30_000 <= sidebar.TTL_MS <= sidebar.STALE_S * 1000


# --- the herdr socket call ----------------------------------------------------------------------

@pytest.fixture
def fake_socket(tmp_path):
    """A one-request herdr socket: records the request and sends the given reply."""
    path = Path("/tmp") / f"hxt-{os.getpid()}-{id(tmp_path) % 10_000}.sock"
    path.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(path))
    srv.listen(1)
    got: dict = {}

    def serve(reply: dict):
        conn, _ = srv.accept()
        with conn:
            got["req"] = json.loads(conn.makefile().readline())
            conn.sendall((json.dumps({"id": got["req"]["id"], **reply}) + "\n").encode())

    yield path, got, serve
    srv.close()
    path.unlink(missing_ok=True)


def test_view_set_sends_one_request_on_the_socket(fake_socket):
    path, got, serve = fake_socket
    t = threading.Thread(target=serve, args=({"result": {"type": "agent_view", "active": True}},))
    t.start()
    h = Herdr()
    h.socket = str(path)
    h.view_set(sidebar.VIEW)
    t.join(5)
    assert got["req"]["method"] == "agent.view.set"
    assert got["req"]["params"] == sidebar.VIEW


def test_view_set_error_reply_raises(fake_socket):
    path, _, serve = fake_socket
    t = threading.Thread(target=serve, args=({"error": {"code": "plugin_not_found", "message": "no hx"}},))
    t.start()
    h = Herdr()
    h.socket = str(path)
    with pytest.raises(HerdrError, match="plugin_not_found"):
        h.view_set(sidebar.VIEW)
    t.join(5)


def test_view_set_without_a_server_raises(tmp_path):
    h = Herdr()
    h.socket = str(tmp_path / "none.sock")
    with pytest.raises(HerdrError):
        h.view_set(sidebar.VIEW)


def test_socket_defaults_to_the_default_session(monkeypatch):
    monkeypatch.delenv("HX_HERDR_SOCKET")
    assert Herdr().socket == str(Path.home() / ".config/herdr/herdr.sock")


def test_tests_never_reach_the_live_socket(monkeypatch):
    monkeypatch.delenv("HX_HERDR_SOCKET")
    with pytest.raises(HerdrError, match="never call live herdr"):
        Herdr().view_set(sidebar.VIEW)


def test_report_tokens_runs_report_metadata(monkeypatch):
    seen = []
    monkeypatch.setattr(Herdr, "_run", lambda self, *a: seen.append(a) or "")
    Herdr().report_tokens("w1:p1", {"hx_item": "tr-a", "hx_cache": "9m!"}, 60_000)
    Herdr().clear_tokens("w1:p1", ("hx_item", "hx_cache"))
    assert seen == [
        ("pane", "report-metadata", "w1:p1", "--source", "hx", "--ttl-ms", "60000",
         "--token", "hx_item=tr-a", "--token", "hx_cache=9m!"),
        ("pane", "report-metadata", "w1:p1", "--source", "hx",
         "--clear-token", "hx_item", "--clear-token", "hx_cache")]


# --- the tab-bar line ---------------------------------------------------------------------------

def test_line_counts_rows_and_names_the_top_scored_one(tmp_path):
    p = tmp_path / "sidebar.json"
    sidebar.write_line(p, {"a": sidebar.Row("a", "tr-1", "52m", 4.0, 3120),
                           "b": sidebar.Row("b", "tr-2", "cold", 38.04, 0),
                           "c": sidebar.Row("c", None, "9m!", None, 540)}, NOW)
    assert sidebar.line(p, NOW + 5) == "⚑ 3 · tr-2 38"


def test_line_without_a_scored_row_is_only_the_count(tmp_path):
    p = tmp_path / "sidebar.json"
    sidebar.write_line(p, {"c": sidebar.Row("c", None, "9m!", None, 540)}, NOW)
    assert sidebar.line(p, NOW) == "⚑ 1"


def test_line_is_empty_when_nothing_waits(tmp_path):
    p = tmp_path / "sidebar.json"
    sidebar.write_line(p, {}, NOW)
    assert sidebar.line(p, NOW) == ""


@pytest.mark.parametrize("age", [sidebar.STALE_S + 1, 10_000])
def test_line_says_stale_when_the_watcher_stopped(tmp_path, age):
    p = tmp_path / "sidebar.json"
    sidebar.write_line(p, {}, NOW)
    assert sidebar.line(p, NOW + age) == "hx stale"


@pytest.mark.parametrize("content", [None, "nope", "[]", '{"at": "x"}'])
def test_line_of_a_missing_or_bad_file_is_stale(tmp_path, content):
    p = tmp_path / "sidebar.json"
    if content is not None:
        p.write_text(content)
    assert sidebar.line(p, NOW) == "hx stale"



# --- the fast refresh: a queue change redraws the panel without a full pass ---------------------

class Waiting:
    def __init__(self, asks):
        self.asks = asks

    def waiting(self):
        return {"asks": self.asks}


def test_refresh_drops_the_row_of_an_answered_pane(dirs, tmp_path, monkeypatch):
    """herdr-jump's prompt hook deletes the pane's item; the refresh clears its row at once."""
    from hx import reconcile, state as st
    queue, cache = dirs
    monkeypatch.setenv("CACHE_STATE_DIR", str(cache))
    queue_item(queue, "w1_p1.json", "w1:p1", "tr-aaaaa")
    queue_item(queue, "w1_p2.json", "w1:p2", "tr-bbbbb")
    path = tmp_path / "state.json"
    s = st.State()
    s.sidebar = ["w1:p1", "w1:p2"]
    st.save(s, path)
    (queue / "w1_p1.json").unlink()
    h = FakeHerdr([pane("w1:p1"), pane("w1:p2")])
    errors = reconcile.refresh_sidebar(Waiting([waiting("tr-bbbbb", 2.0)]), h, path, queue, NOW)
    assert errors == []
    assert ("clear_tokens", "w1:p1", sidebar.NAMES) in h.sidebar
    assert [c[1] for c in h.sidebar if c[0] == "report_tokens"] == ["w1:p2"]
    assert st.load(path).sidebar == ["w1:p2"]
    assert json.loads((tmp_path / "sidebar.json").read_text())["count"] == 1


def test_refresh_shows_a_new_item_and_keeps_the_rest_of_the_state(dirs, tmp_path, monkeypatch):
    from hx import reconcile, state as st
    queue, cache = dirs
    monkeypatch.setenv("CACHE_STATE_DIR", str(cache))
    path = tmp_path / "state.json"
    s = st.State()
    s.done["kept"] = 1.0
    st.save(s, path)
    queue_item(queue, "w1_p1.json", "w1:p1", "tr-aaaaa")
    h = FakeHerdr([pane("w1:p1")])
    assert reconcile.refresh_sidebar(Waiting([]), h, path, queue, NOW) == []
    after = st.load(path)
    assert after.sidebar == ["w1:p1"] and after.done == {"kept": 1.0}


def test_refresh_with_trk_down_still_shows_the_queue_rows(dirs, tmp_path, monkeypatch):
    """UP-0020: a trk failure no longer blanks the panel; the queue files still make rows, and the
    tab line says trk is down instead of `hx stale`."""
    from hx import reconcile, state as st

    class Broken(Waiting):
        def waiting(self):
            raise RuntimeError("trk down")
    queue, cache = dirs
    monkeypatch.setenv("CACHE_STATE_DIR", str(cache))
    queue_item(queue, "w1_p1.json", "w1:p1", "tr-aaaaa")
    path = tmp_path / "state.json"
    st.save(st.State(), path)
    h = FakeHerdr([pane("w1:p1")])
    errors = reconcile.refresh_sidebar(Broken([]), h, path, queue, NOW)
    assert errors and "trk down" in errors[0]
    assert [c[1] for c in h.sidebar if c[0] == "report_tokens"] == ["w1:p1"]
    assert st.load(path).sidebar == ["w1:p1"]
    line_file = tmp_path / "sidebar.json"
    assert "trk down" in json.loads(line_file.read_text())["trk_error"]
    assert sidebar.line(line_file, NOW) == "⚑ 1 · trk down"


def test_refresh_reports_a_left_out_ask_and_shows_the_rest(dirs, tmp_path, monkeypatch):
    from hx import reconcile, state as st

    class Partial(Waiting):
        def waiting(self):
            return {"asks": self.asks, "invalid": [{"id": "tr-ccccc", "error": "'3b1da7854' does not match"}]}
    queue, cache = dirs
    monkeypatch.setenv("CACHE_STATE_DIR", str(cache))
    queue_item(queue, "w1_p1.json", "w1:p1", "tr-aaaaa")
    path = tmp_path / "state.json"
    st.save(st.State(), path)
    h = FakeHerdr([pane("w1:p1")])
    errors = reconcile.refresh_sidebar(Partial([waiting("tr-aaaaa", 2.0)]), h, path, queue, NOW)
    assert len(errors) == 1 and "tr-ccccc" in errors[0]
    assert sidebar.line(tmp_path / "sidebar.json", NOW).startswith("⚑ 1 · tr-aaaaa")


def test_refresh_with_herdr_down_changes_nothing(dirs, tmp_path):
    from hx import reconcile, state as st

    class Down(FakeHerdr):
        def panes(self):
            raise RuntimeError("herdr down")
    queue, _ = dirs
    path = tmp_path / "state.json"
    s = st.State()
    s.sidebar = ["w1:p1"]
    st.save(s, path)
    h = Down([pane("w1:p1")])
    errors = reconcile.refresh_sidebar(Waiting([]), h, path, queue, NOW)
    assert errors and "herdr down" in errors[0]
    assert h.sidebar == [] and st.load(path).sidebar == ["w1:p1"]
