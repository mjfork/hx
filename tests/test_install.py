"""install.sh, run against a throwaway HOME with stub uv, trk, herdr and systemctl: never the live box."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hx.config import load_config
from hx.herdr import Pane
from hx.reconcile import reconcile
from hx.tracker import Trk
from tests.conftest import FakeHerdr

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "install.sh"
STUB = '#!/bin/bash\necho "$(basename "$0") $*" >> "$HOME/calls.log"\n'
UNAME = '#!/bin/bash\necho "${FAKE_UNAME:-Linux}"\n'   # the extras' defaults follow the OS; tests pick it
UV = STUB + '''case "$1 $2" in
  "tool dir") if [[ "$*" == *--bin* ]]; then echo "$HOME/.local/bin"; else echo "$HOME/uv-tools"; fi ;;
  "tool list") for f in "$HOME"/uv-has-*; do [[ -e $f ]] && echo "${f##*/uv-has-} v0.1.0" && echo "- hx"; done ;;
  "tool install"*) [[ -f $HOME/uv-fail ]] && { echo "error: build failed" >&2; exit 1; }
    [[ -f $HOME/uv-has-herdr-extension && " $* " != *" --force "* ]] && { echo "error: Executable already exists: hx (use --force to overwrite)" >&2; exit 2; }
    mkdir -p "$HOME/.local/bin"
    { echo '#!/bin/bash'; echo 'echo "hx $*" >> "$HOME/calls.log"';
      echo '[[ -f $HOME/hx-sidebar-fail && $1 == sidebar ]] && exit 1'; echo 'exit 0'; } > "$HOME/.local/bin/hx"
    chmod +x "$HOME/.local/bin/hx"; touch "$HOME/uv-has-hx" ;;
  "tool uninstall"*) rm -f "$HOME/uv-has-$3" "$HOME/.local/bin/hx" ;;
esac
'''


@pytest.fixture
def box(tmp_path):
    home, bin = tmp_path / "home", tmp_path / "bin"
    home.mkdir(); bin.mkdir()
    for name, body in {"uv": UV, "trk": STUB, "herdr": STUB, "systemctl": STUB, "claude": STUB, "uname": UNAME}.items():
        (bin / name).write_text(body); (bin / name).chmod(0o755)
    (bin / "python3").write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")   # this python, not the box's
    (bin / "python3").chmod(0o755)
    env = {"HOME": str(home), "USER": "alice", "PATH": f"{bin}:/usr/bin:/bin"}

    def run(*args, ok=True):
        out = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)
        if ok:
            assert out.returncode == 0, out.stdout + out.stderr
        return out
    run.home, run.bin, run.env = home, bin, env
    run.calls = lambda: (home / "calls.log").read_text() if (home / "calls.log").exists() else ""
    return run


def test_dry_run_changes_nothing(box):
    out = box("--dry-run")
    assert "[dry-run]" in out.stdout
    assert "uv tool install" not in box.calls() and not (box.home / ".config").exists()
    assert not (box.home / ".claude").exists()


def test_dry_run_only_talks_about_the_herdr_sidebar(box):
    out = box("--dry-run")
    assert "sidebar setup" in out.stdout and "reload-config" in out.stdout
    assert "sidebar setup" not in box.calls() and "reload-config" not in box.calls()


def test_install_sets_the_herdr_sidebar_and_reloads_herdr(box):
    out = box()
    calls = box.calls()
    assert "hx sidebar setup" in calls
    assert calls.index("hx sidebar setup") < calls.index("herdr server reload-config")
    assert "herdr reloaded its config" in out.stdout


def test_no_sidebar_leaves_herdr_config_alone(box):
    out = box("--no-sidebar")
    assert "sidebar setup" not in box.calls() and "reload-config" not in box.calls()
    assert "next steps" in out.stdout                      # the rest of the install still finished
    assert (box.home / ".config/systemd/user/hx.service").exists()


def test_a_failed_sidebar_setup_warns_and_the_install_goes_on(box):
    (box.home / "hx-sidebar-fail").touch()
    out = box()
    assert "hx sidebar setup failed" in out.stdout        # install.sh's warn() prints on stdout
    assert "reload-config" not in box.calls()              # nothing changed, so herdr is not reloaded
    assert (box.home / ".config/systemd/user/hx.service").exists()


def test_install_puts_everything_in_place(box):
    out = box()
    assert f"uv tool install --editable {REPO}" in box.calls()
    skill = box.home / ".claude/skills/hx-work"
    assert skill.is_symlink() and skill.resolve() == (REPO / "claude/skills/hx-work").resolve()
    cfg = (box.home / ".config/hx/config.toml").read_text()
    assert '[humans.alice]\nchannels = ["console", "queue"]' in cfg
    unit = (box.home / ".config/systemd/user/hx.service").read_text()
    assert f"ExecStart={box.home}/.local/bin/hx watch" in unit and "@" not in unit
    assert "systemctl --user enable hx" in box.calls()
    assert "next steps" in out.stdout


def test_install_twice_is_safe_and_keeps_the_config(box):
    box()
    cfg = box.home / ".config/hx/config.toml"
    cfg.write_text("# mine\n")
    out = box()
    assert cfg.read_text() == "# mine\n" and "already installed" in out.stdout


def test_me_and_the_queue_channel_without_the_queue_directory(box):
    """A fresh machine has no ~/.herdr-queue: herdr-jump makes it at runtime, and hx makes it when it first
    delivers. The config asks for the channel either way, so no install order can leave asks out of the count."""
    box("--me", "bob")
    assert not (box.home / ".herdr-queue").exists()
    cfg = box.home / ".config/hx/config.toml"
    assert '[humans.bob]\nchannels = ["console", "queue"]' in cfg.read_text()
    assert load_config(cfg).humans == {"bob": ["console", "queue"]}   # what hx reads back: both channels


def console_only(box, body='[humans.alice]\nchannels = ["console"]\n'):
    """The config an older installer left on the boxes: its asks reach the inbox log, never the ⚑ count."""
    cfg = box.home / ".config/hx/config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(body)
    return cfg


def test_a_config_stuck_on_console_only_is_repaired_and_backed_up(box):
    cfg = console_only(box)

    out = box()

    assert cfg.read_text() == '[humans.alice]\nchannels = ["console", "queue"]\n'
    backup = next(cfg.parent.glob("config.toml.bak-hx-*"))
    assert backup.read_text() == '[humans.alice]\nchannels = ["console"]\n'
    assert "added the queue channel to [humans.alice]" in out.stdout and str(backup) in out.stdout
    assert load_config(cfg).humans == {"alice": ["console", "queue"]}


def test_a_repair_keeps_the_rest_of_the_config_and_the_comments(box):
    cfg = console_only(box, '# mine\n[humans.alice]\nchannels = [ "console" ]   # just me\nmodel = "x"\n\n'
                            '[spaces]\nclose_delay_s = 60\n')

    box()

    assert cfg.read_text() == ('# mine\n[humans.alice]\nchannels = ["console", "queue"]   # just me\nmodel = "x"\n'
                               '\n[spaces]\nclose_delay_s = 60\n')


def test_any_other_channel_list_is_left_alone_with_a_warning(box):
    """Only a list that is exactly ["console"] is safe to rewrite; the rest is a person's call."""
    body = '[humans.alice]\nchannels = ["console", "inbox"]\n\n[humans.bob]\nmodel = "x"\n'
    cfg = console_only(box, body)

    out = box()

    assert cfg.read_text() == body and not list(cfg.parent.glob("config.toml.bak-hx-*"))
    assert "[humans.alice], [humans.bob] name no \"queue\" channel" in out.stdout   # warn() prints on stdout
    assert 'channels = ["console", "queue"]' in out.stdout


