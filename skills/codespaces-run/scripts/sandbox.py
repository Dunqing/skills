#!/usr/bin/env python3
"""Manage reusable, stop-only Codespaces. Python stdlib + gh; Linux/macOS controller."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = "/workspaces/.agent-codespaces"
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}
# Remote task files are writable by the task. Keep identity, paths, instance
# selection and collection/shutdown receipts under the controller's authority.
WORKER_STATUS_FIELDS = {
    "status", "reason", "error", "exit_code", "task_status", "cleanup_error",
    "worker_pid", "worker_identity", "task_pid", "started_at", "finished_at",
    "elapsed_seconds", "runtime_resources", "source_provenance", "worker_timings",
    "transport_timings", "transport_timings_error", "progress", "progress_error",
    "task_phases", "task_phases_error",
}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def emit(value):
    print(json.dumps(value, ensure_ascii=False))


def call(argv, timeout=90):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("{}: {}".format(shlex.join(argv[:3]), result.stderr.strip()[-4000:]))
    return result.stdout


def api(endpoint, data=None):
    args = ["gh", "api", endpoint]
    if data is not None:
        args += ["--method", "POST"]
        for key, value in data.items():
            args += ["-F" if isinstance(value, int) else "-f", "{}={}".format(key, value)]
    return json.loads(call(args) or "{}")


def ssh(cs, argv, timeout=90):
    return call(["gh", "codespace", "ssh", "-c", cs, "--", "-T", shlex.join(argv)], timeout)


def worker(cs, action, job, value=None):
    args = ["python3", ROOT + "/remote_worker.py", action, job]
    if value is not None:
        args.append(str(value))
    result = ssh(cs, args)
    return result if action == "logs" else json.loads(result)


def remote_busy(cs):
    # A newly adopted instance may not have the worker uploaded yet. Absence of
    # the worker means there cannot be a worker-managed task on this instance.
    code = ("import json,pathlib,subprocess,sys; "
            "p=pathlib.Path({!r}); "
            "print(subprocess.check_output([sys.executable,str(p),'busy',{!r}],text=True) "
            "if p.exists() else json.dumps({{'busy':False}}))").format(
                ROOT + "/remote_worker.py", ROOT + "/tasks")
    return json.loads(ssh(cs, ["python3", "-c", code]))["busy"]


def atomic(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def valid_name(value):
    if not NAME.fullmatch(value):
        raise ValueError("invalid identifier: " + value)
    return value


@contextmanager
def timed(store, job, phase):
    """Keep controller timings separately so status refreshes cannot lose them."""
    directory = store.job_dir(job["job_id"])
    directory.mkdir(exist_ok=True)
    path = directory / "controller-timings.json"
    record = {"id": uuid.uuid4().hex, "phase": phase,
              "started_at": time.time(), "outcome": "running"}
    started = time.monotonic()

    def persist():
        with (directory / "timings.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            history = json.loads(path.read_text()) if path.exists() else []
            history = [entry for entry in history if entry["id"] != record["id"]]
            history.append(dict(record))
            atomic(path, history)
            job["controller_timings"] = history

    persist()
    try:
        yield
    except BaseException as error:
        record.update(outcome="failed", error=str(error))
        raise
    else:
        record["outcome"] = "succeeded"
    finally:
        record.update(finished_at=time.time(), duration_seconds=max(0, time.monotonic() - started))
        persist()


class Store:
    def __init__(self, directory):
        self.root = Path(directory).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "jobs").mkdir(exist_ok=True)
        self.path = self.root / "registry.json"

    @contextmanager
    def lock(self):
        with (self.root / "registry.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def load(self):
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"config": {"host_repo": None, "machine": None, "max_instances": 2}, "instances": {}}

    def save(self, data):
        atomic(self.path, data)

    def job_dir(self, job_id):
        return self.root / "jobs" / valid_name(job_id)

    def job(self, job_id):
        return json.loads((self.job_dir(job_id) / "job.json").read_text())

    def save_job(self, job):
        directory = self.job_dir(job["job_id"])
        directory.mkdir(exist_ok=True)
        timings = directory / "controller-timings.json"
        if timings.exists():
            job["controller_timings"] = json.loads(timings.read_text())
        atomic(directory / "job.json", job)


def remote_path(job_id):
    return ROOT + "/tasks/" + valid_name(job_id)


def snapshot(source, archive):
    """Capture the actual working tree, including dirty/untracked, nonignored files."""
    source = Path(source).resolve()
    top = Path(call(["git", "-C", str(source), "rev-parse", "--show-toplevel"]).strip()).resolve()
    if source != top:
        raise ValueError("--source must be the repository root")
    files = call(["git", "-C", str(source), "ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    with tarfile.open(archive, "w:gz") as out:
        for name in sorted(set(files.split("\0")) - {""}):
            path = source / name
            if not path.exists():
                continue  # Preserve locally deleted tracked files as absent.
            if path.is_symlink():
                raise ValueError("source symlinks need an explicit packaging recipe: " + name)
            if not path.is_file() or source not in path.resolve().parents:
                raise ValueError("unsupported source entry (submodule/directory): " + name)
            if name.startswith(".git/") or name == ".git" or path.name == ".env" or re.search(r"(^|/)\.env\.(?!example$|sample$|template$)", name):
                raise ValueError("credential/environment file requires an explicit packaging recipe: " + name)
            out.add(path, arcname=name, recursive=False)
    return {"commit": call(["git", "-C", str(source), "rev-parse", "HEAD"]).strip(),
            "dirty": bool(call(["git", "-C", str(source), "status", "--porcelain"]).strip()),
            "source": str(source), "git_metadata_included": False}


def ensure_ready(cs):
    state = api("user/codespaces/" + cs)["state"]
    if state == "Shutdown":
        api("user/codespaces/" + cs + "/start", {})
    deadline = time.monotonic() + 300
    last = ""
    while time.monotonic() < deadline:
        try:
            ssh(cs, ["python3", "--version"], timeout=25)
            return
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            last = str(error)
            time.sleep(5)
    raise RuntimeError("SSH not ready within 300 seconds: " + last)


def status(store, job):
    cached = dict(job)
    if "codespace" not in job:
        store.save_job(cached)
        return cached
    try:
        state = api("user/codespaces/" + job["codespace"])["state"]
        cached["codespace_state"] = state
        if state == "Available" and not (job.get("status") in TERMINAL and
                (job.get("collected") or not job.get("launch_attempted", True))):
            remote = worker(job["codespace"], "status", remote_path(job["job_id"]))
            if (not isinstance(remote, dict) or not isinstance(remote.get("status"), str)
                    or remote["status"] not in TERMINAL | {"starting", "running", "unknown"}):
                raise RuntimeError("invalid worker status response")
            if "job_id" in remote and remote["job_id"] != job["job_id"]:
                raise RuntimeError("worker status job_id mismatch")
            progress = remote.get("progress")
            if progress is not None and (not isinstance(progress, dict)
                    or not isinstance(progress.get("phase"), str)):
                raise RuntimeError("invalid worker progress response")
            cached.update({key: value for key, value in remote.items() if key in WORKER_STATUS_FIELDS})
            if progress:
                cached["phase"] = progress["phase"]
            elif remote.get("status") == "running":
                cached["phase"] = "executing"
            cached.pop("observation_error", None)
            if (remote.get("reason") == "missing_status" and time.time() - job["created_at"] > 300
                    and not remote_busy(job["codespace"])):
                cached.update(status="interrupted", reason="launch_not_observed")
        elif state == "Shutdown" and job.get("status") not in TERMINAL:
            cached.update(status="interrupted", reason="codespace_stopped")
        elif job.get("status") not in TERMINAL:
            cached.update(status="unknown", reason="codespace_" + state)
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        # A transport failure is not evidence that the task exited.
        if job.get("status") in TERMINAL:
            cached["observation_error"] = str(error)
        else:
            cached.update(status="unknown", reason="transport_error", error=str(error))
    store.save_job(cached)
    return cached


def collect(store, job):
    cs, target = job["codespace"], store.job_dir(job["job_id"])
    # A single archive avoids an SSH/SFTP round trip for every small output file.
    # Inline stdlib code also works when recovering a job made by an older worker.
    bundle_name = "results-" + uuid.uuid4().hex + ".tar.gz"
    code = '''import hashlib,json,os,pathlib,sys,tarfile
job=pathlib.Path(sys.argv[1]); bundle=job/sys.argv[2]; temporary=job/(sys.argv[2]+'.tmp')
with tarfile.open(temporary,'w:gz') as archive:
 for name in ['status.json','output.log','worker.log','progress.json','task-phases.json','progress-events.jsonl','stage-results.json','stages.json','inputs-manifest.json','runtime-resources.json','transport-timings.json','source-provenance.json','artifacts']:
  path=job/name
  if path.exists(): archive.add(path,arcname=name)
os.replace(temporary,bundle)
digest=hashlib.sha256()
with bundle.open('rb') as stream:
 for block in iter(lambda:stream.read(1048576),b''): digest.update(block)
print(json.dumps({'sha256':digest.hexdigest(),'bytes':bundle.stat().st_size}))
'''
    metadata = json.loads(ssh(cs, ["python3", "-I", "-c", code, remote_path(job["job_id"]), bundle_name], timeout=300))
    transfer = Path(tempfile.mkdtemp(prefix="transfer-", dir=target))
    bundle = transfer / "results.tar.gz"
    call(["gh", "codespace", "cp", "--expand", "-c", cs,
          "remote:" + remote_path(job["job_id"]) + "/" + bundle_name, str(bundle)], timeout=300)
    digest = hashlib.sha256()
    with bundle.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            digest.update(block)
    if digest.hexdigest() != metadata["sha256"]:
        raise ValueError("artifact archive checksum mismatch")
    unpacked = transfer / "unpacked"
    unpacked.mkdir()
    with tarfile.open(bundle) as archive:
        members = archive.getmembers()
        for member in members:
            destination = (unpacked / member.name).resolve()
            if (unpacked.resolve() not in destination.parents
                    or not (member.isfile() or member.isdir())):
                raise ValueError("unsafe artifact archive member (package links explicitly): " + member.name)
        for member in members:
            destination = unpacked / member.name
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(member.mode & 0o777)
    for name in ("status.json", "output.log", "worker.log", "progress.json", "task-phases.json", "progress-events.jsonl", "stage-results.json", "stages.json", "inputs-manifest.json", "runtime-resources.json", "transport-timings.json", "source-provenance.json"):
        if (unpacked / name).exists():
            os.replace(unpacked / name, target / name)
    artifacts = unpacked / "artifacts"
    if artifacts.is_dir():
        if (target / "artifacts").exists():
            (target / "artifacts").rename(target / ("artifacts-before-" + uuid.uuid4().hex))
        artifacts.rename(target / "artifacts")
    job["collected"] = True
    job["collection_transfer"] = metadata
    job.pop("collection_error", None)
    store.save_job(job)


def stop(store, cs, expected_job=None):
    with store.lock():
        registry = store.load()
        if cs not in registry["instances"]:
            raise ValueError("refusing to stop an unmanaged Codespace")
        instance = registry["instances"][cs]
        if instance.get("lease") and instance["lease"] != expected_job:
            raise ValueError("instance has an active or unresolved job; finish/cancel it first")
        state = api("user/codespaces/" + cs)["state"]
        if state not in ("Shutdown", "ShuttingDown"):
            if remote_busy(cs):
                raise ValueError("a remote worker still holds the instance lease")
            call(["gh", "codespace", "stop", "-c", cs])
        deadline = time.monotonic() + 90
        while state != "Shutdown":
            state = api("user/codespaces/" + cs)["state"]
            if state == "Shutdown":
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("stop requested but Shutdown not confirmed within 90 seconds")
            time.sleep(2)
        # Hold the lease through shutdown so a new submit cannot race cleanup.
        if expected_job and instance.get("lease") == expected_job:
            instance["lease"] = None
        instance["stopped_at"] = time.time()
        store.save(registry)


def finish(store, job):
    """Persist evidence before releasing the lease and stopping the instance."""
    if job["status"] not in TERMINAL:
        raise ValueError("job is not terminal")
    if "codespace" not in job:
        store.save_job(job)
        return job
    state = api("user/codespaces/" + job["codespace"])["state"]
    if state == "Available" and not job.get("collected") and job.get("launch_attempted", True):
        try:
            with timed(store, job, "collection"):
                collect(store, job)
        except (RuntimeError, subprocess.TimeoutExpired, ValueError, OSError) as error:
            job["collection_error"] = str(error)
            store.save_job(job)
            # Keep data remotely, stop compute, and report incomplete collection.
    job.setdefault("stop_requested_at", time.time())
    store.save_job(job)
    if not (state == "Shutdown" and job.get("shutdown_confirmed_at")):
        with timed(store, job, "shutdown"):
            stop(store, job["codespace"], expected_job=job["job_id"])
        job.setdefault("shutdown_confirmed_at", time.time())
    store.save_job(job)
    return job


def spawn_watch(store, job):
    directory = store.job_dir(job["job_id"])
    with (directory / "watch.log").open("ab") as log:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--state-dir",
                                  str(store.root), "watch", job["job_id"]],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    return child.pid


def environment_for(instance, repo, profile, key, job_id):
    """Select a retained dependency directory without reusing a source snapshot."""
    identity = {"repo": repo, "profile": profile, "key": key}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    environments = instance.setdefault("environments", {})
    if digest in environments:
        saved = environments[digest]
        if saved["identity"] != identity:
            raise ValueError("environment identity mismatch")
        path = Path(saved["env_dir"])
        if path.parent.parent != Path(ROOT + "/tasks") or path.name != "env":
            raise ValueError("environment path is outside managed task directories")
        valid_name(path.parent.name)
        return saved["env_dir"], True
    path = remote_path(job_id) + "/env"
    environments[digest] = {"identity": identity, "env_dir": path}
    return path, False


def machine_spec(info):
    value = info.get("machine")
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        raise ValueError("Codespaces API did not report a machine specification")
    for key in ("cpus", "memory_in_bytes"):
        if type(value.get(key)) is not int or value[key] <= 0:
            raise ValueError("Codespaces API did not report a valid " + key)
    return {key: value[key] for key in ("name", "cpus", "memory_in_bytes")}


def resource_errors(machine, requirements):
    errors = []
    if requirements.get("machine") and machine["name"] != requirements["machine"]:
        errors.append("machine {} != {}".format(machine["name"], requirements["machine"]))
    if requirements.get("cpus") and machine["cpus"] != requirements["cpus"]:
        errors.append("CPUs {} != {}".format(machine["cpus"], requirements["cpus"]))
    if requirements.get("min_memory_bytes") and machine["memory_in_bytes"] < requirements["min_memory_bytes"]:
        errors.append("memory {} < {} bytes".format(machine["memory_in_bytes"], requirements["min_memory_bytes"]))
    return errors


def requested_resources(args, config):
    cpus = getattr(args, "cpus", None)
    memory = getattr(args, "min_memory_mib", None)
    for name, value in (("cpus", cpus), ("min-memory-mib", memory)):
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError(name + " must be a positive integer")
    # Explicit per-task capacities replace the default machine policy, but an
    # explicitly named machine must satisfy all capacities too.
    machine = getattr(args, "machine", None)
    if not machine and cpus is None and memory is None:
        machine = config.get("machine")
    return {"machine": machine, "cpus": cpus,
            "min_memory_bytes": memory * 1024 * 1024 if memory is not None else None}


def idle_timeout_minutes(task_timeout):
    """Let an abandoned instance stop soon after its deadline, not hours later."""
    minutes = max(30, -(-int(task_timeout or 0) // 60) + 15)
    return min(240, minutes)


def select_instance(registry, args, previous, job):
    config = registry["config"]
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        raise ValueError("repo must be OWNER/REPO")
    requirements = requested_resources(args, config)
    selected = previous["codespace"] if previous else args.codespace
    # A known target must not depend on unrelated legacy instances being reachable.
    # Resolve all unknown bindings only when needed to avoid creating a duplicate;
    # an explicit legacy selection needs only that instance's identity.
    observed = {}
    known_target = any((value.get("repo") or "").casefold() == args.repo.casefold()
                       for value in registry["instances"].values())
    for name, value in registry["instances"].items():
        if not value.get("repo") and (name == selected or (not selected and not known_target)):
            observed[name] = api("user/codespaces/" + name)
            repository = observed[name].get("repository", {}).get("full_name")
            if not repository:
                raise ValueError("cannot bind legacy instance without repository metadata: " + name)
            value["repo"] = repository
    same_repo = [(name, value) for name, value in registry["instances"].items()
                 if (value.get("repo") or "").casefold() == args.repo.casefold()]
    if len(same_repo) > 1 and not selected:
        raise ValueError("multiple legacy instances for repository; select one explicitly: " +
                         ", ".join(name for name, _ in same_repo))
    candidates = [(name, value) for name, value in same_repo
                  if not value.get("lease") and value["profile"] == args.profile]
    if selected and selected not in dict(candidates):
        raise ValueError("selected instance belongs to another repository, is busy, or has a different profile")
    if same_repo and not candidates:
        raise ValueError("repository instance is busy or has a different profile; no second instance will be created")
    host_repo = args.repo
    # New instances belong to the task repository; host_repo configuration enables creation.
    available = None
    if not requirements["machine"] and (config.get("allow_create") or config.get("host_repo")):
        available = api("repos/" + host_repo + "/codespaces/machines")["machines"]
        specs = [machine_spec({"machine": value}) for value in available]
        suitable = [value for value in specs if not resource_errors(value, requirements)]
        if not suitable:
            raise ValueError("no available machine satisfies requested resources")
        requirements["machine"] = min(suitable, key=lambda m: (m["cpus"], m["memory_in_bytes"], m["name"]))["name"]
    job["requested_resources"] = dict(requirements)
    matches, rejected = [], []
    for name, value in candidates:
        if selected and name != selected:
            continue
        actual = machine_spec(observed[name] if name in observed else api("user/codespaces/" + name))
        value.update(observed_machine=actual, machine_observed_at=time.time())
        errors = resource_errors(actual, requirements)
        if errors:
            rejected.append(name + ": " + "; ".join(errors))
        else:
            matches.append((name, actual))
    if matches:
        cs, actual = min(matches, key=lambda item: (item[1]["cpus"], item[1]["memory_in_bytes"], item[0]))
    elif candidates:
        # Never resize or create another VM just because an idle VM drifted.
        raise ValueError("resource mismatch; no instance was started or resized: " + " | ".join(rejected))
    else:
        if not (config.get("allow_create") or config.get("host_repo")):
            raise ValueError("configure --allow-create or adopt a repository Codespace first")
        if len(registry["instances"]) >= config["max_instances"]:
            raise ValueError("instance limit reached; reuse/adopt a compatible idle instance")
        if available is None:
            available = api("repos/" + host_repo + "/codespaces/machines")["machines"]
        if not any(not resource_errors(machine_spec({"machine": value}), requirements) for value in available):
            raise ValueError("no available machine satisfies requested resources")
        created = api("repos/" + host_repo + "/codespaces", {
            "machine": requirements["machine"], "display_name": "agent-" + args.profile,
            "idle_timeout_minutes": idle_timeout_minutes(job.get("timeout") or args.timeout),
            "retention_period_minutes": 43200})
        cs = created["name"]
        # Register before verification so an incomplete API reply never leaves
        # a newly created retained instance outside the managed pool.
        registry["instances"][cs] = {"profile": args.profile, "repo": args.repo, "created_at": time.time(),
                                     "keep_codespace_needs_check": True}
        registry["instances"][cs]["lease"] = job["job_id"]
        job["codespace"] = cs
        actual = machine_spec(created)
        errors = resource_errors(actual, requirements)
        if errors:
            raise ValueError("created instance resource mismatch: " + "; ".join(errors))
    registry["instances"][cs].update(observed_machine=actual, machine_observed_at=time.time())
    job["resource_requirements"] = dict(requirements, machine=actual["name"], cpus=actual["cpus"])
    job["observed_machine"] = actual
    return cs


def verify_machine(cs, job):
    actual = machine_spec(api("user/codespaces/" + cs))
    job["observed_machine"] = actual
    errors = resource_errors(actual, job["resource_requirements"])
    if errors:
        raise ValueError("resource mismatch before launch: " + "; ".join(errors))


def pack_task_inputs(source, directory):
    """Snapshot independent task files once, with a manifest of the captured bytes."""
    source = Path(source).resolve()
    if not source.is_dir():
        raise ValueError("--inputs must be a directory")
    manifest = {}
    with tarfile.open(directory / "inputs.tar.gz", "w:gz") as archive:
        for base, dirs, files in os.walk(source, followlinks=False):
            for name in dirs + files:
                path = Path(base) / name
                relative = path.relative_to(source).as_posix()
                if path.is_symlink():
                    raise ValueError("task inputs cannot contain symlinks: " + relative)
                if ("\\" in relative or ".git" in Path(relative).parts or name == ".env"
                        or re.match(r"^\.env\.(?!example$|sample$|template$)", name)):
                    raise ValueError("unsupported task input: " + relative)
            for name in sorted(files):
                path = Path(base) / name
                if not path.is_file():
                    raise ValueError("task inputs require regular files: " + str(path))
                relative = path.relative_to(source).as_posix()
                data = path.read_bytes()
                info = archive.gettarinfo(str(path), arcname=relative)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
                manifest[relative] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    atomic(directory / "inputs-manifest.json", manifest)
    return manifest


def load_stage_plan(path, inputs):
    # Keep the schema shared with the uploaded worker without adding a runtime dependency.
    import importlib.util
    spec = importlib.util.spec_from_file_location("sandbox_stage_schema", HERE / "remote_worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = module.validate_stage_plan(json.loads(Path(path).read_text()))
    for stage in plan["stages"]:
        for script in (stage["script"], stage.get("verify")):
            if script and script not in inputs:
                raise ValueError("stage script missing from --inputs: " + script)
        for name in stage.get("inputs", {}).get("inputs", []):
            if name not in inputs:
                raise ValueError("stage task input missing from --inputs: " + name)
    return plan


def pack_input(store, job):
    directory = store.job_dir(job["job_id"])
    bundle = directory / "input-bundle.tar"
    members = {"remote_worker.py": HERE / "remote_worker.py",
               "task.sh": directory / "task.sh", "job.json": directory / "job.json"}
    for name in ("source.tar.gz", "inputs.tar.gz", "inputs-manifest.json", "stages.json"):
        if (directory / name).exists():
            members[name] = directory / name
    # The source snapshot is already compressed; avoid compressing it twice.
    with tarfile.open(bundle, "w:") as archive:
        for name, path in members.items():
            archive.add(path, arcname=name, recursive=False)
    digest = hashlib.sha256()
    with bundle.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            digest.update(block)
    job["input_transfer"] = {"sha256": digest.hexdigest(), "bytes": bundle.stat().st_size,
                             "members": list(members), "upload_count": 1}
    return bundle


def git_source(args):
    url, commit = getattr(args, "git_url", None), getattr(args, "git_commit", None)
    if url is None and commit is None:
        return None
    from urllib.parse import urlsplit
    if not url or not commit:
        raise ValueError("--git-url and --git-commit must be provided together")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError("git-url must be HTTPS without embedded credentials, query or fragment")
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", commit):
        raise ValueError("git-commit must be a full immutable commit SHA")
    return {"url": url, "commit": commit.lower()}


def submit(store, args):
    script_arg, stages_arg = getattr(args, "script", None), getattr(args, "stages", None)
    if bool(script_arg) == bool(stages_arg):
        raise ValueError("choose exactly one of --script or --stages")
    script = Path(script_arg).resolve() if script_arg else None
    if script and not script.is_file():
        raise ValueError("task script not found")
    from_stage = getattr(args, "from_stage", None)
    if from_stage and (not stages_arg or not args.continue_from):
        raise ValueError("--from-stage requires --stages and --continue-from")
    if args.timeout < 1 or args.timeout > 39600:
        raise ValueError("task timeout must be 1..39600 seconds (11 hours)")
    previous = store.job(args.continue_from) if args.continue_from else None
    git = git_source(args)
    if sum(bool(value) for value in (args.source, previous, git)) != 1:
        raise ValueError("choose exactly one of --source, --git-url/--git-commit, or --continue-from")
    environment_key = getattr(args, "environment_key", None)
    if environment_key and previous:
        raise ValueError("choose --environment-key with a fresh source, or --continue-from")
    if environment_key is not None and (not environment_key.strip() or len(environment_key) > 512):
        raise ValueError("environment key must contain 1..512 non-secret characters")
    if previous and previous["status"] not in TERMINAL:
        raise ValueError("continue-from must reference a finished task")
    if previous and previous["repo"] != args.repo:
        raise ValueError("continue-from must use the same repository")
    if previous and args.source:
        raise ValueError("choose --source or --continue-from")
    job_id = "job-" + uuid.uuid4().hex[:16]
    job = {"job_id": job_id, "status": "preparing", "repo": args.repo,
           "profile": args.profile, "created_at": time.time(), "timeout": args.timeout,
           "phase": "preparing", "launch_attempted": False,
           "execution_target": "codespaces", "local_fallback": "forbidden"}
    jobdir = store.job_dir(job_id)
    jobdir.mkdir()
    if script:
        shutil.copyfile(script, jobdir / "task.sh")
    else:
        (jobdir / "task.sh").write_text('exec python3 -I "$JOB_WORKER" stages "$JOB_DIR"\n')
    if git:
        job["git_source"] = git
    store.save_job(job)
    try:
        inputs_arg = getattr(args, "inputs", None)
        if inputs_arg:
            with timed(store, job, "task_inputs"):
                job["task_inputs"] = pack_task_inputs(inputs_arg, jobdir)
            job["input_dir"] = remote_path(job_id) + "/inputs"
        elif previous and previous.get("input_dir"):
            job["input_dir"] = previous["input_dir"]
            job["task_inputs"] = previous["task_inputs"]
            atomic(jobdir / "inputs-manifest.json", job["task_inputs"])
        if stages_arg:
            plan = load_stage_plan(stages_arg, job.get("task_inputs", {}))
            if from_stage and from_stage not in [stage["name"] for stage in plan["stages"]]:
                raise ValueError("--from-stage is not in the stage plan")
            atomic(jobdir / "stages.json", plan)
            if from_stage:
                job["from_stage"] = from_stage
                job["previous_job_dir"] = remote_path(previous["job_id"])
        if args.source:
            with timed(store, job, "snapshot"):
                job["snapshot"] = snapshot(args.source, jobdir / "source.tar.gz")
        with store.lock():
            registry = store.load()
            try:
                with timed(store, job, "resource_selection"):
                    cs = select_instance(registry, args, previous, job)
                registry["instances"][cs]["lease"] = job_id
                job["codespace"] = cs
                if previous:
                    job["continue_from"] = previous["job_id"]
                    job["workspace"] = previous.get("workspace", remote_path(previous["job_id"]) + "/source")
                    job["env_dir"] = previous.get("env_dir", remote_path(previous["job_id"]) + "/env")
                elif environment_key:
                    job["env_dir"], job["environment_reused"] = environment_for(
                        registry["instances"][cs], args.repo, args.profile, environment_key, job_id)
                    job["environment_key"] = environment_key
            finally:
                store.save(registry)
                store.save_job(job)
        with timed(store, job, "pack"):
            bundle = pack_input(store, job)
        job["phase"] = "starting_codespace"
        store.save_job(job)
        with timed(store, job, "startup"):
            ensure_ready(cs)
        with timed(store, job, "resource_verification"):
            verify_machine(cs, job)
        job["phase"] = "uploading"
        store.save_job(job)
        remote_bundle = str(Path(ROOT).parent / (".codespaces-upload-" + job_id + ".tar"))
        with timed(store, job, "upload"):
            call(["gh", "codespace", "cp", "--expand", "-c", cs, str(bundle), "remote:" + remote_bundle], timeout=300)
        job["launch_attempted"] = True
        job["phase"] = "launching_worker"
        store.save_job(job)
        with timed(store, job, "bootstrap"):
            result = json.loads(ssh(cs, ["python3", "-I", "-c", (HERE / "bootstrap.py").read_text(),
                                        ROOT, job_id, remote_bundle, job["input_transfer"]["sha256"], str(args.timeout)], timeout=300))
            job["transport_timings"] = result.get("transport_timings", [])
            if not result.get("ok"):
                job["launch_attempted"] = result.get("launch_attempted", True)
                raise RuntimeError(result.get("error", "remote bootstrap failed"))
        job["status"] = "starting"
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        job.update(status="unknown" if job.get("launch_attempted") else "failed", error=str(error))
        store.save_job(job)
        if not job.get("launch_attempted"):
            if "codespace" not in job:
                return job  # Mismatch/API failure before lease acquisition: no remote writes.
            # Keep the lease until shutdown is confirmed, including creation/setup failures.
            try:
                job["stop_requested_at"] = time.time()
                with timed(store, job, "shutdown"):
                    stop(store, job["codespace"], expected_job=job_id)
                job["shutdown_confirmed_at"] = time.time()
                store.save_job(job)
                return job
            except (RuntimeError, subprocess.TimeoutExpired, ValueError) as stop_error:
                job["shutdown_error"] = str(stop_error)
    store.save_job(job)
    watch_pid = spawn_watch(store, job)
    atomic(jobdir / "watcher.json", {"pid": watch_pid})
    return dict(job, watch_pid=watch_pid)


class ProgressNotifications:
    """Coalesce counters/messages without delaying observed state changes or errors."""
    def __init__(self, interval=45):
        if not 1 <= interval <= 3600:
            raise ValueError("notification interval must be 1..3600 seconds")
        self.interval = interval
        self.last = None
        self.last_at = 0

    def ready(self, observation, now):
        progress = {key: value for key, value in (observation.get("progress") or {}).items()
                    if key not in {"started_at", "updated_at", "elapsed_seconds"}}
        transition = tuple(observation.get(key) for key in
                           ("status", "phase", "codespace_state", "reason", "error", "task_status",
                            "observation_error", "cleanup_error", "progress_error", "task_phases_error"))
        signature = (transition, progress)
        immediate = self.last is None or transition != self.last[0]
        if immediate or (signature != self.last and now - self.last_at >= self.interval):
            self.last, self.last_at = signature, now
            return True
        return False


def watch(store, job_id, notify_interval=45):
    # One watcher per job. A second command can safely resume a stopped watcher.
    with (store.job_dir(job_id) / "watch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"watching": True, "reason": "another_watcher_holds_lock"}
        notifications = ProgressNotifications(notify_interval)
        cleanup_error = None
        while True:
            job = status(store, store.job(job_id))
            observation = {key: job.get(key) for key in
                           ("job_id", "status", "phase", "progress", "codespace_state",
                            "reason", "error", "task_status",
                            "observation_error", "progress_error", "task_phases_error")}
            observation["cleanup_error"] = cleanup_error
            if notifications.ready(observation, time.monotonic()):
                emit(observation)
                sys.stdout.flush()
            if job["status"] in TERMINAL:
                try:
                    return finish(store, job)
                except (RuntimeError, subprocess.TimeoutExpired, ValueError) as error:
                    # Do not delay the terminal notification while collection/shutdown retries.
                    cleanup_error = str(error)
                    observation["cleanup_error"] = cleanup_error
                    if notifications.ready(observation, time.monotonic()):
                        emit(observation)
                        sys.stdout.flush()
            time.sleep(20)


def watcher_active(store, job_id):
    """True while a watcher still holds this job's lock."""
    path = store.job_dir(job_id) / "watch.lock"
    try:
        with path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def events(store, job_id, follow=False):
    """Print retained watcher events; following ends at the final receipt."""
    path = store.job_dir(job_id) / "watch.log"
    offset = 0
    while True:
        if path.exists():
            with path.open() as stream:
                stream.seek(offset)
                lines = stream.readlines()
                offset = stream.tell()
            for line in lines:
                if not line.strip():
                    continue
                print(line.rstrip("\n"), flush=True)
                try:
                    final = bool(json.loads(line).get("final"))
                except ValueError:
                    final = False
                if final:
                    return 0
        if not follow:
            return 0
        # A terminal job whose watcher has exited will never write a receipt.
        if not watcher_active(store, job_id):
            return 3
        time.sleep(5)


