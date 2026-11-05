#!/usr/bin/env python3
"""Verify one uploaded task bundle, install its worker, then launch once."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time


def bootstrap(root, job_id, bundle, expected_sha256, timeout):
    root, bundle = Path(root), Path(bundle)
    timings = []
    launched = False
    job = None
    record = {"phase": "bundle_unpack", "started_at": time.time(), "outcome": "running"}
    started = time.monotonic()
    timings.append(record)
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", job_id):
            raise ValueError("invalid job identifier")
        digest = hashlib.sha256()
        with bundle.open("rb") as stream:
            for block in iter(lambda: stream.read(1048576), b""):
                digest.update(block)
        if digest.hexdigest() != expected_sha256:
            raise ValueError("input bundle checksum mismatch")
        with tarfile.open(bundle, "r:") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            required = {"remote_worker.py", "task.sh", "job.json"}
            optional = {"source.tar.gz", "inputs.tar.gz", "inputs-manifest.json", "stages.json"}
            if (not required <= set(names) or set(names) - required - optional
                    or ("inputs.tar.gz" in names and "inputs-manifest.json" not in names)
                    or len(names) != len(set(names))
                    or any(not member.isfile() for member in members)):
                raise ValueError("invalid input bundle members")
            # Validate before writes; never overwrite a previous attempt.
            destination = root / "tasks" / job_id
            destination.mkdir(parents=True, exist_ok=False)
            job = destination
            for member in members:
                with archive.extractfile(member) as source, (job / member.name).open("xb") as output:
                    shutil.copyfileobj(source, output)
                (job / member.name).chmod(member.mode & 0o777)
        os.replace(bundle, job / "input-bundle.tar")
        # The job keeps its own worker; the shared copy serves control queries.
        temporary = root / ("remote_worker.py.tmp-" + job_id)
        shutil.copyfile(job / "remote_worker.py", temporary)
        os.replace(temporary, root / "remote_worker.py")
        record.update(outcome="succeeded", finished_at=time.time(),
                      duration_seconds=max(0, time.monotonic() - started))
        (job / "transport-timings.json").write_text(json.dumps(timings))
        record = {"phase": "worker_launch", "started_at": time.time(), "outcome": "running"}
        timings.append(record)
        started = time.monotonic()
        launched = True  # A lost reply must never trigger a second launch.
        reply = subprocess.check_output([sys.executable, "-I", str(job / "remote_worker.py"),
                                         "launch", str(job), str(timeout)], text=True)
        launch = json.loads(reply)
        if not isinstance(launch, dict) or launch.get("status") not in ("starting", "running"):
            raise ValueError("worker did not acknowledge startup: " + reply[-2000:])
        record["outcome"] = "succeeded"
        return {"ok": True, "launch_attempted": True, "launch": launch,
                "transport_timings": timings}
    except Exception as error:
        record.update(outcome="failed", error=str(error))
        return {"ok": False, "launch_attempted": launched, "error": str(error),
                "transport_timings": timings}
    finally:
        record.update(finished_at=time.time(), duration_seconds=max(0, time.monotonic() - started))
        if job is not None:
            (job / "transport-timings.json").write_text(json.dumps(timings))


if __name__ == "__main__":
    print(json.dumps(bootstrap(*sys.argv[1:5], int(sys.argv[5]))))
