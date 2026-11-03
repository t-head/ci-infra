# Temporary Wheel Publishing Template

Download and extract the specified artifact from the current GitHub Actions run, recursively find all files ending in `.whl`, and upload them unchanged to:

```text
https://download.t-head.cn/artifactory/pypi_tmp/{timestamp}/{filename}
```

The timestamp is generated once before the first upload, using UTC in `YYYY-MM-DD-HHmm` format, for example `2026-11-02-1211`. Every file in the batch uses that same directory, even if uploading spans multiple minutes. Original filenames are preserved; artifact subdirectories are not included in the destination path.

- Workflow entry point: [`.github/workflows/push-tmp-wheel.yml`](../../.github/workflows/push-tmp-wheel.yml)
- Upload script: [`scripts/push_tmp_wheel.py`](scripts/push_tmp_wheel.py)
- Complete usage example: [`workflows/demo-push-tmp-wheel.yml`](workflows/demo-push-tmp-wheel.yml)

This is a reusable workflow called through `jobs.<job_id>.uses`, not a regular Action used in `steps`. The template exposes only `workflow_call`. It does not build, validate, install, repackage, or rename wheels, or create GitHub Releases, source distributions, or package indexes.

## Execution Flow

```text
Caller builds wheels
  → actions/upload-artifact@v4 saves the artifact
  → Call the upload template with needs on the build job
  → actions/download-artifact@v4 downloads and extracts the named artifact
  → Recursively find all regular files ending in .whl
  → Generate one UTC timestamp for the entire batch
  → Upload every selected file unchanged to the timestamped directory
  → Return the batch directory as upload_url after all uploads succeed
```

Reusable workflows run in separate jobs and do not share local files with preceding caller jobs. Build outputs must be passed through artifacts. The template does not support artifacts from other workflow runs or search the caller repository for wheels. Artifact extraction is handled by `actions/download-artifact@v4`; nested ZIP or tar files inside the artifact are not unpacked.

## Quick Start

1. Copy [`workflows/demo-push-tmp-wheel.yml`](workflows/demo-push-tmp-wheel.yml) to `.github/workflows/demo-push-tmp-wheel.yml` in the project to publish.
2. Configure `RELEASE_USERNAME` and `RELEASE_TOKEN` in the caller repository or organization Actions Secrets. The publishing account needs upload access to the destination temporary repository.
3. Ensure the caller can use workflows from the template repository. Private template repositories must also allow cross-repository calls in Actions settings; configure `TEMPLATE_TOKEN` and uncomment its mapping in the example as needed.
4. Adjust the example build environment and commands for the project. The example uses Python 3.12 and `python -m build --wheel` and requires a buildable Python project in the caller repository. If hggcrt, PPU SDK, or other system dependencies are needed, configure the build runner/container and dependencies, and write build outputs to the directory expected by the example.
5. After committing the example to the caller default branch, trigger it manually through **Run workflow** on the Actions page and select a trusted branch. No package name or wheel filename is required; the example passes the build job's artifact name and uploads all its `.whl` files.
6. After a successful run, find the batch directory URL in the `result` job logs or job summary. Individual file URLs are printed in the upload step logs.

The example uploads to the real temporary repository; it is not a dry run. Files under this module's `workflows/` directory are provided for copying and are not automatically registered as Actions workflows in this repository.

### Template Version and Repository

The example uses the `main` branch of `t-head/ci-infra`. Ensure the referenced revision contains this workflow and helper script before running it.

- For production, replace both `@main` in `uses` and `template_ref: main` with the same full commit SHA from the template repository to prevent workflow and script version drift.
- For cross-repository calls, do not use the caller `github.sha` as `template_ref`.
- When using a fork, update both the repository path in `uses` and `template_repository`.
- For same-repository relative-path calls, use `uses: ./.github/workflows/push-tmp-wheel.yml` with `template_repository: ${{ github.repository }}` and `template_ref: ${{ github.sha }}`.

The caller must grant at least `contents: read`. The template upload job is fixed to `ubuntu-latest` and Python 3.12, with a 30-minute timeout. Its runner must be able to download Actions artifacts and reach `download.t-head.cn` over HTTPS. The upload script uses only the Python standard library and curl; no wheel validation dependencies are installed. Changing the build runner in the example does not change the template upload runner.

## Inputs

