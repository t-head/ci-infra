"""Validate a wheel from an artifact offline, then upload it unchanged to the fixed temporary repository."""

import csv
from email import policy
from email.parser import Parser
import io
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import quote
from zipfile import BadZipFile

from packaging.tags import parse_tag
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version
from wheel.wheelfile import WheelError, WheelFile


ARTIFACTORY_URL = "https://download.t-head.cn/artifactory/pypi_tmp_generic/"
PACKAGE_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
WHEEL_PATTERN = (
    r"[A-Za-z0-9_][A-Za-z0-9_.]*-[A-Za-z0-9][A-Za-z0-9_.+!]*-"
    r"(?:[0-9][A-Za-z0-9_.]*-)?"
    r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*-"
    r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*-"
    r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*\.whl"
)


def find_wheel(root, wheel_name, package_name):
    if not re.fullmatch(PACKAGE_PATTERN, package_name):
        raise ValueError("package_name must be a valid Python package name")
    if not re.fullmatch(WHEEL_PATTERN, wheel_name):
        raise ValueError("wheel_name must be a valid wheel filename without a path or wildcards")
    name, _, _, _ = parse_wheel_filename(wheel_name)
    if name != canonicalize_name(package_name):
        raise ValueError("The wheel package name does not match package_name")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("artifact directory does not exist or is not a regular directory")

    def scan_error(error):
        raise error

    matches = []
    # Match the exact filename without following directory symlinks or accepting multiple artifacts with the same name.
    for directory, directories, filenames in os.walk(root, onerror=scan_error, followlinks=False):
        if wheel_name in filenames or wheel_name in directories:
            matches.append(Path(directory) / wheel_name)
        directories[:] = [name for name in directories if not (Path(directory) / name).is_symlink()]
    if not matches:
        raise ValueError("artifact does not contain the file specified by wheel_name")
    if len(matches) != 1:
        raise ValueError("artifact contains multiple wheels with the same name; cannot select a file to upload")
    path = matches[0]
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise ValueError("The wheel must be a non-empty regular file, not a symlink")
    return path


def single_header(message, key):
    values = message.get_all(key, [])
    if len(values) != 1 or not str(values[0]).strip():
        raise ValueError(f"Wheel metadata must contain exactly one non-empty {key}")
    return str(values[0])


def read_metadata(archive, path):
    message = Parser(policy=policy.default).parsestr(archive.read(path).decode("utf-8"))
    if message.defects:
        raise ValueError("Wheel metadata is malformed")
    return message


def validate_record(archive, files):
    record_path = archive.record_path
    signatures = {record_path + ".jws", record_path + ".p7s"}
    rows = csv.reader(io.StringIO(archive.read(record_path).decode("utf-8"), newline=""), strict=True)
    recorded = set()
    for row in rows:
        if len(row) != 3:
            raise ValueError("Each RECORD row must contain three fields")
        path, digest, size = row
        if path in recorded or path not in files or path in signatures:
            raise ValueError("RECORD contains duplicate, missing, or disallowed file entries")
        recorded.add(path)
        if path == record_path:
            if digest or size:
                raise ValueError("The hash and size for RECORD itself must be empty")
        elif not digest or not re.fullmatch(r"[0-9]+", size) or int(size) != files[path].file_size:
            raise ValueError("RECORD is missing a hash or has a mismatched file size")
    if recorded != set(files) - signatures:
        raise ValueError("RECORD does not list all required files in the wheel")


def validate_wheel(path):
    name, version, _, filename_tags = parse_wheel_filename(path.name)
    with WheelFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)):
            raise ValueError("The wheel ZIP contains duplicate paths")
        for entry in entries:
            parts = entry.filename.rstrip("/").split("/")
            if (
                entry.orig_filename != entry.filename
                or any(part in {"", ".", ".."} for part in parts)
                or "\\" in entry.filename
                or ":" in parts[0]
                or stat.S_ISLNK(entry.external_attr >> 16)
                or entry.flag_bits & 1
            ):
                raise ValueError("The wheel ZIP contains unsafe paths, symlinks, or encrypted files")
        dist_infos = {entry.split("/", 1)[0] for entry in names if entry.split("/", 1)[0].endswith(".dist-info")}
        if dist_infos != {archive.dist_info_path}:
            raise ValueError("The wheel must contain exactly one .dist-info directory matching its filename")
        files = {entry.filename: entry for entry in entries if not entry.is_dir()}
        required = {f"{archive.dist_info_path}/{name}" for name in ("METADATA", "WHEEL", "RECORD")}
        if not required <= files.keys():
            raise ValueError("The wheel is missing METADATA, WHEEL, or RECORD")

        metadata = read_metadata(archive, f"{archive.dist_info_path}/METADATA")
        metadata_name = single_header(metadata, "Name")
        if not re.fullmatch(PACKAGE_PATTERN, metadata_name) or canonicalize_name(metadata_name) != name:
            raise ValueError("The METADATA package name does not match the wheel filename")
        if Version(single_header(metadata, "Version")) != version:
            raise ValueError("The METADATA version does not match the wheel filename")
        if not re.fullmatch(r"[0-9]+\.[0-9]+", single_header(metadata, "Metadata-Version")):
            raise ValueError("METADATA has an invalid Metadata-Version")

        wheel_metadata = read_metadata(archive, f"{archive.dist_info_path}/WHEEL")
        wheel_version = single_header(wheel_metadata, "Wheel-Version")
        if not re.fullmatch(r"1\.[0-9]+", wheel_version):
            raise ValueError("Unsupported Wheel-Version")
        if int(wheel_version.split(".")[1]) > 0:
            print("::warning::Wheel format version exceeds 1.0; validating against the 1.0 specification", flush=True)
        if single_header(wheel_metadata, "Root-Is-Purelib") not in {"true", "false"}:
            raise ValueError("WHEEL has an invalid Root-Is-Purelib")
        tags = set()
        for value in wheel_metadata.get_all("Tag", []):
            if not re.fullmatch(r"[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+", str(value)):
                raise ValueError("WHEEL has an invalid Tag")
            tags.update(parse_tag(str(value)))
        if tags != filename_tags:
            raise ValueError("WHEEL tags do not match the wheel filename")

        validate_record(archive, files)
        # Read every member in chunks to trigger WheelFile RECORD hash and ZIP CRC validation.
        # Do not install or execute the wheel; allow platform/Python tags that differ from the current runner.
        for entry in entries:
            with archive.open(entry) as member:
                while member.read(1024 * 1024):
                    pass


def upload_wheel(path, package_name, username, token):
    if not username.strip() or not token.strip():
        raise ValueError("Publishing username and token must not be empty")
    if ":" in username or any(char in username + token for char in "\r\n\x00"):
        raise ValueError("Publishing credentials have an invalid format")
    url = ARTIFACTORY_URL + quote(package_name, safe="") + "/" + quote(path.name, safe="")
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
        package_name = os.environ.get("PACKAGE_NAME", "")
        path = find_wheel(Path(artifact_dir), os.environ.get("WHEEL_NAME", ""), package_name)
        validate_wheel(path)
        print(f"Wheel validated: {path.name}", flush=True)
        url = upload_wheel(path, package_name, os.environ.get("RELEASE_USERNAME", ""),
                           os.environ.get("RELEASE_TOKEN", ""))
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write(f"upload_url={url}\n")
        print(f"Uploaded: {url}")
    except subprocess.TimeoutExpired:
        print("::error::Artifactory upload timed out", file=sys.stderr)
        return 1
    except (ValueError, OSError, BadZipFile, WheelError, csv.Error, KeyError, RuntimeError) as error:
        message = str(error).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
