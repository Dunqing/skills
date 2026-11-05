# Rolldown benchmark recipe and warm caches

Reuse a retained Codespace and compatible Rust toolchain/target caches to avoid
unnecessary setup and cold builds. Choose compilation settings before building;
changing flags invalidates the corresponding binary/cache identity.

## Resolve the warm environment

This optional adapter is not part of the generic core. For ordinary new tasks,
prefer the controller's `--environment-key` and `$JOB_ENV_DIR`; this recipe also
retains a verified absolute fixture root for comparable benchmark binaries.

Upload `recipes/rolldown/cache.py` as `rolldown_cache.py` with the task inputs. Before installing Rust, create the new job's build directory and run the helper on the remote worker:

```bash
python3 -I "$JOB_INPUT_DIR/rolldown_cache.py" \
  --toolchain "$RUST_VERSION" \
  --build-config "$BUILD_CONFIG" \
  --fixture-key "$FIXTURE_KEY" \
  --seed-env "$JOB_ENV_DIR" \
  --seed-fixture-root "$JOB_DIR/build" > "$ARTIFACTS_DIR/cache-receipt.json"
```

`FIXTURE_KEY` must hash the fixture content manifest and the benchmark setup/harness recipe, including target package versions and options. `BUILD_CONFIG` must identify the profile, LTO, codegen units, target and any RUSTFLAGS. The helper separates incompatible identities and rejects another repository or paths outside managed job directories.

Read `env_dir` and `fixture_root` from the JSON with Python. Set CARGO_HOME to env_dir/cargo, RUSTUP_HOME to env_dir/rustup and CARGO_TARGET_DIR to env_dir/target. Add CARGO_HOME/bin to PATH. Follow the [Rust preflight](rust.md) to detect incompatible devcontainer shims before building. Install the declared Rust version only if the actual pinned compiler is missing; do not resync/download it on each task.

New commits still get a fresh source snapshot. Share Cargo's environment and target directory within the same compatible cache. Cargo fingerprints source and lockfile changes; changed crates still need compilation. Do not promise that a warm build is instant, especially with LTO.

Set WORKSPACE_DIR to the retained `fixture_root` only after verifying every standard fixture hash and the target package files/entry against the newly prepared inputs. This preserves identical absolute fixture paths in baseline and candidate binaries; it also allows a previously built binary to be reused safely when all inputs match. Never overwrite retained fixtures for a different fixture key.

Keep each compiled benchmark binary and a receipt recording its exact source commit, harness hash, fixture key, compiler version, architecture, build flags and binary SHA256. Before rebuilding a baseline, check for an exact compatible retained binary. A stopped Codespace preserves these files. Do not delete build caches automatically.

## Choose compilation settings deliberately

For a native wall-clock check of the standard CodSpeed cases, follow the CI build configuration (LTO disabled, 16 codegen units) unless the user requests production/profile settings. Explain that this is native execution, not CodSpeed simulation. Use the repository's `profile` build when reproducing measurements explicitly reported with that profile. LTO/profile configuration affects the experiment, so never switch settings halfway through an A/B pair.

Inspect `just --list` first. A direct Cargo invocation is appropriate when a custom target/profile/flags cannot be supplied through the available recipe.

Keep warm-up, balanced baseline/candidate/identical-baseline orders and output assertions. Validate output structure semantically enough to tolerate pure annotations and inlined CJS wrapper calls. Test preflight assertions on generated artifacts before the long repeated measurement phase. A preflight bug after a successful build should resume from existing binaries via `--continue-from`, not trigger compilation again.

Before repeating Criterion2 measurements, run the exact command-line selector
through the real harness and assert a nonzero set of expected benchmark IDs and
parseable samples. Check this version's CLI semantics rather than assuming that
a regular expression or substring selector behaves like another Criterion
version. A successful exit with no selected cases is a failed pilot. Selector
syntax belongs in this recipe/task, not the generic worker.

## Discover retained receipts

Receipts are stored under `/workspaces/.agent-codespaces/cache/rolldown/` on the
registered instance. Discover and verify them for the current account, instance,
toolchain, build configuration and fixture manifest. A fresh installation has no
existing receipts; the helper seeds them from the task's managed directories.
Never assume a previous user's instance, environment or fixture tree exists.
Stop after use; never delete the Codespace.
