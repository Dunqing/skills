import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


controller = load("sandbox")
remote = load("remote_worker")


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tasks = Path(self.temp.name) / "tasks"
        self.tasks.mkdir()
        self.jobs = []

    def tearDown(self):
        for job in self.jobs:
            if (job / "status.json").exists():
                state = remote.inspect(job)
                if state.get("status") not in remote.TERMINAL and state.get("worker_pid"):
                    try:
                        os.kill(state["worker_pid"], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
        self.temp.cleanup()

    def launch(self, script, timeout=10):
        job = self.tasks / ("job-" + str(len(self.jobs)))
        job.mkdir()
        (job / "task.sh").write_text(script)
        self.jobs.append(job)
        subprocess.run([sys.executable, str(SCRIPTS / "remote_worker.py"), "launch", str(job), str(timeout)],
                       check=True, capture_output=True)
        return job

    def wait(self, job, states=None):
        states = states or remote.TERMINAL
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            state = remote.inspect(job)
            if state["status"] in states:
                return state
            time.sleep(0.05)
        self.fail("worker did not reach expected state: " + str(remote.inspect(job)))

    def test_failure_has_real_exit_code_and_dependency_error(self):
        job = self.launch("python3 -c 'import sandbox_definitely_missing_672193'\n")
        state = self.wait(job)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["exit_code"], 1)
        self.assertIn("ModuleNotFoundError", (job / "output.log").read_text())

    def test_timeout_kills_task(self):
        job = self.launch("sleep 60\n", timeout=1)
        state = self.wait(job)
        self.assertEqual(state["status"], "timed_out")
        self.assertIsNone(remote.identity(state["task_pid"]))

    def test_timeout_kills_descendant_that_ignores_sigterm(self):
        job = self.launch("python3 -c 'import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print(os.getpid(),flush=True); time.sleep(60)' &\nwait $!\n", timeout=1)
        self.assertEqual(self.wait(job)['status'], 'timed_out')
        pid = int((job / 'output.log').read_text().strip())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], text=True, capture_output=True)
            if result.returncode != 0 or result.stdout.strip().startswith('Z'):
                return
            time.sleep(0.05)
        self.fail('TERM-ignoring descendant is still alive: ' + result.stdout)

    def test_health_observes_service_and_cancel_ends_it(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        job = self.launch("python3 -m http.server {} --bind 127.0.0.1 &\nwait $!\n".format(port))
        self.wait(job, {"running"})
        command = [sys.executable, str(SCRIPTS / "remote_worker.py")]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            output = subprocess.check_output(command + ["health", str(job), "http://127.0.0.1:{}/".format(port)], text=True)
            if json.loads(output)["healthy"]:
                break
            time.sleep(0.05)
        else:
            self.fail("service never became healthy")
        output = subprocess.check_output(command + ["health", str(job), "http://127.0.0.1:{}/missing".format(port)], text=True)
        self.assertFalse(json.loads(output)["healthy"])
        subprocess.run(command + ["cancel", str(job)], check=True, capture_output=True)
        self.assertEqual(self.wait(job)["status"], "cancelled")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1)

    def test_instance_lease_blocks_second_task(self):
        first = self.launch("sleep 2\n")
        self.wait(first, {"running"})
        second = self.launch("touch should-not-exist\n")
        state = self.wait(second)
        self.assertEqual(state["reason"], "instance_busy")
        self.assertFalse((second / "source" / "should-not-exist").exists())
        self.assertEqual(self.wait(first)["status"], "succeeded")

    def test_lost_worker_is_interrupted_not_running_forever(self):
        job = self.tasks / "lost"
        job.mkdir()
        remote.write_json(job / "status.json", {"status": "running", "worker_pid": os.getpid(),
                                                "worker_identity": "not-this-process"})
        self.assertEqual(remote.inspect(job)["status"], "interrupted")
        self.assertEqual(remote.inspect(self.tasks / "missing")["status"], "unknown")

    def test_archive_traversal_fails_without_writing_outside(self):
        job = self.tasks / "unsafe"
        job.mkdir()
        self.jobs.append(job)
        (job / "task.sh").write_text("true\n")
        with tarfile.open(job / "source.tar.gz", "w:gz") as archive:
            info = tarfile.TarInfo("../escape")
            info.size = 4
            archive.addfile(info, io.BytesIO(b"oops"))
        subprocess.run([sys.executable, str(SCRIPTS / "remote_worker.py"), "launch", str(job), "10"],
                       check=True, capture_output=True)
        state = self.wait(job)
        self.assertEqual(state["status"], "failed")
        self.assertIn("unsafe source", state["error"])
        self.assertFalse((job / "escape").exists())

    def test_task_reports_progress_through_exported_helper(self):
        payload = json.dumps({'phase': 'testing', 'completed': 2, 'total': 4})
        job = self.launch('python3 "$JOB_WORKER" progress "$JOB_DIR" ' + shlex.quote(payload) + '\ntest -f "$JOB_PROGRESS_FILE"\n')
        state = self.wait(job)
        self.assertEqual(state['status'], 'succeeded')
        self.assertEqual(state['progress']['completed'], 2)
        self.assertIn('updated_at', state['progress'])

    def test_continue_reuses_workspace_and_environment(self):
        first = self.launch('echo installed > "$JOB_ENV_DIR/dependency"\necho previous > marker\n')
        self.assertEqual(self.wait(first)["status"], "succeeded")
        second = self.tasks / "continued"
        second.mkdir()
        self.jobs.append(second)
        (second / "job.json").write_text(json.dumps({"workspace": str(first / "source"), "env_dir": str(first / "env")}))
        (second / "task.sh").write_text('cat marker "$JOB_ENV_DIR/dependency"\n')
        subprocess.run([sys.executable, str(SCRIPTS / "remote_worker.py"), "launch", str(second), "10"],
                       check=True, capture_output=True)
        self.assertEqual(self.wait(second)["status"], "succeeded")
        self.assertIn("installed", (second / "output.log").read_text())


