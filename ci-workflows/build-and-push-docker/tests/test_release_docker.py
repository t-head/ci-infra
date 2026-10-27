"""Offline regression tests for the release Docker template: no image builds, logins, or real API requests."""

import ast
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest
import requests
import yaml


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
WORKFLOW = REPO / ".github/workflows/build-and-push-docker.yml"
SCRIPT = ROOT / "scripts/validate_inputs.py"
POST_SCRIPT = ROOT / "scripts/post_data.py"
DEMO = ROOT / "workflows/demo-build-and-push-docker.yml"
README = ROOT / "README.md"
FIELDS = ("os", "python", "hggcrt", "sdk_version", "torch", "network")
DEFAULT_INPUTS = {
    "dockerfile": "Dockerfile", "docker_name": "team/demo", "docker_tag": "1.0.0",
    "os": "ubuntu24.04", "python": "3.12", "hggcrt": "3",
    "sdk_version": "2.2.0", "torch": "2.13.0", "network": "mellanox/EIC",
}
IMAGE = "download.t-head.cn/docker_release/team/demo:1.0.0"
DIGEST = "sha256:" + "a" * 64


def load_module(name, path, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def validator(monkeypatch):
    return load_module("validate_inputs", SCRIPT, monkeypatch)


@pytest.fixture
def post_module(validator, monkeypatch):
    module = load_module("docker_release_post_data", POST_SCRIPT, monkeypatch)
    monkeypatch.setattr(module, "requests", SimpleNamespace(
        post=Mock(side_effect=AssertionError("Real HTTP requests are prohibited in tests")),
        RequestException=requests.RequestException,
    ))
    return module


@pytest.fixture
def source():
    with tempfile.TemporaryDirectory(prefix=".release-docker-test-", dir=ROOT) as directory:
        root = Path(directory).resolve()
        (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        yield root


@pytest.fixture
def release_env(monkeypatch):
    monkeypatch.setenv("INPUTS_JSON", json.dumps(DEFAULT_INPUTS))
    monkeypatch.setenv("IMAGE", IMAGE)
    monkeypatch.setenv("DIGEST", DIGEST)
    monkeypatch.setenv("SIZE", "123456789")
    monkeypatch.delenv("CLIENT_PRIVATE_KEY_B64", raising=False)


@pytest.mark.parametrize("network", ["mellanox", "EIC", "mellanox/EIC"])
def test_allowed_network_values_preserved_without_changing_image(validator, source, network):
    inputs = dict(DEFAULT_INPUTS, network=network)
    assert validator.release_metadata(inputs) == {key: inputs[key] for key in FIELDS}
    assert validator.validate_inputs(inputs, source) == {
        "image": IMAGE, "build_context": str(source), "dockerfile": str(source / "Dockerfile"),
    }


@pytest.mark.parametrize("network", ["eic", "Mellanox", "MELLANOX", "EIC/mellanox", "mellanox/eic",
                                      "mellanox,EIC", "mellanox EIC", "mellanox|EIC", "ethernet", "none"])
def test_network_is_exact_enum(validator, source, network):
    with pytest.raises(ValueError, match="network must be one of"):
        validator.validate_inputs(dict(DEFAULT_INPUTS, network=network), source)


@pytest.mark.parametrize("key", FIELDS)
def test_all_release_fields_required(validator, source, key):
    inputs = dict(DEFAULT_INPUTS)
    inputs.pop(key)
    with pytest.raises(ValueError, match=key):
        validator.validate_inputs(inputs, source)


@pytest.mark.parametrize("key", FIELDS)
@pytest.mark.parametrize("value", [None, True, 3.12, [], {}, "", " ", " value", "value ", "a\nb", "a\rb", "a\tb", "a\x00b"])
def test_release_fields_reject_invalid_values(validator, source, key, value):
    with pytest.raises(ValueError, match=key):
        validator.validate_inputs(dict(DEFAULT_INPUTS, **{key: value}), source)


@pytest.mark.parametrize("value", [None, [], "text", 123])
def test_inputs_must_be_json_object(validator, source, value):
    with pytest.raises(ValueError, match="JSON object"):
        validator.validate_inputs(value, source)


@pytest.mark.parametrize("name", ["demo", "team/demo", "a/b/c", "demo_package", "demo__package", "demo---package"])
def test_image_names_use_fixed_release_repository(validator, name):
    assert validator.image_reference(name, "v1") == f"download.t-head.cn/docker_release/{name}:v1"


@pytest.mark.parametrize("name", ["", "Demo", "demo:tag", "demo@sha256:123", "../demo", "/demo", "demo/",
                                  "team//demo", "demo,other", "demo\nother", "demo space", "other.example/demo",
                                  "localhost/demo", "docker_tmp/demo", "docker_release/demo",
                                  "download.t-head.cn/docker_release/demo", "a" * 256])
def test_invalid_image_names(validator, name):
    with pytest.raises(ValueError, match="docker_name|image name"):
        validator.image_reference(name, "v1")


def test_image_name_length_includes_release_prefix(validator):
    length = 255 - len("download.t-head.cn/docker_release/")
    assert validator.image_reference("a" * length, "v1").endswith(":v1")
    with pytest.raises(ValueError, match="255"):
        validator.image_reference("a" * (length + 1), "v1")


@pytest.mark.parametrize("tag", ["1.2.3", "dev-20261026", "_test", "3", "a" * 128])
def test_valid_tags(validator, tag):
    assert validator.image_reference("demo", tag).endswith(":" + tag)


@pytest.mark.parametrize("tag", ["", ".dev", "-dev", "a" * 129, "dev/test", "dev:tag", "dev,other",
                                 "dev\nother", "1.2.3+build", "dev test", "$(echo dev)"])
def test_invalid_tags(validator, tag):
    with pytest.raises(ValueError, match="docker_tag"):
        validator.image_reference("demo", tag)


@pytest.mark.parametrize("key", ["dockerfile", "build_context"])
@pytest.mark.parametrize("value", ["", "../Dockerfile", "/tmp", "https://example.invalid/context", "a\nb", "a\x00b", "a\\b"])
def test_unsafe_paths(validator, source, key, value):
    with pytest.raises(ValueError, match=key):
        validator.validate_inputs(dict(DEFAULT_INPUTS, **{key: value}), source)


def test_dockerfile_resolves_from_source_not_context(validator, source):
    (source / "app").mkdir()
    outputs = validator.validate_inputs(dict(DEFAULT_INPUTS, build_context="app"), source)
    assert outputs["build_context"] == str(source / "app")
    assert outputs["dockerfile"] == str(source / "Dockerfile")


@pytest.mark.parametrize("key", ["dockerfile", "build_context"])
def test_symlinks_cannot_escape_source(validator, source, key):
    (source / "outside").symlink_to(source.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="outside the caller repository"):
        validator.validate_inputs(dict(DEFAULT_INPUTS, **{key: "outside"}), source)


@pytest.mark.parametrize("key,value", [("dockerfile", "."), ("build_context", "Dockerfile")])
def test_wrong_path_type(validator, source, key, value):
    with pytest.raises(ValueError):
        validator.validate_inputs(dict(DEFAULT_INPUTS, **{key: value}), source)


def test_empty_dockerfile_rejected(validator, source):
    (source / "Dockerfile").write_bytes(b"")
    with pytest.raises(ValueError, match="non-empty"):
        validator.validate_inputs(DEFAULT_INPUTS, source)


@pytest.mark.parametrize("args", ["", "A=one\nB=two", "URL=https://example.invalid?a=b&c=d", "EMPTY=", "A=$(touch marker)"])
def test_build_args_remain_separate_from_metadata(validator, source, args):
    outputs = validator.validate_inputs(dict(DEFAULT_INPUTS, build_args=args), source)
    assert outputs["image"] == IMAGE
    assert "build_args" not in outputs
    assert not (source / "marker").exists()


@pytest.mark.parametrize("args", ["TOKEN", "--push", "--tag=other", "1KEY=value", "A =value", "A=x\nA=y", "A=x\rB=y", "A=x\x00y"])
def test_invalid_build_args(validator, source, args):
    with pytest.raises(ValueError, match="build_args"):
        validator.validate_inputs(dict(DEFAULT_INPUTS, build_args=args), source)


@pytest.mark.parametrize("override", [{}, {"network": "eic"}, {"dockerfile": "missing"}])
def test_validator_cli_outputs_only_on_success(source, override):
    output_file = source / "outputs"
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=source, capture_output=True, text=True, timeout=10,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", SOURCE_DIR=str(source),
                 GITHUB_OUTPUT=str(output_file), INPUTS_JSON=json.dumps(dict(DEFAULT_INPUTS, **override))),
    )
    assert (result.returncode == 0) == (not override), result.stderr
    if not override:
        assert f"image={IMAGE}\n" in output_file.read_text()
    else:
        assert not output_file.exists()


