# release-pypi（ci-infra）

A Python package build and release tool for SAIL environments. It uses a GitHub Actions reusable workflow to generate a build matrix across Python, operating system, HGGCRT Runtime, PyTorch, and SDK versions, then publishes wheels, bootstrap source distributions, and associated metadata.

This repository provides a **reusable workflow**, referenced through `jobs.<job_id>.uses` in the caller's workflow. It is neither a regular Action used in `steps` nor a directly installable Python package.

## Release Pipeline

入口：[`.github/workflows/matrix-release.yml`](../../.github/workflows/matrix-release.yml)。工具和测试位于 `ci-workflows/release-pypi/`，工作流入口必须保留在仓库根目录的 `.github/workflows/`。

```text
Validate the package name, release version, and image mappings
  → Build bootstrap source distributions (deduplicated by release_version)
  → Matrix build → Collect wheels → Save original wheel artifact → Repack → Rename → Upload wheels
  → Publish source distributions after at least one matrix job succeeds
  → Update the package's index.json
  → Submit release data for the current run
```

非发布路径（`should_release=false`）：跳过源码包构建，仅执行矩阵编译 → 收集 wheel → 保存原始 wheel artifact，然后结束。

- `prepare-matrix`：规范化包名、校验版本和镜像映射；矩阵上限为 GitHub Actions 的 256 个任务。
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
| `data_token` | Yes | 客户端 RSA 私钥，Base64 编码的未加密 DER 格式（PKCS8）|
| `template_token` | No | Read-only token for checking out a private template repository; defaults to `github.token` when omitted |

下方示例将调用方的大写 Secrets 映射到对应的工作流参数。不要将凭据写入 YAML 或构建命令。私有模板仓库还需在 Actions 设置中允许跨仓库调用。

发布数据使用 `/open/artifact/saveSingle`，通过 AES-256-GCM 加密业务数据、RSA-OAEP-SHA256 加密 AES 密钥，并使用 RSA-SHA256 签名。工作流保留 `data_token` Secret 名称，其值为客户端 RSA 私钥；工作流将其映射到 `CLIENT_PRIVATE_KEY_B64` 环境变量。直接运行脚本时也通过该环境变量传入私钥。

`ci-workflows/release-pypi/scripts/post_data.py` 顶部的 `OPEN_API_BASE_URL`、`OPEN_API_CLIENT_ID` 和 `SERVER_PUBLIC_KEY_B64` 已包含迁入配置；同名环境变量不生效。正式发布前需向服务方确认配置与调用方私钥匹配，并通过 `template_ref` 引用确认后的版本。地址仅含协议、主机及可选端口，生产环境使用 HTTPS；公钥为 Base64 编码的 DER 格式（SubjectPublicKeyInfo）。私钥不得写入脚本。

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

正式使用前，需先将本工作流合入 `main`，或引用包含它的提交。`uses` 中的 `@main` 与 `template_ref` 应同时替换为同一个完整 commit SHA：前者决定工作流版本，后者决定工具脚本版本。跨仓库调用不得将调用方的 `github.sha` 作为模板版本。

`template_repository` 为可选参数，默认值为 `t-head/ci-infra`，也可像示例一样显式传入。本模块仅提供 `workflow_call` 入口；手动运行需由调用方提供 `workflow_dispatch`。

## Inputs

Arrays are passed as **JSON strings**, not YAML arrays. All version arrays must be non-empty; duplicate values are removed.