def test_a_config_that_names_the_queue_is_left_as_it_is(box):
    box()
    cfg = box.home / ".config/hx/config.toml"
    before = cfg.read_text()

    out = box()

    assert cfg.read_text() == before and not list(cfg.parent.glob("config.toml.bak-hx-*"))
    assert 'names no "queue" channel' not in out.stdout and "added the queue channel" not in out.stdout


def test_dry_run_only_talks_about_repairing_the_config(box):
    cfg = console_only(box)

    out = box("--dry-run")

    assert cfg.read_text() == '[humans.alice]\nchannels = ["console"]\n'
    assert not list(cfg.parent.glob("config.toml.bak-hx-*"))
    assert "[dry-run] adding the queue channel to [humans.alice]" in out.stdout


def test_a_fresh_install_lands_an_open_ask_in_the_queue(box, trk_home, sh, tree, tmp_path):
    """The whole chain, with hx's own delivery: the installer writes the config on a machine with no
    ~/.herdr-queue, hx reads it back, and an open trk ask becomes an item in ~/.herdr-queue/default."""
    box("--me", "michael")
    cfg = load_config(box.home / ".config/hx/config.toml")
    queue = box.home / ".herdr-queue/default"      # the unit and herdr-jump's needs-you.sh name this one
    assert not queue.exists()
    ask = sh("ask", tree["ticket"], "Colour?", "--to", "product", "--question", "-",
             "--as", "worker", "--session", "s-worker", stdin="Which colour?")["item"]["id"]

    reconcile(Trk(home=str(trk_home)), FakeHerdr([Pane("w1:p2", "w1", "s-worker", "done", "/")]),
              tmp_path / "state.json", tmp_path / "inbox.log", queue, cfg.humans, dry_run=False)

    assert json.loads((queue / f"hx_{ask}.json").read_text())["pane"] == "w1:p2"