def test_release_record_contains_only_whitelisted_fields(post_module, release_env, monkeypatch):
    monkeypatch.setenv("INPUTS_JSON", json.dumps(dict(DEFAULT_INPUTS, build_args="TOKEN=do-not-copy", arbitrary="ignored")))
    expected = {key: DEFAULT_INPUTS[key] for key in (*FIELDS, "docker_name", "docker_tag")}
    record = post_module.read_record()
    assert record == dict(expected, image=IMAGE, digest="a" * 64, size=123456789,
                          idempotentKey="f4115805a430e15d115902335153f83f")
    assert type(record["size"]) is int


@pytest.mark.parametrize("key", FIELDS)
def test_missing_release_field_fails_before_build_or_api(validator, source, post_module, release_env, monkeypatch, capsys, key):
    inputs = dict(DEFAULT_INPUTS, hggcrt="3")
    inputs.pop(key)
    monkeypatch.setenv("INPUTS_JSON", json.dumps(inputs))
    with pytest.raises(ValueError, match=key):
        validator.validate_inputs(inputs, source)
    factory = Mock()
    monkeypatch.setattr(post_module, "OpenApiClient", factory)
    assert post_module.main() == 1
    assert key in capsys.readouterr().err
    factory.assert_not_called()
    post_module.requests.post.assert_not_called()


@pytest.mark.parametrize("field,value", [("DIGEST", "sha256:" + "b" * 64), ("SIZE", "999")])
def test_idempotent_key_depends_on_image_not_build_result(post_module, release_env, monkeypatch, field, value):
    original = post_module.read_record()["idempotentKey"]
    monkeypatch.setenv(field, value)
    assert post_module.read_record()["idempotentKey"] == original


@pytest.mark.parametrize("field,value", [("docker_name", "team/other"), ("docker_tag", "2.0.0")])
def test_idempotent_key_changes_with_image(post_module, release_env, monkeypatch, field, value):
    original = post_module.read_record()["idempotentKey"]
    inputs = dict(DEFAULT_INPUTS, **{field: value})
    image = f"download.t-head.cn/docker_release/{inputs['docker_name']}:{inputs['docker_tag']}"
    monkeypatch.setenv("INPUTS_JSON", json.dumps(inputs))
    monkeypatch.setenv("IMAGE", image)
    key = post_module.read_record()["idempotentKey"]
    assert key == hashlib.md5(image.encode("utf-8"), usedforsecurity=False).hexdigest()
    assert key != original


