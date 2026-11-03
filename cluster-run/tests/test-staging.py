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
        if args[:2] == ["auth", "can-i"]:
            if not os.environ.get("STAGING_API_TIMEOUT"):
                raise ValueError("Secret permission checks must have an outer deadline")
            denied = fixture.get("denied_secret_verb") == args[2]
            print("no" if denied else "yes")
            return int(denied)
        if args[0] == "delete":
            return 0
        if args[0] == "get" and args[1] in ("svc", "podgroups.scheduling.x-k8s.io", "secret", "secrets"):
            if fixture.get("remaining_resource") == args[1]:
                print(args[1] + "/remaining")
            return fixture.get("resource_query_status", 0)
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

    def test_existing_destination_is_kept(self):
        source = self.root / "outside" / "artifact"
        source.parent.mkdir()
        source.write_text("external")
        target = self.workspace / "artifact"
        target.write_text("workspace")
        result = self.stage(str(source))
        self.assert_staged(result)
        self.assertEqual(target.read_text(), "workspace")
        with tarfile.open(self.staged()) as archive:
            self.assertEqual(archive.extractfile("./artifact").read(), b"workspace")

    def test_source_symlink_is_copied_as_link(self):
        payload = self.root / "payload"
        payload.write_text("external")
        source = self.root / "artifact-link"
        source.symlink_to(payload)
        result = self.stage(str(source))
        self.assert_staged(result)
        target = self.workspace / source.name
        self.assertTrue(target.is_symlink())
        self.assertEqual(os.readlink(target), str(payload))
        with tarfile.open(self.staged()) as archive:
            self.assertTrue(archive.getmember("./artifact-link").issym())

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

    def test_existing_directory_destination_is_kept(self):
        source = self.root / "outside" / "artifact"
        source.mkdir(parents=True)
        (source / "marker").write_text("external")
        target = self.workspace / "artifact"
        target.mkdir()
        (target / "marker").write_text("workspace")
        result = self.stage(str(source))
        self.assert_staged(result)
        self.assertEqual((target / "marker").read_text(), "workspace")
        self.assertFalse((target / "artifact").exists())

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
        env = dict(self.env, INPUT_IMAGE="fixture:latest", INPUT_COMMAND="exit 0",
                   INPUT_LAUNCH_MODE="each-node", INPUT_SSH_PORT="2222", INPUT_SETUP_COMMAND="")
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
        self.fake("timeout")
        scripts = [step["run"] for job in self.workflow["jobs"].values() for step in job["steps"]
                   if "kubectl get pods" in step.get("run", "")]
        self.assertTrue(scripts)
        env = dict(self.env)
        for script in scripts:
            step = next(step for job in self.workflow["jobs"].values() for step in job["steps"]
                        if step.get("run") == script)
            for key in step.get("env", {}):
                env[key] = "success" if "OUTCOME" in key else "succeeded"
            env.update(NAMESPACE="fixture", TEST_NAMESPACE="fixture", ALLOCATION_NAME="fixture-job")
            script = script.replace("/wl_nas/devops/runtime/master", str(self.nas / "runtime"))
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

    def test_master_action_inputs_and_internal_output_handoff(self):
        existing = {"nnodes", "nproc_per_node", "command", "image", "namespace", "timeout_minutes",
                    "host_volumes", "extra_env", "master_port", "cleanup_policy", "source_dir",
                    "node_selector", "container_options", "artifact_paths", "job_suffix", "dmesg_policy"}
        self.assertEqual(set(self.action["inputs"]) - existing, {"launch_mode", "setup_command", "ssh_port"})
        for key, default in (("launch_mode", "each-node"), ("setup_command", ""), ("ssh_port", "2222")):
            self.assertEqual(str(self.action["inputs"][key]["default"]), default)
            self.assertIs(self.action["inputs"][key].get("required", False), False)
        steps = {step["id"]: step for step in self.action["runs"]["steps"] if "id" in step}
        for key in ("launch_mode", "setup_command", "ssh_port"):
            self.assertEqual(steps["submit"]["env"]["INPUT_" + key.upper()], "${{ inputs." + key + " }}")
        for name in ("monitor", "cleanup"):
            self.assertEqual(steps[name]["env"]["INPUT_LAUNCH_MODE"], "${{ inputs.launch_mode }}")
            self.assertEqual(steps[name]["env"]["MASTER_RUNTIME_DIR"], "${{ steps.submit.outputs.master_runtime_dir }}")
        self.assertEqual(steps["monitor"]["env"]["JOB_DEADLINE"], "${{ steps.submit.outputs.deadline }}")
        self.assertEqual(steps["cleanup"]["if"], "always()")

    def test_master_preflight_validates_mode_and_port_without_cluster_side_effects(self):
        self.fake("kubectl")
        self.fake("timeout")
        script = self.step("Validate inputs and check PodGroup CRD")
        env = dict(self.env, INPUT_IMAGE="fixture:latest", INPUT_COMMAND="exit 0", INPUT_NNODES="0",
                   INPUT_LAUNCH_MODE="master", INPUT_SSH_PORT="2222", INPUT_SETUP_COMMAND="")
        for mode, port, expected in (("each-node", "1", 0), ("master", "65535", 0),
                                     ("each_node", "2222", 1), ("MASTER", "2222", 1),
                                     ("master", "0", 1), ("master", "65536", 1),
                                     ("master", "22x", 1), ("master", "2222;exit 0", 1)):
            with self.subTest(mode=mode, port=port):
                result = self.shell(script, dict(env, INPUT_LAUNCH_MODE=mode, INPUT_SSH_PORT=port))
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                if expected:
                    self.assertIn("::error::", result.stdout + result.stderr)
        self.assertFalse((self.root / "calls.jsonl").exists(), "Standalone preflight must not contact a cluster")

    def test_ci_covers_real_runtime_and_installer_with_required_linux_tools(self):
        steps = self.workflow["jobs"]["checks"]["steps"]
        runs = [step.get("run", "") for step in steps]
        self.assertTrue(any("sudo python3" in run and "cluster-run/tests/test-master-runtime.py" in run
                            for run in runs), "Real sshd tests must run as root in the isolated Ubuntu job")
        self.assertTrue(any("python3 cluster-run/tests/test-ssh-install.py" in run for run in runs))
        dependencies = next(run for run in runs if "apt-get install" in run)
        for package in ("openssh-server", "openssh-client", "jq", "util-linux", "sudo"):
            self.assertIn(package, dependencies)
        self.assertEqual(self.workflow["jobs"]["checks"]["runs-on"], "ubuntu-latest")

    def test_ci_runtime_guard_rejects_missing_or_skipped_real_ssh_coverage(self):
        script = next(step["run"] for step in self.workflow["jobs"]["checks"]["steps"]
                      if step["name"] == "Exercise real two-node SSH runtime and port collision fallback")
        script = script.replace("sudo python3 -", shlex.quote(sys.executable) + " -")
        directory = self.workspace / "cluster-run" / "tests"
        directory.mkdir(parents=True)
        path = directory / "test-master-runtime.py"
        complete = ("import unittest\nclass RealSshdTests(unittest.TestCase):\n"
                    "    def test_real_two_node_public_key(self): pass\n"
                    "    def test_real_port_collision(self): pass\n")
        variants = {
            "complete": (complete, 0),
            "missing_collision": (complete.replace("    def test_real_port_collision(self): pass\n", ""), 1),
            "skipped_two_node": (complete.replace("    def test_real_two_node_public_key(self): pass",
                                                   "    @unittest.skip('locked root')\n"
                                                   "    def test_real_two_node_public_key(self): pass"), 1),
            "class_skipped": (complete.replace("class RealSshdTests", "@unittest.skip('unavailable sshd')\nclass RealSshdTests"), 1),
            "setup_class_skipped": (complete + "    @classmethod\n    def setUpClass(cls):\n"
                                    "        raise unittest.SkipTest('unavailable sshd')\n", 1),
            "only_simulated": (complete.replace("RealSshdTests", "SimulatedClusterTests"), 1),
        }
        for name, (source, expected) in variants.items():
            with self.subTest(name=name):
                path.write_text(source)
                result = subprocess.run([self.bash, "-e", "-o", "pipefail", "-c", script],
                                        cwd=self.workspace, env=dict(self.env, PYTHONDONTWRITEBYTECODE="1"),
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                if expected:
                    self.assertIn("SSH", result.stderr)

    def test_ci_shell_validation_includes_untracked_scripts_but_not_python(self):
        script = next(step["run"] for step in self.workflow["jobs"]["checks"]["steps"]
                      if step["name"] == "Parse Bash scripts")
        self.assertNotIn("git ls-files", script)
        directory = self.workspace / "cluster-run" / "scripts"
        directory.mkdir(parents=True)
        (directory / "not shell.py").write_text("def python_only():\n    return {}\n")
        shell = directory / "untracked script.sh"
        shell.write_text("#!/bin/bash\ntrue\n")

        def parse():
            return subprocess.run([self.bash, "-e", "-o", "pipefail", "-c", script],
                                  cwd=self.workspace, env=self.env, capture_output=True, text=True, timeout=10)

        result = parse()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("untracked script.sh", result.stdout)
        self.assertNotIn("not shell.py", result.stdout)
        shell.write_text("if then\n")
        result = parse()
        self.assertNotEqual(result.returncode, 0, "An untracked invalid shell script must fail CI")

    def master_job(self):
        extension = "$" + "/" + ACTION.name
        jobs = [job for job in self.workflow["jobs"].values()
                if any(step.get("uses") == extension and step.get("with", {}).get("launch_mode") == "master"
                       for step in job["steps"])]
        self.assertEqual(len(jobs), 1)
        return jobs[0]

    def test_ci_exercises_real_signed_apt_and_networkless_cache_reuse(self):
        job = self.workflow["jobs"]["ssh-cache-check"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["needs"], "checks")
        self.assertNotIn("strategy", job)
        scripts = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("mirrors.aliyun.com", scripts)
        self.assertIn('"$image" /bin/bash /action/install-ssh.sh 2>&1', scripts)
        self.assertIn("/etc/ssl/certs/ca-certificates.crt:ro", scripts)
        self.assertNotIn("SOURCE_MODE", scripts)
        self.assertNotIn("127.0.0.1:1", scripts)
        for required in ("docker pull ubuntu:24.04", "docker image inspect", "--network none",
                         "/action/install-ssh.sh", "authenticated offline cache hit",
                         "published authenticated dependency cache", "/cache/openssh:ro"):
            self.assertIn(required, scripts)
        self.assertNotIn("--fake", scripts)

    def test_ci_master_matrix_and_trusted_runner_gate(self):
        job = self.master_job()
        self.assertEqual(job["needs"], ["checks", "ssh-cache-check"])
        self.assertEqual(job["runs-on"], "k8s-runner-group-cpu-thead")
        self.assertEqual(job["if"], "github.event_name == 'push' || github.event.pull_request.head.repo.full_name == github.repository")
        self.assertIs(job["strategy"]["fail-fast"], False)
        entries = {entry["label"]: entry for entry in job["strategy"]["matrix"]["include"]}
        self.assertEqual(set(entries), {"master-success", "master-command-fail", "worker-setup-fail", "master-timeout"})
        for label, expected in (("master-success", "succeeded"), ("master-command-fail", "failed"),
                                ("worker-setup-fail", "failed"), ("master-timeout", "timeout")):
            self.assertEqual(entries[label]["expected_status"], expected)
        launch = next(step for step in job["steps"] if step.get("id") == "master")
        self.assertIs(launch["continue-on-error"], True)
        self.assertEqual(launch["with"]["nnodes"], 2)
        self.assertEqual(launch["with"]["nproc_per_node"], 1)
        self.assertEqual(str(launch["with"]["ssh_port"]), "2222")
        self.assertEqual(launch["with"]["namespace"], "${{ env.TEST_NAMESPACE }}")
        self.assertEqual(launch["with"]["image"], "${{ env.WORKER_IMAGE }}")
        self.assertNotIn("container_options", launch["with"], "Do not force hostNetwork on shared infrastructure")
        probe = self.workflow["env"]["MASTER_SMOKE_COMMAND"]
        for required in ("cluster-exec", "CI_MASTER_SETUP", "WORLD_SIZE", "RANK", "known_hosts",
                         "authorized_keys", "identity", "master-command-once", ".port", "nodes"):
            self.assertIn(required, probe)

    def test_ci_master_expected_failures_require_actual_execution_evidence(self):
        step = next(step for step in self.master_job()["steps"] if step["name"] == "Assert master terminal state")
        proof = self.root / "proof"
        proof.mkdir()
        env = dict(self.env, PROOF_DIR=str(proof), REQUIRED_SCENARIO="worker-setup-fail",
                   ALLOCATION_STATUS="failed", ALLOCATION_OUTCOME="failure",
                   REQUIRED_STATUS="failed", REQUIRED_OUTCOME="failure", EXECUTION_CARD_HOURS="0.0010")
        result = self.shell(step["run"], env)
        self.assertNotEqual(result.returncode, 0, "A package-download failure must not pass the worker setup test")
        (proof / "setup-1").write_text("setup-1")
        (proof / "worker-setup-fault").write_text("7")
        result = self.shell(step["run"], env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (proof / "master-command-reached").write_text("reached")
        result = self.shell(step["run"], env)
        self.assertNotEqual(result.returncode, 0, "Worker setup failure must prevent the master command")
        env["REQUIRED_SCENARIO"] = "master-command-fail"
        (proof / "setup-0").write_text("setup-0")
        result = self.shell(step["run"], env)
        self.assertNotEqual(result.returncode, 0, "Master failure must be the intended command failure")
        (proof / "master-command-fault").write_text("7")
        result = self.shell(step["run"], env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        env.update(REQUIRED_SCENARIO="master-timeout", ALLOCATION_STATUS="timeout", REQUIRED_STATUS="timeout")
        result = self.shell(step["run"], env)
        self.assertNotEqual(result.returncode, 0, "Scheduling or setup timeout must not pass the command timeout test")
        (proof / "timeout-command-started").write_text("started")
        result = self.shell(step["run"], env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ci_master_evidence_cleanup_stays_within_its_owned_directory(self):
        step = next(step for step in self.master_job()["steps"] if step["name"] == "Remove master execution evidence")
        prefix = str(self.root / "master-ci-")
        script = step["run"].replace("/wl_nas/devops/master-ci-", prefix)
        owned = self.root / "master-ci-123-1-ABC123"
        other = self.root / "other-proof"
        for path in (owned, other):
            path.mkdir()
        env = dict(self.env, GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="1", PROOF_DIR=str(other))
        result = self.shell(script, env)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(other.exists())
        result = self.shell(script, dict(env, PROOF_DIR=str(owned)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(owned.exists())
        self.assertTrue(other.exists())

    def test_ci_master_secret_authorization_denial_fails_instead_of_skipping(self):
        self.fake("kubectl")
        self.fake("timeout")
        script = next(step["run"] for step in self.master_job()["steps"]
                      if step["name"] == "Require Secret create and delete permissions")
        # Keep the fixture from accidentally selecting a real runner's /opt/bin/kubectl.
        script = script.replace("/opt/bin/kubectl", str(self.root / "absent-kubectl"))
        for denied, expected in ((None, 0), ("create", 1), ("delete", 1), ("get", 1)):
            with self.subTest(denied=denied):
                self.fixture(denied_secret_verb=denied)
                result = self.shell(script, dict(self.env, TEST_NAMESPACE="fixture"))
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                if denied:
                    self.assertIn("::error::Master CI requires", result.stdout)
                    self.assertIn(denied, result.stdout)

    def test_ci_master_cleanup_rejects_remaining_resources_and_query_failures(self):
        self.fake("kubectl")
        self.fake("timeout")
        script = next(step["run"] for step in self.master_job()["steps"]
                      if step["name"] == "Require full master resource cleanup")
        runtime = self.nas / "runtime"
        runtime.mkdir()
        script = script.replace("/wl_nas/devops/runtime/master", str(runtime))
        env = dict(self.env, TEST_NAMESPACE="fixture", ALLOCATION_NAME="fixture-job")
        for resource in ("svc", "podgroups.scheduling.x-k8s.io", "secret"):
            with self.subTest(resource=resource):
                self.fixture(remaining_resource=resource)
                result = self.shell(script, env)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.fixture(resource_query_status=37)
        result = self.shell(script, env)
        self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
        self.fixture()
        result = self.shell(script, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (runtime / "unrelated-job-id").mkdir()
        result = self.shell(script, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (runtime / "fixture-job-attempt-nonce").mkdir()
        result = self.shell(script, env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_ci_runs_real_zw_810e_workload(self):
        extension = "$/" + ACTION.name
        for job in self.workflow["jobs"].values():
            entries = job.get("strategy", {}).get("matrix", {}).get("include", [])
            for entry in entries:
                board_key = next((key for key, value in entry.items() if value == "ZW-810E"), None)
                if board_key is None:
                    continue
                expected_selector = "board-type=${{ matrix." + board_key + " }}"
                action_steps = [step for step in job["steps"] if step.get("uses") == extension]
                self.assertTrue(action_steps)
                self.assertTrue(any(step.get("with", {}).get("node_selector") == expected_selector
                                    for step in action_steps))
                return
        self.fail("expected a real ZW-810E action workload in the CI matrix")

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
        scripts += [self.workflow["env"][name] for name in ("SMOKE_COMMAND", "MASTER_SMOKE_COMMAND")]
        scripts += [entry[key] for entry in self.master_job()["strategy"]["matrix"]["include"]
                    for key in ("setup", "workload")]
        for script in scripts:
            result = subprocess.run([self.bash, "-n"], input=script, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fake":
        raise SystemExit(fake_command(sys.argv[2], sys.argv[3:]))
    unittest.main()
