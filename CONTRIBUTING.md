# Contributing

## Add or update a skill

Keep each skill under `skills/<skill-name>/`, following the
[Agent Skills specification](https://agentskills.io/specification). The directory
name must match the `name` in `SKILL.md` frontmatter. Use a lowercase name with
single hyphens between words.

Every skill needs:

- `SKILL.md` with a descriptive `name` and `description`; declare specific runtime
  requirements in `compatibility` and identify the license.
- A README covering installation, prerequisites, usage, costs or external actions
  where relevant, and the limits of the evidence supporting it.
- A license bundled with the skill so installation of the directory preserves it.
- Tests when executable helpers implement behavior that needs regression coverage.

Keep task-specific adapters in references or recipes. Generic instructions must
not depend on the author's account, dotfiles, retained job IDs, or private caches.
Resolve supporting paths relative to the skill directory, not the caller's
working directory. Runtime state and task results belong outside this repository.

Update the root README's catalog when adding, renaming, or removing a skill.
Record user-facing changes under `Unreleased` in `CHANGELOG.md`, grouped by skill.
Do not add version numbers to every `SKILL.md`: the Git tag identifies the
collection version without duplicating metadata across all skills.

## Validate

For the Codespaces skill, run from the repository root:

```bash
python3 -I -m unittest discover -s skills/codespaces-run/tests -v
```

The tests use the Python standard library and need Git, Bash, `python3`, `ps`,
`sleep`, subprocess support, and loopback sockets. The full worker suite requires
Linux `/proc` for process-identity assertions. The controller supports macOS, but
a passing macOS run does not establish those Linux assertions.

The CI matrix checks Python 3.9 and 3.14 on Ubuntu 24.04, runs compilation/CLI smoke
checks on macOS, and does not use GitHub credentials or create Codespaces. The
Linux image is pinned because newer Ubuntu images may not provide Python 3.9.
When adding a runtime or a new test harness, extend CI to run the relevant checks.

Check that installers can discover the collection without installing it:

```bash
npx skills add . --list
```

Inspect every staged path before committing. Exclude Python caches, credentials,
source snapshots, job state, logs, artifacts, and downloaded dependencies. Review
changes to executable helpers as code, not just documentation. Live service tests
must be identified separately from local tests using simulated transports.

## Versioning and releases

Use one version for the collection, with immutable `vMAJOR.MINOR.PATCH` Git tags.
No package registry or separate per-skill release tooling is required.

- Patch: fixes and compatible documentation/instruction improvements.
- Minor: new skills or compatible capabilities.
- Major: incompatible installation, command, configuration, or workflow changes.

Before `1.0.0`, use a minor bump for incompatible changes and document migration
steps. A patch release should remain compatible. Removing or renaming an existing
skill is an incompatible change because installation commands depend on its name.

Use focused commits with Conventional Commit subjects, for example
`fix(codespaces-run): preserve stage receipts on retry` or
`feat(new-skill): add release validation`. One commit should describe one logical
change. Do not rewrite published tags or force-push shared release history.

For a release:

1. Finish the changes and pass the affected checks on the exact release commit.
2. Move the relevant `Unreleased` entries to a dated release section, retaining
   an empty `Unreleased` heading for future work. State breaking changes and
   migration steps explicitly.
3. Commit the release changes, then create an annotated version tag at that
   tested commit. Publish the branch and that tag explicitly, avoiding unrelated
   local tags.
4. Verify the published branch/tag targets and the resulting GitHub Actions run.
   If GitHub release notes are created, use the matching changelog entry.

GitHub Actions validates pushes and pull requests. It does not create releases,
publish skills, change repository visibility, or provision remote environments.
Keep publisher credentials out of the repository and do not put live Codespaces
jobs in the ordinary test workflow.

## References

- [Agent Skills format](https://agentskills.io/specification)
- [Anthropic's multi-skill repository](https://github.com/anthropics/skills)
- [Skills CLI installation and management](https://github.com/vercel-labs/skills)
