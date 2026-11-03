#!/usr/bin/env python3
import base64
import datetime
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest


SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
EPOCH = 1700000000


def timestamp(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat()


def pod(index, phase="Pending", scheduled=None, created=EPOCH, started=None,
        finished=None):
    conditions = [{"type": "Ready", "status": "False"}]
    condition = {"type": "PodScheduled", "status": "False",
                 "message": "Insufficient PPU capacity"}
    if scheduled is not None:
        condition.update(status="True", lastTransitionTime=timestamp(scheduled))
    conditions.append(condition)
    state = {"waiting": {"reason": "ContainerCreating", "message": "Pulling image"}}
    if started is not None:
        state = {"running": {"startedAt": timestamp(started)}}
    if finished is not None:
        state = {"terminated": {"startedAt": timestamp(started),
                                "finishedAt": timestamp(finished),
                                "exitCode": int(phase == "Failed"),
                                "reason": "Error" if phase == "Failed" else "Completed"}}
    return {"metadata": {"name": f"worker-{index}",
                         "creationTimestamp": timestamp(created)},
            "spec": {"nodeName": f"node-{index}" if scheduled is not None else ""},
            "status": {"phase": phase, "conditions": conditions,
                       "containerStatuses": [{"name": "worker", "ready": False,
                                              "state": state}]}}


def fake_command(tool, args):
    root = Path(os.environ["LIFECYCLE_FIXTURE"])
    fixture = json.loads((root / "fixture.json").read_text())
    with (root / "calls.jsonl").open("a") as calls:
        calls.write(json.dumps({"tool": tool, "args": args, "pid": os.getpid()}) + "\n")

    def clock(advance=0):
        with (root / "clock").open("r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            now = int(handle.read()) + advance
            if advance:
                handle.seek(0)
                handle.truncate()
                handle.write(str(now))
            return now

    if tool == "date":
        if args[0] == "-d":
            value = datetime.datetime.fromisoformat(args[1].replace("Z", "+00:00"))
            print(int(value.timestamp()))
        elif args == ["+%s"]:
            print(clock())
        else:
            print(datetime.datetime.fromtimestamp(clock(), datetime.timezone.utc)
                  .strftime(args[0][1:]))
        return 0

    if tool == "sleep":
        seconds = int(args[0])
        if fixture.get("step_sleep") and seconds >= 30:
            counter = root / "poll-sleeps"
            index = int(counter.read_text()) + 1 if counter.exists() else 1
            counter.write_text(str(index))
            (root / f"poll-sleep-{index}").touch()
            while not (root / f"release-poll-{index}").exists():
                time.sleep(0.01)
        if fixture.get("block_sleep") and seconds >= 30:
            (root / "sleep-waiting").touch()
            while not (root / "release-sleep").exists():
                time.sleep(0.01)
        time.sleep(0.03)
        clock(seconds)
        return 0

    if tool == "timeout":
        command = next(i for i, arg in enumerate(args) if arg == "kubectl")
        child = subprocess.Popen(args[command:], env={**os.environ,
                                 "LIFECYCLE_API_TIMEOUT": args[command - 1]})

        def forward(signum, frame):
            if child.poll() is None:
                child.send_signal(signum)

        signal.signal(signal.SIGTERM, forward)
        signal.signal(signal.SIGINT, forward)
        try:
            limit = 1 if fixture.get("block_delete") and args[command + 1] == "delete" else 10
            returncode = child.wait(timeout=limit)
            return returncode if returncode >= 0 else 128 - returncode
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            clock(int(args[command - 1].removesuffix("s")))
            return 124

    if tool == "kubectl":
        if not os.environ.get("LIFECYCLE_API_TIMEOUT"):
            raise ValueError("API requests must have an outer deadline")
        if any(arg.startswith("--request-timeout=") for arg in args):
            raise ValueError("Request timeout overrides break in-cluster configuration")
        if args[0] == "delete":
            if "--wait=false" not in args:
                raise ValueError("Deletion must not wait for finalizers")
            if fixture.get("block_delete"):
                while True:
                    time.sleep(0.01)
            if "--force" in args:
                (root / "forced").touch()
            return fixture.get("delete_error", 0)
        if args[0] == "create":
            path = Path(args[args.index("-f") + 1])
            content = path.read_text()
            document = json.loads(content) if content.lstrip().startswith("{") else {}
            if document.get("kind") == "Secret":
                if not path.is_relative_to(root / "dev-shm"):
                    raise ValueError("Secret JSON must be created in the isolated memory filesystem")
                private_parent = any(parent != root / "dev-shm" and
                                     parent.is_relative_to(root / "dev-shm") and
                                     not parent.stat().st_mode & 0o077 for parent in path.parents)
                if path.stat().st_mode & 0o077 and not private_parent:
                    raise ValueError("Secret JSON must be private or enclosed by a private key directory")
                directory = root / "secrets"
                directory.mkdir(mode=0o700, exist_ok=True)
                saved = directory / (document["metadata"]["name"] + ".json")
                saved.write_text(content)
                saved.chmod(0o600)
                return fixture.get("secret_create_error", 0)
            (root / "manifests").mkdir(exist_ok=True)
            (root / "manifests" / path.name).write_text(content)
            if fixture.get("create_error_kind") == document.get("kind", "Pod" if "pod-worker" in path.name else ""):
                return fixture.get("create_error", 42)
            return 0
        if args[:2] == ["get", "crd"]:
            return 0
        if args[0] == "get" and args[1] in ("svc", "podgroups.scheduling.x-k8s.io", "secret", "secrets"):
            if fixture.get("gang_query_error") == args[1]:
                return 42
            if clock() < fixture.get("stale_" + args[1] + "_until", 0):
                print(args[1] + "/old-resource")
            return 0
        if args[0] == "logs":
            if args[1] != os.environ["JOB_NAME"] + "-worker-0":
                raise ValueError("Only worker-0 logs may be streamed")
            if any(arg.startswith(("--tail", "--since")) for arg in args):
                raise ValueError("Early logs must not be filtered")
            if fixture.get("first_log_error") and not (root / "log-failed").exists():
                (root / "log-failed").touch()
                return 42
            lines = fixture.get("logs", ["early training log", "final training log"])
            print("\n".join(lines[:-1]), flush=True)
            (root / "logs-started").touch()
            if fixture.get("block_logs"):
                while True:
                    time.sleep(0.05)
            time.sleep(0.15)
            print(lines[-1], flush=True)
            return 0
        if args[:2] == ["get", "events"]:
            print(json.dumps({"items": fixture["events"]}))
            return 0
        if args[:2] in (["get", "pods"], ["get", "pod"]):
            if "--no-headers" in args:
                if fixture.get("query_error"):
                    return fixture["query_error"]
                if clock() < fixture.get("stale_pods_until", 0) or fixture.get("remaining_pods"):
                    print("old-worker")
                return 0
            if fixture.get("pod_query_error") and not (root / "query-failed").exists():
                if fixture.get("pod_query_error_once"):
                    (root / "query-failed").touch()
                clock(fixture.get("pod_query_error_advance", 0))
                return fixture["pod_query_error"]
            frames = fixture["frames"]
            items = frames[0]["pods"]
            for frame in frames:
                if frame["at"] <= clock():
                    items = frame["pods"]
            if args[1] == "pod":
                name = args[2]
                item = next((item for item in items if item["metadata"]["name"] == name), None)
                if item is None:
                    return 1
                print(item["status"]["phase"])
            else:
                print(json.dumps({"items": items}))
            return 0
    raise ValueError(f"Unexpected fake command: {tool} {args}")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(shutil.which("jq"), "jq is required by the lifecycle scripts")
        self.assertIsNotNone(shutil.which("flock"), "flock is required by the lifecycle scripts")
        self.temp = tempfile.TemporaryDirectory(prefix="ppu-lifecycle_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scripts = self.root / "scripts"
        shutil.copytree(SCRIPTS, self.scripts)
        self.runtime = self.root / "runtime"
        self.shm = self.root / "dev-shm"
        self.shm.mkdir(mode=0o700)
        self.home = self.root / "home"
        self.home.mkdir()
        for path in self.scripts.glob("*.sh"):
            path.write_text(self.isolate_script(path.read_text()))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for tool in ("kubectl", "date", "sleep", "timeout"):
            executable = self.bin / tool
            executable.write_text(
                "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                + shlex.quote(str(Path(__file__).resolve()))
                + " --fake " + tool + ' "$@"\n')
            executable.chmod(0o755)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("INPUT_", "GITHUB_", "JOB_"))
                    and key not in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS",
                                    "USER_ENV_JSON", "SOURCE_STAGE_DIR", "CONTAINER_OPTIONS",
                                    "MASTER_RUNTIME_DIR")}
        self.env.update(
            PATH=str(self.bin) + os.pathsep + os.defpath + os.pathsep
            + str(Path(shutil.which("jq")).parent) + os.pathsep
            + str(Path(shutil.which("flock")).parent),
            HOME=str(self.home), TMPDIR=str(self.root),
            LIFECYCLE_FIXTURE=str(self.root), JOB_NAME=self.root.name.replace("_", "-"),
            INPUT_IMAGE="fixture:latest", INPUT_COMMAND="exit 0",
            INPUT_NAMESPACE="fixture", INPUT_NNODES="0", INPUT_NPROC_PER_NODE="8",
            INPUT_TIMEOUT_MINUTES="1", JOB_START_TIME=str(EPOCH),
            JOB_DEADLINE=str(EPOCH + 60), GITHUB_REPOSITORY="fixture/repository",
            GITHUB_JOB_NAME="lifecycle-test")
        self.processes = []
        self.addCleanup(self.stop_processes)

    def isolate_script(self, text):
        # Only fixture copies are changed. Never create runtime files on the real NAS.
        return (text.replace("/tmp/training-manifests", str(self.root / "manifests"))
                .replace("/wl_nas/devops/ppu-dashboard/tasks", str(self.root / "tasks"))
                .replace("/mnt/wl_nas/devops/runtime/master", str(self.runtime))
                .replace("/wl_nas/devops/runtime/master", str(self.runtime))
                .replace("/mnt/wl_nas/devops/cache", str(self.root / "cache"))
                .replace("/wl_nas/devops/cache", str(self.root / "cache"))
                .replace("/dev/shm", str(self.shm)))

    def stop_processes(self):
        for process in self.processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    def configure(self, pods, now=EPOCH, later=(), **options):
        def named(items):
            items = json.loads(json.dumps(items))
            for item in items:
                item["metadata"]["name"] = self.env["JOB_NAME"] + "-" + item["metadata"]["name"]
            return items

        fixture = {"frames": [{"at": 0, "pods": named(pods)}]
                   + [{"at": at, "pods": named(items)} for at, items in later],
                   "events": [
                       {"involvedObject": {"name": self.env["JOB_NAME"] + "-worker-0"},
                        "lastTimestamp": timestamp(EPOCH), "message": "Older scheduling reason"},
                       {"involvedObject": {"name": self.env["JOB_NAME"] + "-worker-0"},
                        "lastTimestamp": timestamp(EPOCH + 1), "message": "Insufficient PPU capacity"},
                       {"involvedObject": {"name": "another-job-worker-0"},
                        "lastTimestamp": timestamp(EPOCH + 2), "message": "Unrelated reason"}],
                   **options}
        (self.root / "fixture.json").write_text(json.dumps(fixture))
        (self.root / "clock").write_text(str(now))

    def calls(self, tool=None):
        path = self.root / "calls.jsonl"
        calls = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [call for call in calls if tool is None or call["tool"] == tool]

    def start(self, script, **env):
        output = self.root / (script + ".outputs")
        output.write_text("")
        process = subprocess.Popen(
            ["/bin/bash", str(self.scripts / script)], env={**self.env, **env, "GITHUB_OUTPUT": str(output)},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.processes.append(process)
        return process, output

    def finish(self, process, output):
        try:
            text, _ = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            text, _ = process.communicate()
            self.fail("Lifecycle script hung or left an open log pipe:\n" + text)
        self.assertNotIn("Traceback", text)
        outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
        return process.returncode, outputs, text

    def run_script(self, script, **env):
        return self.finish(*self.start(script, **env))

    def wait_for(self, marker, process):
        deadline = time.monotonic() + 20
        while not (self.root / marker).exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                self.fail(f"Script did not reach {marker}")
            time.sleep(0.01)

    def assert_reported(self, outputs, status, count):
        self.assertEqual(outputs["job_status"], status)
        self.assertEqual(outputs["task_pods"], ",".join(
            f"{self.env['JOB_NAME']}-worker-{i}" for i in range(count)))
        for key in ("duration_seconds", "queue_duration_seconds", "unscheduled_pods",
                    "pod_queue", "queue_reason", "occupied_ppu_card_hours",
                    "execution_ppu_card_hours"):
            self.assertIn(key, outputs)

    def test_submit_returns_when_all_scheduled_but_not_ready(self):
        self.env["INPUT_NNODES"] = "8"
        self.configure([pod(i, scheduled=EPOCH + 3) for i in range(8)])
        rc, outputs, text = self.run_script("submit.sh")
        self.assertEqual(rc, 0, text)
        self.assertEqual(outputs["job_status"], "scheduled")
        self.assertEqual(int(outputs["deadline"]) - int(outputs["start_time"]), 60)
        self.assertEqual(int((self.root / "clock").read_text()), int(outputs["start_time"]) + 3)
        self.assertEqual([call["args"] for call in self.calls("sleep")], [["3"]])
        creates = [call for call in self.calls("kubectl") if call["args"][0] == "create"]
        self.assertEqual(len(creates), 10)
        self.assertFalse(any(call["args"][0] in ("wait", "logs") for call in self.calls("kubectl")))

    def test_monitor_drains_early_logs_of_succeeded_pods_without_ready(self):
        self.env["INPUT_NNODES"] = "2"
        logs = [f"early log {i}" for i in range(32)] + ["training completed"]
        self.configure([pod(i, "Succeeded", EPOCH - 20, started=EPOCH - 10,
                            finished=EPOCH - 1) for i in range(2)], logs=logs)
        rc, outputs, text = self.run_script("monitor.sh")
        self.assertEqual(rc, 0, text)
        self.assert_reported(outputs, "succeeded", 2)
        for line in logs:
            self.assertIn("[worker-0] " + line + "\n", text)
        self.assertEqual(len([call for call in self.calls("kubectl")
                              if call["args"][0] == "logs"]), 1)

    def test_monitor_preserves_logs_and_reports_container_exit_one(self):
        self.configure([pod(0, "Failed", EPOCH - 20, started=EPOCH - 10,
                            finished=EPOCH - 1)], logs=["training started", "container exited 1"])
        rc, outputs, text = self.run_script("monitor.sh")
        self.assertEqual(rc, 0, text)
        self.assert_reported(outputs, "failed", 1)
        self.assertIn("[worker-0] training started\n", text)
        self.assertIn("[worker-0] container exited 1\n", text)
        self.assertEqual(outputs["execution_ppu_card_hours"], "0.0200")

    def test_submit_times_out_with_unscheduled_gang_and_zero_execution(self):
        self.env["INPUT_NNODES"] = "8"
        for scheduled_count in (7, 0):
            with self.subTest(scheduled_count=scheduled_count):
                self.configure([pod(i, scheduled=EPOCH + 3 if i < scheduled_count else None)
                                for i in range(8)])
                rc, outputs, text = self.run_script("submit.sh")
                self.assertEqual(rc, 0, text)
                self.assert_reported(outputs, "timeout", 8)
                self.assertEqual(outputs["unscheduled_pods"], str(8 - scheduled_count))
                self.assertEqual(outputs["execution_ppu_card_hours"], "0.0000")
                self.assertEqual(outputs["duration_seconds"], "60")
                self.assertEqual(outputs["queue_reason"], "Insufficient PPU capacity")
                if scheduled_count == 0:
                    self.assertEqual(outputs["occupied_ppu_card_hours"], "0.0000")

    def test_submit_and_monitor_share_the_original_deadline(self):
        pending = [pod(0)]
        scheduled = [pod(0, scheduled=EPOCH + 33)]
        self.configure(pending, later=[(EPOCH + 33, scheduled)], block_logs=True)
        rc, submitted, text = self.run_script("submit.sh")
        self.assertEqual(rc, 0, text)
        self.assertEqual(submitted["job_status"], "scheduled")
        before = len(self.calls())
        rc, outputs, text = self.run_script(
            "monitor.sh", JOB_START_TIME=submitted["start_time"], JOB_DEADLINE=submitted["deadline"])
        self.assertEqual(rc, 0, text)
        self.assert_reported(outputs, "timeout", 1)
        self.assertEqual(outputs["duration_seconds"], "60")
        monitor_sleeps = [call["args"] for call in self.calls()[before:] if call["tool"] == "sleep"]
        self.assertEqual(monitor_sleeps, [["27"]])
        budgets = [call["args"][2] for call in self.calls("timeout") if "logs" in call["args"]]
        self.assertEqual(budgets, ["27s"])

    def test_sigterm_finalizes_both_steps_and_reaps_log_children(self):
        for script in ("submit.sh", "monitor.sh"):
            with self.subTest(script=script):
                for marker in ("sleep-waiting", "release-sleep", "logs-started"):
                    (self.root / marker).unlink(missing_ok=True)
                self.configure([pod(0, "Running", EPOCH - 10, started=EPOCH - 5)
                                if script == "monitor.sh" else pod(0)],
                               block_sleep=True, block_logs=True)
                process, output = self.start(script, JOB_DEADLINE=str(EPOCH + 300))
                self.wait_for("sleep-waiting", process)
                if script == "monitor.sh":
                    self.wait_for("logs-started", process)
                process.send_signal(signal.SIGTERM)
                (self.root / "release-sleep").touch()
                rc, outputs, text = self.finish(process, output)
                self.assertEqual(rc, 130, text)
                self.assert_reported(outputs, "cancelled", 1)
                listing = subprocess.check_output(
                    ["ps", "-axo", "pid=,pgid=,stat=,command="], text=True)
                remaining = [line for line in listing.splitlines()
                             if len(line.split()) >= 3 and line.split()[1] == str(process.pid)
                             and not line.split()[2].startswith("Z")]
                self.assertEqual(remaining, [], "Residual lifecycle subprocesses")

    def test_submit_exits_on_failed_member_of_partial_gang(self):
        self.env["INPUT_NNODES"] = "8"
        self.configure([pod(0, "Failed", EPOCH - 90, started=EPOCH - 80,
                            finished=EPOCH - 1)] + [pod(i) for i in range(1, 8)])
        rc, outputs, text = self.run_script("submit.sh")
        self.assertEqual(rc, 0, text)
        self.assert_reported(outputs, "failed", 8)
        self.assertEqual(outputs["unscheduled_pods"], "7")
        self.assertEqual(outputs["duration_seconds"], "3")
        self.assertEqual([call["args"] for call in self.calls("sleep")], [["3"]])

    def test_monitor_exits_on_failed_member_while_worker_zero_is_running(self):
        self.env["INPUT_NNODES"] = "8"
        self.configure([pod(0, "Running", EPOCH - 20, started=EPOCH - 10),
                        pod(1, "Failed", EPOCH - 20, started=EPOCH - 10, finished=EPOCH - 1)]
                       + [pod(i) for i in range(2, 8)], block_logs=True)
        rc, outputs, text = self.run_script("monitor.sh")
        self.assertEqual(rc, 0, text)
        self.assert_reported(outputs, "failed", 8)
        self.assertEqual(outputs["unscheduled_pods"], "6")
        self.assertEqual(outputs["duration_seconds"], "0")

    def test_cleanup_resolves_cancelled_handoff_and_keeps_terminal_status(self):
        self.configure([pod(0)])
        for status, workflow, expected in (
                ("scheduled", "cancelled", "cancelled"),
                ("scheduled", "failure", "failed"),
                ("succeeded", "cancelled", "succeeded"),
                ("timeout", "failure", "timeout")):
            with self.subTest(status=status, workflow=workflow):
                rc, outputs, text = self.run_script(
                    "cleanup.sh", INPUT_JOB_STATUS=status, INPUT_WORKFLOW_STATUS=workflow)
                self.assertEqual(rc, 0, text)
                self.assertEqual(outputs["job_status"], expected)

    def test_cleanup_preserves_recorded_terminal_status_after_lost_outputs(self):
        self.configure([pod(0)])
        script = self.isolate_script((SCRIPTS / "cleanup.sh").read_text()).replace(
            'TASK_DIR="' + str(self.root / "tasks") + '"',
            "TASK_DIR=" + shlex.quote(str(self.root)))
        task = self.root / (self.env["JOB_NAME"] + ".json")
        output = self.root / "cleanup-record.outputs"
        for status, recorded, expected in (
                ("scheduled", "timeout", "timeout"),
                ("scheduled", "cancelled", "cancelled"),
                ("", "succeeded", "succeeded"),
                ("scheduled", "running", "failed"),
                ("succeeded", "pending", "succeeded")):
            with self.subTest(status=status, recorded=recorded):
                task.write_text(json.dumps({"status": recorded}))
                output.write_text("")
                process = subprocess.Popen(
                    ["/bin/bash", "-c", script],
                    env={**self.env, "INPUT_JOB_STATUS": status,
                         "INPUT_WORKFLOW_STATUS": "failure", "GITHUB_OUTPUT": str(output)},
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, start_new_session=True)
                self.processes.append(process)
                rc, outputs, text = self.finish(process, output)
                self.assertEqual(rc, 0, text)
                self.assertEqual(outputs["job_status"], expected)
                self.assertEqual(json.loads(task.read_text())["status"], expected)

    def paused_poller(self, script, phase="Running"):
        for marker in ("poll-sleeps", "poll-sleep-1", "poll-sleep-2",
                       "release-poll-1", "release-poll-2"):
            (self.root / marker).unlink(missing_ok=True)
        active = pod(0, phase, EPOCH, started=EPOCH,
                     finished=EPOCH + 20 if phase == "Succeeded" else None)
        self.configure([pod(0), pod(1)], later=[(EPOCH + 30, [active, pod(1)])],
                       step_sleep=True)
        task = self.root / "tasks" / (self.env["JOB_NAME"] + ".json")
        task.parent.mkdir(exist_ok=True)
        task.write_text(json.dumps({"status": "pending"}))
        process, output = self.start(script, INPUT_NNODES="2", INPUT_TIMEOUT_MINUTES="5",
                                     JOB_DEADLINE=str(EPOCH + 300))
        self.wait_for("poll-sleep-1", process)
        return process, output, task

    def finish_paused_poller(self, process, output):
        process.send_signal(signal.SIGTERM)
        (self.root / "release-poll-2").touch()
        return self.finish(process, output)

    def test_pollers_promote_only_unfinished_records(self):
        for script in ("submit.sh", "monitor.sh"):
            for phase in ("Running", "Succeeded"):
                for status in ("pending", "scheduled", "running", "cancelled",
                               "failed", "succeeded", "timeout"):
                    with self.subTest(script=script, phase=phase, status=status):
                        process, output, task = self.paused_poller(script, phase)
                        record = {"status": status, "execution_ppu_card_hours": 1.25}
                        task.write_text(json.dumps(record))
                        before = task.stat().st_mtime_ns
                        (self.root / "release-poll-1").touch()
                        self.wait_for("poll-sleep-2", process)
                        expected = dict(record, status="running") if status in (
                            "pending", "scheduled", "running") else record
                        try:
                            self.assertEqual(json.loads(task.read_text()), expected)
                            if expected == record:
                                self.assertEqual(task.stat().st_mtime_ns, before)
                        finally:
                            rc, _, text = self.finish_paused_poller(process, output)
                            self.assertEqual(rc, 130, text)
                        if status in ("cancelled", "failed", "succeeded", "timeout"):
                            self.assertEqual(json.loads(task.read_text()), record)

    def test_pollers_preserve_records_with_an_end_time(self):
        for script in ("submit.sh", "monitor.sh"):
            with self.subTest(script=script):
                process, output, task = self.paused_poller(script)
                record = {"status": "running", "end_time": timestamp(EPOCH + 10),
                          "occupied_ppu_card_hours": 2.5, "execution_ppu_card_hours": 1.25}
                task.write_text(json.dumps(record))
                before = task.stat().st_mtime_ns
                (self.root / "release-poll-1").touch()
                self.wait_for("poll-sleep-2", process)
                try:
                    self.assertEqual(json.loads(task.read_text()), record)
                    self.assertEqual(task.stat().st_mtime_ns, before)
                finally:
                    rc, _, text = self.finish_paused_poller(process, output)
                    self.assertEqual(rc, 130, text)
                self.assertEqual(json.loads(task.read_text()), record)

    def test_cleanup_finalization_survives_a_resumed_poll(self):
        for script in ("submit.sh", "monitor.sh"):
            with self.subTest(script=script):
                process, output, task = self.paused_poller(script)
                task.write_text(json.dumps({"status": "running",
                                            "execution_ppu_card_hours": 1.25}))
                rc, outputs, text = self.run_script(
                    "cleanup.sh", INPUT_NNODES="2", INPUT_JOB_STATUS="scheduled",
                    INPUT_WORKFLOW_STATUS="cancelled")
                self.assertEqual(rc, 0, text)
                self.assertEqual(outputs["job_status"], "cancelled")
                finalized = task.read_bytes()
                before = task.stat().st_mtime_ns
                (self.root / "release-poll-1").touch()
                self.wait_for("poll-sleep-2", process)
                try:
                    self.assertEqual(task.read_bytes(), finalized)
                    self.assertEqual(task.stat().st_mtime_ns, before)
                finally:
                    rc, _, text = self.finish_paused_poller(process, output)
                    self.assertEqual(rc, 130, text)
                self.assertEqual(task.read_bytes(), finalized)

    def test_task_writers_read_the_record_after_acquiring_its_lock(self):
        real_flock = shutil.which("flock")
        for script in ("submit.sh", "monitor.sh", "cleanup.sh", "report-job.sh"):
            with self.subTest(script=script):
                (self.bin / "flock").unlink(missing_ok=True)
                (self.root / "task-lock-waiting").unlink(missing_ok=True)
                if script in ("submit.sh", "monitor.sh"):
                    process, output, task = self.paused_poller(script)
                else:
                    self.configure([pod(0, "Running", EPOCH, started=EPOCH)])
                    task = self.root / "tasks" / (self.env["JOB_NAME"] + ".json")
                    task.parent.mkdir(exist_ok=True)
                    task.write_text(json.dumps({"status": "running"}))
                wrapper = self.bin / "flock"
                wrapper.write_text("#!/bin/sh\n: > "
                                   + shlex.quote(str(self.root / "task-lock-waiting"))
                                   + "\nexec " + shlex.quote(real_flock) + ' "$@"\n')
                wrapper.chmod(0o755)
                record = {"status": "cancelled", "end_time": timestamp(EPOCH + 10),
                          "execution_ppu_card_hours": 1.25}
                with Path(str(task) + ".lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    if script in ("submit.sh", "monitor.sh"):
                        (self.root / "release-poll-1").touch()
                    else:
                        process, output = self.start(
                            script, INPUT_JOB_STATUS="scheduled",
                            INPUT_WORKFLOW_STATUS="cancelled", JOB_STATUS="timeout")
                    self.wait_for("task-lock-waiting", process)
                    task.write_text(json.dumps(record))
                if script in ("submit.sh", "monitor.sh"):
                    self.wait_for("poll-sleep-2", process)
                    try:
                        self.assertEqual(json.loads(task.read_text()), record)
                    finally:
                        rc, _, text = self.finish_paused_poller(process, output)
                        self.assertEqual(rc, 130, text)
                else:
                    rc, _, text = self.finish(process, output)
                    self.assertEqual(rc, 0, text)
                self.assertEqual(json.loads(task.read_text()), record)

    def manifest(self, name):
        path = self.root / "manifests" / name
        return json.loads(subprocess.check_output(
            ["ruby", "-ryaml", "-rjson", "-e",
             "puts JSON.generate(YAML.safe_load(STDIN.read, aliases: true))"],
            input=path.read_text(), text=True, stderr=subprocess.DEVNULL))

    def worker_manifest(self, index=0):
        return self.manifest(f"pod-worker-{index}.yaml")

    def secret(self):
        path = self.root / "secrets" / (self.env["JOB_NAME"] + "-ssh.json")
        self.assertTrue(path.is_file(), "Multi-node master launch must create its SSH Secret")
        document = json.loads(path.read_text())
        values = {key: base64.b64decode(value, validate=True).decode()
                  for key, value in document.get("data", {}).items()}
        values.update(document.get("stringData", {}))
        return document, values

    def submit_master(self, nodes=2, **env):
        self.configure([pod(i, scheduled=EPOCH) for i in range(nodes or 1)])
        rc, outputs, text = self.run_script(
            "submit.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES=str(nodes), **env)
        self.assertEqual(rc, 0, text)
        self.assertEqual(outputs["job_status"], "scheduled", text)
        return outputs, text

    def secret_deletes(self, since=0):
        return [call["args"] for call in self.calls("kubectl")[since:]
                if call["args"][0] == "delete" and call["args"][1] in ("secret", "secrets")]

    def assert_secret_deleted(self, since=0):
        deletes = self.secret_deletes(since)
        self.assertTrue(deletes, "Cleanup must delete the SSH Secret even after partial submission")
        self.assertTrue(any(self.env["JOB_NAME"] + "-ssh" in args for args in deletes))
        self.assertTrue(all("--wait=false" in args for args in deletes))

    def test_validated_ssh_options_take_precedence_in_real_openssh_configuration(self):
        runtime = self.root / "ssh-options"
        runtime.mkdir()
        nodes = [{"rank": 0, "host": "node-0.example", "cards": 1, "port": 2222}]
        (runtime / "nodes.json").write_text(json.dumps(nodes))
        (runtime / "trusted-nodes.json").write_text(json.dumps(nodes))
        shutil.copyfile(self.scripts / "master-worker.sh", runtime / "master-worker.sh")
        wrapper = runtime / "cluster-ssh.sh"
        wrapper.write_text((self.scripts / "cluster-ssh.sh").read_text()
                           .replace("/run/cluster", str(runtime))
                           .replace("exec /usr/bin/ssh ", "exec /usr/bin/ssh -G "))
        for options, expected in (([], {"connecttimeout": "2", "loglevel": "ERROR"}),
                                  (["-o", "ConnectTimeout=9", "-o", "ServerAliveInterval=7", "-o",
                                    "ServerAliveCountMax=4", "-o", "LogLevel=DEBUG3"],
                                   {"connecttimeout": "9", "serveraliveinterval": "7",
                                    "serveralivecountmax": "4", "loglevel": "DEBUG3"})):
            with self.subTest(options=options):
                result = subprocess.run(["/bin/bash", str(wrapper), *options, "node-0.example"],
                                        env=self.env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                config = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
                for key, value in expected.items():
                    self.assertEqual(config[key], value)
                self.assertEqual(config["port"], "2222")
                self.assertIn(config["stricthostkeychecking"], ("yes", "true"))

    def test_submit_rejects_invalid_launch_mode_before_creation(self):
        for mode in ("MASTER", "worker", "master; touch unsafe", "each_node"):
            with self.subTest(mode=mode):
                before = len(self.calls("kubectl"))
                self.configure([pod(0, scheduled=EPOCH)])
                rc, _, text = self.run_script("submit.sh", INPUT_LAUNCH_MODE=mode)
                self.assertNotEqual(rc, 0, text)
                self.assertIn("launch_mode", text)
                self.assertFalse(any(call["args"][0] == "create"
                                     for call in self.calls("kubectl")[before:]))

    def test_submit_rejects_invalid_ssh_port_before_creation(self):
        for port in ("0", "65536", "-1", "22x", "2.2", "2222;exit 0"):
            with self.subTest(port=port):
                before = len(self.calls("kubectl"))
                self.configure([pod(i, scheduled=EPOCH) for i in range(2)])
                rc, _, text = self.run_script(
                    "submit.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2", INPUT_SSH_PORT=port)
                self.assertNotEqual(rc, 0, text)
                self.assertIn("ssh_port", text)
                self.assertFalse(any(call["args"][0] == "create"
                                     for call in self.calls("kubectl")[before:]))

    def test_each_node_default_and_explicit_modes_preserve_resource_counts(self):
        for mode in (None, "each-node"):
            with self.subTest(mode=mode):
                self.configure([pod(i, scheduled=EPOCH) for i in range(2)])
                before = len(self.calls("kubectl"))
                rc, outputs, text = self.run_script(
                    "submit.sh", INPUT_NNODES="2", **({"INPUT_LAUNCH_MODE": mode} if mode else {}))
                self.assertEqual(rc, 0, text)
                self.assertEqual(outputs["job_status"], "scheduled")
                creates = [call for call in self.calls("kubectl")[before:]
                           if call["args"][0] == "create"]
                self.assertEqual(len(creates), 4)
                self.assertFalse((self.root / "secrets").exists())
                self.assertFalse(outputs.get("master_runtime_dir"))
                self.assertFalse(self.runtime.exists())
                for index in range(2):
                    container = self.worker_manifest(index)["spec"]["containers"][0]
                    values = {item["name"]: item.get("value") for item in container["env"]}
                    self.assertEqual(values["RANK"], str(index))
                    self.assertEqual(values["NODE_RANK"], str(index))
                    self.assertEqual(values["WORLD_SIZE"], "2")
                    self.assertEqual(values["NPROC_PER_NODE"], "8")
                    for kind in ("limits", "requests"):
                        self.assertEqual(container["resources"][kind]["alibabacloud.com/ppu"], "8")

    def test_each_node_setup_exports_environment_before_multiline_command(self):
        for index in range(2):
            self.configure([pod(i, scheduled=EPOCH) for i in range(2)])
            marker = self.root / f"setup-{index}"
            setup = "export SETUP_VALUE='quoted value'\nprintf '%s\\n' setup > " + shlex.quote(str(marker))
            command = ('test "$SETUP_VALUE" = "quoted value"\n'
                       'printf \'%s:%s\\n\' "$RANK" "$WORLD_SIZE" >> ' + shlex.quote(str(marker)))
            rc, _, text = self.run_script(
                "submit.sh", INPUT_NNODES="2", INPUT_SETUP_COMMAND=setup, INPUT_COMMAND=command)
            self.assertEqual(rc, 0, text)
            container = self.worker_manifest(index)["spec"]["containers"][0]
            values = {item["name"]: item["value"] for item in container["env"] if "value" in item}
            result = subprocess.run(["/bin/bash", "-c", container["args"][0]],
                                    env={**self.env, **values}, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(marker.read_text(), f"setup\n{index}:2\n")

    def test_setup_failure_never_starts_each_node_workload(self):
        marker = self.root / "workload-ran"
        self.configure([pod(0, scheduled=EPOCH)])
        rc, _, text = self.run_script(
            "submit.sh", INPUT_SETUP_COMMAND="printf 'setup failed\\n'\nfalse\nprintf 'unsafe\\n'",
            INPUT_COMMAND="touch " + shlex.quote(str(marker)))
        self.assertEqual(rc, 0, text)
        command = self.worker_manifest()["spec"]["containers"][0]["args"][0]
        result = subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())

    def test_master_single_pod_uses_compatible_local_command_without_ssh_secret(self):
        for nodes in (0, 1):
            with self.subTest(nodes=nodes):
                marker = self.root / f"single-{nodes}"
                outputs, _ = self.submit_master(
                    nodes, INPUT_SETUP_COMMAND="export SINGLE_SETUP=ready",
                    INPUT_COMMAND='test "$SINGLE_SETUP" = ready\ntouch ' + shlex.quote(str(marker)))
                self.assertFalse(outputs.get("master_runtime_dir"))
                self.assertFalse((self.root / "secrets").exists())
                self.assertFalse(self.runtime.exists())
                container = self.worker_manifest()["spec"]["containers"][0]
                values = {item["name"]: item["value"] for item in container["env"] if "value" in item}
                self.assertEqual(values["RANK"], "0")
                self.assertEqual(values["WORLD_SIZE"], "1")
                result = subprocess.run(["/bin/bash", "-c", container["args"][0]],
                                        env={**self.env, **values}, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(marker.exists())

    def test_master_gang_secret_manifest_hostnames_and_node_environment(self):
        outputs, text = self.submit_master(
            INPUT_SETUP_COMMAND="export FIXTURE_SETUP='value with spaces'",
            INPUT_COMMAND="printf 'master-command-once\\n'")
        document, data = self.secret()
        self.assertEqual(document["apiVersion"], "v1")
        self.assertEqual(document["metadata"]["name"], self.env["JOB_NAME"] + "-ssh")
        self.assertEqual(document["metadata"]["namespace"], "fixture")
        self.assertEqual(document.get("type", "Opaque"), "Opaque")
        nodes = json.loads(data["nodes.json"])
        self.assertIsInstance(nodes, list)
        self.assertEqual(len(nodes), 2)
        self.assertEqual([item["rank"] for item in nodes], [0, 1])
        self.assertEqual([item["cards"] for item in nodes], [8, 8])
        expected_hosts = [f"{self.env['JOB_NAME']}-worker-{i}.{self.env['JOB_NAME']}.fixture.svc.cluster.local"
                          for i in range(2)]
        self.assertEqual([item["host"] for item in nodes], expected_hosts)
        service = self.manifest("service.yaml")["spec"]
        self.assertEqual(service["clusterIP"], "None")
        self.assertIs(service["publishNotReadyAddresses"], True)
        self.assertTrue(any(str(item["port"]) == "29500" and str(item["targetPort"]) == "29500"
                            for item in service["ports"]), "Rendezvous service port must remain unchanged")
        self.assertEqual(self.manifest("podgroup.yaml")["spec"]["minMember"], 2)
        runtime_dir = Path(outputs["master_runtime_dir"])
        self.assertEqual(runtime_dir.parent, self.runtime)
        self.assertTrue(runtime_dir.name)
        self.assertEqual(data["job-id"].strip(), runtime_dir.name)
        self.assertNotEqual(runtime_dir.name, self.env["JOB_NAME"])
        self.assertNotIn("PRIVATE KEY", text)
        commands = []
        for index in range(2):
            spec = self.worker_manifest(index)["spec"]
            self.assertIs(spec["automountServiceAccountToken"], False)
            self.assertEqual(spec["hostname"], self.env["JOB_NAME"] + f"-worker-{index}")
            self.assertEqual(spec["subdomain"], self.env["JOB_NAME"])
            container = spec["containers"][0]
            values = {item["name"]: item.get("value") for item in container["env"]}
            for key, expected in {"RANK": str(index), "NODE_RANK": str(index), "LOCAL_RANK": "0",
                                  "WORLD_SIZE": "2", "NNODES": "2", "NPROC_PER_NODE": "8"}.items():
                self.assertEqual(values[key], expected)
            self.assertEqual(values["MASTER_ADDR"], expected_hosts[0])
            self.assertEqual(values["PPU_SSH_PORT"], "2222")
            self.assertEqual(values["NODE_NAME"], None)
            self.assertTrue(any(item["name"] == "NODE_NAME" and
                                item["valueFrom"]["fieldRef"]["fieldPath"] == "spec.nodeName"
                                for item in container["env"]))
            self.assertEqual(container["resources"]["limits"]["alibabacloud.com/ppu"], "8")
            self.assertEqual(container["resources"]["limits"]["cpu"], "88")
            self.assertEqual(container["resources"]["limits"]["memory"], "896Gi")
            mounts = {item["mountPath"]: item for item in container["volumeMounts"]}
            self.assertIn("/run/cluster-config", mounts)
            self.assertIs(mounts["/run/cluster-config"]["readOnly"], True)
            volumes = {item["name"]: item for item in spec["volumes"]}
            projection = volumes[mounts["/run/cluster-config"]["name"]]["secret"]
            self.assertEqual(projection["secretName"], self.env["JOB_NAME"] + "-ssh")
            projected_keys = {item["key"] for item in projection["items"]}
            self.assertIn("identity", projected_keys)
            self.assertIn(f"host-{index}", projected_keys)
            self.assertNotIn(f"host-{1 - index}", projected_keys,
                             "A node must not receive another node's private SSH host key")
            self.assertTrue({"host-0.pub", "host-1.pub", "nodes.json", "job-id", "install-ssh-rpm.sh"}.issubset(projected_keys))
            self.assertEqual(volumes[mounts["/run/cluster"]["name"]]["emptyDir"]["medium"], "Memory")
            readiness = " ".join(container["readinessProbe"]["exec"]["command"])
            self.assertIn("/run/cluster/ready", readiness)
            command = " ".join(container["command"] + container.get("args", []))
            commands.append(command)
            self.assertIn("/run/cluster-config", command)
            self.assertIn("FIXTURE_SETUP", command, "Setup must be staged for every node")
            # User shell payloads may be staged by the bootstrap, but the runtime
            # must select worker-0 as the only node that executes command.sh.
            self.assertNotRegex(command, r"ulimit\s+[^\n;]*-l")
            deadline_payload = command + "\n" + "\n".join(str(value) for value in values.values())
            self.assertIn(outputs["deadline"], deadline_payload, "Runtime must receive the submission deadline")
        scripts = "\n".join(value for key, value in data.items() if key.endswith(".sh"))
        self.assertIn("master-worker.sh", data)
        self.assertIn("NODE_RANK", scripts)
        payload = scripts + "\n" + "\n".join(commands)
        self.assertIn("FIXTURE_SETUP", payload)
        self.assertIn("master-command-once", payload)
        self.assertNotRegex(scripts, r"ulimit\s+[^\n;]*-l")

    def test_master_secret_uses_real_job_scoped_keys_without_nas_or_log_leaks(self):
        self.assertIsNotNone(shutil.which("ssh-keygen"), "OpenSSH client tools are required")
        keys = []
        runtime_dirs = []
        for attempt in ("1", "2", "2"):
            with self.subTest(attempt=attempt):
                outputs, text = self.submit_master(GITHUB_RUN_ID="1234", GITHUB_RUN_ATTEMPT=attempt)
                _, data = self.secret()
                private = [value for value in data.values() if "PRIVATE KEY-----" in value]
                self.assertTrue(private, "The CPU runner must generate a real private key for the job")
                key = self.root / "verify-key"
                key.write_text(private[0])
                key.chmod(0o600)
                result = subprocess.run(["ssh-keygen", "-y", "-f", str(key)],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                public = result.stdout.strip().split()[:2]
                self.assertTrue(any(" ".join(public) in value for value in data.values()),
                                "The corresponding public key must be included in the Secret")
                host_public_keys = []
                for index in range(2):
                    key.write_text(data[f"host-{index}"])
                    result = subprocess.run(["ssh-keygen", "-y", "-f", str(key)],
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    host_public = result.stdout.strip().split()[:2]
                    self.assertEqual(host_public, data[f"host-{index}.pub"].split()[:2])
                    host_public_keys.append(host_public)
                    self.assertNotIn(data[f"host-{index}"].strip(), text)
                self.assertNotEqual(host_public_keys[0], host_public_keys[1])
                self.assertNotIn(private[0].strip(), text)
                self.assertNotIn(" ".join(public), text)
                for directory in (self.runtime, self.root / "tasks", self.home):
                    if directory.exists():
                        for path in directory.rglob("*"):
                            if path.is_file():
                                self.assertNotIn(b"PRIVATE KEY-----", path.read_bytes(), str(path))
                keys.append(public)
                runtime_dirs.append(outputs["master_runtime_dir"])
        self.assertEqual(len({tuple(key) for key in keys}), 3)
        self.assertEqual(len(set(runtime_dirs)), 3, "Job IDs must vary even for repeated submissions in one attempt")
        secret_creates = [call["args"] for call in self.calls("kubectl") if call["args"][0] == "create"
                          and Path(call["args"][call["args"].index("-f") + 1]).is_relative_to(self.shm)]
        self.assertEqual(len(secret_creates), 3)
        self.assertTrue(all(not Path(args[args.index("-f") + 1]).exists() for args in secret_creates),
                        "Temporary Secret JSON must be removed after create")

    def test_master_custom_ssh_port_reaches_runtime_without_changing_rendezvous(self):
        for port in ("2223", "65535"):
            with self.subTest(port=port):
                self.submit_master(INPUT_SSH_PORT=port)
                ports = self.manifest("service.yaml")["spec"]["ports"]
                self.assertTrue(any(str(item["port"]) == "29500" for item in ports))
                for index in range(2):
                    container = self.worker_manifest(index)["spec"]["containers"][0]
                    values = {item["name"]: item.get("value") for item in container["env"]}
                    self.assertEqual(values["PPU_SSH_PORT"], port)
                    self.assertEqual(values["MASTER_PORT"], "29500")

    def test_master_monitor_completes_from_master_only_and_drains_logs(self):
        self.configure([pod(0, "Succeeded", EPOCH, started=EPOCH, finished=EPOCH + 5),
                        pod(1, "Running", EPOCH, started=EPOCH)],
                       logs=["master early", "master complete"])
        rc, outputs, text = self.run_script("monitor.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2")
        self.assertEqual(rc, 0, text)
        self.assert_reported(outputs, "succeeded", 2)
        self.assertIn("[worker-0] master early\n", text)
        self.assertIn("[worker-0] master complete\n", text)
        self.assertEqual(int((self.root / "clock").read_text()), EPOCH)

    def test_master_monitor_worker_failure_overrides_master_success(self):
        for master_phase in ("Running", "Succeeded"):
            with self.subTest(master_phase=master_phase):
                self.configure([pod(0, master_phase, EPOCH, started=EPOCH,
                                    finished=EPOCH + 5 if master_phase == "Succeeded" else None),
                                pod(1, "Failed", EPOCH, started=EPOCH, finished=EPOCH + 4)])
                rc, outputs, text = self.run_script("monitor.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2")
                self.assertEqual(rc, 0, text)
                self.assert_reported(outputs, "failed", 2)
                self.assertEqual(int((self.root / "clock").read_text()), EPOCH)

    def test_master_monitor_missing_or_abnormal_worker_never_reports_success(self):
        for state in ("missing", "Unknown", "Succeeded", "deleting", "init-failed"):
            with self.subTest(state=state):
                master = pod(0, "Succeeded", EPOCH, started=EPOCH, finished=EPOCH + 5)
                worker = pod(1, state if state in ("Unknown", "Succeeded") else "Pending", EPOCH,
                             started=EPOCH if state == "Succeeded" else None,
                             finished=EPOCH + 2 if state == "Succeeded" else None)
                if state == "deleting":
                    worker["metadata"]["deletionTimestamp"] = timestamp(EPOCH + 1)
                if state == "init-failed":
                    worker["status"]["initContainerStatuses"] = [{"name": "setup", "state": {
                        "terminated": {"exitCode": 7, "reason": "Error", "finishedAt": timestamp(EPOCH + 2)}}}]
                self.configure([master] if state == "missing" else [master, worker])
                rc, outputs, text = self.run_script("monitor.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2")
                self.assertEqual(rc, 0, text)
                self.assertEqual(outputs["job_status"], "failed", text)
                self.assertEqual(int((self.root / "clock").read_text()), EPOCH,
                                 "Broken SSH workers must fail without an unbounded wait")

    def test_master_monitor_missing_master_fails(self):
        self.configure([pod(1, "Running", EPOCH, started=EPOCH)])
        rc, outputs, text = self.run_script("monitor.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2")
        self.assertEqual(rc, 0, text)
        self.assertEqual(outputs["job_status"], "failed")
        self.assertEqual(int((self.root / "clock").read_text()), EPOCH)

    def test_master_monitor_uses_master_finished_at_at_shared_deadline(self):
        for finished, expected in ((55, "succeeded"), (60, "succeeded"), (60.9, "timeout"), (61, "timeout"), (None, "timeout")):
            with self.subTest(finished=finished):
                master = pod(0, "Succeeded", EPOCH, started=EPOCH,
                             finished=EPOCH + finished if finished is not None else None)
                self.configure([master, pod(1, "Running", EPOCH, started=EPOCH)], now=EPOCH + 65)
                rc, outputs, text = self.run_script("monitor.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2")
                self.assertEqual(rc, 0, text)
                self.assert_reported(outputs, expected, 2)

    def test_master_monitor_node_not_ready_is_bounded_by_original_deadline(self):
        worker = pod(1, "Running", EPOCH, started=EPOCH)
        worker["status"]["conditions"].append({"type": "Ready", "status": "False", "reason": "NodeNotReady"})
        worker["status"]["reason"] = "NodeNotReady"
        self.configure([pod(0, "Running", EPOCH, started=EPOCH), worker], block_logs=True)
        rc, outputs, text = self.run_script("monitor.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2")
        self.assertEqual(rc, 0, text)
        self.assertIn(outputs["job_status"], ("failed", "timeout"))
        self.assertLessEqual(int((self.root / "clock").read_text()), EPOCH + 60)

    def test_master_monitor_ready_worker_loss_has_bounded_grace_and_can_recover(self):
        for recover, expected, finished_at in ((False, "failed", 60), (True, "succeeded", 90)):
            with self.subTest(recover=recover):
                master = pod(0, "Running", EPOCH, started=EPOCH)
                ready = pod(1, "Running", EPOCH, started=EPOCH)
                ready["status"]["conditions"][0]["status"] = "True"
                lost = json.loads(json.dumps(ready))
                lost["status"]["conditions"][0].update(status="False", reason="NodeNotReady")
                frames = [(EPOCH + 30, [master, lost])]
                if recover:
                    frames += [(EPOCH + 55, [master, ready]),
                               (EPOCH + 85, [pod(0, "Succeeded", EPOCH, started=EPOCH,
                                                finished=EPOCH + 85), ready])]
                self.configure([master, ready], later=frames)
                rc, outputs, text = self.run_script(
                    "monitor.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2", JOB_DEADLINE=str(EPOCH + 300))
                self.assertEqual(rc, 0, text)
                self.assert_reported(outputs, expected, 2)
                self.assertEqual(int((self.root / "clock").read_text()), EPOCH + finished_at)

    def test_master_monitor_failed_master_obeys_finished_at_deadline(self):
        for finished, expected in ((55, "failed"), (60, "failed"), (60.9, "timeout"), (61, "timeout")):
            with self.subTest(finished=finished):
                self.configure([pod(0, "Failed", EPOCH, started=EPOCH, finished=EPOCH + finished),
                                pod(1, "Running", EPOCH, started=EPOCH)], now=EPOCH + 65)
                rc, outputs, text = self.run_script("monitor.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2")
                self.assertEqual(rc, 0, text)
                self.assert_reported(outputs, expected, 2)

    def test_master_framework_deadline_message_is_timeout_at_exact_or_fractional_deadline(self):
        for finished in (60, 60.9):
            with self.subTest(finished=finished):
                master = pod(0, "Failed", EPOCH, started=EPOCH, finished=EPOCH + finished)
                terminal = master["status"]["containerStatuses"][0]["state"]["terminated"]
                terminal.update(exitCode=124, message=json.dumps({"source": "cluster-master-runtime",
                                "outcome": "timeout", "deadline": EPOCH + 60, "rank": 0}))
                self.configure([master, pod(1, "Running", EPOCH, started=EPOCH)], now=EPOCH + 65)
                rc, outputs, text = self.run_script("monitor.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2")
                self.assertEqual(rc, 0, text)
                self.assert_reported(outputs, "timeout", 2)

    def test_idle_worker_deadline_cannot_override_on_time_master_success(self):
        for kind in ("runtime", "kubernetes"):
            with self.subTest(kind=kind):
                master = pod(0, "Succeeded", EPOCH, started=EPOCH, finished=EPOCH + 55)
                worker = pod(1, "Failed", EPOCH, started=EPOCH, finished=EPOCH + 60)
                terminal = worker["status"]["containerStatuses"][0]["state"]["terminated"]
                if kind == "runtime":
                    terminal.update(exitCode=124, message=json.dumps({"source": "cluster-master-runtime",
                                    "outcome": "timeout", "deadline": EPOCH + 60, "rank": 1}))
                else:
                    terminal["exitCode"] = 137
                    worker["status"]["reason"] = "DeadlineExceeded"
                self.configure([master, worker], now=EPOCH + 65)
                rc, outputs, text = self.run_script("monitor.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2")
                self.assertEqual(rc, 0, text)
                self.assert_reported(outputs, "succeeded", 2)

    def test_user_exit_124_and_unrelated_termination_messages_remain_failures(self):
        messages = ["", "[]", "not json",
                    json.dumps({"source": "cluster-master-runtime", "outcome": "command", "deadline": EPOCH + 60, "rank": 0}),
                    json.dumps({"source": "cluster-master-runtime", "outcome": "timeout", "deadline": EPOCH + 61, "rank": 0}),
                    json.dumps({"source": "cluster-master-runtime", "outcome": "timeout", "deadline": EPOCH + 60, "rank": 1})]
        for message in messages:
            with self.subTest(message=message):
                master = pod(0, "Failed", EPOCH, started=EPOCH, finished=EPOCH + 55)
                master["status"]["containerStatuses"][0]["state"]["terminated"].update(exitCode=124, message=message)
                self.configure([master, pod(1, "Running", EPOCH, started=EPOCH)], now=EPOCH + 65)
                rc, outputs, text = self.run_script("monitor.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2")
                self.assertEqual(rc, 0, text)
                self.assert_reported(outputs, "failed", 2)

    def test_genuine_worker_failure_wins_over_master_framework_timeout(self):
        master = pod(0, "Failed", EPOCH, started=EPOCH, finished=EPOCH + 60)
        master["status"]["containerStatuses"][0]["state"]["terminated"].update(
            exitCode=124, message=json.dumps({"source": "cluster-master-runtime", "outcome": "timeout",
                                             "deadline": EPOCH + 60, "rank": 0}))
        self.configure([master, pod(1, "Failed", EPOCH, started=EPOCH, finished=EPOCH + 55)], now=EPOCH + 65)
        rc, outputs, text = self.run_script("monitor.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2")
        self.assertEqual(rc, 0, text)
        self.assert_reported(outputs, "failed", 2)

    def test_master_monitor_cancellation_reaps_logs_and_preserves_terminal_record(self):
        self.configure([pod(i, "Running", EPOCH, started=EPOCH) for i in range(2)],
                       block_sleep=True, block_logs=True)
        task = self.root / "tasks" / (self.env["JOB_NAME"] + ".json")
        task.parent.mkdir()
        record = {"status": "timeout", "end_time": timestamp(EPOCH - 1)}
        task.write_text(json.dumps(record))
        process, output = self.start("monitor.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2",
                                     JOB_DEADLINE=str(EPOCH + 300))
        self.wait_for("sleep-waiting", process)
        self.wait_for("logs-started", process)
        process.send_signal(signal.SIGTERM)
        (self.root / "release-sleep").touch()
        rc, outputs, text = self.finish(process, output)
        self.assertEqual(rc, 130, text)
        self.assert_reported(outputs, "cancelled", 2)
        self.assertEqual(json.loads(task.read_text()), record)

    def test_master_cleanup_removes_only_job_runtime_and_secret_preserving_cache_and_status(self):
        self.assertIn("_", self.root.name)
        self.assertRegex(self.env["JOB_NAME"], r"^[a-z0-9][a-z0-9-]*$")
        self.configure([pod(0)])
        own = self.runtime / (self.env["JOB_NAME"] + "-0-1-fixture")
        sibling = self.runtime / "other-job-id"
        cache = self.root / "cache" / "openssh"
        for directory in (own, sibling, cache):
            directory.mkdir(parents=True)
            (directory / "marker").write_text("keep outside the current job")
        task = self.root / "tasks" / (self.env["JOB_NAME"] + ".json")
        task.parent.mkdir()
        record = {"status": "succeeded", "end_time": timestamp(EPOCH - 1)}
        task.write_text(json.dumps(record))
        rc, outputs, text = self.run_script(
            "cleanup.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2", MASTER_RUNTIME_DIR=str(own),
            INPUT_JOB_STATUS="scheduled", INPUT_WORKFLOW_STATUS="cancelled", INPUT_CLEANUP_POLICY="never")
        self.assertEqual(rc, 0, text)
        self.assertEqual(outputs["job_status"], "succeeded")
        self.assertEqual(json.loads(task.read_text()), record)
        self.assertFalse(own.exists())
        self.assertTrue((sibling / "marker").is_file())
        self.assertTrue((cache / "marker").is_file())
        self.assert_secret_deleted()
        deletes = {call["args"][1] for call in self.calls("kubectl") if call["args"][0] == "delete"}
        self.assertTrue({"pods", "svc", "podgroups.scheduling.x-k8s.io"}.issubset(deletes))

    def test_master_cleanup_attempts_secret_before_retries_of_blocked_deletes(self):
        self.configure([pod(0)], block_delete=True, query_error=42)
        rc, outputs, text = self.run_script(
            "cleanup.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2", INPUT_JOB_STATUS="failed")
        self.assertNotEqual(rc, 0, text)
        self.assertEqual(outputs["job_status"], "failed")
        deletes = [call["args"] for call in self.calls("kubectl") if call["args"][0] == "delete"]
        self.assertEqual([args[1] for args in deletes[:4]],
                         ["pods", "svc", "podgroups.scheduling.x-k8s.io", "secret"])
        self.assert_secret_deleted()
        self.assertLessEqual(int((self.root / "clock").read_text()), EPOCH + 60)

    def test_master_cleanup_deletes_secret_after_partial_submission_and_lost_runtime_output(self):
        for options in ({"secret_create_error": 42}, {"create_error_kind": "Pod", "create_error": 42}):
            with self.subTest(options=options):
                self.configure([pod(i, scheduled=EPOCH) for i in range(2)], **options)
                rc, outputs, text = self.run_script("submit.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2")
                self.assertNotEqual(rc, 0, text)
                self.assertEqual(outputs["job_status"], "failed")
                before = len(self.calls("kubectl"))
                rc, cleaned, text = self.run_script(
                    "cleanup.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2",
                    INPUT_JOB_STATUS="failed", MASTER_RUNTIME_DIR=outputs.get("master_runtime_dir", ""))
                self.assertEqual(rc, 0, text)
                self.assertEqual(cleaned["job_status"], "failed")
                self.assert_secret_deleted(before)
                before = len(self.calls("kubectl"))
                rc, _, text = self.run_script(
                    "cleanup.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2", MASTER_RUNTIME_DIR="")
                self.assertEqual(rc, 0, text)
                self.assert_secret_deleted(before)

    def test_master_submit_cancellation_hands_secret_and_runtime_to_full_cleanup(self):
        self.configure([pod(i) for i in range(2)], block_sleep=True)
        process, output = self.start("submit.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2",
                                     INPUT_TIMEOUT_MINUTES="5")
        self.wait_for("sleep-waiting", process)
        process.send_signal(signal.SIGTERM)
        (self.root / "release-sleep").touch()
        rc, outputs, text = self.finish(process, output)
        self.assertEqual(rc, 130, text)
        self.assert_reported(outputs, "cancelled", 2)
        self.secret()
        directory = Path(outputs["master_runtime_dir"])
        directory.mkdir(parents=True)
        (directory / "marker").touch()
        rc, cleaned, text = self.run_script(
            "cleanup.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2", MASTER_RUNTIME_DIR=str(directory),
            INPUT_JOB_STATUS=outputs["job_status"], INPUT_WORKFLOW_STATUS="cancelled")
        self.assertEqual(rc, 0, text)
        self.assertEqual(cleaned["job_status"], "cancelled")
        self.assertFalse(directory.exists())
        self.assert_secret_deleted()

    def test_master_cleanup_never_removes_runtime_root_or_unrelated_path(self):
        self.configure([pod(0)])
        cache = self.root / "cache"
        self.runtime.mkdir()
        cache.mkdir()
        for directory in (self.runtime, cache):
            (directory / "keep").write_text("unrelated data")
        link = self.runtime / (self.env["JOB_NAME"] + "-symlink")
        link.symlink_to(cache, target_is_directory=True)
        for directory in (self.runtime, cache, link):
            with self.subTest(directory=directory):
                rc, _, text = self.run_script(
                    "cleanup.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2", MASTER_RUNTIME_DIR=str(directory))
                self.assertNotEqual(rc, 0, "Unsafe cleanup handoff must be rejected: " + text)
                self.assertTrue((self.runtime / "keep").is_file())
                self.assertTrue((cache / "keep").is_file())

    def test_master_cleanup_single_pod_and_invalid_node_count_omit_secret(self):
        for nodes in ("0", "1", "invalid", "-1"):
            with self.subTest(nodes=nodes):
                self.configure([pod(0)])
                before = len(self.calls("kubectl"))
                rc, _, text = self.run_script("cleanup.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES=nodes)
                self.assertEqual(rc, 0, text)
                self.assertFalse(self.secret_deletes(before))

    def test_master_cleanup_attempts_secret_even_when_other_api_deletes_fail(self):
        self.configure([pod(0)], delete_error=42, query_error=42)
        rc, outputs, text = self.run_script(
            "cleanup.sh", INPUT_LAUNCH_MODE="master", INPUT_NNODES="2", INPUT_JOB_STATUS="timeout")
        self.assertNotEqual(rc, 0, "Unconfirmed master cleanup must not claim success: " + text)
        self.assertEqual(outputs["job_status"], "timeout")
        self.assert_secret_deleted()
        self.assertLessEqual(int((self.root / "clock").read_text()), EPOCH + 60)
        self.assertNotIn("All associated pods have terminated", text)

    def test_master_source_directory_is_absolute_after_staging(self):
        archive = self.root / "source.tar.gz"
        source = self.root / "required.txt"
        source.write_text("staged")
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(source, arcname="required.txt")
        for staged in (False, True):
            for relative in (False, True):
                with self.subTest(staged=staged, relative=relative):
                    directory = self.root / (f"source-{staged}-{relative} quoted")
                    directory.mkdir()
                    (directory / "required.txt").write_text("existing")
                    self.submit_master(2, SOURCE_STAGE_DIR=str(archive) if staged else "",
                                       INPUT_SOURCE_DIR=directory.name if relative else str(directory))
                    container = self.worker_manifest()["spec"]["containers"][0]
                    values = {item["name"]: item["value"] for item in container["env"] if "value" in item}
                    prefix = container["args"][0].split("mkdir -p /run/cluster", 1)[0]
                    result = subprocess.run(["/bin/bash", "-c", prefix +
                                             '\ntest "$SOURCE_DIR" = "$PWD"\ntest -f "$SOURCE_DIR/required.txt"'],
                                            cwd=self.root, env={**self.env, **values},
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual((directory / "required.txt").read_text(), "staged" if staged else "existing")

    def test_source_extraction_failure_stops_multiline_workload(self):
        archive = self.root / "broken source.tar.gz"
        archive.write_bytes(b"not a tar archive")
        marker = self.root / "should-not-exist"
        self.configure([pod(0, scheduled=EPOCH)])
        rc, outputs, text = self.run_script(
            "submit.sh", SOURCE_STAGE_DIR=str(archive),
            INPUT_SOURCE_DIR=str(self.root / "source 'quoted"),
            INPUT_COMMAND="set -euo pipefail\ntouch " + shlex.quote(str(marker)) + "\nexit 0")
        self.assertEqual(rc, 0, text)
        self.assertEqual(outputs["job_status"], "scheduled")
        command = self.worker_manifest()["spec"]["containers"][0]["args"][0]
        result = subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(marker.exists())

    def test_source_extraction_failure_prevents_setup_and_workload(self):
        archive = self.root / "broken-source.tar.gz"
        archive.write_bytes(b"invalid tar")
        setup_marker = self.root / "setup-ran"
        command_marker = self.root / "command-ran"
        self.configure([pod(0, scheduled=EPOCH)])
        rc, _, text = self.run_script(
            "submit.sh", SOURCE_STAGE_DIR=str(archive), INPUT_SOURCE_DIR=str(self.root / "source"),
            INPUT_SETUP_COMMAND="touch " + shlex.quote(str(setup_marker)),
            INPUT_COMMAND="touch " + shlex.quote(str(command_marker)))
        self.assertEqual(rc, 0, text)
        command = self.worker_manifest()["spec"]["containers"][0]["args"][0]
        result = subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(setup_marker.exists())
        self.assertFalse(command_marker.exists())

    def test_source_extraction_preserves_paths_and_workload_exit_code(self):
        archive = self.root / "valid source.tar.gz"
        source = self.root / "required.txt"
        source.write_text("source transferred")
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(source, arcname="required.txt")
        self.configure([pod(0, scheduled=EPOCH)])
        rc, outputs, text = self.run_script(
            "submit.sh", SOURCE_STAGE_DIR=str(archive),
            INPUT_SOURCE_DIR=str(self.root / "source 'quoted"),
            INPUT_COMMAND="set -euo pipefail\ntest -f required.txt\nexit 7")
        self.assertEqual(rc, 0, text)
        self.assertEqual(outputs["job_status"], "scheduled")
        command = self.worker_manifest()["spec"]["containers"][0]["args"][0]
        result = subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual((self.root / "source 'quoted" / "required.txt").read_text(), "source transferred")

    def test_810e_board_alias_defaults_and_selector(self):
        for cards in (1, 8, 16, 32):
            for board in ("OAM-810E", "ZW-810E"):
                with self.subTest(cards=cards, board=board):
                    self.configure([pod(0, scheduled=EPOCH)])
                    rc, outputs, text = self.run_script(
                        "submit.sh", INPUT_NPROC_PER_NODE=str(cards),
                        INPUT_NODE_SELECTOR="board-type=" + board + ",pool=fixture")
                    self.assertEqual(rc, 0, text)
                    self.assertEqual(outputs["job_status"], "scheduled")
                    spec = self.worker_manifest()["spec"]
                    expected_board = "OAM-810E" if board == "ZW-810E" else board
                    self.assertEqual(spec["nodeSelector"], {"board-type": expected_board, "pool": "fixture"})
                    resources = spec["containers"][0]["resources"]
                    self.assertEqual(resources["limits"]["cpu"], str(min(cards * 11, 176)))
                    self.assertEqual(resources["limits"]["memory"], str(min(cards * 112, 1792)) + "Gi")
                    for key in ("limits", "requests"):
                        self.assertEqual(resources[key]["alibabacloud.com/ppu"], str(cards))

    def test_810e_board_alias_preserves_explicit_limits(self):
        for options, cpu, memory in (("cpus: 500m", "500m", "896Gi"),
                                     ("memory: 64Gi", "88", "64Gi"),
                                     ("cpus: 1.5\nmemory: 64Gi", "1.5", "64Gi")):
            with self.subTest(options=options):
                self.configure([pod(0, scheduled=EPOCH)])
                rc, _, text = self.run_script(
                    "submit.sh", INPUT_NODE_SELECTOR="board-type=ZW-810E", CONTAINER_OPTIONS=options)
                self.assertEqual(rc, 0, text)
                spec = self.worker_manifest()["spec"]
                self.assertEqual(spec["nodeSelector"]["board-type"], "OAM-810E")
                resources = spec["containers"][0]["resources"]
                self.assertEqual(resources["limits"]["cpu"], cpu)
                self.assertEqual(resources["limits"]["memory"], memory)

    def test_master_address_for_single_pod_and_gang(self):
        for nodes, cards in ((0, 1), (0, 8), (2, 8)):
            with self.subTest(nodes=nodes, cards=cards):
                self.configure([pod(i, scheduled=EPOCH) for i in range(nodes or 1)])
                rc, _, text = self.run_script(
                    "submit.sh", INPUT_NNODES=str(nodes), INPUT_NPROC_PER_NODE=str(cards))
                self.assertEqual(rc, 0, text)
                env = {item["name"]: item.get("value") for item in
                       self.worker_manifest()["spec"]["containers"][0]["env"]}
                expected = (self.env["JOB_NAME"] + "-worker-0." + self.env["JOB_NAME"]
                            + ".fixture.svc.cluster.local") if nodes else "127.0.0.1"
                self.assertEqual(env["MASTER_ADDR"], expected)

    def test_user_environment_preserves_trailing_newlines(self):
        values = {"EMPTY": "", "MULTILINE": "line one\nline two\n\n",
                  "QUOTED": '"quoted"\\value\n', "SENTINEL": "ends with.\n"}
        self.configure([pod(0, scheduled=EPOCH)])
        rc, _, text = self.run_script("submit.sh", USER_ENV_JSON=json.dumps(values))
        self.assertEqual(rc, 0, text)
        env = {item["name"]: item.get("value") for item in
               self.worker_manifest()["spec"]["containers"][0]["env"]}
        for key, value in values.items():
            self.assertEqual(env[key], value)

    def test_shared_memory_quantity_suffixes(self):
        for size in ("512Ki", "512Mi", "8Gi", "1.5Gi", "512k", "8M", "1024", "512ki"):
            with self.subTest(size=size):
                self.configure([pod(0, scheduled=EPOCH)])
                rc, _, text = self.run_script("submit.sh", CONTAINER_OPTIONS="shm_size: " + size)
                if size == "512ki":
                    self.assertNotEqual(rc, 0, text)
                else:
                    self.assertEqual(rc, 0, text)
                    shm = next(volume for volume in self.worker_manifest()["spec"]["volumes"]
                               if volume["name"] == "dshm")
                    self.assertEqual(str(shm["emptyDir"]["sizeLimit"]), size)

    def test_monitor_fetches_terminal_logs_after_initial_failure(self):
        self.configure([pod(0, "Succeeded", EPOCH, started=EPOCH, finished=EPOCH + 1)],
                       first_log_error=True, logs=["early output", "final output"])
        rc, outputs, text = self.run_script("monitor.sh")
        self.assertEqual(rc, 0, text)
        self.assertEqual(outputs["job_status"], "succeeded")
        self.assertIn("[worker-0] early output\n", text)
        self.assertIn("[worker-0] final output\n", text)
        logs = [call["args"] for call in self.calls("kubectl") if call["args"][0] == "logs"]
        self.assertEqual(len(logs), 2)
        self.assertIn("-f", logs[0])
        self.assertNotIn("-f", logs[1])
        final_timeout = [call["args"] for call in self.calls("timeout")
                         if "logs" in call["args"] and "-f" not in call["args"]]
        self.assertEqual(final_timeout[0][2], "10s")

    def test_submission_waits_for_old_pods_within_original_budget(self):
        self.configure([pod(0)], stale_pods_until=EPOCH + 120)
        rc, outputs, text = self.run_script("submit.sh")
        self.assertEqual(rc, 0, text)
        self.assertEqual(outputs["job_status"], "timeout")
        self.assertEqual(outputs["start_time"], str(EPOCH))
        self.assertEqual(outputs["duration_seconds"], "60")
        self.assertFalse(any(call["args"][0] == "create" for call in self.calls("kubectl")))

    def test_submission_waits_for_all_gang_resources(self):
        for resource in ("svc", "podgroups.scheduling.x-k8s.io"):
            for removed_at, expected in ((EPOCH + 9, "scheduled"), (EPOCH + 120, "timeout")):
                with self.subTest(resource=resource, removed_at=removed_at):
                    before = len(self.calls())
                    self.configure([pod(i, scheduled=EPOCH) for i in range(2)],
                                   **{"stale_" + resource + "_until": removed_at})
                    rc, outputs, text = self.run_script("submit.sh", INPUT_NNODES="2")
                    self.assertEqual(rc, 0, text)
                    self.assertEqual(outputs["job_status"], expected)
                    now = int((self.root / "clock").read_text())
                    self.assertEqual(now, EPOCH + (9 if expected == "scheduled" else 60))
                    creates = [call for call in self.calls()[before:]
                               if call["tool"] == "kubectl" and call["args"][0] == "create"]
                    self.assertEqual(len(creates), 4 if expected == "scheduled" else 0)

    def test_submission_aborts_on_gang_resource_query_failure(self):
        for resource in ("svc", "podgroups.scheduling.x-k8s.io"):
            with self.subTest(resource=resource):
                self.configure([pod(i, scheduled=EPOCH) for i in range(2)],
                               gang_query_error=resource)
                rc, outputs, text = self.run_script("submit.sh", INPUT_NNODES="2")
                self.assertEqual(rc, 42, text)
                self.assertEqual(outputs["job_status"], "failed")
                self.assertFalse(any(call["args"][0] == "create" for call in self.calls("kubectl")))

    def test_submission_aborts_on_delete_or_query_failure(self):
        for options in ({"delete_error": 42}, {"query_error": 42}):
            with self.subTest(options=options):
                self.configure([pod(0)], **options)
                rc, outputs, _ = self.run_script("submit.sh")
                self.assertNotEqual(rc, 0)
                self.assertEqual(outputs["job_status"], "failed")
                self.assertFalse(any(call["args"][0] == "create" for call in self.calls("kubectl")))

    def test_submission_bounds_blocked_delete(self):
        self.configure([pod(0)], block_delete=True)
        rc, outputs, _ = self.run_script("submit.sh")
        self.assertNotEqual(rc, 0)
        self.assertEqual(outputs["job_status"], "failed")
        self.assertLessEqual(int((self.root / "clock").read_text()) - EPOCH, 60)
        self.assertTrue(any("delete" in call["args"] for call in self.calls("timeout")))
        self.assertFalse(any(call["args"][0] == "create" for call in self.calls("kubectl")))

    def test_monitor_refreshes_terminal_state_at_last_poll(self):
        running = [pod(0, "Running", EPOCH, started=EPOCH)]
        complete = [pod(0, "Succeeded", EPOCH, started=EPOCH, finished=EPOCH + 55)]
        self.configure(running, later=[(EPOCH + 55, complete)])
        rc, outputs, text = self.run_script("monitor.sh")
        self.assertEqual(rc, 0, text)
        self.assert_reported(outputs, "succeeded", 1)

    def test_monitor_distinguishes_on_time_and_late_terminal_states(self):
        for phase, finish, expected in (("Succeeded", 55, "succeeded"),
                                        ("Succeeded", 60, "succeeded"),
                                        ("Succeeded", 61, "timeout"),
                                        ("Failed", 55, "failed"),
                                        ("Failed", 61, "timeout")):
            with self.subTest(phase=phase, finish=finish):
                self.configure([pod(0, phase, EPOCH, started=EPOCH, finished=EPOCH + finish)],
                               now=EPOCH + 65)
                rc, outputs, text = self.run_script("monitor.sh")
                self.assertEqual(rc, 0, text)
                self.assert_reported(outputs, expected, 1)
        self.configure([pod(0, "Succeeded", EPOCH, started=EPOCH, finished=EPOCH + 55),
                        pod(1, "Succeeded", EPOCH, started=EPOCH, finished=EPOCH + 61)],
                       now=EPOCH + 65)
        rc, outputs, text = self.run_script("monitor.sh", INPUT_NNODES="2")
        self.assertEqual(rc, 0, text)
        self.assert_reported(outputs, "timeout", 2)

    def test_monitor_query_failure_classification_at_deadline(self):
        for now, advance, expected in ((EPOCH + 59, 0, "failed"),
                                       (EPOCH + 59, 1, "timeout"),
                                       (EPOCH + 65, 0, "timeout")):
            with self.subTest(now=now, advance=advance):
                (self.root / "query-failed").unlink(missing_ok=True)
                self.configure([pod(0, "Running", EPOCH, started=EPOCH)],
                               now=now, pod_query_error=42, pod_query_error_once=True,
                               pod_query_error_advance=advance)
                rc, outputs, text = self.run_script("monitor.sh")
                self.assertEqual(rc, 42, text)
                self.assert_reported(outputs, expected, 1)

    def test_cleanup_is_unconditional_for_all_policy_values(self):
        self.configure([pod(0)])
        for policy in ("always", "on_success", "never"):
            with self.subTest(policy=policy):
                before = len(self.calls())
                rc, outputs, text = self.run_script(
                    "cleanup.sh", INPUT_NNODES="2", INPUT_CLEANUP_POLICY=policy,
                    INPUT_JOB_STATUS="failed")
                self.assertEqual(rc, 0, text)
                self.assertEqual(outputs["job_status"], "failed")
                deletes = [call for call in self.calls()[before:]
                           if call["tool"] == "kubectl" and call["args"][0] == "delete"]
                self.assertEqual({call["args"][1] for call in deletes},
                                 {"pods", "svc", "podgroups.scheduling.x-k8s.io"})
                self.assertTrue(all("--wait=false" in call["args"] for call in deletes))

    def test_cleanup_force_deletes_finalized_pods_within_budget(self):
        self.configure([pod(0)], remaining_pods=True)
        rc, _, text = self.run_script("cleanup.sh")
        self.assertEqual(rc, 0, text)
        self.assertTrue((self.root / "forced").exists())
        self.assertLessEqual(int((self.root / "clock").read_text()) - EPOCH, 60)
        self.assertNotIn("All associated pods have terminated", text)

    def test_cleanup_does_not_treat_query_failure_as_empty(self):
        self.configure([pod(0)], query_error=42)
        rc, _, text = self.run_script("cleanup.sh")
        self.assertEqual(rc, 0, text)
        self.assertLessEqual(int((self.root / "clock").read_text()) - EPOCH, 60)
        self.assertNotIn("All associated pods have terminated", text)
        self.assertTrue((self.root / "forced").exists())

    def test_cleanup_bounds_blocked_api_requests(self):
        self.configure([pod(0)], block_delete=True, query_error=42)
        rc, _, text = self.run_script("cleanup.sh")
        self.assertEqual(rc, 0, text)
        self.assertLessEqual(int((self.root / "clock").read_text()) - EPOCH, 60)
        self.assertNotIn("All associated pods have terminated", text)

    def test_cleanup_attempts_every_gang_resource_before_retries(self):
        self.configure([pod(0)], block_delete=True, query_error=42)
        rc, _, text = self.run_script("cleanup.sh", INPUT_NNODES="2")
        self.assertEqual(rc, 0, text)
        deletes = [call["args"] for call in self.calls("kubectl") if call["args"][0] == "delete"]
        self.assertEqual([args[1] for args in deletes[:3]],
                         ["pods", "svc", "podgroups.scheduling.x-k8s.io"])
        self.assertLessEqual(int((self.root / "clock").read_text()) - EPOCH, 60)
        self.assertTrue(any("--force" in args for args in deletes))
        self.assertNotIn("All associated pods have terminated", text)

    def test_dmesg_enabled_policies_keep_the_same_collection_contract(self):
        commands = {}
        for policy in ("never", "on_failure", "always"):
            self.configure([pod(0, scheduled=EPOCH)])
            rc, _, text = self.run_script("submit.sh", INPUT_DMESG_POLICY=policy)
            self.assertEqual(rc, 0, text)
            commands[policy] = self.worker_manifest()["spec"]["containers"][0]["args"][0]
        self.assertNotIn("_ppu_dmesg_begin", commands["never"])
        self.assertIn("_ppu_dmesg_begin", commands["always"])
        self.assertEqual(commands["on_failure"], commands["always"])

    def test_report_card_hours_use_pod_timestamps(self):
        self.configure([
            pod(0, "Succeeded", EPOCH + 600, started=EPOCH + 900, finished=EPOCH + 4500),
            pod(1, "Running", EPOCH + 1200, created=EPOCH + 60, started=EPOCH + 1500),
            pod(2, scheduled=EPOCH + 1800, created=EPOCH + 120),
            pod(3, created=EPOCH + 180)], now=EPOCH + 7200)
        rc, outputs, text = self.run_script("report-job.sh", JOB_STATUS="timeout")
        self.assertEqual(rc, 0, text)
        self.assert_reported(outputs, "timeout", 4)
        self.assertEqual(outputs["duration_seconds"], "7200")
        self.assertEqual(outputs["queue_duration_seconds"], "7200")
        self.assertEqual(outputs["unscheduled_pods"], "1")
        self.assertEqual(outputs["pod_queue"], "worker-0:600:1,worker-1:1140:1,worker-2:1680:1,worker-3:7020:0")
        self.assertEqual(outputs["queue_reason"], "Insufficient PPU capacity")
        self.assertEqual(outputs["occupied_ppu_card_hours"], "34.0000")
        self.assertEqual(outputs["execution_ppu_card_hours"], "20.6667")
        self.assertFalse(self.calls("sleep"))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fake":
        sys.exit(fake_command(sys.argv[2], sys.argv[3:]))
    unittest.main(verbosity=2)
