# Docker Publishing Template

Build a `linux/amd64` image from the caller repository, push it to GHCR, the T-Head registry, or both according to `push_target`, and inspect its size. Defaults are `push_target: github`, `release_type: tmp`, and `publish_data: false`. The metadata publishing script runs only when the target includes `t-head`, `release_type: release`, and `publish_data: true`.

- Reusable workflow: [`.github/workflows/build-and-push-docker.yml`](../../.github/workflows/build-and-push-docker.yml)
- Complete usage example: [`workflows/demo-build-and-push-docker.yml`](workflows/demo-build-and-push-docker.yml)
- Input validation: [`scripts/validate_inputs.py`](scripts/validate_inputs.py)
- Image size inspection: [`scripts/inspect_image.py`](scripts/inspect_image.py)
- Payload assembly and API client: [`scripts/post_data.py`](scripts/post_data.py)

This is a reusable workflow called through `jobs.<job_id>.uses`, not a regular Action used in `steps`.

## Quick Start

1. Commit the Dockerfile, required build files, and an appropriate `.dockerignore` to the caller repository.
2. Copy the [example](workflows/demo-build-and-push-docker.yml) to `.github/workflows/demo-build-and-push-docker.yml` in the caller repository. This module's `workflows/` directory contains examples only; GitHub does not execute them automatically.
3. Grant `contents: read` and `packages: write` permissions in the caller workflow. GHCR uses the caller's `GITHUB_TOKEN` without additional Secrets. `RELEASE_USERNAME` and `RELEASE_TOKEN` are required only when the target includes `t-head`, and the account must have push and read permissions for the selected T-Head repository. `DATA_TOKEN` is required only when `release_type: release` and `publish_data: true` are also set.
4. Update the fixed `os`, `python`, `hggcrt`, `sdk_version`, and `torch` values in the example to match the actual image environment. These values are release metadata only; they do not automatically install dependencies or change the build.
5. Ensure the template revision contains the workflow and all scripts. The example uses `t-head/ci-infra@main`; for production, replace both `@main` in `uses` and `template_ref: main` with the same full commit SHA from the template repository. When using a fork, update both the repository path in `uses` and `template_repository`.
6. Commit the caller workflow to the caller default branch. Select **Run workflow** on the Actions page, choose a trusted branch, and enter the base image name, network type, and other parameters. The image tag is optional; leave it empty to use the current UTC date.
7. After success, find the image address, digest, and size in the logs, job summary, or the example's `result` job.

**The example builds and pushes real images and publishes metadata when the conditions are met; it is not a dry run.** Use only trusted repositories, branches, and Dockerfiles.

Private templates must allow cross-repository calls in Actions settings; enable the `template_token` mapping in the example as needed. For same-repository relative-path calls, use `uses: ./.github/workflows/build-and-push-docker.yml` with `template_repository: ${{ github.repository }}` and `template_ref: ${{ github.sha }}`. For cross-repository calls, do not use the caller `github.sha` as the template revision.

## Execution Flow and Destination

```text
Check out caller source and template tools separately
  → Validate build inputs, release fields, and non-empty credentials
  → Install API dependencies, set up Buildx, and log in to the registry
  → Build and push a linux/amd64 image
  → Inspect the manifest using the pushed digest and calculate size
  → Output image, digest, and size
  → Run pip freeze inside the pushed image and collect installed package names and versions
  → Assemble DOCKER metadata, encrypt and sign it, and call the publishing API
```

All combinations build and push the image and inspect its size. The data key is validated, API dependencies are installed, the package list is collected, and metadata is published only when the target includes `t-head`, `release_type: release`, and `publish_data: true`.

| `push_target` | Image destination | Metadata publishing conditions |
| --- | --- | --- |
| `github` (default) | Only the caller repository's GHCR namespace | Always skipped |
| `t-head` | Only `download.t-head.cn` | `release_type: release` and `publish_data: true` |
| `github/t-head` | GHCR and `download.t-head.cn` | `release_type: release` and `publish_data: true` |

GHCR addresses use the lowercase form of the caller's `github.repository`, not the template repository. The image is linked to the caller repository through the `org.opencontainers.image.source` label:

```text
ghcr.io/<owner>/<repo>/<docker_name>:<docker_tag>-<git_tag_or_commit>
```

The T-Head address is determined by `release_type`: `release` maps to `docker_release`, and `tmp` maps to `docker_tmp`:

```text
download.t-head.cn/<docker_release_or_docker_tmp>/<docker_name>:<docker_tag>-<git_tag_or_commit>
```