def test_no_editable_copies_the_skill(box):
    box("--no-editable", "--no-service")
    assert "--editable" not in box.calls() and "systemctl" not in box.calls()
    skill = box.home / ".claude/skills/hx-work"
    assert skill.is_dir() and not skill.is_symlink()


def test_refuses_a_differing_skill_without_force(box):
    skill = box.home / ".claude/skills/hx-work"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("mine")
    out = box(ok=False)
    assert out.returncode != 0 and "differ" in out.stderr and "uv tool install" not in box.calls()
    box("--force")
    assert skill.is_symlink() and list((box.home / ".local/share/hx-skill-backups").glob("hx-work.*"))
    assert not list(skill.parent.glob("*bak*"))  # no backup loads as a skill


def test_missing_trk_stops_the_install(box):
    (box.bin / "trk").unlink()
    out = box(ok=False)
    assert out.returncode != 0 and "trk" in out.stderr and "uv tool install" not in box.calls()


def test_uninstall_removes_what_it_added_and_keeps_config(box):
    box()
    state = box.home / ".local/state/hx"
    state.mkdir(parents=True)
    box("--uninstall")
    assert "uv tool uninstall hx" in box.calls()
    assert not (box.home / ".claude/skills/hx-work").exists()
    assert not (box.home / ".config/systemd/user/hx.service").exists()
    assert (box.home / ".config/hx/config.toml").exists() and state.exists()


def test_uninstall_leaves_a_tool_it_did_not_install(box):
    (box.home / "uv-has-hx").touch()
    box()
    box("--uninstall")
    assert "uv tool uninstall" not in box.calls()


def test_make_bundle(box, tmp_path):
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    out = tmp_path / "hx.bundle"
    box("make-bundle", str(out))
    subprocess.run(["git", "clone", "-q", str(out), str(tmp_path / "clone")], check=True)
    assert (tmp_path / "clone/install.sh").exists() or (tmp_path / "clone/pyproject.toml").exists()


@pytest.mark.parametrize("name", ["Alice", "al_ice", "a.b", "a@b", "-x"])
def test_me_must_be_a_trk_actor_name(box, name):
    out = box("--me", name, ok=False)
    assert out.returncode != 0 and "actor" in out.stderr
    assert not (box.home / ".config/hx").exists()


def test_xdg_config_home_is_used(box, tmp_path):
    import subprocess as sp
    xdg = tmp_path / "xdg"
    env = {"HOME": str(box.home), "USER": "alice", "PATH": f"{box.bin}:/usr/bin:/bin",
           "XDG_CONFIG_HOME": str(xdg)}
    out = sp.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    assert (xdg / "hx/config.toml").exists()
    unit = (xdg / "systemd/user/hx.service").read_text()
    assert f"Environment=HX_CONFIG={xdg}/hx/config.toml" in unit


def test_hx_config_is_honored(box, tmp_path):
    import subprocess as sp
    cfg = tmp_path / "mine.toml"
    cfg.write_text("# mine\n")
    env = {"HOME": str(box.home), "USER": "alice", "PATH": f"{box.bin}:/usr/bin:/bin", "HX_CONFIG": str(cfg)}
    assert sp.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env).returncode == 0
    assert cfg.read_text() == "# mine\n" and not (box.home / ".config/hx").exists()
    unit = (box.home / ".config/systemd/user/hx.service").read_text()
    assert f"Environment=HX_CONFIG={cfg}" in unit


def test_a_bad_user_name_matters_only_for_a_new_config(box):
    import subprocess as sp
    env = {"HOME": str(box.home), "USER": "first_last", "PATH": f"{box.bin}:/usr/bin:/bin"}
    run = lambda *a: sp.run(["bash", str(SCRIPT), *a], capture_output=True, text=True, env=env)
    assert run().returncode != 0                       # would write [humans.first_last]
    box("--me", "first")                               # config now exists
    assert run().returncode == 0 and run("--uninstall").returncode == 0


def test_switching_modes_replaces_the_skill(box):
    box("--no-service")
    skill = box.home / ".claude/skills/hx-work"
    assert skill.is_symlink()
    box("--no-service", "--no-editable")
    assert skill.is_dir() and not skill.is_symlink()
    box("--no-service")
    assert skill.is_symlink()


