"""Upload all artifact .whl files unchanged into one timestamped temporary directory."""

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote


ARTIFACTORY_URL = "https://download.t-head.cn/artifactory/pypi_tmp/"


def find_wheels(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("artifact directory does not exist or is not a regular directory")

    def scan_error(error):
        raise error

    matches = []
    # Select regular files by suffix only; do not inspect wheel names, metadata, or contents.
    # Do not follow symlinks outside the downloaded artifact.
    for directory, directories, filenames in os.walk(root, onerror=scan_error, followlinks=False):
        directories[:] = [name for name in directories if not (Path(directory) / name).is_symlink()]
        for name in filenames:
            path = Path(directory) / name
            if name.endswith(".whl") and not path.is_symlink() and path.is_file():
                matches.append(path)
    if not matches:
        raise ValueError("No .whl files found in the artifact")
    return sorted(matches)


def upload_wheel(path, batch_url, username, token):
    if not username.strip() or not token.strip():
        raise ValueError("Publishing username and token must not be empty")
    if ":" in username or any(char in username + token for char in "\r\n\x00"):
        raise ValueError("Publishing credentials have an invalid format")
    url = batch_url + quote(path.name, safe="")
    # Disable default curl configuration and redirects; do not log commands containing credentials or response bodies.
    result = subprocess.run(
        [
            "curl", "--disable", "--fail", "--silent", "--show-error", "--retry", "3",
            "--connect-timeout", "30", "--max-time", "600", "--proto", "=https",
            "--user", f"{username}:{token}", "--upload-file", str(path.resolve()),
            "--output", os.devnull, "--write-out", "%{http_code}", "--", url,
        ],
        capture_output=True,
        text=True,
        timeout=1500,
        check=False,
    )
    status = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"2[0-9]{2}", status):
        safe_status = status if re.fullmatch(r"[0-9]{3}", status) else "unknown"
        raise ValueError(f"Artifactory upload failed: curl exit code {result.returncode}, HTTP {safe_status}")
    return url


def main():
    try:
        artifact_dir = os.environ.get("ARTIFACT_DIR", "")
        if not artifact_dir:
            raise ValueError("ARTIFACT_DIR must not be empty")
        paths = find_wheels(Path(artifact_dir))
        # Capture UTC once so every file uses the same directory, even across minute boundaries.
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
        batch_url = ARTIFACTORY_URL + timestamp + "/"
        username = os.environ.get("RELEASE_USERNAME", "")
        token = os.environ.get("RELEASE_TOKEN", "")
        for path in paths:
            url = upload_wheel(path, batch_url, username, token)
            print(f"Uploaded: {url}", flush=True)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write(f"upload_url={batch_url}\n")
        print(f"Uploaded {len(paths)} files to: {batch_url}")
    except subprocess.TimeoutExpired:
        print("::error::Artifactory upload timed out", file=sys.stderr)
        return 1
    except (ValueError, OSError, RuntimeError) as error:
        message = str(error).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
