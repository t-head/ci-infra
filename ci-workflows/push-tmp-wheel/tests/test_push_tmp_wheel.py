"""Offline tests for temporary wheel publishing; uploads are mocked without accessing a real Artifactory instance."""

from datetime import datetime, timedelta, timezone
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import Mock
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/push_tmp_wheel.py"
WORKFLOW = ROOT.parents[1] / ".github/workflows/push-tmp-wheel.yml"
WHEEL_NAME = "demo_package-1.2.3-py3-none-any.whl"
BATCH_URL = "https://download.t-head.cn/artifactory/pypi_tmp/2026-11-02-1211/"


@pytest.fixture
def publisher():
    spec = importlib.util.spec_from_file_location("push_tmp_wheel", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory(prefix=".wheel-test-", dir=ROOT) as directory:
        yield Path(directory).resolve()


@pytest.fixture
def make_wheel(workspace):
    def make(filename=WHEEL_NAME, directory="artifact", content=b"not a wheel ZIP"):
        path = workspace / directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path
    return make


@pytest.fixture
def run_publish(publisher, workspace, monkeypatch):
    monkeypatch.setenv("ARTIFACT_DIR", str(workspace / "artifact"))
    monkeypatch.delenv("PACKAGE_NAME", raising=False)
    monkeypatch.delenv("WHEEL_NAME", raising=False)
    clock = Mock()
    clock.now.return_value = datetime(2026, 11, 2, 12, 11, 59, tzinfo=timezone(timedelta(hours=8)))
    monkeypatch.setattr(publisher, "datetime", clock)
    monkeypatch.setenv("RELEASE_USERNAME", "test-user")
    monkeypatch.setenv("RELEASE_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_OUTPUT", str(workspace / "outputs"))
    transport = Mock(return_value=subprocess.CompletedProcess([], 0, "\n201", ""))
    monkeypatch.setattr(publisher.subprocess, "run", transport)

    def run(**environment):
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        return publisher.main(), transport
    return run


def test_upload_is_exact_and_https_only(make_wheel, run_publish, workspace):
    path = make_wheel(directory="artifact/nested/build output")
    before = path.read_bytes()
    code, transport = run_publish()
    assert code == 0
    transport.assert_called_once()
    args = transport.call_args.args[0]
    assert args[:2] == ["curl", "--disable"]
    assert args[args.index("--upload-file") + 1] == str(path)
    assert args[args.index("--user") + 1] == "test-user:test-token"
    assert args[args.index("--proto") + 1] == "=https"
    assert args[args.index("--retry") + 1] == "3"
    assert "--fail-with-body" in args
    assert "--fail" not in args
    assert "--output" not in args
    assert args[args.index("--write-out") + 1] == "\n%{http_code}"
    assert "--location" not in args
    assert "--insecure" not in args
    assert args[-2:] == ["--", BATCH_URL + WHEEL_NAME]
    assert path.read_bytes() == before
    assert (workspace / "outputs").read_text() == f"upload_url={BATCH_URL}\n"


def test_all_nested_wheels_share_one_timestamp_across_minutes(make_wheel, run_publish, publisher, workspace):
    paths = [make_wheel(filename="b.whl"), make_wheel(filename="a.whl", directory="artifact/nested")]
    publisher.datetime.now.side_effect = [
        datetime(2026, 11, 2, 12, 11, 59, tzinfo=timezone(timedelta(hours=8))),
        datetime(2026, 11, 2, 12, 12, 1, tzinfo=timezone(timedelta(hours=8))),
    ]
    code, transport = run_publish()
    assert code == 0
    publisher.datetime.now.assert_called_once_with(ZoneInfo("Asia/Shanghai"))
    assert transport.call_count == 2
    for call, path in zip(transport.call_args_list, sorted(paths)):
        args = call.args[0]
        assert args[args.index("--upload-file") + 1] == str(path)
        assert args[-1] == BATCH_URL + path.name
    assert (workspace / "outputs").read_text() == f"upload_url={BATCH_URL}\n"


def test_timestamp_is_zero_padded_and_uses_24_hour_time(make_wheel, run_publish, publisher):
    make_wheel()
    publisher.datetime.now.return_value = datetime(2026, 1, 2, 23, 4, 59, tzinfo=timezone(timedelta(hours=8)))
    code, transport = run_publish()
    assert code == 0
    assert transport.call_args.args[0][-1].endswith("/2026-01-02-2304/" + WHEEL_NAME)


def test_only_exact_whl_suffix_files_are_uploaded(make_wheel, run_publish, workspace):
    make_wheel(filename=".hidden.whl", directory="artifact/.hidden")
    for filename in ("notes.txt", "archive.zip", "source.tar.gz", "upper.WHL", "wheel.whl.bak"):
        make_wheel(filename=filename)
    (workspace / "artifact/directory.whl").mkdir()
    code, transport = run_publish()
    assert code == 0
    transport.assert_called_once()
    assert transport.call_args.args[0][-1] == BATCH_URL + ".hidden.whl"


@pytest.mark.parametrize("content", [b"", b"not a ZIP", b"PK\x03\x04broken ZIP"])
def test_empty_or_invalid_wheel_contents_are_uploaded_unchanged(content, make_wheel, run_publish):
    path = make_wheel(filename="arbitrary-name.whl", content=content)
    code, transport = run_publish()
    assert code == 0
    transport.assert_called_once()
    assert path.read_bytes() == content


def test_wheel_archive_is_neither_validated_nor_extracted(make_wheel, run_publish, workspace):
    path = make_wheel()
    with ZipFile(path, "w") as archive:
        archive.writestr("../outside.txt", "not extracted")
        archive.writestr("wrong.dist-info/METADATA", "invalid metadata")
        archive.writestr("wrong.dist-info/RECORD", "invalid hashes")
    before = path.read_bytes()
    code, transport = run_publish()
    assert code == 0
    transport.assert_called_once()
    assert path.read_bytes() == before
    assert not (workspace / "outside.txt").exists()
    assert not (path.parent / "wrong.dist-info").exists()


@pytest.mark.parametrize("filename,encoded", [
    ("demo+cu130.whl", "demo%2Bcu130.whl"),
    ("a b#%.whl", "a%20b%23%25.whl"),
    ("*.whl", "%2A.whl"),
    ("line\nbreak.whl", "line%0Abreak.whl"),
    ("$(touch marker).whl", "%24%28touch%20marker%29.whl"),
])
def test_arbitrary_filenames_are_encoded_without_shell_execution(filename, encoded, make_wheel, run_publish, workspace):
    make_wheel(filename=filename)
    code, transport = run_publish()
    assert code == 0
    assert transport.call_args.args[0][-1] == BATCH_URL + encoded
    assert not transport.call_args.kwargs.get("shell", False)
    assert not (workspace / "marker").exists()


def test_duplicate_basenames_are_all_uploaded_to_the_same_destination(make_wheel, run_publish):
    first = make_wheel(directory="artifact/one", content=b"first")
    second = make_wheel(directory="artifact/two", content=b"second")
    code, transport = run_publish()
    assert code == 0
    assert transport.call_count == 2
    for call, path in zip(transport.call_args_list, [first, second]):
        args = call.args[0]
        assert args[args.index("--upload-file") + 1] == str(path)
        assert args[-1] == BATCH_URL + WHEEL_NAME


@pytest.mark.parametrize("kind", ["missing-dir", "empty-dir", "no-wheels", "directory", "symlink", "root-file", "root-symlink"])
def test_missing_artifact_or_no_regular_wheels_fails(kind, workspace, make_wheel, run_publish):
    root = workspace / "artifact"
    if kind == "root-file":
        root.write_bytes(b"not a directory")
    elif kind == "root-symlink":
        make_wheel(directory="outside")
        root.symlink_to(workspace / "outside", target_is_directory=True)
    elif kind != "missing-dir":
        root.mkdir()
        if kind == "no-wheels":
            (root / "notes.txt").write_text("unrelated")
        elif kind == "directory":
            (root / "directory.whl").mkdir()
        elif kind == "symlink":
            original = make_wheel(directory="outside")
            (root / WHEEL_NAME).symlink_to(original)
    code, transport = run_publish()
    assert code == 1
    transport.assert_not_called()
    assert not (workspace / "outputs").exists()


def test_directory_symlinks_are_not_followed(workspace, make_wheel, run_publish):
    make_wheel(directory="outside")
    (workspace / "artifact").mkdir()
    (workspace / "artifact/link").symlink_to(workspace / "outside", target_is_directory=True)
    code, transport = run_publish()
    assert code == 1
    transport.assert_not_called()


def test_empty_artifact_environment_fails_without_upload(run_publish):
    code, transport = run_publish(ARTIFACT_DIR="")
    assert code == 1
    transport.assert_not_called()


def test_scan_errors_are_not_silently_ignored(make_wheel, run_publish, publisher, monkeypatch):
    make_wheel()

    def fail_walk(root, *, onerror, followlinks):
        onerror(PermissionError("Cannot read artifact directory"))

    monkeypatch.setattr(publisher.os, "walk", fail_walk)
    code, transport = run_publish()
    assert code == 1
    transport.assert_not_called()


def test_partial_failure_stops_batch_without_success_output(make_wheel, run_publish, publisher, workspace):
    for filename in ("a.whl", "b.whl", "c.whl"):
        make_wheel(filename=filename)
    publisher.subprocess.run.side_effect = [
        subprocess.CompletedProcess([], 0, "\n201", ""),
        subprocess.CompletedProcess([], 22, "\n500", ""),
    ]
    code, transport = run_publish()
    assert code == 1
    assert transport.call_count == 2
    assert not (workspace / "outputs").exists()


def test_success_without_github_output(make_wheel, run_publish, capsys):
    make_wheel()
    code, transport = run_publish(GITHUB_OUTPUT="")
    assert code == 0
    transport.assert_called_once()
    assert BATCH_URL in capsys.readouterr().out


@pytest.mark.parametrize("key,value", [("RELEASE_USERNAME", ""), ("RELEASE_TOKEN", ""),
                                       ("RELEASE_USERNAME", "user:bad"), ("RELEASE_TOKEN", "bad\ntoken")])
def test_missing_or_invalid_credentials_never_upload(key, value, make_wheel, run_publish):
    make_wheel()
    code, transport = run_publish(**{key: value})
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("status,returncode", [("302", 0), ("401", 22), ("500", 22), ("000", 7), ("not-http", 0)])
def test_http_or_network_failure_fails_without_outputs(status, returncode, make_wheel, run_publish,
                                                     publisher, workspace, capsys):
    make_wheel()
    publisher.subprocess.run.return_value = subprocess.CompletedProcess([], returncode, f"\n{status}", "test-token")
    code, transport = run_publish()
    assert code == 1
    transport.assert_called_once()
    assert not (workspace / "outputs").exists()
    captured = capsys.readouterr()
    assert "test-token" not in captured.out + captured.err


@pytest.mark.parametrize("status,returncode,body,stderr", [
    ("201", 0, '{\n  "repo": "pypi_tmp",\n  "checksums": {"sha256": "abc123"}\n}\n', ""),
    ("400", 22, '{"errors": [{"status": 400, "message": "Invalid upload"}]}', "curl: (22) HTTP 400"),
    ("502", 22, "<html>Bad Gateway</html>", "curl: (22) HTTP 502"),
    ("302", 0, "Moved temporarily", ""),
    ("204", 0, "", ""),
    ("000", 7, "", "curl: (7) Failed to connect"),
])
def test_upload_logs_detailed_response(status, returncode, body, stderr, make_wheel, run_publish,
                                      publisher, workspace, capsys):
    make_wheel()
    publisher.subprocess.run.return_value = subprocess.CompletedProcess([], returncode, f"{body}\n{status}", stderr)
    code, _ = run_publish()
    succeeded = returncode == 0 and status.startswith("2")
    assert code == (0 if succeeded else 1)
    captured = capsys.readouterr()
    assert f"Artifactory response for {BATCH_URL}{WHEEL_NAME}" in captured.out
    assert f"curl exit code {returncode}, HTTP {status}" in captured.out
    assert "Response body:" in captured.out
    for line in (body or "(empty)").splitlines():
        assert f"  {line}\n" in captured.out
    if stderr:
        assert f"curl stderr:\n  {stderr}" in captured.out
    else:
        assert "curl stderr:" not in captured.out
    if succeeded:
        assert (workspace / "outputs").exists()
    else:
        assert not (workspace / "outputs").exists()
        assert "Artifactory upload failed" in captured.err
        assert "Uploaded:" not in captured.out


@pytest.mark.parametrize("status,returncode", [("201", 0), ("401", 22)])
def test_response_body_and_stderr_redact_credentials(status, returncode, make_wheel, run_publish,
                                                    publisher, capsys):
    make_wheel()
    publisher.subprocess.run.return_value = subprocess.CompletedProcess(
        [], returncode, f'{{"message": "test-user:test-user-token"}}\n{status}',
        "Authentication for test-user failed with test-user-token",
    )
    code, _ = run_publish(RELEASE_TOKEN="test-user-token")
    assert code == (0 if returncode == 0 else 1)
    captured = capsys.readouterr()
    assert "test-user" not in captured.out + captured.err
    assert '"message": "***:***"' in captured.out
    assert "Authentication for *** failed with ***" in captured.out
    assert "--user" not in captured.out + captured.err


def test_timeout_does_not_log_credentials(make_wheel, run_publish, publisher, capsys):
    make_wheel()
    publisher.subprocess.run.side_effect = subprocess.TimeoutExpired(["curl", "test-token"], 1500)
    assert run_publish()[0] == 1
    captured = capsys.readouterr()
    assert "timed out" in captured.err
    assert "test-token" not in captured.out + captured.err


def test_workflow_wiring_and_secret_scope():
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    call = workflow["on"]["workflow_call"]
    assert set(workflow["on"]) == {"workflow_call"}
    assert call["inputs"]["template_repository"]["default"] == "t-head/ci-infra"
    assert set(call["inputs"]) == {"artifact_name", "template_repository", "template_ref"}
    for name in ("artifact_name", "template_ref"):
        assert call["inputs"][name]["required"] == "true"
        assert call["inputs"][name]["type"] == "string"
    job = workflow["jobs"]["push-wheel"]
    guard, checkout, setup, download, upload = job["steps"]
    assert workflow["permissions"] == {"contents": "read"}
    assert checkout["with"] == {
        "repository": "${{ inputs.template_repository }}", "ref": "${{ inputs.template_ref }}",
        "path": "ci-template", "token": "${{ secrets.template_token || github.token }}",
        "persist-credentials": "false",
    }
    assert setup["with"]["python-version"] == "3.12"
    assert not any("pip install" in step.get("run", "") for step in job["steps"])
    assert download["uses"] == "actions/download-artifact@v4"
    assert download["with"] == {"name": "${{ inputs.artifact_name }}",
                                "path": "${{ runner.temp }}/push-tmp-wheel-artifact"}
    assert upload["env"]["ARTIFACT_DIR"] == download["with"]["path"]
    assert set(upload["env"]) == {"ARTIFACT_DIR", "RELEASE_USERNAME", "RELEASE_TOKEN"}
    assert upload["env"]["RELEASE_USERNAME"] == "${{ secrets.release_username }}"
    assert upload["env"]["RELEASE_TOKEN"] == "${{ secrets.release_token }}"
    assert upload["run"] == "python ci-template/ci-workflows/push-tmp-wheel/scripts/push_tmp_wheel.py"
    assert job["outputs"]["upload_url"] == "${{ steps.upload.outputs.upload_url }}"
    assert call["outputs"]["upload_url"]["value"] == "${{ jobs.push-wheel.outputs.upload_url }}"
    assert "env" not in job
    for step in job["steps"]:
        assert "continue-on-error" not in step
        assert "${{" not in step.get("run", "")
        if step is not upload:
            assert "RELEASE_TOKEN" not in step.get("env", {})
    for name in ("", " \n\t", "wheel-artifact"):
        result = subprocess.run(["bash", "-c", guard["run"]], env=dict(os.environ, ARTIFACT_NAME=name),
                                capture_output=True, text=True, timeout=5)
        assert (result.returncode == 0) == bool(name.strip())


def test_demo_workflow_matches_template_contract():
    template = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    demo_path = ROOT / "workflows/demo-push-tmp-wheel.yml"
    demo = yaml.load(demo_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(demo["on"]) == {"workflow_dispatch"}
    assert not demo["on"]["workflow_dispatch"]
    assert demo["permissions"] == {"contents": "read"}
    build, publish, result = (demo["jobs"][name] for name in ("build", "publish", "result"))
    assert publish["needs"] == "build"
    assert "runs-on" not in publish and "steps" not in publish
    assert publish["uses"] == (
        publish["with"]["template_repository"] + "/.github/workflows/push-tmp-wheel.yml@"
        + publish["with"]["template_ref"]
    )
    contract = template["on"]["workflow_call"]
    assert publish["with"]["template_repository"] == contract["inputs"]["template_repository"]["default"]
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
    assert artifact["with"]["path"] == "${{ runner.temp }}/push-tmp-wheel-dist/*.whl"
    assert artifact["with"]["if-no-files-found"] == "error"
    assert result["needs"] == "publish"
    assert result["steps"][0]["env"]["UPLOAD_URL"] == "${{ needs.publish.outputs.upload_url }}"
    for job in (build, result):
        assert "secrets" not in job
        for step in job["steps"]:
            if "run" in step:
                # Check syntax only; do not execute build or publishing commands.
                assert "${{" not in step["run"]
                checked = subprocess.run(["bash", "-n"], input=step["run"], capture_output=True,
                                         text=True, timeout=5)
                assert checked.returncode == 0, checked.stderr


def test_readme_documents_interface_and_local_links():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    contract = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["on"]["workflow_call"]
    for section in ("inputs", "secrets", "outputs"):
        for name in contract[section]:
            assert f"| `{name}` |" in text
    for relative in ("../../.github/workflows/push-tmp-wheel.yml", "scripts/push_tmp_wheel.py",
                     "workflows/demo-push-tmp-wheel.yml"):
        assert f"]({relative})" in text
        assert (ROOT / relative).is_file()
    assert "UTC+8 (Asia/Shanghai)" in text and "2026-11-02-1211" in text
    assert "https://download.t-head.cn/artifactory/pypi_tmp/{timestamp}/{filename}" in text
    assert "No wheel validation" in text
    assert "| `package_name` |" not in text and "| `wheel_name` |" not in text
    assert "| `template_repository` | No | `t-head/ci-infra` |" in text
    assert "python -m pytest ci-workflows/push-tmp-wheel/tests" in text


def test_migrated_workflow_script_runs_from_template_checkout(workspace):
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    upload = next(step for step in workflow["jobs"]["push-wheel"]["steps"] if step.get("id") == "upload")
    # Check out the script using the target repository layout to verify the workflow command does not depend on the source repository or old paths.
    target = workspace / "ci-template" / SCRIPT.relative_to(ROOT.parents[1])
    target.parent.mkdir(parents=True)
    target.write_bytes(SCRIPT.read_bytes())
    command = upload["run"].split()
    assert command[0] == "python"
    result = subprocess.run([sys.executable, "-S", *command[1:]], cwd=workspace,
                            env=dict(os.environ, ARTIFACT_DIR=str(workspace / "missing"),
                                     PYTHONDONTWRITEBYTECODE="1"),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "::error::artifact" in result.stderr


def test_cli_runs_without_third_party_packages_or_network(workspace):
    result = subprocess.run([sys.executable, "-S", str(SCRIPT)],
                            env=dict(os.environ, ARTIFACT_DIR=str(workspace / "missing")),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "::error::artifact" in result.stderr
