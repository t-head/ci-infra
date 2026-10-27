# Temporary Docker Image Build and Push Template

Read a Dockerfile from the caller repository, pass build arguments, build an image with the specified name and tag, and push it to the Artifactory `docker_tmp` repository.

- Workflow entry point: [`.github/workflows/build-and-push-tmp-docker.yml`](../../.github/workflows/build-and-push-tmp-docker.yml)
- Input validation script: [`scripts/validate_inputs.py`](scripts/validate_inputs.py)
- Complete usage example: [`workflows/demo-build-and-push-tmp-docker.yml`](workflows/demo-build-and-push-tmp-docker.yml)

This is a reusable workflow called through `jobs.<job_id>.uses`, not a regular Action used in `steps`. The template exposes only `workflow_call`; the example provides a manual trigger through `workflow_dispatch`.

## Execution Flow and Destination

```text
Check out caller source and template tools into separate directories
  → Validate image name, tag, Dockerfile, build context, and build arguments
  → Validate publishing credentials
  → Set up Docker Buildx
  → Log in to download.t-head.cn
  → Build and push a linux/amd64 image
  → Output image and digest
  → login-action logs out during job cleanup
```

The Artifactory repository URL is:

```text
https://download.t-head.cn/artifactory/docker_tmp/
```

The template follows the path mapping used by other Docker images in this repository. Docker clients use this full image reference:

```text
download.t-head.cn/docker_tmp/{docker_name}:{docker_tag}
```

For example, `docker_name: team/demo` and `docker_tag: dev-20261026` produce:

```text
download.t-head.cn/docker_tmp/team/demo:dev-20261026
```

Docker image references do not include `https://` or `/artifactory/`. Use only the domain `download.t-head.cn` when logging in. The destination service must have the corresponding Docker Registry path mapping configured.

## Quick Start

1. Prepare and commit the Dockerfile, application source, and an appropriate `.dockerignore` in the caller repository.
2. Copy [`workflows/demo-build-and-push-tmp-docker.yml`](workflows/demo-build-and-push-tmp-docker.yml) to `.github/workflows/demo-build-and-push-tmp-docker.yml` in the caller repository.
3. Configure `RELEASE_USERNAME` and `RELEASE_TOKEN` in the caller repository or organization Actions Secrets. The account needs push access to the destination repository and, when using private base images from the same registry, the corresponding pull access.
4. Ensure the caller can use the template workflow. Private template repositories must allow cross-repository calls; configure `TEMPLATE_TOKEN` and uncomment its mapping in the example as needed.
5. Select a template version as described below. After committing the example to the caller default branch, select **Run workflow** on the Actions page, choose a trusted build branch, and enter:

   ```text
   dockerfile: docker/Dockerfile
   build_context: .
   build_args: APP_VERSION=1.2.3.dev1
   docker_name: team/demo
   docker_tag: dev-20261026
   ```

6. After success, find the image reference and digest in the logs or job summary of the template job or the example `result` job.

The example builds and pushes a real image; it is not a dry run. Files under this module's `workflows/` directory are not automatically registered as Actions workflows and must be copied to the caller `.github/workflows/` directory.

The template checks out the caller source at the triggering revision. It does not share working directories with other jobs or download their artifacts. No separate checkout, build, or login job is needed; the template performs these operations. Generate any required build files in Dockerfile build steps instead of relying on local files left by a previous job.

### Template Version

- The example uses the `main` branch of `t-head/ci-infra`. Ensure that revision contains this workflow and validation script before running it.
- For production, replace both `@main` in `uses` and `template_ref: main` with the same full commit SHA from the template repository.
- Do not use the caller `github.sha` as the template version for cross-repository calls. When using a fork, update both the repository path in `uses` and `template_repository`.
- For same-repository relative-path calls, use `uses: ./.github/workflows/build-and-push-tmp-docker.yml` with `template_repository: ${{ github.repository }}` and `template_ref: ${{ github.sha }}`.

## Inputs

All inputs are of type `string`.

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `dockerfile` | Yes | None | Dockerfile path relative to the caller repository root, e.g. `docker/Dockerfile`. The example manual trigger defaults to `Dockerfile`; the template itself has no default. |
| `build_context` | No | `.` | Local build context directory relative to the caller repository root. |
| `build_args` | No | Empty string | One `KEY=VALUE` per line, passed as Docker `ARG` values, not arbitrary Docker command-line options. |
| `docker_name` | Yes | None | Lowercase image name with an optional namespace, e.g. `demo` or `team/demo`; exclude the registry, `docker_tmp/` prefix, and tag. |
| `docker_tag` | Yes | None | Single image tag, e.g. `dev-20261026` or `1.2.3.dev1`, with at most 128 characters. |
| `template_repository` | No | `t-head/ci-infra` | Repository containing the workflow and validation script, in `owner/repo` format. |
| `template_ref` | Yes | None | Validation script revision; must match the workflow revision referenced by `uses`. |

### Dockerfile and Build Context

Both paths are relative to the caller repository root, not to each other. For example:

