#!/usr/bin/env python3
"""Small dependency-free worker uploaded into a Codespace by sandbox.py."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}


def write_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    tmp.write_text(json.dumps(value))
    os.replace(tmp, path)


def memory_total_bytes(path=Path("/proc/meminfo")):
    try:
        for line in Path(path).read_text().splitlines():
            fields = line.split()
            if fields and fields[0] == "MemTotal:" and len(fields) == 3 and fields[2] == "kB":
                value = int(fields[1]) * 1024
                return value if value > 0 else None
    except (OSError, ValueError):
        pass
    return None


def cgroup_memory_limit(root=Path("/sys/fs/cgroup"), membership=Path("/proc/self/cgroup")):
    root = Path(root)
    paths = {root / "memory.max", root / "memory" / "memory.limit_in_bytes",
             root / "memory.limit_in_bytes"}
    try:
        for line in Path(membership).read_text().splitlines():
            _, controllers, relative = line.split(":", 2)
            if not controllers:
                base, filename = root, "memory.max"
            elif "memory" in controllers.split(","):
                base, filename = root / "memory", "memory.limit_in_bytes"
            else:
                continue
            # Include ancestor limits: a parent can constrain an unlimited child.
            directory = base / relative.lstrip("/")
            if ".." in directory.parts:
                continue
            while True:
                paths.add(directory / filename)
                if directory == base:
                    break
                directory = directory.parent
    except (OSError, ValueError):
        pass
    limits = []
    for path in paths:
        try:
            value = int(path.read_text().strip())
            # v2 uses 'max'; v1 uses a page-aligned LONG_MAX for unlimited.
            if value >= 0 and (path.name != "memory.limit_in_bytes" or value < 1 << 60):
                limits.append(value)
        except (OSError, ValueError):
            pass
    return min(limits) if limits else None


def capture_runtime_resources():
    cpu_count = os.cpu_count()
    try:
        affinity = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = None
    physical = memory_total_bytes()
    limit = cgroup_memory_limit()
    return {"captured_at": time.time(), "cpu_count": cpu_count,
            "available_cpus": len(affinity) if affinity is not None else cpu_count,
            "cpu_affinity": affinity, "memory_total_bytes": physical,
            "cgroup_memory_limit_bytes": limit,
            "effective_memory_bytes": min(physical, limit) if physical is not None and limit is not None else physical}


class ResourceMismatch(Exception):
    pass


def check_resources(requirements, resources):
    if requirements is None:
        return
    if not isinstance(requirements, dict):
        raise ResourceMismatch("resource_requirements must be an object")
    for key, actual_key, exact in (("cpus", "available_cpus", True),
                                   ("min_memory_bytes", "effective_memory_bytes", False)):
        requested = requirements.get(key)
        if requested is None:
            continue
        if type(requested) is not int or requested <= 0:
            raise ResourceMismatch("resource_requirements.{} must be a positive integer".format(key))
        actual = resources.get(actual_key)
        if type(actual) is not int or (actual != requested if exact else actual < requested):
            raise ResourceMismatch("requires {} {} {}; runtime {} is {}. Select a compatible machine or adjust the requirement."
                                   .format(key, "==" if exact else ">=", requested, actual_key,
                                           actual if actual is not None else "unavailable"))
    # The API machine name is checked by the controller, not observable here.


@contextmanager
def worker_timing(job, state, phase):
    started = time.monotonic()
    record = {"phase": phase, "started_at": time.time(), "finished_at": None,
              "duration_seconds": None, "outcome": None}
    state.setdefault("worker_timings", []).append(record)
    write_json(job / "status.json", state)
    try:
        yield record
    except BaseException as error:
        record["outcome"] = ("cancelled" if isinstance(error, Cancelled) else
                             "timed_out" if isinstance(error, subprocess.TimeoutExpired) else "failed")
        raise
    else:
        record["outcome"] = record["outcome"] or "succeeded"
    finally:
        record.update(finished_at=time.time(), duration_seconds=max(0, time.monotonic() - started))
        write_json(job / "status.json", state)


def task_phases(job):
    path = job / "task-phases.json"
    if not path.exists():
        return []
    phases = json.loads(path.read_text())
    if not isinstance(phases, list):
        raise ValueError("task phases must be an array")
    for phase in phases:
        if not isinstance(phase, dict) or not isinstance(phase.get("phase"), str):
            raise ValueError("task phase requires a phase string")
        for key in ("started_at", "started_monotonic", "ended_at", "duration_seconds"):
            value = phase.get(key)
            if value is None and key in ("ended_at", "duration_seconds"):
                continue
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("invalid task phase " + key)
    return phases


def end_task_phase(phase, now, monotonic, outcome):
    phase.update(ended_at=now, duration_seconds=max(0, monotonic - phase["started_monotonic"]),
                 outcome=outcome)


def update_progress(job, value):
    if not isinstance(value, dict) or not isinstance(value.get("phase"), str):
        raise ValueError("progress requires a phase string")
    with (job / "progress.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        phases = task_phases(job)
        now, monotonic = time.time(), time.monotonic()
        if not phases or phases[-1].get("ended_at") is not None or phases[-1]["phase"] != value["phase"]:
            if phases and phases[-1].get("ended_at") is None:
                end_task_phase(phases[-1], now, monotonic, "completed")
            phases.append({"phase": value["phase"], "started_at": now, "started_monotonic": monotonic,
                           "ended_at": None, "duration_seconds": None, "outcome": None})
        value = dict(value, started_at=phases[-1]["started_at"], updated_at=now,
                     elapsed_seconds=max(0, monotonic - phases[-1]["started_monotonic"]))
        write_json(job / "task-phases.json", phases)
        write_json(job / "progress.json", value)
        with (job / "progress-events.jsonl").open("a") as events:
            events.write(json.dumps(value) + "\n")
    return value


def close_task_phase(job, outcome):
    with (job / "progress.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        phases = task_phases(job)
        if phases and phases[-1].get("ended_at") is None:
            end_task_phase(phases[-1], time.time(), time.monotonic(), outcome)
            write_json(job / "task-phases.json", phases)


def identity(pid):
    try:
        # Fields after the final ')' start at stat field 3; starttime is field 22.
        stat = Path("/proc/{}/stat".format(pid)).read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return boot_id + ":" + stat[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def inspect(job):
    path = job / "status.json"
    if not path.exists():
        return {"status": "unknown", "reason": "missing_status"}
    state = json.loads(path.read_text())
    if state["status"] not in TERMINAL:
        pid = state.get("worker_pid")
        if pid and identity(pid) != state.get("worker_identity"):
            state.update(status="interrupted", reason="worker_disappeared", finished_at=time.time())
            write_json(path, state)
        elif not pid and time.time() - state.get("started_at", 0) > 60:
            state.update(status="interrupted", reason="worker_never_started", finished_at=time.time())
            write_json(path, state)
    progress = job / "progress.json"
    if progress.exists():
        try:
            value = json.loads(progress.read_text())
            if not isinstance(value, dict) or not isinstance(value.get("phase"), str):
                raise ValueError("progress requires a phase string")
            state["progress"] = value
        except (ValueError, OSError, OverflowError, RecursionError) as error:
            state["progress_error"] = str(error)
    try:
        state["task_phases"] = task_phases(job)
    except (ValueError, OSError, OverflowError, RecursionError) as error:
        state["task_phases_error"] = str(error)
    transport = job / "transport-timings.json"
    if transport.exists():
        try:
            value = json.loads(transport.read_text())
            if not isinstance(value, list):
                raise ValueError("transport timings must be an array")
            state["transport_timings"] = value
        except (ValueError, OSError, RecursionError) as error:
            state["transport_timings_error"] = str(error)
    if state.get("started_at"):
        state["elapsed_seconds"] = round(max(0, state.get("finished_at", time.time()) - state["started_at"]), 1)
    return state


def unpack(job):
    destination = job / "source"
    destination.mkdir(exist_ok=True)
    archive = job / "source.tar.gz"
    if archive.exists():
        # The controller produces regular files only. Reject links and traversal,
        # including archives supplied independently of the controller.
        with tarfile.open(archive) as src:
            members = src.getmembers()
            for member in members:
                target = (destination / member.name).resolve()
                if not member.isfile() or destination.resolve() not in target.parents:
                    raise ValueError("unsafe source archive member: " + member.name)
            for member in members:
                target = destination / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with src.extractfile(member) as stream, target.open("wb") as out:
                    shutil.copyfileobj(stream, out)
                target.chmod(member.mode & 0o777)


def fetch_source(job, source, timeout):
    """Checkout an immutable Git commit in a fresh job directory, never a host checkout."""
    from urllib.parse import urlsplit
    url, commit = source.get("url", ""), source.get("commit", "")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", commit)):
        raise ValueError("Git source requires an HTTPS URL without credentials and a full commit SHA")
    destination = job / "source"
    destination.mkdir(exist_ok=False)
    artifacts = job / "artifacts"
    artifacts.mkdir(exist_ok=True)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    # Repository-selection overrides take precedence over -C. Preserve auth and
    # proxy configuration, but never let an inherited checkout receive these writes.
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                 "GIT_NAMESPACE", "GIT_PREFIX"):
        env.pop(name, None)
    with (artifacts / "source-fetch.log").open("w") as log:
        def git(*args, capture=False):
            result = subprocess.run(["git", "-C", str(destination), *args], env=env,
                                    stdout=subprocess.PIPE if capture else log, stderr=log,
                                    text=True, check=True, timeout=min(timeout, 300))
            return result.stdout.strip() if capture else None
        git("init", "--quiet", *(["--object-format=sha256"] if len(commit) == 64 else []))
        worktree = Path(git("rev-parse", "--show-toplevel", capture=True)).resolve()
        git_dir = Path(git("rev-parse", "--absolute-git-dir", capture=True)).resolve()
        if worktree != destination.resolve() or git_dir != (destination / ".git").resolve():
            raise ValueError("Git source repository is outside the job directory")
        git("remote", "add", "origin", url)
        git("fetch", "--quiet", "--no-tags", "--depth=1", "origin", commit)
        actual = git("rev-parse", "FETCH_HEAD^{commit}", capture=True)
        if actual.lower() != commit.lower():
            raise ValueError("fetched commit does not match requested Git source")
        git("checkout", "--quiet", "--detach", actual)
        if git("rev-parse", "HEAD", capture=True) != actual:
            raise ValueError("Git checkout revision mismatch")
    receipt = {"mode": "remote_git", "url": url, "requested_commit": commit.lower(),
               "actual_commit": actual, "submodules_initialized": False}
    write_json(job / "source-provenance.json", receipt)
    return receipt


def terminate(child):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            return
        if sig == signal.SIGTERM:
            time.sleep(0.3)
            # Reap a terminated leader before escalating; a zombie-only group
            # can reject killpg with EPERM on macOS. Descendants may still live.
            child.poll()


class Cancelled(Exception):
    pass


def relative_file(value):
    if (not isinstance(value, str) or not value or "\\" in value
            or value.startswith("/") or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("expected a relative file path: " + str(value))
    return value


def file_hash(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("expected a regular file: " + str(path))
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            digest.update(block)
    return {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def rooted_file(root, name):
    name = relative_file(name)
    path = root / name
    if root.resolve() not in path.resolve().parents:
        raise ValueError("file escapes declared root: " + name)
    if any(p.is_symlink() for p in (path, *path.parents) if p != root and root in p.parents):
        raise ValueError("file path contains a symlink: " + name)
    return path


def prepare_task_inputs(job, specification):
    root = Path(specification.get("input_dir", str(job / "inputs")))
    if job.parent.resolve() not in root.resolve().parents:
        raise ValueError("task inputs must be inside the managed tasks directory")
    manifest_path = job / "inputs-manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if not isinstance(manifest, dict) or manifest != specification.get("task_inputs", {}):
        raise ValueError("task input manifest disagrees with job metadata")
    for name in manifest:
        relative_file(name)
    archive = job / "inputs.tar.gz"
    if archive.exists():
        if root != job / "inputs":
            raise ValueError("new task inputs cannot overwrite retained inputs")
        with tarfile.open(archive) as source:
            members = source.getmembers()
            if (len(members) != len(manifest) or {m.name for m in members} != set(manifest)
                    or any(not m.isfile() for m in members)):
                raise ValueError("invalid task input archive members")
            root.mkdir(exist_ok=False)
            for member in members:
                target = rooted_file(root, member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as stream, target.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(member.mode & 0o777)
    elif root == job / "inputs":
        root.mkdir(exist_ok=True)
    if not root.is_dir():
        raise ValueError("retained task input directory no longer exists")
    actual = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("task input contains a symlink: " + str(path))
        if not path.is_dir():
            actual[path.relative_to(root).as_posix()] = file_hash(path)
    if actual != manifest:
        raise ValueError("task input file hashes or inventory changed")
    return root


def validate_stage_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {"stages"} or not isinstance(plan["stages"], list) or not plan["stages"]:
        raise ValueError("stage plan requires a nonempty stages array")
    names = set()
    for stage in plan["stages"]:
        if not isinstance(stage, dict) or set(stage) - {"name", "script", "verify", "inputs", "outputs"}:
            raise ValueError("invalid stage fields")
        name = stage.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name in names:
            raise ValueError("stage names must be valid and unique")
        names.add(name)
        relative_file(stage.get("script"))
        if "verify" in stage:
            relative_file(stage["verify"])
        for key in ("inputs", "outputs"):
            roots = stage.get(key, {})
            allowed = {"workspace", "env", "inputs"} if key == "inputs" else {"workspace", "env"}
            if not isinstance(roots, dict) or set(roots) - allowed:
                raise ValueError("invalid stage " + key + " roots")
            for paths in roots.values():
                if not isinstance(paths, list) or len(paths) != len(set(map(str, paths))):
                    raise ValueError("stage file lists must be arrays of unique paths")
                for path in paths:
                    relative_file(path)
    return plan


def stage_fingerprint(stage, roots):
    files = {key: list(value) for key, value in stage.get("inputs", {}).items()}
    files.setdefault("inputs", []).extend([stage["script"]] + ([stage["verify"]] if stage.get("verify") else []))
    hashes = {key + "/" + name: file_hash(rooted_file(roots[key], name))
              for key, names in files.items() for name in names}
    value = {"definition": stage, "files": hashes}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def stage_outputs(stage, roots):
    return {key + "/" + name: file_hash(rooted_file(roots[key], name))
            for key, names in stage.get("outputs", {}).items() for name in names}


def run_stages(job):
    specification = json.loads((job / "job.json").read_text())
    plan = validate_stage_plan(json.loads((job / "stages.json").read_text()))
    roots = {"workspace": Path.cwd(), "env": Path(os.environ["JOB_ENV_DIR"]),
             "inputs": Path(os.environ["JOB_INPUT_DIR"])}
    stages = plan["stages"]
    start = specification.get("from_stage", stages[0]["name"])
    names = [stage["name"] for stage in stages]
    if start not in names:
        raise ValueError("resume stage is not in the plan")
    boundary = names.index(start)
    previous = {}
    if boundary:
        previous_dir = Path(specification["previous_job_dir"])
        if previous_dir.parent.resolve() != job.parent.resolve() or previous_dir.resolve() == job.resolve():
            raise ValueError("previous stage receipts must belong to another managed job")
        previous = json.loads((previous_dir / "stage-results.json").read_text())
    receipts = {}
    write_json(job / "stage-results.json", receipts)

    def execute(script):
        subprocess.run(["bash", "-euo", "pipefail", str(rooted_file(roots["inputs"], script))], check=True)

    # Check the entire skipped prefix before invoking the requested stage.
    for stage in stages[:boundary]:
        name = stage["name"]
        update_progress(job, {"phase": "resume-validation", "message": "Validate retained stage " + name})
        saved = previous.get(name, {})
        outputs = stage_outputs(stage, roots)
        if (saved.get("outcome") not in ("succeeded", "reused")
                or saved.get("fingerprint") != stage_fingerprint(stage, roots)
                or saved.get("outputs") != outputs or not (outputs or stage.get("verify"))):
            raise ValueError("cannot reuse stage " + name + ": inputs, outputs, or successful receipt missing/changed")
        if stage.get("verify"):
            execute(stage["verify"])
        # A verifier must not silently repair or replace declared artifacts.
        if stage_outputs(stage, roots) != outputs:
            raise ValueError("verification changed outputs of retained stage " + name)
        receipts[name] = dict(saved, outcome="reused", reused_from=specification["previous_job_dir"])
        write_json(job / "stage-results.json", receipts)

    # A later verifier can affect an earlier stage's files. Recheck the whole
    # retained prefix after all verifiers, before trusting any of its receipts.
    for stage in stages[:boundary]:
        name = stage["name"]
        try:
            unchanged = (receipts[name]["fingerprint"] == stage_fingerprint(stage, roots)
                         and receipts[name]["outputs"] == stage_outputs(stage, roots))
        except (OSError, ValueError):
            unchanged = False
        if not unchanged:
            error = "retained stage " + name + " changed during verification"
            receipts[name].update(outcome="invalidated", error=error)
            write_json(job / "stage-results.json", receipts)
            raise ValueError(error)

    for stage in stages[boundary:]:
        name = stage["name"]
        update_progress(job, {"phase": name, "message": "Run stage " + name})
        receipt = {"outcome": "running", "fingerprint": stage_fingerprint(stage, roots), "started_at": time.time()}
        receipts[name] = receipt
        write_json(job / "stage-results.json", receipts)
        try:
            execute(stage["script"])
            if stage.get("verify"):
                execute(stage["verify"])
            receipt.update(outcome="succeeded", outputs=stage_outputs(stage, roots))
        except Exception as error:
            receipt.update(outcome="failed", error=str(error))
            raise
        finally:
            receipt["finished_at"] = time.time()
            write_json(job / "stage-results.json", receipts)


def run(job, timeout):
    state = {"status": "starting", "started_at": time.time(),
             "worker_pid": os.getpid(), "worker_identity": identity(os.getpid())}
    write_json(job / "status.json", state)
    child = None
    with (job.parent / "instance.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            state.update(status="failed", reason="instance_busy", finished_at=time.time())
            write_json(job / "status.json", state)
            return

        def cancel(_signum, _frame):
            raise Cancelled()

        signal.signal(signal.SIGTERM, cancel)
        signal.signal(signal.SIGINT, cancel)
        result = {"status": "failed", "reason": "worker_error"}
        try:
            with worker_timing(job, state, "worker_prepare"):
                resources = capture_runtime_resources()
                state["runtime_resources"] = resources
                write_json(job / "runtime-resources.json", resources)
                write_json(job / "status.json", state)
                specification = json.loads((job / "job.json").read_text()) if (job / "job.json").exists() else {}
                check_resources(specification.get("resource_requirements"), resources)
                input_dir = prepare_task_inputs(job, specification)
                workspace = Path(specification.get("workspace", str(job / "source")))
                env_dir = Path(specification.get("env_dir", str(job / "env")))
                for path in (workspace, env_dir):
                    if job.parent.resolve() not in path.resolve().parents:
                        raise ValueError("workspace/env must be inside the managed tasks directory")
                if not specification.get("workspace"):
                    if specification.get("git_source"):
                        with worker_timing(job, state, "source_fetch"):
                            state["source_provenance"] = fetch_source(job, specification["git_source"], timeout)
                    else:
                        unpack(job)
                if not workspace.is_dir():
                    raise ValueError("continued workspace no longer exists")
                for name in ("artifacts", "env"):
                    (job / name).mkdir(exist_ok=True)
                env = os.environ.copy()
                env.update(ARTIFACTS_DIR=str(job / "artifacts"),
                           JOB_DIR=str(job), JOB_ENV_DIR=str(env_dir), JOB_INPUT_DIR=str(input_dir),
                           PYTHONDONTWRITEBYTECODE="1",
                           JOB_PROGRESS_FILE=str(job / "progress.json"),
                           JOB_WORKER=str(Path(__file__).resolve()))
            with worker_timing(job, state, "task_execution") as timing:
                with (job / "output.log").open("ab", buffering=0) as log:
                    child = subprocess.Popen(["bash", "-euo", "pipefail", str(job / "task.sh")],
                                             cwd=workspace, env=env,
                                             stdin=subprocess.DEVNULL, stdout=log,
                                             stderr=subprocess.STDOUT, start_new_session=True)
                    state.update(status="running", task_pid=child.pid)
                    write_json(job / "status.json", state)
                    code = child.wait(timeout=timeout)
                    result = {"status": "succeeded" if code == 0 else "failed", "exit_code": code}
                    timing["outcome"] = result["status"]
        except subprocess.TimeoutExpired:
            result = {"status": "timed_out", "reason": "deadline_exceeded", "exit_code": None}
        except Cancelled:
            result = {"status": "cancelled", "exit_code": None}
        except ResourceMismatch as error:
            result = {"status": "failed", "reason": "resource_mismatch", "error": str(error)}
        except Exception as error:
            result = {"status": "failed", "reason": "worker_error", "error": str(error)}
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                with worker_timing(job, state, "task_cleanup"):
                    try:
                        if child:
                            # Also terminate services left behind by a completed task.
                            terminate(child)
                            child.wait(timeout=5)
                    finally:
                        try:
                            close_task_phase(job, result["status"])
                        except (ValueError, OSError, OverflowError, RecursionError) as error:
                            state["task_phases_error"] = str(error)
            except (OSError, subprocess.TimeoutExpired) as error:
                result.update(task_status=result["status"], status="failed",
                              reason="cleanup_failed", cleanup_error=str(error))
            finally:
                # Publish the terminal outcome only after process cleanup and phase closure.
                state.update(result, finished_at=time.time())
                write_json(job / "status.json", state)


class NoHealthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        # Health checks validate the requested local endpoint, not its redirect target.
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["launch", "run", "stages", "status", "logs", "cancel", "health", "busy", "progress"])
    parser.add_argument("job", type=Path)
    parser.add_argument("value", nargs="?", default="7200")
    args = parser.parse_args()
    job = args.job
    if args.action == "stages":
        run_stages(job)
        return
    if args.action == "run":
        run(job, int(args.value))
        return
    if args.action == "launch":
        job.mkdir(parents=True, exist_ok=True)
        if (job / "status.json").exists():
            print(json.dumps(inspect(job)))
            return
        write_json(job / "status.json", {"status": "starting", "started_at": time.time()})
        with (job / "worker.log").open("ab") as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run",
                                        str(job), args.value], stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True)
        print(json.dumps({"status": "starting", "worker_pid": process.pid}))
    elif args.action == "busy":
        # Hold the same lease used by workers while checking availability.
        with (job / "instance.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                print(json.dumps({"busy": False}))
            except BlockingIOError:
                print(json.dumps({"busy": True}))
    elif args.action == "progress":
        print(json.dumps(update_progress(job, json.loads(args.value))))
    elif args.action == "status":
        print(json.dumps(inspect(job)))
    elif args.action == "logs":
        size = max(0, min(int(args.value), 131072))
        path = job / "output.log"
        if path.exists():
            with path.open("rb") as log:
                log.seek(max(0, path.stat().st_size - size))
                sys.stdout.buffer.write(log.read(size))
    elif args.action == "cancel":
        state = inspect(job)
        if state["status"] not in TERMINAL and state.get("worker_pid"):
            os.kill(state["worker_pid"], signal.SIGTERM)
        print(json.dumps(state))
    elif args.action == "health":
        # Probe localhost only: no public port forwarding is required.
        from urllib.parse import urlparse
        url = urlparse(args.value)
        if url.scheme not in ("http", "https") or url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("health URL must use HTTP(S) on localhost")
        try:
            request = urllib.request.Request(args.value)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoHealthRedirect())
            with opener.open(request, timeout=5) as response:
                print(json.dumps({"healthy": 200 <= response.status < 300, "http_status": response.status}))
        except urllib.error.HTTPError as error:
            error.close()
            print(json.dumps({"healthy": False, "http_status": error.code, "error": str(error)}))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            print(json.dumps({"healthy": False, "error": str(error)}))


if __name__ == "__main__":
    main()