def test_a_rejected_actor_changes_nothing(box):
    out = box("--me", "first_last", ok=False)
    assert out.returncode != 0 and "uv tool install" not in box.calls()
    assert not (box.home / ".claude").exists()


def test_uninstall_without_uv_keeps_the_marker(box):
    box("--no-service")
    (box.bin / "uv").unlink()
    box("--uninstall", ok=False)
    marker = box.home / ".local/state/hx-install/tool-installed"
    assert marker.exists()


def test_unit_path_reaches_the_tools_the_checks_found(box, tmp_path):
    import subprocess as sp
    opt = tmp_path / "opt-bin"
    opt.mkdir()
    (box.bin / "herdr").rename(opt / "herdr")
    env = {"HOME": str(box.home), "USER": "alice", "PATH": f"{box.bin}:{opt}:/usr/bin:/bin"}
    assert sp.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env).returncode == 0
    unit = (box.home / ".config/systemd/user/hx.service").read_text()
    path = next(l for l in unit.splitlines() if l.startswith("Environment=PATH="))
    assert str(opt) in path and str(box.bin) in path


def test_relative_hx_config_becomes_absolute(box, tmp_path):
    import subprocess as sp
    work = tmp_path / "work"
    work.mkdir()
    env = {"HOME": str(box.home), "USER": "alice", "PATH": f"{box.bin}:/usr/bin:/bin", "HX_CONFIG": "rel.toml"}
    assert sp.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, cwd=work).returncode == 0
    unit = (box.home / ".config/systemd/user/hx.service").read_text()
    assert f"Environment=HX_CONFIG={work}/rel.toml" in unit and (work / "rel.toml").exists()


def test_uninstall_keeps_a_skill_it_did_not_install(box):
    skills = box.home / ".claude/skills"
    skills.mkdir(parents=True)
    (skills / "hx-work").symlink_to(REPO / "claude/skills/hx-work")
    box()
    box("--uninstall")
    assert (skills / "hx-work").is_symlink()


def test_a_failed_render_keeps_the_old_unit(box, tmp_path):
    import subprocess as sp
    box()
    unit = box.home / ".config/systemd/user/hx.service"
    before = unit.read_text()
    bad = tmp_path / "a b"
    bad.mkdir()
    (bad / "c.toml").write_text("")
    env = {"HOME": str(box.home), "USER": "alice", "PATH": f"{box.bin}:/usr/bin:/bin",
           "HX_CONFIG": str(bad / "c.toml")}
    assert sp.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env).returncode != 0
    assert unit.read_text() == before


def test_uninstall_keeps_a_unit_it_did_not_write(box):
    units = box.home / ".config/systemd/user"
    units.mkdir(parents=True)
    (units / "hx.service").write_text("[Service]\nExecStart=/my/hx watch\n")
    box("--no-service")
    box("--uninstall")
    assert (units / "hx.service").exists()


def test_ampersand_paths_render_literally(box, tmp_path):
    import subprocess as sp
    d = tmp_path / "r&d"
    d.mkdir()
    env = {"HOME": str(box.home), "USER": "alice", "PATH": f"{box.bin}:/usr/bin:/bin", "HX_CONFIG": str(d / "c.toml")}
    assert sp.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env).returncode == 0
    unit = (box.home / ".config/systemd/user/hx.service").read_text()
    assert f"Environment=HX_CONFIG={d}/c.toml" in unit


def test_uninstall_removes_an_unedited_copy_after_an_upgrade(box):
    box("--no-editable", "--no-service")
    skill = box.home / ".claude/skills/hx-work"
    (skill / "SKILL.md").write_text((skill / "SKILL.md").read_text().replace("hx-work", "hx-work", 1) + "\n")
    # simulate an older copy: the installed copy no longer matches the checkout, but its mark does
    import subprocess as sp
    marks = box.home / ".local/state/hx-install"
    h = sp.run(["bash", "-c", f'cd "{skill}" && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum'],
               capture_output=True, text=True).stdout.split()[0]
    (marks / "skill-hx-work").write_text(h + "\n")
    box("--uninstall")
    assert not skill.exists()


def test_first_install_starts_the_watcher_once(box):
    box()
    calls = box.calls()
    assert "enable --now" not in calls and calls.count("restart hx") == 1


def test_a_tool_dir_with_a_space_is_warned_about(box, tmp_path):
    import subprocess as sp
    odd = tmp_path / "agent tools"
    odd.mkdir()
    (box.bin / "herdr").rename(odd / "herdr")
    env = {"HOME": str(box.home), "USER": "alice", "PATH": f"{box.bin}:{odd}:/usr/bin:/bin"}
    out = sp.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)
    assert out.returncode == 0 and "will not find herdr" in out.stderr
    path = next(l for l in (box.home / ".config/systemd/user/hx.service").read_text().splitlines()
                if l.startswith("Environment=PATH="))
    assert "agent" not in path and "tools" not in path


