# Skills

Reusable [Agent Skills](https://agentskills.io) for coding agents. Each skill is a
self-contained directory with instructions, supporting files, and its own
requirements. Install only the skills you need.

## Available skills

| Skill | Purpose | Requirements |
| --- | --- | --- |
| [codespaces-run](skills/codespaces-run/README.md) | Run tests, builds, benchmarks, and temporary services in reusable [GitHub Codespaces](https://docs.github.com/en/codespaces); collect results and stop after use. | Linux/macOS controller with Python 3.9+, Git, and an authenticated GitHub CLI that can create, connect to, and stop Codespaces. |

## Install

Use the [Skills CLI](https://github.com/vercel-labs/skills) with its supported Node.js
version. List the available skills without installing:

```bash
npx skills add Dunqing/skills --list
```

Install a selected skill into the current project and choose your agent interactively:

```bash
npx skills add Dunqing/skills --skill codespaces-run
```

For a specific agent, for example Claude Code:

```bash
npx skills add Dunqing/skills --skill codespaces-run -a claude-code
```

Add `-g` for installation across projects. Other agents can be selected through
the CLI's prompt or `-a` option. Installing a skill does not authenticate services,
provision infrastructure, or authorize spending; follow the selected skill's README.

An installed skill is also a slash command: `/codespaces-run <task in plain
words>`. Each skill's README starts with a quick start for that form.

Alternatively, clone this repository and copy or link an individual
`skills/<name>` directory into your agent's supported skill directory. Include
its scripts and references, not just `SKILL.md`.

## Updates and releases

`main` contains the latest changes. Release tags such as `v0.1.0` identify fixed
snapshots of the entire collection. [CHANGELOG.md](CHANGELOG.md) records changes
by skill; see [CONTRIBUTING.md](CONTRIBUTING.md#versioning-and-releases) for the
versioning policy.

For CLI-managed installations, check and apply updates deliberately:

```bash
npx skills check
```

```bash
npx skills update codespaces-run
```

To install a specific release, check out its tag and install from that local
checkout. For example, after `v0.1.0` is published:

```bash
git clone --branch v0.1.0 --depth 1 https://github.com/Dunqing/skills.git skills-v0.1.0
```

```bash
npx skills add ./skills-v0.1.0 --skill codespaces-run
```

Keep that checkout if you link it directly into an agent. Select a new tag and
reinstall explicitly when upgrading a fixed release; do not treat a moving
branch or an update command as a version pin.

## Repository layout

```text
skills/
  <skill-name>/
    SKILL.md       Agent instructions and metadata
    README.md      Human-facing setup and usage
    scripts/       Optional executable helpers
    references/    Optional detailed guidance
    recipes/       Optional task-specific adapters
    tests/         Tests for bundled executable helpers
CHANGELOG.md       Collection releases, grouped by skill
CONTRIBUTING.md    Adding skills, validation, and release conventions
LICENSE           MIT license
```

Each skill owns its dependencies and validation. Adding a skill does not require
changing the installation or release structure. The GitHub Actions workflow runs
Python compilation checks on Linux/macOS and discovers each skill's Python tests
on Linux; add checks when introducing another runtime.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Keep skills portable: no local account
state, credentials, personal installation paths, or private task history.

## License

[MIT](LICENSE). Bundled skill directories include a license so they can also be
distributed independently.