@pytest.mark.parametrize("environment,value", [
    ("INPUTS_JSON", "{"), ("INPUTS_JSON", "[]"), ("INPUTS_JSON", "{}"),
    ("IMAGE", ""), ("IMAGE", IMAGE.replace("docker_release", "docker_tmp")),
    ("IMAGE", IMAGE + "-other"), ("DIGEST", ""), ("DIGEST", "sha256:abcd"),
    ("DIGEST", "sha256:" + "a" * 64 + "\n"), ("DIGEST", "sha512:" + "a" * 64),
    ("SIZE", ""), ("SIZE", "0"), ("SIZE", "-1"), ("SIZE", "1.5"), ("SIZE", "123 MB"),
    ("SIZE", "1\ninjected=2"), ("SIZE", " 123"), ("SIZE", "true"), ("SIZE", "01"),
])
def test_invalid_release_data_never_calls_api(post_module, release_env, monkeypatch, environment, value):
    monkeypatch.setenv(environment, value)
    client = Mock()
    monkeypatch.setattr(post_module, "OpenApiClient", client)
    assert post_module.main() == 1
    client.assert_not_called()
    post_module.requests.post.assert_not_called()


@pytest.fixture
def expected_payload():
    return {
        "artifactName": "team/demo",
        "artifactType": "docker",
        "businessType": "DOCKER",
        "idempotentKey": "f4115805a430e15d115902335153f83f",
        "osTypeList": ["ubuntu24.04"],
        "hggcrtVersion": "3",
        "ppuSdk": "2.2.0",
        "python": "3.12",
        "image": IMAGE,
        "digest": "a" * 64,
        "packageSize": "123456789",
        "torch": "2.13.0",
        "nicType": "mellanox/EIC",
    }


@pytest.mark.parametrize("network", ["mellanox", "EIC", "mellanox/EIC"])
def test_release_payload_mapping(post_module, release_env, monkeypatch, expected_payload, network):
    monkeypatch.setenv("INPUTS_JSON", json.dumps(dict(
        DEFAULT_INPUTS, network=network, build_args="TOKEN=do-not-copy", arbitrary="ignored",
    )))
    record = post_module.read_record()
    original = dict(record)
    expected_payload["nicType"] = network
    payload = post_module.build_payload(record)
    assert payload == expected_payload
    assert json.loads(json.dumps(payload)) == expected_payload
    assert record == original
    assert type(record["size"]) is int
    assert payload["osTypeList"] == [record["os"]]
    assert all(isinstance(value, str) for key, value in payload.items() if key != "osTypeList")
    assert "attributes" not in payload
    post_module.requests.post.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_assembled_payload_is_published_once(post_module, release_env, monkeypatch, expected_payload, failure):
    client = Mock()
    if failure:
        client.save_artifact.side_effect = ValueError("Simulated API failure")
    factory = Mock(return_value=client)
    monkeypatch.setattr(post_module, "OpenApiClient", factory)
    assert post_module.main() == int(failure)
    factory.assert_called_once()
    client.save_artifact.assert_called_once_with(expected_payload)
    post_module.requests.post.assert_not_called()


@pytest.mark.parametrize("payload", [None, {}, [], "", "text"])
def test_empty_payload_is_never_published(post_module, release_env, monkeypatch, payload):
    monkeypatch.setattr(post_module, "build_payload", lambda record: payload)
    client = Mock()
    monkeypatch.setattr(post_module, "OpenApiClient", client)
    assert post_module.main() == 1
    client.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_custom_payload_is_sent_once_and_failure_propagates(post_module, release_env, monkeypatch, failure):
    payload = {"testBusinessField": "replace-me"}
    builder = Mock(return_value=payload)
    client = Mock()
    if failure:
        client.save_artifact.side_effect = ValueError("test failure")
    monkeypatch.setattr(post_module, "build_payload", builder)
    monkeypatch.setattr(post_module, "OpenApiClient", Mock(return_value=client))
    assert post_module.main() == int(failure)
    builder.assert_called_once_with(post_module.read_record())
    client.save_artifact.assert_called_once_with(payload)


@pytest.fixture(scope="module")
def test_keys():
    return (rsa.generate_private_key(public_exponent=65537, key_size=2048),
            rsa.generate_private_key(public_exponent=65537, key_size=2048))


@pytest.fixture
def client_config(post_module, test_keys):
    server_key, client_key = test_keys
    return post_module.OpenApiConfig(
        base_url="https://example.invalid", client_id="test-client",
        server_public_key_b64=base64.b64encode(server_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode(),
        client_private_key_b64=base64.b64encode(client_key.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())).decode(),
    )


