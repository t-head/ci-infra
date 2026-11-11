# Upload Artifact With Properties Template

Downloads the raw ZIP of a named artifact from the current GitHub Actions run, uploads the ZIP directly to a versioned Artifactory path, and sets Properties on the ZIP. The whole flow never unpacks, traverses, or repackages the ZIP contents.

Upload destination:

```text
https://download.t-head.cn/artifactory/github_generic/nightly/{owner}/{repository}/{version}/{filename}
```

`owner` and `repository` come from the caller's `${{ github.repository }}`, passed to the script as `GITHUB_REPOSITORY` in `owner/repository` format. The owner is the organization name (or username for a personal repository), not the template repository owner. `run_id` and `run_attempt` come from `${{ github.run_id }}` and `${{ github.run_attempt }}`, are written only as the same-named properties, and no longer participate in the upload path. These values require no caller inputs; the path keeps the fixed `nightly` prefix plus the `version` and `filename` segments. The service root (`ARTIFACTORY_URL`) and repository key (`ARTIFACTORY_REPOSITORY`, fixed to `github_generic`) are defined inside [`scripts/upload_artifact.py`](scripts/upload_artifact.py). Together they form `ARTIFACTORY_BASE_URL`; neither is a workflow input.

The raw ZIP filename comes from the `Content-Disposition` of the download response handled by the official Action and is usually `{artifact_name}.zip`; when no filename is provided, the Action falls back to its default name `artifact`. The upload script keeps the filename saved by the Action and never renames it. After a successful upload, the template sends a PUT with an empty request body for this ZIP artifact to set its Properties:

```text
https://download.t-head.cn/artifactory/api/storage/github_generic/nightly/{owner}/{repository}/{version}/{filename}?properties=key1=value1;key2=value2&recursive=0
```

