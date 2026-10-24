"""The README's quickstart and scripts/sync-contract.sh, run against throwaway stores and copies."""
import os
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).parent.parent


def test_quickstart_creates_a_work_item(trk_home, tmp_path):
    """Step 2 of the quickstart, run as written, makes an item that `hx start` can take."""
    blocks = re.findall(r"```bash\n(.*?)```", (REPO / "README.md").read_text(), re.S)
    block = next(b for b in blocks if "trk new work-item" in b)
    app = tmp_path / "myapp"
    subprocess.run(["git", "init", "-q", str(app)], check=True)
    work = tmp_path / "cwd"
    work.mkdir()
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", block.replace("~/src/myapp", str(app))],
                         cwd=work, capture_output=True, text=True,
                         env={**os.environ, "TRK_ACTOR": "michael"})   # as a person's shell: trk wants an actor from an agent session
    assert out.returncode == 0, out.stdout + out.stderr
    assert '"form": "self-contained"' in out.stdout


def fake_repo(tmp_path: Path) -> Path:
    fake = tmp_path / "hx"
    (fake / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts/sync-contract.sh", fake / "scripts")
    shutil.copytree(REPO / "src/hx/contract", fake / "src/hx/contract")
    return fake


def test_sync_contract_keeps_the_schemas_when_the_source_is_empty(tmp_path):
    fake = fake_repo(tmp_path)
    before = sorted(p.name for p in (fake / "src/hx/contract").glob("*.schema.json"))
    (tmp_path / "trk/contract").mkdir(parents=True)
    out = subprocess.run(["bash", str(fake / "scripts/sync-contract.sh"), str(tmp_path / "trk")],
                         capture_output=True, text=True)
    assert out.returncode != 0
    assert sorted(p.name for p in (fake / "src/hx/contract").glob("*.schema.json")) == before


def test_sync_contract_keeps_the_schemas_when_the_source_lacks_one(tmp_path):
    """A source without a schema hx calls would leave hx unable to check that reply."""
    fake = fake_repo(tmp_path)
    src = tmp_path / "trk/contract"
    shutil.copytree(REPO / "src/hx/contract", src)
    victim = src / "show.schema.json"
    victim.unlink()
    out = subprocess.run(["bash", str(fake / "scripts/sync-contract.sh"), str(tmp_path / "trk")],
                         capture_output=True, text=True)
    assert out.returncode != 0
    assert (fake / "src/hx/contract" / victim.name).exists()
    assert victim.name in out.stderr


def test_sync_contract_replaces_the_schemas(tmp_path):
    fake = fake_repo(tmp_path)
    src = tmp_path / "trk/contract"
    shutil.copytree(REPO / "src/hx/contract", src)
    (src / "extra.schema.json").write_text("{}\n")
    (fake / "src/hx/contract/stale.schema.json").write_text("{}\n")
    out = subprocess.run(["bash", str(fake / "scripts/sync-contract.sh"), str(tmp_path / "trk")],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    names = {p.name for p in (fake / "src/hx/contract").glob("*.schema.json")}
    assert "extra.schema.json" in names and "stale.schema.json" not in names
