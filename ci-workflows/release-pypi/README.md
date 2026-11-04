# release-pypi (ci-infra)

A Python package build and release tool for SAIL environments. It uses a GitHub Actions reusable workflow to generate a build matrix across Python, operating system, HGGCRT Runtime, PyTorch, and SDK versions, then publishes wheels, bootstrap source distributions, and associated metadata.

This repository provides a **reusable workflow**, referenced through `jobs.<job_id>.uses` in the caller's workflow. It is neither a regular Action used in `steps` nor a directly installable Python package.

## Release Pipeline

Entry point: [`.github/workflows/matrix-release.yml`](../../.github/workflows/matrix-release.yml). Tools and tests are located in `ci-workflows/release-pypi/`; the workflow entry point must remain in the repository root `.github/workflows/`.

```text
Validate the package name, release version, and image mappings
  → Build bootstrap source distributions (deduplicated by release_version)
  → Matrix build → Collect wheels → Save original wheel artifact → Repack → Rename → Upload wheels
  → Publish source distributions after at least one matrix job succeeds
  → Update the package's index.json
  → Submit release data for the current run
```

Non-release path (`should_release=false`): skip sdist build, execute only matrix build → collect wheels → save original wheel artifacts, then finish.

- `prepare-matrix`: Normalize the package name, validate versions and image mappings; the matrix limit is GitHub Actions' 256 jobs.
- `build-sdist`: Generates `.tar.gz` files using this repository's `setup.py` template. These bootstrap packages match and download prebuilt wheels; they are **not archives of the caller's complete source code**.
- `matrix-job`: Builds the caller's repository in the specified image, updates the wheel version with `revpkg`, and uploads it to Artifactory.
- `publish-sdist`: Checks success markers for the current `run_id` / `run_attempt` and publishes all built source distribution versions through Twine only after at least one matrix job succeeds.
- `update-metadata`: Queries Artifactory for the package's wheels and generates an index.
- `post-release-data`: After the index is successfully updated, submits saved release records individually and generates installation commands and idempotency keys.

The matrix uses `fail-fast: false`. Failed jobs do not cancel other matrix jobs. Source distributions and metadata can still be published when the conditions above are met, but this does not mean the entire workflow succeeded, and previously uploaded artifacts are not rolled back.

## Quick Start

### 1. Prepare the Environment and Credentials

The caller's repository requires:

- GitHub Actions enabled, with permission to use this repository's reusable workflow.
- A build command capable of generating wheels in a Linux x86_64 container, along with the required build dependencies.
- Runner access to the container registry and Artifactory at `download.t-head.cn`.
- The required Secrets listed below. The publishing account needs permissions to upload wheels / source distributions, execute AQL queries, and read and write the package index.

| Workflow Secret | Required | Purpose |
| --- | --- | --- |
| `release_username` | Yes | Artifactory publishing username |
| `release_token` | Yes | Artifactory publishing credential |
| `data_token` | Yes | Client RSA private key, Base64-encoded unencrypted DER format (PKCS8) |
| `template_token` | No | Read-only token for checking out a private template repository; defaults to `github.token` when omitted |

The example below maps the caller's uppercase Secrets to the corresponding workflow parameters. Do not embed credentials in YAML or build commands. Private template repositories also require cross-repository invocation to be allowed in Actions settings.

Release data uses `/open/artifact/saveSingle`, encrypting business data with AES-256-GCM, encrypting the AES key with RSA-OAEP-SHA256, and signing with RSA-SHA256. The workflow retains the `data_token` Secret name whose value is the client RSA private key; the workflow maps it to the `CLIENT_PRIVATE_KEY_B64` environment variable. Running the script directly also passes the private key via this environment variable.

`OPEN_API_BASE_URL`, `OPEN_API_CLIENT_ID` and `SERVER_PUBLIC_KEY_B64` at the top of `ci-workflows/release-pypi/scripts/post_data.py` already contain the migrated configuration; environment variables of the same name have no effect. Before the official release, confirm with the service provider that the configuration matches the caller's private key, and reference the confirmed version via `template_ref`. The address contains only the protocol, host and optional port; production uses HTTPS; the public key is in Base64-encoded DER format (SubjectPublicKeyInfo). The private key must never be written into the script.

### 2. Add a Workflow to the Caller's Repository

Create `.github/workflows/release.yml` in the repository of the package to publish, and adjust the package name and build command for your project:

