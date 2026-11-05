import contextlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('security_helpers', Path(__file__).with_name('test_sandbox.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
controller, remote = helpers.controller, helpers.remote


class StatusBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = controller.Store(self.root / 'controller')
        self.remote_job = self.root / 'remote-job'
        self.remote_job.mkdir()
        self.job = {'job_id': 'job-current', 'codespace': 'expected-cs', 'status': 'running',
                    'repo': 'example/repo', 'workspace': '/managed/source', 'env_dir': '/managed/env',
                    'resource_requirements': {'cpus': 2}}
        self.store.save_job(self.job)
        registry = self.store.load()
        registry['instances'] = {'expected-cs': {'lease': 'job-current'}, 'other-cs': {'lease': None}}
        self.store.save(registry)

    def tearDown(self):
        self.temp.cleanup()

    def observe(self, reply):
        (self.remote_job / 'status.json').write_text(json.dumps(reply))
        with patch.object(controller, 'api', return_value={'state': 'Available'}), \
             patch.object(controller, 'worker', side_effect=lambda *args: remote.inspect(self.remote_job)):
            return controller.status(self.store, self.job)

    def test_remote_job_id_cannot_overwrite_another_local_job(self):
        other = {'job_id': 'job-other', 'status': 'running', 'marker': 'preserve'}
        self.store.save_job(other)
        observed = self.observe({'job_id': 'job-other', 'status': 'succeeded'})
        self.assertEqual(self.store.job('job-other'), other)
        self.assertEqual(observed['job_id'], 'job-current')
        self.assertEqual(observed['status'], 'unknown')
        self.assertIn('job_id mismatch', observed['error'])

    def test_remote_metadata_cannot_redirect_cleanup_or_forge_receipts(self):
        observed = self.observe({
            'status': 'succeeded', 'exit_code': 0, 'job_id': 'job-current',
            'codespace': 'other-cs', 'repo': 'other/repo', 'workspace': '/outside/source',
            'env_dir': '/outside/env', 'resource_requirements': {'cpus': 32},
            'codespace_state': 'Shutdown', 'collected': True, 'shutdown_confirmed_at': 123,
            'stop_requested_at': 123, 'collection_transfer': {'sha256': 'forged'},
            'controller_timings': [{'phase': 'forged'}], 'unexpected_field': 'ignore',
            'progress': {'phase': 'validate', 'completed': 1},
            'worker_timings': [{'phase': 'task_execution', 'outcome': 'succeeded'}],
        })
        for key, value in self.job.items():
            if key != 'status':
                self.assertEqual(observed[key], value, key)
        self.assertEqual(observed['status'], 'succeeded')
        self.assertEqual(observed['exit_code'], 0)
        self.assertEqual(observed['phase'], 'validate')
        self.assertEqual(observed['worker_timings'][0]['phase'], 'task_execution')
        self.assertEqual(observed['codespace_state'], 'Available')
        for key in ('collected', 'shutdown_confirmed_at', 'stop_requested_at', 'collection_transfer',
                    'controller_timings', 'unexpected_field'):
            self.assertNotIn(key, observed)
        def collect(store, job):
            self.assertEqual(job['codespace'], 'expected-cs')
            job['collected'] = True
        with patch.object(controller, 'api', side_effect=[{'state': 'Available'}, {'state': 'Available'},
                                                        {'state': 'Shutdown'}]), \
             patch.object(controller, 'remote_busy', return_value=False), \
             patch.object(controller, 'collect', side_effect=collect) as collection, \
             patch.object(controller, 'call', return_value='') as execute:
            finished = controller.finish(self.store, observed)
        collection.assert_called_once()
        execute.assert_called_once_with(['gh', 'codespace', 'stop', '-c', 'expected-cs'])
        self.assertTrue(finished['collected'])
        self.assertNotEqual(finished['shutdown_confirmed_at'], 123)
        self.assertIsNone(self.store.load()['instances']['expected-cs']['lease'])

    def test_malformed_status_does_not_release_or_redirect_the_lease(self):
        for reply in ([], {'status': []}, {'status': 'invalid'}, {'status': 'running', 'progress': 'invalid'}):
            with self.subTest(reply=reply):
                with patch.object(controller, 'api', return_value={'state': 'Available'}), \
                     patch.object(controller, 'worker', return_value=reply):
                    observed = controller.status(self.store, self.job)
                self.assertEqual(observed['status'], 'unknown')
                self.assertEqual(observed['codespace'], 'expected-cs')
                self.assertEqual(self.store.load()['instances']['expected-cs']['lease'], 'job-current')


class GitBoundaryTests(unittest.TestCase):
    def test_fetch_ignores_repository_overrides_and_preserves_auth_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def repository(name, value):
                path = root / name
                subprocess.run(['git', 'init', '-q', str(path)], check=True)
                (path / 'value').write_text(value)
                subprocess.run(['git', '-C', str(path), 'add', 'value'], check=True)
                subprocess.run(['git', '-C', str(path), '-c', 'user.name=Test',
                                '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
                commit = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
                return path, commit
            origin, commit = repository('origin', 'source bytes')
            unrelated, original_commit = repository('unrelated', 'preserve these bytes')
            index = unrelated / '.git/index'
            original_index = index.read_bytes()
            job = root / 'job'
            job.mkdir()
            environment = {
                'GIT_DIR': str(unrelated / '.git'), 'GIT_WORK_TREE': str(unrelated),
                'GIT_COMMON_DIR': str(unrelated / '.git'), 'GIT_INDEX_FILE': str(index),
                'GIT_OBJECT_DIRECTORY': str(unrelated / '.git/objects'),
                'HTTPS_PROXY': 'http://127.0.0.1:9',
                'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.extraHeader',
                'GIT_CONFIG_VALUE_0': 'X-Test: fixture-value',
            }
            original_run = subprocess.run
            def local_transport(command, **kwargs):
                self.assertEqual(kwargs['env']['HTTPS_PROXY'], environment['HTTPS_PROXY'])
                self.assertEqual(kwargs['env']['GIT_CONFIG_VALUE_0'], environment['GIT_CONFIG_VALUE_0'])
                command = list(command)
                if command[-4:-1] == ['remote', 'add', 'origin']:
                    command[-1] = str(origin)
                return original_run(command, **kwargs)
            with patch.dict(os.environ, environment), patch.object(remote.subprocess, 'run', side_effect=local_transport):
                receipt = remote.fetch_source(job, {'url': 'https://example.invalid/repo.git', 'commit': commit}, 30)
            self.assertEqual(receipt['actual_commit'], commit)
            self.assertEqual((job / 'source/value').read_text(), 'source bytes')
            self.assertEqual((unrelated / 'value').read_text(), 'preserve these bytes')
            self.assertEqual(index.read_bytes(), original_index)
            self.assertEqual(subprocess.check_output(['git', '-C', str(unrelated), 'rev-parse', 'HEAD'],
                                                    text=True).strip(), original_commit)
            self.assertEqual(subprocess.check_output(['git', '-C', str(unrelated), 'remote'], text=True), '')


class HealthBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.path == '/redirect':
                    self.send_response(302)
                    # This hostname resolves to loopback but fails the explicit allowlist.
                    # The old handler followed it; the test never contacts an external host.
                    self.send_header('Location', 'http://2130706433:{}/target'.format(self.server.server_port))
                else:
                    self.send_response(200)
                self.end_headers()
            def log_message(self, *args):
                pass
        self.server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def probe(self, path):
        url = 'http://127.0.0.1:{}{}'.format(self.server.server_port, path)
        output = io.StringIO()
        with patch.object(sys, 'argv', ['remote_worker.py', 'health', '/unused', url]), \
             contextlib.redirect_stdout(output):
            remote.main()
        return json.loads(output.getvalue())

    def test_direct_local_health_check_succeeds(self):
        self.assertTrue(self.probe('/ready')['healthy'])
        self.assertEqual(self.requests, ['/ready'])

    def test_redirect_does_not_request_a_disallowed_hostname(self):
        result = self.probe('/redirect')
        self.assertFalse(result['healthy'])
        self.assertEqual(result['http_status'], 302)
        self.assertEqual(self.requests, ['/redirect'])


if __name__ == '__main__':
    unittest.main()