FAKE_GH = r'''#!/usr/bin/env python3
import json, os, pathlib, shutil, subprocess, sys
root = pathlib.Path(os.environ["SANDBOX_TEST_ROOT"])
args = sys.argv[1:]
with (root / "calls").open("a") as log: log.write(json.dumps(args) + "\n")
def mapped(path):
    return path.removeprefix("remote:").replace("/workspaces/.agent-codespaces", str(root / "remote")).replace("/workspaces/.codespaces-upload-", str(root / ".codespaces-upload-"))
if args[0] == "api":
    if args[1].endswith("/start"):
        (root / "state").write_text("Available")
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count()
    print(json.dumps({"state": (root / "state").read_text(), "name": "test-cs", "machine": {"name": "test-machine", "cpus": cpus, "memory_in_bytes": 8*1024**3}}))
elif args[:2] == ["codespace", "ssh"]:
    result = subprocess.run(mapped(args[-1]), shell=True)
    sys.exit(result.returncode)
elif args[:2] == ["codespace", "cp"]:
    src, dst = map(mapped, args[-2:])
    if pathlib.Path(src).is_dir(): shutil.copytree(src, dst, dirs_exist_ok=True)
    else: shutil.copyfile(src, dst)
elif args[:2] == ["codespace", "stop"]:
    (root / "state").write_text("Shutdown")
else:
    sys.exit("unexpected command: " + repr(args))
'''


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = controller.Store(self.root / "state-dir")

    def tearDown(self):
        self.temp.cleanup()

    def registry(self, lease=None):
        data = self.store.load()
        data["instances"]["test-cs"] = {"profile": "default", "repo": "example/repo", "lease": lease}
        self.store.save(data)

    def checkout(self):
        source = self.root / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        (source / "tracked").write_text("original")
        (source / "deleted").write_text("delete me")
        (source / ".gitignore").write_text("ignored\n")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "initial"], check=True)
        (source / "tracked").write_text("dirty")
        (source / "deleted").unlink()
        (source / "untracked").write_text("new")
        (source / "ignored").write_text("do not ship")
        return source

    def test_working_tree_snapshot_preserves_changes(self):
        source = self.checkout()
        metadata = controller.snapshot(source, self.root / "source.tar.gz")
        self.assertTrue(metadata["dirty"])
        with tarfile.open(self.root / "source.tar.gz") as archive:
            self.assertEqual(archive.extractfile("tracked").read(), b"dirty")
            self.assertIn("untracked", archive.getnames())
            self.assertNotIn("deleted", archive.getnames())
            self.assertNotIn("ignored", archive.getnames())

    def test_configure_preserves_unspecified_budget_and_creation_policy(self):
        registry = self.store.load()
        registry['config'].update(host_repo='example/host', allow_create=True, max_instances=1, machine='old')
        self.store.save(registry)
        args = ['sandbox.py', '--state-dir', str(self.store.root), 'configure', '--machine', 'new']
        with patch.object(sys, 'argv', args), patch.object(controller, 'emit'):
            controller.main()
        config = self.store.load()['config']
        self.assertEqual(config['machine'], 'new')
        self.assertEqual(config['max_instances'], 1)
        self.assertTrue(config['allow_create'])
        self.assertEqual(config['host_repo'], 'example/host')

    def test_transport_failure_is_unknown_and_does_not_stop(self):
        job = {"job_id": "job-test", "codespace": "test-cs", "status": "running"}
        with patch.object(controller, "api", side_effect=RuntimeError("network failure")), patch.object(controller, "call") as execute:
            state = controller.status(self.store, job)
        self.assertEqual(state["status"], "unknown")
        execute.assert_not_called()

    def test_known_success_survives_later_api_outage(self):
        job = {"job_id": "job-done", "codespace": "test-cs", "status": "succeeded", "exit_code": 0}
        with patch.object(controller, "api", side_effect=RuntimeError("network failure")):
            state = controller.status(self.store, job)
        self.assertEqual(state["status"], "succeeded")
        self.assertIn("network failure", state["observation_error"])

    def test_watcher_retries_cleanup_after_transient_failure(self):
        job = {"job_id": "job-watch", "codespace": "test-cs", "status": "failed", "exit_code": 1}
        self.store.save_job(job)
        with patch.object(controller, "status", return_value=job), \
             patch.object(controller, "finish", side_effect=[RuntimeError("temporary outage"), job]) as finish, \
             patch.object(controller.time, "sleep"), patch.object(controller, "emit"):
            self.assertEqual(controller.watch(self.store, job["job_id"]), job)
        self.assertEqual(finish.call_count, 2)

    def test_stop_refuses_active_and_unmanaged_instances(self):
        self.registry("job-busy")
        with patch.object(controller, "call") as execute:
            with self.assertRaisesRegex(ValueError, "active or unresolved"):
                controller.stop(self.store, "test-cs")
            with self.assertRaisesRegex(ValueError, "unmanaged"):
                controller.stop(self.store, "someone-elses-cs")
        execute.assert_not_called()

    def test_idle_adopted_instance_can_stop_before_worker_upload(self):
        self.registry()
        with patch.object(controller, "api", side_effect=[{"state": "Available"}, {"state": "Shutdown"}]), \
             patch.object(controller, "remote_busy", return_value=False), \
             patch.object(controller, "call", return_value="") as execute:
            controller.stop(self.store, "test-cs")
        execute.assert_called_once_with(["gh", "codespace", "stop", "-c", "test-cs"])

    def test_submit_collect_stop_with_real_worker_and_fake_github(self):
        self.registry()
        source = self.checkout()
        script = self.root / "task.sh"
        script.write_text('set -eu\ncat tracked\necho result > "$ARTIFACTS_DIR/metrics.json"\n')
        (self.root / "state").write_text("Shutdown")
        executable = self.root / "gh"
        executable.write_text(FAKE_GH)
        executable.chmod(0o755)
        args = types.SimpleNamespace(script=str(script), source=str(source), repo="example/repo",
                                     timeout=10, profile="default", codespace=None, continue_from=None)
        with patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                                     "SANDBOX_TEST_ROOT": str(self.root)}), patch.object(controller, "spawn_watch", return_value=0):
            job = controller.submit(self.store, args)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                state = controller.status(self.store, self.store.job(job["job_id"]))
                if state["status"] in controller.TERMINAL:
                    break
                time.sleep(0.05)
            self.assertEqual(state["status"], "succeeded", state)
            controller.finish(self.store, state)
        self.assertEqual((self.root / "state").read_text(), "Shutdown")
        self.assertIsNone(self.store.load()["instances"]["test-cs"]["lease"])
        output = self.store.job_dir(job["job_id"])
        self.assertEqual((output / "artifacts" / "metrics.json").read_text(), "result\n")
        self.assertEqual((output / "output.log").read_text(), "dirty")
        calls = [json.loads(line) for line in (self.root / "calls").read_text().splitlines()]
        self.assertTrue(any(command[:2] == ["codespace", "stop"] for command in calls))
        self.assertFalse(any("delete" in command or "DELETE" in command for command in calls))
        final = self.store.job(job['job_id'])
        report = controller.timing_report(self.store, final)
        self.assertEqual({r['phase'] for r in report['controller_timings']},
                         {'snapshot','resource_selection','pack','startup','resource_verification','upload','bootstrap','collection','shutdown'})
        uploads=[c for c in calls if c[:2]==['codespace','cp'] and not c[-2].startswith('remote:')]
        self.assertEqual(len(uploads),1)
        self.assertEqual({r['phase'] for r in report['transport_timings']},{'bundle_unpack','worker_launch'})
        self.assertTrue(all(r['outcome']=='succeeded' and r['duration_seconds']>=0
                            for r in report['controller_timings']))
        self.assertEqual({r['phase'] for r in report['worker_timings']},
                         {'worker_prepare','task_execution','task_cleanup'})
        runtime=json.loads((output/'runtime-resources.json').read_text())
        self.assertEqual(runtime['available_cpus'],final['resource_requirements']['cpus'])
        self.assertIn('controller_timings', controller.status(self.store, final))

    def test_remote_git_submit_uses_no_source_upload_and_preserves_exact_commit(self):
        self.registry()
        origin=self.checkout()
        commit=subprocess.check_output(['git','-C',str(origin),'rev-parse','HEAD'],text=True).strip()
        script=self.root/'git-task.sh';script.write_text('cat tracked\n')
        (self.root/'state').write_text('Shutdown')
        executable=self.root/'gh';executable.write_text(FAKE_GH);executable.chmod(0o755)
        args=types.SimpleNamespace(script=str(script),source=None,git_url='https://example.invalid/repo.git',git_commit=commit,
                                   repo='example/repo',timeout=10,profile='default',codespace=None,continue_from=None)
        env={'PATH':str(self.root)+os.pathsep+os.environ['PATH'],'SANDBOX_TEST_ROOT':str(self.root),
             'GIT_CONFIG_COUNT':'2','GIT_CONFIG_KEY_0':'url.'+origin.as_uri()+'.insteadOf',
             'GIT_CONFIG_VALUE_0':'https://example.invalid/repo.git',
             'GIT_CONFIG_KEY_1':'protocol.file.allow','GIT_CONFIG_VALUE_1':'always'}
        with patch.dict(os.environ,env), patch.object(controller,'spawn_watch',return_value=0):
            job=controller.submit(self.store,args)
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                state=controller.status(self.store,self.store.job(job['job_id']))
                if state['status'] in controller.TERMINAL:
                    break
                time.sleep(.05)
            self.assertEqual(state['status'],'succeeded',state)
            controller.finish(self.store,state)
        output=self.store.job_dir(job['job_id'])
        self.assertEqual((output/'output.log').read_text(),'original')
        self.assertEqual(json.loads((output/'source-provenance.json').read_text())['actual_commit'],commit)
        self.assertNotIn('source.tar.gz',job['input_transfer']['members'])
        self.assertFalse((output/'source.tar.gz').exists())
        self.assertTrue(any(r['phase']=='source_fetch' for r in state['worker_timings']))
        self.assertEqual((self.root/'state').read_text(),'Shutdown')


if __name__ == "__main__":
    unittest.main()
