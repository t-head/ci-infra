#!/usr/bin/env python3
"""Offline runtime tests. Real sshd tests require Linux root and preinstalled tools."""
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
FILES = ("master-worker.sh", "master-session.sh", "cluster-ssh.sh", "cluster-exec.sh")
THIS = Path(__file__).resolve()
BASH = "/bin/bash"
SCRATCH_NAMES = ("entry", "name", "value", "temp", "rank", "rc", "task", "command_pid", "child",
                 "now", "file", "limit", "size", "path", "create", "part", "current", "oldifs",
                 "root", "listing", "mode", "links", "uid", "gid", "rest", "chown", "host",
                 "preferred", "master", "low", "high", "seed", "offset", "port", "emitted",
                 "phase", "target", "reply", "budget", "type", "key", "comment", "listener",
                 "i", "bind", "next", "pid", "children", "session", "health_now", "health_last")


def environment_file(path):
    return dict(entry.split(b"=", 1) for entry in path.read_bytes().split(b"\0") if entry)


def event(root, **values):
    with (root / "events").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(dict(order=handle.tell(), time=time.time(), **values)) + "\n")


def fake(tool, root, rank, args):
    runtime = root / f"node-{rank}" / "runtime"
    if tool == "timeout":
        limit = float(args[2].rstrip("s"))
        try:
            return subprocess.run(args[3:], timeout=limit).returncode
        except subprocess.TimeoutExpired:
            return 124
    if tool == "date":
        assert args == ["+%s.%N"]
        now = time.time_ns()
        print(f"{now // 1000000000}.{now % 1000000000:09d}")
        return 0
    if tool == "sshd":
        if "-t" in args:
            return 0
        config = Path(args[args.index("-f") + 1]).read_text().splitlines()
        entries = dict(line.split(" ", 1) for line in config)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", int(entries["Port"]) + rank * 10000))
            listener.listen(64)
        except OSError:
            return 255
        event(root, kind="bound", rank=rank, port=int(entries["Port"]), pid=os.getpid())
        while True:
            connection, _ = listener.accept()
            connection.close()
    if tool == "ssh":
        pos = args.index("--")
        host = args[pos + 1].removeprefix("root@")
        port = int(args[args.index("-p") + 1])
        intent = " ".join(args[pos + 2:])
        event(root, kind="ssh", rank=rank, host=host, port=port, intent=intent)
        target = next((i for i in range(2) if host == f"127.0.0.{i + 2}"), None)
        if target is None:
            return 255
        try:
            with socket.create_connection(("127.0.0.1", port + target * 10000), timeout=0.3):
                pass
        except OSError:
            return 255
        target_dir = root / f"node-{target}"
        env = json.loads((target_dir / "env.json").read_text())
        env["SSH_ORIGINAL_COMMAND"] = intent
        return subprocess.run([BASH, "--noprofile", "--norc",
                               str(target_dir / "runtime" / "master-session.sh")], env=env).returncode
    if tool == "capture":
        print(json.dumps(args))
        return 0
    if tool == "event":
        event(root, kind=args[0], rank=rank, details=args[1:])
        return 0
    raise ValueError(tool)


def executable(path, text):
    path.write_text(text)
    path.chmod(0o700)


