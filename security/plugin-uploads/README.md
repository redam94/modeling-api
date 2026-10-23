# Upload corpus (v0.19)

This directory holds the hostile-upload corpus for uploaded code (V0.19-PLAN WP12). The
corpus is test data. Do not install, extract or import it outside a sandbox run.

## Files

| File | Contents |
|---|---|
| `cases.toml` | One entry for each case: name, group, threat (T19.x), the stage that must refuse or neutralize the case, the expected stable result, and the artifact type. |
| `build_corpus.py` | Builds the cases into a directory that you name. The tests build into a tmp directory. |

## Groups and stages

| Group | Stage | Expected result | Test |
|---|---|---|---|
| `archive`, `content`, `metadata` | `inspect` | An `InspectCheck` from `mapi_runner.plugin_inspect.inspect_wheel` | `tests/security/test_plugin_upload_corpus.py` |
| `archive` (tickets) | `upload` | An `ErrorCode` from `POST /v1/plugins/{id}/uploads` | `tests/security/test_plugin_upload_review.py` |
| `text` | `describe` | A `check_describe` rule at the case's JSON pointer | `tests/security/test_plugin_upload_text.py` |
| `text` | `render` | `neutralized`: kept as plain text, not in platform messages or mail, `'` before a CSV formula, plain data in the SBOM | `tests/security/test_plugin_upload_text.py` |
| `review` | `review` | An `ErrorCode` from the review API; the version state does not change | `tests/security/test_plugin_upload_review.py` |
| `runtime` | `runtime` | A bounded run result on docker | Not built yet (see "Status") |

The member contents of the wheels are inert (`x = 1`, zeros, magic-byte headers). The
inspector refuses each case because of its names, structure or metadata. It does not refuse
a case because of what its code does.

## Import sentinel

`tests/security/conftest.py` puts a meta path finder at the start of `sys.meta_path`. The
finder refuses every import of a name that starts with `t_crpsxx_` (the corpus namespace and
the probe namespace). Each test and the session fail if such an import is attempted or such a
module is in `sys.modules`. The corpus tests also replace `ZipFile.extract` and
`ZipFile.extractall` so that they fail.

## Commands

```bash
# The CI gate (the CI `python` job runs it, because `tests` is a testpath)
.venv/bin/pytest tests/security -q -m "not docker"

# Build the corpus into a directory to examine it (the cases are not installed)
.venv/bin/python security/plugin-uploads/build_corpus.py /tmp/corpus
.venv/bin/python security/plugin-uploads/build_corpus.py /tmp/corpus --case bomb_overlap
```

## Status

- Built and tested: all `archive`, `content`, `metadata`, `text` and `review` cases, and the
  two `upload` stage ticket cases.
- Probes as an upload (docker): `tests/security/test_probes_as_upload.py` re-packages the
  security suite's existing probes (`security/plugin`, `security/src/mapi_security`) as an
  uploaded plugin, a rename only, and runs them through the real pipeline on a running
  stack B. It also covers `overlay_swap_after_approve` and `two_overlays_mount_minimal`.
  See the module docstring for the environment it needs.
- Not built: the other `runtime` cases. `build_case` raises `NotBuilt` for them.
  `test_plugin_upload_runtime.py` does not exist yet.