@pytest.mark.parametrize("assembled", [False, True])
def test_request_can_be_decrypted_and_signature_verified(
    post_module, client_config, test_keys, monkeypatch, release_env, expected_payload, assembled,
):
    response = SimpleNamespace(status_code=200, headers={"Content-Type": "application/json"})
    post_module.requests.post.side_effect = None
    post_module.requests.post.return_value = response
    monkeypatch.setattr(post_module.time, "time", lambda: 1700000000.125)
    if assembled:
        # Replace only the test keys and network transport; exercise the real input, assembly, encryption, and signing flow.
        payload = expected_payload
        monkeypatch.setattr(post_module.OpenApiConfig, "from_env", classmethod(lambda cls: client_config))
        assert post_module.main() == 0
    else:
        payload = {"testBusinessField": "offline data", "network": "mellanox/EIC"}
        client = post_module.OpenApiClient(client_config)
        assert client.save_artifact(payload) is response
    post_module.requests.post.assert_called_once()
    args, kwargs = post_module.requests.post.call_args
    assert args == ("https://example.invalid/open/artifact/saveSingle",)
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == (30, 300)
    body = kwargs["data"]
    envelope = json.loads(body)
    assert set(envelope) == {"encKey", "iv", "cipher"}
    server_key, client_key = test_keys
    aes_key = server_key.decrypt(base64.b64decode(envelope["encKey"]), padding.OAEP(
        mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    assert len(aes_key) == 32
    iv = base64.b64decode(envelope["iv"])
    assert len(iv) == 12
    plaintext = AESGCM(aes_key).decrypt(iv, base64.b64decode(envelope["cipher"]), None)
    assert json.loads(plaintext) == payload
    headers = kwargs["headers"]
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Open-Client"] == "test-client"
    assert headers["X-Open-Timestamp"] == "1700000000125"
    canonical = (f"test-client\nPOST\n{post_module.PATH_SAVE}\n1700000000125\n"
                 + hashlib.sha256(body).hexdigest()).encode()
    client_key.public_key().verify(base64.b64decode(headers["X-Open-Sign"]), canonical,
                                   padding.PKCS1v15(), hashes.SHA256())
    assert client_config.client_private_key_b64 not in repr(client_config)


@pytest.mark.parametrize("status,content_type", [
    (301, "application/json"), (400, "application/json"), (401, "application/json"),
    (500, "application/json"), (200, "text/html; charset=utf-8"), (200, "application/xhtml+xml"),
])
def test_api_failures_are_not_success(post_module, client_config, status, content_type):
    post_module.requests.post.side_effect = None
    post_module.requests.post.return_value = SimpleNamespace(status_code=status, headers={"Content-Type": content_type})
    with pytest.raises(ValueError):
        post_module.OpenApiClient(client_config).save_artifact({"test": True})
    post_module.requests.post.assert_called_once()


def test_request_errors_hide_sensitive_details(post_module, client_config, capsys):
    post_module.requests.post.side_effect = requests.ConnectionError("sensitive-detail")
    with pytest.raises(ValueError, match="ConnectionError") as error:
        post_module.OpenApiClient(client_config).save_artifact({"test": True})
    assert "sensitive-detail" not in str(error.value) + capsys.readouterr().out
    post_module.requests.post.assert_called_once()


@pytest.mark.parametrize("key,value", [
    ("base_url", "https://example.invalid/path"), ("base_url", "https://user:password@example.invalid"),
    ("base_url", "https://example.invalid:bad"), ("client_id", ""), ("client_id", "bad\nclient"),
    ("client_private_key_b64", ""), ("client_private_key_b64", "invalid-key"), ("server_public_key_b64", "invalid-key"),
])
def test_bad_api_configuration_never_calls_network(post_module, client_config, key, value):
    setattr(client_config, key, value)
    with pytest.raises(ValueError):
        post_module.OpenApiClient(client_config)
    post_module.requests.post.assert_not_called()


def test_public_configuration_comes_from_local_copy(post_module, monkeypatch):
    monkeypatch.setenv("OPEN_API_BASE_URL", "https://ignored.invalid")
    monkeypatch.setenv("CLIENT_PRIVATE_KEY_B64", "test-private-key")
    config = post_module.OpenApiConfig.from_env()
    assert config.base_url == post_module.OPEN_API_BASE_URL
    assert config.client_id == post_module.OPEN_API_CLIENT_ID
    assert config.server_public_key_b64 == post_module.SERVER_PUBLIC_KEY_B64
    assert config.client_private_key_b64 == "test-private-key"
    assert "release-pypi" not in POST_SCRIPT.read_text()


def test_workflow_contract_and_execution_order():
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    contract = workflow["on"]["workflow_call"]
    # Validate the release workflow interface directly, without depending on the temporary Docker template absent from this branch.
    required_inputs = {"dockerfile", "docker_name", "docker_tag", "template_ref", *FIELDS}
    defaults = {"build_context": ".", "build_args": "", "template_repository": "t-head/ci-infra"}
    assert set(workflow["on"]) == {"workflow_call"}
    assert set(contract["inputs"]) == required_inputs | set(defaults)
    for key, definition in contract["inputs"].items():
        assert definition["type"] == "string"
        assert definition["required"] == ("true" if key in required_inputs else "false")
        if key in required_inputs:
            assert "default" not in definition
        else:
            assert definition["default"] == defaults[key]
    assert set(contract["secrets"]) == {"template_token", "release_username", "release_token", "data_token"}
    assert contract["secrets"]["data_token"]["required"] == "true"
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["build-and-push"]
    source, template, python, validate, credentials, dependencies, buildx, login, build, inspect, summary, post = job["steps"]
    assert job["runs-on"] == "ubuntu-latest" and job["timeout-minutes"] == "120"
    assert source["uses"] == template["uses"] == "actions/checkout@v4"
    assert source["with"] == {"path": "caller-source", "fetch-depth": "0", "submodules": "recursive",
                              "persist-credentials": "false"}
    assert template["with"]["token"] == "${{ secrets.template_token || github.token }}"
    assert template["with"]["path"] == "ci-template"
    assert template["with"]["persist-credentials"] == "false"
    assert template["with"]["repository"] == "${{ inputs.template_repository }}"
    assert template["with"]["ref"] == "${{ inputs.template_ref }}"
    assert python["with"]["python-version"] == "3.12"
    assert validate["env"] == {"SOURCE_DIR": "${{ github.workspace }}/caller-source", "INPUTS_JSON": "${{ toJSON(inputs) }}"}
    assert validate["run"] == "python ci-template/ci-workflows/build-and-push-docker/scripts/validate_inputs.py"
    assert dependencies["run"] == "python -m pip install --no-input requests cryptography"
    assert buildx["uses"] == "docker/setup-buildx-action@v3"
    assert login["uses"] == "docker/login-action@v3"
    assert login["with"] == {"registry": "download.t-head.cn", "username": "${{ secrets.release_username }}",
                              "password": "${{ secrets.release_token }}", "logout": "true"}
    assert build["uses"] == "docker/build-push-action@v6"
    assert build["with"] == {
        "context": "${{ steps.validate.outputs.build_context }}", "file": "${{ steps.validate.outputs.dockerfile }}",
        "build-args": "${{ inputs.build_args }}", "tags": "${{ steps.validate.outputs.image }}",
        "platforms": "linux/amd64", "push": "true",
    }
    assert inspect["id"] == "inspect"
    assert inspect["env"] == {"IMAGE": "${{ steps.validate.outputs.image }}", "DIGEST": "${{ steps.build.outputs.digest }}"}
    assert inspect["run"] == "python ci-template/ci-workflows/build-and-push-docker/scripts/inspect_image.py"
    assert summary["env"]["SIZE"] == "${{ steps.inspect.outputs.size }}"
    assert post["env"] == {"INPUTS_JSON": "${{ toJSON(inputs) }}", "IMAGE": "${{ steps.validate.outputs.image }}",
                            "DIGEST": "${{ steps.build.outputs.digest }}", "SIZE": "${{ steps.inspect.outputs.size }}",
                            "CLIENT_PRIVATE_KEY_B64": "${{ secrets.data_token }}"}
    assert post["run"] == "python ci-template/ci-workflows/build-and-push-docker/scripts/post_data.py"
    assert job["outputs"] == {"image": "${{ steps.validate.outputs.image }}", "digest": "${{ steps.build.outputs.digest }}",
                              "size": "${{ steps.inspect.outputs.size }}"}
    assert "env" not in job and "env" not in workflow
    for key in ("image", "digest", "size"):
        assert contract["outputs"][key]["value"] == "${{ jobs.build-and-push.outputs." + key + " }}"
    for step in job["steps"]:
        assert "if" not in step and "continue-on-error" not in step
        assert "${{" not in step.get("run", "")
        if step not in (credentials, post):
            assert "secrets.data_token" not in str(step)
        if step not in (credentials, login):
            assert "secrets.release_" not in str(step)
        if "run" in step:
            result = subprocess.run(["bash", "-n"], input=step["run"], capture_output=True, text=True, timeout=5)
            assert result.returncode == 0, result.stderr


def test_demo_matches_reusable_workflow_contract():
    demo = yaml.load(DEMO.read_text(), Loader=yaml.BaseLoader)
    contract = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)["on"]["workflow_call"]
    assert set(demo["on"]) == {"workflow_dispatch"}
    assert demo["permissions"] == {"contents": "read"}
    publish = demo["jobs"]["publish"]
    inputs = publish["with"]
    assert set(inputs) == set(contract["inputs"])
    repository, workflow_path = publish["uses"].split("/.github/workflows/")
    filename, ref = workflow_path.rsplit("@", 1)
    assert filename == WORKFLOW.name
    assert repository == inputs["template_repository"] == contract["inputs"]["template_repository"]["default"]
    assert ref == inputs["template_ref"]
    assert "steps" not in publish and "runs-on" not in publish
    manual_inputs = demo["on"]["workflow_dispatch"]["inputs"]
    for key in manual_inputs:
        assert inputs[key] == "${{ inputs." + key + " }}"
    assert manual_inputs["network"]["type"] == "choice"
    assert manual_inputs["network"]["required"] == "true"
    assert manual_inputs["network"]["options"] == ["mellanox", "EIC", "mellanox/EIC"]
    for key in ("docker_name", "docker_tag", "dockerfile"):
        assert manual_inputs[key]["required"] == "true"
    assert publish["secrets"] == {
        "release_username": "${{ secrets.RELEASE_USERNAME }}",
        "release_token": "${{ secrets.RELEASE_TOKEN }}",
        "data_token": "${{ secrets.DATA_TOKEN }}",
    }
    required_secrets = {key for key, value in contract["secrets"].items() if value["required"] == "true"}
    assert required_secrets <= set(publish["secrets"]) <= set(contract["secrets"])
    typed_inputs = yaml.safe_load(DEMO.read_text())["jobs"]["publish"]["with"]
    assert all(isinstance(value, str) for value in typed_inputs.values())


@pytest.mark.parametrize("network", ["mellanox", "EIC", "mellanox/EIC"])
def test_demo_resolved_inputs_pass_validation(validator, source, post_module, release_env, monkeypatch, network):
    demo = yaml.load(DEMO.read_text(), Loader=yaml.BaseLoader)
    manual = {key: value.get("default", "")
              for key, value in demo["on"]["workflow_dispatch"]["inputs"].items()}
    manual.update(docker_name="team/demo", docker_tag="1.0.0", network=network)
    inputs = dict(demo["jobs"]["publish"]["with"])
    for key, value in manual.items():
        inputs[key] = value
    assert validator.validate_inputs(inputs, source) == {
        "image": IMAGE, "build_context": str(source), "dockerfile": str(source / "Dockerfile"),
    }
    # The example uses its own environment versions; verify metadata is preserved rather than replaced with unit test defaults.
    assert validator.release_metadata(inputs) == {
        **{key: demo["jobs"]["publish"]["with"][key] for key in FIELDS if key != "network"},
        "network": network,
    }
    monkeypatch.setenv("INPUTS_JSON", json.dumps(inputs))
    client = Mock()
    monkeypatch.setattr(post_module, "OpenApiClient", Mock(return_value=client))
    assert post_module.main() == 0
    client.save_artifact.assert_called_once_with({
        "artifactName": "team/demo", "artifactType": "docker", "businessType": "DOCKER",
        "idempotentKey": "f4115805a430e15d115902335153f83f",
        "osTypeList": [inputs["os"]], "hggcrtVersion": inputs["hggcrt"],
        "ppuSdk": inputs["sdk_version"], "python": inputs["python"],
        "image": IMAGE, "digest": "a" * 64, "packageSize": "123456789",
        "torch": inputs["torch"], "nicType": network,
    })


def test_demo_prints_outputs_without_shell_interpolation():
    demo = yaml.load(DEMO.read_text(), Loader=yaml.BaseLoader)
    result = demo["jobs"]["result"]
    assert result["needs"] == "publish"
    assert "if" not in result and "continue-on-error" not in result
    assert result["timeout-minutes"] == "5"
    step, = result["steps"]
    assert step["shell"] == "bash"
    assert step["env"] == {
        key.upper(): "${{ needs.publish.outputs." + key + " }}" for key in ("image", "digest", "size")
    }
    assert "${{" not in step["run"]
    assert "secrets." not in str(result)
    syntax = subprocess.run(["bash", "-n"], input=step["run"], capture_output=True, text=True, timeout=5)
    assert syntax.returncode == 0, syntax.stderr


def test_template_documentation_links_and_fields(expected_payload):
    text = README.read_text()
    contract = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)["on"]["workflow_call"]
    documented = set(re.findall(r"^\| `([^`]+)` \|", text, flags=re.MULTILINE))
    for group in ("inputs", "secrets", "outputs"):
        assert set(contract[group]) <= documented
    assert set(expected_payload) <= documented
    assert "| `attributes` |" not in text
    links = re.findall(r"\]\(([^)]+)\)", text)
    assert "../../.github/workflows/build-and-push-docker.yml" in links
    assert "workflows/demo-build-and-push-docker.yml" in links
    for link in links:
        if not link.startswith(("https://", "http://", "#")):
            assert (ROOT / link.split("#", 1)[0]).is_file(), link
    assert "(ci-workflows/build-and-push-docker/README.md)" in (REPO / "README.md").read_text()


