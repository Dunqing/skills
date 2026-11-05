---
name: codespaces-run
description: Run tests, builds, benchmarks, services, and long tasks in reusable remote GitHub Codespaces across repositories. Manage setup, dependency repair, async jobs, logs, artifacts, and stop-only cleanup without editing target repositories' devcontainer configuration.
license: MIT
argument-hint: "<repository and revision, what to run, what counts as success>"
compatibility: Requires a Linux/macOS controller with Python 3.9+, Git, authenticated GitHub CLI and Codespaces access. The remote Codespace requires Linux, Python 3.9+, Bash and writable /workspaces.
---

# Run tasks in Codespaces

Use this skill when the user asks to run work in Codespaces, needs remote
execution, or an authorized task needs a separate execution environment. Routine
work within the configured machine and instance budget is autonomous, subject to
the user's approval rules. The lifecycle policy is **stop after use; never delete
the Codespace**. Prefer **one repository, one Codespace**: reuse its registered
instance across jobs; never borrow another repository's instance or create a
second one to evade a busy/profile/resource mismatch.

This is a general execution skill across repositories: tests, builds, temporary
services, data processing and benchmarks. The core must not assume a repository,
language, package manager, benchmark harness or statistical method. Keep those
choices in optional recipes. Prefer retained compatible environments over repeated
cold setup; never claim a cache hit without checking the required runtime and inputs.

Its controller requires Python 3.9+,
`git`, and `gh` on a Linux/macOS host. The remote worker requires Linux, Python
3.9+, and Bash. GitHub's default Codespaces image is the intended baseline.

## Locate the tool

Resolve `scripts/sandbox.py` relative to this SKILL.md. In commands below,
`$TOOL` means that absolute path; do not assume the current directory is the
skill directory. `$STATE` is an optional persistent controller directory.

```bash
python3 "$TOOL" doctor
python3 "$TOOL" list
```

`doctor` makes real read-only API calls. A GitHub connector connection is not
proof that CLI/API/SSH access works. Preserve injected proxy, credential, and
identity configuration. A `Forbidden` response may be network policy or
authorization; do not replace credentials or attempt to bypass that policy.

## One-time provisioning

Prefer the user's configured pool. Only operate on registered instances. To use
an existing, dedicated Codespace, adopt it:

```bash
python3 "$TOOL" adopt CODESPACE_NAME --profile default
```

Alternatively enable creation within the configured budget. Each new instance is
created in the task's `--repo`, after inspecting that repository's devcontainer
configuration and creation hooks. Do not require or create a separate environment
repository. Do not expand the resource budget without authorization.

```bash
python3 "$TOOL" configure --allow-create --max-instances 2
```

Instances are bound to repository names, independently of profiles. A busy
instance, mismatched profile, or incompatible machine blocks another job for the
same repository instead of creating a second instance. Legacy registrations are
bound from the live API's repository metadata without starting them; ambiguous
or duplicate registrations require an explicit selection and are never deleted.
The legacy `--host-repo` flag still enables creation, but no longer routes other
repositories' jobs to a shared host repository.

If no machine was configured, the tool selects the smallest available machine.
Specify `--machine MACHINE_NAME` to pin a machine after checking the repository's
available machine types. Do not increase the user's budget or instance limit
without authorization. A reached pool limit is not a reason to delete an
instance. Reuse compatible profiles or report the resource constraint.

A task can declare `submit --machine NAME`, `--cpus N` (exact CPU count), and
`--min-memory-mib N` (minimum usable memory capacity). Task capacity flags replace
an implicit configured machine choice; an explicit machine must satisfy all
flags. Without task resource flags, the configured machine remains the default;
with creation enabled but no configured machine, select the smallest suitable machine.
Read live API specs before reserving/starting an instance and verify again after
startup. An idle instance whose hardware has drifted is not silently accepted,
resized or replaced. Specify the intended machine explicitly for a continuation
that deliberately differs from the current default policy. Resource selection
must not change environment-cache compatibility keys.

The worker separately records CPU affinity and physical/cgroup memory capacity,
and rejects unmet runtime requirements before executing the task script. The
runtime memory minimum needs headroom below advertised VM RAM for OS/container
overhead. Keep advertised API specs and observed runtime capacity distinct.

