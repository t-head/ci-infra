import itertools
import json
import math
import os
import re
import subprocess

IMAGES_MAP = {
    "ubuntu2404-py312": "download.t-head.cn/docker_tool/ubuntu2404-py312:06",
    "ubuntu2204-py310": "download.t-head.cn/docker_tool/ubuntu2204-py310:05",
    "ubuntu2004-py38" : "download.t-head.cn/docker_tool/ubuntu2004-py38:01",
    "alios7u2-py312"  : "download.t-head.cn/docker_tool/alios7u2-py312:03",
    "alios7u2-py310"  : "download.t-head.cn/docker_tool/alios7u2-py310:08",
    "alios7u2-py38"   : "download.t-head.cn/docker_tool/alios7u2-py38:10",
}

SDK_MAP = {
    "2.1.1": "a5c56e",
    "2.2.0": "663c22",
}


def get_sdk_package_name(sdk_version, runtime_version, os_version):
    hggcrt_version = runtime_version.lower().replace("hggcrt", "").replace("hggc", "")
    return f"ppu_hggcrt{hggcrt_version}_{os_version}-{sdk_version}-{SDK_MAP[sdk_version]}.run"


def get_sdk_url(sdk_version, runtime_version, os_version):
    sdk_version = sdk_version.lower().replace("v", "")
    sdk_package_name = get_sdk_package_name(sdk_version, runtime_version, os_version)
    return f"https://download.t-head.cn/artifactory/ptgai-generic-release/PPU_SDK/v{sdk_version}/{sdk_package_name}"

def get_pccl_url(pccl_version, runtime_version, os_version):
    _pccl_version = pccl_version.lower().replace("v", "")
    _runtime_version = runtime_version.lower().replace("hggcrt", "").replace("hggc", "")
    return f"https://download.t-head.cn/artifactory/ptgai-generic-release/COMM/v{_pccl_version}/pccl_hggcrt{_runtime_version}_{os_version}-v{_pccl_version}.tar.gz"

def fail(message):
    raise SystemExit(f"::error::{message}")


def parse_json(raw, label):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        fail(f"{label} must be valid JSON")


def get_torch_url(
    python_version: str,
    os_version: str,
    runtime_version: str,
    torch_version: str,
    sdk_version: str,
) -> str:
    """Resolve the Torch download URL from the five versions; currently returns an empty string as a placeholder."""
    # TODO: implement the Torch download URL lookup logic.
    return ""