| Input | Required | Current Default | Description |
| --- | --- | --- | --- |
| `package_name` | Yes | None | Python package name; the entry step replaces `-` with `_`, e.g. `demo-package` → `demo_package` |
| `framework` | Yes | None | Framework module name detected by the installation template, such as `torch`; an explicit empty string is allowed; not included in matrix expansion |
| `python_versions` | No | `'["312"]'` | 支持 `"38"`、`"310"`、`"312"`，也接受 JSON 整数，如 `[38,310]` |
| `os_versions` | No | `'["ubuntu2404"]'` | 支持 `ubuntu2004`、`ubuntu2204`、`ubuntu2404`、`alios7u2` |
| `runtime_versions` | No | `'["3"]'` | 支持 `"2"`、`"3"`，元素必须是字符串 |
| `torch_versions` | No | `'["2.10.0"]'` | 支持 `"2.8.0"`、`"2.10.0"`、`"2.13.0"`；`'[""]'` 表示不包含 Torch 版本 |
| `sdk_versions` | No | `'["2.2.0"]'` | 支持 `"2.1.0"`、`"2.2.0"` |
| `template_repository` | No | `t-head/ci-infra` | 保存模板工具脚本的仓库 |
| `template_ref` | Yes | None | Template repository version; must match the version referenced in `uses` |
| `build_command` | Yes | None | Bash command executed at the root of the caller's repository; supports multiple lines; must come from trusted configuration |
| `tag_version` | No | Empty string | Overrides the release version without changing the checked-out source version |

An allowed value does not imply that a corresponding image exists. The matrix expands into the Cartesian product of the five version axes, and **every combination must have an image mapping**. Combinations with missing mappings are not automatically filtered out.

### Current Image Configuration

镜像映射维护在 [`prepare_matrix.py`](scripts/prepare_matrix.py) 的 `IMAGES_MAP` 中，目前仅配置了以下组合：

| OS | Python | Runtime | SDK | Torch |
| --- | --- | --- | --- | --- |
| `ubuntu2404` | `312` | `3` | `2.2.0` | `2.13.0` |
| `ubuntu2404` | `312` | `3` | `2.2.0` | `2.10.0` |

The corresponding images are:

```text
download.t-head.cn/docker_release/pytorch:sdk2.2.0-2.13.0-ubuntu24.04-hggcrt3-py312-20261005
download.t-head.cn/docker_release/pytorch:sdk2.2.0-2.10.0-ubuntu24.04-hggcrt3-py312-20261005
```

省略版本数组时，默认使用 Python 312 / Ubuntu 2404 / Runtime 3 / Torch 2.10.0 / SDK 2.2.0，对应已有镜像。显式传入空值仍会报错。新增组合需按以下键格式更新 `IMAGES_MAP`：

```text
{os_version}-py{python_version}-sdk{sdk_version}-hggcrt{runtime_version}-torch{torch_version}
```

The `BUILD_CONTAINER_IMAGES missing ...` error refers to this mapping table. The current implementation does not load image configuration from an environment variable of the same name. Although `torch_versions: '[""]'` is a valid input, no corresponding image is currently configured.

## Versions and Artifact Naming

The release version is selected in the following order of precedence:

1. A non-empty `tag_version`, which must conform to the script's tag format and PEP 440. Invalid values cause an error without falling back.
2. `github.ref_name` for tag-triggered runs.
3. For other triggers, only tags on the caller's current commit are checked. The first correctly formatted version without `rc` is selected in descending Git version order; historical commits are not searched.

所有版本来源均去除开头的一个 `v` / `V`，保留 `+v0.3.0` 等内部内容。指定版本或触发 tag 含 `rc`（忽略大小写），或当前提交没有合格 tag 时，`should_release=false`：仍校验构建参数和镜像映射，生成矩阵并编译、收集 wheel、保存 Actions artifact；不构建源码包，不执行 Repack、重命名及任何后续发布任务。此时 `RELEASE_VERSION` 和 `WHL_NAME` 为空，wheel 版本与文件名由调用方的构建命令决定。

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

每个矩阵任务在成功收集 wheel 后都会保存原始包到 GitHub Actions artifacts，名称为 `{package_name}-collected-wheels-{run_id}-{run_attempt}-{job-index}`，按包名、运行、重试及矩阵索引隔离。保存发生在 Repack 和重命名之前，保留原始文件名与内容；仅上传 `dist/*.whl`，构建或收集失败时不上传。

