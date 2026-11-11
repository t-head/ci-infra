"""Offline tests for the upload artifact properties template; curl requests are mocked and never reach Artifactory."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import Mock
from urllib.parse import unquote
import zipfile

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/upload_artifact.py"
WORKFLOW = ROOT.parents[1] / ".github/workflows/upload-artifact-properties.yml"
DEMO = ROOT / "workflows/demo-upload-artifact-properties.yml"

BASE_URL = "https://download.t-head.cn/artifactory/github_generic/nightly"
RUN_ID = "100"
RUN_ATTEMPT = "1"
CALLER_REPOSITORY = "caller-org/demo-repo"
VERSION = "1.2.3"
UPLOAD_DIR = f"{BASE_URL}/{CALLER_REPOSITORY}/{VERSION}/"
STORAGE_DIR = (
    "https://download.t-head.cn/artifactory/api/storage/github_generic/nightly/"
    f"{CALLER_REPOSITORY}/{VERSION}/"
)
DEMO_FILENAME = "demo.txt"

DEFAULT_INPUTS = {
    "version": VERSION,
    "package_type": "raw",
    "package_name": "demo_package",
    "package_version": VERSION,
    "os": "ubuntu24.04",
    "python_version": "3.12",
    "runtime_type": "sail",
    "runtime": "13.0.0",
    "torch_version": "",
    "sdk_version": "2.2.0",
    "package_edition": "standard",
    "extra_info": "",
}
# Values injected by the workflow YAML from GitHub Actions context; the script
# reads them from environment variables rather than inputs.
DEFAULT_CONTEXT_ENV = {
    "GITHUB_BRANCH": "main",
    "GITHUB_TAG": "",
    "GITHUB_SHA": "abcdef1234567890",
    "GITHUB_LOG_URL": "https://example.invalid/logs/100",
    "GITHUB_RUN_ID": RUN_ID,
    "GITHUB_RUN_ATTEMPT": RUN_ATTEMPT,
}
PROPERTY_ORDER = (
    "package_type", "package_name", "package_version", "version", "os", "python_version",
    "runtime_type", "runtime", "torch_version", "sdk_version", "package_edition", "branch",
    "tag", "commit", "log_url", "run_id", "run_attempt", "extra_info",
)
CONTEXT_SOURCED_PROPERTIES = ("branch", "tag", "commit", "log_url", "run_id", "run_attempt")
INPUT_SOURCED_PROPERTIES = tuple(
    k for k in PROPERTY_ORDER if k not in (*CONTEXT_SOURCED_PROPERTIES, "version")
)
CONTEXT_PROPERTY_ENV = {
    "branch": "GITHUB_BRANCH",
    "tag": "GITHUB_TAG",
    "commit": "GITHUB_SHA",
    "log_url": "GITHUB_LOG_URL",
    "run_id": "GITHUB_RUN_ID",
    "run_attempt": "GITHUB_RUN_ATTEMPT",
}


def expected_property_values():
    values = {}
    for key in PROPERTY_ORDER:
        if key in CONTEXT_PROPERTY_ENV:
            values[key] = DEFAULT_CONTEXT_ENV[CONTEXT_PROPERTY_ENV[key]]
        else:
            values[key] = DEFAULT_INPUTS[key]
    return values


@pytest.fixture
def uploader():
    spec = importlib.util.spec_from_file_location("upload_artifact", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory(prefix=".upload-artifact-test-", dir=ROOT) as directory:
        yield Path(directory).resolve()


@pytest.fixture
def make_file(workspace):
    def make(filename=DEMO_FILENAME, directory="artifact", content=b"payload"):
        path = workspace / directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path
    return make


@pytest.fixture
def run_upload(uploader, workspace, monkeypatch):
    def respond(args, **kwargs):
        url, source = _last_curl_flags(args)
        if url.startswith("https://download.t-head.cn/artifactory/api/storage/github_generic/nightly/"):
            assert source == os.devnull
            assert url.endswith("&recursive=0")
            _query_pairs(url)
            return subprocess.CompletedProcess(args, 0, "204", "")
        assert url.startswith(BASE_URL + "/")
        # A misplaced /api/storage is an ordinary artifact upload and returns 201.
        return subprocess.CompletedProcess(args, 0, "201", "")

    transport = Mock(side_effect=respond)
    monkeypatch.setattr(uploader.subprocess, "run", transport)

    def run(inputs=None, **environment):
        env = {
            "ARTIFACT_DIR": str(workspace / "artifact"),
            "GITHUB_REPOSITORY": CALLER_REPOSITORY,
            "INPUTS_JSON": json.dumps(DEFAULT_INPUTS if inputs is None else inputs),
            "RELEASE_USERNAME": "test-user",
            "RELEASE_TOKEN": "test-token",
            "GITHUB_OUTPUT": str(workspace / "outputs"),
            **DEFAULT_CONTEXT_ENV,
        }
        env.update(environment)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return uploader.main(), transport
    return run


@pytest.fixture
def artifact_zip_bytes():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nested/demo.txt", b"original payload")
        archive.writestr("binary.dat", bytes(range(256)))
        archive.writestr("../outside.txt", b"must never be extracted")
    return buffer.getvalue()


@pytest.mark.parametrize("version", ["custom", ""])
@pytest.mark.parametrize("filename", ["bundle.zip", "artifact"])
def test_downloaded_zip_is_uploaded_without_extraction_or_renaming(
    version, filename, make_file, run_upload, artifact_zip_bytes, workspace,
):
    archive = make_file(filename=filename, content=artifact_zip_bytes)
    code, uploads = run_upload(inputs=dict(DEFAULT_INPUTS, version=version))
    assert code == 0
    assert uploads.call_count == 2
    upload_url, source = _last_curl_flags(_curl_args(uploads)[0])
    storage_url, _ = _last_curl_flags(_curl_args(uploads)[1])
    expected_version = version or "1.2.3+abcdef1.ppu2.2.0"
    encoded = expected_version.replace("+", "%2B")
    assert upload_url == f"{BASE_URL}/{CALLER_REPOSITORY}/{encoded}/{filename}"
    assert source == str(archive)
    assert storage_url.partition("?")[0] == (
        "https://download.t-head.cn/artifactory/api/storage/github_generic/nightly/"
        f"{CALLER_REPOSITORY}/{encoded}/{filename}"
    )
    assert _query_pairs(storage_url) == dict(expected_property_values(), version=expected_version)
    assert archive.read_bytes() == artifact_zip_bytes
    assert list((workspace / "artifact").iterdir()) == [archive]
    assert not (workspace / "outside.txt").exists()
    assert "demo.txt" not in (workspace / "outputs").read_text()


def _curl_args(transport):
    return [call.args[0] for call in transport.call_args_list]


def _last_curl_flags(args):
    # Return (url, upload_source) for a curl invocation.
    url = args[args.index("--") + 1]
    source = args[args.index("--upload-file") + 1] if "--upload-file" in args else None
    return url, source


def _query_pairs(url):
    _, _, query = url.partition("?")
    assert query.startswith("properties="), url
    properties, separator, options = query[len("properties="):].partition("&")
    assert (separator, options) == ("&", "recursive=0")
    pairs = {}
    for chunk in properties.split(";"):
        key, separator, value = chunk.partition("=")
        assert separator == "=" and key, chunk
        decoded = iter(unquote(value))
        literal = []
        for char in decoded:
            if char == "\\":
                escaped = next(decoded, None)
                assert escaped in ("\\", ",", "|", "="), value
                literal.append(escaped)
            else:
                assert char not in ",|=", value
                literal.append(char)
        assert unquote(key) not in pairs
        pairs[unquote(key)] = "".join(literal)
    return pairs


def test_uploads_and_sets_properties_for_every_regular_file(make_file, run_upload, workspace):
    path = make_file(directory="artifact/nested/build output")
    before = path.read_bytes()
    code, transport = run_upload()
    assert code == 0, _curl_args(transport)
    assert transport.call_count == 2
    upload_args, storage_args = _curl_args(transport)
    upload_url, upload_source = _last_curl_flags(upload_args)
    storage_url, storage_source = _last_curl_flags(storage_args)
    assert upload_url == UPLOAD_DIR + DEMO_FILENAME
    assert upload_source == str(path)
    assert storage_url.startswith(STORAGE_DIR + DEMO_FILENAME + "?properties=")
    assert storage_source == os.devnull
    for args in (upload_args, storage_args):
        assert args[:2] == ["curl", "--disable"]
        assert args[args.index("--user") + 1] == "test-user:test-token"
        assert args[args.index("--proto") + 1] == "=https"
        assert args[args.index("--retry") + 1] == "3"
        assert "--location" not in args
        assert "--insecure" not in args
    assert path.read_bytes() == before
    outputs = (workspace / "outputs").read_text()
    assert outputs.startswith(f"batch_url={UPLOAD_DIR}\nupload_urls<<")
    assert UPLOAD_DIR + DEMO_FILENAME in outputs


def test_property_query_carries_every_configured_field(make_file, run_upload):
    make_file()
    code, transport = run_upload()
    assert code == 0
    storage_url = _last_curl_flags(_curl_args(transport)[1])[0]
    pairs = _query_pairs(storage_url)
    assert tuple(pairs) == PROPERTY_ORDER
    expected = expected_property_values()
    for key in PROPERTY_ORDER:
        assert pairs[key] == expected[key], key


@pytest.mark.parametrize("version", [VERSION, ""])
@pytest.mark.parametrize("override,expected", [
    ({}, "sail"),
    ({"runtime_type": None}, "sail"),
    ({"runtime_type": "sail"}, "sail"),
    ({"runtime_type": "rocm"}, "rocm"),
    ({"runtime_type": "cpu"}, "cpu"),
    ({"runtime_type": "sail"}, "sail"),
    ({"runtime_type": ""}, ""),
    ({"runtime_type": "custom;x=y,z|w&recursive=1"}, "custom;x=y,z|w&recursive=1"),
])
def test_runtime_type_default_and_override_only_change_zip_property(
    version, override, expected, make_file, run_upload, artifact_zip_bytes, workspace,
):
    archive = make_file(filename="bundle.zip", content=artifact_zip_bytes)
    inputs = dict(DEFAULT_INPUTS, version=version)
    del inputs["runtime_type"]
    inputs.update(override)
    code, transport = run_upload(inputs=inputs)
    assert code == 0
    assert transport.call_count == 2
    upload_url, source = _last_curl_flags(_curl_args(transport)[0])
    storage_url, _ = _last_curl_flags(_curl_args(transport)[1])
    expected_version = version or "1.2.3+abcdef1.ppu2.2.0"
    encoded = expected_version.replace("+", "%2B")
    batch_url = f"{BASE_URL}/{CALLER_REPOSITORY}/{encoded}/"
    assert upload_url == batch_url + "bundle.zip"
    assert source == str(archive)
    assert storage_url.partition("?")[0] == (
        "https://download.t-head.cn/artifactory/api/storage/github_generic/nightly/"
        f"{CALLER_REPOSITORY}/{encoded}/bundle.zip"
    )
    assert _query_pairs(storage_url) == dict(
        expected_property_values(), runtime_type=expected, version=expected_version,
    )
    assert archive.read_bytes() == artifact_zip_bytes
    assert (workspace / "outputs").read_text() == (
        f"batch_url={batch_url}\nupload_urls<<UPLOAD_URLS_EOF\n{upload_url}\nUPLOAD_URLS_EOF\n"
    )


@pytest.mark.parametrize("value", [123, False, [], {}])
def test_non_string_runtime_type_stops_before_upload(value, make_file, run_upload, workspace, capsys):
    make_file()
    code, transport = run_upload(inputs=dict(DEFAULT_INPUTS, runtime_type=value))
    assert code == 1
    transport.assert_not_called()
    assert not (workspace / "outputs").exists()
    assert "runtime_type must be a string" in capsys.readouterr().err


@pytest.mark.parametrize("version,encoded", [
    ("custom-v2", "custom-v2"),
    ("1.0.0+custom", "1.0.0%2Bcustom"),
    ("0", "0"),
])
def test_explicit_version_is_shared_by_paths_properties_and_outputs(
    version, encoded, make_file, run_upload, workspace,
):
    make_file()
    code, transport = run_upload(inputs=dict(DEFAULT_INPUTS, version=version))
    assert code == 0
    upload_url = _last_curl_flags(_curl_args(transport)[0])[0]
    storage_url = _last_curl_flags(_curl_args(transport)[1])[0]
    batch_url = f"{BASE_URL}/{CALLER_REPOSITORY}/{encoded}/"
    assert upload_url == batch_url + DEMO_FILENAME
    assert storage_url.partition("?")[0] == (
        "https://download.t-head.cn/artifactory/api/storage/github_generic/nightly/"
        f"{CALLER_REPOSITORY}/{encoded}/{DEMO_FILENAME}"
    )
    assert _query_pairs(storage_url) == dict(expected_property_values(), version=version)
    assert f";version={encoded};" in storage_url
    assert (workspace / "outputs").read_text() == (
        f"batch_url={batch_url}\nupload_urls<<UPLOAD_URLS_EOF\n{upload_url}\nUPLOAD_URLS_EOF\n"
    )


@pytest.mark.parametrize("override", [{}, {"version": ""}, {"version": None}])
@pytest.mark.parametrize("package_version,sdk_version,sha,expected,encoded", [
    ("1.2.3", "2.2.0", "abcdef1234567890", "1.2.3+abcdef1.ppu2.2.0", "1.2.3%2Babcdef1.ppu2.2.0"),
    ("2.0.0rc1", "3.0.1", "1234567890abcdef", "2.0.0rc1+1234567.ppu3.0.1", "2.0.0rc1%2B1234567.ppu3.0.1"),
])
def test_default_version_is_shared_by_all_files_paths_properties_and_outputs(
    override, package_version, sdk_version, sha, expected, encoded, make_file, run_upload, workspace,
):
    for name in ("a.txt", "b.txt"):
        make_file(filename=name)
    inputs = dict(DEFAULT_INPUTS, package_version=package_version, sdk_version=sdk_version,
                  commit="fake", short_commit="fake", template_ref="another-template-ref")
    del inputs["version"]
    inputs.update(override)
    code, transport = run_upload(inputs=inputs, GITHUB_SHA=sha)
    assert code == 0
    assert transport.call_count == 4
    batch_url = f"{BASE_URL}/{CALLER_REPOSITORY}/{encoded}/"
    for index, name in enumerate(("a.txt", "b.txt")):
        upload_url = _last_curl_flags(_curl_args(transport)[index * 2])[0]
        storage_url = _last_curl_flags(_curl_args(transport)[index * 2 + 1])[0]
        assert upload_url == batch_url + name
        assert storage_url.partition("?")[0] == (
            "https://download.t-head.cn/artifactory/api/storage/github_generic/nightly/"
            f"{CALLER_REPOSITORY}/{encoded}/{name}"
        )
        assert _query_pairs(storage_url) == dict(
            expected_property_values(), version=expected, package_version=package_version,
            sdk_version=sdk_version, commit=sha,
        )
        assert f";version={encoded};" in storage_url
    assert (workspace / "outputs").read_text() == (
        f"batch_url={batch_url}\nupload_urls<<UPLOAD_URLS_EOF\n"
        f"{batch_url}a.txt\n{batch_url}b.txt\nUPLOAD_URLS_EOF\n"
    )


@pytest.mark.parametrize("key", ["package_version", "sdk_version"])
@pytest.mark.parametrize("value", [None, "", " ", "../other", "v1/other", "v1?x=1", 123, "v" * 250])
def test_invalid_default_version_components_stop_before_upload(key, value, make_file, run_upload, workspace):
    make_file()
    code, transport = run_upload(inputs=dict(DEFAULT_INPUTS, version="", **{key: value}))
    assert code == 1
    transport.assert_not_called()
    assert not (workspace / "outputs").exists()


@pytest.mark.parametrize("key", ["package_version", "sdk_version"])
def test_missing_default_version_component_stops_before_upload(key, make_file, run_upload):
    make_file()
    inputs = dict(DEFAULT_INPUTS, version="")
    del inputs[key]
    code, transport = run_upload(inputs=inputs)
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("sha", ["", "123456", "not-hex", "abcdef1/other", "abcdef1\n"])
def test_invalid_default_commit_stops_before_upload(sha, make_file, run_upload, workspace):
    make_file()
    code, transport = run_upload(inputs=dict(DEFAULT_INPUTS, version=""), GITHUB_SHA=sha)
    assert code == 1
    transport.assert_not_called()
    assert not (workspace / "outputs").exists()


def test_explicit_version_does_not_require_fallback_components(uploader):
    assert uploader.resolve_version({"version": "custom"}, {}) == "custom"


def test_property_query_matches_artifactory_wire_format(uploader):
    assert uploader.build_property_query(DEFAULT_INPUTS, DEFAULT_CONTEXT_ENV) == (
        "properties=package_type=raw;package_name=demo_package;package_version=1.2.3;version=1.2.3;"
        "os=ubuntu24.04;python_version=3.12;runtime_type=sail;runtime=13.0.0;torch_version=;"
        "sdk_version=2.2.0;package_edition=standard;branch=main;tag=;"
        "commit=abcdef1234567890;log_url=https%3A%2F%2Fexample.invalid%2Flogs%2F100;"
        "run_id=100;run_attempt=1;extra_info="
    )


@pytest.mark.parametrize("value,encoded", [
    ("1=1", "1%5C%3D1"),
    ("a,b", "a%5C%2Cb"),
    ("a|b", "a%5C%7Cb"),
    ("C:\\build\\", "C%3A%5C%5Cbuild%5C%5C"),
    ("a;b", "a%3Bb"),
    ("a&recursive=1#x%+", "a%26recursive%5C%3D1%23x%25%2B"),
    ('{"note":"a,b;c=d"}', "%7B%22note%22%3A%22a%5C%2Cb%3Bc%5C%3Dd%22%7D"),
])
def test_property_values_are_escaped_and_percent_encoded(value, encoded, make_file, run_upload):
    make_file()
    code, transport = run_upload(inputs=dict(DEFAULT_INPUTS, extra_info=value))
    assert code == 0
    storage_url = _last_curl_flags(_curl_args(transport)[1])[0]
    assert storage_url.endswith(f";extra_info={encoded}&recursive=0")
    assert _query_pairs(storage_url)["extra_info"] == value


def test_reported_run_targets_the_existing_artifact(make_file, run_upload):
    make_file()
    code, transport = run_upload(
        inputs=dict(DEFAULT_INPUTS, version="test0001"),
        GITHUB_RUN_ID="42607174840",
    )
    assert code == 0
    upload_url = _last_curl_flags(_curl_args(transport)[0])[0]
    storage_url = _last_curl_flags(_curl_args(transport)[1])[0]
    assert upload_url == (
        "https://download.t-head.cn/artifactory/github_generic/nightly/caller-org/demo-repo/test0001/demo.txt"
    )
    assert storage_url.partition("?")[0] == (
        "https://download.t-head.cn/artifactory/api/storage/github_generic/nightly/"
        "caller-org/demo-repo/test0001/demo.txt"
    )
    assert "github_generic/api/storage/" not in storage_url
    assert "github_generic/nightly/api/storage/" not in storage_url
    assert _query_pairs(storage_url) == dict(
        expected_property_values(), version="test0001", run_id="42607174840",
    )


@pytest.mark.parametrize("repository", ["another-org/My_Repo.v2", "alice/.github", "team/_tools"])
@pytest.mark.parametrize("run_id", ["100", "200"])
@pytest.mark.parametrize("attempt", ["1", "2"])
def test_run_metadata_changes_properties_without_changing_paths(
    repository, run_id, attempt, make_file, run_upload, workspace,
):
    make_file()
    code, transport = run_upload(
        inputs=dict(DEFAULT_INPUTS, template_repository="template-org/template-tools"),
        GITHUB_REPOSITORY=repository,
        GITHUB_RUN_ID=run_id,
        GITHUB_RUN_ATTEMPT=attempt,
    )
    assert code == 0
    prefix = f"{repository}/1.2.3/"
    upload_url = _last_curl_flags(_curl_args(transport)[0])[0]
    storage_url = _last_curl_flags(_curl_args(transport)[1])[0]
    assert upload_url == f"{BASE_URL}/{prefix}demo.txt"
    assert storage_url.partition("?")[0] == (
        f"https://download.t-head.cn/artifactory/api/storage/github_generic/nightly/{prefix}demo.txt"
    )
    assert _query_pairs(storage_url) == dict(
        expected_property_values(), run_id=run_id, run_attempt=attempt,
    )
    assert (workspace / "outputs").read_text() == (
        f"batch_url={BASE_URL}/{prefix}\nupload_urls<<UPLOAD_URLS_EOF\n"
        f"{upload_url}\nUPLOAD_URLS_EOF\n"
    )


@pytest.mark.parametrize("repository", [
    "", "owner", "owner/", "/repo", "owner/repo/extra", "../repo", "owner/.", "owner/..",
    "owner/%2e%2e", "owner/repo?x=1", "owner/repo#frag", "owner/repo name", "owner/repo\\name",
])
def test_invalid_caller_repository_stops_before_upload(repository, make_file, run_upload, workspace, capsys):
    make_file()
    code, transport = run_upload(GITHUB_REPOSITORY=repository)
    assert code == 1
    transport.assert_not_called()
    assert not (workspace / "outputs").exists()
    assert "GITHUB_REPOSITORY" in capsys.readouterr().err


@pytest.mark.parametrize("filename,encoded", [
    ("demo+cu130.txt", "demo%2Bcu130.txt"),
    ("a b#%.txt", "a%20b%23%25.txt"),
    ("$(touch marker).txt", "%24%28touch%20marker%29.txt"),
])
def test_filenames_are_percent_encoded_without_shell_execution(filename, encoded, make_file, run_upload, workspace):
    make_file(filename=filename)
    code, transport = run_upload()
    assert code == 0
    args = _curl_args(transport)
    upload_url = _last_curl_flags(args[0])[0]
    storage_url = _last_curl_flags(args[1])[0]
    assert upload_url == UPLOAD_DIR + encoded
    assert storage_url.startswith(STORAGE_DIR + encoded + "?properties=")
    assert not transport.call_args.kwargs.get("shell", False)
    assert not (workspace / "marker").exists()


def test_multiple_files_upload_sequentially(make_file, run_upload):
    paths = [make_file(filename=name) for name in ("c.txt", "a.txt", "b.txt")]
    code, transport = run_upload()
    assert code == 0
    assert transport.call_count == 6
    args = _curl_args(transport)
    for index, path in enumerate(sorted(paths)):
        upload_url, upload_source = _last_curl_flags(args[index * 2])
        storage_url, _ = _last_curl_flags(args[index * 2 + 1])
        assert upload_url == UPLOAD_DIR + path.name
        assert upload_source == str(path)
        assert storage_url.startswith(STORAGE_DIR + path.name + "?properties=")
        assert _query_pairs(storage_url) == expected_property_values()


def test_duplicate_basenames_both_upload_to_the_same_destination(make_file, run_upload):
    first = make_file(filename=DEMO_FILENAME, directory="artifact/one", content=b"first")
    second = make_file(filename=DEMO_FILENAME, directory="artifact/two", content=b"second")
    code, transport = run_upload()
    assert code == 0
    args = _curl_args(transport)
    upload_urls = [_last_curl_flags(a)[0] for a in args[0::2]]
    upload_sources = [_last_curl_flags(a)[1] for a in args[0::2]]
    assert set(upload_urls) == {UPLOAD_DIR + DEMO_FILENAME}
    assert sorted(upload_sources) == sorted([str(first), str(second)])


@pytest.mark.parametrize("key,value", [
    ("version", "two/three"),
    ("version", " "),
    ("version", "$(touch marker)"),
    ("version", "../other"),
    ("version", "v1?x=1"),
    ("version", "v1;tag=other"),
    ("version", "v" * 256),
    ("version", 123),
    ("version", False),
    ("version", []),
])
def test_invalid_url_inputs_rejected_before_any_request(key, value, make_file, run_upload, workspace):
    make_file()
    code, transport = run_upload(inputs=dict(DEFAULT_INPUTS, **{key: value}))
    assert code == 1
    transport.assert_not_called()
    assert not (workspace / "marker").exists()


def test_upload_destination_cannot_be_overridden_by_inputs(make_file, run_upload):
    # The service and repository are script constants, not caller inputs.
    make_file()
    code, transport = run_upload(inputs=dict(
        DEFAULT_INPUTS, artifact_base_url="https://attacker.invalid", repository_id="other_repo",
    ))
    assert code == 0
    upload_url = _last_curl_flags(_curl_args(transport)[0])[0]
    storage_url = _last_curl_flags(_curl_args(transport)[1])[0]
    assert upload_url == UPLOAD_DIR + DEMO_FILENAME
    assert storage_url.partition("?")[0] == STORAGE_DIR + DEMO_FILENAME


@pytest.mark.parametrize("key", [
    "package_type", "package_name", "package_version", "os", "python_version",
    "runtime", "sdk_version", "package_edition",
])
def test_missing_required_input_property_stops_before_upload(key, make_file, run_upload):
    make_file()
    inputs = dict(DEFAULT_INPUTS)
    del inputs[key]
    code, transport = run_upload(inputs=inputs)
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("key", ["commit", "log_url", "run_id", "run_attempt"])
def test_missing_required_context_property_stops_before_upload(key, make_file, run_upload):
    make_file()
    code, transport = run_upload(**{CONTEXT_PROPERTY_ENV[key]: ""})
    assert code == 1
    transport.assert_not_called()


def test_context_derived_inputs_are_ignored_even_when_present(make_file, run_upload):
    # Callers must not be able to spoof context-derived properties, including run_id and run_attempt, through INPUTS_JSON.
    make_file()
    inputs = dict(
        DEFAULT_INPUTS, branch="evil", tag="evil", commit="evil", log_url="https://evil.invalid",
        run_id="999999", run_attempt="999",
    )
    code, transport = run_upload(inputs=inputs)
    assert code == 0
    storage_url = _last_curl_flags(_curl_args(transport)[1])[0]
    pairs = _query_pairs(storage_url)
    expected = expected_property_values()
    for key in CONTEXT_SOURCED_PROPERTIES:
        assert pairs[key] == expected[key], key


@pytest.mark.parametrize("key", ["run_id", "run_attempt"])
@pytest.mark.parametrize("value", ["", "0", "-1", "abc", "1.5", "01", " 1", "1\n", "1;tag=evil"])
def test_invalid_run_metadata_stops_before_any_request(key, value, make_file, run_upload, workspace):
    make_file()
    code, transport = run_upload(**{CONTEXT_PROPERTY_ENV[key]: value})
    assert code == 1
    transport.assert_not_called()
    assert not (workspace / "outputs").exists()


@pytest.mark.parametrize("key", ["run_id", "run_attempt"])
def test_run_properties_require_context_even_with_input_fallback(key, uploader):
    env = dict(DEFAULT_CONTEXT_ENV)
    del env[CONTEXT_PROPERTY_ENV[key]]
    with pytest.raises(ValueError, match=f"{key} must not be empty"):
        uploader.build_property_query(dict(DEFAULT_INPUTS, **{key: "123"}), env)


@pytest.mark.parametrize("environment", [
    {"INPUTS_JSON": "{"}, {"INPUTS_JSON": "[]"},
    {"ARTIFACT_DIR": ""},
])
def test_invalid_environment_or_json_stops_before_upload(environment, make_file, run_upload):
    make_file()
    code, transport = run_upload(**environment)
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("kind", ["missing-dir", "empty-dir", "only-subdirs", "symlink", "root-file", "root-symlink"])
def test_missing_artifact_or_no_regular_files_fails(kind, workspace, make_file, run_upload):
    root = workspace / "artifact"
    if kind == "root-file":
        root.write_bytes(b"not a directory")
    elif kind == "root-symlink":
        make_file(directory="outside")
        root.symlink_to(workspace / "outside", target_is_directory=True)
    elif kind != "missing-dir":
        root.mkdir()
        if kind == "only-subdirs":
            (root / "sub").mkdir()
        elif kind == "symlink":
            original = make_file(directory="outside")
            (root / DEMO_FILENAME).symlink_to(original)
    code, transport = run_upload()
    assert code == 1
    transport.assert_not_called()
    assert not (workspace / "outputs").exists()


def test_directory_symlinks_are_not_followed(workspace, make_file, run_upload):
    make_file(directory="outside")
    (workspace / "artifact").mkdir()
    (workspace / "artifact/link").symlink_to(workspace / "outside", target_is_directory=True)
    code, transport = run_upload()
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("key,value", [
    ("RELEASE_USERNAME", ""), ("RELEASE_TOKEN", ""),
    ("RELEASE_USERNAME", "user:bad"), ("RELEASE_TOKEN", "bad\ntoken"),
])
def test_missing_or_invalid_credentials_never_upload(key, value, make_file, run_upload):
    make_file()
    code, transport = run_upload(**{key: value})
    assert code == 1
    transport.assert_not_called()


def test_upload_failure_stops_before_setting_properties(make_file, run_upload, uploader, workspace):
    make_file()
    uploader.subprocess.run.side_effect = [subprocess.CompletedProcess([], 22, "500", "")]
    code, transport = run_upload()
    assert code == 1
    assert transport.call_count == 1
    assert not (workspace / "outputs").exists()


def test_property_failure_stops_batch_without_success_output(make_file, run_upload, uploader, workspace):
    for name in ("a.txt", "b.txt"):
        make_file(filename=name)
    uploader.subprocess.run.side_effect = [
        subprocess.CompletedProcess([], 0, "201", ""),   # upload a succeeds
        subprocess.CompletedProcess([], 22, "500", ""),  # set properties a fails
    ]
    code, transport = run_upload()
    assert code == 1
    assert transport.call_count == 2
    assert not (workspace / "outputs").exists()


@pytest.mark.parametrize("status", ["200", "201", "202", "301", "400", "401", "403", "404", "500"])
def test_property_response_requires_204(status, make_file, run_upload, uploader, workspace, capsys):
    make_file(filename="a.txt")
    make_file(filename="b.txt")
    uploader.subprocess.run.side_effect = [
        subprocess.CompletedProcess([], 0, "201", ""),
        subprocess.CompletedProcess([], 0, status, ""),
        subprocess.CompletedProcess([], 0, "201", ""),
        subprocess.CompletedProcess([], 0, "204", ""),
    ]
    code, transport = run_upload()
    assert code == 1
    assert transport.call_count == 2
    assert not (workspace / "outputs").exists()
    captured = capsys.readouterr()
    assert f"HTTP {status}" in captured.err
    assert "Uploaded:" not in captured.out
    assert "test-token" not in captured.out + captured.err


def test_timeout_is_reported_without_leaking_credentials(make_file, run_upload, uploader, capsys):
    make_file()
    uploader.subprocess.run.side_effect = subprocess.TimeoutExpired(["curl", "test-token"], 1500)
    assert run_upload()[0] == 1
    captured = capsys.readouterr()
    assert "timed out" in captured.err
    assert "test-token" not in captured.out + captured.err


def test_workflow_contract_and_step_order():
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_call"}
    contract = workflow["on"]["workflow_call"]
    expected_inputs = {
        "artifact_name", "version",
        *INPUT_SOURCED_PROPERTIES, "template_repository", "template_ref",
    }
    assert set(contract["inputs"]) == expected_inputs
    for name in ("artifact_name", "template_ref"):
        assert contract["inputs"][name]["required"] == "true", name
    assert contract["inputs"]["version"]["required"] == "false"
    assert contract["inputs"]["version"]["default"] == ""
    assert contract["inputs"]["runtime_type"]["required"] == "false"
    assert contract["inputs"]["runtime_type"]["type"] == "string"
    assert contract["inputs"]["runtime_type"]["default"] == "sail"
    for name in INPUT_SOURCED_PROPERTIES:
        if name not in ("extra_info", "runtime_type"):
            assert contract["inputs"][name]["required"] == "true", name
    for name in CONTEXT_SOURCED_PROPERTIES:
        assert name not in contract["inputs"], name
    assert "artifact_base_url" not in contract["inputs"]
    assert contract["inputs"]["extra_info"]["default"] == ""
    assert contract["inputs"]["template_repository"]["default"] == "t-head/ci-infra"
    assert set(contract["secrets"]) == {"template_token", "release_username", "release_token"}
    for name in ("release_username", "release_token"):
        assert contract["secrets"][name]["required"] == "true"
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["upload-artifact"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == "30"
    guard, checkout, setup, download, upload = job["steps"]
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"] == {
        "repository": "${{ inputs.template_repository }}", "ref": "${{ inputs.template_ref }}",
        "path": "ci-template", "token": "${{ secrets.template_token || github.token }}",
        "persist-credentials": "false",
    }
    assert setup["with"]["python-version"] == "3.12"
    assert download["uses"] == "actions/download-artifact@v8"
    assert download["with"] == {
        "name": "${{ inputs.artifact_name }}",
        "path": "${{ runner.temp }}/upload-artifact-properties-source",
        "skip-decompress": "true",
    }
    assert "run" not in download and "env" not in download
    assert upload["env"]["ARTIFACT_DIR"] == download["with"]["path"]
    assert "if" not in upload
    assert "--download" not in SCRIPT.read_text()
    assert "def download_artifact" not in SCRIPT.read_text()
    assert upload["env"]["GITHUB_RUN_ID"] == "${{ github.run_id }}"
    assert upload["env"]["GITHUB_RUN_ATTEMPT"] == "${{ github.run_attempt }}"
    assert upload["env"]["GITHUB_REPOSITORY"] == "${{ github.repository }}"
    assert upload["env"]["GITHUB_BRANCH"] == "${{ github.ref_type == 'branch' && github.ref_name || '' }}"
    assert upload["env"]["GITHUB_TAG"] == "${{ github.ref_type == 'tag' && github.ref_name || '' }}"
    assert upload["env"]["GITHUB_SHA"] == "${{ github.sha }}"
    assert upload["env"]["GITHUB_LOG_URL"] == (
        "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}"
    )
    assert upload["env"]["INPUTS_JSON"] == "${{ toJSON(inputs) }}"
    assert upload["env"]["RELEASE_USERNAME"] == "${{ secrets.release_username }}"
    assert upload["env"]["RELEASE_TOKEN"] == "${{ secrets.release_token }}"
    assert upload["run"] == (
        "python ci-template/ci-templates/upload-artifact-properties/scripts/upload_artifact.py"
    )
    assert job["outputs"]["batch_url"] == "${{ steps.upload.outputs.batch_url }}"
    assert job["outputs"]["upload_urls"] == "${{ steps.upload.outputs.upload_urls }}"
    for key in ("batch_url", "upload_urls"):
        assert contract["outputs"][key]["value"] == "${{ jobs.upload-artifact.outputs." + key + " }}"
    assert "env" not in job
    for step in job["steps"]:
        assert "continue-on-error" not in step
        assert "${{" not in step.get("run", "")
        if step is not upload:
            assert "RELEASE_TOKEN" not in str(step.get("env", {}))
        assert "GH_TOKEN" not in step.get("env", {})
        assert "GH_ENTERPRISE_TOKEN" not in step.get("env", {})
        assert "GITHUB_API_URL" not in step.get("env", {})
    assert guard["env"] == {"ARTIFACT_NAME": "${{ inputs.artifact_name }}"}
    for version in (None, "", "1.0.0"):
        env = dict(os.environ, ARTIFACT_NAME="x")
        env.pop("VERSION", None)
        if version is not None:
            env["VERSION"] = version
        passed = subprocess.run(["bash", "-c", guard["run"]], env=env,
                                capture_output=True, text=True, timeout=5)
        assert passed.returncode == 0, passed.stderr
    for artifact_name in ("", " "):
        failed = subprocess.run(["bash", "-c", guard["run"]],
                                env=dict(os.environ, ARTIFACT_NAME=artifact_name),
                                capture_output=True, text=True, timeout=5)
        assert failed.returncode == 1
        assert "artifact_name" in failed.stderr


def test_demo_workflow_matches_template_contract():
    template = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    demo = yaml.load(DEMO.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(demo["on"]) == {"workflow_dispatch"}
    assert demo["on"]["workflow_dispatch"]["inputs"]["version"]["required"] == "false"
    assert demo["on"]["workflow_dispatch"]["inputs"]["version"]["default"] == ""
    assert demo["jobs"]["publish"]["with"]["version"] == "${{ inputs.version }}"
    assert demo["on"]["workflow_dispatch"]["inputs"]["runtime_type"]["required"] == "false"
    assert demo["on"]["workflow_dispatch"]["inputs"]["runtime_type"]["default"] == "sail"
    assert demo["jobs"]["publish"]["with"]["runtime_type"] == "${{ inputs.runtime_type }}"
    assert demo["permissions"] == {"contents": "read"}
    build, publish, result = (demo["jobs"][name] for name in ("build", "publish", "result"))
    assert publish["needs"] == "build"
    assert "runs-on" not in publish and "steps" not in publish
    assert publish["uses"] == (
        publish["with"]["template_repository"] + "/.github/workflows/upload-artifact-properties.yml@"
        + publish["with"]["template_ref"]
    )
    contract = template["on"]["workflow_call"]
    assert set(publish["with"]) == set(contract["inputs"])
    required_secrets = {key for key, value in contract["secrets"].items() if value["required"] == "true"}
    assert required_secrets <= set(publish["secrets"]) <= set(contract["secrets"])
    assert publish["secrets"] == {
        "release_username": "${{ secrets.RELEASE_USERNAME }}",
        "release_token": "${{ secrets.RELEASE_TOKEN }}",
    }
    artifact = next(step for step in build["steps"] if step.get("uses") == "actions/upload-artifact@v4")
    assert artifact["with"]["name"] == build["outputs"]["artifact_name"]
    assert "${{ github.run_id }}" in artifact["with"]["name"]
    assert "${{ github.run_attempt }}" in artifact["with"]["name"]
    assert publish["with"]["artifact_name"] == "${{ needs.build.outputs.artifact_name }}"
    assert artifact["with"]["if-no-files-found"] == "error"
    for key in CONTEXT_SOURCED_PROPERTIES:
        assert key not in publish["with"], key
    assert result["needs"] == "publish"
    step, = result["steps"]
    assert step["env"]["BATCH_URL"] == "${{ needs.publish.outputs.batch_url }}"
    assert step["env"]["UPLOAD_URLS"] == "${{ needs.publish.outputs.upload_urls }}"
    for job in (build, result):
        assert "secrets" not in job
        for step in job["steps"]:
            if "run" in step:
                assert "${{" not in step["run"]
                checked = subprocess.run(["bash", "-n"], input=step["run"], capture_output=True,
                                         text=True, timeout=5)
                assert checked.returncode == 0, checked.stderr


def test_registered_demo_grants_required_actions_permissions():
    demo = yaml.load((WORKFLOW.parent / "demo-upload-artifact-properties.yml").read_text(), Loader=yaml.BaseLoader)
    assert demo["permissions"] == {"contents": "read"}


def test_readme_documents_interface_and_local_links():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    contract = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["on"]["workflow_call"]
    for section in ("inputs", "secrets", "outputs"):
        for name in contract[section]:
            assert f"| `{name}` |" in text, name
    for relative in ("../../.github/workflows/upload-artifact-properties.yml",
                     "scripts/upload_artifact.py",
                     "workflows/demo-upload-artifact-properties.yml"):
        assert f"]({relative})" in text
        assert (ROOT / relative).is_file()
    assert (
        "https://download.t-head.cn/artifactory/github_generic/nightly/"
        "{owner}/{repository}/{version}/{filename}"
    ) in text
    assert (
        "/api/storage/github_generic/nightly/{owner}/{repository}/{version}/{filename}"
    ) in text
    assert "{run_id}_{run_attempt}" not in text
    assert "GITHUB_REPOSITORY" in text
    assert "{repository_id}" not in text and "{artifact_base_url}" not in text
    assert "/api/storage" in text and "properties=" in text
    assert "| `version` | No |" in text
    assert "{package_version}+{short_commit}.ppu{sdk_version}" in text
    assert "1.2.3+abcdef1.ppu2.2.0" in text
    assert "{artifact_name}.zip" in text
    assert "contents: read" in text
    assert "actions/download-artifact@v8" in text
    assert "skip-decompress: true" in text
    assert "gh api" not in text
    assert "actions/download-artifact@v4" not in text


def test_readme_documents_context_derived_property_source():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in CONTEXT_SOURCED_PROPERTIES:
        assert f"| `{name}` |" in text, name
    for env_name in CONTEXT_PROPERTY_ENV.values():
        assert env_name in text, env_name
    assert "github.ref_type" in text and "github.sha" in text and "github.server_url" in text
    for name in CONTEXT_SOURCED_PROPERTIES:
        assert name not in contract_input_names(), name


def contract_input_names():
    contract = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["on"]["workflow_call"]
    return set(contract["inputs"])


def test_cli_runs_without_third_party_packages_or_network(workspace):
    result = subprocess.run([sys.executable, "-S", str(SCRIPT)],
                            env=dict(os.environ, ARTIFACT_DIR=str(workspace / "missing"),
                                     INPUTS_JSON="{}",
                                     GITHUB_RUN_ID=RUN_ID, GITHUB_RUN_ATTEMPT=RUN_ATTEMPT,
                                     RELEASE_USERNAME="u", RELEASE_TOKEN="t"),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "::error::" in result.stderr
