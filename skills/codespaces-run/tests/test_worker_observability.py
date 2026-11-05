import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

WORKER = Path(__file__).resolve().parents[1] / 'scripts' / 'remote_worker.py'
spec = importlib.util.spec_from_file_location('observability_worker', WORKER)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.membership = self.root / 'membership'
        self.membership.write_text('')

    def tearDown(self):
        self.temp.cleanup()

    def test_memtotal_kibibytes_and_unavailable(self):
        path = self.root / 'meminfo'
        path.write_text('MemFree: 10 kB\nMemTotal: 123456 kB\n')
        self.assertEqual(worker.memory_total_bytes(path), 123456 * 1024)
        for text in ('MemTotal: broken kB', 'MemTotal: 0 kB', 'MemTotal: 10 MB', ''):
            path.write_text(text)
            self.assertIsNone(worker.memory_total_bytes(path))
        self.assertIsNone(worker.memory_total_bytes(self.root / 'missing'))

    def test_v2_limits_include_membership_and_ancestors(self):
        nested = self.root / 'parent' / 'child'
        nested.mkdir(parents=True)
        (self.root / 'memory.max').write_text('max\n')
        (nested.parent / 'memory.max').write_text('2048\n')
        (nested / 'memory.max').write_text('4096\n')
        self.membership.write_text('0::/parent/child\n')
        self.assertEqual(worker.cgroup_memory_limit(self.root, self.membership), 2048)
        (nested / 'memory.max').write_text('1024\n')
        self.assertEqual(worker.cgroup_memory_limit(self.root, self.membership), 1024)
        (nested / 'memory.max').write_text('0\n')
        self.assertEqual(worker.cgroup_memory_limit(self.root, self.membership), 0)

    def test_v1_limits_and_unlimited_sentinel(self):
        memory = self.root / 'memory'
        child = memory / 'task'
        child.mkdir(parents=True)
        (memory / 'memory.limit_in_bytes').write_text('9223372036854771712\n')
        (child / 'memory.limit_in_bytes').write_text('3072\n')
        self.membership.write_text('2:cpu,cpuacct:/task\n3:memory:/task\n')
        self.assertEqual(worker.cgroup_memory_limit(self.root, self.membership), 3072)
        (child / 'memory.limit_in_bytes').write_text('9223372036854771712\n')
        self.assertIsNone(worker.cgroup_memory_limit(self.root, self.membership))
        (self.root / 'memory.limit_in_bytes').write_text('2048\n')
        self.assertEqual(worker.cgroup_memory_limit(self.root, self.membership), 2048)

    def test_missing_malformed_and_escaping_cgroups(self):
        self.assertIsNone(worker.cgroup_memory_limit(self.root, self.membership))
        (self.root / 'memory.max').write_text('-1\n')
        self.membership.write_text('invalid\n')
        self.assertIsNone(worker.cgroup_memory_limit(self.root, self.membership))
        self.membership.write_text('0::/../../escape\n')
        (self.root / 'memory.max').write_text('512\n')
        self.assertEqual(worker.cgroup_memory_limit(self.root, self.membership), 512)

    def test_capture_affinity_and_effective_memory(self):
        with patch.object(worker.os, 'cpu_count', return_value=16), \
             patch.object(worker.os, 'sched_getaffinity', return_value={4, 2}, create=True), \
             patch.object(worker, 'memory_total_bytes', return_value=8192), \
             patch.object(worker, 'cgroup_memory_limit', return_value=4096):
            resources = worker.capture_runtime_resources()
        self.assertEqual(resources['cpu_count'], 16)
        self.assertEqual(resources['cpu_affinity'], [2, 4])
        self.assertEqual(resources['available_cpus'], 2)
        self.assertEqual(resources['memory_total_bytes'], 8192)
        self.assertEqual(resources['cgroup_memory_limit_bytes'], 4096)
        self.assertEqual(resources['effective_memory_bytes'], 4096)
        self.assertGreater(resources['captured_at'], 0)

    def test_capture_fallback_and_unknown_memory(self):
        with patch.object(worker.os, 'cpu_count', return_value=4), \
             patch.object(worker.os, 'sched_getaffinity', side_effect=AttributeError, create=True), \
             patch.object(worker, 'memory_total_bytes', return_value=8192), \
             patch.object(worker, 'cgroup_memory_limit', return_value=None):
            resources = worker.capture_runtime_resources()
            self.assertEqual(resources['available_cpus'], 4)
            self.assertIsNone(resources['cpu_affinity'])
            self.assertEqual(resources['effective_memory_bytes'], 8192)
            with patch.object(worker, 'memory_total_bytes', return_value=None):
                self.assertIsNone(worker.capture_runtime_resources()['effective_memory_bytes'])

    def test_requirements_fail_closed_and_cpu_count_is_exact(self):
        resources = {'available_cpus': 4, 'effective_memory_bytes': 8192}
        worker.check_resources(None, {})
        worker.check_resources({'machine': 'api-name', 'cpus': None, 'min_memory_bytes': None}, {})
        worker.check_resources({'cpus': 4, 'min_memory_bytes': 8192}, resources)
        for requirements in ({'cpus': 2}, {'cpus': 8}, {'min_memory_bytes': 8193},
                             {'cpus': True}, {'cpus': 4.0}, {'cpus': 0},
                             {'min_memory_bytes': -1}, {'min_memory_bytes': True}, []):
            with self.subTest(requirements=requirements), self.assertRaises(worker.ResourceMismatch):
                worker.check_resources(requirements, resources)
        for key in ('cpus', 'min_memory_bytes'):
            with self.subTest(key=key), self.assertRaisesRegex(worker.ResourceMismatch, 'unavailable'):
                worker.check_resources({key: 1}, {})


class WorkerObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tasks = Path(self.temp.name) / 'tasks'
        self.tasks.mkdir()
        self.jobs = []
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=10)
        self.temp.cleanup()

    def job(self, script='true\n', requirements=None):
        job = self.tasks / ('job-' + str(len(self.jobs)))
        job.mkdir()
        (job / 'task.sh').write_text(script)
        if requirements is not None:
            worker.write_json(job / 'job.json', {'resource_requirements': requirements})
        self.jobs.append(job)
        return job

    def run_worker(self, job, timeout=5, resources=None):
        # Use a separate process so worker signal handlers cannot affect unittest.
        if resources is None:
            command = [sys.executable, '-I', str(WORKER), 'run', str(job), str(timeout)]
        else:
            code = '''import importlib.util,json,pathlib,sys
spec=importlib.util.spec_from_file_location('worker',sys.argv[1])
worker=importlib.util.module_from_spec(spec); spec.loader.exec_module(worker)
resources=json.loads(sys.argv[3]); worker.capture_runtime_resources=lambda: resources
worker.run(pathlib.Path(sys.argv[2]),int(sys.argv[4]))
'''
            command = [sys.executable, '-I', '-c', code, str(WORKER), str(job), json.dumps(resources), str(timeout)]
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=12)
        return worker.inspect(job)

    def assert_timings(self, state, outcomes):
        timings = state['worker_timings']
        self.assertEqual([record['phase'] for record in timings], list(outcomes))
        for record in timings:
            self.assertEqual(record['outcome'], outcomes[record['phase']])
            self.assertIsInstance(record['started_at'], (float, int))
            self.assertIsInstance(record['finished_at'], (float, int))
            self.assertGreaterEqual(record['duration_seconds'], 0)

    def progress_command(self, phase, **values):
        return '"{}" "$JOB_WORKER" progress "$JOB_DIR" {}\n'.format(
            sys.executable, shlex.quote(json.dumps(dict(values, phase=phase))))

    def test_unmet_resources_never_execute_script(self):
        resources = {'cpu_count': 8, 'available_cpus': 4, 'cpu_affinity': [0, 1, 2, 3],
                     'memory_total_bytes': 8192, 'cgroup_memory_limit_bytes': 4096,
                     'effective_memory_bytes': 4096, 'captured_at': 123}
        for requirements, actual, expected_error in (
                ({'cpus': 8}, resources, 'available_cpus is 4'),
                ({'cpus': 2}, resources, 'available_cpus is 4'),
                ({'min_memory_bytes': 8192}, resources, 'effective_memory_bytes is 4096'),
                ({'cpus': 4}, {}, 'unavailable'),
                ({'min_memory_bytes': 1}, {}, 'unavailable')):
            with self.subTest(requirements=requirements, actual=actual):
                job = self.job('touch "$JOB_DIR/executed"\n', requirements)
                state = self.run_worker(job, resources=actual)
                self.assertEqual(state['status'], 'failed')
                self.assertEqual(state['reason'], 'resource_mismatch')
                self.assertIn(expected_error, state['error'])
                self.assertFalse((job / 'executed').exists())
                self.assertNotIn('task_pid', state)
                self.assertEqual(state['runtime_resources'], actual)
                self.assertEqual(json.loads((job / 'runtime-resources.json').read_text()), actual)
                self.assert_timings(state, {'worker_prepare': 'failed', 'task_cleanup': 'succeeded'})

    def test_resources_and_prepare_are_durable_before_execution(self):
        script = '''"{}" -c 'import json,os,pathlib
job=pathlib.Path(os.environ["JOB_DIR"])
state=json.loads((job/"status.json").read_text())
assert state["runtime_resources"] == json.loads((job/"runtime-resources.json").read_text())
assert state["worker_timings"][0]["outcome"] == "succeeded"
(job/"observed").write_text("yes")'
'''.format(sys.executable)
        resources = {'available_cpus': 2, 'effective_memory_bytes': 4096}
        job = self.job(script, {'cpus': 2, 'min_memory_bytes': 4096, 'machine': 'controller-verified'})
        state = self.run_worker(job, resources=resources)
        self.assertEqual(state['status'], 'succeeded', state)
        self.assertEqual((job / 'observed').read_text(), 'yes')
        self.assert_timings(state, {'worker_prepare': 'succeeded', 'task_execution': 'succeeded',
                                   'task_cleanup': 'succeeded'})

    def test_legacy_job_captures_actual_runtime_without_requirements(self):
        state = self.run_worker(self.job())
        self.assertEqual(state['status'], 'succeeded', state)
        resources = state['runtime_resources']
        self.assertIn('cpu_count', resources)
        self.assertGreater(resources['available_cpus'], 0)
        self.assertIn('cpu_affinity', resources)
        self.assertIn('memory_total_bytes', resources)
        self.assertIn('cgroup_memory_limit_bytes', resources)
        self.assertIn('effective_memory_bytes', resources)

    def test_real_progress_counters_transitions_and_reentry(self):
        job = self.job(self.progress_command('setup', completed=1) +
                       self.progress_command('setup', completed=2) +
                       self.progress_command('test') + self.progress_command('setup', completed=3))
        state = self.run_worker(job)
        self.assertEqual(state['status'], 'succeeded', state)
        phases = state['task_phases']
        self.assertEqual([phase['phase'] for phase in phases], ['setup', 'test', 'setup'])
        self.assertEqual([phase['outcome'] for phase in phases], ['completed', 'completed', 'succeeded'])
        self.assertEqual(phases[0]['ended_at'], phases[1]['started_at'])
        self.assertEqual(phases[1]['ended_at'], phases[2]['started_at'])
        self.assertEqual(state['progress']['completed'], 3)
        self.assertEqual(state['progress']['started_at'], phases[-1]['started_at'])
        for phase in phases:
            self.assertIsNotNone(phase['ended_at'])
            self.assertGreaterEqual(phase['duration_seconds'], 0)

    def test_same_phase_updates_preserve_start_and_use_monotonic_time(self):
        job = self.job()
        with patch.object(worker.time, 'time', side_effect=[100, 90, 80, 70]), \
             patch.object(worker.time, 'monotonic', side_effect=[10, 12, 15, 17]):
            first = worker.update_progress(job, {'phase': 'test', 'completed': 1})
            second = worker.update_progress(job, {'phase': 'test', 'completed': 2})
            worker.update_progress(job, {'phase': 'collect'})
            worker.close_task_phase(job, 'succeeded')
        self.assertEqual(first['started_at'], second['started_at'])
        self.assertEqual(second['elapsed_seconds'], 2)
        phases = worker.task_phases(job)
        self.assertEqual(len(phases), 2)
        self.assertEqual([phase['duration_seconds'] for phase in phases], [5, 2])

    def test_concurrent_progress_updates_are_serialized(self):
        job = self.job()
        processes = [subprocess.Popen([sys.executable, '-I', str(WORKER), 'progress', str(job),
                                      json.dumps({'phase': 'test', 'completed': n})],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for n in range(8)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)['phase'], 'test')
        self.assertEqual(len(worker.task_phases(job)), 1)
        self.assertEqual(json.loads((job / 'progress.json').read_text())['phase'], 'test')
        self.assertTrue((job / 'progress.lock').exists())
        events = [json.loads(line) for line in (job / 'progress-events.jsonl').read_text().splitlines()]
        self.assertEqual(sorted(event['completed'] for event in events), list(range(8)))

    def test_failure_and_timeout_close_phase_and_record_timings(self):
        for script, timeout, outcome in (('exit 7\n', 5, 'failed'), ('sleep 60\n', 1, 'timed_out')):
            with self.subTest(outcome=outcome):
                job = self.job(self.progress_command('work') + script)
                state = self.run_worker(job, timeout=timeout)
                self.assertEqual(state['status'], outcome)
                self.assert_timings(state, {'worker_prepare': 'succeeded', 'task_execution': outcome,
                                           'task_cleanup': 'succeeded'})
                self.assertEqual(state['task_phases'][-1]['outcome'], outcome)
                self.assertIsNotNone(state['task_phases'][-1]['ended_at'])
                if outcome == 'failed':
                    self.assertEqual(state['exit_code'], 7)

    def test_cancel_closes_phase_and_records_execution_timing(self):
        job = self.job(self.progress_command('service-test') + 'sleep 60\n')
        process = subprocess.Popen([sys.executable, '-I', str(WORKER), 'run', str(job), '10'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (job / 'task-phases.json').exists():
                break
            if process.poll() is not None:
                self.fail('worker exited before reporting progress')
            time.sleep(0.02)
        else:
            self.fail('worker did not report progress')
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        state = worker.inspect(job)
        self.assertEqual(state['status'], 'cancelled', state)
        self.assert_timings(state, {'worker_prepare': 'succeeded', 'task_execution': 'cancelled',
                                   'task_cleanup': 'succeeded'})
        self.assertEqual(state['task_phases'][-1]['outcome'], 'cancelled')
        self.assertIsNotNone(state['task_phases'][-1]['ended_at'])

    def test_malformed_progress_and_history_cannot_hide_failure_or_block_cleanup(self):
        for filename, text, error in (('progress.json', '{broken', 'progress_error'),
                                      ('task-phases.json', '{}', 'task_phases_error'),
                                      ('task-phases.json', '[null]', 'task_phases_error'),
                                      ('task-phases.json', '[{"phase":"bad","started_monotonic":"x"}]', 'task_phases_error'),
                                      ('task-phases.json', '[' * 2000, 'task_phases_error'),
                                      ('task-phases.json', '[{"phase":"bad","started_at":' + '9' * 1000 + '}]', 'task_phases_error')):
            with self.subTest(filename=filename, text=text):
                script = self.progress_command('test') + 'printf %s {} > "$JOB_DIR/{}"\nexit 7\n'.format(
                    shlex.quote(text), filename)
                job = self.job(script)
                state = self.run_worker(job)
                self.assertEqual(state['status'], 'failed')
                self.assertEqual(state['exit_code'], 7)
                self.assertIn(error, state)
                self.assert_timings(state, {'worker_prepare': 'succeeded', 'task_execution': 'failed',
                                           'task_cleanup': 'succeeded'})

    def test_cleanup_failure_closes_phase_and_records_failed_timing(self):
        job = self.job(self.progress_command('work'))
        code = '''import importlib.util,pathlib,sys
spec=importlib.util.spec_from_file_location('worker',sys.argv[1])
worker=importlib.util.module_from_spec(spec); spec.loader.exec_module(worker)
def fail(child): raise PermissionError('cleanup denied')
worker.terminate=fail
worker.run(pathlib.Path(sys.argv[2]),5)
'''
        subprocess.run([sys.executable, '-I', '-c', code, str(WORKER), str(job)],
                       check=True, capture_output=True, timeout=10)
        state = worker.inspect(job)
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['reason'], 'cleanup_failed')
        self.assertEqual(state['task_status'], 'succeeded')
        self.assertEqual(state['task_phases'][-1]['outcome'], 'succeeded')
        self.assertIsNotNone(state['task_phases'][-1]['ended_at'])
        self.assert_timings(state, {'worker_prepare': 'succeeded', 'task_execution': 'succeeded',
                                   'task_cleanup': 'failed'})

    def test_worker_timing_duration_is_monotonic_and_durable(self):
        job = self.job()
        state = {'status': 'starting'}
        with patch.object(worker.time, 'time', side_effect=[100, 50]), \
             patch.object(worker.time, 'monotonic', side_effect=[10, 14]):
            with worker.worker_timing(job, state, 'worker_prepare'):
                durable = json.loads((job / 'status.json').read_text())['worker_timings'][0]
                self.assertEqual(durable['started_at'], 100)
                self.assertIsNone(durable['finished_at'])
        record = json.loads((job / 'status.json').read_text())['worker_timings'][0]
        self.assertEqual(record['duration_seconds'], 4)
        self.assertEqual(record['outcome'], 'succeeded')


if __name__ == '__main__':
    unittest.main()
