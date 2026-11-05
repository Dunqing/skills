import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('notification_helpers', Path(__file__).with_name('test_sandbox.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
controller = helpers.controller


def machine():
    return {'name': 'small', 'cpus': 2, 'memory_in_bytes': 8192 * 1024**2}


class NotificationTests(unittest.TestCase):
    def observation(self, phase='build', completed=0, status='running', **extra):
        return dict(job_id='job-test', status=status, phase=phase,
                    progress={'phase': phase, 'completed': completed, 'updated_at': completed}, **extra)

    def test_counters_are_coalesced_and_latest_value_is_published(self):
        gate = controller.ProgressNotifications()
        self.assertTrue(gate.ready(self.observation(completed=0), 0))
        self.assertFalse(gate.ready(self.observation(completed=1), 20))
        self.assertFalse(gate.ready(self.observation(completed=2), 40))
        self.assertTrue(gate.ready(self.observation(completed=3), 60))
        self.assertEqual(gate.last[1]['completed'], 3)

    def test_timestamp_only_updates_never_notify(self):
        gate = controller.ProgressNotifications()
        self.assertTrue(gate.ready(self.observation(), 0))
        changed = self.observation()
        changed['progress'].update(updated_at=999, elapsed_seconds=999, started_at=100)
        self.assertFalse(gate.ready(changed, 999))

    def test_phase_failure_unknown_state_and_errors_notify_immediately(self):
        for changed in [self.observation(phase='validate'), self.observation(status='failed'),
                        self.observation(status='unknown'), self.observation(observation_error='lost SSH'),
                        self.observation(progress_error='invalid progress'),
                        self.observation(reason='transport_error'), self.observation(error='SSH rejected'),
                        self.observation(task_status='interrupted')]:
            gate = controller.ProgressNotifications()
            self.assertTrue(gate.ready(self.observation(), 0))
            self.assertTrue(gate.ready(changed, 1), changed)

    def test_terminal_failure_is_reported_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = controller.Store(directory)
            job = {'job_id': 'job-failed', 'status': 'failed'}
            store.save_job(job)
            events = []
            def finish(*args):
                self.assertEqual(events[0]['status'], 'failed')
                return job
            with patch.object(controller, 'status', return_value=job), patch.object(controller, 'finish', side_effect=finish), \
                 patch.object(controller, 'emit', side_effect=events.append):
                controller.watch(store, job['job_id'])
            self.assertEqual(len(events), 1)

    def test_transport_error_details_reach_watcher_notifications(self):
        with tempfile.TemporaryDirectory() as directory:
            store = controller.Store(directory)
            job = {'job_id': 'job-watch', 'status': 'unknown',
                   'reason': 'transport_error', 'error': 'SSH connection rejected'}
            store.save_job(job)
            terminal = dict(job, status='interrupted')
            events = []
            with patch.object(controller, 'status', side_effect=[job, terminal]), \
                 patch.object(controller, 'finish', return_value=terminal), \
                 patch.object(controller.time, 'sleep'), patch.object(controller, 'emit', side_effect=events.append):
                controller.watch(store, job['job_id'])
            self.assertEqual(events[0].get('reason'), 'transport_error')
            self.assertEqual(events[0].get('error'), 'SSH connection rejected')

    def test_final_record_is_a_marked_receipt_not_a_repeated_status(self):
        with tempfile.TemporaryDirectory() as directory:
            job = {'job_id': 'job-final', 'status': 'succeeded'}
            controller.Store(directory).save_job(job)
            finished = dict(job, collected=True, shutdown_confirmed_at=123, artifact_paths={'x': 'y'})
            events = []
            argv = ['sandbox.py', '--state-dir', directory, 'watch', job['job_id']]
            with patch.object(controller.sys, 'argv', argv), \
                 patch.object(controller, 'watch', return_value=finished), \
                 patch.object(controller, 'emit', side_effect=events.append):
                controller.main()
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0]['final'])
            self.assertTrue(events[0]['collected'])
            self.assertEqual(events[0]['shutdown_confirmed_at'], 123)
            self.assertNotIn('artifact_paths', events[0])

    def test_second_watcher_is_reported_without_a_final_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            controller.Store(directory).save_job({'job_id': 'job-busy', 'status': 'running'})
            events = []
            argv = ['sandbox.py', '--state-dir', directory, 'watch', 'job-busy']
            with patch.object(controller.sys, 'argv', argv), \
                 patch.object(controller, 'watch', return_value={'watching': True, 'reason': 'lock'}), \
                 patch.object(controller, 'emit', side_effect=events.append):
                controller.main()
            self.assertEqual(events, [{'watching': True, 'reason': 'lock'}])

    def test_repeated_cleanup_failure_does_not_toggle_notifications(self):
        with tempfile.TemporaryDirectory() as directory:
            store = controller.Store(directory)
            job = {'job_id': 'job-failed', 'status': 'failed'}
            store.save_job(job)
            events = []
            with patch.object(controller, 'status', return_value=job), \
                 patch.object(controller, 'finish', side_effect=[RuntimeError('network')] * 3 + [job]), \
                 patch.object(controller.time, 'monotonic', return_value=0), patch.object(controller.time, 'sleep'), \
                 patch.object(controller, 'emit', side_effect=events.append):
                controller.watch(store, job['job_id'])
            self.assertEqual(len(events), 2)
            self.assertEqual(events[1]['cleanup_error'], 'network')


class EventStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = controller.Store(self.temp.name)
        self.store.save_job({'job_id': 'job-events', 'status': 'running'})
        self.path = self.store.job_dir('job-events') / 'watch.log'

    def tearDown(self):
        self.temp.cleanup()

    def run_events(self, follow=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = controller.events(self.store, 'job-events', follow)
        return code, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_retained_events_print_without_follow(self):
        self.path.write_text('{"status": "running", "phase": "build"}\n'
                             '{"status": "succeeded", "final": true}\n')
        code, events = self.run_events()
        self.assertEqual(code, 0)
        self.assertEqual([event['status'] for event in events], ['running', 'succeeded'])

    def test_follow_returns_at_the_final_receipt(self):
        self.path.write_text('{"status": "running"}\n{"status": "succeeded", "final": true}\n')
        with patch.object(controller.time, 'sleep') as sleep:
            code, events = self.run_events(follow=True)
        self.assertEqual(code, 0)
        sleep.assert_not_called()
        self.assertTrue(events[-1]['final'])

    def test_follow_reports_a_missing_watcher_instead_of_waiting(self):
        self.path.write_text('{"status": "failed"}\n')
        with patch.object(controller, 'watcher_active', return_value=False), \
             patch.object(controller.time, 'sleep') as sleep:
            code, events = self.run_events(follow=True)
        self.assertEqual(code, 3)
        sleep.assert_not_called()
        self.assertEqual(len(events), 1)

    def test_events_is_wired_to_the_cli(self):
        argv = ['sandbox.py', '--state-dir', self.temp.name, 'events', 'job-events', '--follow']
        with patch.object(controller.sys, 'argv', argv), \
             patch.object(controller, 'events', return_value=0) as stream:
            with self.assertRaises(SystemExit) as stopped:
                controller.main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(stream.call_args.args[1:], ('job-events', True))


class RepositoryInstanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = controller.Store(self.temp.name)
        self.registry = self.store.load()
        self.registry['config'].update(allow_create=True, machine='small')
        self.registry['instances'] = {'other-cs': {'repo': 'example/other', 'profile': 'default', 'lease': None},
                                      'target-cs': {'repo': 'example/repo', 'profile': 'default', 'lease': None}}
        self.args = types.SimpleNamespace(repo='example/repo', profile='default', codespace=None,
                                         cpus=None, min_memory_mib=None, machine=None, timeout=1800)
        self.job = {'job_id': 'job-test'}

    def tearDown(self):
        self.temp.cleanup()

    def test_unrelated_legacy_instance_does_not_block_bound_repository(self):
        self.registry['instances']['orphan'] = {'profile': 'default', 'lease': None}
        def api(endpoint):
            if endpoint == 'user/codespaces/target-cs':
                return {'machine': machine()}
            raise RuntimeError('unrelated instance unavailable')
        with patch.object(controller, 'api', side_effect=api) as request:
            self.assertEqual(controller.select_instance(self.registry, self.args, None, self.job), 'target-cs')
        request.assert_called_once_with('user/codespaces/target-cs')
        self.assertNotIn('repo', self.registry['instances']['orphan'])

    def test_same_repository_reuses_its_instance(self):
        with patch.object(controller, 'api', return_value={'machine': machine()}) as api:
            selected = controller.select_instance(self.registry, self.args, None, self.job)
        self.assertEqual(selected, 'target-cs')
        api.assert_called_once_with('user/codespaces/target-cs')

    def test_explicit_foreign_instance_is_rejected(self):
        self.args.codespace = 'other-cs'
        with patch.object(controller, 'api') as api:
            with self.assertRaisesRegex(ValueError, 'another repository'):
                controller.select_instance(self.registry, self.args, None, self.job)
        api.assert_not_called()

    def test_busy_profile_or_resource_mismatch_never_creates_second_instance(self):
        for kind in ['busy', 'profile', 'resources']:
            target = self.registry['instances']['target-cs']
            target.update(profile='different' if kind == 'profile' else 'default',
                          lease='job-busy' if kind == 'busy' else None)
            actual = dict(machine(), cpus=4) if kind == 'resources' else machine()
            self.args.machine = 'small'
            self.args.cpus = 2 if kind == 'resources' else None
            with patch.object(controller, 'api', return_value={'machine': actual}) as api:
                with self.assertRaises(ValueError):
                    controller.select_instance(self.registry, self.args, None, self.job)
            self.assertTrue(all(len(call.args) == 1 for call in api.call_args_list))
            self.assertEqual(len(self.registry['instances']), 2)

    def test_creation_uses_target_repository_not_legacy_host(self):
        self.registry['instances'].pop('target-cs')
        self.registry['config']['host_repo'] = 'example/old-host'
        replies = [{'machines': [machine()]}, {'name': 'new-cs', 'machine': machine()}]
        with patch.object(controller, 'api', side_effect=replies) as api:
            selected = controller.select_instance(self.registry, self.args, None, self.job)
        self.assertEqual(selected, 'new-cs')
        self.assertEqual(api.call_args_list[0].args[0], 'repos/example/repo/codespaces/machines')
        self.assertEqual(api.call_args_list[1].args[0], 'repos/example/repo/codespaces')
        self.assertEqual(self.registry['instances']['new-cs']['repo'], 'example/repo')
        self.assertIn('other-cs', self.registry['instances'])

    def test_creation_idle_timeout_follows_the_deadline(self):
        self.registry['instances'].pop('target-cs')
        replies = [{'machines': [machine()]}, {'name': 'new-cs', 'machine': machine()}]
        with patch.object(controller, 'api', side_effect=replies) as api:
            controller.select_instance(self.registry, self.args, None, self.job)
        created = next(c for c in api.call_args_list if c.args[0].endswith('/codespaces'))
        self.assertEqual(created.args[1]['idle_timeout_minutes'], 45)

    def test_creation_prefers_recorded_job_deadline(self):
        self.registry['instances'].pop('target-cs')
        self.job['timeout'] = 7200
        replies = [{'machines': [machine()]}, {'name': 'new-cs', 'machine': machine()}]
        with patch.object(controller, 'api', side_effect=replies) as api:
            controller.select_instance(self.registry, self.args, None, self.job)
        created = next(c for c in api.call_args_list if c.args[0].endswith('/codespaces'))
        self.assertEqual(created.args[1]['idle_timeout_minutes'], 135)

    def test_idle_timeout_bounds(self):
        for timeout, expected in [(0, 30), (60, 30), (1800, 45), (1801, 46), (7200, 135), (39600, 240)]:
            self.assertEqual(controller.idle_timeout_minutes(timeout), expected, timeout)

    def test_legacy_binding_uses_actual_repository(self):
        self.registry['instances'] = {'legacy': {'profile': 'default', 'lease': None}}
        self.registry['config']['allow_create'] = False
        info = {'repository': {'full_name': 'example/repo'}, 'machine': machine()}
        with patch.object(controller, 'api', return_value=info) as api:
            self.assertEqual(controller.select_instance(self.registry, self.args, None, self.job), 'legacy')
        self.assertEqual(self.registry['instances']['legacy']['repo'], 'example/repo')
        api.assert_called_once()

    def test_ambiguous_legacy_repository_is_not_guessed(self):
        self.registry['instances'] = {'legacy': {'profile': 'default', 'lease': None}}
        with patch.object(controller, 'api', return_value={'machine': machine()}):
            with self.assertRaisesRegex(ValueError, 'repository metadata'):
                controller.select_instance(self.registry, self.args, None, self.job)

    def test_existing_duplicate_registration_requires_explicit_selection(self):
        self.registry['instances']['duplicate'] = dict(self.registry['instances']['target-cs'])
        with patch.object(controller, 'api') as api:
            with self.assertRaisesRegex(ValueError, 'multiple legacy instances'):
                controller.select_instance(self.registry, self.args, None, self.job)
        api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
