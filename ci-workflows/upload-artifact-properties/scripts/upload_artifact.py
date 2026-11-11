"""Upload the raw ZIP saved by the official download Action to Artifactory and set its Properties.

The destination layout is:

    https://download.t-head.cn/artifactory/github_generic/nightly/{owner}/{repository}/{version}/{filename}

The base URL (including the repository key) is fixed here rather than passed as a
workflow input so callers cannot redirect uploads and property writes to an
unintended host. After each successful PUT, the script issues a second PUT against
`/api/storage/...` with a `properties=` query so every configured field is stored
as an Artifactory item property. The API path precedes the repository key. Values
are escaped for Artifactory and then percent-encoded using the
`{key}={value};{key}={value}` syntax. Property updates require HTTP 204.
"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote


ARTIFACTORY_URL = "https://download.t-head.cn/artifactory"
ARTIFACTORY_REPOSITORY = "github_generic"
ARTIFACTORY_PATH_PREFIX = "nightly"
ARTIFACTORY_BASE_URL = f"{ARTIFACTORY_URL}/{ARTIFACTORY_REPOSITORY}"
PROPERTY_KEYS = (
    "package_type",
    "package_name",
    "package_version",
    "version",
    "os",
    "python_version",
    "runtime_type",
    "runtime",
    "torch_version",
    "sdk_version",
    "package_edition",
    "branch",
    "tag",
    "commit",
    "log_url",
    "run_id",
    "run_attempt",
    "extra_info",
)
# Properties derived from the GitHub Actions context in the wrapper workflow (via env)
# instead of caller inputs, so the values reflect the actual triggering ref/sha/log URL
# and cannot be spoofed by the calling repository.
CONTEXT_PROPERTY_ENV = {
    "branch": "GITHUB_BRANCH",
    "tag": "GITHUB_TAG",
    "commit": "GITHUB_SHA",
    "log_url": "GITHUB_LOG_URL",
    "run_id": "GITHUB_RUN_ID",
    "run_attempt": "GITHUB_RUN_ATTEMPT",
}
# Fields that must be present for the uploaded artifact to be identifiable. Optional
# metadata (torch_version, branch, tag, extra_info) is still sent as an empty property.
REQUIRED_PROPERTY_KEYS = frozenset({
    "package_type",
    "package_name",
    "package_version",
    "version",
    "os",
    "python_version",
    "runtime",
    "sdk_version",
    "package_edition",
    "commit",
    "log_url",
    "run_id",
    "run_attempt",
})

SEGMENT_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,254}"
POSITIVE_INT_PATTERN = r"[1-9][0-9]*"
UPLOAD_TIMEOUT = 600
PROPERTY_TIMEOUT = 120
CURL_HARD_TIMEOUT = 1500


def string_input(inputs, key, default=None):
    value = inputs.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def validate_segment(name, value, pattern):
    if not re.fullmatch(pattern, value):
        raise ValueError(
            f"{name} must be a single URL path segment using letters, digits, dot, underscore, hyphen, or plus"
        )
    return value


def resolve_version(inputs, env):
    version = string_input(inputs, "version", "")
    if not version:
        package_version = validate_segment(
            "package_version", string_input(inputs, "package_version"), SEGMENT_PATTERN,
        )
        sdk_version = validate_segment(
            "sdk_version", string_input(inputs, "sdk_version"), SEGMENT_PATTERN,
        )
        commit = env.get("GITHUB_SHA", "")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{7,}", commit):
            raise ValueError("GITHUB_SHA must contain at least 7 hexadecimal characters to derive version")
        version = f"{package_version}+{commit[:7]}.ppu{sdk_version}"
    return validate_segment("version", version, SEGMENT_PATTERN)


def find_files(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("artifact directory does not exist or is not a regular directory")

    def scan_error(error):
        raise error

    matches = []
    # Select every regular file without inspecting names, contents, or metadata;
    # do not follow symlinks out of the downloaded artifact directory.
    for directory, directories, filenames in os.walk(root, onerror=scan_error, followlinks=False):
        directories[:] = [name for name in directories if not (Path(directory) / name).is_symlink()]
        for name in filenames:
            path = Path(directory) / name
            if not path.is_symlink() and path.is_file():
                matches.append(path)
    if not matches:
        raise ValueError("No regular files found in the artifact")
    return sorted(matches)


def build_property_query(inputs, env):
    pairs = []
    for key in PROPERTY_KEYS:
        if key in CONTEXT_PROPERTY_ENV:
            value = env.get(CONTEXT_PROPERTY_ENV[key], "")
        elif key == "runtime_type":
            value = string_input(inputs, key, "sail")
        else:
            value = inputs.get(key, "")
            if value is None:
                value = ""
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        if key in REQUIRED_PROPERTY_KEYS and not value:
            raise ValueError(f"{key} must not be empty")
        if key in ("run_id", "run_attempt") and not re.fullmatch(POSITIVE_INT_PATTERN, value):
            raise ValueError(f"{CONTEXT_PROPERTY_ENV[key]} must be a positive integer")
        # Escape Artifactory's value separators before URL encoding; percent-encoding
        # alone does not protect them after the server decodes the query values.
        escaped_value = re.sub(r"([\\,|=])", r"\\\1", value)
        pairs.append(f"{quote(key, safe='')}={quote(escaped_value, safe='')}")
    return "properties=" + ";".join(pairs)


def validate_credentials(username, token):
    if not username.strip() or not token.strip():
        raise ValueError("Publishing username and token must not be empty")
    if ":" in username or any(char in username + token for char in "\r\n\x00"):
        raise ValueError("Invalid publishing credential format")


def build_context(inputs, github_repository):
    repository_match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)", github_repository)
    if repository_match is None or repository_match.group(2) in (".", ".."):
        raise ValueError("GITHUB_REPOSITORY must be in owner/repository format without path traversal")
    owner, repository = repository_match.groups()
    version = validate_segment("version", string_input(inputs, "version"), SEGMENT_PATTERN)
    return "/".join(
        quote(part, safe="")
        for part in (ARTIFACTORY_PATH_PREFIX, owner, repository, version)
    )


def _run_curl(extra_args, request_timeout, expected_status=None):
    result = subprocess.run(
        [
            "curl", "--disable", "--fail", "--silent", "--show-error", "--retry", "3",
            "--connect-timeout", "30", "--max-time", str(request_timeout),
            "--proto", "=https", "--output", os.devnull,
            "--write-out", "%{http_code}", *extra_args,
        ],
        capture_output=True,
        text=True,
        timeout=CURL_HARD_TIMEOUT,
        check=False,
    )
    status = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"2[0-9]{2}", status):
        safe_status = status if re.fullmatch(r"[0-9]{3}", status) else "unknown"
        raise ValueError(
            f"Artifactory request failed: curl exit code {result.returncode}, HTTP {safe_status}"
        )
    if expected_status is not None and status != expected_status:
        raise ValueError(f"Artifactory request returned HTTP {status}; expected HTTP {expected_status}")


def upload_file(path, url, username, token):
    _run_curl(
        [
            "--user", f"{username}:{token}",
            "--upload-file", str(path.resolve()),
            "--", url,
        ],
        UPLOAD_TIMEOUT,
    )
    return url


def set_properties(storage_url, query, username, token):
    # PUT an empty body against /api/storage so only the query string carries the payload.
    _run_curl(
        [
            "--user", f"{username}:{token}",
            "--upload-file", os.devnull,
            "--", f"{storage_url}?{query}&recursive=0",
        ],
        PROPERTY_TIMEOUT,
        expected_status="204",
    )


def write_outputs(batch_url, upload_urls):
    output_path = os.environ.get("GITHUB_OUTPUT", "")
    if not output_path:
        return
    body = "\n".join(upload_urls)
    delimiter = "UPLOAD_URLS_EOF"
    while delimiter in body:
        delimiter = f"{delimiter}_MORE"
    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"batch_url={batch_url}\n")
        output.write(f"upload_urls<<{delimiter}\n")
        output.write(body + "\n")
        output.write(f"{delimiter}\n")


def main():
    try:
        artifact_dir = os.environ.get("ARTIFACT_DIR", "")
        if not artifact_dir:
            raise ValueError("ARTIFACT_DIR must not be empty")
        try:
            inputs = json.loads(os.environ.get("INPUTS_JSON", "{}"))
        except json.JSONDecodeError:
            raise ValueError("INPUTS_JSON must be a JSON object") from None
        if not isinstance(inputs, dict):
            raise ValueError("INPUTS_JSON must be a JSON object")
        inputs["version"] = resolve_version(inputs, os.environ)
        encoded_prefix = build_context(inputs, os.environ.get("GITHUB_REPOSITORY", ""))
        property_query = build_property_query(inputs, os.environ)
        username = os.environ.get("RELEASE_USERNAME", "")
        token = os.environ.get("RELEASE_TOKEN", "")
        validate_credentials(username, token)

        paths = find_files(Path(artifact_dir))
        batch_url = f"{ARTIFACTORY_BASE_URL}/{encoded_prefix}/"
        upload_urls = []
        for path in paths:
            file_path = f"{encoded_prefix}/{quote(path.name, safe='')}"
            file_url = f"{ARTIFACTORY_BASE_URL}/{file_path}"
            storage_url = f"{ARTIFACTORY_URL}/api/storage/{ARTIFACTORY_REPOSITORY}/{file_path}"
            upload_file(path, file_url, username, token)
            set_properties(storage_url, property_query, username, token)
            print(f"Uploaded: {file_url}", flush=True)
            upload_urls.append(file_url)
        write_outputs(batch_url, upload_urls)
        print(f"Uploaded {len(upload_urls)} files to: {batch_url}")
    except subprocess.TimeoutExpired:
        print("::error::Artifactory request timed out", file=sys.stderr)
        return 1
    except (ValueError, OSError, RuntimeError) as error:
        message = str(error).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
