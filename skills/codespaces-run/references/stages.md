# Task-defined execution stages

The normal `--script task.sh` entrypoint remains supported. For a task that may
need to resume after an expensive step, use `--stages PLAN.json` and independent
`--inputs DIR` files. The controller and worker do not assume Cargo, a benchmark
framework, or fixed stage names.

## Declare a plan

```json
{
  "stages": [
    {
      "name": "build",
      "script": "build.sh",
      "inputs": {
        "workspace": ["dependency.lock", "source-manifest.json"],
        "inputs": ["build.py", "common-env.sh"]
      },
      "outputs": {"env": ["binaries/tool", "binaries/build-receipt.json"]},
      "verify": "verify-build.sh"
    },
    {"name": "validate", "script": "validate.sh"},
    {"name": "run", "script": "run.sh"}
  ]
}
```

This example assumes those files and scripts are supplied by the task. Each stage
runs its Bash script with `-euo pipefail`, in the source workspace, inside the task's
process group and deadline. Scripts and optional `verify` scripts are relative to
`JOB_INPUT_DIR`. The three roots for declared input files are `workspace`, `env`
and `inputs`; output files can use `workspace` or `env`. Paths must name regular
files beneath the root; absolute paths, traversal and symlinks are rejected.
Outputs that will be reused belong in the retained workspace/environment, not
in a previous job's report directory. Reports still go in `ARTIFACTS_DIR`.

Each stage has a separate shell. Exports from `setup.sh` do not automatically
propagate to `build.sh`; source a shared environment script in each stage and
include that script among its declared inputs. Declare all relevant dependency
files or include a source-inventory receipt plus a verifier that checks that
inventory against the actual tree. Undeclared source/runtime changes cannot be
detected automatically. A `verify` script should check runtime versions, source
inventory or other semantic assertions that file hashes alone cannot establish;
it runs after execution and again before reuse, and must not repair artifacts.

The worker saves per-stage input fingerprints, output hashes and outcomes in
`stage-results.json`. Script bytes, verifier bytes and the stage definition are
included automatically; other task dependencies must be declared explicitly.
`progress-events.jsonl` and `task-phases.json` retain progress separately. A progress
phase named `build` is not a successful stage receipt.

## Resume explicitly

```bash
python3 "$TOOL" submit --repo OWNER/REPO --source /absolute/checkout \
  --stages /absolute/plan.json --inputs /absolute/task-files
```

After a failed validation, repair the validation files and resume:

```bash
python3 "$TOOL" submit --repo OWNER/REPO --continue-from JOB_ID \
  --stages /absolute/plan.json --inputs /absolute/task-files --from-stage validate
```

Keep the original profile and resource flags where required. Each submission is
a new job with new logs/artifacts; it reuses the previous workspace and environment.
Omitting `--inputs` on a continuation reuses the previous immutable task files.

Before invoking `validate`, the worker checks every stage before it in plan order:

- Its prior receipt must say `succeeded` or `reused`.
- Its definition, script, verifier and declared input hashes must match.
- Every declared output must still exist and have the recorded hash.
- The stage must declare at least one output or a verifier to be skipped.
- Any verifier must succeed and leave the declared output bytes unchanged.
- After all verifiers complete, every retained stage's input fingerprint and output
  hashes are checked again. A later verifier must not change an earlier stage's
  inputs or artifacts; a detected change marks that receipt `invalidated`.

A mismatch refuses the skip before the requested stage runs. Choose an earlier
`--from-stage` explicitly to rebuild; there is no silent restart. Do not change
skipped stages' inputs/definitions simply to reuse old artifacts. Updating only
the validation runner does not invalidate an unrelated build stage. Both normal
failure and cancellation leave completed receipts intact; incomplete stages are
never considered reusable. A legacy one-script job has no stage receipts, so it
cannot supply a skipped prefix.

The controller validates schema, stage names and task-file presence locally
before selecting/starting an instance. These checks do not predict automatic
approval, remote tool availability, or correctness of the task's assertions.
