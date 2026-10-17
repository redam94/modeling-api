# Contributing

All contributions are subject to the [Contributor Licence Agreement](CONTRIBUTOR-LICENSE.md).

## Branching model

| Branch | Purpose |
|---|---|
| `main` | Releases only. Each milestone is merged from `develop` and tagged `vX.Y.Z`. |
| `develop` | Integration branch. Feature branches merge here with `--no-ff`. |
| `feat/<milestone>-<topic>` | One unit of work, branched from `develop`. |

```bash
git checkout -b feat/m1-my-change develop
# ... commit ...
git checkout develop && git merge --no-ff feat/m1-my-change
```

## Before you commit

```bash
make fmt lint typecheck test
```

## Writing style

Write all text for users in ASD-STE100 Simplified Technical English: documentation, text in
the web app, and the messages that the server sends for the app to show. The rules are in
[Writing style](docs/project/writing-style.md).

## Commit messages

Conventional commits: `feat(sdk): …`, `fix(server): …`, `docs: …`, `test: …`, `chore: …`, `refactor: …`.

## Code style

- Python 3.12, ruff (line length 100), type hints everywhere, mypy-strict for core packages.
- Tests live next to each package in `tests/`. PyMC fits in tests must use tiny draw counts.
- Docstrings are concise (Google style); contract tests are the executable documentation.
