#!/usr/bin/env python3
"""Clone each candidate from selection.json, install it, smoke-test it, and
write runs/<TODAY>-<SLOT>.md for the first one that works.

Reads selection.json (written by select-project.py). Tries up to 3 candidates
in order. Success = install OK and at least one smoke probe exit 0.

Env:
  TODAY  date YYYY-MM-DD (required)
  SLOT is read from selection.json (two-digit slot 01..10).
No tokens are used here on purpose: the runner checks out without persisted
credentials and this step runs with no token in its environment.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

INSTALL_TIMEOUT = 300
PROBE_TIMEOUT = 120
LOG_LIMIT = 3000


def module_name(full_name: str) -> str | None:
    repo = full_name.split("/")[-1].lower().replace("-", "_")
    repo = repo.split(".")[0]
    return repo if repo.isidentifier() else None


def run_cmd(cmd, cwd, timeout, log, label):
    """Run cmd, append labelled output to log, return (exit_code, output)."""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace",
        )
        out = proc.stdout or ""
        log.append(f"$ {shlex.join(cmd)}\n(exit {proc.returncode})\n{out}")
        return proc.returncode, out
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        log.append(f"$ {shlex.join(cmd)}\n(TIMEOUT after {timeout}s)\n{out}")
        return None, out
    except Exception as exc:
        log.append(f"$ {shlex.join(cmd)}\n(error: {exc})\n")
        return None, ""


def console_scripts(repo_dir):
    """Console script names from pyproject.toml [project.scripts], if any."""
    path = os.path.join(repo_dir, "pyproject.toml")
    if not os.path.isfile(path):
        return []
    try:
        import tomllib
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        return list(data.get("project", {}).get("scripts", {}).keys())
    except Exception:
        # Fallback: crude regex over the [project.scripts] table.
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            m = re.search(r"\[project\.scripts\](.*?)(?:\n\[|\Z)", text, re.S)
            if not m:
                return []
            return re.findall(r"^([A-Za-z0-9_.-]+)\s*=", m.group(1), re.M)
        except Exception:
            return []


def package_json(repo_dir):
    path = os.path.join(repo_dir, "package.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def try_candidate(cand, today, work_root):
    """Returns (ok, problems, probe_log) for one candidate."""
    full_name = cand["full_name"]
    log: list[str] = []
    problems: list[str] = []
    repo_dir = os.path.join(work_root, "repo")
    rc, _ = run_cmd(
        ["git", "clone", "--depth", "1", f"https://github.com/{full_name}.git", repo_dir],
        cwd=work_root, timeout=180, log=log, label="clone",
    )
    if rc != 0:
        return False, [f"git clone failed (exit {rc})"], log

    has_pkg = os.path.isfile(os.path.join(repo_dir, "package.json"))
    has_go = os.path.isfile(os.path.join(repo_dir, "go.mod"))
    has_py = any(os.path.isfile(os.path.join(repo_dir, f))
                 for f in ("requirements.txt", "pyproject.toml", "setup.py"))

    stack = "node" if has_pkg else ("go" if has_go else ("python" if has_py else None))
    if stack is None:
        return False, ["no recognizable stack (no package.json / go.mod / Python files)"], log

    # ---- install ----
    if stack == "python":
        venv = os.path.join(repo_dir, ".venv")
        rc, _ = run_cmd(["python3", "-m", "venv", venv], cwd=repo_dir,
                        timeout=120, log=log, label="venv")
        if rc != 0:
            return False, ["python venv creation failed"], log
        pip = os.path.join(venv, "bin", "pip")
        py = os.path.join(venv, "bin", "python")
        req = os.path.join(repo_dir, "requirements.txt")
        if os.path.isfile(req):
            rc, _ = run_cmd([pip, "install", "-r", "requirements.txt"], cwd=repo_dir,
                            timeout=INSTALL_TIMEOUT, log=log, label="pip install")
            if rc != 0:
                return False, ["pip install -r requirements.txt failed"], log
        elif os.path.isfile(os.path.join(repo_dir, "pyproject.toml")) or \
                os.path.isfile(os.path.join(repo_dir, "setup.py")):
            rc, _ = run_cmd([pip, "install", "-e", "."], cwd=repo_dir,
                            timeout=INSTALL_TIMEOUT, log=log, label="pip install -e")
            if rc != 0:
                rc2, _ = run_cmd([pip, "install", "."], cwd=repo_dir,
                                 timeout=INSTALL_TIMEOUT, log=log, label="pip install .")
                if rc2 != 0:
                    return False, ["pip install failed (both -e . and .)"], log
    elif stack == "node":
        rc, _ = run_cmd(["npm", "ci"], cwd=repo_dir, timeout=INSTALL_TIMEOUT, log=log, label="npm ci")
        if rc != 0:
            rc, _ = run_cmd(["npm", "install"], cwd=repo_dir, timeout=INSTALL_TIMEOUT, log=log, label="npm install")
            if rc != 0:
                return False, ["npm install failed (both ci and install)"], log
    elif stack == "go":
        run_cmd(["go", "mod", "download"], cwd=repo_dir, timeout=INSTALL_TIMEOUT, log=log, label="go mod download")

    # ---- smoke probes (at least one must exit 0) ----
    probe_ok = False
    if stack == "python":
        venv = os.path.join(repo_dir, ".venv")
        py = os.path.join(venv, "bin", "python")
        scripts = console_scripts(repo_dir)
        probed = False
        for script in scripts:
            probed = True
            rc, _ = run_cmd([os.path.join(venv, "bin", script), "--help"], cwd=repo_dir,
                            timeout=60, log=log, label="console script --help")
            if rc == 0:
                probe_ok = True
                break
            problems.append(f"console script {script} --help exit {rc}")
        if not probe_ok and not probed:
            mod = module_name(full_name)
            if mod:
                rc, _ = run_cmd([py, "-m", mod, "--help"], cwd=repo_dir,
                                timeout=60, log=log, label="python -m --help")
                if rc == 0:
                    probe_ok = True
                else:
                    problems.append(f"python -m {mod} --help exit {rc}")
                    rc2, _ = run_cmd([py, "-c", f"import {mod}"], cwd=repo_dir,
                                     timeout=60, log=log, label="import")
                    if rc2 == 0:
                        probe_ok = True
                    else:
                        problems.append(f"import {mod} exit {rc2}")
            else:
                problems.append("could not derive a valid Python module name")
    elif stack == "node":
        pkg = package_json(repo_dir)
        scripts = pkg.get("scripts", {}) or {}
        if "test" in scripts:
            rc, _ = run_cmd(["npm", "test"], cwd=repo_dir, timeout=PROBE_TIMEOUT, log=log, label="npm test")
            if rc == 0:
                probe_ok = True
            else:
                problems.append(f"npm test exit {rc}")
        if not probe_ok:
            bins = pkg.get("bin") or {}
            if isinstance(bins, str):
                bins = {os.path.basename(bins): bins}
            probed = False
            for name in bins:
                probed = True
                rc, _ = run_cmd(["npx", "--no-install", name, "--help"], cwd=repo_dir,
                                timeout=60, log=log, label="bin --help")
                if rc == 0:
                    probe_ok = True
                    break
                problems.append(f"bin {name} --help exit {rc}")
            if not probe_ok and not probed:
                rc, _ = run_cmd(["node", "-e", "require('./')"], cwd=repo_dir,
                                timeout=60, log=log, label="require")
                if rc == 0:
                    probe_ok = True
                else:
                    problems.append(f"node require('./') exit {rc}")
    elif stack == "go":
        rc, _ = run_cmd(["go", "build", "./..."], cwd=repo_dir, timeout=INSTALL_TIMEOUT, log=log, label="go build")
        if rc == 0:
            probe_ok = True
        else:
            problems.append(f"go build ./... exit {rc}")

    if not probe_ok:
        return False, problems, log
    return True, problems, log


def write_log(today, slot, ptype, cand, problems, log_text):
    os.makedirs("runs", exist_ok=True)
    stars = cand.get("stars", 0)
    lic = cand.get("license", "Unknown")
    desc = cand.get("description") or "(no description)"
    problems_text = "; ".join(problems) if problems else "无"
    body = f"""# {today} run log (slot {slot})

