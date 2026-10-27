"""Validate release Docker build inputs and metadata, with the target repository fixed to docker_release."""

import json
import os
from pathlib import Path
import re
import sys


REGISTRY = "download.t-head.cn"
REPOSITORY = "docker_release"
NAME_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
NAME_PATTERN = rf"{NAME_COMPONENT}(?:/{NAME_COMPONENT})*"
TAG_PATTERN = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
RELEASE_FIELDS = ("os", "python", "hggcrt", "sdk_version", "torch", "network")
NETWORK_VALUES = ("mellanox", "EIC", "mellanox/EIC")


def string_input(inputs, key, default=None):
    value = inputs.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def release_metadata(inputs):
    if not isinstance(inputs, dict):
        raise ValueError("INPUTS_JSON must be a JSON object")
    metadata = {}
    for key in RELEASE_FIELDS:
        value = string_input(inputs, key)
        if not value or value != value.strip() or not value.isprintable():
            raise ValueError(f"{key} must be a non-empty printable string without surrounding whitespace")
        metadata[key] = value
    if metadata["network"] not in NETWORK_VALUES:
        raise ValueError("network must be one of: mellanox, EIC, mellanox/EIC (case-sensitive)")
    return metadata


def image_reference(name, tag):
    if not re.fullmatch(NAME_PATTERN, name):
        raise ValueError("docker_name must be a valid lowercase image name, optionally namespaced, without a tag or special characters")
    if name.startswith(("docker_release/", "docker_tmp/")) or (
        "/" in name and ("." in name.split("/", 1)[0] or name.split("/", 1)[0] == "localhost")
    ):
        raise ValueError("docker_name must not include a registry or docker_release/docker_tmp prefix")
    if not re.fullmatch(TAG_PATTERN, tag):
        raise ValueError("docker_tag must be a single valid tag of up to 128 characters; paths, colons, and + are not supported")
    repository = f"{REGISTRY}/{REPOSITORY}/{name}"
    if len(repository) > 255:
        raise ValueError("The image name, including the registry and docker_release prefix, must not exceed 255 characters")
    return f"{repository}:{tag}"


def local_path(source, value, key, *, directory):
    path = Path(value)
    if (
        not value or value != value.strip() or not value.isprintable()
        or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value
    ):
        raise ValueError(f"{key} must be a relative path within the caller repository, without .. or a remote address")
    resolved = (source / path).resolve(strict=True)
    if not resolved.is_relative_to(source):
        raise ValueError(f"{key} must not resolve through symlinks to a location outside the caller repository")
    if not str(resolved).isprintable():
        raise ValueError(f"{key} must not contain control characters in its resolved path")
    if directory:
        if not resolved.is_dir():
            raise ValueError("build_context must be a directory")
    elif not resolved.is_file() or resolved.stat().st_size == 0:
        raise ValueError("dockerfile must be a non-empty regular file")
    return str(resolved)


def validate_build_args(value):
    # Pass arguments only to Buildx, not the shell; require explicit values to avoid implicitly reading runner environment variables.
    normalized = value.replace("\r\n", "\n")
    if any(not char.isprintable() and char not in "\n\t" for char in normalized):
        raise ValueError("build_args contains unsupported control characters")
    keys = set()
    for line in normalized.split("\n"):
        line = line.strip()
        if not line:
            continue
        key, separator, _ = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError("build_args must contain one KEY=VALUE per line, not bare variables or Docker command-line options")
        if key in keys:
            raise ValueError("build_args must not define the same KEY more than once")
        keys.add(key)


def validate_inputs(inputs, source):
    release_metadata(inputs)
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("SOURCE_DIR must be the caller source directory")
    image = image_reference(string_input(inputs, "docker_name"), string_input(inputs, "docker_tag"))
    context = local_path(source, string_input(inputs, "build_context", "."), "build_context", directory=True)
    dockerfile = local_path(source, string_input(inputs, "dockerfile"), "dockerfile", directory=False)
    validate_build_args(string_input(inputs, "build_args", ""))
    # Resolve the Dockerfile relative to the caller repository root; it may be outside the build context.
    return {"image": image, "build_context": context, "dockerfile": dockerfile}


def main():
    try:
        source_dir = os.environ.get("SOURCE_DIR", "")
        if not source_dir:
            raise ValueError("SOURCE_DIR must not be empty")
        outputs = validate_inputs(json.loads(os.environ.get("INPUTS_JSON", "{}")), Path(source_dir))
        output_path = os.environ.get("GITHUB_OUTPUT", "")
        if not output_path:
            raise ValueError("GITHUB_OUTPUT must not be empty")
        with open(output_path, "a", encoding="utf-8") as output:
            for key, value in outputs.items():
                output.write(f"{key}={value}\n")
        # Do not log build arguments, to avoid exposing sensitive values accidentally supplied by the caller.
        print(f"Build input validation passed; target image: {outputs['image']}")
    except FileNotFoundError:
        print("::error::Caller source, Dockerfile, or build context does not exist", file=sys.stderr)
        return 1
    except (ValueError, OSError, RuntimeError) as error:
        message = str(error).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
