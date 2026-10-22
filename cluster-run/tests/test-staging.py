#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


ACTION = Path(__file__).resolve().parent.parent
WORKFLOW = ACTION.parent / ".github" / "workflows" / (ACTION.name + "-ci.yml")


def load_yaml(path):
    result = subprocess.run(
        ["ruby", "-ryaml", "-rjson", "-e",
         "puts JSON.generate(YAML.safe_load(File.read(ARGV.fetch(0)), aliases: true))",
         str(path)], check=True, text=True, capture_output=True)
    return json.loads(result.stdout)


def wrapper(tool):
    return ("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
            + shlex.quote(str(Path(__file__).resolve()))
            + " --fake " + tool + ' "$@"\n')


def fake_command(tool, args):
    root = Path(os.environ["STAGING_FIXTURE"])
    fixture = json.loads((root / "fixture.json").read_text())
    with (root / "calls.jsonl").open("a") as calls:
        calls.write(json.dumps({"tool": tool, "args": args}) + "\n")
    if tool == "tar":
        if fixture.get("tar_status"):
            return fixture["tar_status"]
        return subprocess.run([os.environ["STAGING_REAL_TAR"], *args]).returncode
    if tool == "cp":
        if fixture.get("cp_status") and args[-1] == fixture["partial_destination"]:
            Path(args[-1]).write_bytes(Path(args[-2]).read_bytes()[:16])
            return fixture["cp_status"]
        return subprocess.run([os.environ["STAGING_REAL_CP"], *args]).returncode
    if tool == "timeout":
        return subprocess.run(args[2:], env={**os.environ, "STAGING_API_TIMEOUT": args[1]}).returncode
    if tool == "kubectl":
        if any(arg.startswith("--request-timeout=") for arg in args):
            raise ValueError("Request timeout overrides break in-cluster configuration")
        if args[0] == "version":
            if "--client" not in args and not os.environ.get("STAGING_API_TIMEOUT"):
                raise ValueError("Server version requests must have an outer deadline")
            print("fixture kubectl")
            return 0 if "--client" in args else fixture.get("server_status", 0)
        if args[:2] == ["get", "crd"]:
            if not os.environ.get("STAGING_API_TIMEOUT"):
                raise ValueError("CRD requests must have an outer deadline")
            if args[2] != "podgroups.scheduling.x-k8s.io":
                raise ValueError("Only x-k8s.io PodGroups are supported")
            return fixture.get("crd_status", 0)
        if args[0] == "delete":
            return 0
        if args[:2] == ["get", "pods"]:
            print(fixture.get("pods", ""), end="")
            return fixture.get("query_status", 0)
    if tool == "curl":
        if "-o" in args:
            Path(args[args.index("-o") + 1]).write_text(wrapper("kubectl"))
        else:
            print("v1.31.0")
        return 0
    if tool == "file":
        print("fixture download: " + ("ELF binary" if fixture.get("elf", True) else "HTML document"))
        return 0
    if tool == "install":
        return 1
    raise ValueError(f"Unexpected fixture command: {tool} {args}")


class StagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.action = load_yaml(ACTION / "action.yml")
        cls.workflow = load_yaml(WORKFLOW)
        cls.bash = shutil.which("bash")
        cls.tar = os.environ.get("STAGING_TAR") or shutil.which("gtar") or shutil.which("tar")
        if not cls.bash or not cls.tar:
            raise RuntimeError("Bash and GNU tar are required")

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ppu-staging-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.nas = self.root / "nas"
        self.nas.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        (self.root / "fixture.json").write_text("{}")
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("INPUT_", "GITHUB_", "JOB_", "GIT_"))
                    and key not in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "TAR_OPTIONS")}
        self.env.update(
            HOME=str(self.home), PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            STAGING_FIXTURE=str(self.root), STAGING_REAL_TAR=str(self.tar),
            GITHUB_WORKSPACE=str(self.workspace), GITHUB_RUN_ID="1234", GITHUB_RUN_ATTEMPT="1",
            JOB_NAME="fixture-job", ARTIFACT_PATHS="", GITHUB_ENV=str(self.root / "env"),
            GITHUB_PATH=str(self.root / "path"), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.fake("tar")

    def fake(self, tool, directory=None):
        path = (directory or self.bin) / tool
        path.write_text(wrapper(tool))
        path.chmod(0o755)
        return path

    def fixture(self, **values):
        (self.root / "fixture.json").write_text(json.dumps(values))

    def step(self, name):
        return next(step["run"] for step in self.action["runs"]["steps"] if step["name"] == name)

    def shell(self, script, env=None):
        return subprocess.run([self.bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                              env=env or self.env, text=True, capture_output=True, timeout=30)

    def stage(self, paths=""):
        env = dict(self.env, ARTIFACT_PATHS=paths)
        script = self.step("Stage source to NAS").replace("/wl_nas/devops", str(self.nas))
        script = script.replace("/tmp/source-", str(self.root / "source-"))
        return self.shell(script, env)

    def staged(self):
        return self.nas / "1234-1" / "fixture-job" / "source.tar.gz"

    def assert_staged(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.staged().is_file())
        self.assertEqual((self.root / "env").read_text(), "SOURCE_STAGE_DIR=" + str(self.staged()) + "\n")

    def assert_symlink_rejected(self, result):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::error::", result.stdout)
        self.assertIn("symlink", result.stdout)
        self.assertFalse(self.staged().exists())
        self.assertFalse((self.root / "env").exists())

    def git(self, *args, directory=None):
        result = subprocess.run(
            ["git", "-C", str(directory or self.workspace),
             "-c", "user.name=Staging Fixture", "-c", "user.email=staging@example.invalid", *args],
            env=self.env, check=True, text=True, capture_output=True, timeout=30)
        return result.stdout.strip()

    def test_gnu_tar_is_used(self):
        result = subprocess.run([self.tar, "--version"], check=True, text=True, capture_output=True)
        self.assertIn("GNU tar", result.stdout, "Set STAGING_TAR to a GNU tar binary on macOS")

    def test_git_configs_excluded_and_version_metadata_preserved(self):
        self.git("init", "-q")
        (self.workspace / "tracked.txt").write_text("version fixture\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "Create isolated staging fixture")
        self.git("tag", "-a", "staging-v1", "-m", "Version fixture")
        expected = self.git("describe", "--always")
        snapshots = {}
        for prefix in ("", "nested/"):
            for suffix in ("config", "config.worktree", "modules/sub/config", "modules/sub/config.worktree",
                           "modules/sub/modules/deep/config", "modules/sub/modules/deep/config.worktree",
                           "worktrees/linked/config", "worktrees/linked/config.worktree",
                           "modules/sub/worktrees/linked/config.worktree"):
                relative = prefix + ".git/" + suffix
                config = self.workspace / relative
                config.parent.mkdir(parents=True, exist_ok=True)
                with config.open("a") as handle:
                    handle.write('\n[http "https://example.invalid/"]\n\textraheader = fake-tokenmarker-' + relative + '\n')
                snapshots[relative] = config.read_bytes()
        result = self.stage()
        self.assert_staged(result)
        with tarfile.open(self.staged()) as archive:
            members = archive.getmembers()
            names = {member.name.removeprefix("./") for member in members}
            for relative in snapshots:
                self.assertNotIn(relative, names)
            self.assertIn(".git/HEAD", names)
            self.assertIn(".git/refs/tags/staging-v1", names)
            self.assertTrue(any(name.startswith(".git/objects/") for name in names))
            for member in members:
                if member.isfile():
                    self.assertNotIn(b"fake-tokenmarker", archive.extractfile(member).read())
        extracted = self.root / "extracted"
        extracted.mkdir()
        subprocess.run([self.tar, "xzf", str(self.staged()), "-C", str(extracted)], check=True, env=self.env)
        self.assertEqual(self.git("describe", "--always", directory=extracted), expected)
        for relative, contents in snapshots.items():
            self.assertEqual((self.workspace / relative).read_bytes(), contents)

    def test_same_source_and_duplicate_paths_are_skipped(self):
        artifact = self.workspace / "artifact"
        artifact.mkdir()
        (artifact / "marker").write_text("original")
        self.assert_staged(self.stage("artifact\n" + str(artifact) + "\n" + str(artifact)))
        self.assertFalse((artifact / "artifact").exists())
        self.assertEqual((artifact / "marker").read_text(), "original")
        with tarfile.open(self.staged()) as archive:
            self.assertEqual(archive.extractfile("./artifact/marker").read(), b"original")

    def test_symlink_to_same_source_is_rejected(self):
        artifact = self.workspace / "artifact"
        artifact.mkdir()
        (artifact / "marker").write_text("original")
        external = self.root / "artifact"
        external.symlink_to(artifact, target_is_directory=True)
        self.assert_symlink_rejected(self.stage(str(external)))
        self.assertTrue(external.is_symlink())
        self.assertEqual(os.readlink(external), str(artifact))
        self.assertEqual((artifact / "marker").read_text(), "original")
        self.assertFalse((artifact / "artifact").exists())

    def test_destination_symlink_to_same_source_is_rejected(self):
        artifact = self.root / "artifact"
        artifact.mkdir()
        (artifact / "marker").write_text("external")
        target = self.workspace / "artifact"
        target.symlink_to(artifact, target_is_directory=True)
        self.assert_symlink_rejected(self.stage(str(artifact)))
        self.assertTrue(target.is_symlink())
        self.assertEqual(os.readlink(target), str(artifact))
        self.assertEqual((artifact / "marker").read_text(), "external")
        self.assertFalse((artifact / "artifact").exists())

    def test_selected_workspace_symlink_is_rejected(self):
        artifact = self.root / "payload"
        artifact.mkdir()
        (artifact / "marker").write_text("runner-only")
        target = self.workspace / "artifact"
        target.symlink_to(artifact, target_is_directory=True)
        for path in ("artifact", str(target), "artifact/", str(target) + "///"):
            with self.subTest(path=path):
                self.assert_symlink_rejected(self.stage(path))
                self.assertTrue(target.is_symlink())
                self.assertEqual(os.readlink(target), str(artifact))
                self.assertEqual((artifact / "marker").read_text(), "runner-only")

    def test_source_symlink_without_destination_is_rejected(self):
        for kind in ("file", "directory", "dangling"):
            with self.subTest(kind=kind):
                payload = self.root / ("payload-" + kind)
                if kind == "file":
                    payload.write_text("external")
                elif kind == "directory":
                    payload.mkdir()
                    (payload / "marker").write_text("external")
                source = self.root / ("artifact-" + kind)
                source.symlink_to(payload, target_is_directory=kind == "directory")
                self.assert_symlink_rejected(self.stage(str(source)))
                self.assertTrue(source.is_symlink())
                self.assertEqual(os.readlink(source), str(payload))
                self.assertFalse((self.workspace / source.name).exists())
                self.assertFalse((self.workspace / source.name).is_symlink())
                if kind == "file":
                    self.assertEqual(payload.read_text(), "external")
                elif kind == "directory":
                    self.assertEqual((payload / "marker").read_text(), "external")
                else:
                    self.assertFalse(payload.exists())

    def test_missing_artifact_path_is_warned_and_skipped(self):
        result = self.stage("missing-artifact")
        self.assert_staged(result)
        self.assertIn("[warn]", result.stdout)
        self.assertIn(str(self.workspace / "missing-artifact"), result.stdout)
        self.assertNotIn("::error::", result.stdout)

    def test_hardlink_to_same_file_is_skipped(self):
        artifact = self.root / "artifact"
        artifact.write_text("same inode")
        os.link(artifact, self.workspace / "artifact")
        self.assert_staged(self.stage(str(artifact)))
        self.assertTrue(os.path.samefile(artifact, self.workspace / "artifact"))
        with tarfile.open(self.staged()) as archive:
            self.assertEqual(archive.extractfile("./artifact").read(), b"same inode")

    def test_different_sources_with_same_basename_fail(self):
        for kind in ("file", "directory", "symlink", "dangling"):
            with self.subTest(kind=kind):
                source = self.root / ("outside-" + kind) / "artifact"
                source.parent.mkdir()
                source.mkdir()
                (source / "marker").write_text("external")
                target = self.workspace / "artifact"
                if kind == "file":
                    target.write_text("workspace")
                elif kind == "directory":
                    target.mkdir()
                    (target / "marker").write_text("workspace")
                else:
                    other = self.root / "unrelated"
                    if kind == "symlink":
                        other.write_text("workspace")
                    target.symlink_to(other)
                result = self.stage(str(source))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("::error::", result.stdout)
                self.assertFalse(self.staged().exists())
                self.assertFalse((self.root / "env").exists())
                self.assertEqual((source / "marker").read_text(), "external")
                if kind == "directory":
                    self.assertEqual((target / "marker").read_text(), "workspace")
                    self.assertFalse((target / "artifact").exists())
                    shutil.rmtree(target)
                else:
                    if kind != "dangling":
                        self.assertEqual(target.read_text(), "workspace")
                    if kind in ("symlink", "dangling"):
                        self.assertTrue(target.is_symlink())
                    target.unlink()

    def test_external_artifacts_are_copied(self):
        directory = self.root / "extra files"
        directory.mkdir()
        (directory / "marker").write_text("directory fixture")
        file = self.root / "extra.txt"
        file.write_text("file fixture")
        self.assert_staged(self.stage(" \n " + str(directory) + " \n" + str(file) + "\n"))
        self.assertEqual((self.workspace / "extra files" / "marker").read_text(), "directory fixture")
        self.assertEqual((self.workspace / "extra.txt").read_text(), "file fixture")
        with tarfile.open(self.staged()) as archive:
            self.assertEqual(archive.extractfile("./extra.txt").read(), b"file fixture")
            self.assertEqual(archive.extractfile("./extra files/marker").read(), b"directory fixture")

    def test_large_listing_does_not_abort_staging(self):
        for index in range(2000):
            (self.workspace / (f"entry-{index:05d}-" + "x" * 160)).touch()
        unguarded = self.shell('ls -la "$GITHUB_WORKSPACE/" | head -30')
        self.assertNotEqual(unguarded.returncode, 0, "Fixture must trigger the listing pipeline failure")
        self.assert_staged(self.stage())

    def test_archive_failure_is_not_ignored(self):
        self.fixture(tar_status=23)
        result = self.stage()
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        self.assertFalse(self.staged().exists())
        self.assertFalse((self.root / "env").exists())

    def test_partial_nas_copy_failure_is_handed_to_cleanup(self):
        self.env["STAGING_REAL_CP"] = shutil.which("cp")
        self.fake("cp")
        self.fixture(cp_status=29, partial_destination=str(self.staged()))
        artifact = self.root / "extra.txt"
        artifact.write_text("file fixture")
        result = self.stage(str(artifact))
        self.assertEqual(result.returncode, 29, result.stdout + result.stderr)
        self.assertEqual((self.workspace / "extra.txt").read_text(), "file fixture")
        local_tarball = self.root / "source-1234-1.tar.gz"
        with tarfile.open(local_tarball) as archive:
            self.assertEqual(archive.extractfile("./extra.txt").read(), b"file fixture")
        self.assertEqual(self.staged().read_bytes(), local_tarball.read_bytes()[:16])
        handoff = (self.root / "env").read_text()
        self.assertEqual(handoff, "SOURCE_STAGE_DIR=" + str(self.staged()) + "\n")
        cleanup_step = next(step for step in self.action["runs"]["steps"] if step.get("id") == "cleanup")
        self.assertEqual(cleanup_step["env"]["SOURCE_STAGE_DIR"], "${{ env.SOURCE_STAGE_DIR }}")
        self.fake("kubectl")
        self.fake("timeout")
        env = dict(self.env, INPUT_NAMESPACE="fixture", INPUT_NNODES="0",
                   INPUT_JOB_STATUS="", INPUT_WORKFLOW_STATUS="failure",
                   GITHUB_OUTPUT=str(self.root / "output"))
        env.update(line.split("=", 1) for line in handoff.splitlines())
        cleanup = (ACTION / "scripts" / "cleanup.sh").read_text()
        cleanup = cleanup.replace("/wl_nas/devops", str(self.nas))
        cleanup = cleanup.replace("/tmp/training-manifests", str(self.root / "training-manifests"))
        result = self.shell(cleanup, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(self.staged()), result.stdout)
        self.assertFalse(self.staged().exists())
        self.assertFalse(self.staged().parent.exists())

    def setup_script(self):
        for tool in ("mkdir", "mv", "chmod", "head", "grep"):
            (self.bin / tool).symlink_to(shutil.which(tool))
        self.fake("timeout")
        self.env["PATH"] = str(self.bin)
        script = self.step("Setup kubectl").replace("/opt/bin", str(self.root / "opt" / "bin"))
        script = script.replace("/tmp/kubectl", str(self.root / "downloaded-kubectl"))
        return script.replace("/usr/local/bin/kubectl", str(self.root / "system-kubectl"))

    def next_step_kubectl(self, expected):
        paths = (self.root / "path").read_text().splitlines()
        self.assertEqual(paths, [str(expected)])
        env = dict(self.env, PATH=os.pathsep.join(paths + [self.env["PATH"]]))
        result = self.shell("kubectl version --client", env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fixture kubectl", result.stdout)

    def test_hostpath_kubectl_survives_next_step(self):
        script = self.setup_script()
        host = self.root / "opt" / "bin"
        host.mkdir(parents=True)
        self.fake("kubectl", host)
        result = self.shell(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.next_step_kubectl(host)

    def test_preflight_server_failure_is_bounded_and_nonfatal(self):
        script = self.setup_script()
        host = self.root / "opt" / "bin"
        host.mkdir(parents=True)
        self.fake("kubectl", host)
        self.fixture(server_status=42)
        result = self.shell(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("server version unavailable", result.stdout)
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        requests = [call for call in calls if call["tool"] == "timeout"]
        self.assertEqual(requests[0]["args"][:2], ["--signal=KILL", "10s"])
        self.assertNotIn("log_artifact_name", self.action["outputs"])

    def test_preflight_requires_supported_crd_only_in_gang_mode(self):
        self.fake("kubectl")
        self.fake("timeout")
        script = self.step("Validate inputs and check PodGroup CRD")
        env = dict(self.env, INPUT_IMAGE="fixture:latest", INPUT_COMMAND="exit 0")
        for nodes, status, expected in (("0", 42, 0), ("2", 0, 0), ("2", 42, 1)):
            with self.subTest(nodes=nodes, status=status):
                self.fixture(crd_status=status)
                result = self.shell(script, dict(env, INPUT_NNODES=nodes))
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        crds = [call for call in calls if call["tool"] == "kubectl"]
        self.assertEqual(len(crds), 2)
        self.assertTrue(all(call["args"][2] == "podgroups.scheduling.x-k8s.io" for call in crds))
        requests = [call for call in calls if call["tool"] == "timeout"]
        self.assertTrue(all(call["args"][:2] == ["--signal=KILL", "10s"] for call in requests))

    def test_download_fallback_survives_next_step(self):
        script = self.setup_script()
        for tool in ("curl", "file", "install"):
            self.fake(tool)
        result = self.shell(script)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.next_step_kubectl(self.home / ".local" / "bin")
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertTrue(any(call["tool"] == "file" for call in calls))

    def test_invalid_download_fails_before_installation(self):
        script = self.setup_script()
        for tool in ("curl", "file", "install"):
            self.fake(tool)
        self.fixture(elf=False)
        result = self.shell(script)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::error::", result.stdout)
        self.assertFalse((self.root / "path").exists())
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        self.assertFalse(any(call["tool"] == "install" for call in calls))

    def test_ci_query_failures_and_remaining_pods_fail(self):
        self.fake("kubectl")
        scripts = [step["run"] for job in self.workflow["jobs"].values() for step in job["steps"]
                   if "kubectl get pods" in step.get("run", "")]
        self.assertTrue(scripts)
        env = dict(self.env)
        for script in scripts:
            step = next(step for job in self.workflow["jobs"].values() for step in job["steps"]
                        if step.get("run") == script)
            for key in step.get("env", {}):
                env[key] = "success" if "OUTCOME" in key else "succeeded"
            env.update(NAMESPACE="fixture", TEST_NAMESPACE="fixture")
            for status, pods, expected in ((0, "", 0), (0, "pod/remaining\n", 1), (37, "", 37)):
                with self.subTest(query_status=status, pods=pods):
                    self.fixture(query_status=status, pods=pods)
                    result = self.shell(script, env)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_ci_checkout_and_runner_extension_are_preserved(self):
        steps = [step for job in self.workflow["jobs"].values() for step in job["steps"]]
        checkouts = [step for step in steps if step.get("uses") == "actions/checkout@v4"]
        self.assertTrue(checkouts)
        for step in checkouts:
            self.assertIs(step.get("with", {}).get("persist-credentials"), False)
        extension = "$/" + ACTION.name
        self.assertTrue(any(step.get("uses") == extension for step in steps))
        self.assertFalse(any(step.get("uses") == "./" + ACTION.name for step in steps))
        lifecycle = "python3 " + ACTION.name + "/tests/test-lifecycle.py"
        staging = "python3 " + ACTION.name + "/tests/test-staging.py"
        sequence = next(job["steps"] for job in self.workflow["jobs"].values()
                        if any(step.get("run") == lifecycle for step in job["steps"]))
        index = next(index for index, step in enumerate(sequence) if step.get("run") == lifecycle)
        self.assertEqual(sequence[index + 1].get("run"), staging)

    def test_scheduling_step_recognizes_810e_board_alias(self):
        step = next(step for step in self.action["runs"]["steps"] if step.get("id") == "submit")
        self.assertIn(
            "(contains(format(',{0},', inputs.node_selector), ',board-type=OAM-810E,') || "
            "contains(format(',{0},', inputs.node_selector), ',board-type=ZW-810E,')) && '810E'",
            step["name"])
        self.assertIn("',board-type=ZW-M890P,') && '890P' || 'PPU'", step["name"])

    def test_yaml_inline_bash_syntax(self):
        scripts = [step["run"] for step in self.action["runs"]["steps"] if "run" in step]
        scripts += [step["run"] for job in self.workflow["jobs"].values()
                    for step in job["steps"] if "run" in step]
        for script in scripts:
            result = subprocess.run([self.bash, "-n"], input=script, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fake":
        raise SystemExit(fake_command(sys.argv[2], sys.argv[3:]))
    unittest.main()