Dual-target mode builds once and tags the same build result with both destination addresses. Size inspection and metadata publishing use the T-Head address. A failure to push to either destination fails the job and prevents metadata publishing; images already pushed are not automatically rolled back. GHCR addresses do not distinguish `release_type`, so the same revision and Docker tag use the same address.

The suffix comes from the caller source's checked-out `HEAD`, not the template repository: use a Git tag pointing exactly at that commit, or the full commit SHA when no exact tag exists. Both lightweight and annotated tags are supported. If multiple tags point at the commit, the first in lexicographic name order is used; tags on ancestor commits are ignored. The suffix is appended to the Docker tag after a hyphen and must be a valid lowercase Docker tag suffix; unsupported characters (such as uppercase letters, `/`, or `+`) fail validation rather than being silently rewritten.

`docker_tag` is independent of the Git tag. If omitted or set to an empty string, it defaults to the UTC date at input validation time in `YYYY-MM-DD` format. The resolved suffix and Docker tag are reused for building, inspection, and metadata publishing, even if the build crosses midnight.

Examples with `docker_name: team/demo` (all use the prefix `download.t-head.cn/docker_release/`):

| Checked-out commit | `docker_tag` input | Image name and tag |
| --- | --- | --- |
| Tagged `v1.2.3` | `1.0.0` | `team/demo:1.0.0-v1.2.3` |
| Untagged | `1.0.0` | `team/demo:1.0.0-<full_commit_sha>` |
| Tagged `v1.2.3` | Omitted, on UTC 2026-10-28 | `team/demo:2026-10-28-v1.2.3` |
| Untagged | Omitted, on UTC 2026-10-28 | `team/demo:2026-10-28-<full_commit_sha>` |

Repeated builds of the same revision on the same UTC day target the same image address unless a distinct `docker_tag` is supplied.

The T-Head Artifactory URL is `https://download.t-head.cn/artifactory/docker_release/` or `https://download.t-head.cn/artifactory/docker_tmp/`, while the Docker login address is only `download.t-head.cn`. Image addresses do not include `https://` or `/artifactory/`; the server must configure the corresponding path mappings.

## Inputs

`publish_data` has type `boolean` and accepts only unquoted `true`/`false`. All other inputs have type `string`; quote version numbers. `workflow_call` does not support `choice`, so the script strictly validates both enum inputs. The manually triggered demo uses dropdown options.

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `release_type` | No | `tmp` | Only `release` or `tmp`; selects the T-Head repository path without changing the GHCR address. |
| `publish_data` | No | `false` | Enables metadata publishing only when the target includes `t-head` and the release type is `release`. |
| `push_target` | No | `github` | Only `github`, `t-head`, or `github/t-head`, case-sensitive. |
| `dockerfile` | Yes | None | Dockerfile path relative to the caller repository root, such as `docker/Dockerfile`. |
| `build_context` | No | `.` | Local build context directory relative to the caller repository root. |
| `build_args` | No | Empty string | One `KEY=VALUE` per line, passed as Docker ARG values, not shell or Buildx command-line options. |
| `docker_name` | Yes | None | Lowercase base image name, optionally namespaced, such as `team/demo`; without a registry, `docker_release/` or `docker_tmp/` prefix, or tag. The Git tag or full commit SHA is appended to the image tag automatically. |
| `docker_tag` | No | Current UTC date (`YYYY-MM-DD`) | Omitted or empty uses the date. Otherwise, a base tag such as `1.0.0`; the Git tag or full commit SHA is appended after a hyphen, and the combined tag must stay within 128 characters. `+`, spaces, `/`, and `:` are not supported. |
| `os` | Yes | None | Operating system release metadata, such as `ubuntu24.04`; does not select the runner. |
| `python` | Yes | None | Python version, such as `3.12`. |
| `hggcrt` | Yes | None | HGGCRT version, such as `'3'`, reported as the original string. |
| `sdk_version` | Yes | None | SDK version, such as `2.2.0`. |
| `torch` | Yes | None | PyTorch version, such as `2.13.0`. |
| `network` | Yes | None | Case-sensitive; must be exactly one of `mellanox`, `EIC`, or `mellanox/EIC`. |
| `template_repository` | No | `t-head/ci-infra` | Template repository in `owner/repo` format. |
| `template_ref` | Yes | None | Template script revision; must match the ref referenced by `uses`. |

The six release fields must be non-empty printable strings without leading or trailing whitespace. Apart from the `network` enum, no additional version format restrictions apply. These fields do not automatically form the tag, become Docker ARG values, set image labels, change the runner, or configure the build network.

