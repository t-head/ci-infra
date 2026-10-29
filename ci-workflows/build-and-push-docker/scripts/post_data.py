"""Standalone Docker release data client for assembling, encrypting, signing, and publishing release metadata."""

import base64
import hashlib
import json
import os
import re
import requests
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from validate_inputs import REGISTRY, REPOSITORY, image_reference, release_metadata, string_input, versioned_image_reference


REQUEST_TIMEOUT = (30, 300)
OPEN_API_BASE_URL = 'https://developer.t-head.cn'
PATH_SAVE = '/open/artifact/saveSingle'
OPEN_API_CLIENT_ID = 'sync-artifact-client'
SERVER_PUBLIC_KEY_B64 = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA34jHUC8n1EOOpc71hBu8vImFIv0qiuvCJAH2HPTW2GvFXGH//o8oRBfy0fWn24+Bk7GYvrS8BhPV7H8wwqBqba1/+b40leFFKDX1NYm+6cq4J7Rt+LzOSse5PghAZGiiGf+ImHoaActV7A9diQMdIBWZRqtHm4VEG/KJejq7fvAKm8geXoUoLzPQfFrpcj4bLcFmfUpFBmLARUHUUC9rlAjGDnQQcVWujveU0sBcTSTIRNwzkuDRkSRKbB0vnNIClt7tqB84FypWGwH2otF4WqeEC56pLX2esLzNh736tbR5rnXwGKjzUG3qx/CICwu9+GAFiIQZZH4jYce3E9CRUQIDAQAB'


@dataclass
class OpenApiConfig:
    base_url: str
    client_id: str
    server_public_key_b64: str = field(repr=False)
    client_private_key_b64: str = field(repr=False)
    timeout: tuple[int, int] = REQUEST_TIMEOUT

    @classmethod
    def from_env(cls):
        """Use script constants for public configuration; read only the client private key from the environment."""
        return cls(
            base_url=OPEN_API_BASE_URL,
            client_id=OPEN_API_CLIENT_ID,
            server_public_key_b64=SERVER_PUBLIC_KEY_B64,
            client_private_key_b64=os.environ.get("CLIENT_PRIVATE_KEY_B64", ""),
        )

    def validate(self):
        for name, value in (
            ("OPEN_API_BASE_URL", self.base_url),
            ("OPEN_API_CLIENT_ID", self.client_id),
            ("SERVER_PUBLIC_KEY_B64", self.server_public_key_b64),
            ("CLIENT_PRIVATE_KEY_B64", self.client_private_key_b64),
        ):
            if (
                not isinstance(value, str) or not value or value != value.strip()
                or any(character.isspace() for character in value) or value.startswith("<")
            ):
                raise ValueError(f"{name} must not be empty, contain whitespace, or use a placeholder value")
        if not self.client_id.isascii():
            raise ValueError("OPEN_API_CLIENT_ID must contain only ASCII characters")
        try:
            parts = urlsplit(self.base_url)
            valid_url = (
                parts.scheme in {"http", "https"} and parts.hostname
                and parts.username is None and parts.password is None
                and parts.path in {"", "/"} and not parts.query and not parts.fragment
            )
            parts.port
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("OPEN_API_BASE_URL must be an HTTP(S) URL containing only a scheme, host, and optional port")


