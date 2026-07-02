# Release Operations Guide

This document summarizes the procedure and policy for releasing loop-agent to PyPI.
Publishing is performed automatically by GitHub Actions
([`.github/workflows/release.yml`](../.github/workflows/release.yml)) through
**OIDC Trusted Publishing**. It uses neither API tokens nor secrets.

## Versioning Policy (SemVer)

Use `MAJOR.MINOR.PATCH` versions according to
[Semantic Versioning](https://semver.org/).

- **MAJOR**: Changes that break backward compatibility, such as removing,
  renaming, or changing the signature of a public API, or making incompatible
  changes to a persistent schema.
- **MINOR**: Backward-compatible feature additions, such as new public APIs, new
  extras, or new options.
- **PATCH**: Backward-compatible bug fixes, documentation changes, and internal
  improvements.

### Notes for the 0.x Series

During `0.y.z`, the public API is not covered by a stability guarantee. In
practice, use the following policy:

- Raise MINOR (`y` in `0.y`) for breaking changes.
- Raise PATCH (`z`) for feature additions and bug fixes.
- Once the public API is stable, release `1.0.0` and move to strict SemVer from
  then on.

"Public API" means the symbols exported through `__all__` in
`loop_agent/__init__.py`.

### Compatibility After 1.0.0

After `1.0.0`, [stability.md](./stability.md) is the authoritative stability
contract.

- Removing, renaming, or making incompatible signature changes to a public API is
  MAJOR.
- Backward-compatible feature additions, new options, and new helpers are MINOR.
- Bug fixes, docs, internal improvements, and backward-compatible metadata fixes
  are PATCH.
- CLI subcommand names, exit codes, and primary TOML keys are included in the
  stability contract.
- Non-destructive migrations for state.db are MINOR/PATCH; changes that break
  read compatibility with existing DBs are MAJOR.

For breaking changes, announce the deprecation in a minor release whenever
possible, then remove the deprecated behavior in a subsequent major release. If
the old behavior cannot be preserved for safety or correctness reasons, document
the reason and migration steps in the CHANGELOG.

### 1.0.0 Release Gate

This is the `1.0.0 release gate`.

Before releasing `1.0.0`, the following must be true:

- [stability.md](./stability.md) is reachable from the README.
- The `pyproject.toml` classifier is `Development Status :: 5 - Production/Stable`.
- `pyproject.toml` / `loop_agent.__version__` / `CHANGELOG.md` / tag all use the
  same version.
- `python -m ruff check .` passes.
- `python -m mypy` passes.
- `python -m pytest` passes.
- `python -m build` passes.
- `python -m twine check dist/*` passes.
- `python scripts/verify_wheel_skill_bundle.py` passes.

## Single Version Source

The version is written in two places. They **must match** before release:

1. `[project].version` in [`pyproject.toml`](../pyproject.toml)
   (this becomes the version of the build artifacts, namely the wheel/sdist; the
   tag publishes this version)
2. `__version__` in
   [`src/loop_agent/__init__.py`](../src/loop_agent/__init__.py)

Align the `X.Y.Z` in the `git tag` value `vX.Y.Z` with the two values above. If
the tag version and the pyproject version differ, a package version that does
not match the tag name may be published.

## Release Procedure

1. **Version bump**: Update the versions in `pyproject.toml` and `__init__.py`
   to the new `X.Y.Z`.
2. **Update CHANGELOG**: Move the contents of `[Unreleased]` in
   [`CHANGELOG.md`](../CHANGELOG.md) into an `[X.Y.Z] - YYYY-MM-DD` section and
   finalize the date. Leave a new empty `[Unreleased]` section, and update the
   link definitions at the end of the file (compare URLs).
3. **Create PR -> review -> merge to `main`**: Open a PR containing the changes
   above, confirm that CI ([`ci.yml`](../.github/workflows/ci.yml)) is green,
   then merge it to `main`.
4. **Push tag**: Create and push a `vX.Y.Z` tag on the relevant commit in
   `main`.

   ```bash
   git checkout main && git pull
   git tag v1.0.0
   git push origin v1.0.0
   ```

   This is the **final gate performed by human judgment**. Pushing the tag
   triggers publishing.
5. **Automatic publish**: Pushing a `v*` tag starts `release.yml`, which runs
   `python -m build` -> `twine check` -> PyPI publish.
6. **Verify**: Confirm that the new version appears on the PyPI page
   (https://pypi.org/project/loop-agent/) and that the GitHub Actions job
   succeeded.

### Local Verification Before Release

Before creating the tag, you can run the same checks as the workflow locally:

```bash
python -m pip install -e .[dev]
python -m ruff check .
python -m mypy
python -m pytest
python -m build                                # Generate wheel and sdist in dist/
python -m twine check dist/*                   # Validate metadata / long description
```

`twine check` also verifies that the README (long description) will render
correctly on PyPI. Because `readme = "README.md"`, the content type is
automatically set to `text/markdown`.

## How OIDC Trusted Publishing Works

Publishing to PyPI is done through **OIDC (OpenID Connect) Trusted Publishing**.
The key point is that the repository does not hold a long-lived API token and
**does not store any secrets**.

Flow:

1. On the PyPI side, register in advance a **trusted publisher** that says "this
   PyPI project trusts publishing from this workflow in this GitHub repository"
   (specifying the repository, workflow file name, and environment).
2. During workflow execution, GitHub Actions issues a short-lived **OIDC token**
   (a signed JWT proving the source repository, workflow, ref, and related
   claims).
3. `pypa/gh-action-pypi-publish` presents that OIDC token to PyPI. PyPI compares
   it with the registered trusted publisher, validates it, and returns a
   **short-lived publishing credential for that run only**.
4. That short-lived credential is used to upload the wheel/sdist. The token
   expires when the job ends.

For this reason, the job in `release.yml` needs the following permissions:

```yaml
permissions:
  id-token: write   # Allows the workflow to receive an OIDC token (the core of Trusted Publishing)
  contents: read
```

Without `id-token: write`, the workflow cannot obtain an OIDC token and
publishing fails. Conversely, with this permission in place, there is no need to
store a PyPI API token in secrets.

> NOTE: Registering the trusted publisher is a manual setting on the PyPI side;
> it cannot be completed solely in repository code. For new projects or changes
> to publisher settings, confirm in PyPI's "Publishing" settings that the target
> workflow is registered.

## Incident Response

### Yank (Withdraw a Published Version)

If a broken version has been published, **yank it instead of deleting files**
from PyPI. A yanked version can still be resolved for existing users who
explicitly pin that version, but it is excluded from candidates for a new
`pip install loop-agent`.

- Procedure: Yank the target version from the PyPI project management screen
  (Manage -> Releases).
- You cannot re-upload the same version number (PyPI does not allow overwriting
  versions). Release the fix as the next PATCH version, for example `0.1.1`.

### Emergency Fix (Hotfix)

1. Create a `fix/...` branch from `main` and apply the smallest necessary fix.
2. Raise PATCH, for example `0.1.0` -> `0.1.1`. Record the fix under `Fixed` in
   the CHANGELOG.
3. Publish using the normal release procedure (PR -> merge -> tag push).
4. If the old version has a serious defect, yank it as described above to guide
   users to the new version.

### If Publishing Fails

Check the GitHub Actions job log for `release.yml`. Common causes:

- `twine check` failure (invalid metadata / long description) -> fix it and
  release again.
- OIDC failure (`id-token: write` missing / trusted publisher not registered on
  the PyPI side / repository or workflow name mismatch) -> check the permission
  and PyPI settings.

If the job failed before uploading to PyPI, the version has not been published,
so after fixing the issue you can recreate the same tag (delete the existing tag
first, then push it again). If the job reached the upload step, the version is
final; handle it with yank + next PATCH.