```yaml
name: Publish Python Package

on:
  push:
    tags:
      - 'v*'
      - 'V*'
      - '[0-9]*'
  workflow_dispatch:
    inputs:
      tag_version:
        description: 'Optional release version; leave empty to use a version tag on the current commit'
        required: false
        type: string
        default: ''

permissions:
  contents: read

jobs:
  release:
    uses: t-head/ci-infra/.github/workflows/matrix-release.yml@main
    with:
      package_name: demo-package
      framework: torch
      python_versions: '["312"]'
      os_versions: '["ubuntu2404"]'
      runtime_versions: '["3"]'
      torch_versions: '["2.13.0", "2.10.0"]'
      sdk_versions: '["2.2.0"]'
      template_repository: t-head/ci-infra
      template_ref: main
      tag_version: ${{ inputs.tag_version || '' }}
      build_command: |
        python3 -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
    secrets:
      release_username: ${{ secrets.RELEASE_USERNAME }}
      release_token: ${{ secrets.RELEASE_TOKEN }}
      data_token: ${{ secrets.DATA_TOKEN }}
      # Enable as needed for a private template repository.
      # template_token: ${{ secrets.TEMPLATE_TOKEN }}
```

This example uses the two image combinations currently configured. `pip wheel` is only a generic example; ensure your project's build dependencies are installed, or replace it with your project's own Bash build command.

Before formal use, merge this workflow into `main` or reference the commit containing it. `@main` in `uses` and `template_ref` should both be replaced with the same full commit SHA: the former determines the workflow version, the latter determines the tool script version. Cross-repository invocations must not use the caller's `github.sha` as the template version.

`template_repository` is an optional parameter with the default value `t-head/ci-infra`; it can also be passed explicitly as shown in the example. This module only provides the `workflow_call` entry point; manual runs require the caller to provide a `workflow_dispatch`.

## Inputs

Arrays are passed as **JSON strings**, not YAML arrays. All version arrays must be non-empty; duplicate values are removed.

| Input | Required | Current Default | Description |
| --- | --- | --- | --- |
| `package_name` | Yes | None | Python package name; the entry step replaces `-` with `_`, e.g. `demo-package` → `demo_package` |
| `framework` | Yes | None | Framework module name detected by the installation template, such as `torch`; an explicit empty string is allowed; not included in matrix expansion |
| `python_versions` | No | `'["312"]'` | Supports `"38"`, `"310"`, `"312"`; also accepts JSON integers such as `[38,310]` |
| `os_versions` | No | `'["ubuntu2404"]'` | Supports `ubuntu2004`, `ubuntu2204`, `ubuntu2404`, `alios7u2` |
| `runtime_versions` | No | `'["3"]'` | Supports `"2"`, `"3"`; elements must be strings |
| `torch_versions` | No | `'["2.10.0"]'` | Supports `"2.10.0"`, `"2.13.0"`; `'[""]'` means no Torch version component |
| `sdk_versions` | No | `'["2.2.0"]'` | Supports `"2.1.0"`, `"2.2.0"` |
| `template_repository` | No | `t-head/ci-infra` | Repository containing the template tool scripts |
| `template_ref` | Yes | None | Template repository version; must match the version referenced in `uses` |
| `build_command` | Yes | None | Bash command executed at the root of the caller's repository; supports multiple lines; must come from trusted configuration |
| `tag_version` | No | Empty string | Overrides the release version without changing the checked-out source version |

An allowed value does not imply that a corresponding image exists. The matrix expands into the Cartesian product of the five version axes, and **every combination must have an image mapping**. Combinations with missing mappings are not automatically filtered out.

### Current Image Configuration

Image mappings are maintained in `IMAGES_MAP` in [`prepare_matrix.py`](scripts/prepare_matrix.py); currently only the following combinations are configured:

| OS | Python | Runtime | SDK | Torch |
| --- | --- | --- | --- | --- |
| `ubuntu2404` | `312` | `3` | `2.2.0` | `2.13.0` |
| `ubuntu2404` | `312` | `3` | `2.2.0` | `2.10.0` |

The corresponding images are:

```text
download.t-head.cn/docker_release/pytorch:sdk2.2.0-2.13.0-ubuntu24.04-hggcrt3-py312-20261005
download.t-head.cn/docker_release/pytorch:sdk2.2.0-2.10.0-ubuntu24.04-hggcrt3-py312-20261005
```