# --- herdr-extension became hx: an install over the old names moves them, never loses them ---

def old_install(box):
    """What a herdr-extension install left behind: its config, state, marks, unit, tool and skill link."""
    h = box.home
    (h / ".config/herdr-extension").mkdir(parents=True)
    (h / ".config/herdr-extension/config.toml").write_text("# mine\n")
    (h / ".local/state/herdr-extension").mkdir(parents=True)
    (h / ".local/state/herdr-extension/state.json").write_text("{}")
    (h / ".local/state/herdr-extension-install").mkdir(parents=True)
    (h / ".local/state/herdr-extension-install/service").touch()
    (h / ".config/systemd/user").mkdir(parents=True)
    (h / ".config/systemd/user/herdr-extension.service").write_text("[Service]\nExecStart=/old/hx watch\n")
    (h / "uv-has-herdr-extension").touch()
    (h / ".claude/skills").mkdir(parents=True)
    (h / ".claude/skills/hx-work").symlink_to(h / "gone/herdr-extension/claude/skills/hx-work")


def test_install_moves_the_old_folders_and_keeps_the_config(box):
    old_install(box)
    box()
    h = box.home
    assert (h / ".config/hx/config.toml").read_text() == "# mine\n"
    assert (h / ".local/state/hx/state.json").read_text() == "{}"
    assert (h / ".local/state/hx-install/service").exists()
    assert not (h / ".config/herdr-extension").exists() and not (h / ".local/state/herdr-extension").exists()
    assert not (h / ".local/state/herdr-extension-install").exists()


def test_install_replaces_the_old_service(box):
    old_install(box)
    box()
    calls = box.calls()
    units = box.home / ".config/systemd/user"
    assert "systemctl --user disable --now herdr-extension" in calls
    assert not (units / "herdr-extension.service").exists() and (units / "hx.service").exists()
    assert calls.index("disable --now herdr-extension") < calls.index("restart hx")


def test_install_leaves_an_old_unit_it_did_not_write_and_says_so(box):
    old_install(box)
    (box.home / ".local/state/herdr-extension-install/service").unlink()
    out = box()
    assert (box.home / ".config/systemd/user/herdr-extension.service").exists()
    assert "disable --now herdr-extension" not in box.calls()
    assert "systemctl --user disable --now herdr-extension" in out.stdout + out.stderr


def test_install_replaces_the_old_tool_without_force(box):
    old_install(box)
    box()
    assert (box.home / "uv-has-hx").exists()
    assert "--force" in next(l for l in box.calls().splitlines() if "uv tool install" in l)


def test_install_replaces_a_dangling_skill_link_without_force(box):
    old_install(box)
    box()
    skill = box.home / ".claude/skills/hx-work"
    assert skill.is_symlink() and skill.resolve() == (REPO / "claude/skills/hx-work").resolve()
    assert not list(skill.parent.glob("hx-work.bak-*"))


def test_dry_run_over_an_old_install_moves_nothing(box):
    old_install(box)
    out = box("--dry-run")
    h = box.home
    assert (h / ".config/herdr-extension/config.toml").exists() and not (h / ".config/hx").exists()
    assert (h / ".config/systemd/user/herdr-extension.service").exists()
    assert "uv tool" not in box.calls().replace("uv tool dir", "").replace("uv tool list", "")
    assert "[dry-run] mv" in out.stdout


def test_install_keeps_both_folders_when_the_new_one_exists(box):
    old_install(box)
    (box.home / ".config/hx").mkdir()
    (box.home / ".config/hx/config.toml").write_text("# new\n")
    out = box()
    assert (box.home / ".config/hx/config.toml").read_text() == "# new\n"
    assert (box.home / ".config/herdr-extension/config.toml").exists()
    assert "herdr-extension" in out.stdout


def test_the_old_service_stops_before_its_folders_move(box):
    old_install(box)
    (box.bin / "systemctl").write_text(STUB + '[[ "$*" == *"disable --now herdr-extension"* && -d $HOME/.local/state/herdr-extension ]] && echo "old-state-still-in-place" >> "$HOME/calls.log"\ntrue\n')
    box()
    assert "old-state-still-in-place" in box.calls()


