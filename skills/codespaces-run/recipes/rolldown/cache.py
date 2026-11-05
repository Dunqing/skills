#!/usr/bin/env python3
"""Resolve a versioned Rolldown Rust environment and stable benchmark fixture root."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform


def managed_path(root, path):
    path = Path(path).resolve()
    path.relative_to((root / 'tasks').resolve())
    if not path.is_dir():
        raise ValueError('missing retained job directory: ' + str(path))
    return str(path)


def resolve(root, identity, env_dir, fixture_root):
    root = Path(root).resolve()
    if identity['repo'] != 'rolldown/rolldown':
        raise ValueError('this recipe is only for rolldown/rolldown')
    encoded = json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()
    key = hashlib.sha256(encoded).hexdigest()
    cache = root / 'cache' / 'rolldown'
    cache.mkdir(parents=True, exist_ok=True)
    receipt = cache / (key + '.json')
    with (cache / 'registry.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if receipt.exists():
            value = json.loads(receipt.read_text())
            if value['identity'] != identity:
                raise ValueError('cache identity mismatch')
            managed_path(root, value['env_dir'])
            managed_path(root, value['fixture_root'])
            value['reused'] = True
            return value
        value = {'identity': identity, 'env_dir': managed_path(root, env_dir),
                 'fixture_root': managed_path(root, fixture_root), 'reused': False}
        temporary = receipt.with_suffix('.tmp-' + str(os.getpid()))
        temporary.write_text(json.dumps(value, indent=2) + '\n')
        os.replace(temporary, receipt)
        return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='/workspaces/.agent-codespaces')
    parser.add_argument('--repo', default='rolldown/rolldown')
    parser.add_argument('--toolchain', required=True)
    parser.add_argument('--build-config', required=True)
    parser.add_argument('--fixture-key', required=True)
    parser.add_argument('--seed-env', required=True)
    parser.add_argument('--seed-fixture-root', required=True)
    parser.add_argument('--field', choices=['env_dir', 'fixture_root'])
    args = parser.parse_args()
    identity = {'repo': args.repo, 'toolchain': args.toolchain,
                'build_config': args.build_config, 'architecture': platform.machine(),
                'fixture_key': args.fixture_key, 'recipe_version': 1}
    value = resolve(args.root, identity, args.seed_env, args.seed_fixture_root)
    print(value[args.field] if args.field else json.dumps(value))


if __name__ == '__main__':
    main()
