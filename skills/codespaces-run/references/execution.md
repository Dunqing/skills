# Reusable execution environments and progress

The same lifecycle serves test suites, builds, temporary services, data jobs and benchmarks. Source, command, dependencies, validation and outputs come from the task; the controller does not choose a language or project.

## Choose source acquisition

Use the input that matches the task; source upload is not mandatory:

- `--git-url https://HOST/OWNER/REPO.git --git-commit FULL_SHA`: fetch the exact
  published commit directly on the worker into a fresh managed job directory.
  Verify FETCH_HEAD and checkout HEAD, retain `.git` and write source provenance.
  Accept only HTTPS without embedded credentials and full commit hashes. This
  mode never uploads a local source snapshot and never accepts a floating ref.
- `--source /absolute/checkout`: upload the actual local working-tree snapshot,
  preserving uncommitted and nonignored untracked files as described in SKILL.md.
  Choose this when local changes are the requested input.
- `--continue-from JOB_ID`: reuse the prior workspace, dependencies and existing
  local changes without source upload or Git fetch. This does not reset/clean the
  workspace or assert that its current files still equal an original commit;
  task-specific cache/input assertions remain necessary.

These modes are mutually exclusive. There is no silent fallback after a fetch
failure, and no automatic replacement of a dirty local checkout with its Git HEAD.
Remote Git relies on the instance's existing authorized read access; controller
credentials are not forwarded. Submodules are not initialized automatically and
LFS is not guaranteed; use a task-specific recipe for those inputs.

## Input transport

Pass independent task files with `--inputs DIR`; the worker exports their
absolute directory as `JOB_INPUT_DIR`, separate from the source workspace and
dependency environment. Call runners by path instead of encoding them into Bash.
Task files are captured as regular files with per-file SHA256/size in
`inputs-manifest.json`. Links, special files, Git metadata and secret environment
files are rejected. The worker verifies the complete inventory and hashes before
running the task. Treat this directory as immutable; write scratch/output files
under `JOB_DIR`, `JOB_ENV_DIR` or `ARTIFACTS_DIR`. Python bytecode writes are disabled.

A continuation without `--inputs` reuses and revalidates the earlier input
directory. Providing `--inputs` creates a fresh one for the new job; it never
overwrites the old inputs. This lets a repaired validation runner reuse an
unchanged build. See [stage plans](stages.md) for verified execution recovery.

All submissions use one regular-file tar bundle: worker, task script, job metadata,
optional compressed task inputs and their manifest, an optional stage plan, and,
for local snapshot mode only, the already-compressed source archive. The outer
tar is uncompressed to avoid recompressing the source. Upload once to a unique
persistent `/workspaces` path; one SSH bootstrap validates SHA256, rejects links,
unexpected/duplicate members and existing job-directory overwrites, extracts into
a fresh managed job, retains the input bundle there, and launches the job's worker.
No separate mkdir SSH request or per-control-file copy is needed. A shared worker
copy remains available for control queries against older jobs.

`input_transfer` records bytes, checksum, members and the single planned upload.
`pack` and `upload` are controller intervals; `bootstrap` includes SSH overhead.
`transport_timings` contains the remote `bundle_unpack` (checksum plus extraction)
and `worker_launch` intervals. Source archive extraction belongs to worker_prepare;
remote Git fetch has a separate source_fetch interval. These intervals overlap;
do not add bootstrap and its remote subphases to claim a total.

A structured bootstrap failure before launch is a known failed task and follows
confirmed-stop cleanup. SSH failure after bootstrap was attempted is unknown;
retain the lease and observe the durable job before retrying. Never launch again
just because the acknowledgement was lost. Failed uploads/inputs remain evidence;
no instance or source cleanup is performed to make room.

## Task resources

Machine requirements and dependency compatibility are separate. For example:

```bash
python3 "$TOOL" submit --repo example/project --source /absolute/checkout \
  --script /absolute/test.sh --machine standardLinux32gb --cpus 4 \
  --min-memory-mib 14000 --environment-key RUNTIME_LOCK_AND_BUILD_COMPATIBILITY
```