The `/api/storage/` prefix belongs before the repository key, not inside the repository. The property request must return `204 No Content`; a `201 Created` response is not accepted as a successful property update. See the [Set Item Properties API](https://docs.jfrog.com/artifactory/reference/setitemproperties).

Six properties (`branch`, `tag`, `commit`, `log_url`, `run_id`, `run_attempt`) are derived by the template from GitHub Actions context and injected into the script through environment variables (`GITHUB_BRANCH`, `GITHUB_TAG`, `GITHUB_SHA`, `GITHUB_LOG_URL`, `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`); they are not caller parameters and cannot be overridden through inputs.

- Workflow entry point: [`.github/workflows/upload-artifact-properties.yml`](../../.github/workflows/upload-artifact-properties.yml)
- Upload script: [`scripts/upload_artifact.py`](scripts/upload_artifact.py)
- Complete usage example: [`workflows/demo-upload-artifact-properties.yml`](workflows/demo-upload-artifact-properties.yml)

This is a reusable workflow called through `jobs.<job_id>.uses`, not a regular Action used in `steps`. The template only exposes `workflow_call`. It does not build, validate, repackage, or rename files, and does not create GitHub Releases or package indexes.

## Execution Flow

```text
Caller produces the artifact directory
  → actions/upload-artifact@v4 saves the artifact for the current run
  → caller invokes the template from a job that declares needs on the producing job
  → actions/download-artifact@v8 downloads the current run's artifact by artifact_name
  → skip-decompress: true keeps the raw ZIP without unpacking it
  → HTTPS PUT uploads the ZIP to the versioned Artifactory path
  → HTTPS PUT /api/storage/...?properties=... sets the properties on the ZIP
  → on success, emits batch_url and upload_urls
```

The reusable workflow runs in a separate job and does not share local files with the caller's previous job. Build outputs must be transferred through Actions artifacts. Nested ZIP or tar files inside the artifact are not unpacked.

## Quick Start

1. Copy [`workflows/demo-upload-artifact-properties.yml`](workflows/demo-upload-artifact-properties.yml) to `.github/workflows/demo-upload-artifact-properties.yml` in the project to publish.
2. Configure `RELEASE_USERNAME` and `RELEASE_TOKEN` in the caller repository or organization Actions Secrets. The account needs PUT permission on both the destination path and on the item properties in the target Artifactory repository.
3. Ensure the caller can use the template repository workflows. Private template repositories must also allow cross-repository calls in Actions settings; configure `TEMPLATE_TOKEN` and uncomment the corresponding example line as needed.
4. Replace the example artifact-preparation step with the real build command. The whole Actions artifact ZIP is published as a single file; to publish only part of it, filter the files in the caller before the artifact is saved.
5. Configure the property inputs below as needed; `runtime_type` is optional and defaults to `sail` when not provided; `version` is optional and is generated automatically when omitted or when an empty string is passed. `branch`, `tag`, `commit`, `log_url`, `run_id`, and `run_attempt` come from the GitHub context automatically.
6. After committing the example to the caller default branch, trigger it manually through **Run workflow** on the Actions page and select a trusted branch.

The example actually uploads to Artifactory; it is not a dry run. Files under this directory's `workflows/` are provided for copying and are not automatically registered as Actions workflows in this repository.

### Template Version and Repository

The example uses the `main` branch of `t-head/ci-infra`. Ensure the referenced revision contains this workflow and the helper script before running it.

- For production, replace both `@main` in `uses` and `template_ref: main` with the same full commit SHA from the template repository to prevent workflow and script version drift.
- Do not use the caller's `github.sha` as `template_ref` for cross-repository calls.
- When using a fork, update both the repository path in `uses` and `template_repository`.
- For same-repository relative-path calls, use `uses: ./.github/workflows/upload-artifact-properties.yml` with `template_repository: ${{ github.repository }}` and `template_ref: ${{ github.sha }}`.

The caller must grant `contents: read` to check out the template tools. The download uses the official `actions/download-artifact@v8` with `skip-decompress: true` enabled; downloading an artifact from the current run is authenticated automatically by the Action, so no explicit `github-token` is needed and no extra `actions: read` permission is required for the download. The template no longer depends on the GitHub CLI or a custom download script.

The template runs on `ubuntu-latest` with Python 3.12 and a job timeout of 30 minutes. The official Actions run on Node.js 24, provided by the GitHub-hosted runner; the upload still uses only the Python standard library and curl. The runner must be able to reach the GitHub artifact service, the download storage URLs, and `download.t-head.cn`. The official `download-artifact@v8` does not support GitHub Enterprise Server (GHES). Artifactory credentials are injected only in the upload step, and upload requests still do not follow redirects.

## Inputs

All inputs have type `string`.

### Path and Transport Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `artifact_name` | Yes | None | Name of the artifact saved by the caller using `actions/upload-artifact@v4`; must belong to the current workflow run. |
| `version` | No | `{package_version}+{short_commit}.ppu{sdk_version}` | Used as-is when passed explicitly; generated when omitted or when an empty string is passed. The final value is used for both the upload path and the same-named Properties; `short_commit` is the first 7 characters of the caller's `GITHUB_SHA`. |
| `template_repository` | No | `t-head/ci-infra` | Repository containing the template script, in `owner/repo` format. |
| `template_ref` | Yes | None | Template script version; must match the workflow version referenced by `uses`. |

### Version Selection and Properties

- When a non-empty `version` is passed explicitly, it is used as-is, without appending a commit number or SDK suffix.
- When `version` is omitted or an empty string is passed, `{package_version}+{short_commit}.ppu{sdk_version}` is used. For example, with `package_version=1.2.3`, caller SHA `abcdef1234567890`, and `sdk_version=2.2.0`, the result is `1.2.3+abcdef1.ppu2.2.0`.
- `short_commit` uses the first 7 characters of the caller's `${{ github.sha }}`, never the commit checked out by the template, and a same-named input cannot override it; when generating automatically, the SHA must be a hexadecimal string of at least 7 characters.
- The final `version` is used for the upload URL, the Properties API path, and the `version` property. The `+` in the URL is encoded as `%2B`, while the property keeps the raw version value; the `package_version` property keeps the caller's original value.
- Both explicit and auto-generated versions go through single-segment path validation: it must start with a letter or digit and may only contain letters, digits, dots, underscores, hyphens, and plus signs, with a total length of at most 255 characters. Whitespace-only or invalid explicit versions fail instead of silently falling back; during auto-generation, `package_version` and `sdk_version` must also satisfy the same single-segment validation.

### Property Inputs

Every field below is written as an Artifactory item property with the same name. Pass an empty string when a value does not apply; the property is still set with an empty value except for the required fields listed in the file selection section.

| Input | Required | Description |
| --- | --- | --- |
| `package_type` | Yes | Package type, such as `wheel`, `sdist`, `docker`, or `raw`. |
| `package_name` | Yes | Package name. |
| `package_version` | Yes | Package version, typically matching the released distribution. |
| `os` | Yes | Operating system, such as `ubuntu24.04`. |
| `python_version` | Yes | Python version, such as `3.12`. |
| `runtime_type` | No | Runtime type; defaults to `sail` when not provided, and an explicitly passed value is written as-is to the same-named Properties (for example `rocm` or `cpu`). Does not affect `runtime`, the upload path, or version generation. |
| `runtime` | Yes | Runtime version, such as a HGGCRT version. |
| `torch_version` | Yes | PyTorch version; pass an empty string when Torch is not applicable. |
| `sdk_version` | Yes | SDK version. |
| `package_edition` | Yes | Package edition, such as `standard` or `enterprise`. |
| `extra_info` | No | Free-form extra information, such as a JSON string. |

The query uses `properties=key1=value1;key2=value2`. Each field remains a single property value: literal commas, backslashes, pipes, and equals signs are backslash-escaped according to the Artifactory protocol before URL encoding. For example, `1=1` becomes `1%5C%3D1`, and `a,b` becomes `a%5C%2Cb`. Semicolons and other URL-sensitive characters inside values are percent-encoded; only the separators between property pairs remain literal semicolons.

### Context-derived Properties

The following item properties are populated by the template workflow from GitHub Actions context and are not exposed as inputs:

| Property | Source expression | Notes |
| --- | --- | --- |
| `branch` | `${{ github.ref_type == 'branch' && github.ref_name || '' }}` | Empty on tag triggers. |
| `tag` | `${{ github.ref_type == 'tag' && github.ref_name || '' }}` | Empty on branch triggers. |
| `commit` | `${{ github.sha }}` | Always set to the SHA of the triggering revision. |
| `log_url` | `${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}` | Link to the workflow run page. |
| `run_id` | `${{ github.run_id }}` | Current run ID, must be a positive integer, stored only as a property. |
| `run_attempt` | `${{ github.run_attempt }}` | Attempt number of the current run, must be a positive integer, stored only as a property. |

## Secrets

| Template Secret | Required | Caller Secret in the Example | Description |
| --- | --- | --- | --- |
| `release_username` | Yes | `RELEASE_USERNAME` | Artifactory publishing username. |
| `release_token` | Yes | `RELEASE_TOKEN` | Artifactory publishing credential with PUT permission for both the destination path and the item properties. |
| `template_token` | No | `TEMPLATE_TOKEN` | Read-only token for checking out a private template repository; defaults to `github.token`, which may lack cross-repository read access. |

The example explicitly maps credentials through `jobs.publish.secrets` and passes publishing credentials only to the upload job. Credential values are not written into YAML, environment echoes, or output logs.

## Outputs

| Output | Description |
| --- | --- |
| `batch_url` | Directory URL shared by every uploaded file, ending with a trailing slash; available through `${{ needs.publish.outputs.batch_url }}`. |
| `upload_urls` | Newline-separated list of the uploaded file URLs, in sorted local-path order; available through `${{ needs.publish.outputs.upload_urls }}`. |

Both outputs are emitted only after every file uploads and every property setter succeeds. Individual file URLs are also logged line by line in the upload step.

## File Selection and Properties

The template passes `artifact_name` to the official Action's `name` parameter to download the specified artifact from the current run. Locating, downloading, retrying, and verifying the digest are all handled by the official Action; the v8 default behavior of failing on a digest mismatch is kept. If the download step fails, the subsequent upload never runs. The download directory is managed by the Action, and the upload step reads from the same dedicated directory.

The uploaded object is the raw ZIP saved by the official Action, not the files inside the ZIP. The internal directory structure, filenames, and contents stay inside the raw ZIP, and the ZIP contents are never unpacked or executed on the runner. The template rejects a blank `artifact_name`. All Properties (including `version`, `run_id`, `run_attempt`, and `runtime_type`) are set on the ZIP artifact; `package_type` keeps the value passed by the caller and is not automatically changed to `zip`.

`artifact_name` and every non-optional property must be non-empty; `torch_version` and `extra_info` may be empty. The `version` input may be omitted, but the final version must pass the validation described above; on failure the run stops before any PUT request is sent. Among the context-derived properties, `commit` and `log_url` must be non-empty and `run_id` and `run_attempt` must be positive integer strings, while `branch` and `tag` may be empty depending on the trigger type.

## Upload and Failure Behavior

- Both requests use HTTPS PUT with the username and token sent through Basic Auth. Redirects are not followed to avoid forwarding files or publishing credentials to other destinations.
- Files upload sequentially in sorted local-path order. If any upload or property-set request fails, the job stops and emits neither `batch_url` nor `upload_urls`; files already uploaded are not rolled back.
- Network or HTTP errors fail the job. File uploads require HTTP 2xx; property updates require HTTP 204 specifically, including when another 2xx response is returned. curl is configured for 3 retries, but not every error is retried.
- The same caller repository, `version`, and filename resolve to the same URL across different runs or re-attempts; depending on repository permissions, later uploads may overwrite the existing file and its properties (including `run_id` and `run_attempt`), and separate copies are no longer kept per run number.
- Empty or malformed publishing credentials also fail the job before any request is sent.

## Local Tests

Run from the repository root in a prepared Python 3.12 environment:

```bash
python -m pip install --no-input pytest PyYAML
PYTHONDONTWRITEBYTECODE=1 python -m pytest ci-templates/upload-artifact-properties/tests -q -p no:cacheprovider
```

Local tests check the official Action versions and their `skip-decompress` configuration, the consistency between the download and upload directories, raw ZIP byte preservation, and the Properties contract. Upload and property requests are both mocked, so no real credentials are needed, no official Actions are executed, and no actual Artifactory is contacted; downloading, permissions, and network verification in a real GitHub Actions environment still require an online run.
