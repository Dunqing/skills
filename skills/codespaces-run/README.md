# Run tasks in GitHub Codespaces

Run tests, builds, benchmarks, data jobs, and temporary services in reusable
[GitHub Codespaces](https://docs.github.com/en/codespaces). An agent prepares the
task, submits it, collects evidence, and stops the instance after use. Files and
dependency caches remain available for later jobs. The tool never deletes a
Codespace.

This skill is installed on the agent/controller side. Target repositories need
neither the skill nor a custom `devcontainer.json`. Existing devcontainer
configuration and startup hooks must still be inspected before use.

## Why run in Codespaces

- **Your machine stays free.** Long builds, full test suites, and repeated
  benchmark rounds run remotely. They do not compete with your editor, browser, or
  another compile for the CPU and memory you are working on.
- **The environment is reproducible.** Toolchain, dependencies, and build caches
  live in the instance and are reused across jobs, so a result depends on the code
  and inputs you chose rather than on what happens to be installed on your laptop.
- **A benchmark gets a quiet machine.** Local numbers are contaminated by whatever
  else is running; a dedicated instance gives each round the same machine, memory,
  and toolchain.
- **The conditions are recorded.** Machine specs are checked before and after
  startup, and the source revision, task parameters, raw samples, and scripts are
  kept in the job directory, so a number can be explained later.
- **Costs stay bounded.** The instance is stopped after collection and never
  deleted; retained files and caches serve the next job instead of rebuilding.
- **Personal accounts include free usage.** [GitHub
  billing](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces)
  gives GitHub Free 120 compute hours and 15 GB-month of storage per month, and
  GitHub Pro 180 and 20. Compute is charged per core, so one hour on a 4-core
  machine consumes 4 hours. Organization-owned repositories have no included quota,
  and their usage bills to the creator or the organization.

### Especially for benchmarks

- Keep A/B on **one instance, one machine type, one toolchain, one build
  configuration**. A different machine is reported rather than silently
  substituted, so a changed condition shows up instead of quietly skewing the
  comparison.
- **Keep the previous build.** Package and compiler caches stay on the instance, so
  only changed inputs rebuild; recipes can additionally reuse a previously built
  binary once its receipt matches the current inputs.
- **Keep the raw evidence.** Samples, harness scripts, machine specs, and the
  commands that produced them land in the job directory.
- **Compare within a round, not across machines or days.** These are shared cloud
  VMs, and instance startup plus confirmed shutdown add roughly a minute or two to
  the round trip, so a short task may not be worth it.

## Install

```bash
npx skills add Dunqing/skills --skill codespaces-run
```

Select your agent in the prompt, or pass its `-a` identifier. Add `-g` for use
across projects. The [collection README](https://github.com/Dunqing/skills#updates-and-releases)
covers releases and manual installation. Keep this entire directory: [SKILL.md](SKILL.md)
loads the scripts and references relative to its own location.

## Requirements

| Component | Requirements |
| --- | --- |
| Controller | Linux or macOS, Python 3.9+, Git (required for `--source` snapshots), an authenticated GitHub CLI (`gh`) whose Codespaces, SSH, and file-transfer commands work for your account, and writable durable local state. Windows is not supported. |
| Codespace | Linux, Python 3.9+, Bash, readable `/proc`, writable `/workspaces`, and the tools needed by the task. Git is required for remote Git source mode. |
| Account | Codespaces enabled for the intended account and repository, sufficient quota or spending allowance, and permission for lifecycle/SSH/file-transfer operations. |
| Connectivity | GitHub API and the connection/tunnel destinations used by `gh codespace ssh` and `gh codespace cp`. |

The Python helpers use only the standard library. Node.js is needed only if you
choose the Skills CLI installer. GitHub connector access alone does not establish
that `gh` can create, connect to, or stop a Codespace. A classic personal access
token or OAuth token needs the `codespace` scope for the Codespaces API. Repository,
organization, enterprise, and billing policies can still prevent creation.

## Check the setup

Ask the agent to verify the setup before the first job. It runs the bundled
`doctor` check, which confirms `gh` is installed, reads the signed-in account, and
lists your Codespaces. That catches a broken CLI or missing API access early, but
it does not prove repository-specific creation rights, SSH/file transfer, remote
runtime compatibility, or eventual shutdown, so a passing check is not a promise
that a job will run.

## Quick start

Installing the skill creates a `/codespaces-run` command. Everything after it
is your task, in ordinary words:

```text
/codespaces-run Run the minifier tests for oxc-project/oxc at abc1234, save the JUnit XML, and stop afterward.
```

A task needs three things: a repository and revision, what to run, and what counts
as success. For example:

```text
/codespaces-run Benchmark commits abc1234 and def5678 of acme/router with the same machine and toolchain, keep the raw samples, then stop.
```

```text
/codespaces-run Start acme/api, wait for readiness, test POST /v1/jobs, save the logs, then stop.
```

First time only, tell the agent which Codespace to use: either name an existing
dedicated one to adopt, or say it may create instances and within what limit. You
can see your instances at [github.com/codespaces](https://github.com/codespaces)
and create one at [github.com/codespaces/new](https://github.com/codespaces/new).
A fresh installation creates nothing on its own.

If your agent has no slash commands, say the same thing in a sentence and name
the skill: "Use the codespaces-run skill to run …".

## What happens next

- **Progress** appears in the reply as phases such as setup, build, run, and
  collect, with counters when the task provides them. Ordinary updates are
  coalesced, so silence during a long compile is normal; ask for status any time.
- **The result** comes back as a job ID, an outcome, and the paths of the logs and
  artifacts. Evidence stays on the controller under
  `~/.local/state/codespaces-run/jobs/JOB_ID/`: `job.json`, `watch.log`,
  `output.log`, `artifacts/`, and lifecycle timings. Nothing is deleted, so a later
  question can be answered from that directory.
- **Failure** is diagnosed from the logs; the agent may repair and retry, at most
  three attempts, each as a new job ID. A failed check after a successful build
  resumes from the retained workspace instead of rebuilding. Ask to cancel to end
  work early.
- **Cleanup** is automatic: collection, a stop request, and a confirmed `Shutdown`.
  The instance and its files remain for the next job; the tool has no delete
  operation.

A run looks like this (illustrative):

```text
> /codespaces-run Run the minifier tests for oxc-project/oxc at abc1234, save the JUnit XML, and stop afterward.

job 8f2c19… on cs-oxc (standardLinux32gb), phase build
job 8f2c19…, phase run, 1204/2093 tests
job 8f2c19… succeeded: 2093 tests, 0 failures
artifacts: ~/.local/state/codespaces-run/jobs/8f2c19…/artifacts/junit.xml
log:       ~/.local/state/codespaces-run/jobs/8f2c19…/output.log
instance cs-oxc stopped, Shutdown confirmed
```

You get the job ID back even when the run fails or the connection to the instance
is lost; that ID is what makes the evidence traceable later.

## Effects on your account and data

- **Adopted instances are dedicated.** Managed jobs stop them after use, ending
  running processes; saved files remain.
- **One repository, one instance.** Jobs reuse that instance serially. A busy
  instance or incompatible profile/machine is reported, never worked around by
  creating a second instance or borrowing another repository's.
- **Local source is uploaded.** A local snapshot sends the actual working tree,
  minus Git metadata, symlinks, submodules, and `.env` files. That is not a general
  secret scanner, so review what the checkout contains before a task runs.
- **Tasks are not sandboxed from credentials.** They run as the Codespace user
  and inherit its remote environment. Do not use this for untrusted code without
  a separate credential and network policy.
- **Compute and retained storage can be billed.** Personal accounts have the
  included monthly usage above; organization-owned repositories do not. Stopped
  instances still hold storage until GitHub's retention removes them. The instance
  limit bounds the managed pool; it is not a monetary spending cap.

## Lifecycle and limits

The watcher collects results, requests stop, and confirms `Shutdown` before
releasing the instance. Keep the controller host running: a remote worker can
enforce its task deadline, but it cannot guarantee prompt shutdown if the
controller disappears.

New instances use `ceil(task timeout seconds / 60) + 15` minutes of idle timeout,
bounded to 30–240 minutes; the default two-hour task gets 135 minutes. Reused
instances retain their existing setting. This is an inactivity limit controlled
by Codespaces, not a shutdown scheduled 15 minutes after the task ends.

Creation requests 30-day retention. Stop-only cleanup does not prevent GitHub's
retention policy from deleting a stopped instance later. Check **Keep codespace**
when longer retention is required; organization policies may constrain it, and
this tool does not enable it automatically.

## Documentation

- [Agent instructions and command interface](SKILL.md)
- [Execution, sources, dependency reuse, and progress](references/execution.md)
- [Task-defined stages and verified resume](references/stages.md)
- [Lifecycle, authentication, recovery, and validation limits](references/operations.md)
- Optional recipes: [Rust](references/rust.md) and [Rolldown](references/rolldown.md)
- [GitHub creation/access troubleshooting](https://docs.github.com/en/codespaces/troubleshooting/troubleshooting-creation-and-deletion-of-codespaces)

## License

[MIT](LICENSE).