Machine names must come from the host repository's available-machine listing.
`--machine` pins a name, `--cpus` requires an exact CPU count, and
`--min-memory-mib` requires minimum memory capacity. Capacity-only task flags
override an implicit configured machine; an explicitly named machine must also
satisfy those flags. Without task flags, enforce the configured machine, or choose
the smallest available suitable machine when creation is enabled. For an
adopted-only pool, validate the repository's registered idle instance. Never silently resize or create a replacement for incompatible idle
instances; report the mismatch before starting anything.

The controller stores `requested_resources`, effective `resource_requirements`,
and live API `observed_machine` in the job, and records observed specs in the
registry. It checks before reservation and again after startup. The worker stores
`runtime_resources` in status and `runtime-resources.json`, verifies CPU affinity
and effective physical/cgroup memory capacity, then starts the task. API-advertised
RAM and runtime memory differ: leave OS/container headroom in a minimum memory
request. A failed or unavailable required runtime check prevents script execution.
CPU count is not automatically added to dependency environment identity; tasks
that build hardware-specific artifacts must include those flags/architecture in
their compatibility key as usual.

`--continue-from` remains bound to the previous instance and workspace. Resource
policy still applies; use explicit task resource flags for an intentional change
from the configured default. No automatic resize occurs.

## Environment identity

Submit a fresh source with `--environment-key KEY` to retain dependencies across jobs. The key is namespaced by repository, instance profile and Codespace. The worker receives the retained path as `JOB_ENV_DIR`; source and artifacts remain job-specific. Omitting the key preserves the original per-job isolation. `--continue-from JOB_ID` instead reuses both the old source workspace and its environment, and cannot be combined with `--environment-key`.

Choose a non-secret compatibility key from declared runtime versions, dependency manifests/lockfiles, target architecture and relevant build configuration. Do not include the source commit unnecessarily: compatible code changes should reuse dependencies. Examples of key inputs:

| Task environment | Compatibility inputs | Retained files |
|---|---|---|
| Python tests/data processing | Python version, dependency lock hash, required native libraries | venv, package download cache, versioned datasets |
| Node builds/services/tests | Node and package-manager versions, lock hash, native target | package download store, compatible installed dependencies/build cache |
| Rust build/test/benchmark | rustc version, target, flags/profile, native dependencies | Cargo home, Rustup home, Cargo target directory |

Cargo safely fingerprints changed dependency versions within a compatible target directory. Python installed packages and Node dependency trees need explicit lockfile synchronization. A selected retained path is not proof its setup is complete: verify required executables, versions and files, then install/synchronize only what is missing. Do not silently fall back to another repository's environment.

New source still gets a fresh job workspace, whether snapshotted or fetched. Local snapshot symlink/submodule/ignored-input requirements need an explicit packaging recipe. Stable datasets and benchmark fixtures can live under a retained job directory when their content hashes match; they are separate from source. Never reuse an executable just because its filename matches: check source revision, toolchain, target, flags, recipe/input hashes and binary hash. Keep outputs under ARTIFACTS_DIR and large reusable binaries/caches under JOB_ENV_DIR. Do not delete old work automatically to make room.

A continuation should check the inputs it consumes. If it only executes retained
binaries, verify their build receipts/hashes and the corpus/harness inputs rather
than demanding a clean entire checkout: unrelated lockfile changes or startup
hooks need not invalidate those binaries. If rebuilding from source, validate the
actual source inputs, including relevant local changes. Preserve and report
unrelated modifications; do not reset them to satisfy a preflight.

Retained executables are a general reuse pattern, not benchmark-specific. Store
large artifacts under `JOB_ENV_DIR` with a receipt of source/input hashes,
builder/runtime version, platform/architecture, flags, dependency-lock identity
and output SHA256. An exact verified match can skip rebuilding; a filename or
successful previous command alone cannot. Receipts do not establish compatibility
with a different OS or hardware. Rust toolchain setup is covered by the optional
[Rust recipe](rust.md), not the generic controller.

## Task phases

The worker exports JOB_WORKER and JOB_PROGRESS_FILE. Update progress atomically using the worker helper:

```bash
python3 "$JOB_WORKER" progress "$JOB_DIR" '{"phase":"setup","message":"Checking declared runtime and dependencies"}'
```

```bash
python3 "$JOB_WORKER" progress "$JOB_DIR" '{"phase":"testing","message":"Integration tests","completed":3,"total":8}'
```