class OpenApiClient:
    def __init__(self, config: OpenApiConfig):
        self.config = config
        self.config.validate()

        try:
            self._server_pub_key = serialization.load_der_public_key(
                base64.b64decode(self.config.server_public_key_b64, validate=True)
            )
            self._client_priv_key = serialization.load_der_private_key(
                base64.b64decode(self.config.client_private_key_b64, validate=True),
                password=None,
            )
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise ValueError("Failed to load keys; provide a Base64-encoded DER public key and an unencrypted DER private key") from None
        if not isinstance(self._server_pub_key, rsa.RSAPublicKey) or not isinstance(
            self._client_priv_key, rsa.RSAPrivateKey
        ):
            raise ValueError("The server public key and client private key must be RSA keys")

    def _build_url(self, path: str) -> str:
        return self.config.base_url.rstrip("/") + path

    def _encrypt_payload(self, plain_bytes: bytes) -> dict[str, str]:
        aes_key = os.urandom(32)
        iv = os.urandom(12)

        cipher_blob = AESGCM(aes_key).encrypt(iv, plain_bytes, None)

        enc_key = self._server_pub_key.encrypt(
            aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

        return {
            "encKey": base64.b64encode(enc_key).decode('utf-8'),
            "iv": base64.b64encode(iv).decode('utf-8'),
            "cipher": base64.b64encode(cipher_blob).decode('utf-8'),
        }

    def _sign_request(self, body_bytes: bytes, timestamp: str) -> str:
        digest_hex = hashlib.sha256(body_bytes).hexdigest()
        string_to_sign = (
            f"{self.config.client_id}\nPOST\n{PATH_SAVE}\n{timestamp}\n{digest_hex}"
        ).encode("utf-8")

        signature = self._client_priv_key.sign(
            string_to_sign,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def save_artifact(self, business_data: dict[str, Any]) -> requests.Response:
        """Encrypt business data and sign the actual request body; do not automatically retry failures."""
        plain_bytes = json.dumps(
            business_data, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        envelope = self._encrypt_payload(plain_bytes)
        body_bytes = json.dumps(envelope, separators=(",", ":")).encode("utf-8")

        timestamp_ms = str(int(time.time() * 1000))
        signature = self._sign_request(body_bytes, timestamp_ms)

        headers = {
            "Content-Type": "application/json",
            "X-Open-Client": self.config.client_id,
            "X-Open-Timestamp": timestamp_ms,
            "X-Open-Sign": signature,
        }
        url = self._build_url(PATH_SAVE)
        try:
            response = requests.post(
                url,
                data=body_bytes,
                headers=headers,
                timeout=self.config.timeout,
                # The signature binds POST and the fixed path; prevent redirects from changing the request or forwarding signature headers.
                allow_redirects=False,
            )
        except requests.RequestException as error:
            # Request exceptions may contain authentication information; retain only the exception type.
            raise ValueError(f"Release data request failed: {type(error).__name__}") from None
        print(f"Release data API response: HTTP {response.status_code}", flush=True)
        check_response(response)
        return response


def check_response(response):
    # requests.raise_for_status does not reject 3xx; also prevent redirects from being treated as success.
    if not 200 <= response.status_code < 300:
        raise ValueError(f"Release data API request failed, HTTP {response.status_code}")
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type in {"text/html", "application/xhtml+xml"}:
        raise ValueError("The API returned HTML, possibly a login page; publishing success cannot be confirmed")


def parse_pip_freeze(output):
    if not isinstance(output, str):
        return []
    packages = {}
    name_pattern = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(rf"({name_pattern})\s*={{2,3}}\s*([A-Za-z0-9][A-Za-z0-9.!+_-]*)", line)
        if not match:
            continue
        name, version = match.groups()
        key = re.sub(r"[-_.]+", "-", name).lower()
        packages.setdefault(key, {"packageName": name, "packageVersion": version})
    return [packages[key] for key in sorted(packages)]


def run_image_pip(reference, *arguments):
    container = f"release-packages-{uuid.uuid4().hex}"
    try:
        result = subprocess.run(
            ["docker", "run", "--rm", "--name", container, "--platform", "linux/amd64",
             "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
             "--entrypoint", "pip", reference, "--disable-pip-version-check", *arguments],
            check=True, capture_output=True, text=True, timeout=900,
        )
    except Exception:
        try:
            subprocess.run(
                ["docker", "rm", "--force", container],
                check=False, capture_output=True, text=True, timeout=30,
            )
        except Exception:
            pass
        return ""
    return result.stdout


def collect_image_packages(image, digest):
    try:
        prefix = f"{REGISTRY}/{REPOSITORY}/"
        if not isinstance(image, str) or not image.startswith(prefix):
            return []
        name, separator, tag = image[len(prefix):].rpartition(":")
        if not separator or image_reference(name, tag) != image:
            return []
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            return []
        reference = f"{image.rsplit(':', 1)[0]}@{digest}"
        frozen = run_image_pip(reference, "freeze", "--all")
        return parse_pip_freeze(frozen)
    except Exception:
        # 包清单仅作补充信息，采集或解析异常不得阻断发布。
        return []


def read_record():
    """Collect only release fields and pushed image details, excluding build arguments and credentials from payload assembly.

    返回包含以下 13 个字段的字典：
        os (str): Operating system, preserving the original input.
        python (str): Python version, preserving the original input.
        hggcrt (str): HGGCRT version, preserving the original input.
        sdk_version (str): SDK version, preserving the original input.
        torch (str): PyTorch version, preserving the original input.
        network (str): mellanox, EIC, or mellanox/EIC, case-sensitive.
        docker_name (str): Image name, optionally namespaced, without the registry address or tag.
        docker_tag (str): Image tag.
        image (str): Full image address in the format
            download.t-head.cn/docker_release/<docker_name>-<git_tag_or_commit>:<docker_tag>.
        digest (str): Pushed digest as 64 lowercase hexadecimal characters, without the sha256: prefix.
        size (int): Total size in bytes of the linux/amd64 image config and stored layers,
            excluding manifest JSON and attestations; not the locally unpacked size.
        idempotentKey (str): MD5 of the full image string encoded as UTF-8,
            represented as 32 lowercase hexadecimal characters, unaffected by digest or size changes.
        packageList (list[dict[str, str]]): 镜像中安装的包，包含 packageName 和 packageVersion。
    """
    inputs = json.loads(os.environ.get("INPUTS_JSON", "{}"))
    record = release_metadata(inputs)
    name = string_input(inputs, "docker_name")
    input_tag = string_input(inputs, "docker_tag", "")
    tag = os.environ.get("RESOLVED_DOCKER_TAG", "")
    if input_tag and tag != input_tag:
        raise ValueError("RESOLVED_DOCKER_TAG must match the supplied docker_tag")
    revision = os.environ.get("SOURCE_REVISION", "")
    expected_image = versioned_image_reference(name, revision, tag)
    image = os.environ.get("IMAGE", "")
    if image != expected_image:
        raise ValueError("IMAGE must match docker_name, SOURCE_REVISION, and RESOLVED_DOCKER_TAG in docker_release")
    digest = os.environ.get("DIGEST", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("DIGEST must be the sha256 digest returned by the successful image push")
    size = os.environ.get("SIZE", "")
    if not re.fullmatch(r"[1-9][0-9]*", size):
        raise ValueError("SIZE must be the positive image size in bytes returned by registry inspection")
    record.update(docker_name=name, docker_tag=tag, image=image,
                  digest=digest.removeprefix("sha256:"), size=int(size))
    record["idempotentKey"] = hashlib.md5(image.encode("utf-8"), usedforsecurity=False).hexdigest()
    record["packageList"] = collect_image_packages(image, digest)
    return record


def build_payload(record):
    payload = {
        "artifactName": record["docker_name"],
        "artifactType": "docker",
        "businessType": "DOCKER",
        "idempotentKey": record["idempotentKey"],
        "packageList": record["packageList"],
        "osTypeList": [record["os"]],
        "hggcrtVersion": record["hggcrt"],
        "ppuSdk": record["sdk_version"],
        "python": record["python"],
        "image": record["image"],
        "digest": record["digest"],
        "packageSize": str(record["size"]),
        "torch": record["torch"],
        "nicType": record["network"],
    }
    return payload


def main():
    try:
        record = read_record()
        payload = build_payload(record)
        if not isinstance(payload, dict) or not payload:
            raise ValueError("build_payload must return a non-empty business data object")
        client = OpenApiClient(OpenApiConfig.from_env())
        client.save_artifact(payload)
        print(f"Published Docker release data: {record['image']}", flush=True)
    except (ValueError, OSError, NotImplementedError) as error:
        message = str(error).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::Failed to publish Docker release data: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