@pytest.mark.parametrize("missing", [None, "RELEASE_USERNAME", "RELEASE_TOKEN", "CLIENT_PRIVATE_KEY_B64"])
def test_credentials_checked_before_build_without_logging(missing):
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    guard = next(step for step in workflow["jobs"]["build-and-push"]["steps"] if step["name"] == "Validate publishing credentials")
    env = dict(os.environ, RELEASE_USERNAME="test-user", RELEASE_TOKEN="test-token", CLIENT_PRIVATE_KEY_B64="test-private-key")
    if missing:
        env[missing] = " \t\n"
    result = subprocess.run(["bash", "-c", guard["run"]], env=env, capture_output=True, text=True, timeout=5)
    assert (result.returncode == 0) == (missing is None)
    for secret in ("test-user", "test-token", "test-private-key"):
        assert secret not in result.stdout + result.stderr


@pytest.fixture
def image_inspector(validator, monkeypatch):
    module = load_module("release_image_inspector", ROOT / "scripts/inspect_image.py", monkeypatch)
    monkeypatch.setattr(module, "subprocess", SimpleNamespace(
        run=Mock(side_effect=AssertionError("Real Docker commands are prohibited in tests")),
        SubprocessError=subprocess.SubprocessError,
    ))
    return module


@pytest.fixture
def image_manifest():
    return {
        "schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"size": 100}, "layers": [{"size": 1024}, {"size": 2048}],
    }