Both the Dockerfile and build context are relative to the caller repository root, not to each other. The Dockerfile must be a non-empty regular file, and the context must be a directory. Absolute paths, `..`, remote URLs, and symlinks pointing outside the repository are not supported. The full image repository name, including the registry, repository namespace, and `docker_name`, must not exceed 255 characters, while the combined Docker tag, including the Git tag or commit suffix, must not exceed 128 characters.

Variable names in `build_args` must match `[A-Za-z_][A-Za-z0-9_]*` and must not be duplicated; empty values and blank lines are allowed. Do not pass passwords or tokens, as build arguments may appear in image history or provenance. The template does not expose BuildKit secret mount inputs. The Dockerfile must declare and use the corresponding ARG values itself; `docker_tag` does not automatically become a build argument.

## Secrets

| Template Secret | Required | Caller Secret in Example | Description |
| --- | --- | --- | --- |
| `release_username` | When the target includes `t-head` | `RELEASE_USERNAME` | T-Head image registry username. |
| `release_token` | When the target includes `t-head` | `RELEASE_TOKEN` | Token with push and read permissions for the selected T-Head repository. |
| `data_token` | When the target includes `t-head`, `release_type: release`, and `publish_data: true` | `DATA_TOKEN` | Base64-encoded unencrypted DER (PKCS8) client RSA private key, not a Bearer token. |
| `template_token` | No | `TEMPLATE_TOKEN` | Token for reading a private template repository; defaults to `github.token`, which may lack read access in cross-repository calls. |

The workflow declares `contents: read` and `packages: write`; the caller must also grant both permissions. GHCR login uses `github.actor` and the caller's `github.token`. If an existing GHCR package is not linked to the caller repository, grant that repository Actions write access in the package settings. Organization policies must also allow package publishing. Credentials are injected only into the relevant steps and must not be placed in source code, logs, or build arguments.

## Outputs and Size Definition

| Workflow Output | Description |
| --- | --- |
| `image` | Primary image address: the T-Head address when the target includes `t-head`, otherwise the GHCR address. |
| `github_image` | Full GHCR image address; empty when `github` is not selected. |
| `t_head_image` | Full T-Head image address; empty when `t-head` is not selected. |
| `digest` | Original digest returned by Buildx, retaining the `sha256:` prefix; may identify a single image manifest or an image index. |
| `size` | Total size in bytes of the `linux/amd64` image config and stored layers; returned as a decimal string in GitHub Actions outputs. |

The example reads outputs through `${{ needs.publish.outputs.image }}`, `${{ needs.publish.outputs.digest }}`, and `${{ needs.publish.outputs.size }}`. By default, downstream jobs run only after the entire calling job, including metadata publishing, succeeds.

Size inspection uses `repository@digest`, without relying on mutable tags or pulling image layers. If an image index is returned, it selects the unique `linux/amd64` runnable image and ignores other platforms and attestations. `size` includes the config and stored layers (usually compressed), but excludes manifest JSON, other platforms, and provenance/SBOM. It is not the locally unpacked image size, and the manifest JSON size in an index descriptor is not used as a substitute for image size.

## Payload Assembly and Publishing API

`build_payload(record)` is implemented and directly produces the business object below. The API client is self-contained in this template and does not depend on the PyPI template.

All business fields are at the top level; the nested `attributes` object is no longer used. `osTypeList` is a single-element list of strings and `packageList` is a list of objects; all other fields are strings:

| Field | Source or Fixed Value |
| --- | --- |
| `artifactName` | `docker_name`. |
| `artifactType` | Fixed to `docker`. |
| `businessType` | Fixed to `DOCKER`. |
| `idempotentKey` | MD5 of the full `image` string encoded as UTF-8, represented as 32 lowercase hexadecimal characters. |
| `osTypeList` | `[os]`, preserving the original operating system input. |
| `hggcrtVersion` | `hggcrt`, preserving the original version string. |
| `ppuSdk` | `sdk_version`, preserving the original version string. |
| `python` | `python`, preserving the original version string. |
| `image` | `image`, including the full image name and tag. |
| `digest` | The 64-character lowercase hexadecimal digest with the `sha256:` prefix removed. |
| `packageSize` | `str(size)`, the size in bytes as a string. |
| `torch` | `torch`, preserving the original version string. |
| `nicType` | `network`; `mellanox/EIC` remains a single string and is not split. |
| `packageList` | Packages installed in the pushed image, as objects containing `packageName` and `packageVersion`. |

