# Operations and constraints

## State and concurrency

Default state directory: `~/.local/state/codespaces-run`. Override it with
`--state-dir PATH` before the subcommand, or `CODESPACES_RUN_STATE`.
Use durable controller storage. Credentials are inherited by `gh`, not written
into job records or forwarded in source snapshots.

Only explicitly adopted or tool-created Codespaces are managed. An exclusive
registry lock serializes leases, and a remote instance lock prevents managed
workers from running concurrently on one instance. Instances with unresolved
jobs are not reusable. Prefer one registered Codespace per repository: jobs
reuse that instance serially, and a profile/resource mismatch does not create a
second instance. Profiles express compatible runtime/system requirements; they
do not provide isolation between simultaneous jobs. Concurrent jobs for the same
repository wait or report the constraint rather than borrowing another repository's
instance. New instances use the task repository. Legacy registrations are bound
using live repository metadata; ambiguous metadata or duplicate registrations
are reported without starting, deleting or moving those instances.

The pool limit counts retained managed instances, including stopped instances.
This bounds compute concurrency and the number of retained disks. If all slots
are occupied by incompatible profiles, explain the constraint; never delete a
Codespace to make room. Reconfiguration is an explicit resource-policy change. Profiles do not imply a
machine size. Resource requests are checked against live API specs before lease
acquisition and after startup; the worker separately checks runtime CPU affinity
and physical/cgroup memory limits. Incompatible idle instances are reported rather
than automatically resized/replaced. CPU/memory selection does not invalidate
compatible dependency environments.

Task installation and commands run with the normal Codespace user's privileges.
Directory isolation is not a security boundary. Do not put broad controller
credentials into a task's environment. The host-repository Codespace token may
still be present remotely. A Codespace used for untrusted code must have an
appropriate credential and network policy established separately.

## Background processes

There are two processes:

* The remote worker owns the task process group and its deadline. It writes
  `status.json`, `output.log`, and `worker.log` under
  `/workspaces/.agent-codespaces/tasks/JOB_ID`.
* The external watcher polls every twenty seconds, collects outputs as one
  SHA256-verified archive, requests stop and confirms `Shutdown`, then releases
  the registry lease. Ordinary progress notifications are coalesced to 45 seconds;
  observed stage/state changes, errors and terminal outcomes bypass coalescing.
  The last line of a finished watcher is a `final: true` receipt with collection
  and shutdown evidence, not a repeated full status record.
  Full progress events are retained separately. It retries transient cleanup
  failures and has its own `watch.log` and process lock. A stop request is not
  treated as confirmed shutdown.

If the controller machine stops, the watcher also stops. The remote worker can
still enforce its task deadline while Codespaces runs, but it cannot guarantee
prompt Codespace shutdown. Resume the watcher on the same state directory or
host it on a persistent service manager. `nohup` survives terminal disconnects,
not host shutdown, runtime replacement, or a platform that kills background jobs.

Services that deliberately detach from the task process group are outside the
worker's cleanup contract. Keep service processes as ordinary children of the
task script, and use traps if application-specific shutdown is needed.

## Lifecycle

`stop` ends running processes while retaining saved filesystem changes.
`start` preserves files; it does not restore process memory. Do not store important
state in `/tmp`. A container rebuild may discard installations outside the
persistent workspace; record dependency recipes and keep job environments under
`/workspaces`.

Creation requests `ceil(task timeout seconds / 60) + 15` minutes of idle timeout,
bounded to 30–240 minutes, and 30-day retention. A default two-hour task requests
135 minutes; existing instances keep their setting. The idle timeout measures
inactivity, not time after the task deadline. Organization policies may impose
smaller values. Terminal output can count as activity, but
background CPU work or redirected files alone are not a reliable keepalive.
Do not manufacture heartbeat activity as a substitute for checkpoint/recovery.
GitHub documents a maximum continuous session lifetime of twelve hours.

The Keep codespace setting exempts an instance from ordinary automatic deletion,
unless organization policy prevents it. The tool does not claim to have applied
that setting through an undocumented API. Stopped storage may remain billable.

## Network/authentication diagnostics

The controller needs GitHub API access and the Codespaces connection/tunnel
destinations used by `gh codespace ssh`. Allowing only github.com may be
insufficient. Diagnose actual failures and use the host's supported network
configuration workflow; never bypass enforced proxies or disable TLS checks.

A GitHub app/connector that can read repositories does not necessarily provide
Codespaces lifecycle access or SSH. The default Codespace token is scoped to
its source repository; arbitrary private target repositories need separate
authorization. Controller-side checkout plus source upload avoids forwarding
the controller token into remote tasks.

Worker status can update only defined execution-result fields; the controller
retains job identity, instance selection, paths, and collection/shutdown receipts.
Remote Git clears inherited repository-selection overrides and checks that the
effective repository/worktree belong to the new job before fetching source.
Health probes are restricted to HTTP(S) loopback URLs and do not follow redirects.

Read-only access checks are not a forecast of automatic execution approval.
Before preparing a large task, establish its repository/revision, target machine,
allowed operations and whether local fallback was explicitly authorized. A
Codespaces task cannot silently move to macOS after a denial. If remote
submission or a lifecycle action is blocked, report the exact action and reason;
do not retry the same action through another transport. An existing watcher may
still collect/stop its job: inspect its evidence instead of issuing redundant
`finish` calls. `cancel` remains an intentional interruption, not watcher recovery.

## Validation

```bash
python3 -m unittest discover -s tests -v
```

The tests run real detached local worker processes and a fake `gh` transport.
They cover dependency failures and exit codes, deadlines and process-group cleanup,
lost workers, instance exclusion, snapshot fidelity, traversal rejection, fresh
source with compatible environment reuse, structured progress, artifact collection
and confirmed stop before lease release. The controller supports macOS/Linux;
production worker identity checks require Linux `/proc`. A passing macOS smoke
suite does not validate Linux process identity. Tests do not establish live GitHub
permissions, organization policy, startup or tunnel connectivity. Additional
regressions cover default-machine drift, explicit resource requirements,
post-start resource changes, runtime resource rejection before script execution,
monotonic timing, retries, phase transitions and offline timing reports. Task-file
and stage tests cover one-bundle transport, per-file/inventory integrity, repaired
validation after a successful build, refusal of stale inputs/artifacts, and
receipt validation. Notification tests use a controlled clock; repository tests
ensure foreign instances and busy/mismatched same-repository instances are not
used to create a second instance. These remain local tests with simulated APIs.

## Official references

* [Default dev containers](https://docs.github.com/en/codespaces/setting-up-your-project-for-codespaces/adding-a-dev-container-configuration/introduction-to-dev-containers)
* [Codespaces lifecycle](https://docs.github.com/en/codespaces/about-codespaces/understanding-the-codespace-lifecycle)
* [Retention and Keep codespace](https://docs.github.com/en/codespaces/setting-your-user-preferences/configuring-automatic-deletion-of-your-codespaces)
* [Codespaces REST API](https://docs.github.com/en/rest/codespaces/codespaces)
* [GitHub CLI SSH](https://cli.github.com/manual/gh_codespace_ssh)
* [Additional repository access](https://docs.github.com/en/codespaces/managing-your-codespaces/managing-repository-access-for-your-codespaces)