All inputs are of type `string`.

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `artifact_name` | Yes | None | Non-empty name of the artifact saved by the caller using `actions/upload-artifact@v4`; it must belong to the current workflow run. |
| `template_repository` | No | `t-head/ci-infra` | Repository containing the template script, in `owner/repo` format. |
| `template_ref` | Yes | None | Template script revision; must match the workflow revision referenced by `uses`. |

Existing callers must remove `package_name` and `wheel_name` from `with`; these inputs are no longer accepted. The template repository/ref and Secret settings remain unchanged.

For example, an artifact containing:

```text
artifact_name: tmp-wheel-123456-1
artifact contents:
  dist/demo_package-1.2.3.dev1-py3-none-any.whl
  nested/other_package-2.0-py3-none-any.whl
  notes.txt
```

uploads both `.whl` files to the same directory and ignores `notes.txt`:

```text
https://download.t-head.cn/artifactory/pypi_tmp/2026-11-02-1211/demo_package-1.2.3.dev1-py3-none-any.whl
https://download.t-head.cn/artifactory/pypi_tmp/2026-11-02-1211/other_package-2.0-py3-none-any.whl
```

The calling job must use `needs` to wait for the job that saves the artifact. The example combines `run_id` and `run_attempt` in the artifact name, then passes the actual name through the build job output to avoid using a new `run_attempt` to find an old artifact when rerunning only the publish job.

## Secrets

| Template Secret | Required | Caller Secret in Example | Description |
| --- | --- | --- | --- |
| `release_username` | Yes | `RELEASE_USERNAME` | Artifactory publishing username. |
| `release_token` | Yes | `RELEASE_TOKEN` | Artifactory publishing credentials with upload access to the destination path. |
| `template_token` | No | `TEMPLATE_TOKEN` | Read-only token for checking out a private template repository. Defaults to `github.token` when not configured; that token may lack read access in cross-repository calls. |

The example explicitly maps credentials through `jobs.publish.secrets` and passes publishing credentials only to the publish job. It does not embed credential values in YAML, build arguments, or output logs.

## Outputs

| Output | Description |
| --- | --- |
| `upload_url` | Batch directory URL with a trailing slash, emitted only after all files upload successfully, available through `${{ needs.publish.outputs.upload_url }}`; `publish` is the ID of the job calling the template. |

For example, `upload_url` is `https://download.t-head.cn/artifactory/pypi_tmp/2026-11-02-1211/`. It is now a directory URL, not a single wheel URL. Individual file URLs are logged after each successful upload.

Filenames are percent-encoded in URLs; for example, `+` becomes `%2B`. No package or version directory is added, and uploaded file contents remain unchanged.

## File Selection

No wheel validation is performed. Every regular file whose name ends with the case-sensitive suffix `.whl` is uploaded, including files in nested or hidden directories. Empty files, arbitrary wheel names, malformed ZIP contents, and invalid or missing metadata are not rejected. The script does not open, extract, install, or execute wheels; the caller is responsible for their contents.

Other suffixes, directories, and symlinks are skipped. Directory symlinks are not followed, so files outside the downloaded artifact are not uploaded. The job fails if the artifact directory is missing or no matching regular files are found.

## Upload and Failure Behavior

- Uploads use HTTPS PUT, with the username and token sent through Basic Auth. Redirects are not followed, to avoid sending files and publishing credentials to other destinations.
- Network or HTTP errors fail the job; only HTTP 2xx is accepted. curl is configured for three retries, but not every error is retried.
- Files are uploaded sequentially in sorted local-path order. On the first upload failure, the job stops and emits no successful `upload_url`; files already uploaded are not rolled back. Empty or malformed publishing credentials also fail the job.
- The template does not check for duplicate filenames or existing server files. Files with the same basename in different artifact subdirectories target the same URL and are all uploaded; later uploads may overwrite earlier ones depending on Artifactory permissions. Use distinct filenames to preserve every file.
- Separate batches whose timestamps are captured in the same UTC minute share the same destination directory. Reruns capture the current timestamp again but may still use the same directory. Minute-level timestamps do not guarantee unique directories or overwrite prevention.

## Local Tests

Run from the repository root in a prepared Python 3.12 environment:

```bash
python -m pip install --no-input pytest PyYAML
PYTHONDONTWRITEBYTECODE=1 python -m pytest ci-workflows/push-tmp-wheel/tests -q -p no:cacheprovider
```

Tests mock uploads, require no real publishing credentials, and do not access a real Artifactory instance. They do not replace permission, network, and build validation in the actual GitHub Actions environment.
