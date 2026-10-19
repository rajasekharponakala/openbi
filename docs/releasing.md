# Releasing and repository settings

## Cutting a release

1. Update `__version__` in `openbi/__init__.py` (the single source of the version).
2. Commit, then tag and push:

   ```bash
   git tag v0.2.0 && git push origin v0.2.0
   ```

3. `.github/workflows/release.yml` runs the tests and then builds:

   | Job | Output | Tool |
   |---|---|---|
   | Wheel and sdist | `openbi-<v>-py3-none-any.whl`, `openbi-<v>.tar.gz` | `python -m build` (hatchling) |
   | Executable (×5) | Windows `.exe`/`.zip`, Linux x86_64/arm64 and macOS arm64/x86_64 `.tar.gz` | PyInstaller (`packaging/openbi.spec`) |
   | Windows installer | `openbi-<v>-windows-x86_64-setup.exe` | Inno Setup (`packaging/windows/openbi.iss`) |
   | .deb and .rpm (amd64, arm64) | `openbi_<v>_<arch>.deb`, `openbi-<v>-1.<arch>.rpm` | nFPM (`packaging/nfpm.yaml`) |
   | Docker image | `ghcr.io/<owner>/openbi:<v>`, `:<major>.<minor>`, `:latest` (linux/amd64 + arm64) | Docker Buildx |
   | GitHub Release | all of the above + `SHA256SUMS` | softprops/action-gh-release |
   | PyPI (optional) | the wheel and sdist | PyPI trusted publishing |

   Every executable is smoke-tested on its own runner. The test builds the
   sample project, runs a query, starts the web service and calls the API. The
   `.deb` is installed and removed in the job, and the Windows installer is
   installed silently.

Run the workflow manually (**Actions → Release → Run workflow**) to build all
artifacts without publishing. Tick *publish* to publish from a manual run.

### Optional: PyPI

1. On PyPI, add a trusted publisher for this repository with workflow
   `release.yml` and environment `pypi`.
2. In the repository settings, create the variable `PUBLISH_PYPI` = `true`.

### Notes

- The macOS binaries are not signed or notarized. After extracting, users
  run `xattr -d com.apple.quarantine openbi`.
- Linux binaries are built on Ubuntu 22.04 (glibc 2.35), so the `.deb`/`.rpm`
  packages target distributions from 2022 onward.

## Repository description, topics and labels

These live in version control:

- `.github/repo.yml`: description, homepage and topics
- `.github/labels.yml`: issue/PR labels (`type:`, `area:`, `priority:`, `status:`)
- `.github/labeler.yml`: automatic `area:` labels on pull requests by changed path
- `.github/ISSUE_TEMPLATE/`: bug and feature forms that apply `type:` and `status: needs-triage`
- `.github/dependabot.yml`: dependency updates labelled `dependencies`

`.github/workflows/repo-settings.yml` applies them on pushes to `main` that
change these files, or when run manually. The workflow's `GITHUB_TOKEN`
syncs the labels.

Changing the description and topics needs administration rights, which
`GITHUB_TOKEN` never has. To enable it, create a fine-grained personal access
token with **Administration: read and write** on this repository and save it
as the secret `REPO_ADMIN_TOKEN`. Without it, the workflow skips that step
and prints a notice.
