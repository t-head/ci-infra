# Release Docker Publishing Template

Build a `linux/amd64` image from the caller repository, push it to `docker_release`, inspect its size, and publish Docker artifact metadata to the data API.

- Reusable workflow: [`.github/workflows/build-and-push-docker.yml`](../../.github/workflows/build-and-push-docker.yml)
- Complete usage example: [`workflows/demo-build-and-push-docker.yml`](workflows/demo-build-and-push-docker.yml)
- Input validation: [`scripts/validate_inputs.py`](scripts/validate_inputs.py)
- Image size inspection: [`scripts/inspect_image.py`](scripts/inspect_image.py)
- Payload assembly and API client: [`scripts/post_data.py`](scripts/post_data.py)

This is a reusable workflow called through `jobs.<job_id>.uses`, not a regular Action used in `steps`.

## Quick Start

1. Commit the Dockerfile, required build files, and an appropriate `.dockerignore` to the caller repository.
2. Copy the [example](workflows/demo-build-and-push-docker.yml) to `.github/workflows/demo-build-and-push-docker.yml` in the caller repository. This module's `workflows/` directory contains examples only; GitHub does not execute them automatically.
3. Configure Actions Secrets: `RELEASE_USERNAME`, `RELEASE_TOKEN`, and `DATA_TOKEN`. The registry account must have push and read access to `docker_release`; the data API private key must match the client identity.
4. Update the fixed `os`, `python`, `hggcrt`, `sdk_version`, and `torch` values in the example to match the actual image environment. These values are release metadata only; they do not automatically install dependencies or change the build.
5. Ensure the template revision contains the workflow and all scripts. The example uses `t-head/ci-infra@main`; for production, replace both `@main` in `uses` and `template_ref: main` with the same full commit SHA from the template repository. When using a fork, update both the repository path in `uses` and `template_repository`.
6. Commit the caller workflow to the caller default branch. Select **Run workflow** on the Actions page, choose a trusted branch, and enter the image name, tag, network type, and other parameters.
7. After success, find the image address, digest, and size in the logs, job summary, or the example's `result` job.

**The example builds and pushes a real image and publishes metadata; it is not a dry run.** Run it only with trusted repositories, branches, and Dockerfiles.

Private templates must allow cross-repository calls in Actions settings; enable the `template_token` mapping in the example as needed. For same-repository relative-path calls, use `uses: ./.github/workflows/build-and-push-docker.yml` with `template_repository: ${{ github.repository }}` and `template_ref: ${{ github.sha }}`. For cross-repository calls, do not use the caller `github.sha` as the template revision.

## Execution Flow and Destination

```text
Check out caller source and template tools separately
  → Validate build inputs, release fields, and non-empty credentials
  → Install API dependencies, set up Buildx, and log in to the registry
  → Build and push a linux/amd64 image
  → Inspect the manifest using the pushed digest and calculate size
  → Output image, digest, and size
  → Assemble DOCKER metadata, encrypt and sign it, and call the publishing API
```

The Docker client address is fixed to:

```text
download.t-head.cn/docker_release/<docker_name>:<docker_tag>
```

For example, `docker_name: team/demo` and `docker_tag: '1.0.0'` produce `download.t-head.cn/docker_release/team/demo:1.0.0`.

The Artifactory repository URL is `https://download.t-head.cn/artifactory/docker_release/`, but Docker login uses only `download.t-head.cn`. Image addresses do not include `https://` or `/artifactory/`; the destination service must configure the corresponding Docker Registry path mapping.

## Inputs

