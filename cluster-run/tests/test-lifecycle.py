#!/usr/bin/env python3
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
        if fixture.get("block_sleep") and seconds >= 30:
            (root / "sleep-waiting").touch()
            while not (root / "release-sleep").exists():
                time.sleep(0.01)
        time.sleep(0.03)
        clock(seconds)
        return 0

    if tool == "timeout":
        command = next(i for i, arg in enumerate(args) if arg == "kubectl")
        child = subprocess.Popen(args[command:])

        def forward(signum, frame):
            if child.poll() is None:
                child.send_signal(signum)

        signal.signal(signal.SIGTERM, forward)
        signal.signal(signal.SIGINT, forward)
        try:
            limit = 0.15 if fixture.get("block_delete") and args[command + 1] == "delete" else 10
            returncode = child.wait(timeout=limit)
            return returncode if returncode >= 0 else 128 - returncode
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            clock(int(args[command - 1].removesuffix("s")))
            return 124

    if tool == "kubectl":
        if args[0] != "logs" and not any(arg.startswith("--request-timeout=") for arg in args):
            raise ValueError("API requests must be bounded")
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
            (root / "manifests").mkdir(exist_ok=True)
            (root / "manifests" / path.name).write_text(path.read_text())
            return 0
        if args[:2] == ["get", "crd"]:
            return 0
        if args[0] == "logs":
            if args[1] != os.environ["JOB_NAME"] + "-worker-0":
                raise ValueError("Only worker-0 logs may be streamed")
            if any(arg.startswith(("--tail", "--since")) for arg in args):
                raise ValueError("Early logs must not be filtered")
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
                print(next(item["status"]["phase"] for item in items
                           if item["metadata"]["name"] == name))
            else:
                print(json.dumps({"items": items}))
            return 0
    raise ValueError(f"Unexpected fake command: {tool} {args}")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(shutil.which("jq"), "jq is required by the lifecycle scripts")
        self.temp = tempfile.TemporaryDirectory(prefix="ppu-lifecycle-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scripts = self.root / "scripts"
        shutil.copytree(SCRIPTS, self.scripts)
        for path in self.scripts.glob("*.sh"):
            path.write_text(path.read_text()
                            .replace("/tmp/training-manifests", str(self.root / "manifests"))
                            .replace("/wl_nas/devops/ppu-dashboard/tasks", str(self.root / "tasks")))
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
                                    "USER_ENV_JSON", "SOURCE_STAGE_DIR", "CONTAINER_OPTIONS")}
        self.env.update(
            PATH=str(self.bin) + os.pathsep + os.defpath + os.pathsep
            + str(Path(shutil.which("jq")).parent),
            LIFECYCLE_FIXTURE=str(self.root), JOB_NAME=self.root.name,
            INPUT_IMAGE="fixture:latest", INPUT_COMMAND="exit 0",
            INPUT_NAMESPACE="fixture", INPUT_NNODES="0", INPUT_NPROC_PER_NODE="8",
            INPUT_TIMEOUT_MINUTES="1", JOB_START_TIME=str(EPOCH),
            JOB_DEADLINE=str(EPOCH + 60), GITHUB_REPOSITORY="fixture/repository",
            GITHUB_JOB_NAME="lifecycle-test")
        self.processes = []
        self.addCleanup(self.stop_processes)

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
        process = subprocess.Popen(
            ["/bin/bash", str(self.scripts / script)], env={**self.env, **env, "GITHUB_OUTPUT": str(output)},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.processes.append(process)
        return process, output

    def finish(self, process, output):
        try:
            text, _ = process.communicate(timeout=15)
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
        deadline = time.monotonic() + 5
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
        script = (SCRIPTS / "cleanup.sh").read_text().replace(
            'TASK_DIR="/wl_nas/devops/ppu-dashboard/tasks"',
            "TASK_DIR=" + shlex.quote(str(self.root))).replace(
                "/tmp/training-manifests", str(self.root / "manifests"))
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

    def worker_manifest(self):
        path = self.root / "manifests" / "pod-worker-0.yaml"
        return json.loads(subprocess.check_output(
            ["ruby", "-ryaml", "-rjson", "-e",
             "puts JSON.generate(YAML.safe_load(STDIN.read, aliases: true))"],
            input=path.read_text(), text=True, stderr=subprocess.DEVNULL))

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

    def test_submission_waits_for_old_pods_within_original_budget(self):
        self.configure([pod(0)], stale_pods_until=EPOCH + 120)
        rc, outputs, text = self.run_script("submit.sh")
        self.assertEqual(rc, 0, text)
        self.assertEqual(outputs["job_status"], "timeout")
        self.assertEqual(outputs["start_time"], str(EPOCH))
        self.assertEqual(outputs["duration_seconds"], "60")
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
