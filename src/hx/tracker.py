"""The only door to trk: run `trk … --json`, check the reply against the tracker's contract."""
import functools
import json
import os
import subprocess
from pathlib import Path

import jsonschema
from referencing import Registry, Resource

DEFAULT_CONTRACT = Path(__file__).parent / "contract"   # a copy from the trk version hx is tested with


class TrkError(Exception):
    pass


@functools.cache
def _validators(contract_dir: str) -> dict[str, jsonschema.Draft202012Validator]:
    schemas = {p.name.removesuffix(".schema.json"): json.loads(p.read_text())
               for p in Path(contract_dir).glob("*.schema.json")}
    registry = Registry().with_resources(
        [(s["$id"], Resource.from_contents(s)) for s in schemas.values()])
    return {name: jsonschema.Draft202012Validator(s, registry=registry)
            for name, s in schemas.items()}


class Trk:
    def __init__(self, home: str | None = None, bin: str = "trk", contract_dir: str | None = None):
        self.home = home
        self.bin = bin
        self.contract_dir = contract_dir or os.environ.get("HX_TRK_CONTRACT", str(DEFAULT_CONTRACT))

    def _env(self) -> dict:
        env = dict(os.environ)
        if self.home:
            env["TRK_HOME"] = self.home
        return env

    def call(self, contract: str, *args: str) -> dict:
        env = self._env()
        try:
            out = subprocess.run([self.bin, *args, "--json"], capture_output=True, text=True,
                                 env=env, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise TrkError(f"trk {args[0]}: {e}") from e
        try:
            data = json.loads(out.stdout)
        except json.JSONDecodeError as e:
            raise TrkError(f"trk {args[0]}: not JSON (exit {out.returncode})") from e
        if out.returncode != 0:
            raise TrkError(f"trk {args[0]}: {data.get('error', data)}")
        return self._valid(contract, args[0], data)

    def _check(self, contract: str, verb: str, stdout: str) -> dict:
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise TrkError(f"trk {verb}: not JSON") from e
        return self._valid(contract, verb, data)

    def _valid(self, contract: str, verb: str, data: dict) -> dict:
        validator = _validators(self.contract_dir).get(contract)
        if validator is None:            # a [trk] contract path that moved, or never held this schema
            raise TrkError(f"trk {verb}: reply breaks contract {contract}: "
                           f"no {contract}.schema.json in {self.contract_dir}")
        errors = list(validator.iter_errors(data))
        if errors:
            raise TrkError(f"trk {verb}: reply breaks contract {contract}: {errors[0].message}")
        return data

    def changes(self, since: int, limit: int | None = None) -> dict:
        extra = ("--limit", str(limit)) if limit else ()
        return self.call("changes", "changes", "--since", str(since), *extra)

    def waiting(self) -> dict:
        return self.call("waiting", "waiting", "--all")

    def claims(self) -> dict:
        return self.call("claims", "claims")

    def show(self, item_id: str) -> dict:
        return self.call("show", "show", item_id)

    def where(self, path: str) -> dict | None:
        """The trk items that record the tree holding `path`, newest first; None when none does."""
        try:
            out = subprocess.run([self.bin, "where", path, "--json"], capture_output=True, text=True,
                                 env=self._env(), timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise TrkError(f"trk where: {e}") from e
        if out.returncode != 0:          # a refusal is JSON on stderr, after a store line
            try:
                err = next((json.loads(l) for l in out.stderr.splitlines() if l.startswith("{")), {})
            except ValueError:
                err = {}
            err = err if isinstance(err, dict) else {}
            if (err.get("error") or {}).get("rule") == "not-found":
                return None
            raise TrkError(f"trk where: {err.get('error') or out.stderr.strip()[:200]}")
        return self._check("where", "where", out.stdout)

    def worktree_clear(self, item: str) -> None:
        self._write("worktree", "clear", item)

    def release(self, session: str) -> None:
        """The plugin's one write in this slice (ADR 0002): end every claim a session holds."""
        self._write("claim", "release", "--session", session)

    def _write(self, *args: str) -> None:
        cmd = [self.bin, *args, "--as", "herdr", "--json"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, env=self._env(), timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise TrkError(f"trk {' '.join(args[:2])}: {e}") from e
        if out.returncode != 0:
            raise TrkError(f"trk {' '.join(args)}: {(out.stdout or out.stderr).strip()[:200]}")
