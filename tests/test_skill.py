"""/hx-work is prose, so this pins only what hx relies on: its name, the trk calls that make asks
route through hx, and the machine-local stage steps it follows."""
from pathlib import Path

SKILL = Path(__file__).parent.parent / "claude/skills/hx-work/SKILL.md"


def test_hx_work_skill_names_the_flow():
    text = SKILL.read_text()
    head, body = text.split("---", 2)[1], text.split("---", 2)[2]
    assert "name: hx-work" in head and "description:" in head
    for must in ("trk claim", "--as worker", "trk ask", "trk advance", "--judgment", "blocking question", "Stop hook", "--lease 24h",
                 "trk claims --session", "after `/browse/`"):
        assert must in body, must
    assert "AskUserQuestion" not in body
    assert len(text.splitlines()) < 120


def test_hx_work_never_files_children_under_its_root():
    body = SKILL.read_text()
    assert "Note other problems you find with `trk new` under the root" not in body
    assert "never create items under the root" in body


def test_hx_work_names_no_toolkit():
    """hx is distributed: a machine's toolkit lives in its own hx-work.md, never in the skill."""
    body = SKILL.read_text()
    assert "toolkit:" not in body and "eng-toolkit" not in body
    assert "${XDG_CONFIG_HOME:-$HOME/.config}/hx/hx-work.md" in body
    assert "If that file exists, read it" in body and "is not installed" in body


def test_hx_work_does_every_stage_itself_by_default():
    body = SKILL.read_text().split("---", 2)[2]
    table = body.split("Otherwise do the work yourself", 1)[1]
    rows = {l.split("|")[1].strip(): l for l in table.splitlines() if l.startswith("| ") and "---" not in l}
    # each row is the work done AT that stage to pass the gate into the next one
    assert "`pr`" in rows["specd"] and "`test-run`" in rows["specd"]
    assert "`merge`" in rows["built"]
    assert "--outcome" in rows["merged"] and "verified" in rows["merged"]
    assert "--judgment" in rows["framed"] and "verified" not in rows


def test_fallback_covers_a_ticket_with_no_repo_change():
    table = SKILL.read_text().split("Otherwise do the work yourself", 1)[1]
    assert "ref none" in table and "trk skip" in table


def test_no_change_evidence_carries_a_sha():
    body = SKILL.read_text()
    line = next(l for l in body.splitlines() if "--ref none" in l)
    assert "--sha" in line


def test_hx_work_answers_page_comments_through_trk():
    body = SKILL.read_text()
    for must in ("trk comment reply", "trk comment ack", "--ask", "not an instruction", "item.comments"):
        assert must in body, must