New instances use an idle timeout of `ceil(task timeout seconds / 60) + 15`
minutes, bounded to **30–240 minutes**. A two-hour task gets 135 minutes. This
applies only at creation; reusing an existing instance preserves its setting.
Idle timeout measures inactivity, not time since the task's deadline; it is a
fallback when controller cleanup is unavailable, not a scheduled shutdown.

Creation sets the maximum ordinary retention period, **30 days**. This does not
mean indefinite retention. Check the instance's **Keep codespace** setting; use
an authenticated browser if available and authorized, otherwise report that
this one-time setting remains needed. Organization retention rules may prevent
it. Never set retention to zero: this can delete a stopped instance. See
[operations.md](references/operations.md) for lifecycle and network details.

## Prepare a task

Establish the target repository, immutable revisions or local inputs, required
execution environment, resource limits, and operation scope before preparing a
large task. A Codespaces request requires Codespaces unless the user explicitly
allows a local fallback. If fallback is allowed, report its OS/hardware/runtime
and keep its measurements separate from remote results. `doctor` and other
read-only checks can reveal access/configuration problems, but cannot predict
whether submission or task execution will pass automatic approval. Resolve any
required approval before expensive preparation where possible. A denial is a
boundary: do not repackage the same action to bypass it, substitute local runs,
or change credentials; report the rejected action and reason.

Decide what must be fresh (source and results) and what can be retained
(toolchains, dependency environments, build/download caches and verified artifacts).
Read [environment reuse and task progress](references/execution.md). Optional
project recipes, such as [Rolldown](references/rolldown.md), apply only to their
own tasks; the controller and worker do not depend on them.

1. Inspect the exact local checkout, README, lockfiles, runtime version files,
   package manifests, and CI scripts. Resolve the requested commit and preserve
   any user changes. Choose the source mode deliberately: use remote Git for an
   already-published exact commit when remote read access is available; upload a
   local snapshot when local changes or local inputs are required; continue a
   retained workspace when that is the intended input. Never discard local edits
   by silently switching to a Git commit. For private targets without remote read
   access, clone/package on the controller using its existing authorization; the
   environment repo's token does not automatically authorize other repositories.
2. Prepare a Bash task script outside the target repository. Use `--inputs DIR`
   for Python runners, harnesses and lockfiles instead of embedding them in Bash;
   the worker exports `JOB_INPUT_DIR`. For resumable tasks, use `--stages PLAN.json`
   instead of `--script`; stages are task-defined, not tied to a build tool.
   See [task files and stage recovery](references/stages.md). Install declared
   dependencies first. Bash runs with `-euo pipefail`; each stage has its own shell.
3. Use isolated, reusable environments. With `--environment-key KEY`, a new
   source snapshot gets a retained `$JOB_ENV_DIR` scoped to repository, instance
   profile and compatibility key; without it, the environment is job-local.
   Include runtime/dependency/build compatibility in the key and verify installed
   versions before reuse. Place outputs in `$ARTIFACTS_DIR`. `$JOB_DIR` is the
   job's persistent directory; its working directory is the uploaded source.
4. Add task-specific assertions: a zero exit code alone is insufficient if the
   expected tests, records, service responses, metrics or output files are missing.
   Record source revision, machine, runtime versions and task parameters; include
   seeds and statistical parameters when applicable.
5. Before expensive repetition, run the smallest representative command and
   check that the output assertions and parsers accept its real output. Keep
   successful builds when resuming a failed assertion; do not rebuild them.
   Choose build settings for the requested task and state the cold-start cost.
6. Report phases using `$JOB_WORKER progress` (see execution.md), including
   meaningful counters where available. Distinguish setup, build, validation,
   execution and collection; do not infer a hang from an unchanged log alone.
7. Keep services in the task's process group. Start a service, wait for readiness
   with a bounded health check, run tests, then let the script end. The worker
   terminates remaining processes in that group on completion/cancellation.
   Do not use `setsid`, double-forking daemons, or unmanaged system services.

Example Python test task (adapt to the project's declared runtime, dependency
files and supported test command):

```bash
test -x "$JOB_ENV_DIR/venv/bin/python" || python3 -m venv "$JOB_ENV_DIR/venv"
"$JOB_ENV_DIR/venv/bin/python" -m pip install -r requirements.txt
"$JOB_ENV_DIR/venv/bin/python" -m pytest \
  --junitxml="$ARTIFACTS_DIR/tests.xml"
test -s "$ARTIFACTS_DIR/tests.xml"
```