def test_an_old_mark_counts_when_the_new_marks_folder_exists_too(box):
    old_install(box)
    (box.home / ".local/state/hx-install").mkdir(parents=True)
    box()
    assert "systemctl --user disable --now herdr-extension" in box.calls()
    assert not (box.home / ".config/systemd/user/herdr-extension.service").exists()


def test_a_failed_tool_install_keeps_the_old_tool_and_watcher(box):
    old_install(box)
    (box.home / "uv-fail").touch()
    out = box(ok=False)
    assert out.returncode != 0
    assert (box.home / "uv-has-herdr-extension").exists()
    assert "uv tool uninstall" not in box.calls() and "disable --now" not in box.calls()
    assert (box.home / ".config/systemd/user/herdr-extension.service").exists()


def test_the_old_tool_goes_without_taking_the_hx_link(box):
    old_install(box)
    (box.home / "uv-tools/herdr-extension").mkdir(parents=True)
    (box.home / "uv-tools/herdr-extension/uv-receipt.toml").write_text("")
    box()
    calls = box.calls()
    assert "uv tool uninstall herdr-extension" not in calls          # uv's uninstall deletes the hx link
    assert calls.count("uv tool install") == 1
    assert not (box.home / "uv-tools/herdr-extension").exists()
    assert (box.home / ".local/bin/hx").exists()


@pytest.mark.parametrize("path", [".config/hx", ".local/state/hx", ".local/state/hx-install"])
def test_a_file_where_a_new_folder_goes_stops_the_install_before_any_change(box, path):
    old_install(box)
    (box.home / path).write_text("not a folder")
    out = box(ok=False)
    assert out.returncode != 0 and "not a folder" in out.stderr
    assert "disable --now" not in box.calls() and "uv tool install" not in box.calls()
    assert (box.home / ".config/systemd/user/herdr-extension.service").exists()


@pytest.mark.parametrize("path", [".config/hx", ".local/state/hx", ".claude/skills", ".config/systemd/user"])
def test_a_dangling_link_or_file_where_a_folder_goes_stops_the_install_first(box, path):
    old_install(box)
    target = box.home / path
    if target.is_dir() and not target.is_symlink():
        import shutil
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(box.home / "nowhere")
    out = box(ok=False)
    assert out.returncode != 0 and "not a folder" in out.stderr
    assert "uv tool install" not in box.calls() and "disable --now" not in box.calls()


def test_a_dangling_config_link_stops_the_install_first(box):
    old_install(box)
    import shutil
    shutil.rmtree(box.home / ".config/herdr-extension")
    (box.home / ".config/hx").mkdir()
    (box.home / ".config/hx/config.toml").symlink_to(box.home / "gone/dir/config.toml")
    out = box(ok=False)
    assert out.returncode != 0 and "config.toml" in out.stderr and "disable --now" not in box.calls()


def test_a_bad_hx_path_stops_the_install_first(box):
    old_install(box)
    stub = (box.bin / "uv").read_text().replace('echo "$HOME/.local/bin"', 'echo "$HOME/my bin"')
    (box.bin / "uv").write_text(stub)
    out = box(ok=False)
    assert out.returncode != 0 and "quoted" in out.stderr
    assert "uv tool install" not in box.calls() and "disable --now" not in box.calls()


def test_a_dangling_default_config_folder_stops_the_install_even_with_hx_config(box, tmp_path):
    import subprocess as sp
    old_install(box)
    (box.home / ".config/hx").symlink_to(box.home / "nowhere")
    cfg = tmp_path / "mine.toml"
    cfg.write_text("# mine\n")
    env = {"HOME": str(box.home), "USER": "alice", "PATH": f"{box.bin}:/usr/bin:/bin", "HX_CONFIG": str(cfg)}
    out = sp.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)
    assert out.returncode != 0 and "not a folder" in out.stderr and "disable --now" not in box.calls()


def test_install_links_the_herdr_plugin(box):
    box()
    assert f"herdr plugin link {REPO}/herdr-plugin" in box.calls()


def test_install_skips_the_plugin_link_when_hx_is_linked(box):
    (box.bin / "herdr").write_text(STUB + '[[ "$1 $2" == "plugin list" ]] && echo \'{"plugins":[{"plugin_id":"hx"}]}\'\nexit 0\n')
    out = box()
    assert "plugin link" not in box.calls() and "already linked" in out.stdout


def test_install_without_a_plugin_link_still_finishes(box):
    (box.bin / "herdr").write_text(STUB + '[[ "$1 $2" == "plugin link" ]] && exit 1\nexit 0\n')
    out = box()
    assert "could not link" in out.stdout and "next steps" in out.stdout


def test_dry_run_links_no_plugin(box):
    box("--dry-run")
    assert "plugin link" not in box.calls()