@pytest.mark.parametrize("media_type", ["application/vnd.oci.image.manifest.v1+json",
                                        "application/vnd.docker.distribution.manifest.v2+json"])
def test_single_manifest_size_uses_pushed_digest(image_inspector, image_manifest, media_type):
    image_manifest["mediaType"] = media_type
    image_inspector.subprocess.run.side_effect = None
    image_inspector.subprocess.run.return_value = SimpleNamespace(stdout=json.dumps(image_manifest))
    assert image_inspector.inspect_image_size(IMAGE, DIGEST) == 3172
    image_inspector.subprocess.run.assert_called_once_with(
        ["docker", "buildx", "imagetools", "inspect", "--raw", IMAGE.rsplit(":", 1)[0] + "@" + DIGEST],
        check=True, capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize("media_type", ["application/vnd.oci.image.index.v1+json",
                                        "application/vnd.docker.distribution.manifest.list.v2+json"])
def test_index_ignores_attestations_and_other_platforms(image_inspector, image_manifest, media_type):
    child_digest = "sha256:" + "b" * 64
    index = {"schemaVersion": 2, "mediaType": media_type, "manifests": [
        {"digest": "sha256:" + "c" * 64, "size": 987654,
         "platform": {"os": "unknown", "architecture": "unknown"}},
        {"digest": "sha256:" + "d" * 64, "size": 888888,
         "platform": {"os": "linux", "architecture": "arm64"}},
        {"digest": "sha256:" + "e" * 64, "size": 777777,
         "platform": {"os": "linux", "architecture": "amd64"},
         "annotations": {"vnd.docker.reference.type": "attestation-manifest"}},
        {"digest": child_digest, "size": 1234,
         "platform": {"os": "linux", "architecture": "amd64"}},
    ]}
    image_inspector.subprocess.run.side_effect = [
        SimpleNamespace(stdout=json.dumps(index)), SimpleNamespace(stdout=json.dumps(image_manifest)),
    ]
    assert image_inspector.inspect_image_size(IMAGE, DIGEST) == 3172
    references = [call.args[0][-1] for call in image_inspector.subprocess.run.call_args_list]
    assert references == [IMAGE.rsplit(":", 1)[0] + "@" + value for value in (DIGEST, child_digest)]


@pytest.mark.parametrize("overrides", [
    {"config": None}, {"layers": None}, {"layers": [None]}, {"config": {}},
    {"config": {"size": -1}}, {"config": {"size": "100"}}, {"config": {"size": True}},
    {"layers": [{"size": -1}]}, {"layers": [{"size": "2048"}]}, {"layers": [{"size": 1.5}]},
    {"layers": [{"size": False}]}, {"layers": [{}]}, {"config": {"size": 0}, "layers": []},
    {"mediaType": "unsupported"}, {"mediaType": {}}, {"schemaVersion": 1},
])
def test_invalid_manifest_never_produces_a_size(image_inspector, image_manifest, overrides):
    image_manifest.update(overrides)
    image_inspector.subprocess.run.side_effect = None
    image_inspector.subprocess.run.return_value = SimpleNamespace(stdout=json.dumps(image_manifest))
    with pytest.raises(ValueError):
        image_inspector.inspect_image_size(IMAGE, DIGEST)


def test_scratch_image_with_no_layers_includes_config(image_inspector, image_manifest):
    image_manifest["layers"] = []
    assert image_inspector.manifest_size(image_manifest) == 100


@pytest.mark.parametrize("entries", [None, {}, [], [None], [{"platform": None}], [{"annotations": []}],
    [{"platform": {"os": "linux", "architecture": "arm64"}}],
    [{"platform": {"os": "linux", "architecture": "amd64"}}],
    [{"platform": {"os": "linux", "architecture": "amd64"}, "digest": "invalid"}],
    [{"platform": {"os": "linux", "architecture": "amd64"}, "digest": DIGEST}] * 2,
])
def test_index_requires_one_valid_amd64_descriptor(image_inspector, entries):
    image_inspector.subprocess.run.side_effect = None
    image_inspector.subprocess.run.return_value = SimpleNamespace(stdout=json.dumps({
        "schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": entries,
    }))
    with pytest.raises(ValueError):
        image_inspector.inspect_image_size(IMAGE, DIGEST)
    image_inspector.subprocess.run.assert_called_once()


@pytest.mark.parametrize("image,digest", [
    ("", DIGEST), (IMAGE.replace("docker_release", "docker_tmp"), DIGEST),
    (IMAGE.replace("download.t-head.cn", "example.invalid"), DIGEST),
    (IMAGE.rsplit(":", 1)[0], DIGEST), (IMAGE + "\ninjected", DIGEST),
    (IMAGE, ""), (IMAGE, "sha256:abc"), (IMAGE, "sha256:" + "a" * 64 + "\n"),
])
def test_inspection_rejects_unsafe_references_without_docker(image_inspector, image, digest):
    with pytest.raises(ValueError):
        image_inspector.inspect_image_size(image, digest)
    image_inspector.subprocess.run.assert_not_called()


@pytest.mark.parametrize("error", [
    subprocess.CalledProcessError(1, "docker", stderr="sensitive-registry-error"),
    subprocess.TimeoutExpired("docker", 120, stderr="sensitive-registry-error"),
    FileNotFoundError("sensitive-registry-error"),
])
def test_registry_failure_does_not_emit_size_or_credentials(image_inspector, release_env, source, monkeypatch, capsys, error):
    output = source / "outputs"
    output.write_text("previous=value\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    image_inspector.subprocess.run.side_effect = error
    assert image_inspector.main() == 1
    assert output.read_text() == "previous=value\n"
    captured = capsys.readouterr()
    assert "sensitive-registry-error" not in captured.out + captured.err


@pytest.mark.parametrize("raw", ["not-json", "null", "[]", "{}"])
def test_malformed_registry_response_fails(image_inspector, raw):
    image_inspector.subprocess.run.side_effect = None
    image_inspector.subprocess.run.return_value = SimpleNamespace(stdout=raw)
    with pytest.raises(ValueError):
        image_inspector.inspect_image_size(IMAGE, DIGEST)


def test_inspect_output_flows_to_release_record(image_inspector, image_manifest, post_module, release_env, source, monkeypatch):
    output = source / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    image_inspector.subprocess.run.side_effect = None
    image_inspector.subprocess.run.return_value = SimpleNamespace(stdout=json.dumps(image_manifest))
    assert image_inspector.main() == 0
    assert output.read_text() == "size=3172\n"
    monkeypatch.setenv("SIZE", output.read_text().strip().split("=", 1)[1])
    record = post_module.read_record()
    assert record["size"] == 3172
    assert record["digest"] == "a" * 64
    assert os.environ["DIGEST"] == DIGEST
    builder = Mock(return_value={"test": True})
    client = Mock()
    monkeypatch.setattr(post_module, "build_payload", builder)
    monkeypatch.setattr(post_module, "OpenApiClient", Mock(return_value=client))
    assert post_module.main() == 0
    builder.assert_called_once_with(record)
    client.save_artifact.assert_called_once_with({"test": True})


def test_inspection_requires_output_path_before_docker(image_inspector, release_env, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert image_inspector.main() == 1
    image_inspector.subprocess.run.assert_not_called()


def test_root_readme_routes_to_template_documentation():
    index = (REPO / "README.md").read_text()
    assert "(ci-workflows/build-and-push-docker/README.md)" in index
    assert "build_payload(record)" not in index
    assert "build_payload(record)" in README.read_text()
    assert "(workflows/demo-build-and-push-docker.yml)" in README.read_text()


def test_public_configuration_matches_repository_api(post_module):
    # Parse only the existing public configuration in this repository; do not import or execute the PyPI publishing script.
    path = REPO / "ci-workflows/release-pypi/scripts/post_data.py"
    keys = {"OPEN_API_BASE_URL", "PATH_SAVE", "OPEN_API_CLIENT_ID", "SERVER_PUBLIC_KEY_B64"}
    values = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in keys
    }
    assert set(values) == keys
    for name, value in values.items():
        assert getattr(post_module, name) == value
    assert post_module.OPEN_API_BASE_URL == "https://developer.t-head.cn"
    assert post_module.PATH_SAVE == "/open/artifact/saveSingle"
    assert post_module.OPEN_API_CLIENT_ID == "sync-artifact-client"
    key = serialization.load_der_public_key(base64.b64decode(post_module.SERVER_PUBLIC_KEY_B64, validate=True))
    assert isinstance(key, rsa.RSAPublicKey)


@pytest.mark.parametrize("script_name,override,success", [
    ("validate_inputs.py", {}, True),
    ("validate_inputs.py", {"dockerfile": "missing"}, False),
    ("inspect_image.py", {}, False),
    ("post_data.py", {}, False),
])
def test_migrated_workflow_commands_run_from_checkout(source, script_name, override, success):
    workspace = source / "workspace"
    caller = workspace / "caller-source"
    caller.mkdir(parents=True)
    (caller / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    scripts = workspace / "ci-template" / SCRIPT.parent.relative_to(REPO)
    scripts.mkdir(parents=True)
    for script in SCRIPT.parent.glob("*.py"):
        (scripts / script.name).write_bytes(script.read_bytes())
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    step = next(step for step in workflow["jobs"]["build-and-push"]["steps"]
                if step.get("run", "").endswith("/" + script_name))
    output = workspace / "outputs"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", SOURCE_DIR=str(caller),
               INPUTS_JSON=json.dumps(dict(DEFAULT_INPUTS, **override)), GITHUB_OUTPUT=str(output),
               IMAGE="", DIGEST=DIGEST, SIZE="3172", CLIENT_PRIVATE_KEY_B64="", TEST_PYTHON=sys.executable)
    # An empty IMAGE makes inspection and publishing exit before accessing Docker/API, while still validating paths and local imports.
    result = subprocess.run(["bash"], cwd=workspace,
                            input='python() { "$TEST_PYTHON" "$@"; }\n' + step["run"],
                            env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == (0 if success else 1), result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    if success:
        assert dict(line.split("=", 1) for line in output.read_text().splitlines()) == {
            "image": IMAGE, "build_context": str(caller), "dockerfile": str(caller / "Dockerfile"),
        }
    else:
        expected = "does not exist" if script_name == "validate_inputs.py" else "IMAGE must"
        assert expected in result.stderr
        assert not output.exists()


def test_module_ci_covers_migrated_files_without_publishing():
    path = REPO / ".github/workflows/build-and-push-docker-ci.yml"
    text = path.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"pull_request", "push"}
    paths = {"ci-workflows/build-and-push-docker/**", ".github/workflows/build-and-push-docker.yml",
             ".github/workflows/build-and-push-docker-ci.yml", "ci-workflows/release-pypi/scripts/post_data.py",
             "README.md"}
    for event in ("pull_request", "push"):
        assert set(workflow["on"][event]["paths"]) == paths
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets" not in text
    job = workflow["jobs"]["tests"]
    assert job["runs-on"] == "ubuntu-latest" and job["timeout-minutes"] == "15"
    assert job["strategy"] == {"fail-fast": "false", "matrix": {"python": ["3.12", "3.14"]}}
    checkout, setup, install, run = job["steps"]
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"]["persist-credentials"] == "false"
    assert setup["uses"] == "actions/setup-python@v5"
    assert setup["with"]["python-version"] == "${{ matrix.python }}"
    assert install["run"] == "python -m pip install --no-input pytest PyYAML requests cryptography"
    assert run["env"] == {"PYTHONDONTWRITEBYTECODE": "1"}
    assert run["run"] == "python -m pytest ci-workflows/build-and-push-docker/tests -q -p no:cacheprovider"
    for step in (install, run):
        assert "${{" not in step["run"]
        syntax = subprocess.run(["bash", "-n"], input=step["run"], capture_output=True, text=True, timeout=5)
        assert syntax.returncode == 0, syntax.stderr
    assert "(../../.github/workflows/build-and-push-docker-ci.yml)" in README.read_text()
    assert run["run"] in README.read_text()