Do not blindly upgrade everything or install a package simply because its name
resembles a missing import. Prefer declared versions; inspect import-to-package
mapping, the active environment, PATH, and system libraries. Put runtime setup
in the task script or a controller-side recipe cache. Do not edit or commit
`devcontainer.json` in target repositories for this workflow.

## Submit and observe

```bash
python3 "$TOOL" submit --repo OWNER/REPO --source /absolute/checkout \
  --script /absolute/task.sh --timeout 7200 --profile default \
  --environment-key RUNTIME_AND_DEPENDENCY_COMPATIBILITY
python3 "$TOOL" status JOB_ID
python3 "$TOOL" logs JOB_ID
python3 "$TOOL" events JOB_ID --follow
python3 "$TOOL" timings JOB_ID
python3 "$TOOL" health JOB_ID http://127.0.0.1:8000/health
```

`events` reads the retained watcher stream from local files and never wakes an
instance. `--follow` waits for new events and exits at the `final` receipt; exit
code 3 means the watcher is gone and no receipt was written. Prefer it over
writing an ad hoc log filter.

Choose exactly one source mode: `--source /absolute/checkout` uploads the actual
local working tree; `--git-url https://HOST/OWNER/REPO.git --git-commit FULL_SHA`
fetches directly on the worker and verifies the immutable commit; `--continue-from`
reuses the prior workspace and its changes. Remote Git preserves Git metadata but
does not initialize submodules or supply missing private-repository credentials.
It never falls back silently to a different source. Resolve full SHAs first; do
not pass floating branch names or URLs with embedded credentials.

Submission records a durable job ID, exclusively reserves an instance, starts it
if stopped, uploads one input bundle, bootstraps a detached remote worker,
and starts a detached **controller watcher**. The bundle contains the worker,
script and job metadata; optional task files, manifests and a stage plan share this
single upload. Only local snapshot mode adds a source archive. Existing
source, dependencies and binaries are not re-uploaded for continuations. The
outer bundle is uncompressed because a local source snapshot is already compressed.
One SSH bootstrap verifies SHA256 and allowed members before extraction, then
launches the worker. A known pre-launch failure is failed; a lost launch reply
remains unknown and must not cause a duplicate submission. The worker records logs, process
identity, exit code, progress and timeout state. Submission exits nonzero on a
known failure (1) or uncertain launch (2); keep the printed job ID and inspect it
before retrying. The watcher collects evidence, requests stop and confirms
`Shutdown` before releasing the instance lease. The controller host must remain
running.

Controller phases record timestamps, monotonic durations, outcome and each retry
in `controller-timings.json`, including `pack`, `upload`, and `bootstrap`.
Remote checksum/extraction and worker-launch durations are kept separately in
`transport-timings.json`; remote Git adds a worker `source_fetch` interval.
Worker preparation/execution/cleanup use a separate `worker_timings` namespace. Task `progress` transitions retain intervals in
`task-phases.json`, without resetting on same-phase counter updates. The local
`timings` command reads retained evidence without waking the instance. Controller,
worker and task intervals can overlap and must not be added into a total. Missing
historical timings remain missing; do not infer them from unrelated timestamps.

The watcher coalesces ordinary counter/message changes to one notification per
45 seconds, ignoring timestamp-only changes. Observed phase/state changes,
errors, and terminal outcomes are reported immediately, before cleanup retries.
Complete progress events remain in `progress-events.jsonl`; `status` exposes the
latest value. Use watcher notifications for user updates; do not stream raw task
logs or announce every iteration. A finished watcher's last line is a receipt
marked `final: true` carrying `collected`, `shutdown_confirmed_at` and any
`cleanup_error`, not a second full status record. `watch --notify-interval N`
adjusts coalescing when restarting a watcher; the observation interval remains
20 seconds.

The default deadline is two hours; the maximum accepted deadline is eleven
hours, leaving headroom under Codespaces' documented twelve-hour session limit.
Idle timeout can still interrupt background work: neither CPU activity nor a
log file is a guarantee of continued service. Use checkpoints for valuable work.

Read stdout JSON; do not infer success from the submit command returning.
States are `preparing`, `starting`, `running`, `succeeded`, `failed`, `timed_out`,
`cancelled`, `interrupted`, and `unknown`. `unknown` is not evidence of completion.
SSH/API failure does not prove the service failed. A disappeared worker or a
stopped instance without a terminal result is an interruption, not success.
If the response to submit was lost, inspect `list` and its leased job IDs before
retrying. Do not blindly submit the same work again; each submission is a new job.
Status, logs, health, and cancellation queries do not intentionally wake a
stopped Codespace. Explicit `fetch` may wake it and reports that it is left running.