```text
caller-repository/
├── docker/Dockerfile
└── app/
    ├── .dockerignore
    └── main.py
```

With `dockerfile: docker/Dockerfile` and `build_context: app`, `COPY main.py /app/` in the Dockerfile reads `app/main.py`. The Dockerfile may be outside the build context but must remain within the caller repository.

Both paths must exist. The Dockerfile must be a non-empty regular file, and the build context must be a directory. Absolute paths, paths containing `..`, remote URLs, and symlinks pointing outside the repository are not supported. Internal symlinks resolve to actual paths within the repository. Buildx checks Dockerfile syntax and whether the build succeeds.

### Build Arguments

By default, the example forwards manually supplied `build_args`. For fixed multiline arguments, replace `jobs.publish.with.build_args` in the example with:

```yaml
build_args: |
  PYTHON_VERSION=3.12
  APP_VERSION=1.2.3.dev1
```

Declare and use the corresponding `ARG` values in the caller Dockerfile, for example:

```dockerfile
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim
ARG APP_VERSION=dev
LABEL org.opencontainers.image.version="${APP_VERSION}"
WORKDIR /app
COPY . .
```

Argument names must match `[A-Za-z_][A-Za-z0-9_]*` and must not be defined more than once. Empty values (`KEY=`) and blank lines are supported; bare variable names, comment lines, and command-line options such as `--target=...` are not. `APP_VERSION` is passed only as a build argument; `docker_tag` independently determines the image tag.

Do not pass passwords, tokens, or other secrets through `build_args`. Build arguments may appear in image history, build records, or provenance. The template does not currently expose BuildKit secret mount inputs.

### Image Names and Tags

- Image names use lowercase letters, digits, and valid separators, with `/`-separated namespaces supported. The full repository name, including the registry and `docker_tmp/` prefix, must not exceed 255 characters.
- Tags must match `[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}`. Uppercase letters are allowed; spaces, `/`, `:`, `+`, and multiple tags are not supported.
- Docker tags do not use Python PEP 440 validation: `1.2.3.dev1` is valid, but the Python version `1.2.3.dev1+gabc123` cannot be used directly as a Docker tag. You may choose `1.2.3.dev1-gabc123` instead; the template does not convert it automatically.

## Secrets

| Template Secret | Required | Caller Secret in Example | Description |
| --- | --- | --- | --- |
| `release_username` | Yes | `RELEASE_USERNAME` | Docker Registry publishing username. |
| `release_token` | Yes | `RELEASE_TOKEN` | Publishing token with push access to the destination repository. |
| `template_token` | No | `TEMPLATE_TOKEN` | Read-only token for checking out a private template repository. Defaults to `github.token`, which may lack read access to the template repository in cross-repository calls. |

The example explicitly maps these credentials through `jobs.publish.secrets`. The caller must grant at least `contents: read`. GitHub Packages write access is unnecessary because the destination is Artifactory.

## Outputs

| Output | Description |
| --- | --- |
| `image` | Full image reference including the tag, available to downstream jobs through `${{ needs.publish.outputs.image }}`. |
| `digest` | Digest of the successfully pushed image, e.g. `sha256:...`, available through `${{ needs.publish.outputs.digest }}`. |

Here, `publish` is the ID of the job that calls the template in the example. Downstream jobs with `needs: publish` run only after successful publishing by default. The example `result` job prints both outputs.

## Limitations and Security Considerations

- The build runner is fixed to `ubuntu-latest`, the platform to `linux/amd64`, and the job timeout to 120 minutes. Inputs cannot select another runner or platform or pass arbitrary Buildx options.
- The runner must be able to reach GitHub, base image repositories, build dependency endpoints, and the destination registry. The template logs in only to `download.t-head.cn` and provides no login configuration for other private registries.
- Dockerfile `RUN` instructions execute code. Run only with trusted repositories, branches, and Dockerfiles. Input path validation is not a security audit of the build contents.
- Template tools and source code are checked out into separate directories, and checkout credentials are not persisted. The caller must still maintain `.dockerignore` to prevent sensitive or unrelated files in the source directory from being copied into the image.
- Validation, login, build, or push failures fail the job; they are not ignored or reported as success. The template does not run image functionality tests or vulnerability scans.
- The template does not check whether a tag already exists or guarantee overwrite protection. Pushing the same name and tag depends on Artifactory overwrite permissions; use a distinct tag for each build. The template provides neither rollback nor expired-image cleanup.

## Local Tests

Run from the repository root in a prepared Python 3.12 environment:

```bash
python -m pip install --no-input pytest PyYAML
PYTHONDONTWRITEBYTECODE=1 python -m pytest ci-workflows/build-and-push-tmp-docker/tests -q -p no:cacheprovider
```

Tests cover input validation rules, workflow configuration, and usage examples. They do not require a Docker daemon or real credentials and do not build, log in, or push. Verify actual network connectivity, permissions, and Dockerfile builds in GitHub Actions.

The module's [offline test CI](../../.github/workflows/build-and-push-tmp-docker-ci.yml) runs a Python 3.12/3.14 test matrix for relevant pull requests and pushes to `main`, without publishing images.