def timing_report(store, job):
    """Read retained timing evidence without waking or querying a Codespace."""
    directory = store.job_dir(job["job_id"])
    timings = directory / "controller-timings.json"
    remote_status = directory / "status.json"
    phases = directory / "task-phases.json"
    errors = {}

    def read_evidence(path, fallback, expected_type):
        if not path.exists():
            return fallback
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, expected_type):
                raise ValueError("unexpected evidence type")
            return value
        except (ValueError, OSError, RecursionError) as error:
            errors[path.name] = str(error)
            return fallback

    remote = read_evidence(remote_status, job, dict)
    return {"job_id": job["job_id"], "status": job["status"],
            "controller_timings": read_evidence(timings, job.get("controller_timings", []), list),
            "worker_timings": remote.get("worker_timings", []),
            "transport_timings": read_evidence(directory / "transport-timings.json", job.get("transport_timings", []), list),
            "task_phases": read_evidence(phases, job.get("task_phases", []), list),
            "shutdown_confirmed_at": job.get("shutdown_confirmed_at"), "evidence_errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default=os.environ.get("CODESPACES_RUN_STATE", "~/.local/state/codespaces-run"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("list")
    config = sub.add_parser("configure")
    config.add_argument("--host-repo", help="legacy creation opt-in; new instances use the task repository")
    config.add_argument("--allow-create", action="store_true", default=None, help="allow one Codespace per task repository within the instance limit")
    config.add_argument("--machine")
    config.add_argument("--max-instances", type=int)
    adopt = sub.add_parser("adopt")
    adopt.add_argument("codespace")
    adopt.add_argument("--profile", default="default")
    submit_parser = sub.add_parser("submit")
    task_mode = submit_parser.add_mutually_exclusive_group(required=True)
    task_mode.add_argument("--script", help="one Bash task entrypoint")
    task_mode.add_argument("--stages", help="JSON plan of named Bash stages in --inputs")
    submit_parser.add_argument("--inputs", help="independent task files, checksummed and uploaded once")
    submit_parser.add_argument("--from-stage", help="with --continue-from, validate and reuse the successful prefix")
    submit_parser.add_argument("--source", help="upload the actual local working tree, including uncommitted edits")
    submit_parser.add_argument("--git-url", help="fetch source directly on the worker from an HTTPS Git repository")
    submit_parser.add_argument("--git-commit", help="full immutable commit SHA for --git-url")
    submit_parser.add_argument("--repo", required=True, help="target repository OWNER/REPO; binds the dedicated instance, not credentials")
    submit_parser.add_argument("--codespace")
    submit_parser.add_argument("--profile", default="default")
    submit_parser.add_argument("--continue-from")
    submit_parser.add_argument("--environment-key", help="reuse compatible dependencies for this repo/profile with a fresh source; include runtime and dependency/build compatibility")
    submit_parser.add_argument("--machine", help="exact per-task machine name; otherwise use the configured default")
    submit_parser.add_argument("--cpus", type=int, help="exact CPU count required in API metadata and worker affinity")
    submit_parser.add_argument("--min-memory-mib", type=int, help="minimum usable memory in MiB; allow for OS/container overhead")
    submit_parser.add_argument("--timeout", type=int, default=7200)
    for name in ("status", "logs", "watch", "events", "finish", "cancel", "fetch", "health", "timings"):
        cmd = sub.add_parser(name)
        cmd.add_argument("job_id")
        if name == "health":
            cmd.add_argument("url")
        elif name == "watch":
            cmd.add_argument("--notify-interval", type=int, default=45,
                             help="coalesce progress updates for this many seconds (default: 45)")
        elif name == "events":
            cmd.add_argument("--follow", action="store_true",
                             help="wait for new events and stop at the final receipt")
    stop_parser = sub.add_parser("stop")
    stop_parser.add_argument("codespace")
    args = parser.parse_args()
    store = Store(args.state_dir)
    if args.command == "doctor":
        result = {"gh_installed": bool(shutil.which("gh")), "state_dir": str(store.root)}
        try:
            result["github_login"] = api("user")["login"]
            result["codespaces_api_accessible"] = "codespaces" in api("user/codespaces?per_page=1")
        except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as error:
            result["error"] = str(error)
        emit(result)
    elif args.command == "configure":
        if args.max_instances is not None and args.max_instances < 1:
            raise ValueError("max-instances must be positive")
        if args.host_repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", args.host_repo):
            raise ValueError("host-repo must be OWNER/REPO")
        with store.lock():
            registry = store.load()
            for name in ("host_repo", "machine", "max_instances", "allow_create"):
                value = getattr(args, name)
                if value is not None:
                    registry["config"][name] = value
            if args.host_repo:
                registry["config"]["allow_create"] = True
            store.save(registry)
        emit(registry["config"])
    elif args.command == "adopt":
        valid_name(args.codespace)
        valid_name(args.profile)
        info = api("user/codespaces/" + args.codespace)
        with store.lock():
            registry = store.load()
            if args.codespace in registry["instances"]:
                raise ValueError("instance already registered; leave its lease unchanged")
            repository = info.get("repository", {}).get("full_name")
            if not repository:
                raise ValueError("Codespaces API did not identify the repository")
            if any(value.get("repo", "").casefold() == repository.casefold() for value in registry["instances"].values()):
                raise ValueError("repository already has a registered Codespace")
            registry["instances"][args.codespace] = {"profile": args.profile, "repo": repository, "lease": None,
                "retention_expires_at": info.get("retention_expires_at"), "keep_codespace_needs_check": True}
            store.save(registry)
        emit({"adopted": args.codespace})
    elif args.command == "list":
        emit(store.load())
    elif args.command == "submit":
        valid_name(args.profile)
        result = submit(store, args)
        emit(result)
        if result["status"] in TERMINAL and result["status"] != "succeeded":
            raise SystemExit(1)
        if result["status"] == "unknown":
            raise SystemExit(2)
    elif args.command == "stop":
        stop(store, args.codespace)
        emit({"stop_requested": args.codespace, "state": "Shutdown", "shutdown_confirmed": True})
    else:
        job = store.job(args.job_id)
        if args.command == "timings":
            emit(timing_report(store, job))
        elif args.command == "status":
            emit(status(store, job))
        elif args.command == "watch":
            # The watcher already reports the outcome when it observes it, before
            # collection/shutdown retries. This last record is the collected
            # receipt, so mark it rather than repeat an unlabelled status line.
            result = watch(store, args.job_id, args.notify_interval)
            if result.get("watching"):
                emit(result)
            else:
                # The outcome was already reported above; this is only the
                # collected receipt, so do not repeat the full job record.
                emit(dict({key: result.get(key) for key in ("job_id", "status", "collected",
                                                            "shutdown_confirmed_at", "cleanup_error")},
                          final=True))
        elif args.command == "events":
            sys.exit(events(store, args.job_id, args.follow))
        elif args.command == "finish":
            emit(finish(store, status(store, job)))
        elif args.command == "fetch":
            with timed(store, job, "fetch_startup"):
                ensure_ready(job["codespace"])
            with timed(store, job, "collection"):
                collect(store, job)
            store.save_job(job)
            emit({"artifacts": str(store.job_dir(args.job_id) / "artifacts"), "codespace_left_running": True})
        elif args.command == "logs":
            path = store.job_dir(args.job_id) / "output.log"
            # Prefer collected logs after shutdown; do not wake an instance to read them.
            if path.exists() and job.get("collected"):
                print(path.read_bytes()[-65536:].decode(errors="replace"), end="")
            elif api("user/codespaces/" + job["codespace"])["state"] == "Available":
                print(worker(job["codespace"], "logs", remote_path(args.job_id), 65536), end="")
            else:
                emit({"status": "unavailable", "reason": "instance_not_running", "local_dir": str(path.parent)})
        elif args.command in ("cancel", "health"):
            if api("user/codespaces/" + job["codespace"])["state"] != "Available":
                raise ValueError("instance is not running; it will not be woken for this query")
            emit(worker(job["codespace"], args.command, remote_path(args.job_id), getattr(args, "url", None)))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        emit({"error": str(error)})
        sys.exit(1)
