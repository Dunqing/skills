import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('task_helpers', Path(__file__).with_name('test_sandbox.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
controller, remote = helpers.controller, helpers.remote
spec = importlib.util.spec_from_file_location('task_bootstrap', helpers.SCRIPTS / 'bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class TaskInputsAndStagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inputs = self.root / 'task-files'
        self.inputs.mkdir()
        (self.inputs / 'build.sh').write_text('printf built >> "$JOB_ENV_DIR/build-count"\ncp source.txt "$JOB_ENV_DIR/tool"\n')
        (self.inputs / 'validate.sh').write_text('test -f "$JOB_INPUT_DIR/allow-validation"\n')
        (self.inputs / 'run.sh').write_text('cp "$JOB_ENV_DIR/tool" "$ARTIFACTS_DIR/result"\n')
        self.plan = {'stages': [
            {'name': 'build', 'script': 'build.sh', 'inputs': {'workspace': ['source.txt']},
             'outputs': {'env': ['tool']}},
            {'name': 'validate', 'script': 'validate.sh'},
            {'name': 'run', 'script': 'run.sh'},
        ]}

    def tearDown(self):
        self.temp.cleanup()

    def job(self, name, previous=None, plan=None):
        job = self.root / 'tasks' / name
        job.mkdir(parents=True)
        workspace = previous / 'source' if previous else job / 'source'
        if not previous:
            workspace.mkdir()
            (workspace / 'source.txt').write_text('source bytes')
        env = previous / 'env' if previous else job / 'env'
        inputs = controller.pack_task_inputs(self.inputs, job)
        specification = {'workspace': str(workspace), 'env_dir': str(env),
                         'input_dir': str(job / 'inputs'), 'task_inputs': inputs}
        if previous:
            specification.update(from_stage='validate', previous_job_dir=str(previous))
        (job / 'job.json').write_text(json.dumps(specification))
        (job / 'stages.json').write_text(json.dumps(plan or self.plan))
        (job / 'task.sh').write_text('exec python3 -I "$JOB_WORKER" stages "$JOB_DIR"\n')
        return job

    def run_worker(self, job):
        subprocess.run([sys.executable, '-I', str(helpers.SCRIPTS / 'remote_worker.py'), 'run', str(job), '10'],
                       check=True, capture_output=True, timeout=15)
        return remote.inspect(job)

    def test_independent_inputs_use_one_verified_bundle(self):
        store = controller.Store(self.root / 'state')
        job = {'job_id': 'job-test', 'status': 'preparing'}
        store.save_job(job)
        directory = store.job_dir(job['job_id'])
        (directory / 'task.sh').write_text('true\n')
        manifest = controller.pack_task_inputs(self.inputs, directory)
        job.update(task_inputs=manifest, input_dir=str(self.root / 'remote/tasks/job-test/inputs'))
        store.save_job(job)
        bundle = controller.pack_input(store, job)
        self.assertEqual(job['input_transfer']['upload_count'], 1)
        with tarfile.open(bundle) as archive:
            self.assertEqual(set(archive.getnames()), {'remote_worker.py', 'task.sh', 'job.json',
                                                       'inputs.tar.gz', 'inputs-manifest.json'})
        with patch.object(bootstrap.subprocess, 'check_output', return_value='{"status":"starting"}'):
            result = bootstrap.bootstrap(self.root / 'remote', 'job-test', bundle, job['input_transfer']['sha256'], 10)
        self.assertTrue(result['ok'], result)
        remote_job = self.root / 'remote/tasks/job-test'
        input_dir = remote.prepare_task_inputs(remote_job, json.loads((remote_job / 'job.json').read_text()))
        self.assertEqual((input_dir / 'build.sh').read_bytes(), (self.inputs / 'build.sh').read_bytes())
        self.assertNotEqual(input_dir, remote_job / 'source')

    def test_symlinks_and_environment_files_rejected_before_remote_selection(self):
        store = controller.Store(self.root / 'state')
        script = self.root / 'task.sh'
        script.write_text('true\n')
        args = types.SimpleNamespace(script=str(script), source='unused', continue_from=None, timeout=10,
                                     repo='example/repo', profile='default', inputs=str(self.inputs))
        for name in ['symlink', '.env']:
            path = self.inputs / name
            if name == 'symlink':
                path.symlink_to('build.sh')
            else:
                path.write_text('private')
            with patch.object(controller, 'api') as api:
                result = controller.submit(store, args)
            self.assertEqual(result['status'], 'failed')
            api.assert_not_called()
            path.unlink()

    def test_file_tampering_fails_before_task_runs(self):
        job = self.job('job-tamper')
        # Rewrite archive bytes while leaving the independently recorded file hashes intact.
        with tarfile.open(job / 'inputs.tar.gz', 'w:gz') as archive:
            for path in self.inputs.iterdir():
                data = b'exit 0\n' if path.name == 'build.sh' else path.read_bytes()
                member = tarfile.TarInfo(path.name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        state = self.run_worker(job)
        self.assertEqual(state['status'], 'failed', state)
        self.assertIn('hashes or inventory changed', state['error'])
        self.assertFalse((job / 'env/build-count').exists())
        self.assertNotIn('task_pid', state)

    def test_traversal_duplicate_and_link_members_rejected(self):
        for index, bad in enumerate(['../escape', 'build.sh', 'link']):
            job = self.job('job-unsafe-' + str(index))
            manifest = json.loads((job / 'inputs-manifest.json').read_text())
            with tarfile.open(job / 'inputs.tar.gz', 'w:gz') as archive:
                for name in list(manifest) + [bad]:
                    member = tarfile.TarInfo(name)
                    member.size = 0
                    if name == 'link':
                        member.type = tarfile.SYMTYPE
                        member.linkname = '../escape'
                    archive.addfile(member, io.BytesIO(b'') if member.isfile() else None)
            state = self.run_worker(job)
            self.assertEqual(state['status'], 'failed', state)
            self.assertIn('archive members', state['error'])
            self.assertFalse((job / 'escape').exists())

    def test_resume_validation_uses_new_runner_without_rebuilding(self):
        first = self.job('job-first')
        state = self.run_worker(first)
        self.assertEqual(state['status'], 'failed', state)
        receipts = json.loads((first / 'stage-results.json').read_text())
        self.assertEqual(receipts['build']['outcome'], 'succeeded')
        self.assertEqual(receipts['validate']['outcome'], 'failed')
        (self.inputs / 'validate.sh').write_text('test "$(cat "$JOB_ENV_DIR/tool")" = "source bytes"\n')
        second = self.job('job-second', first)
        state = self.run_worker(second)
        self.assertEqual(state['status'], 'succeeded', state)
        self.assertEqual((second / 'artifacts/result').read_text(), 'source bytes')
        self.assertEqual((first / 'env/build-count').read_text(), 'built')
        receipts = json.loads((second / 'stage-results.json').read_text())
        self.assertEqual(receipts['build']['outcome'], 'reused')
        self.assertEqual(receipts['run']['outcome'], 'succeeded')
        events = [json.loads(line) for line in (second / 'progress-events.jsonl').read_text().splitlines()]
        self.assertEqual([event['phase'] for event in events], ['resume-validation', 'validate', 'run'])

    def test_changed_inputs_outputs_or_build_script_refuse_reuse(self):
        for target in ['source', 'output', 'script', 'receipt']:
            with self.subTest(target=target):
                first = self.job('job-first-' + target)
                self.assertEqual(self.run_worker(first)['status'], 'failed')
                if target == 'source':
                    (first / 'source/source.txt').write_text('changed source')
                elif target == 'output':
                    (first / 'env/tool').write_text('stale tool')
                elif target == 'script':
                    (self.inputs / 'build.sh').write_text('exit 0\n')
                else:
                    receipts = json.loads((first / 'stage-results.json').read_text())
                    receipts['build']['outcome'] = 'failed'
                    (first / 'stage-results.json').write_text(json.dumps(receipts))
                (self.inputs / 'validate.sh').write_text('touch "$JOB_ENV_DIR/should-not-run"\n')
                second = self.job('job-second-' + target, first)
                state = self.run_worker(second)
                self.assertEqual(state['status'], 'failed', state)
                self.assertFalse((first / 'env/should-not-run').exists())
                self.assertIn('cannot reuse stage build', (second / 'output.log').read_text())
                (self.inputs / 'build.sh').write_text('printf built >> "$JOB_ENV_DIR/build-count"\ncp source.txt "$JOB_ENV_DIR/tool"\n')
                (self.inputs / 'validate.sh').write_text('exit 7\n')

    def test_invalid_stage_plans_are_rejected_locally(self):
        for change in ['duplicate', 'traversal', 'unknown-root', 'missing-script']:
            plan = copy.deepcopy(self.plan)
            if change == 'duplicate':
                plan['stages'][1]['name'] = 'build'
            elif change == 'traversal':
                plan['stages'][0]['script'] = '../build.sh'
            elif change == 'unknown-root':
                plan['stages'][0]['outputs'] = {'outside': ['anything']}
            else:
                plan['stages'][0]['script'] = 'missing.sh'
            path = self.root / 'stages.json'
            path.write_text(json.dumps(plan))
            with self.assertRaises(ValueError):
                controller.load_stage_plan(path, {p.name: {} for p in self.inputs.iterdir()})

    def test_staged_submit_resume_collect_with_real_worker_and_fake_github(self):
        store = controller.Store(self.root / 'state-dir')
        registry = store.load()
        registry['instances']['test-cs'] = {'repo': 'example/repo', 'profile': 'default', 'lease': None}
        store.save(registry)
        source = self.root / 'checkout'
        source.mkdir()
        (source / 'source.txt').write_text('source bytes')
        subprocess.run(['git', 'init', '-q', str(source)], check=True)
        subprocess.run(['git', '-C', str(source), 'add', 'source.txt'], check=True)
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test', '-c',
                        'user.email=test@localhost', 'commit', '-qm', 'input'], check=True)
        plan = self.root / 'plan.json'
        plan.write_text(json.dumps(self.plan))
        (self.root / 'state').write_text('Shutdown')
        executable = self.root / 'gh'
        executable.write_text(helpers.FAKE_GH)
        executable.chmod(0o755)
        args = types.SimpleNamespace(script=None, stages=str(plan), inputs=str(self.inputs),
                                     source=str(source), repo='example/repo', timeout=10, profile='default',
                                     codespace=None, continue_from=None, from_stage=None)
        jobs = []
        with patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH'],
                                     'SANDBOX_TEST_ROOT': str(self.root)}), \
             patch.object(controller, 'ROOT', str(self.root / 'remote')), \
             patch.object(controller, 'spawn_watch', return_value=0):
            for index in range(2):
                if index:
                    args.source = None
                    args.continue_from = jobs[0]['job_id']
                    args.from_stage = 'validate'
                    (self.inputs / 'validate.sh').write_text('test -s "$JOB_ENV_DIR/tool"\n')
                job = controller.submit(store, args)
                jobs.append(job)
                probe = '''import json,pathlib,sys,time
p=pathlib.Path(sys.argv[1]); deadline=time.monotonic()+10
while time.monotonic()<deadline:
 try:
  if json.loads(p.read_text())["status"] in ["succeeded","failed","timed_out","interrupted"]: break
 except FileNotFoundError: pass
 time.sleep(.02)
else: raise SystemExit("worker deadline")
'''
                subprocess.run([sys.executable, '-I', '-c', probe,
                                str(self.root / 'remote/tasks' / job['job_id'] / 'status.json')], check=True, timeout=12)
                state = controller.status(store, store.job(job['job_id']))
                self.assertEqual(state['status'], 'succeeded' if index else 'failed', state)
                controller.finish(store, state)
                self.assertEqual(job['input_transfer']['upload_count'], 1)
        result = store.job_dir(jobs[1]['job_id'])
        self.assertEqual((result / 'artifacts/result').read_text(), 'source bytes')
        self.assertEqual(json.loads((result / 'stage-results.json').read_text())['build']['outcome'], 'reused')
        self.assertTrue((result / 'progress-events.jsonl').is_file())
        self.assertEqual((self.root / 'remote/tasks' / jobs[0]['job_id'] / 'env/build-count').read_text(), 'built')
        self.assertNotEqual(jobs[0]['input_dir'], jobs[1]['input_dir'])
        self.assertNotIn('source.tar.gz', jobs[1]['input_transfer']['members'])
        self.assertIn('stages.json', jobs[1]['input_transfer']['members'])

    def test_verifier_rechecks_environment_before_reuse(self):
        (self.inputs / 'build.sh').write_text((self.inputs / 'build.sh').read_text() +
                                            'printf version1 > "$JOB_ENV_DIR/runtime-version"\n')
        (self.inputs / 'verify.sh').write_text('test "$(cat "$JOB_ENV_DIR/runtime-version")" = version1\n')
        plan = copy.deepcopy(self.plan)
        plan['stages'][0]['verify'] = 'verify.sh'
        first = self.job('job-first-verify', plan=plan)
        self.assertEqual(self.run_worker(first)['status'], 'failed')
        self.assertEqual(json.loads((first / 'stage-results.json').read_text())['build']['outcome'], 'succeeded')
        (first / 'env/runtime-version').write_text('version2')
        (self.inputs / 'validate.sh').write_text('touch "$JOB_ENV_DIR/should-not-run"\n')
        second = self.job('job-second-verify', first, plan)
        self.assertEqual(self.run_worker(second)['status'], 'failed')
        self.assertFalse((first / 'env/should-not-run').exists())
        self.assertEqual((first / 'env/build-count').read_text(), 'built')

    def test_later_verifier_cannot_change_an_earlier_retained_artifact(self):
        (self.inputs / 'validate.sh').write_text('printf checked > "$JOB_ENV_DIR/checked"\n')
        (self.inputs / 'verify.sh').write_text(
            'if test -f "$JOB_ENV_DIR/first-run-finished"; then printf corrupted > "$JOB_ENV_DIR/tool"; fi\n')
        (self.inputs / 'run.sh').write_text(
            'touch "$JOB_ENV_DIR/first-run-finished"\ncp "$JOB_ENV_DIR/tool" "$ARTIFACTS_DIR/result"\n')
        plan = copy.deepcopy(self.plan)
        plan['stages'][1].update(verify='verify.sh', outputs={'env': ['checked']})
        first = self.job('job-before-verifier', plan=plan)
        self.assertEqual(self.run_worker(first)['status'], 'succeeded')
        second = self.job('job-after-verifier', first, plan)
        spec = json.loads((second / 'job.json').read_text())
        spec['from_stage'] = 'run'
        (second / 'job.json').write_text(json.dumps(spec))
        state = self.run_worker(second)
        self.assertEqual(state['status'], 'failed', state)
        self.assertFalse((second / 'artifacts/result').exists())
        self.assertIn('retained stage build changed during verification', (second / 'output.log').read_text())
        receipts = json.loads((second / 'stage-results.json').read_text())
        self.assertEqual(receipts['build']['outcome'], 'invalidated')
        self.assertNotIn('run', receipts)

    def test_resume_requires_stage_plan_and_finished_task(self):
        args = types.SimpleNamespace(script=str(self.inputs / 'build.sh'), source='unused', continue_from=None,
                                     timeout=10, from_stage='validate')
        with self.assertRaisesRegex(ValueError, '--from-stage requires'):
            controller.submit(controller.Store(self.root / 'state'), args)


if __name__ == '__main__':
    unittest.main()
