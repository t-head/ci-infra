"""Query the registry by the pushed digest and total the linux/amd64 image config and layer sizes."""

import json
import os
import re
import subprocess
import sys

from validate_inputs import REGISTRY, REPOSITORY, image_reference


INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
DIGEST_PATTERN = r"sha256:[0-9a-f]{64}"


def fetch_manifest(repository, digest):
    if not isinstance(digest, str) or not re.fullmatch(DIGEST_PATTERN, digest):
        raise ValueError("Manifest digest must be a valid sha256 digest")
    try:
        # Reuse login-action authentication without pulling image layers or constructing shell commands.
        result = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", "--raw", f"{repository}@{digest}"],
            check=True, capture_output=True, text=True, timeout=120,
        )
    except (subprocess.SubprocessError, OSError) as error:
        # Do not print Docker error output directly; it may contain authentication information.
        raise ValueError(f"Failed to inspect pushed image: {type(error).__name__}") from None
    manifest = json.loads(result.stdout)
    if (not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2
            or not isinstance(manifest.get("mediaType"), str)):
        raise ValueError("Expected a schema version 2 image manifest or index with a media type")
    return manifest


def manifest_size(manifest):
    if manifest.get("mediaType") not in MANIFEST_TYPES:
        raise ValueError("Expected an OCI image manifest or Docker schema 2 manifest")
    config = manifest.get("config")
    layers = manifest.get("layers")
    if not isinstance(config, dict) or not isinstance(layers, list):
        raise ValueError("Image manifest must contain config and layers")
    size = 0
    for descriptor in [config, *layers]:
        if not isinstance(descriptor, dict):
            raise ValueError("Invalid image blob descriptor")
        blob_size = descriptor.get("size")
        if type(blob_size) is not int or blob_size < 0:
            raise ValueError("Image blob size must be a non-negative integer in bytes")
        size += blob_size
    if size <= 0:
        raise ValueError("Image size must be positive")
    return size


def inspect_image_size(image, digest):
    prefix = f"{REGISTRY}/{REPOSITORY}/"
    if not isinstance(image, str) or not image.startswith(prefix):
        raise ValueError("IMAGE must be an image in the fixed docker_release repository")
    name, separator, tag = image[len(prefix):].rpartition(":")
    if not separator or image_reference(name, tag) != image:
        raise ValueError("IMAGE must contain a valid image name and tag")
    repository = image.rsplit(":", 1)[0]
    manifest = fetch_manifest(repository, digest)
    if manifest.get("mediaType") in INDEX_TYPES:
        entries = manifest.get("manifests")
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("Image index must contain a list of manifests")
        candidates = []
        for entry in entries:
            platform = entry.get("platform", {})
            annotations = entry.get("annotations", {})
            if not isinstance(platform, dict) or not isinstance(annotations, dict):
                raise ValueError("Invalid image platform or annotations")
            # Buildx may include provenance/SBOM by default; these are not runnable images and are excluded from the size.
            if (platform.get("os") == "linux" and platform.get("architecture") == "amd64"
                    and annotations.get("vnd.docker.reference.type") != "attestation-manifest"):
                candidates.append(entry)
        if len(candidates) != 1:
            raise ValueError("Image index must contain exactly one linux/amd64 image manifest")
        manifest = fetch_manifest(repository, candidates[0].get("digest"))
    # Do not use descriptor.size from the index; it is the manifest JSON size, not the image size.
    return manifest_size(manifest)


def main():
    try:
        output_path = os.environ.get("GITHUB_OUTPUT", "")
        if not output_path:
            raise ValueError("GITHUB_OUTPUT must not be empty")
        size = inspect_image_size(os.environ.get("IMAGE", ""), os.environ.get("DIGEST", ""))
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"size={size}\n")
        print(f"Pushed image size (config + layers): {size} bytes")
    except (ValueError, OSError) as error:
        message = str(error).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
