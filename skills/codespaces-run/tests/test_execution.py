import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('sandbox_test_helpers', Path(__file__).with_name('test_sandbox.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
controller = helpers.controller
remote = helpers.remote


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = controller.Store(self.root / 'state-dir')

    def tearDown(self):
        self.temp.cleanup()

    def test_environment_cache_isolated_by_repository_profile_and_key(self):
        instance = {}
        first, reused = controller.environment_for(instance, 'example/python', 'linux', 'python3.12-lock123', 'job-first')
        self.assertFalse(reused)
        second, reused = controller.environment_for(instance, 'example/python', 'linux', 'python3.12-lock123', 'job-second')
        self.assertTrue(reused)
        self.assertEqual(first, second)
        for repo, profile, key in [('example/node', 'linux', 'python3.12-lock123'),
                                   ('example/python', 'other', 'python3.12-lock123'),
                                   ('example/python', 'linux', 'python3.13-lock456')]:
            with self.subTest(repo=repo, profile=profile, key=key):
                path, reused = controller.environment_for(instance, repo, profile, key, 'job-third')
                self.assertFalse(reused)
                self.assertNotEqual(first, path)

    def test_reuse_cannot_escape_managed_environment_directories(self):
        instance = {}
        controller.environment_for(instance, 'example/python', 'linux', 'key', 'job-first')
        next(iter(instance['environments'].values()))['env_dir'] = '/tmp/unmanaged/env'
        with self.assertRaisesRegex(ValueError, 'outside managed'):
            controller.environment_for(instance, 'example/python', 'linux', 'key', 'job-second')

    def test_finish_keeps_lease_until_shutdown_is_confirmed(self):
        registry = self.store.load()
        registry['instances']['test-cs'] = {'profile': 'default', 'lease': 'job-test'}
        self.store.save(registry)
        states = iter(['Available', 'Available', 'ShuttingDown', 'Shutdown'])
        def observe(_):
            self.assertEqual(self.store.load()['instances']['test-cs']['lease'], 'job-test')
            return {'state': next(states)}
        job = {'job_id': 'job-test', 'codespace': 'test-cs', 'status': 'succeeded', 'collected': True}
        with patch.object(controller, 'api', side_effect=observe), patch.object(controller, 'call'), \
             patch.object(controller, 'remote_busy', return_value=False), patch.object(controller.time, 'sleep'):
            controller.finish(self.store, job)
        self.assertIsNone(self.store.load()['instances']['test-cs']['lease'])
        self.assertIn('shutdown_confirmed_at', job)

    def test_unconfirmed_shutdown_does_not_release_lease(self):
        registry = self.store.load()
        registry['instances']['test-cs'] = {'profile': 'default', 'lease': 'job-test'}
        self.store.save(registry)
        with patch.object(controller, 'api', return_value={'state': 'ShuttingDown'}), \
             patch.object(controller.time, 'monotonic', side_effect=[0, 91]), patch.object(controller, 'call') as execute:
            with self.assertRaisesRegex(RuntimeError, 'not confirmed'):
                controller.stop(self.store, 'test-cs', expected_job='job-test')
        execute.assert_not_called()
        self.assertEqual(self.store.load()['instances']['test-cs']['lease'], 'job-test')

    def test_progress_does_not_replace_terminal_outcome(self):
        job = self.root / 'job'
        job.mkdir()
        remote.write_json(job / 'status.json', {'status': 'succeeded', 'started_at': 10, 'finished_at': 20})
        remote.write_json(job / 'progress.json', {'phase': 'testing', 'completed': 4, 'total': 10})
        state = remote.inspect(job)
        self.assertEqual(state['status'], 'succeeded')
        self.assertEqual(state['progress']['completed'], 4)
        self.assertEqual(state['elapsed_seconds'], 10)
        (job / 'progress.json').write_text('{broken')
        state = remote.inspect(job)
        self.assertEqual(state['status'], 'succeeded')
        self.assertIn('progress_error', state)

    def test_cleanup_failure_still_records_a_terminal_result(self):
        job = self.root / 'job'
        job.mkdir()
        (job / 'task.sh').write_text('true\n')
        code = '''import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location('worker',sys.argv[1]); worker=importlib.util.module_from_spec(spec); spec.loader.exec_module(worker)
def fail(child): raise PermissionError('simulated cleanup failure')
worker.terminate=fail
worker.run(pathlib.Path(sys.argv[2]),5)
'''
        subprocess.run([sys.executable, '-I', '-c', code, str(helpers.SCRIPTS/'remote_worker.py'), str(job)], check=True, timeout=10)
        state = remote.inspect(job)
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['reason'], 'cleanup_failed')
        self.assertEqual(state['task_status'], 'succeeded')
        self.assertIn('finished_at', state)

    def test_upload_failure_confirms_shutdown_before_releasing_lease(self):
        registry=self.store.load()
        registry['instances']['test-cs']={'profile':'default','repo':'example/repo','lease':None}
        self.store.save(registry)
        script=self.root/'task.sh'
        script.write_text('true\n')
        args=types.SimpleNamespace(script=str(script),source='unused',repo='example/repo',timeout=10,profile='default',codespace=None,continue_from=None)
        state={'state':'Available','machine':{'name':'test-machine','cpus':2,'memory_in_bytes':8*1024**3}}
        def execute(command,*args,**kwargs):
            if command[:3]==['gh','codespace','cp']:
                raise RuntimeError('upload failed')
            if command[:3]==['gh','codespace','stop']:
                self.assertIsNotNone(self.store.load()['instances']['test-cs']['lease'])
                state['state']='Shutdown'
                return ''
            raise AssertionError(command)
        with patch.object(controller,'snapshot',return_value={}), patch.object(controller,'ensure_ready'), \
             patch.object(controller,'remote_busy',return_value=False), \
             patch.object(controller,'ssh',return_value=''), patch.object(controller,'call',side_effect=execute), \
             patch.object(controller,'api',side_effect=lambda endpoint:dict(state)), patch.object(controller,'spawn_watch') as watch:
            job=controller.submit(self.store,args)
        self.assertEqual(job['status'],'failed')
        self.assertIn('shutdown_confirmed_at',job)
        self.assertIsNone(self.store.load()['instances']['test-cs']['lease'])
        watch.assert_not_called()

    def test_invalid_collection_still_stops_and_preserves_error(self):
        job={'job_id':'job-test','codespace':'test-cs','status':'succeeded'}
        self.store.save_job(job)
        with patch.object(controller,'api',return_value={'state':'Available'}), \
             patch.object(controller,'collect',side_effect=ValueError('unsafe archive')), patch.object(controller,'stop') as stop:
            result=controller.finish(self.store,job)
        self.assertEqual(result['collection_error'],'unsafe archive')
        self.assertFalse(result.get('collected',False))
        stop.assert_called_once_with(self.store,'test-cs',expected_job='job-test')

    def test_artifact_archive_rejects_traversal_and_bad_checksum(self):
        import hashlib
        import io
        import shutil
        import tarfile
        payload = self.root/'payload.tar.gz'
        with tarfile.open(payload,'w:gz') as archive:
            member=tarfile.TarInfo('../escape')
            member.size=4
            archive.addfile(member,io.BytesIO(b'oops'))
        job={'job_id':'job-artifacts','codespace':'test-cs','status':'succeeded'}
        self.store.save_job(job)
        metadata={'sha256':hashlib.sha256(payload.read_bytes()).hexdigest(),'bytes':payload.stat().st_size}
        def download(args,timeout):
            shutil.copyfile(payload,args[-1])
        with patch.object(controller,'ssh',return_value=json.dumps(metadata)), patch.object(controller,'call',side_effect=download):
            with self.assertRaisesRegex(ValueError,'unsafe artifact archive'):
                controller.collect(self.store,job)
        self.assertFalse((self.store.job_dir(job['job_id'])/'escape').exists())
        metadata['sha256']='wrong'
        with patch.object(controller,'ssh',return_value=json.dumps(metadata)), patch.object(controller,'call',side_effect=download):
            with self.assertRaisesRegex(ValueError,'checksum mismatch'):
                controller.collect(self.store,job)
        self.assertFalse(job.get('collected',False))

    def test_submit_cli_returns_failure_exit_code(self):
        args = ['sandbox.py', '--state-dir', str(self.root/'state-dir'), 'submit', '--repo', 'example/repo', '--script', 'unused']
        with patch.object(sys, 'argv', args), patch.object(controller, 'submit', return_value={'status': 'failed'}), patch.object(controller, 'emit'):
            with self.assertRaises(SystemExit) as result:
                controller.main()
        self.assertEqual(result.exception.code, 1)

    def test_two_fresh_snapshots_share_environment_without_sharing_source(self):
        registry = self.store.load()
        registry['instances']['test-cs'] = {'profile': 'default', 'repo': 'example/python', 'lease': None}
        self.store.save(registry)
        source = self.root/'source'
        source.mkdir()
        subprocess.run(['git', 'init', '-q', str(source)],check=True)
        (source/'value').write_text('first')
        subprocess.run(['git','-C',str(source),'add','.'],check=True)
        subprocess.run(['git','-C',str(source),'-c','user.name=Test','-c','user.email=test@localhost','commit','-qm','source'],check=True)
        (self.root/'state').write_text('Shutdown')
        executable=self.root/'gh'
        executable.write_text(helpers.FAKE_GH)
        executable.chmod(0o755)
        script=self.root/'task.sh'
        script.write_text('test -f "$JOB_ENV_DIR/dependency" || printf installed > "$JOB_ENV_DIR/dependency"\npython3 -c \'import os,pathlib; pathlib.Path(os.environ["ARTIFACTS_DIR"],"result").write_text(pathlib.Path("value").read_text()+pathlib.Path(os.environ["JOB_ENV_DIR"],"dependency").read_text())\'\n')
        args=types.SimpleNamespace(script=str(script),source=str(source),repo='example/python',timeout=10,profile='default',codespace=None,continue_from=None,environment_key='python3.12-lock123')
        jobs=[]
        with patch.dict(os.environ,{'PATH':str(self.root)+os.pathsep+os.environ['PATH'],'SANDBOX_TEST_ROOT':str(self.root)}), \
             patch.object(controller,'ROOT',str(self.root/'remote')), patch.object(controller,'spawn_watch',return_value=0):
            for value in ['first','second']:
                (source/'value').write_text(value)
                job=controller.submit(self.store,args)
                # The worker is a real detached process; wait with a bounded subprocess.
                probe='import importlib.util,json,pathlib,sys,time; p=pathlib.Path(sys.argv[1]); deadline=time.monotonic()+10\nwhile time.monotonic()<deadline:\n try:\n  state=json.loads(p.read_text())\n  if state["status"] in ["succeeded","failed","timed_out","interrupted"]: break\n except FileNotFoundError: pass\n time.sleep(.05)\nelse: raise SystemExit("worker deadline")'
                status=self.root/'remote/tasks'/job['job_id']/'status.json'
                subprocess.run([sys.executable,'-I','-c',probe,str(status)],check=True,timeout=12)
                state=controller.status(self.store,self.store.job(job['job_id']))
                self.assertEqual(state['status'],'succeeded',state)
                controller.finish(self.store,state)
                self.assertEqual((self.store.job_dir(job['job_id'])/'artifacts/result').read_text(),value+'installed')
                jobs.append(job)
        self.assertFalse(jobs[0]['environment_reused'])
        self.assertTrue(jobs[1]['environment_reused'])
        self.assertEqual(jobs[0]['env_dir'],jobs[1]['env_dir'])
        self.assertNotEqual(jobs[0]['job_id'],jobs[1]['job_id'])


if __name__ == '__main__':
    unittest.main()