`read_record()` returns 13 keys: `os`, `python`, `hggcrt`, `sdk_version`, `torch`, `network`, `docker_name`, `docker_tag`, `image`, `digest`, `size`, `idempotentKey`, and `packageList`. `size` is an integer and `packageList` is a list of objects; all other values are strings. `docker_name` remains the caller's base name for `artifactName`; `image` includes the Git tag or commit suffix. `docker_tag` contains the resolved explicit tag or UTC date and is included in `image` rather than mapped to a separate property. Build arguments and credentials are excluded from the business object.

Package collection runs `pip freeze --all` inside a `linux/amd64` container addressed by the pushed `repository@digest`, not the mutable tag or the runner's Python environment. `--all` also includes packaging tools such as pip. Lines such as `torch==2.5.8` become `{"packageName": "torch", "packageVersion": "2.5.8"}`; version suffixes such as `+cu124` are preserved. Blank lines, comments, malformed entries, and URL or editable references without a parseable version are skipped individually. Valid entries are deduplicated and sorted by normalized package name; the first version is retained for duplicate names. Parsing never blocks publishing, and no fallback command is run for skipped entries.

Package collection uses the `pip` on the image's configured `PATH`; only that Python environment is inspected, and an unavailable pip produces an empty list. The original image entrypoint is bypassed, so virtual environments must already be configured through the image environment. Containers have networking disabled and receive no host mounts or publishing credentials. The Docker daemon can pull the image using the existing registry login. Each collection command has a 15-minute timeout; failed or timed-out runs attempt container cleanup. Collection failures (including missing pip, Docker errors, timeouts, and output decoding errors) yield `packageList: []` without logging an error or blocking API publishing. Cleanup failures are also ignored. If parsing finds no valid entries, the list is empty; otherwise, successfully parsed entries are retained. Existing release metadata, image, digest, and size validation remains mandatory. `packageList` is independent of the caller-supplied `torch` release metadata.

The `idempotentKey` stays the same for the same image address, even if the digest or size changes; it is recalculated when the image name or tag changes. This rule defines only the client-side key; actual deduplication or overwrite behavior depends on the server. Use distinct tags for different releases.

Publishing uses `POST https://developer.t-head.cn/open/artifact/saveSingle`. The business object is encrypted with AES-256-GCM, the AES key is encrypted with the server RSA public key, and the request is signed with the client private key. Public API configuration is maintained in `post_data.py`; the private key is injected from a Secret. Requests are not automatically retried and do not follow redirects. Non-2xx or HTML responses cause failure. The current client does not validate the business success code in JSON responses; server-side field constraints and the success response format still require live integration validation.

The migrated API endpoint, `sync-artifact-client` client ID, and server RSA public key match the existing publishing client in this repository, without a runtime dependency on the PyPI module. The caller must configure `DATA_TOKEN` to match this client identity; do not assume the original service's private key is valid. Docker business data uses the top-level field mapping shown above. The destination API's field constraints and success response format still require integration validation.

## Failure Behavior and Limitations

- Metadata is not published if the build, push, or size inspection fails. API publishing failures also fail the workflow, but images already pushed are not rolled back.
- The runner is fixed to `ubuntu-latest`, the platform to `linux/amd64`, and the job timeout to 120 minutes. The template does not provide login configuration for other registries.
- Dockerfile RUN instructions execute code; path validation is not a security audit. Build only trusted code and exclude sensitive files with `.dockerignore`.
- Caller source and template tools are checked out separately and do not share local build outputs from other jobs. Required files must be committed or generated by Dockerfile build steps.
- The template does not perform image functional tests, vulnerability scanning, tag overwrite protection, or automatic rollback. Validate network connectivity, registry permissions, and actual build results in the execution environment.

## Local Offline Tests

Run from the repository root in a prepared Python 3.12 environment:

```bash
python -m pip install --no-input pytest PyYAML requests cryptography
PYTHONDONTWRITEBYTECODE=1 python -m pytest ci-workflows/build-and-push-docker/tests -q -p no:cacheprovider
```

Tests cover input validation, field mapping, all three network enum values, encryption and signing, publishing success and failure propagation, manifest size calculation, the workflow, and the usage example. Tests do not connect to real APIs or registries, or build or push images.

This module's [offline test CI](../../.github/workflows/build-and-push-docker-ci.yml) runs a Python 3.12/3.14 test matrix on relevant pull requests and pushes to `main`. Tests validate the release Docker template directly, without installing or checking out the temporary Docker module. They also verify the actual template checkout paths and that public API configuration matches the existing repository configuration.