The health command is a point-in-time HTTP probe on the remote loopback
interface; it does not automatically monitor services. During active service
work, poll health separately from process state. Startup health may fail before
readiness; use a startup deadline. Test the endpoint required by the task, not
only whether a TCP port exists. A permanent service has no natural "finished"
state; define the test or benchmark that determines when the job should end.

## Diagnose and repair

On failure, read the bounded task logs, worker status, and controller logs. Check
the actual environment before changing it. Distinguish missing packages,
version conflicts, missing system libraries, service dependencies, configuration,
application bugs, and infrastructure failures. Exit 137 alone does not establish
an OOM; seek corroborating evidence.

The watcher normally collects terminal jobs and confirms shutdown itself. Use
`finish` to recover a terminal job when the watcher is absent/stalled, not to
compete with active cleanup. `cancel` is an intentional interruption of a live
job, not an alternative to `finish`. If either action is denied, do not retry it
through another command; observe any already-running watcher and report whether
`collected` and `shutdown_confirmed_at` were actually recorded.

```bash
python3 "$TOOL" finish JOB_ID
```

For a staged task, submit the repaired inputs with `--continue-from JOB_ID
--stages PLAN.json --inputs DIR --from-stage validate`; the worker revalidates
successful prior stages' declared input and artifact hashes before skipping them.
An absent/failed receipt or changed dependency refuses reuse; it does not silently
rebuild. For a one-script task, submit a repair script on the same workspace:

```bash
python3 "$TOOL" submit --repo OWNER/REPO --continue-from JOB_ID \
  --script /absolute/repair-and-retry.sh --profile default
```

This produces a new job ID with separate logs/artifacts while preserving the
previous source directory and environment. Do not re-create an existing virtualenv
in a repair script; use the recorded `$JOB_ENV_DIR`. Limit automatic repair to
three attempts within the user's total runtime budget, then report the concrete
blocker. Credentials, unsupported hardware, licensing, and missing input data
are not resolved by speculative package installation.

For a new commit/source tree, use a new snapshot. Reuse a previous dependency
environment only after verifying compatibility; never silently mix repositories.

## Finish and recover

```bash
python3 "$TOOL" cancel JOB_ID          # request remote cancellation
python3 "$TOOL" finish JOB_ID          # collect terminal evidence and stop
python3 "$TOOL" fetch JOB_ID           # explicit artifact retrieval; may wake
python3 "$TOOL" stop CODESPACE_NAME    # refuses active/unresolved jobs
```

The watcher writes `watch.log`; collected logs, artifacts, and `job.json` are in
the controller state directory's `jobs/JOB_ID`. A collection error is recorded
and remote files are preserved before the instance is stopped. Inspect the
error, fetch again explicitly when needed, then stop it again.

For an interrupted agent session, `list` reveals instance leases and job IDs.
Resume observation with `status`/`logs`. Restart a dead watcher with:

```bash
nohup python3 "$TOOL" watch JOB_ID >/absolute/watcher-resume.log 2>&1 </dev/null &
```

Only one watcher per job holds its watcher lock. One shared state directory is
required across controllers; independent registries are not a distributed lock.
Do not reuse a lease merely because SSH failed. Preserve unresolved work.

Never call Codespaces delete APIs, `gh codespace delete`, destructive resets,
or automatic disk cleanup as part of this skill. Code changes remain in the
remote workspace after stop; export any intended changes as reviewed artifacts
before reporting them as delivered. Do not push commits merely to persist files.

## Packaging limits

The built-in source snapshot copies tracked and nonignored untracked files from
the actual working tree, including local edits and deletions. It excludes Git
metadata. It deliberately rejects symlinks, submodule directories, and included
`.env` files: do not silently claim these were preserved. For tasks requiring
Git history, submodules, LFS, symlinks, ignored fixtures, or native credentials,
prepare an explicit repository-specific staging/clone recipe and verify it
before execution. The generic snapshot is not a replacement for every Git clone.

Keep long user-facing scripts out of the response. Report the job ID, outcome,
evidence, result locations, and whether shutdown was confirmed. Clearly separate
offline validation from a live GitHub/SSH run.
