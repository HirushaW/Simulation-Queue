# Contributing to SimQ

## Branches

`master` is the stable branch: every commit on it passes the checks and can be
installed. Work happens on short-lived branches cut from `master`, named by
the kind of change:

| Prefix | For | Example |
|---|---|---|
| `feature/` | new functionality | `feature/gromacs-progress` |
| `fix/` | bug fixes | `fix/cli-wording` |
| `docs/` | documentation only | `docs/readme-guide` |
| `refactor/` | code changes that keep behaviour | `refactor/scheduler-plan` |

```bash
git checkout master && git pull
git checkout -b feature/my-change
# … edit, commit …
git push -u origin feature/my-change
```

Then open a pull request into `master`. Keep one topic per pull request.

## Before you push

```bash
pip install -e ".[dev]"
ruff check .
ruff format --check .
QT_QPA_PLATFORM=offscreen pytest
```

GitHub Actions runs the same checks on Python 3.10 and 3.12 for every pull
request. A pull request is merged only when they pass.

## Commits

- Start with a short summary line in the imperative ("Add …", "Fix …"), at most
  72 characters, followed by a blank line and an explanation of *why*.
- One logical change per commit.

## Releases

1. Update the version in `simq/__init__.py` and add a section to `CHANGELOG.md`.
2. Merge to `master`.
3. Tag it: `git tag -a v0.2.0 -m "SimQ 0.2.0" && git push origin v0.2.0`.

## Tests

- Unit tests sit next to the area they test (`tests/test_scheduler.py`,
  `tests/test_tcl.py` …).
- `tests/test_queue.py` runs the real queue service with real processes. The
  UDB tests run the scripts in `tests/data/udb_g4/` against the fake programs
  in `tests/fakes/`. Those fakes can be told to crash at a given step
  (`FAKE_NAMD_FAIL`), which is how crash recovery is tested.
- New behaviour needs a test; a bug fix needs a test that failed before the fix.