Each phase transition closes the preceding interval in `task-phases.json`.
Repeated updates to the same phase change counters/messages without resetting
its start. Returning to an earlier phase creates another interval, preserving
retries. The worker closes the active interval on success, failure, cancellation
or timeout. Interrupted/lost processes may leave an open interval; do not treat
that as a measured completed duration.

Use phases meaningful to the task: setup, build, validate, test, run, or service-test.
`status` exposes the latest progress and elapsed_seconds without interpreting log
prose. Every accepted update is appended under the progress lock to
`progress-events.jsonl`, collected with the results. The watcher coalesces ordinary
updates to 45 seconds and ignores timestamp-only changes; observed phase/status
changes and errors bypass the throttle. Polling remains every 20 seconds, so this
means immediate on observation, not a push event at the moment of transition.
The terminal outcome is reported once, before collection/shutdown retries; the
watcher's last line is a receipt with `final: true` and `collected`,
`shutdown_confirmed_at`, `cleanup_error` rather than a second full status record.
`events JOB_ID --follow` prints that stream from retained files and stops at the
receipt; it does not query the instance. Exit code 3 means the watcher is gone
without writing a receipt.
Do not stream the full event history to the user or add a second per-iteration
Monitor filter. Malformed progress is reported without replacing the task outcome.
Progress-only phases do not mark a stage reusable; execution recovery requires an
explicit [stage plan](stages.md) and verified receipts.

For a long compile, report the phase and elapsed time. Check active processes and resource availability if progress appears stalled; do not infer failure from quiet compiler output. Avoid competing work on the same instance while timing a performance experiment.

## Built-in lifecycle timings

```bash
python3 "$TOOL" timings JOB_ID
```

This is a local read: it never calls GitHub or wakes a stopped Codespace. While a
job is running, use `status` to refresh remote evidence first. The report keeps
four namespaces separate:

- `controller_timings`: snapshot, resource selection, input packing, startup,
  post-start resource verification, upload, bootstrap, collection and shutdown.
  Explicit recovery fetches record `fetch_startup` too.
- `transport_timings`: remote bundle verification/extraction and worker launch.
- `worker_timings`: worker preparation, optional source fetch, execution and cleanup, retained
  with `status.json` and exposed by `status`.
- `task_phases`: task-defined installation/build/validation/run intervals.

Each completed lifecycle attempt records wall-clock timestamps, a monotonic
`duration_seconds` and outcome. Failed attempts and retries remain separate;
starting an attempt is persisted before the operation. Controller timings have
their own local file and lock so a stale status refresh cannot overwrite them.
Missing old-job evidence is an empty list, not a zero-duration cache hit.

Worker/task phases overlap; controller launch can overlap worker preparation.
Do not sum these lists or subtract timestamps across controller and worker hosts
to claim a total. Worker timeouts and cleanup failures keep their failed outcome.
Collection and shutdown failures are recorded even when task execution succeeded;
only `shutdown_confirmed_at` establishes successful stop confirmation.

## Early validation and recovery

Run a representative smoke operation before expensive repetition:

- Tests: ensure discovery finds the intended suite and the result parser records a nonzero test count.
- Builds: assert that the expected executable/package exists and has the expected entry point.
- Services: wait for bounded HTTP readiness, then run the requested endpoint tests; keep processes in the task group.
- Data processing: verify input schema and a small output sample before the full run.
- Benchmarks: exercise the real workload and verify output assertions/parsing before the repeated rounds; use a separately recorded pilot when needed.

Preserve successful setup/build outputs. If a later validator fails, use `--continue-from` to resume that phase with the retained environment; do not redo completed expensive work. A known failed submission returns exit code 1; uncertain launch returns 2. Both print the durable job ID, so inspect it rather than blindly resubmitting.

## Finish

The watcher packs artifacts and logs into one remote archive, transfers it once, verifies SHA256 and extracts regular files into a fresh local directory. This avoids thousands of slow per-file SFTP operations. Links/special files are rejected during extraction; package such outputs explicitly into a regular archive artifact if needed. Source and dependency caches remain remote. The watcher then requests stop and verifies the API reaches Shutdown before releasing the lease. If shutdown cannot be confirmed, retain the lease and retry/report the error. A stop request alone is not shutdown confirmation. Cleanup exceptions must produce a terminal failed result with the original task outcome and cleanup_error, rather than leave status stuck at running. Never delete a Codespace.