def wrapper(tool, root, rank):
    return ("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(THIS))
            + " --fake " + shlex.quote(tool) + " " + shlex.quote(str(root))
            + " " + str(rank) + ' "$@"\n')


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("jq") or not shutil.which("ssh-keygen"):
            raise unittest.SkipTest("jq and ssh-keygen are required (no network installation)")
        cls.keys = tempfile.TemporaryDirectory(prefix="ppu-master-keys-")
        cls.keydir = Path(cls.keys.name)
        for name in ("identity", "host-0", "host-1"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f",
                            str(cls.keydir / name)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        # StrictModes rejects /tmp's writable ancestor for real AuthorizedKeysFile.
        self.temp = tempfile.TemporaryDirectory(prefix="ppu-master-", dir="/run" if getattr(self, "real_daemon", False) else None)
        self.root = Path(self.temp.name).resolve()
        self.registry_root = self.root / "registry"
        self.procs = []
        self.logs = []
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS")
                    and not k.startswith(("GITHUB_", "ACTIONS_", "RUNNER_", "SSH_"))}
        self.env.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin", PPU_DEADLINE=str(int(time.time()) + 40))
        self.addCleanup(self.dispose)

    def dispose(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        # Tests also run on macOS, where production's Linux /proc traversal is unavailable.
        for record in self.events():
            if record["kind"] == "bound":
                try:
                    os.kill(record["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for log in self.logs:
            log.close()
        self.temp.cleanup()

    def events(self):
        path = self.root / "events"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def stage(self, rank=0, real=False):
        # Model the fixed, infra-provisioned NAS root; runtime must not create it.
        self.registry_root.mkdir(mode=0o700, exist_ok=True)
        node = self.root / f"node-{rank}"
        runtime = node / "runtime"
        config = node / "config"
        source = node / "source"
        tools = node / "tools"
        for path in (runtime, config, source, tools, runtime / "sessions", runtime / "bin"):
            path.mkdir(parents=True, exist_ok=True)
        replacements = {
            "/run/cluster-config": str(config),
            "/run/cluster": str(runtime),
            "/mnt/wl_nas/devops/runtime/master": str(self.registry_root),
            # Also applied to real Linux tests: never write the host's /dev.
            "/dev/termination-log": str(node / "termination-log"),
        }
        if not real:
            replacements["/run/sshd"] = str(node / "privsep")
            for command in ("ssh", "sshd", "timeout", "date"):
                executable(tools / command, wrapper(command, self.root, rank))
            replacements.update({"/bin/date +%s.%N": str(tools / "date") + " +%s.%N",
                                 "/usr/bin/ssh ": str(tools / "ssh") + " ",
                                 "/usr/sbin/sshd ": str(tools / "sshd") + " ",
                                 "/usr/bin/timeout ": str(tools / "timeout") + " ",
                                 "command -v timeout": "command -v " + str(tools / "timeout")})
        for name in FILES:
            text = (SCRIPTS / name).read_text()
            for old, new in replacements.items():
                text = text.replace(old, new)
            executable(config / name, text)
            executable(runtime / name, text)
        for name in ("identity", "identity.pub", "host-0.pub", "host-1.pub", f"host-{rank}"):
            shutil.copyfile(self.keydir / name, config / name)
        (config / "job-id").write_text("job-independent-123\n")
        (config / "nodes.json").write_text(json.dumps([
            {"rank": i, "host": f"127.0.0.{i + 2}", "cards": 2} for i in range(2)]))
        (config / "install-ssh.sh").write_text("#!/bin/bash\nexit 0\n")
        env = dict(self.env, NNODES="2", NODE_RANK=str(rank), RANK=str(rank), WORLD_SIZE="2",
                   NPROC_PER_NODE="2", MASTER_ADDR="127.0.0.2", MASTER_PORT="29500",
                   POD_IP=f"127.0.0.{rank + 2}", HOST_IP=f"127.0.0.{rank + 2}",
                   POD_UID=f"pod-{rank}", SOURCE_DIR=str(source), PPU_SSH_PORT="2222")
        (node / "env.json").write_text(json.dumps(env))
        (runtime / "setup.sh").write_text(":\n")
        (runtime / "command.sh").write_text(":\n")
        executable(tools / "event", wrapper("event", self.root, rank))
        return node, runtime, config, source, tools, env

    def bash(self, script, env=None, stdin=None, timeout=10):
        return subprocess.run([BASH, "--noprofile", "--norc", "-c", script], env=env or self.env,
                              input=stdin, text=True, capture_output=True, timeout=timeout)

    def library(self, code, env=None):
        return self.bash("source " + shlex.quote(str(SCRIPTS / "master-worker.sh")) + "\n" + code, env)

    def registry(self, runtime, code, env=None):
        return self.bash("source " + shlex.quote(str(runtime / "master-worker.sh"))
                         + "\numask 077\nCLUSTER_JOB_ID=job-independent-123\nCLUSTER_REGISTRY="
                         + shlex.quote(str(self.registry_root / "job-independent-123" / "nodes"))
                         + "\nCLUSTER_HOST=127.0.0.2\nCLUSTER_PORT=2222\n" + code, env)

    def registry_chown_tool(self, runtime, tools, text):
        tool = tools / "chown"
        executable(tool, text)
        script = runtime / "master-worker.sh"
        script.write_text(script.read_text().replace("/bin/chown", str(tool))
                          .replace("/usr/sbin/chown", str(tool)))
        return tool

    def start(self, node, env):
        log = (node / "output").open("w+")
        self.logs.append(log)
        proc = subprocess.Popen([BASH, str(node / "config" / "master-worker.sh")], env=env,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        self.procs.append(proc)
        return proc

    def await_condition(self, predicate, timeout=15):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return
            if self.procs and all(p.poll() is not None for p in self.procs):
                break
            time.sleep(0.02)
        output = "\n".join((p.parent / "output").read_text() for p in self.root.glob("node-*/runtime")
                           if (p.parent / "output").exists())
        states = [str(path) + ": " + path.read_text() for path in self.root.glob("node-*/runtime/status.json")]
        self.fail("condition not reached: " + output + "\n" + "\n".join(states))

    def assert_termination(self, node, env, outcome):
        expected = dict(source="cluster-master-runtime", outcome=outcome,
                        deadline=int(env["PPU_DEADLINE"]), rank=int(env["NODE_RANK"]))
        finished = node / "runtime/command.finished"
        if outcome == "command" and finished.exists():
            value = finished.read_text().strip()
            self.assertRegex(value, r"^[0-9]+\.[0-9]{9}$")
            expected.update(command_finished_at=value,
                            exit_code=int((node / "runtime/command.exit").read_text()))
        self.assertEqual(json.loads((node / "termination-log").read_text()), expected)

    def wait_cluster(self, procs, expected=(0, 143), timeout=30):
        try:
            codes = []
            for index, process in enumerate(procs):
                if index and expected[index] == 143 and process.poll() is None:
                    self.assertTrue((self.root / f"node-{index}" / "runtime" / "ready").exists())
                    process.terminate()
                codes.append(process.wait(timeout=timeout))
        except subprocess.TimeoutExpired:
            diagnostics = []
            for node in sorted(self.root.glob("node-*")):
                for name in ("output", "runtime/status.json", "runtime/sshd.log"):
                    path = node / name
                    if path.exists():
                        diagnostics.append(str(path) + "\n" + path.read_text())
            diagnostics.append(json.dumps(self.events()[-10:]))
            self.fail("cluster did not terminate:\n" + "\n".join(diagnostics))
        output = "\n".join((p / "output").read_text() for p in sorted(self.root.glob("node-*")))
        self.assertEqual(codes, list(expected), output)


class RuntimeUnitTests(Fixture):
    def test_bash32_syntax(self):
        for name in FILES:
            subprocess.run([BASH, "-n", str(SCRIPTS / name)], check=True)

    def test_runtime_scratch_locals_use_reserved_namespace(self):
        for name in ("master-worker.sh", "master-session.sh"):
            for line in (SCRIPTS / name).read_text().splitlines():
                if line.lstrip().startswith("local "):
                    for declaration in line.split()[1:]:
                        if not declaration.startswith("-"):
                            self.assertTrue(declaration.startswith("CLUSTER_"), name + ": " + line)

    def test_candidates_bound_exclusions_and_fallback(self):
        result = self.library("cluster_port_candidates 2222 20003 20005 20007 0")
        ports = [int(p) for p in result.stdout.splitlines()]
        self.assertEqual(len(ports), 128)
        self.assertEqual(ports[0], 2222)
        self.assertEqual(len(set(ports)), 128)
        self.assertFalse(set(ports) & {20003, 20005, 20006, 20007})
        result = self.library("cluster_port_candidates 2222 2222 20000 24999 0")
        self.assertEqual(result.stdout, "")

    def test_registry_identity_size_and_symlinks(self):
        record = dict(jobid="job", rank=1, host="worker.example", port=2222,
                      pod_uid="uid-1", phase="ready")
        file = self.root / "node.json"
        command = ('CLUSTER_JOB_ID=job\ncluster_registry_read 1 '
                   + shlex.quote(str(file)) + ' worker.example')
        file.write_text(json.dumps(record))
        self.assertEqual(self.library(command).returncode, 0)
        for key, value in (("jobid", "stale"), ("rank", 0), ("host", "evil.example"),
                           ("port", 2.5), ("port", 70000), ("pod_uid", "../escape"),
                           ("phase", "READY")):
            file.write_text(json.dumps(dict(record, **{key: value})))
            self.assertNotEqual(self.library(command).returncode, 0, key)
        file.write_text(json.dumps(record) + " " * 4096)
        self.assertNotEqual(self.library(command).returncode, 0)
        file.unlink()
        target = self.root / "target"
        target.write_text(json.dumps(record))
        file.symlink_to(target)
        self.assertNotEqual(self.library(command).returncode, 0)
        directory = self.root / "linkdir"
        directory.symlink_to(self.root, target_is_directory=True)
        self.assertNotEqual(self.library("cluster_safe_dir " + shlex.quote(str(directory / "child"))).returncode, 0)

    def test_registry_inherits_fixed_owner_only_for_job_nodes_and_metadata(self):
        _, runtime, _, _, tools, env = self.stage()
        root = self.root / "registry"
        other = root / "other-job" / "nodes"
        other.mkdir(parents=True, mode=0o700)
        (other / "1.json").write_text('{"phase":"ready"}\n')
        cache = self.root / "cache" / "openssh"
        cache.mkdir(parents=True, mode=0o755)
        key = cache / "identity"
        key.write_text("private-key-must-not-change\n")
        key.chmod(0o600)
        protected = [self.root, root, other.parent, other, other / "1.json", cache, key]
        snapshot = {p: (p.stat().st_uid, p.stat().st_gid, p.stat().st_mode,
                        p.read_bytes() if p.is_file() else None) for p in protected}
        trace = self.root / "chown-calls"
        self.registry_chown_tool(runtime, tools,
                                 "#!/bin/sh\nprintf '%s\\n' \"$@\" >> " + shlex.quote(str(trace))
                                 + "\nexec " + shlex.quote(shutil.which("chown")) + ' "$@"\n')
        owner = root.stat().st_uid, root.stat().st_gid
        # Neither shell input nor a cached owner may determine NAS ownership.
        result = self.registry(runtime, "CLUSTER_REGISTRY_OWNER=1234:1234\ncluster_registry_initialize", env)
        self.assertEqual(result.returncode, 0, result.stderr)
        nodes = root / "job-independent-123" / "nodes"
        for path in (nodes.parent, nodes):
            self.assertEqual((path.stat().st_uid, path.stat().st_gid), owner)
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        for phase in ("online", "ready", "running", "done"):
            result = self.registry(runtime, "CLUSTER_REGISTERED=1\nCLUSTER_REGISTRY_OWNER=1234:1234\n"
                                   + "cluster_status " + phase, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            record = nodes / "0.json"
            self.assertEqual((record.stat().st_uid, record.stat().st_gid), owner)
            self.assertEqual(record.stat().st_mode & 0o777, 0o600)
            values = json.loads(record.read_text())
            self.assertEqual(set(values), {"jobid", "rank", "host", "port", "pod_uid", "phase"})
            self.assertEqual(values["phase"], phase)
        calls = trace.read_text().splitlines()
        owner_arg = f"{owner[0]}:{owner[1]}"
        self.assertEqual(calls[:3], [owner_arg, str(nodes.parent), str(nodes)])
        self.assertEqual(len(calls), 11)
        for index in range(3, len(calls), 2):
            self.assertEqual(calls[index], owner_arg)
            self.assertTrue(calls[index + 1].startswith(str(nodes / ".0.")))
        self.assertEqual(list(nodes.glob(".0.*")), [])
        for path, before in snapshot.items():
            self.assertEqual((path.stat().st_uid, path.stat().st_gid, path.stat().st_mode,
                              path.read_bytes() if path.is_file() else None), before, str(path))

    def test_registry_root_required_fixed_path_and_symlinks_rejected(self):
        _, runtime, _, _, _, env = self.stage()
        root = self.root / "registry"
        root.rmdir()
        result = self.registry(runtime, "cluster_registry_initialize", env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pre-provisioned", result.stderr)
        self.assertFalse(root.exists())
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        sentinel = outside / "identity"
        sentinel.write_text("unchanged")
        before = outside.stat().st_uid, outside.stat().st_gid, outside.stat().st_mode
        for relative in ("", "job-independent-123", "job-independent-123/nodes"):
            with self.subTest(symlink=relative):
                if relative:
                    root.mkdir(exist_ok=True)
                link = root / relative if relative else root
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(outside, target_is_directory=True)
                result = self.registry(runtime, "cluster_registry_initialize", env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("symlinks", result.stderr)
                self.assertEqual((outside.stat().st_uid, outside.stat().st_gid, outside.stat().st_mode), before)
                self.assertEqual(sentinel.read_text(), "unchanged")
                link.unlink()
        wrong = root / "other-job" / "nodes"
        result = self.registry(runtime, "CLUSTER_REGISTRY=" + shlex.quote(str(wrong))
                               + "\ncluster_registry_initialize", env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid registry path", result.stderr)
        self.assertFalse(wrong.exists())
        result = self.registry(runtime, "CLUSTER_JOB_ID=../escape\ncluster_registry_initialize", env)
        self.assertNotEqual(result.returncode, 0)
        ancestor = self.root / "linked-parent"
        ancestor.symlink_to(self.root, target_is_directory=True)
        linked_root = ancestor / "registry"
        script = runtime / "master-worker.sh"
        script.write_text(script.read_text().replace(str(root), str(linked_root)))
        result = self.registry(runtime, "CLUSTER_REGISTRY="
                               + shlex.quote(str(linked_root / "job-independent-123" / "nodes"))
                               + "\ncluster_registry_initialize", env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinks", result.stderr)

    def test_registry_unreadable_or_nonnumeric_owner_fails_before_creation(self):
        _, runtime, _, _, tools, env = self.stage()
        tool = tools / "ls"
        script = runtime / "master-worker.sh"
        script.write_text(script.read_text().replace("/bin/ls", str(tool)))
        for body in ("exit 1\n", "printf 'drwx------ 1 not-numeric 1001 0 registry\\n'\n"):
            with self.subTest(body=body):
                executable(tool, "#!/bin/sh\n" + body)
                result = self.registry(runtime, "cluster_registry_initialize", env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("registry root ownership", result.stderr)
                self.assertFalse((self.root / "registry" / "job-independent-123").exists())

    def test_registry_chown_failure_and_symlink_target_never_publish(self):
        node, runtime, config, _, tools, env = self.stage()
        result = self.registry(runtime, "cluster_registry_initialize\nCLUSTER_REGISTERED=1\ncluster_status online", env)
        self.assertEqual(result.returncode, 0, result.stderr)
        nodes = self.root / "registry" / "job-independent-123" / "nodes"
        record = nodes / "0.json"
        before = record.read_bytes()
        tool = self.registry_chown_tool(runtime, tools, "#!/bin/sh\nexit 1\n")
        for code in ("cluster_registry_initialize", "CLUSTER_REGISTERED=1\ncluster_status ready"):
            result = self.registry(runtime, code, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot assign registry ownership", result.stderr)
            self.assertIn("root squash", result.stderr)
            self.assertEqual(record.read_bytes(), before)
            self.assertEqual(list(nodes.glob(".0.*")), [])
        shutil.copyfile(runtime / "master-worker.sh", config / "master-worker.sh")
        process = self.start(node, env)
        self.assertEqual(process.wait(timeout=5), 65)
        self.assertIn("cannot assign registry ownership", (node / "output").read_text())
        self.assertFalse((runtime / "listener.pid").exists())
        executable(tool, "#!/bin/sh\nexec " + shlex.quote(shutil.which("chown")) + ' "$@"\n')
        outside = self.root / "identity"
        outside.write_bytes(b"private-key")
        outside.chmod(0o600)
        record.unlink()
        record.symlink_to(outside)
        result = self.registry(runtime, "CLUSTER_REGISTERED=1\ncluster_status ready", env)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(record.is_symlink())
        self.assertEqual(outside.read_bytes(), b"private-key")
        self.assertEqual(outside.stat().st_mode & 0o777, 0o600)

    def test_trusted_nodes_validation(self):
        _, _, config, _, _, env = self.stage()
        path = config / "nodes.json"
        self.assertEqual(self.library("cluster_validate_nodes " + shlex.quote(str(path)), env).returncode, 0)
        for nodes in ([{"rank": 0, "host": "-unsafe", "cards": 2}],
                      [{"rank": 1, "host": "worker.example", "cards": 2}] * 2,
                      [{"rank": i, "host": "same.example", "cards": 2} for i in range(2)]):
            path.write_text(json.dumps(nodes))
            self.assertNotEqual(self.library("cluster_validate_nodes " + shlex.quote(str(path)), env).returncode, 0)

    def session_fixture(self):
        node, runtime, config, source, tools, env = self.stage()
        (runtime / "metadata.env").write_bytes(b"RANK=0\0NODE_RANK=0\0WORLD_SIZE=2\0NNODES=2\0SOURCE_DIR="
                                                + str(source).encode() + b"\0")
        (runtime / "environment.env").write_bytes(
            b"VALUE=line1\n'$(touch SHOULD_NOT_EXIST)'\nline3\0EMPTY=\0RANK=99\0"
            b"GITHUB_TOKEN=secret\0ACTIONS_TOKEN=secret\0RUNNER_TOKEN=secret\0SSH_AUTH_SOCK=evil\0"
            b"BASH_ENV=/evil\0ENV=/evil\0PATH=/usr/bin:/bin\0intent=printf WRONG_COMMAND\0"
            b"CLUSTER_SESSION_INTENT=printf WRONG_COMMAND\0")
        return node, runtime, source, env

    def test_session_environment_shellintent_stdin_and_probe(self):
        node, runtime, source, env = self.session_fixture()
        command = ('printf "%s\\0" "$VALUE" "$EMPTY" "$RANK" "$WORLD_SIZE" "$PWD" "$PATH"; '
                   'env | /usr/bin/jq -Rs . >/dev/null; /bin/cat')
        result = subprocess.run([BASH, str(runtime / "master-session.sh")],
                                env=dict(env, SSH_ORIGINAL_COMMAND=command, GITHUB_TOKEN="inherited"),
                                input=b"stdin\nwith 'quotes'\n", capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = result.stdout.split(b"\0")
        self.assertEqual(values[:5], [b"line1\n'$(touch SHOULD_NOT_EXIST)'\nline3", b"", b"0", b"2", str(source).encode()])
        self.assertTrue(values[5].startswith(str(runtime / "bin").encode() + b":"))
        self.assertEqual(values[6], b"stdin\nwith 'quotes'\n")
        self.assertFalse((source / "SHOULD_NOT_EXIST").exists())
        self.assertEqual(result.stderr, b"")
        command = 'printf "%s" "${GITHUB_TOKEN-unset}|${ACTIONS_TOKEN-unset}|${RUNNER_TOKEN-unset}|${SSH_AUTH_SOCK-unset}|${BASH_ENV-unset}|${ENV-unset}"'
        result = subprocess.run([BASH, str(runtime / "master-session.sh")],
                                env=dict(env, SSH_ORIGINAL_COMMAND=command), capture_output=True, timeout=5)
        self.assertEqual(result.stdout, b"unset|unset|unset|unset|unset|unset")
        (runtime / "environment.env").unlink()
        (runtime / "status.json").write_text('{"phase":"online"}\n')
        result = subprocess.run([BASH, str(runtime / "master-session.sh")],
                                env=dict(env, SSH_ORIGINAL_COMMAND="__ppu_master_probe_v1__"), capture_output=True)
        self.assertEqual(result.stdout, b'{"phase":"online"}\n')
        self.assertEqual(result.stderr, b"")

    def test_helpers_preserve_business_names_and_active_reserved_locals(self):
        _, runtime, _, _, _, env = self.stage()
        expected = {name: ("" if index % 2 else "current\n'$(touch BAD)'\n")
                    for index, name in enumerate(SCRATCH_NAMES)}
        (runtime / "metadata.env").write_bytes(b"")
        (runtime / "input.env").write_bytes(b"".join(
            name.encode() + b"=" + value.encode() + b"\0" for name, value in expected.items()))
        env.update({name: "old-pod-value" for name in expected})
        env.update(CLUSTER_ACTIVE="old-export", CLUSTER_LOAD_NAME="old-export",
                   CLUSTER_SAVE_ENTRY="old-export", CLUSTER_SAVE_TEMP="old-export")
        code = ("outer() { local CLUSTER_ACTIVE=still-live; cluster_env_clean; "
                '[[ $CLUSTER_ACTIVE == still-live ]] || return 91; '
                "cluster_env_load_file " + shlex.quote(str(runtime / "input.env")) + "; "
                "cluster_env_save; }; outer")
        result = self.registry(runtime, code, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = environment_file(runtime / "environment.env")
        for name, value in expected.items():
            self.assertEqual(snapshot[name.encode()], value.encode(), name)
        self.assertFalse(any(name.startswith(b"CLUSTER_") for name in snapshot))
        self.assertFalse((runtime / "BAD").exists())

    def test_session_business_names_and_reserved_export_attributes(self):
        node, runtime, _, env = self.session_fixture()
        expected = {name: ("" if index % 2 else "session\n'quoted'\n")
                    for index, name in enumerate(SCRATCH_NAMES)}
        with (runtime / "environment.env").open("ab") as handle:
            for name, value in expected.items():
                handle.write(name.encode() + b"=" + value.encode() + b"\0")
        env.update({name: "old-pod-value" for name in expected})
        env.update(CLUSTER_SESSION_INTENT="exported-wrong-command", CLUSTER_SESSION_CHILD="123",
                   CLUSTER_SESSION_RC="99", CLUSTER_CLEAN_ENTRY="exported-old-entry")
        for intent, stdin in (("/usr/bin/env -0", None), ("", b"/usr/bin/env -0\n")):
            with self.subTest(intent=intent):
                result = subprocess.run([BASH, str(runtime / "master-session.sh")],
                                        env=dict(env, SSH_ORIGINAL_COMMAND=intent), input=stdin,
                                        capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                values = dict(entry.split(b"=", 1) for entry in result.stdout.split(b"\0") if entry)
                for name, value in expected.items():
                    self.assertEqual(values[name.encode()], value.encode(), name)
                self.assertEqual({name for name in values if name.startswith(b"CLUSTER_")}, set())
                self.assertEqual(list((runtime / "sessions").iterdir()), [])
        self.assertFalse((node / "termination-log").exists())

    def capture_fixture(self):
        _, runtime, config, _, tools, env = self.stage()
        executable(tools / "capture", wrapper("capture", self.root, 0))
        script = runtime / "cluster-ssh.sh"
        script.write_text(script.read_text().replace(str(tools / "ssh") + " ", str(tools / "capture") + " "))
        nodes = [{"rank": i, "host": f"node-{i}.example", "cards": 2, "port": 21000 + i,
                  "pod_ip": f"10.0.0.{i + 1}", "host_ip": f"10.1.0.{i + 1}"} for i in range(2)]
        (runtime / "nodes.json").write_text(json.dumps(nodes))
        (runtime / "trusted-nodes.json").write_text(json.dumps(nodes))
        return runtime, env

    def test_ssh_safe_args_aliases_and_rejected_overrides(self):
        runtime, env = self.capture_fixture()
        command = [BASH, str(runtime / "cluster-ssh.sh")]
        result = subprocess.run(command + ["-T", "-o", "BatchMode yes", "-l", "root", "10.1.0.2", "echo hello"],
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(result.stdout)
        self.assertEqual(args[-3:], ["--", "root@node-1.example", "echo hello"])
        self.assertEqual(args[args.index("-p") + 1], "21001")
        self.assertIn("HostKeyAlias=cluster-rank-1", args)
        for options in (["-p", "22"], ["-p22"], ["-i", "/evil"], ["-F", "/evil"],
                        ["-oProxyCommand=touch evil"], ["-o", "StrictHostKeyChecking=no"],
                        ["-oHostName=evil"], ["-oUserKnownHostsFile=/dev/null"], ["-oIdentityAgent=evil"],
                        ["-L", "2222:evil:22"], ["-X"], ["-J", "evil"], ["-oSendEnv=*"], ["-oControlPath=/evil"]):
            result = subprocess.run(command + options + ["node-0.example"], env=env, capture_output=True)
            self.assertEqual(result.returncode, 64, options)
        result = subprocess.run(command + ["nobody@node-0.example"], env=env, capture_output=True)
        self.assertEqual(result.returncode, 64)
        mapping = json.loads((runtime / "nodes.json").read_text())
        mapping[0]["host_ip"] = mapping[1]["host_ip"]
        (runtime / "nodes.json").write_text(json.dumps(mapping))
        result = subprocess.run(command + [mapping[1]["host_ip"]], env=env, capture_output=True)
        self.assertEqual(result.returncode, 64)

    def test_exec_posix_quoting_empty_newlines_and_stdin(self):
        runtime, env = self.capture_fixture()
        args = [sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:])); print(sys.stdin.read(),end='')",
                "", "a'b", "line1\nline2", "$HOME;$(touch NO)", "x\\y", " spaced "]
        for separator in ([], ["--"]):
            with self.subTest(separator=separator):
                result = subprocess.run([BASH, str(runtime / "cluster-exec.sh"), "node-1.example", *separator, *args],
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                wire = json.loads(result.stdout)
                result = subprocess.run(["/bin/sh", "-c", wire[-1]], input="stdin\n'quoted'", capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                line, stdin = result.stdout.split("\n", 1)
                self.assertEqual(json.loads(line), args[3:])
                self.assertEqual(stdin, "stdin\n'quoted'")
                self.assertFalse((self.root / "NO").exists())

    def test_probe_requires_live_job_rank_host_port_and_uid(self):
        _, runtime, _, _, tools, env = self.stage()
        record = dict(jobid="job", rank=1, host="node-1.example", port=2222,
                      pod_uid="uid-1", phase="ready", exit_code=0,
                      pod_ip="10.0.0.2", host_ip="10.1.0.2")
        command = ('source ' + shlex.quote(str(runtime / "master-worker.sh"))
                   + '\nCLUSTER_JOB_ID=job\ncluster_probe 1 node-1.example 2222 uid-1')
        for changed in ({}, {"jobid": "stale"}, {"rank": 0}, {"host": "evil.example"},
                        {"port": 22}, {"pod_uid": "stale-uid"}, {"phase": "READY"}):
            reply = json.dumps(dict(record, **changed))
            executable(tools / "ssh", "#!/bin/sh\nprintf '%s\\n' " + shlex.quote(reply) + "\n")
            result = self.bash(command, env)
            self.assertEqual(result.returncode, 1 if changed else 0, changed)

    def test_deadline_and_term_bound_blocked_startup(self):
        for terminate in (False, True):
            node, runtime, config, _, _, env = self.stage(rank=int(terminate))
            (config / "install-ssh.sh").write_text("/bin/sleep 60\n")
            if not terminate:
                env["PPU_DEADLINE"] = str(int(time.time()) + 2)
            start = time.monotonic()
            proc = self.start(node, env)
            if terminate:
                time.sleep(0.2)
                proc.terminate()
            self.assertEqual(proc.wait(timeout=6), 143 if terminate else 124)
            self.assertLess(time.monotonic() - start, 5)
            self.assert_termination(node, env, "runtime" if terminate else "timeout")
            self.assertEqual((runtime / "deadline-exceeded").exists(), not terminate)

    def test_expired_startup_and_child_deadline_marker(self):
        node, runtime, _, _, _, env = self.stage()
        env["PPU_DEADLINE"] = str(int(time.time()) - 1)
        proc = self.start(node, env)
        self.assertEqual(proc.wait(timeout=5), 124)
        self.assert_termination(node, env, "timeout")
        self.assertTrue((runtime / "deadline-exceeded").is_file())
        self.assertFalse((runtime / "listener.pid").exists())
        (runtime / "deadline-exceeded").unlink()
        message = (node / "termination-log").read_bytes()
        result = self.registry(runtime, "cluster_check_deadline", env)
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertTrue((runtime / "deadline-exceeded").is_file())
        self.assertEqual((node / "termination-log").read_bytes(), message)
        self.assertEqual(list(self.registry_root.rglob("deadline-exceeded")), [])

    def test_installer_exit124_before_deadline_is_runtime_failure(self):
        node, runtime, config, _, _, env = self.stage()
        (config / "install-ssh.sh").write_text("exit 124\n")
        proc = self.start(node, env)
        self.assertEqual(proc.wait(timeout=5), 124)
        self.assert_termination(node, env, "runtime")
        self.assertFalse((runtime / "deadline-exceeded").exists())
        self.assertFalse((runtime / "command.exit").exists())

    def test_marker_and_termination_write_failures_are_reported(self):
        node, runtime, _, _, _, env = self.stage()
        env["PPU_DEADLINE"] = str(int(time.time()) - 1)
        # A directory reliably fails even under Linux root, without chmod grants.
        (runtime / "deadline-exceeded").mkdir()
        result = self.registry(runtime, "cluster_check_deadline", env)
        self.assertEqual(result.returncode, 73)
        self.assertIn("cannot write local deadline marker", result.stderr)
        (runtime / "deadline-exceeded").rmdir()
        (node / "termination-log").mkdir()
        proc = self.start(node, env)
        self.assertEqual(proc.wait(timeout=5), 73)
        self.assertIn("cannot write", (node / "output").read_text())
        self.assertIn("termination-log", (node / "output").read_text())

    def test_completed_task_wins_over_final_deadline_poll(self):
        # Exercise the real parent loop while delaying only the task's exit
        # status publication; no dependency on which side of a second we start.
        for code in (0, 124):
            with self.subTest(code=code):
                node, runtime, _, _, _, env = self.stage(rank=code // 124)
                env["PPU_DEADLINE"] = str(int(time.time()) + 20)
                script = ("source " + shlex.quote(str(runtime / "master-worker.sh")) + "\n"
                          "CLUSTER_OUTCOME=runtime\n"
                          "cluster_task() { printf '%s\\n' " + str(code) + " > "
                          + shlex.quote(str(runtime / "command.exit")) + "; "
                          "printf '{\"phase\":\"done\"}' > " + shlex.quote(str(runtime / "status.json"))
                          + "; /bin/sleep 0.3; return " + str(code) + "; }\n"
                          "cluster_remaining() { [[ ! -f " + shlex.quote(str(runtime / "command.exit")) + " ]]; }\n"
                          "cluster_main; CLUSTER_RESULT=$?; cluster_termination; exit \"$CLUSTER_RESULT\"\n")
                result = self.bash(script, env=env)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assert_termination(node, env, "command")
                self.assertFalse((runtime / "deadline-exceeded").exists())

    def test_command_finished_time_survives_shutdown_delay(self):
        node, runtime, _, _, _, env = self.stage()
        script = ("source " + shlex.quote(str(runtime / "master-worker.sh")) + "\n"
                  "CLUSTER_OUTCOME=runtime\n"
                  "cluster_task() { " + shlex.quote(str(node / "tools/date")) + " +%s.%N > "
                  + shlex.quote(str(runtime / "command.finished")) + "; printf 0 > "
                  + shlex.quote(str(runtime / "command.exit")) + "; }\n"
                  "cluster_cleanup() { /bin/sleep 0.3; }\n"
                  "cluster_main; CLUSTER_RESULT=$?; cluster_termination; exit \"$CLUSTER_RESULT\"\n")
        result = self.bash(script, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_termination(node, env, "command")
        completed = json.loads((node / "termination-log").read_text())["command_finished_at"]
        self.assertGreater(time.time() - float(completed), 0.3)

    def test_command_completion_wins_over_stalled_health_probe_at_deadline(self):
        for code in (0, 124):
            with self.subTest(code=code):
                node, runtime, _, _, _, env = self.stage(rank=code // 124)
                # This test exercises master-only health checks for both codes.
                env.update(NODE_RANK="0", RANK="0")
                trigger = shlex.quote(str(runtime / "probe-started"))
                result_path = shlex.quote(str(runtime / "command.exit"))
                status = shlex.quote(str(runtime / "status.json"))
                job = shlex.quote(str(runtime / "job-id"))
                script = ("source " + shlex.quote(str(runtime / "master-worker.sh")) + "\n"
                          "CLUSTER_OUTCOME=runtime\n"
                          "cluster_task() { printf job-independent-123 > " + job + "; "
                          "printf '{\"phase\":\"running\"}' > " + status + "; "
                          "while [[ ! -f " + trigger + " ]]; do /bin/sleep 0.01; done; "
                          "printf '%s\\n' " + str(code) + " > " + result_path + "; "
                          "printf '{\"phase\":\"done\"}' > " + status + "; "
                          "/bin/sleep 0.3; return " + str(code) + "; }\n"
                          "cluster_peer() { : > " + trigger + "; /bin/sleep 0.5; return 1; }\n"
                          "cluster_remaining() { [[ ! -f " + result_path + " ]]; }\n"
                          "cluster_main; CLUSTER_RESULT=$?; cluster_termination; exit \"$CLUSTER_RESULT\"\n")
                result = self.bash(script, env=env)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assert_termination(node, env, "command")
                self.assertFalse((runtime / "deadline-exceeded").exists())


class SimulatedClusterTests(Fixture):
    def launch_pair(self, setup=None, command=":\n", real=False, ports=None):
        nodes = [self.stage(i, real=real) for i in range(2)]
        for rank, (node, runtime, _, _, tools, env) in enumerate(nodes):
            env["PPU_SSH_PORT"] = str(ports[rank]) if ports else (str(23000 + rank) if real else "2222")
            (node / "env.json").write_text(json.dumps(env))
            default = (shlex.quote(str(tools / "event")) + " setup-start\n"
                       + "export BUSINESS=business-$NODE_RANK\n"
                       + "ssh 127.0.0." + str(3 - rank) + " 'printf %s \"$RANK\"' > peer-rank\n"
                       + shlex.quote(str(tools / "event")) + " setup-end\n")
            (runtime / "setup.sh").write_text(setup[rank] if setup else default)
            (runtime / "command.sh").write_text(command)
        procs = [self.start(n[0], n[5]) for n in nodes]
        return nodes, procs

    def test_mesh_then_setup_ssh_then_master_only_command_and_exports(self):
        nodes, procs = self.launch_pair(command=(
            'printf "%s" "$RANK:$NODE_RANK:$WORLD_SIZE:$NNODES:$BUSINESS" > command-result\n'
            'cluster-exec 127.0.0.3 /bin/bash -c \'printf "%s" "$RANK:$BUSINESS"\' > remote-result\n'
            'exit 7\n'))
        self.wait_cluster(procs, (7, 143))
        for rank, (_, runtime, _, source, _, _) in enumerate(nodes):
            self.assertEqual((source / "peer-rank").read_text(), str(1 - rank))
            self.assertEqual((runtime / "identity").stat().st_mode & 0o777, 0o600)
            mapping = json.loads((runtime / "nodes.json").read_text())
            self.assertTrue(all(1 <= item["port"] <= 65535 for item in mapping))
        self.assertEqual((nodes[0][3] / "command-result").read_text(), "0:0:2:2:business-0")
        self.assertEqual((nodes[0][3] / "remote-result").read_text(), "1:business-1")
        self.assertFalse((nodes[1][3] / "command-result").exists())
        starts = [e for e in self.events() if e["kind"] == "setup-start"]
        self.assertEqual(len(starts), 2)
        first_setup = min(e["order"] for e in starts)
        for caller in range(2):
            for host in ("127.0.0.2", "127.0.0.3"):
                self.assertTrue(any(e["kind"] == "ssh" and e["rank"] == caller and e["host"] == host
                                    and e["intent"] == "__ppu_master_probe_v1__" and e["order"] < first_setup
                                    for e in self.events()))

    def test_snapshot_exports_filters_and_metadata_survive_setup_overrides(self):
        setup = [
            "export BUSINESS=$'line1\\nquote\\'line2' EMPTY=''\n"
            "export RANK=99 NODE_RANK=99 NNODES=99 WORLD_SIZE=99\n"
            "export GITHUB_TOKEN=secret ACTIONS_TOKEN=secret RUNNER_TOKEN=secret SSH_AUTH_SOCK=evil\n"
            "export PATH=/no-system-tools\n" for _ in range(2)]
        nodes, procs = self.launch_pair(setup=setup, command=(
            'printf "%s" "$RANK:$NODE_RANK:$WORLD_SIZE:$NNODES" > identity\n'
            'RANK=9 /bin/bash -c \'printf %s "$RANK"\' > override\n'
            'cluster-exec 127.0.0.3 /usr/bin/env -0 > remote.env\n'
            'export NODE_RANK=99\n'))
        self.wait_cluster(procs)
        self.assertEqual((nodes[0][3] / "identity").read_text(), "0:0:2:2")
        self.assertEqual((nodes[0][3] / "override").read_text(), "9")
        values = dict(entry.split(b"=", 1) for entry in (nodes[0][3] / "remote.env").read_bytes().split(b"\0") if entry)
        self.assertEqual(values[b"BUSINESS"], b"line1\nquote'line2")
        self.assertEqual(values[b"EMPTY"], b"")
        self.assertEqual(values[b"RANK"], b"1")
        self.assertEqual(values[b"WORLD_SIZE"], b"2")
        self.assertTrue(values[b"PATH"].startswith(str(nodes[1][1] / "bin").encode() + b":"))
        for name in (b"GITHUB_TOKEN", b"ACTIONS_TOKEN", b"RUNNER_TOKEN", b"SSH_AUTH_SOCK", b"BASH_ENV", b"ENV"):
            self.assertNotIn(name, values)
        self.assertEqual(json.loads((nodes[0][1] / "status.json").read_text())["rank"], 0)

    def check_business_scratch_exports(self, real=False):
        nodes = [self.stage(i, real=real) for i in range(2)]
        expected = []
        initial = {name: f"pod-old-{name}\n'quoted'\n" for name in SCRATCH_NAMES}
        for rank, (node, runtime, _, _, _, env) in enumerate(nodes):
            current = {name: ("" if index % 2 == rank else f"setup-{rank}-{name}\n'$(touch BAD)'\n")
                       for index, name in enumerate(SCRATCH_NAMES)}
            expected.append(current)
            env.update(initial)
            # Reserved input must neither unset dynamically active locals nor
            # inherit export attributes into snapshots or independent Bash.
            env.update({name: "reserved-old-export" for name in
                        ("CLUSTER_MAIN_NAME", "CLUSTER_MAIN_TASK", "CLUSTER_MAIN_RC",
                         "CLUSTER_INIT_NAME", "CLUSTER_LOAD_NAME", "CLUSTER_SAVE_TEMP",
                         "CLUSTER_COMMAND_PID", "CLUSTER_SESSION_CHILD", "CLUSTER_SESSION_RC")})
            if real:
                env["PPU_SSH_PORT"] = str(23000 + rank)
            (node / "env.json").write_text(json.dumps(env))
            setup = "/usr/bin/env -0 > initial.env\n" + "".join(
                "export " + name + "=" + shlex.quote(value) + "\n" for name, value in current.items())
            setup += "export CLUSTER_COMMAND_PID=reserved-setup-export CLUSTER_PRIVATE=must-not-leak\n"
            (runtime / "setup.sh").write_text(setup)
            (runtime / "command.sh").write_text(
                "/usr/bin/env -0 > local.env\n"
                "/bin/bash --noprofile --norc -c '/usr/bin/env -0' > local-child.env\n"
                "ssh 127.0.0.3 '/usr/bin/env -0' > direct-remote.env\n"
                "cluster-exec 127.0.0.3 /usr/bin/env -0 > exec-remote.env\n")
        procs = [self.start(n[0], n[5]) for n in nodes]
        self.wait_cluster(procs)
        for rank, (node, runtime, _, source, _, env) in enumerate(nodes):
            for path, wanted in ((source / "initial.env", initial),
                                 (runtime / "environment.env", expected[rank])):
                values = environment_file(path)
                for name, value in wanted.items():
                    self.assertEqual(values[name.encode()], value.encode(), str(path) + ": " + name)
                self.assertEqual({name for name in values if name.startswith(b"CLUSTER_")},
                                 {b"CLUSTER_HOSTS_FILE", b"CLUSTER_NODES_FILE"})
            self.assertFalse((source / "BAD").exists())
            self.assert_termination(node, env, "command" if rank == 0 else "runtime")
        for filename, rank in (("local.env", 0), ("local-child.env", 0),
                               ("direct-remote.env", 1), ("exec-remote.env", 1)):
            values = environment_file(nodes[0][3] / filename)
            for name, value in expected[rank].items():
                self.assertEqual(values[name.encode()], value.encode(), filename + ": " + name)
            self.assertEqual({name for name in values if name.startswith(b"CLUSTER_")},
                             {b"CLUSTER_HOSTS_FILE", b"CLUSTER_NODES_FILE"})

    def test_business_scratch_names_survive_full_local_and_remote_paths(self):
        self.check_business_scratch_exports()

    def test_user_exit124_is_command_failure_not_framework_timeout(self):
        nodes, procs = self.launch_pair(command="exit 124\n")
        self.wait_cluster(procs, (124, 143))
        self.assert_termination(nodes[0][0], nodes[0][5], "command")
        self.assertFalse((nodes[0][1] / "deadline-exceeded").exists())
        self.assertLess(int(time.time()), int(nodes[0][5]["PPU_DEADLINE"]))
        self.assertEqual(json.loads((nodes[0][1] / "status.json").read_text())["exit_code"], 124)

    def test_setup_exit124_remains_runtime_failure(self):
        nodes, procs = self.launch_pair(setup=["exit 124\n", ":\n"],
                                       command="printf BAD > command-result\n")
        self.assertEqual(procs[0].wait(timeout=15), 124)
        self.assert_termination(nodes[0][0], nodes[0][5], "runtime")
        self.assertFalse((nodes[0][1] / "deadline-exceeded").exists())
        self.assertFalse((nodes[0][3] / "command-result").exists())

    def test_command_exec_preserves_setup_shell_state_and_exit_code(self):
        nodes, procs = self.launch_pair(setup=["LOCAL_VALUE=from-setup\n"] * 2,
                                       command='printf %s "$LOCAL_VALUE" > inherited\nexec /bin/sh -c \'exit 13\'\n')
        self.wait_cluster(procs, (13, 143))
        self.assertEqual((nodes[0][3] / "inherited").read_text(), "from-setup")
        self.assertFalse((nodes[1][3] / "inherited").exists())

    def test_registry_ready_cannot_bypass_live_setup_barrier(self):
        nodes = [self.stage(i) for i in range(2)]
        release = self.root / "release-setup"
        (nodes[1][1] / "setup.sh").write_text('while [[ ! -f ' + shlex.quote(str(release))
                                                  + ' ]]; do /bin/sleep 0.1; done\n')
        (nodes[0][1] / "command.sh").write_text('printf done > command-result\n')
        procs = [self.start(n[0], n[5]) for n in nodes]
        self.await_condition(lambda: (nodes[0][1] / "status.json").exists()
                             and json.loads((nodes[0][1] / "status.json").read_text())["phase"] == "ready")
        registry = self.root / "registry" / "job-independent-123" / "nodes" / "1.json"
        record = json.loads(registry.read_text())
        record["phase"] = "ready"
        registry.write_text(json.dumps(record))
        time.sleep(0.3)
        self.assertFalse((nodes[0][3] / "command-result").exists())
        release.touch()
        self.wait_cluster(procs)
        self.assertTrue((nodes[0][3] / "command-result").exists())

    def test_real_bind_conflict_fallback_does_not_stop_existing_server(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", 2222))
            listener.listen(1)
        except OSError as exc:
            listener.close()
            self.skipTest("loopback port unavailable: " + str(exc))
        self.addCleanup(listener.close)
        nodes, procs = self.launch_pair()
        self.wait_cluster(procs)
        ports = [json.loads((node[1] / "status.json").read_text())["port"] for node in nodes]
        self.assertTrue(20000 <= ports[0] <= 24999)
        self.assertEqual(ports[1], 2222)
        self.assertEqual(listener.getsockname(), ("127.0.0.1", 2222))

    def test_setup_failure_propagates_and_command_never_starts(self):
        nodes, procs = self.launch_pair(setup=["/bin/sleep 0.3\n", "false\nprintf BAD > should-not-run\n"],
                                        command="printf BAD > command-result\n")
        self.wait_cluster(procs, (70, 1))
        self.assertFalse((nodes[1][3] / "should-not-run").exists())
        self.assertFalse((nodes[0][3] / "command-result").exists())

    def test_worker_listener_failure_interrupts_master_command(self):
        nodes, procs = self.launch_pair(command="printf started > started\n/bin/sleep 60\n")
        self.await_condition(lambda: (nodes[0][3] / "started").exists())
        pid = int((nodes[1][1] / "listener.pid").read_text())
        os.kill(pid, signal.SIGKILL)
        self.assertEqual(procs[0].wait(timeout=12), 70)
        self.assertEqual(procs[1].wait(timeout=12), 70)

    def test_deadline_interrupts_command_and_stops_listener(self):
        self.env["PPU_DEADLINE"] = str(int(time.time()) + 8)
        nodes, procs = self.launch_pair(command="/bin/sleep 60\n")
        self.wait_cluster(procs, (124, 124), timeout=15)
        for node, runtime, _, _, _, env in nodes:
            self.assert_termination(node, env, "timeout")
            self.assertTrue((runtime / "deadline-exceeded").is_file())
            self.assertEqual(list(self.registry_root.rglob("deadline-exceeded")), [])
            if (runtime / "listener.pid").exists():
                pid = int((runtime / "listener.pid").read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)


class RealSshdTests(SimulatedClusterTests):
    real_daemon = True

    @classmethod
    def setUpClass(cls):
        if sys.platform != "linux" or os.geteuid() != 0:
            raise unittest.SkipTest("real sshd integration requires Linux root; macOS/non-root skipped")
        for tool in ("/usr/sbin/sshd", "/usr/bin/ssh", "/usr/bin/jq", "/usr/bin/timeout"):
            if not Path(tool).is_file():
                raise unittest.SkipTest("preinstall openssh-server, jq and coreutils offline; missing " + tool)
        if not Path("/etc/pam.d/sshd").is_file():
            raise unittest.SkipTest("the OpenSSH PAM account policy must be installed")
        cls.root_shadow = next(line for line in Path("/etc/shadow").read_text().splitlines()
                               if line.startswith("root:"))
        Fixture.setUpClass.__func__(cls)

    # Only the following explicit tests use the real daemon; inherited fake cases are suppressed below.
    def test_real_two_node_distinct_ports_setup_ssh_and_master_only(self):
        nodes, procs = self.launch_pair(real=True, command=(
            'printf "%s" "$RANK:$WORLD_SIZE" > command-result\n'
            'cluster-exec 127.0.0.3 /bin/bash -c \'printf "%s" "$RANK:$BUSINESS"\' > remote-result\n'))
        self.wait_cluster(procs)
        self.assertEqual((nodes[0][3] / "command-result").read_text(), "0:2")
        self.assertEqual((nodes[0][3] / "remote-result").read_text(), "1:business-1")
        self.assertFalse((nodes[1][3] / "command-result").exists())
        self.assertNotEqual(json.loads((nodes[0][1] / "status.json").read_text())["port"],
                            json.loads((nodes[1][1] / "status.json").read_text())["port"])

    def test_real_business_scratch_names_survive_local_and_session_paths(self):
        self.check_business_scratch_exports(real=True)

    def test_real_user_exit124_is_command_failure(self):
        nodes, procs = self.launch_pair(real=True, command="exit 124\n")
        self.wait_cluster(procs, (124, 143))
        self.assert_termination(nodes[0][0], nodes[0][5], "command")
        self.assertFalse((nodes[0][1] / "deadline-exceeded").exists())

    def test_real_actual_framework_deadline(self):
        self.env["PPU_DEADLINE"] = str(int(time.time()) + 10)
        nodes, procs = self.launch_pair(real=True, command="/bin/sleep 60\n")
        self.wait_cluster(procs, (124, 124), timeout=20)
        for node, runtime, _, _, _, env in nodes:
            self.assert_termination(node, env, "timeout")
            self.assertTrue((runtime / "deadline-exceeded").is_file())

    def test_real_runner_uid1001_reads_renames_and_cleans_registry_without_acl(self):
        # An independent NAS fixture avoids exposing the 0700 SSH fixture under
        # /run. Its only ancestor is /run, which must already permit traversal.
        nas = tempfile.TemporaryDirectory(prefix="ppu-master-nas-", dir="/run")
        self.addCleanup(nas.cleanup)
        self.registry_root = Path(nas.name).resolve()
        os.chown(self.registry_root, 1001, 1001)
        self.registry_root.chmod(0o700)
        other = self.registry_root / "other-job" / "nodes"
        other.mkdir(parents=True, mode=0o700)
        sentinel = other / "0.json"
        sentinel.write_text("other-job-unchanged\n")
        protected = [self.registry_root, other.parent, other, sentinel]
        before = {p: (p.stat().st_uid, p.stat().st_gid, p.stat().st_mode,
                      p.read_bytes() if p.is_file() else None) for p in protected}
        nodes, procs = self.launch_pair(real=True)
        self.wait_cluster(procs)
        job = self.registry_root / "job-independent-123"
        directory = job / "nodes"
        for path in (job, directory, directory / "0.json", directory / "1.json"):
            self.assertEqual((path.stat().st_uid, path.stat().st_gid), (1001, 1001))
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        for path, values in before.items():
            self.assertEqual((path.stat().st_uid, path.stat().st_gid, path.stat().st_mode,
                              path.read_bytes() if path.is_file() else None), values)
        for _, runtime, _, _, _, _ in nodes:
            for private in (runtime, runtime / "identity", runtime / "environment.env"):
                self.assertEqual((private.stat().st_uid, private.stat().st_gid), (0, 0))
                self.assertEqual(private.stat().st_mode & 0o777, 0o700 if private.is_dir() else 0o600)
        for rank, (_, runtime, _, _, _, _) in enumerate(nodes):
            self.assertEqual((runtime / f"host-{rank}").stat().st_uid, 0)
            self.assertEqual((runtime / f"host-{rank}").stat().st_mode & 0o777, 0o600)

        def runner_identity():
            os.setgroups([])
            os.setgid(1001)
            os.setuid(1001)

        # No host account changes, ACLs or chmod grants: exercise kernel DAC as
        # the workflow identity, including read, atomic rename and all rmdirs.
        result = subprocess.run(
            [BASH, "--noprofile", "--norc", "-c",
             'set -e; [[ $(/usr/bin/id -u) == 1001 && $(/usr/bin/id -g) == 1001 ]]; '
             'for rank in 0 1; do '
             '/usr/bin/jq -e --argjson rank "$rank" '
             '\'.rank == $rank and (.phase == "done" or .phase == "failed")\' "$1/nodes/$rank.json"; '
             '/bin/mv "$1/nodes/$rank.json" "$1/nodes/$rank.renamed"; '
             '/bin/rm "$1/nodes/$rank.renamed"; done; '
             '/bin/rmdir "$1/nodes" "$1"', "runner", str(job)],
            env=self.env, preexec_fn=runner_identity, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(job.exists())
        self.assertEqual(sentinel.read_text(), "other-job-unchanged\n")
        after = next(line for line in Path("/etc/shadow").read_text().splitlines()
                     if line.startswith("root:"))
        self.assertEqual(after, self.root_shadow)

    def test_real_pam_public_key_login_preserves_root_account(self):
        nodes, procs = self.launch_pair(real=True)
        self.wait_cluster(procs)
        after = next(line for line in Path("/etc/shadow").read_text().splitlines()
                     if line.startswith("root:"))
        self.assertTrue(after == self.root_shadow, "SSH must not change the root account credentials")
        for _, runtime, _, _, _, _ in nodes:
            self.assertIn("UsePAM yes", (runtime / "sshd_config").read_text())
            self.assertIn("PasswordAuthentication no", (runtime / "sshd_config").read_text())

    def test_real_sshd_port_collision_falls_back_without_stopping_owner(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.2", 2222))
        listener.listen(8)
        nodes, procs = self.launch_pair(real=True, ports=[2222, 2222], command=(
            'cluster-exec 127.0.0.3 -- /bin/bash -c \'printf %s "$RANK"\' > peer\n'))
        self.wait_cluster(procs)
        ports = [json.loads((node[1] / "status.json").read_text())["port"] for node in nodes]
        self.assertTrue(20000 <= ports[0] <= 24999)
        self.assertEqual(ports[1], 2222)
        self.assertEqual((nodes[0][3] / "peer").read_text(), "1")
        self.assertEqual(listener.getsockname(), ("127.0.0.2", 2222))
        with socket.create_connection(("127.0.0.2", 2222), timeout=1):
            pass

    def test_real_worker_failure(self):
        nodes, procs = self.launch_pair(real=True, command="printf started > started\n/bin/sleep 60\n")
        self.await_condition(lambda: (nodes[0][3] / "started").exists(), timeout=20)
        os.kill(int((nodes[1][1] / "listener.pid").read_text()), signal.SIGKILL)
        self.assertEqual(procs[0].wait(timeout=15), 70)
        self.assertEqual(procs[1].wait(timeout=15), 70)


# Avoid running the simulated cases twice under the real class.
for _name in list(SimulatedClusterTests.__dict__):
    if _name.startswith("test_"):
        setattr(RealSshdTests, _name, None)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fake":
        sys.exit(fake(sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]), sys.argv[5:]))
    unittest.main()
