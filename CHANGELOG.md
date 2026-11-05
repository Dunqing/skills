# Changelog

Versions identify the entire skill collection. Entries are grouped by affected
skill. Release tags are immutable; `main` may contain unreleased changes.

## Unreleased

## 0.1.0 — 2026-11-05

### Collection

- Establish the `skills/<name>/` layout, catalog, installation instructions,
  MIT licensing, contribution guide, and collection-wide version tags.
- Add Linux Python 3.9/3.14 tests and Linux/macOS compilation and CLI smoke checks;
  pin Linux jobs to Ubuntu 24.04 so Python 3.9 remains available.

### codespaces-run

- Add a reusable Codespaces controller and Linux worker for tests, builds,
  benchmarks, data jobs, and temporary services.
- Reuse one registered instance per repository with explicit resource policy,
  compatible dependency environments, and immutable source or local snapshots.
- Support separate task inputs and task-defined execution stages with verified
  receipts before resuming completed stages.
- Collect artifacts and stop after use without deleting instances; retain
  shutdown evidence and report uncertain or failed cleanup.
- Expose coalesced progress events and lifecycle timings from retained local data.
- Derive new-instance idle timeout from the task deadline, bounded to 30–240 minutes.
- Restrict remote status to worker-owned result fields, preserve controller job
  identity and cleanup receipts, and reject malformed status replies.
- Isolate remote Git operations from inherited repository-selection variables and
  verify the effective worktree before fetching or checking out source.
- Reject health-check redirects so a localhost probe cannot request another host.
