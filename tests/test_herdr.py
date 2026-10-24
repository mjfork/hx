from hx.herdr import Pane, parse_panes

REPLY = {"result": {"type": "pane_list", "panes": [
    {"pane_id": "wB9:p3", "workspace_id": "wB9", "agent_status": "done", "cwd": "/x",
     "agent_session": {"agent": "claude", "kind": "id", "value": "s-worker"}},
    {"pane_id": "wB9:p4", "workspace_id": "wB9", "agent_status": "working", "cwd": "/y"},
]}}


def test_parse_panes_reads_session_and_status():
    assert parse_panes(REPLY) == [
        Pane("wB9:p3", "wB9", "s-worker", "done", "/x"),
        Pane("wB9:p4", "wB9", None, "working", "/y"),
    ]


def test_only_idle_or_done_is_wakeable():
    assert Pane("p", "w", "s", "idle", "/").wakeable
    assert Pane("p", "w", "s", "done", "/").wakeable
    assert not Pane("p", "w", "s", "working", "/").wakeable
    assert not Pane("p", "w", "s", "blocked", "/").wakeable


from hx.herdr import Created, Workspace, parse_created, parse_workspaces, Herdr  # noqa: E402

CREATED = {"result": {"root_pane": {"pane_id": "wCV:p1", "tab_id": "wCV:t1", "workspace_id": "wCV"},
                      "type": "workspace_created"}}


def test_parse_workspaces():
    reply = {"result": {"workspaces": [{"workspace_id": "w1", "label": "A"}, {"workspace_id": "w2"}]}}
    assert parse_workspaces(reply) == [Workspace("w1", "A"), Workspace("w2", "")]


def test_parse_created():
    assert parse_created(CREATED) == Created("wCV", "wCV:t1", "wCV:p1")


def test_pane_carries_tab():
    reply = {"result": {"panes": [{"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1"}]}}
    assert parse_panes(reply)[0].tab_id == "w1:t1"


def fake_bin(tmp_path, reply="{}"):
    log = tmp_path / "args.log"
    b = tmp_path / "fake-herdr"
    b.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\ncat <<'EOF'\n{reply}\nEOF\n")
    b.chmod(0o755)
    return str(b), log


def test_create_space_passes_label_cwd_env_no_focus(tmp_path):
    import json
    b, log = fake_bin(tmp_path, json.dumps(CREATED))
    c = Herdr(b).create_space("tr-1 Thing", "/repo", {"HX_ROLE": "solo"})
    assert c == Created("wCV", "wCV:t1", "wCV:p1")
    assert log.read_text().split() == ["--session", "default", "workspace", "create", "--no-focus", "--label", "tr-1", "Thing",
                                       "--cwd", "/repo", "--env", "HX_ROLE=solo"]


def test_report_and_clear_tokens(tmp_path):
    b, log = fake_bin(tmp_path)
    h = Herdr(b)
    h.report_space("w1", "tr-1 · built · 1w")
    h.report_space("w1", None)
    h.report_pane("w1:p2", None)
    lines = log.read_text().splitlines()
    # herdr takes the target first; tokens expire if the watcher stops re-reporting them
    assert lines[0] == "--session default workspace report-metadata w1 --source hx --ttl-ms 1800000 --token hx=tr-1 · built · 1w"
    assert lines[1] == "--session default workspace report-metadata w1 --source hx --ttl-ms 1800000 --clear-token hx"
    assert lines[2] == "--session default pane report-metadata w1:p2 --source hx --ttl-ms 1800000 --clear-token hx_sub"