def test_next_steps_say_the_sidebar_is_set_and_how_to_skip_it(box):
    out = box().stdout
    assert "Agents panel now shows the needs-you list" in out and "--no-sidebar" in out


# --- extras: claude-worker, launch guard and hooks, on by default ---

def hook_commands(home):
    s = home / ".claude/settings.json"
    if not s.exists():
        return set()
    hooks = json.loads(s.read_text()).get("hooks", {})
    return {h["command"] for entries in hooks.values() for e in entries for h in e["hooks"]}


ALL_HOOKS = {f"{REPO.resolve()}/claude/hooks/mem-gate.sh", f"{REPO.resolve()}/claude/hooks/checkout-freshness.sh"}


def test_on_linux_every_extra_is_on_by_default(box):
    box()
    for b in ("launch-guard.sh", "claude-worker"):
        assert (box.home / ".local/bin" / b).resolve() == (REPO / "scripts" / b).resolve()
    assert hook_commands(box.home) == ALL_HOOKS


def test_off_linux_the_proc_readers_are_skipped_unless_named(box):
    box.env["FAKE_UNAME"] = "Darwin"
    box()
    assert (box.home / ".local/bin/claude-worker").is_symlink()
    assert not (box.home / ".local/bin/launch-guard.sh").exists()
    assert hook_commands(box.home) == {f"{REPO.resolve()}/claude/hooks/checkout-freshness.sh"}
    box("--launch-guard", "--mem-gate")
    assert (box.home / ".local/bin/launch-guard.sh").is_symlink() and hook_commands(box.home) == ALL_HOOKS


def test_each_extra_can_be_skipped(box):
    box("--no-launch-guard", "--no-claude-worker", "--no-mem-gate", "--no-freshness-hook")
    assert not list((box.home / ".local/bin").glob("*-*")) and not hook_commands(box.home)


def test_someone_elses_worker_is_left_alone_unless_named_or_forced(box):
    worker = box.home / ".local/bin/claude-worker"
    worker.parent.mkdir(parents=True)
    worker.write_text("#!/bin/bash\n# my own worker\n")
    out = box()
    assert "claude-worker: not ours" in out.stdout and "my own worker" in worker.read_text()
    assert (box.home / ".local/bin/launch-guard.sh").is_symlink()
    out = box("--claude-worker", ok=False)
    assert out.returncode != 0 and "differ from the repo copy" in out.stderr
    box("--force")
    assert worker.is_symlink() and list(worker.parent.glob("claude-worker.bak-*"))


def test_a_hook_the_box_already_runs_from_elsewhere_is_not_added_twice(box):
    s = box.home / ".claude/settings.json"
    s.parent.mkdir(parents=True)
    mine = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/x/hooks/mem-gate.sh"}]}]}}
    s.write_text(json.dumps(mine))
    out = box()
    assert "already runs a mem-gate.sh from elsewhere" in out.stdout
    assert hook_commands(box.home) == {"/x/hooks/mem-gate.sh", f"{REPO.resolve()}/claude/hooks/checkout-freshness.sh"}
    box()   # hx's own entry is not "elsewhere": a second run keeps it, once
    assert hook_commands(box.home) == {"/x/hooks/mem-gate.sh", f"{REPO.resolve()}/claude/hooks/checkout-freshness.sh"}


def test_a_copy_an_earlier_release_installed_is_replaced(box):
    box("--no-editable")
    worker = box.home / ".local/bin/claude-worker"
    worker.write_text("#!/bin/bash\n# an older release\n")    # as an earlier install left it, marker and all
    import hashlib
    (box.home / ".local/state/hx-install/bin-claude-worker").write_text(hashlib.sha256(worker.read_bytes()).hexdigest() + "\n")
    box("--no-editable")
    assert worker.read_text() == (REPO / "scripts/claude-worker").read_text()


def test_extras_install_and_uninstall(box):
    out = box("--launch-guard", "--mem-gate", "--freshness-hook")
    guard = box.home / ".local/bin/launch-guard.sh"
    assert guard.is_symlink() and guard.resolve() == (REPO / "scripts/launch-guard.sh").resolve()
    assert hook_commands(box.home) == {f"{REPO.resolve()}/claude/hooks/mem-gate.sh",
                                       f"{REPO.resolve()}/claude/hooks/checkout-freshness.sh"}
    assert "Restart Claude Code sessions so they load the new hooks" in out.stdout
    again = box("--launch-guard", "--mem-gate", "--freshness-hook")
    assert "already installed (linked" in again.stdout and "already up to date" in again.stdout
    box("--uninstall")
    assert not guard.exists() and not guard.is_symlink()
    assert not hook_commands(box.home)


