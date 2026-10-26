"""Offline tests for temporary wheel publishing; uploads are mocked without accessing a real Artifactory instance."""

import csv
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import Mock
import warnings
from zipfile import ZipFile

import pytest
from wheel.wheelfile import WheelFile
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/push_tmp_wheel.py"
WORKFLOW = ROOT.parents[1] / ".github/workflows/push-tmp-wheel.yml"
WHEEL_NAME = "demo_package-1.2.3-py3-none-any.whl"
DIST_INFO = "demo_package-1.2.3.dist-info"


@pytest.fixture
def publisher():
    spec = importlib.util.spec_from_file_location("push_tmp_wheel", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory(prefix=".wheel-test-", dir=ROOT) as directory:
        yield Path(directory)


@pytest.fixture
def make_wheel(workspace):
    def make(filename=WHEEL_NAME, directory="artifact", metadata=None, wheel_metadata=None,
             dist_info=DIST_INFO, extra=None):
        path = workspace / directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with WheelFile(path, "w") as archive:
            archive.writestr("demo_package/__init__.py", "# Test artifact; do not execute\n")
            archive.writestr(f"{dist_info}/METADATA", metadata if metadata is not None else (
                "Metadata-Version: 2.1\nName: demo-package\nVersion: 1.2.3\n\n"
            ))
            archive.writestr(f"{dist_info}/WHEEL", wheel_metadata if wheel_metadata is not None else (
                "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"
            ))
            for name, content in (extra or {}).items():
                archive.writestr(name, content)
        return path
    return make


@pytest.fixture
def run_publish(publisher, workspace, monkeypatch):
    monkeypatch.setenv("ARTIFACT_DIR", str(workspace / "artifact"))
    monkeypatch.setenv("PACKAGE_NAME", "demo_package")
    monkeypatch.setenv("WHEEL_NAME", WHEEL_NAME)
    monkeypatch.setenv("RELEASE_USERNAME", "test-user")
    monkeypatch.setenv("RELEASE_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_OUTPUT", str(workspace / "outputs"))
    transport = Mock(return_value=subprocess.CompletedProcess([], 0, "201", ""))
    monkeypatch.setattr(publisher.subprocess, "run", transport)

    def run(**environment):
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        return publisher.main(), transport
    return run


def rewrite_wheel(path, edit):
    with ZipFile(path) as archive:
        entries = [(entry, archive.read(entry)) for entry in archive.infolist()]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with ZipFile(path, "w") as archive:
            for entry, content in edit(entries):
                archive.writestr(entry, content)


def test_valid_wheel_upload_is_exact_and_https_only(make_wheel, run_publish, workspace):
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
    assert "--location" not in args
    assert "--insecure" not in args
    url = "https://download.t-head.cn/artifactory/pypi_tmp_generic/demo_package/" + WHEEL_NAME
    assert args[-2:] == ["--", url]
    assert path.read_bytes() == before
    assert (workspace / "outputs").read_text() == f"upload_url={url}\n"


@pytest.mark.parametrize("package", ["demo-package", "demo.package", "Demo_Package"])
def test_package_comparison_is_normalized_but_target_path_is_preserved(make_wheel, run_publish, package):
    make_wheel()
    code, transport = run_publish(PACKAGE_NAME=package)
    assert code == 0
    assert transport.call_args.args[0][-1].endswith(f"/{package}/{WHEEL_NAME}")


def test_local_version_build_tag_and_foreign_platform(make_wheel, run_publish):
    filename = "demo_package-1.2.3+cu130-2-cp310-cp310-linux_x86_64.whl"
    make_wheel(filename=filename, dist_info="demo_package-1.2.3+cu130.dist-info",
               metadata="Metadata-Version: 2.1\nName: demo_package\nVersion: 1.2.3+cu130\n\n",
               wheel_metadata="Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp310-cp310-linux_x86_64\n\n")
    code, transport = run_publish(WHEEL_NAME=filename)
    assert code == 0
    assert "1.2.3%2Bcu130-2" in transport.call_args.args[0][-1]


def test_compressed_tags(make_wheel, run_publish):
    filename = "demo_package-1.2.3-py2.py3-none-any.whl"
    make_wheel(filename=filename, wheel_metadata=(
        "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py2-none-any\nTag: py3-none-any\n\n"
    ))
    assert run_publish(WHEEL_NAME=filename)[0] == 0


@pytest.mark.parametrize("package", ["", "../escape", "a/b", "a%2fb", "demo\npackage", "other", "-demo"])
def test_invalid_or_mismatched_package_never_uploads(make_wheel, run_publish, package):
    make_wheel()
    code, transport = run_publish(PACKAGE_NAME=package)
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("filename", ["", "invalid.whl", "*.whl", "../" + WHEEL_NAME,
                                     "dist/" + WHEEL_NAME, "demo_package-notversion-py3-none-any.whl",
                                     "demo_package-1.2.3-py3-none-any\n.whl"])
def test_invalid_wheel_name_never_uploads(make_wheel, run_publish, filename):
    make_wheel()
    code, transport = run_publish(WHEEL_NAME=filename)
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("kind", ["missing-dir", "missing-file", "empty", "text", "directory", "symlink", "duplicate"])
def test_missing_invalid_or_ambiguous_file_never_uploads(kind, workspace, make_wheel, run_publish):
    path = workspace / "artifact" / WHEEL_NAME
    if kind != "missing-dir":
        path.parent.mkdir()
    if kind == "missing-file":
        (path.parent / "unrelated.txt").write_text("unrelated")
    elif kind in {"empty", "text"}:
        path.write_bytes(b"" if kind == "empty" else b"not a wheel")
    elif kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        original = make_wheel(directory="elsewhere")
        path.symlink_to(original)
    elif kind == "duplicate":
        make_wheel(directory="artifact/one")
        make_wheel(directory="artifact/two")
    code, transport = run_publish()
    assert code == 1
    transport.assert_not_called()


def test_directory_symlinks_are_not_followed(workspace, make_wheel, run_publish):
    make_wheel(directory="outside")
    (workspace / "artifact").mkdir()
    (workspace / "artifact/link").symlink_to(workspace / "outside", target_is_directory=True)
    code, transport = run_publish()
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("member", ["METADATA", "WHEEL", "RECORD"])
def test_missing_required_metadata_never_uploads(member, make_wheel, run_publish):
    path = make_wheel()
    rewrite_wheel(path, lambda entries: [(entry, data) for entry, data in entries
                                        if entry.filename != f"{DIST_INFO}/{member}"])
    code, transport = run_publish()
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("metadata", [
    "Metadata-Version: 2.1\nName: other\nVersion: 1.2.3\n\n",
    "Metadata-Version: 2.1\nName: demo_package\nVersion: 9.9\n\n",
    "Name: demo_package\nVersion: 1.2.3\n\n",
    "Metadata-Version: 2.1\nName: demo_package\nName: other\nVersion: 1.2.3\n\n",
    "Metadata-Version: 2.1\nName: demo_package\nVersion: bad\n\n",
    "not metadata",
])
def test_invalid_core_metadata_never_uploads(metadata, make_wheel, run_publish):
    make_wheel(metadata=metadata)
    code, transport = run_publish()
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("header", ["Wheel-Version: 2.0", "Root-Is-Purelib: maybe", "Tag: cp310-none-any"])
def test_invalid_wheel_metadata_never_uploads(header, make_wheel, run_publish):
    headers = {"Wheel-Version": "1.0", "Root-Is-Purelib": "true", "Tag": "py3-none-any"}
    key, value = header.split(": ")
    headers[key] = value
    make_wheel(wheel_metadata="\n".join(f"{key}: {value}" for key, value in headers.items()) + "\n\n")
    code, transport = run_publish()
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("mutation", ["content", "size", "no-hash", "missing-row", "duplicate-row", "phantom-row", "weak-hash"])
def test_record_corruption_never_uploads(mutation, make_wheel, run_publish):
    path = make_wheel()

    def edit(entries):
        for entry, data in entries:
            if mutation == "content" and entry.filename == "demo_package/__init__.py":
                # Keep the size unchanged so hash validation detects the tampering.
                data = b"x" * len(data)
            if mutation != "content" and entry.filename.endswith("/RECORD"):
                rows = list(csv.reader(io.StringIO(data.decode())))
                if mutation == "size":
                    rows[0][2] = "999"
                elif mutation == "no-hash":
                    rows[0][1] = ""
                elif mutation == "missing-row":
                    rows.pop(0)
                elif mutation == "duplicate-row":
                    rows.append(rows[0])
                elif mutation == "phantom-row":
                    rows.append(["missing.py", rows[0][1], "0"])
                elif mutation == "weak-hash":
                    rows[0][1] = rows[0][1].replace("sha256=", "md5=")
                stream = io.StringIO()
                csv.writer(stream).writerows(rows)
                data = stream.getvalue().encode()
            yield entry, data
    rewrite_wheel(path, edit)
    code, transport = run_publish()
    assert code == 1
    transport.assert_not_called()


@pytest.mark.parametrize("extra", ["../escape.py", "/absolute.py", "dir/../../escape.py", "dir\\escape.py",
                                  "other-1.0.dist-info/METADATA"])
def test_unsafe_or_extra_dist_info_paths_never_upload(extra, make_wheel, run_publish):
    make_wheel(extra={extra: b"unexpected"})
    code, transport = run_publish()
    assert code == 1
    transport.assert_not_called()


def test_duplicate_zip_members_never_upload(make_wheel, run_publish):
    path = make_wheel()
    rewrite_wheel(path, lambda entries: entries + [entries[0]])
    code, transport = run_publish()
    assert code == 1
    transport.assert_not_called()


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
    publisher.subprocess.run.return_value = subprocess.CompletedProcess([], returncode, status, "test-token")
    code, transport = run_publish()
    assert code == 1
    transport.assert_called_once()
    assert not (workspace / "outputs").exists()
    captured = capsys.readouterr()
    assert "test-token" not in captured.out + captured.err


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
    for name in ("package_name", "wheel_name", "artifact_name", "template_ref"):
        assert call["inputs"][name]["required"] == "true"
        assert call["inputs"][name]["type"] == "string"
    job = workflow["jobs"]["push-wheel"]
    guard, checkout, setup, install, download, upload = job["steps"]
    assert workflow["permissions"] == {"contents": "read"}
    assert checkout["with"] == {
        "repository": "${{ inputs.template_repository }}", "ref": "${{ inputs.template_ref }}",
        "path": "ci-template", "token": "${{ secrets.template_token || github.token }}",
        "persist-credentials": "false",
    }
    assert setup["with"]["python-version"] == "3.12"
    assert install["run"] == "python -m pip install --no-input packaging wheel"
    assert download["uses"] == "actions/download-artifact@v4"
    assert download["with"] == {"name": "${{ inputs.artifact_name }}",
                                "path": "${{ runner.temp }}/push-tmp-wheel-artifact"}
    assert upload["env"]["ARTIFACT_DIR"] == download["with"]["path"]
    assert upload["env"]["WHEEL_NAME"] == "${{ inputs.wheel_name }}"
    assert upload["env"]["PACKAGE_NAME"] == "${{ inputs.package_name }}"
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
    assert set(demo["on"]["workflow_dispatch"]["inputs"]) == {"package_name", "wheel_name"}
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
    for name in ("package_name", "wheel_name"):
        assert publish["with"][name] == "${{ inputs." + name + " }}"
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
    assert "1.2.3.dev12+gabc123" in text
    assert "https://download.t-head.cn/artifactory/pypi_tmp_generic/{package_name}/{wheel_name}" in text
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
    result = subprocess.run([sys.executable, *command[1:]], cwd=workspace,
                            env=dict(os.environ, ARTIFACT_DIR=str(workspace / "missing"),
                                     PACKAGE_NAME="demo_package", WHEEL_NAME=WHEEL_NAME,
                                     PYTHONDONTWRITEBYTECODE="1"),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "::error::artifact" in result.stderr


def test_ci_covers_migrated_workflow_and_tests():
    ci = yaml.load((ROOT.parents[1] / ".github/workflows/release-pypi-ci.yml").read_text(encoding="utf-8"),
                   Loader=yaml.BaseLoader)
    for event in ("pull_request", "push"):
        assert "ci-workflows/push-tmp-wheel/**" in ci["on"][event]["paths"]
        assert ".github/workflows/push-tmp-wheel.yml" in ci["on"][event]["paths"]
    steps = ci["jobs"]["tests"]["steps"]
    run = next(step for step in steps if step.get("run", "").startswith("python -m pytest "))
    assert run["run"] == (
        "python -m pytest ci-workflows/release-pypi/tests ci-workflows/push-tmp-wheel/tests -q -p no:cacheprovider"
    )
    assert run["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_cli_reports_missing_artifact_without_network(workspace):
    result = subprocess.run([sys.executable, str(SCRIPT)],
                            env=dict(os.environ, ARTIFACT_DIR=str(workspace / "missing"),
                                     PACKAGE_NAME="demo_package", WHEEL_NAME=WHEEL_NAME),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "::error::artifact" in result.stderr