All reusable workflow inputs are of type `string`; quote version numbers.

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `dockerfile` | Yes | None | Dockerfile path relative to the caller repository root, such as `docker/Dockerfile`. |
| `build_context` | No | `.` | Local build context directory relative to the caller repository root. |
| `build_args` | No | Empty string | One `KEY=VALUE` per line, passed as Docker ARG values, not shell or Buildx command-line options. |
| `docker_name` | Yes | None | Lowercase image name, optionally namespaced, such as `team/demo`; excludes the registry, `docker_release/` or `docker_tmp/` prefix, and tag. |
| `docker_tag` | Yes | None | A single tag of up to 128 characters, such as `1.0.0`; `+`, spaces, `/`, and `:` are not supported. |
| `os` | Yes | None | Operating system release metadata, such as `ubuntu24.04`; does not select the runner. |
| `python` | Yes | None | Python version, such as `3.12`. |
| `hggcrt` | Yes | None | HGGCRT version, such as `'3'`, reported as the original string. |
| `sdk_version` | Yes | None | SDK version, such as `2.2.0`. |
| `torch` | Yes | None | PyTorch version, such as `2.13.0`. |
| `network` | Yes | None | Case-sensitive; must be exactly one of `mellanox`, `EIC`, or `mellanox/EIC`. |
| `template_repository` | No | `t-head/ci-infra` | Template repository in `owner/repo` format. |
| `template_ref` | Yes | None | Template script revision; must match the ref referenced by `uses`. |

The six release fields must be non-empty printable strings without leading or trailing whitespace. Apart from the `network` enum, no additional version format restrictions apply. These fields do not automatically form the tag, become Docker ARG values, set image labels, change the runner, or configure the build network.

Both the Dockerfile and build context are relative to the caller repository root, not to each other. The Dockerfile must be a non-empty regular file, and the context must be a directory. Absolute paths, `..`, remote URLs, and symlinks pointing outside the repository are not supported. The full image repository name, including the registry and `docker_release/`, must not exceed 255 characters.

Variable names in `build_args` must match `[A-Za-z_][A-Za-z0-9_]*` and must not be duplicated; empty values and blank lines are allowed. Do not pass passwords or tokens, as build arguments may appear in image history or provenance. The template does not expose BuildKit secret mount inputs. The Dockerfile must declare and use the corresponding ARG values itself; `docker_tag` does not automatically become a build argument.

## Secrets

| Template Secret | Required | Caller Secret in Example | Description |
| --- | --- | --- | --- |
| `release_username` | Yes | `RELEASE_USERNAME` | Docker Registry publishing username. |
| `release_token` | Yes | `RELEASE_TOKEN` | Credentials with push and read access to the release repository. |
| `data_token` | Yes | `DATA_TOKEN` | Client RSA private key encoded as Base64 unencrypted DER (PKCS8), not a regular Bearer token. |
| `template_token` | No | `TEMPLATE_TOKEN` | Token for reading a private template repository; defaults to `github.token`, which may lack read access in cross-repository calls. |

The caller must grant at least `contents: read`. The destination is Artifactory, so GitHub Packages write access is not required. Pass publishing credentials only to the relevant steps; do not include private keys in source code, logs, or build arguments.

## Outputs and Size Definition

| Workflow Output | Description |
| --- | --- |
| `image` | Full image address including the tag. |
| `digest` | Original digest returned by Buildx, retaining the `sha256:` prefix; may identify a single image manifest or an image index. |
| `size` | Total size in bytes of the `linux/amd64` image config and stored layers; returned as a decimal string in GitHub Actions outputs. |

The example reads outputs through `${{ needs.publish.outputs.image }}`, `${{ needs.publish.outputs.digest }}`, and `${{ needs.publish.outputs.size }}`. By default, downstream jobs run only after the entire calling job, including metadata publishing, succeeds.

Size inspection uses `repository@digest`, without relying on mutable tags or pulling image layers. If an image index is returned, it selects the unique `linux/amd64` runnable image and ignores other platforms and attestations. `size` includes the config and stored layers (usually compressed), but excludes manifest JSON, other platforms, and provenance/SBOM. It is not the locally unpacked image size, and the manifest JSON size in an index descriptor is not used as a substitute for image size.

## Payload Assembly and Publishing API

`build_payload(record)` is implemented and directly produces the business object below. The API client is self-contained in this template and does not depend on the PyPI template.

All business fields are at the top level; the nested `attributes` object is no longer used. Only `osTypeList` is a single-element list of strings; all other fields are strings:

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

`read_record()` returns 12 keys: `os`, `python`, `hggcrt`, `sdk_version`, `torch`, `network`, `docker_name`, `docker_tag`, `image`, `digest`, `size`, and `idempotentKey`. `size` is an integer; all other values are strings. `docker_tag` is included in `image` rather than mapped to a separate property; build arguments and credentials are excluded from the business object.

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
