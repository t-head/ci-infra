"""Offline tests for the temporary Docker template; no image builds, registry logins, or pushes to real repositories."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/validate_inputs.py"
WORKFLOW = ROOT.parents[1] / ".github/workflows/build-and-push-tmp-docker.yml"
DEFAULT_INPUTS = {"dockerfile": "Dockerfile", "docker_name": "demo", "docker_tag": "dev-1"}


@pytest.fixture
def validator():
    spec = importlib.util.spec_from_file_location("docker_validate_inputs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory(prefix=".docker-test-", dir=ROOT) as directory:
        root = Path(directory).resolve()
        source = root / "caller-source"
        source.mkdir()
        (source / "Dockerfile").write_text("FROM scratch\nARG VERSION\n", encoding="utf-8")
        yield root


@pytest.fixture
def source(workspace):
    return workspace / "caller-source"


@pytest.fixture
def run_validate(workspace, source):
    def run(inputs=None, **environment):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", SOURCE_DIR=str(source),
                   GITHUB_OUTPUT=str(workspace / "outputs"),
                   INPUTS_JSON=json.dumps(DEFAULT_INPUTS if inputs is None else inputs))
        env.update(environment)
        result = subprocess.run([sys.executable, str(SCRIPT)], env=env, cwd=workspace,
                                text=True, capture_output=True, timeout=10)
        output = workspace / "outputs"
        return result, dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
    return run


def test_defaults_select_caller_root_and_fixed_registry(run_validate, source):
    result, outputs = run_validate()
    assert result.returncode == 0, result.stderr
    assert outputs == {"image": "download.t-head.cn/docker_tmp/demo:dev-1",
                       "build_context": str(source), "dockerfile": str(source / "Dockerfile")}


def test_dockerfile_is_relative_to_source_not_build_context(validator, source):
    (source / "docker").mkdir()
    (source / "docker/Custom.Dockerfile").write_text("FROM scratch\n")
    (source / "component/build context").mkdir(parents=True)
    inputs = dict(DEFAULT_INPUTS, dockerfile="docker/Custom.Dockerfile", build_context="component/build context")
    outputs = validator.validate_inputs(inputs, source)
    assert outputs["dockerfile"] == str(source / "docker/Custom.Dockerfile")
    assert outputs["build_context"] == str(source / "component/build context")


@pytest.mark.parametrize("name", ["demo", "team/demo", "team/sub/project", "demo_package", "demo__package",
                                  "demo-package", "demo---package", "demo.package", "123"])
def test_valid_image_names(validator, name):
    assert validator.image_reference(name, "dev") == f"download.t-head.cn/docker_tmp/{name}:dev"


@pytest.mark.parametrize("name", ["", "Demo", "demo:tag", "demo@sha256:123", "../demo", "/demo", "demo/",
                                  "team//demo", "demo,other", "demo\nother", "demo space", "demo%2fother",
                                  "https://other.example/demo", "other.example/demo", "localhost/demo",
                                  "docker_tmp/demo", "download.t-head.cn/docker_tmp/demo", "demo___package",
                                  ".demo", "demo_", "a" * 256])
def test_invalid_image_names(validator, name):
    with pytest.raises(ValueError, match="docker_name|image name"):
        validator.image_reference(name, "dev")


@pytest.mark.parametrize("tag", ["dev", "1.2.3.dev1", "dev-20261026", "_test", "CUDA13", "a" * 128])
def test_valid_tags(validator, tag):
    assert validator.image_reference("demo", tag).endswith(":" + tag)


@pytest.mark.parametrize("tag", ["", ".dev", "-dev", "a" * 129, "dev/test", "dev:tag", "dev,other",
                                 "dev\nother", "1.2.3+build", "dev test", "$(echo dev)"])
def test_invalid_tags(validator, tag):
    with pytest.raises(ValueError, match="docker_tag"):
        validator.image_reference("demo", tag)


@pytest.mark.parametrize("key", ["dockerfile", "build_context"])
@pytest.mark.parametrize("path", ["", "../ci-template", "/tmp", "https://example.com/context.tar.gz",
                                  "git://example.com/repo", "docker/../../Dockerfile", "a\nb", "a\x00b",
                                  "a\\b", " Dockerfile "])
def test_unsafe_paths_rejected(validator, source, key, path):
    with pytest.raises(ValueError, match=key):
        validator.validate_inputs(dict(DEFAULT_INPUTS, **{key: path}), source)


@pytest.mark.parametrize("key", ["dockerfile", "build_context"])
def test_missing_paths_fail_without_outputs(run_validate, key):
    result, outputs = run_validate(dict(DEFAULT_INPUTS, **{key: "missing"}))
    assert result.returncode == 1
    assert "does not exist" in result.stderr
    assert outputs == {}


def test_empty_dockerfile_rejected(validator, source):
    (source / "Dockerfile").write_bytes(b"")
    with pytest.raises(ValueError, match="non-empty regular file"):
        validator.validate_inputs(DEFAULT_INPUTS, source)


@pytest.mark.parametrize("key,path", [("dockerfile", "."), ("build_context", "Dockerfile")])
def test_wrong_path_types_rejected(validator, source, key, path):
    with pytest.raises(ValueError):
        validator.validate_inputs(dict(DEFAULT_INPUTS, **{key: path}), source)


@pytest.mark.parametrize("key", ["dockerfile", "build_context"])
def test_symlinks_cannot_escape_caller_source(validator, workspace, source, key):
    template = workspace / "ci-template"
    template.mkdir()
    (template / "Dockerfile").write_text("FROM scratch\n")
    (source / "outside").symlink_to(template, target_is_directory=True)
    path = "outside/Dockerfile" if key == "dockerfile" else "outside"
    with pytest.raises(ValueError, match="outside the caller repository"):
        validator.validate_inputs(dict(DEFAULT_INPUTS, **{key: path}), source)


def test_internal_dockerfile_symlink_is_allowed(validator, source):
    (source / "alias").symlink_to(source / "Dockerfile")
    outputs = validator.validate_inputs(dict(DEFAULT_INPUTS, dockerfile="alias"), source)
    assert outputs["dockerfile"] == str(source / "Dockerfile")


@pytest.mark.parametrize("args", ["", "\n \n", "VERSION=dev-1\nFEATURE=on", "EMPTY=", "A=hello world",
                                  "URL=https://example.com/?a=b&c=d", "LIST=a,b,c", "A=one\r\nB=two\r\n",
                                  'OPTIONS={"enabled":true}', "A=$(touch should-not-exist)"])
def test_valid_build_args_are_data_and_not_logged(run_validate, workspace, args):
    result, outputs = run_validate(dict(DEFAULT_INPUTS, build_args=args))
    assert result.returncode == 0, result.stderr
    assert "build_args" not in outputs
    assert "should-not-exist" not in result.stdout
    assert not (workspace / "should-not-exist").exists()


@pytest.mark.parametrize("args", ["TOKEN", "--push", "--tag=other", "1KEY=value", "=value", "A =value",
                                  "A=one\nA=two", "# comment", "A=x\rB=y", "A=bad\x00value"])
def test_invalid_build_args_rejected_without_exposing_values(run_validate, args):
    result, outputs = run_validate(dict(DEFAULT_INPUTS, build_args=args))
    assert result.returncode == 1
    assert "build_args" in result.stderr
    assert outputs == {}
    assert args not in result.stdout


@pytest.mark.parametrize("key", ["dockerfile", "build_context", "docker_name", "docker_tag", "build_args"])
@pytest.mark.parametrize("value", [None, 123, [], {}])
def test_inputs_must_be_strings(validator, source, key, value):
    with pytest.raises(ValueError, match=key):
        validator.validate_inputs(dict(DEFAULT_INPUTS, **{key: value}), source)


@pytest.mark.parametrize("value", [[], None, "text", 1])
def test_input_json_must_be_object(run_validate, value):
    result, outputs = run_validate(INPUTS_JSON=json.dumps(value))
    assert result.returncode == 1
    assert "JSON object" in result.stderr
    assert outputs == {}


@pytest.mark.parametrize("environment", [{"SOURCE_DIR": ""}, {"GITHUB_OUTPUT": ""}, {"INPUTS_JSON": "{"}])
def test_missing_environment_or_malformed_json_fails(run_validate, environment):
    result, outputs = run_validate(**environment)
    assert result.returncode == 1
    assert "::error::" in result.stderr
    assert outputs == {}


def test_workflow_contract_and_execution_order():
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_call"}
    contract = workflow["on"]["workflow_call"]
    for key in ("dockerfile", "docker_name", "docker_tag", "template_ref"):
        assert contract["inputs"][key]["required"] == "true"
    assert contract["inputs"]["template_repository"]["default"] == "t-head/ci-infra"
    assert contract["inputs"]["build_context"]["default"] == "."
    assert contract["inputs"]["build_args"]["default"] == ""
    assert set(contract["secrets"]) == {"template_token", "release_username", "release_token"}
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["build-and-push"]
    source, template, python, validate, credentials, buildx, login, build, summary = job["steps"]
    assert source["uses"] == template["uses"] == "actions/checkout@v4"
    assert source["with"] == {"path": "caller-source", "fetch-depth": "0", "submodules": "recursive",
                               "persist-credentials": "false"}
    assert template["with"] == {
        "repository": "${{ inputs.template_repository }}", "ref": "${{ inputs.template_ref }}",
        "path": "ci-template", "token": "${{ secrets.template_token || github.token }}",
        "persist-credentials": "false",
    }
    assert python["with"]["python-version"] == "3.12"
    assert validate["id"] == "validate"
    assert validate["env"] == {"SOURCE_DIR": "${{ github.workspace }}/caller-source", "INPUTS_JSON": "${{ toJSON(inputs) }}"}
    assert validate["run"] == "python ci-template/ci-workflows/build-and-push-tmp-docker/scripts/validate_inputs.py"
    assert buildx["uses"] == "docker/setup-buildx-action@v3"
    assert login["uses"] == "docker/login-action@v3"
    assert login["with"] == {"registry": "download.t-head.cn", "username": "${{ secrets.release_username }}",
                              "password": "${{ secrets.release_token }}", "logout": "true"}
    assert build["uses"] == "docker/build-push-action@v6"
    assert build["id"] == "build"
    assert build["with"] == {
        "context": "${{ steps.validate.outputs.build_context }}", "file": "${{ steps.validate.outputs.dockerfile }}",
        "build-args": "${{ inputs.build_args }}", "tags": "${{ steps.validate.outputs.image }}",
        "platforms": "linux/amd64", "push": "true",
    }
    assert job["outputs"] == {"image": "${{ steps.validate.outputs.image }}", "digest": "${{ steps.build.outputs.digest }}"}
    for key in ("image", "digest"):
        assert contract["outputs"][key]["value"] == "${{ jobs.build-and-push.outputs." + key + " }}"
    assert "env" not in job and "env" not in workflow
    for step in job["steps"]:
        assert "continue-on-error" not in step
        assert "if" not in step
        assert "${{" not in step.get("run", "")
        if step not in (credentials, login):
            assert "secrets.release_" not in str(step)
        if "run" in step:
            result = subprocess.run(["bash", "-n"], input=step["run"], text=True, capture_output=True, timeout=5)
            assert result.returncode == 0, result.stderr


def test_demo_matches_template_contract():
    demo_path = ROOT / "workflows/demo-build-and-push-tmp-docker.yml"
    demo = yaml.load(demo_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    template = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    contract = template["on"]["workflow_call"]
    assert set(demo["on"]) == {"workflow_dispatch"}
    inputs = demo["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"dockerfile", "build_context", "build_args", "docker_name", "docker_tag"}
    assert inputs["dockerfile"]["default"] == "Dockerfile"
    for key in ("build_context", "build_args"):
        assert inputs[key]["default"] == contract["inputs"][key]["default"]
    assert demo["permissions"] == {"contents": "read"}
    publish = demo["jobs"]["publish"]
    assert "steps" not in publish and "runs-on" not in publish
    assert publish["uses"] == (
        publish["with"]["template_repository"] + "/.github/workflows/build-and-push-tmp-docker.yml@"
        + publish["with"]["template_ref"]
    )
    assert publish["with"]["template_repository"] == contract["inputs"]["template_repository"]["default"]
    assert set(publish["with"]) == set(contract["inputs"])
    for key in inputs:
        assert inputs[key]["type"] == "string"
        assert publish["with"][key] == "${{ inputs." + key + " }}"
    required_secrets = {key for key, value in contract["secrets"].items() if value["required"] == "true"}
    assert required_secrets <= set(publish["secrets"]) <= set(contract["secrets"])
    assert publish["secrets"] == {
        "release_username": "${{ secrets.RELEASE_USERNAME }}",
        "release_token": "${{ secrets.RELEASE_TOKEN }}",
    }
    result = demo["jobs"]["result"]
    assert result["needs"] == "publish"
    assert "secrets" not in result
    step, = result["steps"]
    assert step["env"] == {"IMAGE": "${{ needs.publish.outputs.image }}",
                           "DIGEST": "${{ needs.publish.outputs.digest }}"}
    assert "${{" not in step["run"]
    # Check example shell syntax only; do not build, log in, or push.
    checked = subprocess.run(["bash", "-n"], input=step["run"], capture_output=True, text=True, timeout=5)
    assert checked.returncode == 0, checked.stderr


def test_readme_documents_interface_links_and_valid_examples(validator):
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    contract = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["on"]["workflow_call"]
    for section in ("inputs", "secrets", "outputs"):
        for key in contract[section]:
            assert f"| `{key}` |" in text
    assert "| `template_repository` | No | `t-head/ci-infra` |" in text
    assert "python -m pytest ci-workflows/build-and-push-tmp-docker/tests" in text
    for relative in ("../../.github/workflows/build-and-push-tmp-docker.yml", "scripts/validate_inputs.py",
                     "../../.github/workflows/build-and-push-tmp-docker-ci.yml",
                     "workflows/demo-build-and-push-tmp-docker.yml"):
        assert f"]({relative})" in text
        assert (ROOT / relative).is_file()
    assert "linux/amd64" in text and "120 minutes" in text
    assert "https://download.t-head.cn/artifactory/docker_tmp/" in text
    assert validator.image_reference("team/demo", "dev-20261026") in text
    snippet = text.split("```yaml\n", 1)[1].split("```", 1)[0]
    build_args = yaml.load(snippet, Loader=yaml.BaseLoader)["build_args"]
    assert build_args == "PYTHON_VERSION=3.12\nAPP_VERSION=1.2.3.dev1\n"
    validator.validate_build_args(build_args)


@pytest.mark.parametrize("username,token,success", [("test-user", "test-token", True), ("", "test-token", False),
                                                   ("test-user", "", False), (" \n", " \t", False)])
def test_credentials_guard_does_not_log_values(username, token, success):
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    guard = next(step for step in workflow["jobs"]["build-and-push"]["steps"] if step["name"] == "Validate publishing credentials")
    result = subprocess.run(["bash", "-c", guard["run"]],
                            env=dict(os.environ, RELEASE_USERNAME=username, RELEASE_TOKEN=token),
                            capture_output=True, text=True, timeout=5)
    assert (result.returncode == 0) == success
    assert "test-user" not in result.stdout + result.stderr
    assert "test-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("length", [255, 256])
def test_repository_length_includes_migrated_registry(validator, length):
    prefix = "download.t-head.cn/docker_tmp/"
    name = "a" * (length - len(prefix))
    if length == 255:
        assert validator.image_reference(name, "dev") == prefix + name + ":dev"
    else:
        with pytest.raises(ValueError, match="255"):
            validator.image_reference(name, "dev")


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "missing"])
def test_migrated_workflow_runs_from_template_checkout(workspace, source, dockerfile):
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["build-and-push"]["steps"]
    validate = next(step for step in steps if step.get("id") == "validate")
    login = next(step for step in steps if step.get("uses") == "docker/login-action@v3")
    # Run the YAML validation command using the actual checkout layout, without Docker build or login steps.
    target = workspace / "ci-template" / SCRIPT.relative_to(ROOT.parents[1])
    target.parent.mkdir(parents=True)
    target.write_bytes(SCRIPT.read_bytes())
    output = workspace / "outputs"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", SOURCE_DIR=str(source),
               INPUTS_JSON=json.dumps(dict(DEFAULT_INPUTS, dockerfile=dockerfile)),
               GITHUB_OUTPUT=str(output), TEST_PYTHON=sys.executable)
    result = subprocess.run(["bash"], input='python() { "$TEST_PYTHON" "$@"; }\n' + validate["run"],
                            cwd=workspace, env=env, text=True, capture_output=True, timeout=10)
    if dockerfile == "missing":
        assert result.returncode == 1
        assert "does not exist" in result.stderr
        assert not output.exists()
    else:
        assert result.returncode == 0, result.stderr
        outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
        assert outputs == {"image": login["with"]["registry"] + "/docker_tmp/demo:dev-1",
                           "build_context": str(source), "dockerfile": str(source / "Dockerfile")}


def test_module_ci_covers_migrated_paths_without_release_credentials():
    path = ROOT.parents[1] / ".github/workflows/build-and-push-tmp-docker-ci.yml"
    text = path.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"pull_request", "push"}
    expected_paths = {"ci-workflows/build-and-push-tmp-docker/**",
                      ".github/workflows/build-and-push-tmp-docker.yml",
                      ".github/workflows/build-and-push-tmp-docker-ci.yml"}
    for event in ("pull_request", "push"):
        assert set(workflow["on"][event]["paths"]) == expected_paths
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets" not in text
    job = workflow["jobs"]["tests"]
    assert job["strategy"]["matrix"]["python"] == ["3.12", "3.14"]
    checkout, setup, install, run = job["steps"]
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"]["persist-credentials"] == "false"
    assert setup["with"]["python-version"] == "${{ matrix.python }}"
    assert install["run"] == "python -m pip install --no-input pytest PyYAML"
    assert run["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert run["run"] == "python -m pytest ci-workflows/build-and-push-tmp-docker/tests -q -p no:cacheprovider"
    for step in (install, run):
        checked = subprocess.run(["bash", "-n"], input=step["run"], capture_output=True, text=True, timeout=5)
        assert checked.returncode == 0, checked.stderr
