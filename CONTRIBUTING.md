# Contributing

The [Contributor Licence Agreement](CONTRIBUTOR-LICENSE.md) applies to all contributions.

## Branching model

| Branch | Purpose |
|---|---|
| `main` | Releases only. Each milestone comes from `develop` with a merge and gets the tag `vX.Y.Z`. |
| `develop` | The integration branch. Feature branches merge into it with `--no-ff`. |
| `feat/<milestone>-<topic>` | One unit of work. Make the branch from `develop`. |

```bash
git checkout -b feat/m1-my-change develop
# ... commit ...
git checkout develop && git merge --no-ff feat/m1-my-change
```

## Before you commit

Run these checks:

```bash
make fmt lint typecheck test
```

## Writing style

Write all text for users in ASD-STE100 Simplified Technical English: documentation, text in
the web app, and the messages that the server sends for the app to show. The rules are in
[Writing style](docs/project/writing-style.md).

## Commit messages

Use conventional commits: `feat(sdk): …`, `fix(server): …`, `docs: …`, `test: …`, `chore: …`,
`refactor: …`.

## Code style

- Use Python 3.12 and ruff (line length 100). Add type hints to all code. The core packages
  use mypy in strict mode.
- Put the tests of each package in its `tests/` folder. PyMC fits in tests must use very
  small draw counts.
- Keep docstrings short (Google style). The contract tests are the executable documentation.