def test_claude_worker_installs_beside_the_guard_and_uninstalls(box):
    out = box("--claude-worker", "--no-launch-guard")
    worker = box.home / ".local/bin/claude-worker"
    assert worker.is_symlink() and worker.resolve() == (REPO / "scripts/claude-worker").resolve()
    assert not (box.home / ".local/bin/launch-guard.sh").exists()
    assert "Solo sessions now run under claude-worker" in out.stdout
    box("--claude-worker", "--launch-guard")
    assert (box.home / ".local/bin/launch-guard.sh").is_symlink()
    box("--uninstall")
    assert not worker.is_symlink() and not (box.home / ".local/bin/launch-guard.sh").is_symlink()


def test_no_editable_copies_the_guard_and_uninstall_keeps_an_edited_copy(box):
    box("--launch-guard", "--no-editable")
    guard = box.home / ".local/bin/launch-guard.sh"
    assert not guard.is_symlink() and guard.read_text() == (REPO / "scripts/launch-guard.sh").read_text()
    guard.write_text(guard.read_text() + "# mine\n")
    out = box("--uninstall")
    assert "edited since install; leaving it alone" in out.stdout and guard.exists()


def test_a_different_guard_is_refused_without_force_and_kept_with_it(box):
    guard = box.home / ".local/bin/launch-guard.sh"
    guard.parent.mkdir(parents=True)
    guard.write_text("#!/bin/bash\n# my own guard\n")
    out = box("--launch-guard", ok=False)
    assert out.returncode != 0 and "differ from the repo copy" in out.stderr
    assert "uv tool install" not in box.calls() and "my own guard" in guard.read_text()
    box("--launch-guard", "--force")
    assert guard.is_symlink() and list(guard.parent.glob("launch-guard.sh.bak-*"))
    box("--uninstall")
    assert not guard.is_symlink() and not guard.exists()


def test_uninstall_leaves_a_guard_it_did_not_install(box):
    guard = box.home / ".local/bin/launch-guard.sh"
    guard.parent.mkdir(parents=True)
    guard.write_text("#!/bin/bash\n")
    assert "launch-guard.sh: not ours" in box().stdout
    assert "not installed by this script; leaving it alone" in box("--uninstall").stdout and guard.exists()


def test_bad_settings_json_stops_the_hooks_before_any_change(box):
    s = box.home / ".claude/settings.json"
    s.parent.mkdir(parents=True)
    s.write_text("{oops")
    out = box("--mem-gate", ok=False)
    assert out.returncode != 0 and "cannot take the hooks" in out.stderr
    assert "uv tool install" not in box.calls() and s.read_text() == "{oops"


def test_dry_run_extras_change_nothing(box):
    out = box("--dry-run", "--launch-guard", "--mem-gate")
    assert "would change" in out.stdout and "[dry-run] ln -s" in out.stdout
    assert not (box.home / ".local/bin/launch-guard.sh").exists() and not (box.home / ".claude/settings.json").exists()


def test_uninstall_removes_only_hx_hooks(box):
    s = box.home / ".claude/settings.json"
    s.parent.mkdir(parents=True)
    mine = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/x/mine.sh"}]}]}}
    s.write_text(json.dumps(mine))
    box("--mem-gate")
    box("--uninstall")
    assert json.loads(s.read_text()) == mine


def test_on_macos_the_watcher_is_a_launchd_agent(box, monkeypatch):
    (box.bin / "launchctl").write_text(STUB); (box.bin / "launchctl").chmod(0o755)
    env = {"HOME": str(box.home), "USER": "alice", "PATH": f"{box.bin}:/usr/bin:/bin", "HX_SERVICE_MANAGER": "launchd"}
    out = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    agent = box.home / "Library/LaunchAgents/com.hx.watch.plist"
    import plistlib
    d = plistlib.loads(agent.read_bytes())
    assert d["Label"] == "com.hx.watch" and d["ProgramArguments"] == [f"{box.home}/.local/bin/hx", "watch"]
    assert d["KeepAlive"] is True and d["EnvironmentVariables"]["HX_CONFIG"] == f"{box.home}/.config/hx/config.toml"
    assert d["StandardOutPath"] == f"{box.home}/.local/state/hx/watch.log"
    uid = os.getuid()
    assert f"launchctl bootstrap gui/{uid} {agent}" in box.calls() and "systemctl" not in box.calls()
    assert not (box.home / ".config/systemd/user/hx.service").exists()
    out = subprocess.run(["bash", str(SCRIPT), "--uninstall"], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    assert not agent.exists() and f"launchctl bootout gui/{uid}/com.hx.watch" in box.calls()
