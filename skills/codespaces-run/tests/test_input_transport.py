import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('transport_helpers',Path(__file__).with_name('test_sandbox.py'))
helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)
controller,remote=helpers.controller,helpers.remote
spec=importlib.util.spec_from_file_location('bootstrap',helpers.SCRIPTS/'bootstrap.py')
bootstrap=importlib.util.module_from_spec(spec);spec.loader.exec_module(bootstrap)


class InputTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=controller.Store(self.root/'state')
        self.job={'job_id':'job-test','status':'preparing'}
        self.store.save_job(self.job)
        (self.store.job_dir('job-test')/'task.sh').write_text('true\n')

    def tearDown(self):
        self.temp.cleanup()

    def bundle(self,source=False):
        if source:
            (self.store.job_dir('job-test')/'source.tar.gz').write_bytes(b'compressed source')
        path=controller.pack_input(self.store,self.job)
        return path,self.job['input_transfer']['sha256']

    def test_git_and_continuation_bundle_do_not_include_source(self):
        path,_=self.bundle()
        with tarfile.open(path,'r:') as archive:
            self.assertEqual(set(archive.getnames()),{'remote_worker.py','task.sh','job.json'})
        self.assertEqual(self.job['input_transfer']['upload_count'],1)

    def test_local_source_bundle_keeps_compressed_snapshot_bytes(self):
        path,_=self.bundle(source=True)
        with tarfile.open(path,'r:') as archive:
            self.assertEqual(archive.extractfile('source.tar.gz').read(),b'compressed source')

    def test_verified_unpack_and_launch_use_one_bootstrap(self):
        path,digest=self.bundle()
        with patch.object(bootstrap.subprocess,'check_output',return_value='{"status":"starting","worker_pid":1}') as launch:
            result=bootstrap.bootstrap(self.root/'remote','job-test',path,digest,30)
        self.assertTrue(result['ok'],result)
        launch.assert_called_once()
        job=self.root/'remote/tasks/job-test'
        self.assertTrue((job/'input-bundle.tar').exists())
        self.assertEqual((job/'task.sh').read_text(),'true\n')
        self.assertEqual({r['phase'] for r in result['transport_timings']},{'bundle_unpack','worker_launch'})
        self.assertTrue(all(r['duration_seconds']>=0 for r in result['transport_timings']))

    def test_bad_checksum_prevents_writes_and_launch(self):
        path,_=self.bundle()
        with patch.object(bootstrap.subprocess,'check_output') as launch:
            result=bootstrap.bootstrap(self.root/'remote','job-test',path,'bad',30)
        self.assertFalse(result['ok'])
        self.assertFalse(result['launch_attempted'])
        self.assertIn('checksum',result['error'])
        self.assertFalse((self.root/'remote').exists())
        launch.assert_not_called()

    def test_duplicate_traversal_and_link_members_prevent_launch(self):
        for bad in ['../escape','task.sh','link']:
            with self.subTest(bad=bad):
                path=self.root/(bad.replace('/','_')+'.tar')
                with tarfile.open(path,'w:') as archive:
                    for name in ['remote_worker.py','task.sh','job.json',bad]:
                        info=tarfile.TarInfo(name); data=b'{}';info.size=len(data)
                        if name=='link':
                            info.type=tarfile.SYMTYPE;info.linkname='../escape';info.size=0
                        archive.addfile(info,io.BytesIO(data) if info.isfile() else None)
                digest=hashlib.sha256(path.read_bytes()).hexdigest()
                with patch.object(bootstrap.subprocess,'check_output') as launch:
                    result=bootstrap.bootstrap(self.root/'remote','job-test',path,digest,30)
                self.assertFalse(result['ok']);self.assertFalse(result['launch_attempted'])
                launch.assert_not_called()
        self.assertFalse((self.root/'remote').exists())

    def test_existing_job_is_never_overwritten(self):
        path,digest=self.bundle()
        destination=self.root/'remote/tasks/job-test';destination.mkdir(parents=True)
        (destination/'task.sh').write_text('existing')
        with patch.object(bootstrap.subprocess,'check_output') as launch:
            result=bootstrap.bootstrap(self.root/'remote','job-test',path,digest,30)
        self.assertFalse(result['ok']);launch.assert_not_called()
        self.assertEqual((destination/'task.sh').read_text(),'existing')

    def test_uncertain_worker_launch_is_not_claimed_as_safe_to_retry(self):
        path,digest=self.bundle()
        with patch.object(bootstrap.subprocess,'check_output',side_effect=RuntimeError('lost reply')):
            result=bootstrap.bootstrap(self.root/'remote','job-test',path,digest,30)
        self.assertFalse(result['ok']);self.assertTrue(result['launch_attempted'])
        self.assertEqual(result['transport_timings'][-1]['outcome'],'failed')

    def test_git_requires_https_and_exact_sha_without_embedded_credentials(self):
        for url,commit in [('file:///tmp/repo','a'*40),('https://token@example.com/repo','a'*40),
                           ('https://example.com/repo?token=x','a'*40),('https://example.com/repo','main')]:
            with self.subTest(url=url,commit=commit):
                with self.assertRaises(ValueError):
                    controller.git_source(types.SimpleNamespace(git_url=url,git_commit=commit))
        result=controller.git_source(types.SimpleNamespace(git_url='https://example.com/repo.git',git_commit='A'*40))
        self.assertEqual(result['commit'],'a'*40)

    def test_git_and_local_snapshot_modes_are_mutually_exclusive(self):
        args=types.SimpleNamespace(script=str(self.store.job_dir('job-test')/'task.sh'),timeout=30,
                                   continue_from=None,source='local',git_url='https://example.com/repo.git',git_commit='a'*40)
        with self.assertRaisesRegex(ValueError,'exactly one'):
            controller.submit(self.store,args)

    def test_fetches_exact_commit_not_branch_tip_without_external_network(self):
        origin=self.root/'origin';origin.mkdir()
        subprocess.run(['git','init','-q',str(origin)],check=True)
        def commit(value):
            (origin/'value').write_text(value)
            subprocess.run(['git','-C',str(origin),'add','.'],check=True)
            subprocess.run(['git','-C',str(origin),'-c','user.name=Test','-c','user.email=test@localhost','commit','-qm',value],check=True)
            return subprocess.check_output(['git','-C',str(origin),'rev-parse','HEAD'],text=True).strip()
        first=commit('first');commit('second')
        destination=self.root/'fetch-job';destination.mkdir()
        original_run=subprocess.run
        def local_transport(command,**kwargs):
            command=list(command)
            if command[-4:-1]==['remote','add','origin']:
                command[-1]=str(origin)
            return original_run(command,**kwargs)
        with patch.object(remote.subprocess,'run',side_effect=local_transport):
            receipt=remote.fetch_source(destination,{'url':'https://example.invalid/repo.git','commit':first},30)
        self.assertEqual(receipt['actual_commit'],first)
        self.assertEqual((destination/'source/value').read_text(),'first')
        self.assertTrue((destination/'source-provenance.json').exists())
        self.assertFalse(receipt['submodules_initialized'])

    def test_bootstrap_failure_and_lost_reply_have_distinct_cleanup(self):
        script=self.store.job_dir('job-test')/'task.sh'
        args=types.SimpleNamespace(script=str(script),source=None,git_url='https://example.invalid/repo',git_commit='a'*40,
                                   timeout=30,repo='example/repo',profile='default',codespace=None,continue_from=None)
        for lost in [False,True]:
            with self.subTest(lost=lost):
                registry=self.store.load()
                registry['instances']={'test-cs':{'profile':'default','repo':'example/repo','lease':None}}
                self.store.save(registry)
                reply=RuntimeError('lost SSH reply') if lost else json.dumps({'ok':False,'launch_attempted':False,'error':'checksum mismatch'})
                api={'machine':{'name':'test','cpus':2,'memory_in_bytes':8*1024**3},'state':'Available'}
                with patch.object(controller,'api',return_value=api), patch.object(controller,'ensure_ready'), \
                     patch.object(controller,'call',return_value=''), \
                     patch.object(controller,'ssh',side_effect=[reply]), patch.object(controller,'stop') as stop, \
                     patch.object(controller,'spawn_watch',return_value=42) as watch:
                    result=controller.submit(self.store,args)
                self.assertEqual(result['status'],'unknown' if lost else 'failed')
                if lost:
                    stop.assert_not_called();watch.assert_called_once()
                    self.assertEqual(self.store.load()['instances']['test-cs']['lease'],result['job_id'])
                else:
                    stop.assert_called_once();watch.assert_not_called()
                    self.assertIn('shutdown_confirmed_at',result)

    def test_fetch_failure_does_not_run_task(self):
        job=self.root/'tasks/job-failed';job.mkdir(parents=True)
        (job/'job.json').write_text(json.dumps({'git_source':{'url':'https://example.invalid/repo','commit':'a'*40}}))
        (job/'task.sh').write_text('touch should-not-run\n')
        with patch.object(remote,'fetch_source',side_effect=RuntimeError('fetch denied')):
            remote.run(job,10)
        state=remote.inspect(job)
        self.assertEqual(state['status'],'failed')
        self.assertIn('fetch denied',state['error'])
        self.assertFalse((job/'source/should-not-run').exists())
        timing=next(r for r in state['worker_timings'] if r['phase']=='source_fetch')
        self.assertEqual(timing['outcome'],'failed')


if __name__=='__main__':
    unittest.main()
