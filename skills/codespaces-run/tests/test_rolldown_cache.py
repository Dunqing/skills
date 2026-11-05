import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('rolldown_cache', Path(__file__).parents[1] / 'recipes/rolldown/cache.py')
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = self.root / 'tasks/first/env'
        self.fixtures = self.root / 'tasks/first/build'
        self.env.mkdir(parents=True)
        self.fixtures.mkdir()
        self.identity = {'repo': 'rolldown/rolldown', 'toolchain': '1.99.0',
                         'build_config': 'profile-thin-cgu1', 'architecture': 'x86_64',
                         'fixture_key': 'verified-fixtures', 'recipe_version': 1}

    def tearDown(self):
        self.temp.cleanup()

    def test_new_job_reuses_existing_environment_and_fixture_paths(self):
        first = cache.resolve(self.root, self.identity, self.env, self.fixtures)
        self.assertFalse(first['reused'])
        other = self.root / 'tasks/second/env'
        other.mkdir(parents=True)
        second = cache.resolve(self.root, self.identity, other, other)
        self.assertTrue(second['reused'])
        self.assertEqual(second['env_dir'], str(self.env.resolve()))
        self.assertEqual(second['fixture_root'], str(self.fixtures.resolve()))

    def test_incompatible_runtime_flags_or_fixtures_do_not_reuse(self):
        cache.resolve(self.root, self.identity, self.env, self.fixtures)
        for field in ['toolchain', 'build_config', 'architecture', 'fixture_key']:
            with self.subTest(field=field):
                changed = dict(self.identity, **{field: 'different'})
                result = cache.resolve(self.root, changed, self.env, self.fixtures)
                self.assertFalse(result['reused'])

    def test_other_repositories_and_outside_paths_are_rejected(self):
        with self.assertRaises(ValueError):
            cache.resolve(self.root, dict(self.identity, repo='other/repo'), self.env, self.fixtures)
        with self.assertRaises(ValueError):
            cache.resolve(self.root, self.identity, self.root, self.fixtures)

    def test_missing_retained_environment_is_reported(self):
        cache.resolve(self.root, self.identity, self.env, self.fixtures)
        self.env.rmdir()
        with self.assertRaisesRegex(ValueError, 'missing retained'):
            cache.resolve(self.root, self.identity, self.env, self.fixtures)


if __name__ == '__main__':
    unittest.main()