def get_build_tag(tag_version=""):
    """Prefer the specified version; filter rc from all sources and strip the leading v/V uniformly."""
    from packaging.version import InvalidVersion, Version

    version_pattern = r"[vV]?[0-9][A-Za-z0-9.!_]*(?:\+[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*)?"
    if tag_version != "":
        # Validate release-path safety and PEP 440 together; an invalid override never falls back to the repository tag.
        if not isinstance(tag_version, str) or not re.fullmatch(version_pattern, tag_version):
            fail("tag_version must be a valid version number matching the tag format, for example 1.0.0 or 1.0.0+v0.3.0")
        try:
            Version(tag_version)
        except InvalidVersion:
            fail("tag_version must be a valid PEP 440 version, for example 1.0.0 or 1.0.0+v0.3.0")
        if "rc" in tag_version.lower():
            return None
        tag = tag_version
    elif os.environ.get("GITHUB_REF_TYPE") == "tag":
        tag = os.environ.get("GITHUB_REF_NAME", "")
        if "rc" in tag.lower():
            return None
    else:
        source_dir = os.environ.get("SOURCE_DIR")
        if not source_dir:
            fail("branch builds must specify the caller repository directory via SOURCE_DIR")
        try:
            result = subprocess.run(
                ["git", "-C", source_dir, "tag", "--points-at", "HEAD", "--sort=-version:refname"],
                check=True, capture_output=True, text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            fail("unable to read the tags of the caller repository current commit")
        # With multiple tags pick the first qualifying version in descending Git order; never fall back to older commits.
        tag = next((tag for tag in result.stdout.splitlines()
                    if re.fullmatch(version_pattern, tag) and "rc" not in tag.lower()), None)
        if tag is None:
            return None
    if not re.fullmatch(version_pattern, tag):
        fail("tag must be a version number, for example 1.0.0+v0.3.0")
    # Strip only one leading prefix from the whole version; keep inner content such as +v0.3.0.
    return re.sub(r"^[vV]", "", tag)


def main():
    # Read the override first, then apply the release gate based on the finally selected version.
    inputs = parse_json(os.environ["INPUTS_JSON"], "trigger inputs")
    if not isinstance(inputs, dict):
        fail("trigger inputs must be a JSON object")
    nightly_build = os.environ.get("NIGHTLY_BUILD") == "true"
    tag = None if nightly_build else get_build_tag(inputs.get("tag_version", ""))
    should_release = tag is not None
    if not should_release and not nightly_build:
        print("tag contains rc (case-insensitive), or the current commit has no qualifying version tag; only build wheels and save Actions artifacts, no publish")

    package_name = inputs.get("package_name", "")
    if not isinstance(package_name, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", package_name
    ):
        fail("package_name must be a valid Python package name")
    # Unify separators in the wheel distribution name as underscores.
    wheel_package_name = re.sub(r"[-_.]+", "_", package_name)
    pccl_version = inputs.get("pccl_version", "2.2.1")
    if not isinstance(pccl_version, str) or pccl_version not in {"2.1.1", "2.2.0", "2.2.1"}:
        fail("pccl_version must be 2.1.1, 2.2.0 or 2.2.1")
    candidates = {
        "python_versions": {"38", "310", "312"},
        "os_versions": {"ubuntu2004", "ubuntu2204", "ubuntu2404", "alios7u2"},
        "runtime_versions": {"2", "3"},
        "torch_versions": {"", "2.10.0", "2.13.0"},
        "sdk_versions": set(SDK_MAP),
    }
    axes = {}
    for name, allowed in candidates.items():
        values = parse_json(inputs.get(name, ""), name)
        if not isinstance(values, list) or not values:
            fail(f"{name} must be a non-empty JSON array")
        normalized = []
        for value in values:
            # Python versions accept both [38,310] and ["38","310"].
            if name == "python_versions" and type(value) is int:
                value = str(value)
            # An empty Torch version is expressed as [""]; other axes still require non-empty versions.
            empty_torch = name == "torch_versions" and value == ""
            if not empty_torch and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]*", value)):
                fail(f"{name} elements must be non-empty version strings allowing letters, digits and . _ + -; torch_versions may use an empty string")
            if allowed is not None and value not in allowed:
                fail(f"{name} contains unsupported value {value}; allowed values: {', '.join(sorted(allowed))}")
            if value not in normalized:
                normalized.append(value)
        axes[name.removesuffix("s")] = normalized

    count = math.prod(len(values) for values in axes.values())
    if count > 256:
        fail(f"matrix contains {count} jobs, exceeding the GitHub Actions limit of 256 jobs; please narrow the version range")

    for os_version, python_version in itertools.product(axes["os_version"], axes["python_version"]):
        key = f"{os_version}-py{python_version}"
        image = IMAGES_MAP.get(key)
        if not isinstance(image, str) or not image or re.search(r"\s", image):
            fail(f"BUILD_CONTAINER_IMAGES is missing {key} or the image address is empty/contains whitespace")

    # Tags already have the v/V prefix stripped; release keeps the local version, the wheel only takes the part before +.
    wheel_version = tag.split("+", 1)[0] if should_release else ""
    combinations = []
    for values in itertools.product(*axes.values()):
        entry = dict(zip(axes, values))
        key = f"{entry['os_version']}-py{entry['python_version']}"
        entry["image"] = IMAGES_MAP[key]
        entry["sdk_url"] = get_sdk_url(
            sdk_version=entry["sdk_version"],
            runtime_version=entry["runtime_version"],
            os_version=entry["os_version"],
        )
        entry["pccl_url"] = get_pccl_url(
            pccl_version=pccl_version,
            runtime_version=entry["runtime_version"],
            os_version=entry["os_version"],
        )
        # Non-release builds produce no release version or target file name, keeping the original wheel from the build command.
        entry.update(tag="", release_version="", whl_name="")
        if nightly_build:
            entry["release_version"] = inputs["package_version"]
        if should_release:
            # Record the final version (the specified tag_version or the repository tag) for release data aggregation.
            entry["tag"] = tag
            entry["release_version"] = f"{tag}.sail{entry['sdk_version']}"
            if "+" not in tag:
                entry["release_version"] = f"{tag}+sail{entry['sdk_version']}"
            torch_suffix = f"torch{entry['torch_version']}" if entry["torch_version"] else ""
            python_tag = f"cp{entry['python_version']}"
            entry["whl_name"] = (
                f"{wheel_package_name}-{wheel_version}+hggcrt{entry['runtime_version']}"
                f"{torch_suffix}{entry['os_version']}oe-{python_tag}-{python_tag}-linux_x86_64.whl"
            )
        combinations.append(entry)

    matrix = json.dumps({"include": combinations}, separators=(",", ":"))
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"matrix={matrix}\n")
        output.write(f"should_release={str(should_release).lower()}\n")
    print(f"Generated {count} matrix jobs")


if __name__ == "__main__":
    main()