- **Project:** [{cand['full_name']}]({cand['html_url']})
- **Type:** {ptype}
- **Version:** {stars} stars, {lic} license
- **What it does:** {desc}
- **Ran on:** GitHub Actions (ubuntu-latest)
- **Ran successfully:** 是
- **Key output:**
  ```
{log_text}
  ```
- **Problems:** {problems_text}
- **Safety check:** {stars} stars, {lic} license; 代码在一次性 runner 中运行，无凭证：checkout 未持久化凭证，运行步骤无 token
- **Cleanup:** 一次性 runner，运行结束即销毁，无残留
"""
    with open(f"runs/{today}-{slot}.md", "w", encoding="utf-8") as fh:
        fh.write(body)
    with open("run-meta.json", "w", encoding="utf-8") as fh:
        json.dump({"full_name": cand["full_name"], "type": ptype, "stars": stars},
                  fh, ensure_ascii=False, indent=2)


def main() -> None:
    today = os.environ.get("TODAY", "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        print("TODAY env var must be set to YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)
    try:
        with open("selection.json", encoding="utf-8") as fh:
            selection = json.load(fh)
    except Exception as exc:
        print(f"cannot read selection.json: {exc}", file=sys.stderr)
        sys.exit(1)
    ptype = selection.get("type", "")
    candidates = selection.get("candidates", [])[:3]
    if not candidates:
        print("selection.json has no candidates", file=sys.stderr)
        sys.exit(1)

    failures = []
    for cand in candidates:
        print(f"trying {cand['full_name']} ...")
        with tempfile.TemporaryDirectory(prefix="daily-run-") as work_root:
            ok, problems, log = try_candidate(cand, today, work_root)
        if ok:
            log_text = "\n".join(log)
            if len(log_text) > LOG_LIMIT:
                log_text = log_text[:LOG_LIMIT] + "\n... (truncated)"
            write_log(today, selection.get("slot", "01"), ptype, cand, problems, log_text)
            print(f"SUCCESS: {cand['full_name']} ({ptype})")
            if problems:
                print(f"  notes: {'; '.join(problems)}")
            return
        failures.append((cand["full_name"], problems))
        print(f"  FAILED: {cand['full_name']}: {'; '.join(problems) or 'no probe passed'}")

    print("All candidates failed:", file=sys.stderr)
    for name, problems in failures:
        print(f"  - {name}: {'; '.join(problems) or 'no probe passed'}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