When version arrays are omitted, the defaults are Python 312 / Ubuntu 2404 / Runtime 3 / Torch 2.10.0 / SDK 2.2.0, which correspond to a configured image. Explicitly passing empty values still causes an error. Adding new combinations requires updating `IMAGES_MAP` with the following key format:

```text
{os_version}-py{python_version}-sdk{sdk_version}-hggcrt{runtime_version}-torch{torch_version}
```

The `BUILD_CONTAINER_IMAGES missing ...` error refers to this mapping table. The current implementation does not load image configuration from an environment variable of the same name. Although `torch_versions: '[""]'` is a valid input, no corresponding image is currently configured.

## Versions and Artifact Naming

The release version is selected in the following order of precedence:

1. A non-empty `tag_version`, which must conform to the script's tag format and PEP 440. Invalid values cause an error without falling back.
2. `github.ref_name` for tag-triggered runs.
3. For other triggers, only tags on the caller's current commit are checked. The first correctly formatted version without `rc` is selected in descending Git version order; historical commits are not searched.

All version sources strip a single leading `v` / `V`, preserving internal content such as `+v0.3.0`. When the specified version or triggering tag contains `rc` (case-insensitive), or the current commit has no qualifying tag, `should_release=false`: build parameters and image mappings are still validated, the matrix is generated and wheels are built, collected and saved as Actions artifacts; no sdist is built, no Repack, rename or any subsequent publish job runs. In this case `RELEASE_VERSION` and `WHL_NAME` are empty, and the wheel version and file name are determined by the caller's build command.

`tag_version` only determines the release version, artifact paths, and release data; it does not check out source code from the tag with that name. Manual runs build the triggering revision of the selected branch / ref.

For SDK `2.2.0`, for example:

| Input Version | Normalized Tag | `release_version` |
| --- | --- | --- |
| `v1.0.0` | `1.0.0` | `1.0.0+sail2.2.0` |
| `V1.0.0+v0.3.0` | `1.0.0+v0.3.0` | `1.0.0+v0.3.0.sail2.2.0` |

Source distributions and repacked wheels use `release_version` as their version. The final wheel filename uses the portion of the tag before `+` as its base version, combined with environment information, for example:

```text
demo_package-1.0.0+hggcrt3torch2.13.0ubuntu2404oe-cp312-cp312-linux_x86_64.whl
```

The final wheel rename does not modify its internal metadata again. The SDK version and the tag's original local version suffix are reflected in the release directory, rather than directly included in the wheel filename above.

## Build Conventions and Publishing Locations

The build step sources `envsetup.sh` before executing `build_command`. Build images must have a working SDK, compiler toolchain, and the corresponding Python / Torch environment preinstalled.

The build command can access:

```text
PYTHON_VERSION  OS_VERSION  RUNTIME_VERSION  TORCH_VERSION  SDK_VERSION
RELEASE_VERSION  WHL_NAME  CONTAINER_IMAGE
```

The workflow retains `TORCH_URL` and `SDK_URL` environment variable entries, but their lookup logic is not enabled, so no download URLs are currently available. The "Install SDK and Torch" step is also commented out and cannot be relied on to complete the environment automatically.

The caller's code is checked out with its full Git history and recursive submodules. After compilation, the workflow recursively scans the source workspace for regular `.whl` files. If multiple files are found, it filters them by package name prefix; exactly one must remain. The selected artifact is collected into `dist/`, which retains only that wheel. Avoid leaving multiple artifacts for the same package in the build directory.

These addresses are currently hardcoded in the workflow or scripts, rather than exposed as configurable inputs. Ensure upload paths do not conflict across matrix combinations.

Each matrix job saves the original wheel to GitHub Actions artifacts after successfully collecting it, named `{package_name}-collected-wheels-{run_id}-{run_attempt}-{job-index}`, isolated by package name, run, retry and matrix index. Saving occurs before Repack and rename, preserving original file names and content; only `dist/*.whl` is uploaded; nothing is uploaded when building or collecting fails.

The release pipeline also saves sdists, release data JSON and success markers. Only when `should_release=true` and triggered by a tag or with an explicit `tag_version`, the repacked `release-wheels` artifact is additionally saved. Non-release builds only save the original wheel artifacts described above.

**The workflow currently does not create GitHub Releases or upload Release attachments.** The "Save GitHub Release wheel artifacts" step actually uses `actions/upload-artifact`.

