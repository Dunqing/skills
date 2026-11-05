import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('resource_helpers', Path(__file__).with_name('test_sandbox.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
controller = helpers.controller


def machine(name='small', cpus=2, memory=8192):
    return {'machine': {'name': name, 'cpus': cpus, 'memory_in_bytes': memory*1024**2}, 'state': 'Shutdown'}


class ResourceAndTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = controller.Store(self.root/'state')
        self.script = self.root/'task.sh'
        self.script.write_text('true\n')
        self.args = types.SimpleNamespace(script=str(self.script), source='source', repo='example/repo', timeout=30,
                                         profile='default', codespace=None, continue_from=None,
                                         machine=None, cpus=None, min_memory_mib=None)
        self.registry = self.store.load()
        self.registry['config']['machine'] = 'small'
        self.registry['instances'] = {'test-cs': {'profile': 'default', 'repo': 'example/repo', 'lease': None}}
        self.store.save(self.registry)
        self.job = {'job_id': 'job-test', 'status': 'preparing'}

    def tearDown(self):
        self.temp.cleanup()

    def test_default_machine_drift_rejected_before_start_or_lease(self):
        with patch.object(controller, 'api', return_value=machine('large', 4)), \
             patch.object(controller, 'snapshot', return_value={}), \
             patch.object(controller, 'ensure_ready') as start, patch.object(controller, 'call') as call:
            job = controller.submit(self.store, self.args)
        self.assertEqual(job['status'], 'failed')
        self.assertIn('resource mismatch', job['error'])
        start.assert_not_called()
        call.assert_not_called()
        self.assertIsNone(self.store.load()['instances']['test-cs']['lease'])
        self.assertEqual(self.store.load()['instances']['test-cs']['observed_machine']['cpus'], 4)
        self.assertEqual(controller.status(self.store, job)['status'], 'failed')
        self.assertEqual(job['controller_timings'][-1]['outcome'], 'failed')

    def test_explicit_machine_reuses_existing_environment_identity(self):
        self.args.machine = 'large'
        self.args.cpus = 4
        first, _ = controller.environment_for(self.registry['instances']['test-cs'], 'example/repo', 'default', 'rust-v1', 'job-first')
        with patch.object(controller, 'api', return_value=machine('large',4)):
            cs = controller.select_instance(self.registry, self.args, None, self.job)
        again, reused = controller.environment_for(self.registry['instances'][cs], 'example/repo', 'default', 'rust-v1', 'job-second')
        self.assertTrue(reused)
        self.assertEqual(first, again)
        self.assertEqual(self.job['resource_requirements']['cpus'], 4)

    def test_capacity_request_overrides_default_machine_but_named_machine_does_not(self):
        self.args.cpus = 4
        self.args.min_memory_mib = 12000
        self.assertIsNone(controller.requested_resources(self.args, self.registry['config'])['machine'])
        self.args.machine = 'small'
        requirements = controller.requested_resources(self.args, self.registry['config'])
        errors = controller.resource_errors(machine()['machine'], requirements)
        self.assertEqual(len(errors), 2)

    def test_cpu_only_request_resolves_matching_machine_without_changing_default(self):
        self.args.cpus=4
        self.registry['config']['host_repo']='example/host'
        replies=[{'machines':[machine()['machine'],machine('large',4,16384)['machine']]},machine('large',4,16384)]
        with patch.object(controller,'api',side_effect=replies):
            cs=controller.select_instance(self.registry,self.args,None,self.job)
        self.assertEqual(cs,'test-cs')
        self.assertEqual(self.job['requested_resources']['machine'],'large')
        self.assertEqual(self.registry['config']['machine'],'small')

    def test_named_machine_with_wrong_cpu_count_is_rejected(self):
        self.args.cpus = 4
        self.args.machine = 'small'
        with patch.object(controller, 'api', return_value=machine()):
            with self.assertRaisesRegex(ValueError, 'CPUs 2 != 4'):
                controller.select_instance(self.registry, self.args, None, self.job)

    def test_minimum_memory_is_enforced(self):
        self.args.min_memory_mib = 12000
        with patch.object(controller, 'api', return_value=machine()):
            with self.assertRaisesRegex(ValueError, 'memory'):
                controller.select_instance(self.registry, self.args, None, self.job)

    def test_foreign_repository_candidate_is_not_reused(self):
        self.registry['instances']['test-cs']['repo'] = 'example/other'
        self.registry['instances']['second'] = {'profile':'default','repo':'example/repo','lease':None}
        with patch.object(controller, 'api', return_value=machine()) as api:
            self.assertEqual(controller.select_instance(self.registry,self.args,None,self.job),'second')
        api.assert_called_once_with('user/codespaces/second')

    def test_missing_machine_metadata_fails_closed_without_start(self):
        with patch.object(controller, 'api', return_value={'state':'Shutdown'}), \
             patch.object(controller, 'snapshot', return_value={}), patch.object(controller, 'ensure_ready') as start:
            job = controller.submit(self.store,self.args)
        self.assertEqual(job['status'],'failed')
        self.assertIn('machine specification',job['error'])
        start.assert_not_called()
        self.assertIsNone(self.store.load()['instances']['test-cs']['lease'])

    def test_api_failure_does_not_create_replacement(self):
        with patch.object(controller,'api',side_effect=RuntimeError('API unavailable')) as api:
            with self.assertRaisesRegex(RuntimeError,'unavailable'):
                controller.select_instance(self.registry,self.args,None,self.job)
        self.assertEqual(api.call_count,1)
        self.assertEqual(len(self.registry['instances']),1)

    def test_continuation_cannot_bypass_resource_policy(self):
        previous={'codespace':'test-cs'}
        with patch.object(controller,'api',return_value=machine('large',4)):
            with self.assertRaisesRegex(ValueError,'resource mismatch'):
                controller.select_instance(self.registry,self.args,previous,self.job)

    def test_resource_change_after_start_never_launches_task(self):
        states = iter([machine(), machine('large',4)])
        def observe(_):
            return next(states)
        with patch.object(controller,'api',side_effect=observe), \
             patch.object(controller,'snapshot',return_value={}), patch.object(controller,'ensure_ready'), \
             patch.object(controller,'stop') as stop, patch.object(controller,'worker') as worker, \
             patch.object(controller,'call') as call:
            job=controller.submit(self.store,self.args)
        self.assertEqual(job['status'],'failed')
        self.assertIn('before launch',job['error'])
        worker.assert_not_called()
        call.assert_not_called()
        stop.assert_called_once_with(self.store,'test-cs',expected_job=job['job_id'])
        self.assertIn('shutdown_confirmed_at',job)

    def test_creation_validates_contradictory_requirements_before_post(self):
        self.registry['instances']={}
        self.registry['config']['host_repo']='example/host'
        self.args.machine='small'
        self.args.cpus=4
        with patch.object(controller,'api',return_value={'machines':[machine()['machine']]}) as api:
            with self.assertRaisesRegex(ValueError,'no available machine'):
                controller.select_instance(self.registry,self.args,None,self.job)
        self.assertEqual(api.call_count,1)
        self.assertEqual(api.call_args.args,('repos/example/repo/codespaces/machines',))

    def test_incomplete_creation_reply_still_retains_instance_and_confirms_stop(self):
        self.registry['instances']={}
        self.registry['config']['host_repo']='example/host'
        self.store.save(self.registry)
        replies=[{'machines':[machine()['machine']]},{'name':'new-cs'}]
        with patch.object(controller,'api',side_effect=replies), patch.object(controller,'snapshot',return_value={}), \
             patch.object(controller,'stop') as stop:
            job=controller.submit(self.store,self.args)
        self.assertEqual(job['status'],'failed')
        self.assertEqual(job['codespace'],'new-cs')
        self.assertIn('new-cs',self.store.load()['instances'])
        stop.assert_called_once_with(self.store,'new-cs',expected_job=job['job_id'])
        self.assertIn('shutdown_confirmed_at',job)

    def test_smallest_matching_machine_is_default_for_host(self):
        self.registry['config'].update(host_repo='example/host',machine=None)
        replies=[{'machines':[machine('large',4,16384)['machine'],machine()['machine']]},machine('large',4)]
        with patch.object(controller,'api',side_effect=replies):
            with self.assertRaisesRegex(ValueError,'machine large != small'):
                controller.select_instance(self.registry,self.args,None,self.job)

    def test_timing_attempts_preserve_failures_retries_and_monotonic_duration(self):
        with patch.object(controller.time,'time',side_effect=[100,90,80,70]), \
             patch.object(controller.time,'monotonic',side_effect=[5,8,10,12]):
            with self.assertRaisesRegex(RuntimeError,'transient'):
                with controller.timed(self.store,self.job,'collection'):
                    raise RuntimeError('transient')
            with controller.timed(self.store,self.job,'collection'):
                pass
        self.store.save_job(self.job)
        stale=dict(self.job,controller_timings=[])
        self.store.save_job(stale)
        history=self.store.job('job-test')['controller_timings']
        self.assertEqual([r['outcome'] for r in history],['failed','succeeded'])
        self.assertEqual([r['duration_seconds'] for r in history],[3,2])

    def test_failed_shutdown_is_timed_without_releasing_lease(self):
        self.job.update(status='succeeded',codespace='test-cs',collected=True)
        self.registry['instances']['test-cs']['lease']='job-test'
        self.store.save(self.registry)
        with patch.object(controller,'api',return_value={'state':'Available'}), \
             patch.object(controller,'stop',side_effect=RuntimeError('not confirmed')):
            with self.assertRaisesRegex(RuntimeError,'not confirmed'):
                controller.finish(self.store,self.job)
        report=controller.timing_report(self.store,self.job)
        self.assertEqual(report['controller_timings'][-1]['outcome'],'failed')
        self.assertIsNone(report['shutdown_confirmed_at'])
        self.assertEqual(self.store.load()['instances']['test-cs']['lease'],'job-test')

    def test_offline_timing_report_does_not_call_github(self):
        self.store.save_job(self.job)
        directory=self.store.job_dir('job-test')
        (directory/'status.json').write_text(json.dumps({'worker_timings':[{'phase':'task_execution'}]}))
        (directory/'task-phases.json').write_text(json.dumps([{'phase':'testing'}]))
        with patch.object(controller,'api') as api:
            report=controller.timing_report(self.store,self.job)
        api.assert_not_called()
        self.assertEqual(report['worker_timings'][0]['phase'],'task_execution')
        self.assertEqual(report['task_phases'][0]['phase'],'testing')

    def test_corrupt_task_phase_evidence_does_not_hide_lifecycle_report(self):
        self.job['status']='failed'
        self.store.save_job(self.job)
        with controller.timed(self.store,self.job,'collection'):
            pass
        (self.store.job_dir('job-test')/'task-phases.json').write_text('{broken')
        report=controller.timing_report(self.store,self.job)
        self.assertEqual(report['status'],'failed')
        self.assertEqual(report['controller_timings'][0]['phase'],'collection')
        self.assertIn('task-phases.json',report['evidence_errors'])

    def test_invalid_resource_values_rejected(self):
        for key,value in [('cpus',0),('cpus',-1),('min_memory_mib',0)]:
            with self.subTest(key=key,value=value):
                args=types.SimpleNamespace(machine=None,cpus=None,min_memory_mib=None)
                setattr(args,key,value)
                with self.assertRaisesRegex(ValueError,'positive integer'):
                    controller.requested_resources(args,self.registry['config'])


if __name__ == '__main__':
    unittest.main()