发布流程还会保存源码包、发布数据 JSON 和成功标记。仅在 `should_release=true` 且由 tag 触发或显式指定 `tag_version` 时，额外保存重打包后的 `release-wheels` artifact。非发布构建仅保存上述原始 wheel artifact。

**The workflow currently does not create GitHub Releases or upload Release attachments.** The "Save GitHub Release wheel artifacts" step actually uses `actions/upload-artifact`.

## 发布边界与重跑限制

- 源码包 artifact 使用 `{package_name}-sdist-{run_id}-{run_attempt}` 命名，消费方按构建任务输出的 artifact ID 下载，避免完整重跑时的同名冲突。
- 成功标记和发布记录仍严格限定当前 `run_attempt`。**暂不支持只重跑失败或下游任务**：未重跑矩阵的旧记录不会被复用，可能跳过源码包发布或缺少发布数据。需要完整重跑，并先确认 Artifactory 对已发布版本的重复上传策略；工作流不保证外部发布回滚或幂等。不能通过放宽 artifact 通配符规避此限制，否则可能混入旧失败任务的记录。
- 保留原有策略：任一矩阵成功即可发布所有源码包版本；只排除包含 `rc` 的版本，不排除所有预发布版本。是否允许部分发布及 `alpha/beta/dev` 版本由调用方确认。
- 组织门禁未启用；调用方负责可信触发来源、发布审批和同包发布串行化。不要向不可信 PR 的构建代码提供发布凭据。
- 远端策略脚本会执行代码，当前未启用签名验证；索引更新也没有跨工作流运行的并发保护。服务端 API 的业务成功判定及配置仍需联调确认。

## Bootstrap Package Behavior

[`setup.py`](scripts/setup.py) 是源码包模板，不是本仓库的安装入口。工作流填入包名、框架及版本，设置 `__FAKE_BUILD=FAKE_WHEEL` 后生成 Bootstrap 源码包，此时不下载 wheel。

安装前需在目标 Python 环境中预装匹配的 SAIL SDK / Torch，以及 `setuptools`、`wheel`、`packaging`；需使用 `--no-build-isolation` 才能探测宿主框架。`--no-cache-dir` 避免复用其他 Torch / Runtime 环境生成的 Bootstrap wheel。示例：

```bash
python -m pip install setuptools wheel packaging
python -m pip install demo_package==1.0.0+sail2.2.0 --no-build-isolation --no-cache-dir -i https://download.t-head.cn/artifactory/api/pypi/pypi_index/simple
```

安装模板校验下载包的名称、发布版本与当前平台兼容性，并保留内部 `WHEEL` 声明的标签；不会将二进制包改名为 `py3-none-any`。

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
├── matrix-release.yml           # 可复用发布入口
└── release-pypi-ci.yml          # 无凭据的离线回归测试
ci-workflows/release-pypi/
├── scripts/
│   ├── prepare_matrix.py        # 版本门禁、矩阵与镜像映射
│   ├── setup.py                 # Bootstrap 安装模板
│   ├── get_metadata.py          # Artifactory 索引更新
│   └── post_data.py             # 发布数据转换、加密与推送
└── tests/                       # 迁移契约、构建、安装及发布测试
```

## Local Testing

推荐使用与工作流工具任务一致的 **Python 3.12**；CI 同时在 Python 3.14 上测试。需要 Git 和 Bash。

Run the following from the repository root (if `.venv` already exists, check its Python version first):

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --no-input pytest PyYAML packaging setuptools wheel requests cryptography
PYTHONDONTWRITEBYTECODE=1 python -m pytest ci-workflows/release-pypi/tests -q -p no:cacheprovider
```

These tests use mocks / shell shims to simulate the SDK, `revpkg`, `curl`, Twine, and HTTP requests. They do not require real publishing credentials, hardware, or build images, and do not upload artifacts to publishing services. They do not replace build and release validation in real images.