## Release Boundaries and Re-run Limitations

- The sdist artifact uses `{package_name}-sdist-{run_id}-{run_attempt}` naming; consumers download by the artifact ID output from the build job, avoiding name conflicts on full re-runs.
- Success markers and release records are still strictly scoped to the current `run_attempt`. **Re-running only failed or downstream jobs is not yet supported**: old records from a matrix that was not re-run will not be reused, potentially skipping sdist publishing or missing release data. A full re-run is required; first confirm Artifactory's duplicate-upload policy for already-published versions. The workflow does not guarantee external publishing rollback or idempotency. This limitation cannot be worked around by loosening artifact wildcards, as that may mix in records from old failed jobs.
- The original strategy is retained: any single matrix success allows publishing all sdist versions; only versions containing `rc` are excluded, not all pre-release versions. Whether partial publishing and `alpha/beta/dev` versions are allowed is determined by the caller.
- Organization gating is not enabled; the caller is responsible for trusted trigger sources, publishing approval and serializing releases of the same package. Do not provide publishing credentials to build code from untrusted PRs.
- The remote strategy script executes code; signature verification is not currently enabled; index updates also lack concurrency protection across workflow runs. Server-side API business success determination and configuration still need integration testing confirmation.

## Bootstrap Package Behavior

[`setup.py`](scripts/setup.py) is the sdist template, not an install entry point for this repository. The workflow fills in the package name, framework and version, sets `__FAKE_BUILD=FAKE_WHEEL` and then builds the bootstrap sdist; no wheel is downloaded at this point.

Before installation, the target Python environment must have a matching SAIL SDK / Torch pre-installed along with `setuptools`, `wheel`, `packaging`; `--no-build-isolation` is required to detect the host framework. `--no-cache-dir` avoids reusing a bootstrap wheel generated for a different Torch / Runtime environment. Example:

```bash
python -m pip install setuptools wheel packaging
python -m pip install demo_package==1.0.0+sail2.2.0 --no-build-isolation --no-cache-dir -i https://download.t-head.cn/artifactory/api/pypi/pypi_index/simple
```

The install template validates the downloaded package's name, release version and compatibility with the current platform, and preserves the tags declared in the internal `WHEEL` metadata; it does not rename the binary wheel to `py3-none-any`.

When a user installs a bootstrap package, the template detects the Python, OS, Runtime, and framework versions. By default, it downloads and executes the remote `strategy_hggc_sail.py` to match a prebuilt wheel, then extracts its dependency information. This process depends on a trusted remote strategy service, an accessible artifact repository, and `wget`; it does not compile the caller's source code on the user's machine.

Relevant environment variables:

| Variable | Description |
| --- | --- |
| `HGGCRT_VERSION` | Explicit Runtime version, such as `hggcrt3`; if unset or incorrectly formatted, detection falls back to `hgcc --version` |
| `SAIL_SDK_MATCH_STRATEGY` | Set to `off` to construct the wheel URL directly using local naming rules |
| `SAIL_PYPI_SDK_VERSION` | SDK version lookup entry provided by the template; `VESION` is the existing spelling, and its use depends on the matching strategy |

## Repository Structure

```text
.github/workflows/
├── matrix-release.yml           # Reusable release entry point
└── release-pypi-ci.yml          # Credential-free offline regression tests
ci-workflows/release-pypi/
├── scripts/
│   ├── prepare_matrix.py        # Version gating, matrix and image mappings
│   ├── setup.py                 # Bootstrap install template
│   ├── get_metadata.py          # Artifactory index update
│   └── post_data.py             # Release data transformation, encryption and push
└── tests/                       # Migration contract, build, install and publish tests
```

## Local Testing

Using **Python 3.12** consistent with the workflow tool jobs is recommended; CI also tests on Python 3.14. Git and Bash are required.

Run the following from the repository root (if `.venv` already exists, check its Python version first):

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --no-input pytest PyYAML packaging setuptools wheel requests cryptography
PYTHONDONTWRITEBYTECODE=1 python -m pytest ci-workflows/release-pypi/tests -q -p no:cacheprovider
```

These tests use mocks / shell shims to simulate the SDK, `revpkg`, `curl`, Twine, and HTTP requests. They do not require real publishing credentials, hardware, or build images, and do not upload artifacts to publishing services. They do not replace build and release validation in real images.
