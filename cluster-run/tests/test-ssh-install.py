#!/usr/bin/env python3
"""Isolated installer tests. No real APT, root writes, network or NAS access.

The fake gpgv represents a cryptographic trust oracle: only test-issued signed
Release bytes authenticated by the LOCAL keyring pass. Hashes/Packages/debs are
real bytes and SHA256 operations, so writable-cache edits cannot bless themselves.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install-ssh.sh"
EPOCH = 1900000000
PACKAGES = {
    "openssh-server": "1:9.6p1-3ubuntu13",
    "openssh-client": "1:9.6p1-3ubuntu13",
    "jq": "1.7.1-3",
    "libjq1": "1.7.1-3",
    "libonig5": "6.9.9-1",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def wrapper(tool, driver):
    return ("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
            + shlex.quote(str(driver)) + " --fake " + tool + ' "$@"\n')


def config_paths():
    config = Path(os.environ["APT_CONFIG"]).read_text()
    return {key: Path(value) for key, value in re.findall(r'([\w:.-]+) "([^"]+)";', config)}


def fake_command(tool, args):
    root = Path(os.environ["SSH_INSTALL_FIXTURE"])
    fixture = json.loads((root / "fixture.json").read_text())
    with (root / "calls.jsonl").open("a") as calls:
        calls.write(json.dumps({"tool": tool, "args": args,
                               "bounded": os.environ.get("SSH_INSTALL_TIMEOUT")}) + "\n")
    clock_path = root / "clock"
    if tool == "date":
        if args == ["+%s"]:
            print(clock_path.read_text())
        elif args[:2] == ["-u", "-d"]:
            # All dates are RFC2822 UTC, just like distro Release files.
            dt = datetime.datetime.strptime(args[2], "%a, %d %b %Y %H:%M:%S UTC")
            print(int(dt.replace(tzinfo=datetime.timezone.utc).timestamp()))
        else:
            raise ValueError(args)
        return 0
    if tool == "sleep":
        clock_path.write_text(str(int(clock_path.read_text()) + int(args[0])))
        return 0
    if tool == "id":
        print(fixture.get("uid", 0))
        return 0
    if tool == "timeout":
        index = next(i for i, arg in enumerate(args) if re.fullmatch(r"\d+s", arg))
        command = args[index + 1:]
        duration = int(args[index][:-1])
        if fixture.get("timeout_tool") == Path(command[0]).name:
            clock_path.write_text(str(int(clock_path.read_text()) + duration))
            return 124
        return subprocess.run(command, env={**os.environ,
                              "SSH_INSTALL_TIMEOUT": str(duration)}).returncode
    if tool in ("mkdir", "mktemp", "mv", "cp"):
        nas = str(root / "nas")
        if tool == "mkdir" and fixture.get("cache_readonly") and any(a.startswith(nas) for a in args):
            return 1
        if tool == "mkdir" and fixture.get("lock_busy") and args[-1].endswith("/.lock"):
            return 1
        if fixture.get("root_readonly") and tool == "mktemp" and any("/.ppu-ssh-write." in a for a in args):
            return 1
        if fixture.get("backfill_fail") and tool == "mktemp" and any("/.publish." in a for a in args):
            return 1
        if tool == "cp" and fixture.get("race_after_copy") and args[-1].endswith("/snapshot"):
            result = subprocess.run([os.environ["SSH_REAL_CP"], *args]).returncode
            for deb in (Path(args[-2]) / "debs").glob("*.deb"):
                deb.write_bytes(b"NAS changed after local copy")
            return result
        return subprocess.run([os.environ["SSH_REAL_" + tool.upper()], *args]).returncode
    if tool == "dpkg":
        if args == ["--print-architecture"]:
            print(fixture.get("arch", "amd64"))
            return 0
        if args[:1] == ["--compare-versions"]:
            # Enough Debian version ordering for the documented minimum/reuse tests.
            value = args[1].split(":", 1)[-1]
            minimum = args[3].split(":", 1)[-1]
            return int(tuple(int(n) for n in re.findall(r"\d+", value)[:2]) <
                       tuple(int(n) for n in re.findall(r"\d+", minimum)[:2]))
        raise ValueError(args)
    if tool == "dpkg-query":
        if args[-1] in PACKAGES:
            if not (root / "installed").exists():
                return 1
            name = args[-1]
            version = fixture.get("installed_versions", {}).get(name, PACKAGES[name])
            print("install ok installed|" + version)
        else:
            print("base-files|" + fixture.get("base", "13ubuntu10") + "|" +
                  fixture.get("arch", "amd64") + "|install ok installed")
            if (root / "installed").exists():
                for name, version in PACKAGES.items():
                    print(f"{name}|{version}|amd64|install ok installed")
        return 0
    if tool == "dpkg-deb":
        try:
            deb = json.loads(Path(args[1]).read_text())
        except (ValueError, OSError):
            return 1
        for name in args[2:]:
            print(name + ": " + deb[name])
        return 0
    if tool == "gpgv":
        if not os.environ.get("SSH_INSTALL_TIMEOUT"):
            raise ValueError("gpgv must be deadline-bounded")
        key = Path(args[args.index("--keyring") + 1])
        if "/nas/" in str(key) or key.read_text() != "local distro trust root\n":
            raise ValueError("NAS cannot supply a trust root")
        issued = json.loads((root / "issued.json").read_text())
        if "--output" in args:
            envelope = Path(args[-1]).read_bytes()
            if digest(envelope) not in issued:
                return 1
            Path(args[args.index("--output") + 1]).write_text(issued[digest(envelope)])
        else:
            signature, release = Path(args[-2]).read_bytes(), Path(args[-1]).read_text()
            if issued.get(digest(signature)) != release:
                return 1
        return 0
    if tool == "apt-helper":
        if fixture.get("helper_unsupported"):
            return 1
        if args[0] != "cat-file" or not os.environ.get("SSH_INSTALL_TIMEOUT"):
            raise ValueError("only deadline-bounded cat-file is supported")
        data = Path(args[1]).read_bytes()
        if data.startswith(b"\x1f\x8b"):
            import gzip
            data = gzip.decompress(data)
        sys.stdout.buffer.write(data)
        return 0
    if tool == "apt-get":
        if not os.environ.get("SSH_INSTALL_TIMEOUT"):
            raise ValueError("APT must be deadline-bounded")
        paths = config_paths()
        config = Path(os.environ["APT_CONFIG"]).read_text()
        for directive in ('APT::Get::AllowUnauthenticated "false"',
                          'Acquire::AllowInsecureRepositories "false"',
                          'Acquire::AllowWeakRepositories "false"',
                          'Dir::Etc::main "/dev/null"'):
            if directive not in config:
                raise ValueError("missing APT trust isolation: " + directive)
        if "DPkg::Lock::Timeout=" not in " ".join(args):
            raise ValueError("APT lock is not deadline-bounded")
        if fixture.get("offline") and ("update" in args or
                                       ("--download-only" in args and "--no-download" not in args)):
            return 100
        lists = paths["Dir::State::lists"]
        archives = paths["Dir::Cache::archives"]
        repo = root / "repo"
        if "update" in args:
            if fixture.get("update_fail"):
                return 100
            for source in (repo / "metadata").iterdir():
                shutil.copy2(source, lists / source.name)
            return 0
        if "indextargets" in args:
            format_string = args[args.index("--format") + 1]
            for source in lists.glob("*_Packages*"):
                fields = {"FILENAME": str(source),
                          "METAKEY": f"main/binary-{fixture.get('arch', 'amd64')}/Packages",
                          "RELEASE": fixture.get("suite", "jammy")}
                print(re.sub(r"\$\((\w+)\)", lambda m: fields[m[1]], format_string))
            return 0
        if "install" not in args:
            raise ValueError(args)
        if args[-3:] != ["openssh-server", "openssh-client", "jq"] or "--no-remove" not in args:
            raise ValueError("only the requested dependency closure may be installed")
        if "--download-only" in args and "--no-download" not in args:
            if fixture.get("download_fail"):
                return 100
            for source in (repo / "debs").iterdir():
                if fixture.get("missing_dependency") and "libonig5" in source.name:
                    continue
                shutil.copy2(source, archives / source.name)
            return 0
        # Fake resolver uses the same required closure on fresh and offline installs.
        for source in (repo / "debs").iterdir():
            if not (archives / source.name).is_file():
                return 100
        if "--download-only" in args:
            return 0
        if "--no-download" not in args:
            raise ValueError("dpkg installation must never acquire packages")
        policy = root / "system/usr/sbin/policy-rc.d"
        result = subprocess.run(["/bin/sh", str(policy)])
        if result.returncode != 101:
            raise ValueError("postinst services were not suppressed")
        (root / "policy-observed").write_text(policy.read_text())
        if fixture.get("install_fail"):
            return 100
        (root / "installed").touch()
        return 0
    if tool == "ssh":
        print("OpenSSH_9.6p1", file=sys.stderr)
        return 0
    if tool == "jq":
        print("jq-1.7.1")
        return 0
    if tool in ("sshd", "ssh-keygen"):
        raise ValueError("the installer must not launch or configure sshd")
    raise ValueError(f"unexpected fake command {tool} {args}")


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--fake":
    raise SystemExit(fake_command(sys.argv[2], sys.argv[3:]))

import shlex
import tempfile
import unittest


class SSHInstallTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ppu-ssh-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.system = self.root / "system"
        # Freeze fake commands per fixture; concurrent source edits cannot mutate
        # commands in an already-running test job.
        self.driver = self.root / "fake-tool.py"
        self.driver.write_text(Path(__file__).read_text())
        for path in ("etc/apt/sources.list.d", "etc/ssh", "usr/sbin", "usr/lib/apt",
                     "usr/share/keyrings", "var/lib/dpkg"):
            (self.system / path).mkdir(parents=True, exist_ok=True)
        (self.root / "bin").mkdir()
        (self.root / "nas").mkdir()
        (self.root / "tmp").mkdir()
        (self.root / "clock").write_text(str(EPOCH))
        self.options = {}
        self.fixture()
        self.set_os()
        (self.system / "etc/apt/sources.list").write_text("deb https://mirror.example/ubuntu jammy main\n")
        (self.system / "usr/share/keyrings/ubuntu-archive-keyring.gpg").write_text("local distro trust root\n")
        (self.system / "usr/share/keyrings/debian-archive-keyring.gpg").write_text("local distro trust root\n")
        self.config = self.system / "etc/ssh/sshd_config"
        self.config.write_text("# user sshd configuration\nPort 2222\n")
        self.policy = self.system / "usr/sbin/policy-rc.d"
        self.policy.write_text("#!/bin/sh\n# original user policy\nexit 0\n")
        self.policy.chmod(0o751)
        self.original_policy = self.policy.read_bytes()
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "APT_CONFIG", "TMPDIR", "PYTHONPYCACHEPREFIX")}
        self.env.update(SSH_INSTALL_FIXTURE=str(self.root),
                        PATH=str(self.root / "bin") + os.pathsep + os.environ["PATH"],
                        PPU_DEADLINE=str(EPOCH + 120), HOME=str(self.root),
                        PYTHONDONTWRITEBYTECODE="1")
        for tool in ("cp", "mv", "mkdir", "mktemp"):
            self.env["SSH_REAL_" + tool.upper()] = shutil.which(tool)
        for tool in ("date", "sleep", "id", "timeout", "dpkg", "dpkg-query", "dpkg-deb",
                     "gpgv", "apt-get", "ssh", "sshd", "ssh-keygen", "jq", "mkdir", "mktemp", "mv", "cp"):
            self.fake(tool)
        helper = self.system / "usr/lib/apt/apt-helper"
        helper.write_text(wrapper("apt-helper", self.driver))
        helper.chmod(0o755)
        # Replace ONLY production absolute constants, not arbitrary $WORK/etc paths.
        script = SCRIPT.read_text()
        replacements = {
            "/mnt/wl_nas/devops/cache/openssh": str(self.root / "nas"),
            "/usr/lib/apt/apt-helper": str(helper),
            "/usr/share/keyrings": str(self.system / "usr/share/keyrings"),
            "/usr/sbin": str(self.system / "usr/sbin"),
            "/var/lib/dpkg": str(self.system / "var/lib/dpkg"),
            "/etc/os-release": str(self.system / "etc/os-release"),
            "/etc/apt": str(self.system / "etc/apt"),
            "[ -w /etc ]": "[ -w " + str(self.system / "etc") + " ]",
            "/tmp/ppu-ssh-install.": str(self.root / "tmp/ppu-ssh-install."),
        }
        for old, new in replacements.items():
            script = script.replace(old, new)
        self.installer = self.root / "install-ssh.sh"
        self.installer.write_text(script)
        self.issue_repo()

    def fake(self, tool):
        path = self.root / "bin" / tool
        path.write_text(wrapper(tool, self.driver))
        path.chmod(0o755)

    def fixture(self, **values):
        self.options.update(values)
        (self.root / "fixture.json").write_text(json.dumps(self.options))

    def set_os(self, name="ubuntu", version="22.04"):
        (self.system / "etc/os-release").write_text(f'ID={name}\nVERSION_ID="{version}"\n')

    def issue_repo(self, detached=False, suite="jammy", expired=False, future=False, compressed=False):
        repo = self.root / "repo"
        if repo.exists():
            shutil.rmtree(repo)
        (repo / "metadata").mkdir(parents=True)
        (repo / "debs").mkdir()
        arch = self.options.get("arch", "amd64")
        stanzas = []
        for name, version in PACKAGES.items():
            data = json.dumps({"Package": name, "Version": version, "Architecture": arch}).encode()
            filename = f"{name}_{version.replace(':', '%3a')}_{arch}.deb"
            (repo / "debs" / filename).write_bytes(data)
            stanzas.append(f"Package: {name}\nVersion: {version}\nArchitecture: {arch}\n"
                           f"Filename: pool/main/{name}/{filename}\nSHA256: {digest(data)}\nSize: {len(data)}\n")
        packages = ("\n".join(stanzas) + "\n").encode()
        metakey = f"main/binary-{arch}/Packages"
        prefix = f"mirror.example_ubuntu_dists_{self.options.get('suite', 'jammy')}_"
        index = prefix + metakey.replace("/", "_") + (".gz" if compressed else ".lz4")
        if compressed:
            import gzip
            (repo / "metadata" / index).write_bytes(gzip.compress(packages))
        else:
            (repo / "metadata" / index).write_bytes(packages)
        def rfc(seconds):
            return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S UTC")
        release = (f"Origin: Ubuntu\nSuite: {suite}\nCodename: {suite}\n"
                   f"Date: {rfc(EPOCH + 100 if future else EPOCH - 60)}\n"
                   f"Valid-Until: {rfc(EPOCH - 1 if expired else EPOCH + 86400)}\nSHA256:\n"
                   f" {digest(packages)} {len(packages)} {metakey}\n")
        if detached:
            envelope = ("detached signature: " + digest(release.encode())).encode()
            (repo / "metadata" / (prefix + "Release")).write_text(release)
            (repo / "metadata" / (prefix + "Release.gpg")).write_bytes(envelope)
        else:
            envelope = ("signed InRelease: " + digest(release.encode()) + "\n" + release).encode()
            (repo / "metadata" / (prefix + "InRelease")).write_bytes(envelope)
        issued_path = self.root / "issued.json"
        issued = json.loads(issued_path.read_text()) if issued_path.exists() else {}
        issued[digest(envelope)] = release
        issued_path.write_text(json.dumps(issued))

    def run_installer(self, **env):
        return subprocess.run([shutil.which("bash"), "--noprofile", "--norc", str(self.installer)],
                              env={**self.env, **env}, text=True, capture_output=True, timeout=120)

    def calls(self, tool=None):
        path = self.root / "calls.jsonl"
        result = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [call for call in result if tool is None or call["tool"] == tool]

    def generations(self):
        return sorted((self.root / "nas").glob("**/generation-*"))

    def success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.policy.read_bytes(), self.original_policy)
        self.assertEqual(self.policy.stat().st_mode & 0o777, 0o751)
        self.assertEqual(self.config.read_text(), "# user sshd configuration\nPort 2222\n")
        self.assertFalse(list((self.root / "tmp").iterdir()))
        self.assertFalse(list((self.root / "nas").glob("**/.lock")))

    def fresh_job(self, **values):
        (self.root / "installed").unlink(missing_ok=True)
        (self.root / "calls.jsonl").unlink(missing_ok=True)
        self.fixture(**values)

    def download_once(self):
        result = self.run_installer()
        self.success(result)
        self.assertEqual(len(self.generations()), 1)
        self.assertIn("published authenticated", result.stdout)
        return self.generations()[0]

    def assert_offline_rejected(self):
        self.fresh_job(offline=True)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("rejected", result.stderr)
        self.assertFalse((self.root / "installed").exists())
        self.assertEqual(self.policy.read_bytes(), self.original_policy)

    def test_bash_syntax(self):
        result = subprocess.run(["/bin/bash", "-n", str(SCRIPT)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_download_then_authenticated_offline_hit_and_dependencies(self):
        generation = self.download_once()
        self.assertEqual(len(list((generation / "debs").iterdir())), len(PACKAGES))
        key = generation.parent
        self.assertFalse(any("installed" in p.name for p in key.iterdir()))
        self.fresh_job(offline=True)
        result = self.run_installer()
        self.success(result)
        self.assertIn("authenticated offline cache hit", result.stdout)
        self.assertFalse(any("update" in c["args"] for c in self.calls("apt-get")))
        self.assertTrue(self.calls("gpgv"))
        self.assertTrue(self.calls("apt-helper"))
        self.assertEqual(self.generations(), [generation])
        self.assertEqual(self.generations()[0].parent, key)

    def test_detached_release_chain(self):
        self.issue_repo(detached=True)
        self.download_once()
        self.fresh_job(offline=True)
        self.success(self.run_installer())
        self.assertTrue(all("--output" not in c["args"] for c in self.calls("gpgv")))

    def test_auxiliary_vendor_index_cannot_supply_install_trust(self):
        (self.system / "etc/apt/sources.list.d/vendor.list").write_text(
            "deb https://vendor.example/packages jammy main\n")
        metadata = self.root / "repo/metadata"
        prefix = "vendor.example_packages_dists_jammy_"
        (metadata / (prefix + "InRelease")).write_text("signed by a vendor, not the distro")
        (metadata / (prefix + "main_binary-amd64_Packages.lz4")).write_text("vendor index")
        result = self.run_installer()
        self.success(result)
        self.assertIn("ignoring auxiliary index", result.stderr)
        self.assertEqual(len(list((self.generations()[0] / "metadata").glob("*Packages*"))), 1)
        self.fresh_job(offline=True)
        self.success(self.run_installer())
        self.assertFalse(any("update" in c["args"] for c in self.calls("apt-get")))

    def test_release_hash_is_of_decompressed_packages(self):
        self.issue_repo(compressed=True)
        self.download_once()
        self.fresh_job(offline=True)
        self.success(self.run_installer())
        self.assertTrue(any(call["args"][-1].endswith(".gz") for call in self.calls("apt-helper")))

    def test_reuse_compatible_tools_without_sources_or_signature_tools(self):
        (self.root / "installed").touch()
        (self.root / "bin/gpgv").unlink()
        (self.system / "usr/lib/apt/apt-helper").unlink()
        (self.system / "etc/apt/sources.list").unlink()
        result = self.run_installer()
        self.success(result)
        self.assertIn("reusing compatible", result.stdout)
        self.assertFalse(self.calls("apt-get"))

    def test_mismatched_installed_openssh_not_reused(self):
        (self.root / "installed").touch()
        self.fixture(installed_versions={"openssh-client": "1:8.9p1-1"}, download_fail=True)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.calls("apt-get"))

    def test_corrupt_deb_and_self_authored_checksum_rejected(self):
        generation = self.download_once()
        deb = next((generation / "debs").glob("*.deb"))
        data = json.loads(deb.read_text())
        data["extra"] = "attacker content"
        deb.write_text(json.dumps(data))
        (generation / "SHA256SUMS").write_text(digest(deb.read_bytes()) + " " + deb.name + "\n")
        self.assert_offline_rejected()

    def test_deb_version_and_architecture_must_match_signed_stanza(self):
        generation = self.download_once()
        deb = next((generation / "debs").glob("*.deb"))
        original = deb.read_text()
        for field, value in (("Version", "1:0.1-1"), ("Architecture", "arm64")):
            with self.subTest(field=field):
                data = json.loads(original)
                data[field] = value
                deb.write_text(json.dumps(data))
                self.assert_offline_rejected()

    def test_incomplete_cached_dependency_closure_rejected_before_dpkg(self):
        generation = self.download_once()
        dependency = next((generation / "debs").glob("libonig5_*.deb"))
        dependency.unlink()
        manifest = generation / "MANIFEST"
        manifest.write_text("\n".join(line for line in manifest.read_text().splitlines()
                                      if dependency.name not in line) + "\n")
        (self.root / "policy-observed").unlink()
        self.assert_offline_rejected()
        self.assertFalse((self.root / "policy-observed").exists())

    def test_invalid_inrelease_and_nas_keyring_rejected(self):
        generation = self.download_once()
        signature = next((generation / "metadata").glob("*InRelease"))
        signature.write_text(signature.read_text() + "tampered")
        (generation / "distro-keyring.gpg").write_text("attacker keyring")
        self.assert_offline_rejected()

    def test_modified_packages_even_with_matching_nas_checksums_rejected(self):
        generation = self.download_once()
        index = next((generation / "metadata").glob("*Packages*"))
        index.write_text(index.read_text().replace("libjq1", "eviljq"))
        (generation / "SHA256SUMS").write_text(digest(index.read_bytes()))
        self.assert_offline_rejected()

    def test_invalid_detached_signature_rejected(self):
        self.issue_repo(detached=True)
        generation = self.download_once()
        next((generation / "metadata").glob("*Release.gpg")).write_text("bad signature")
        self.assert_offline_rejected()

    def test_partial_generation_rejected(self):
        generation = self.download_once()
        (generation / "COMPLETE").unlink()
        self.assert_offline_rejected()

    def test_manifest_is_data_and_rejects_path_escape(self):
        generation = self.download_once()
        marker = self.root / "executed"
        (generation / "MANIFEST").write_text(f"deb|../../$(touch {marker})\n")
        self.assert_offline_rejected()
        self.assertFalse(marker.exists())

    def test_duplicate_manifest_index_ids_rejected(self):
        generation = self.download_once()
        manifest = generation / "MANIFEST"
        index = next(line for line in manifest.read_text().splitlines() if line.startswith("index|"))
        manifest.write_text(manifest.read_text() + index + "\n")
        self.assert_offline_rejected()

    def test_rejected_cache_online_refilled_without_overwriting_generation(self):
        generation = self.download_once()
        next((generation / "metadata").glob("*InRelease")).write_text("bad signature")
        self.fresh_job()
        result = self.run_installer()
        self.success(result)
        self.assertIn("rejected", result.stderr)
        self.assertIn("published authenticated", result.stdout)
        self.assertEqual(len(self.generations()), 2)
        self.assertEqual(next((generation / "metadata").glob("*InRelease")).read_text(), "bad signature")

    def test_manifest_symlink_rejected(self):
        generation = self.download_once()
        manifest = generation / "MANIFEST"
        target = self.root / "external-manifest"
        target.write_bytes(manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(target)
        self.assert_offline_rejected()

    def test_local_snapshot_resists_nas_toctou(self):
        self.download_once()
        self.fresh_job(offline=True, race_after_copy=True)
        result = self.run_installer()
        self.success(result)
        self.assertIn("offline cache hit", result.stdout)

    def test_os_version_isolation(self):
        self.download_once()
        self.set_os(version="24.04")
        self.fresh_job(offline=True)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls("gpgv"))

    def test_architecture_isolation(self):
        self.download_once()
        self.fresh_job(offline=True, arch="arm64")
        self.issue_repo()
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertFalse(self.calls("gpgv"))

    def test_base_package_fingerprint_isolation(self):
        self.download_once()
        self.fresh_job(offline=True, base="changed-base")
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertFalse(self.calls("gpgv"))

    def test_source_and_local_keyring_fingerprint_isolation(self):
        self.download_once()
        (self.system / "etc/apt/sources.list").write_text("deb https://another-mirror/ubuntu jammy main\n")
        self.fresh_job(offline=True)
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertFalse(self.calls("gpgv"))
        (self.system / "etc/apt/sources.list").write_text("deb https://mirror.example/ubuntu jammy main\n")
        (self.system / "usr/share/keyrings/ubuntu-archive-keyring.gpg").write_text("rotated local key\n")
        self.fresh_job(offline=True)
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertFalse(self.calls("gpgv"))

    def test_cache_cannot_redirect_to_other_signed_suite(self):
        generation = self.download_once()
        self.issue_repo(suite="noble")
        shutil.rmtree(generation / "metadata")
        shutil.copytree(self.root / "repo/metadata", generation / "metadata")
        self.assert_offline_rejected()

    def test_expired_or_future_signed_metadata_rejected(self):
        for options in ({"expired": True}, {"future": True}):
            with self.subTest(options=options):
                self.issue_repo(**options)
                result = self.run_installer()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("verification", result.stderr)
                self.assertFalse((self.root / "installed").exists())

    def test_root_and_unsupported_os_boundaries(self):
        self.fixture(uid=1000)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("root", result.stderr)
        self.fixture(uid=0)
        self.set_os("alpine", "3.20")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported OS", result.stderr)
        self.assertFalse(self.calls("apt-get"))

    def test_readonly_root_and_missing_signature_tools_fail_clearly(self):
        self.fixture(root_readonly=True)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("root filesystem is not writable", result.stderr)
        self.assertFalse(self.calls("apt-get"))
        self.fixture(root_readonly=False)
        # Override with an executable failure boundary, independent of host tools.
        (self.root / "bin/gpgv").write_text("#!/bin/sh\nexit 1\n")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verification", result.stderr)
        self.assertFalse((self.root / "installed").exists())
        (self.system / "usr/lib/apt/apt-helper").unlink()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("apt-helper cat-file support", result.stderr)

    def test_debian_download(self):
        self.set_os("debian", "12")
        self.download_once()
        self.assertIn("/debian/12/amd64/", str(self.generations()[0]))

    def test_insecure_source_options_fail_closed(self):
        for source in ("deb [trusted=yes] https://mirror.example/ubuntu jammy main\n",
                       "deb [allow-insecure=yes] https://mirror.example/ubuntu jammy main\n",
                       "Types: deb\nURIs: https://mirror.example/ubuntu\nTrusted:\n yes\n",
                       "deb [allowUnauthenticated=true] https://mirror.example/ubuntu jammy main\n"):
            with self.subTest(source=source):
                (self.system / "etc/apt/sources.list").write_text(source)
                result = self.run_installer()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("insecure APT", result.stderr)
                self.assertFalse(self.calls("apt-get"))

    def test_download_fail_and_missing_dependency_never_install_or_publish(self):
        for option in ("update_fail", "download_fail", "missing_dependency"):
            with self.subTest(option=option):
                self.fixture(update_fail=False, download_fail=False, missing_dependency=False)
                self.fixture(**{option: True})
                result = self.run_installer()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "installed").exists())
                self.assertFalse(self.generations())
                self.assertEqual(self.policy.read_bytes(), self.original_policy)

    def test_deadline_validation_and_bounded_apt_timeout(self):
        for deadline in (str(EPOCH), "not-an-epoch", ""):
            result = self.run_installer(PPU_DEADLINE=deadline)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PPU_DEADLINE", result.stderr)
        self.fixture(timeout_tool="apt-get")
        result = self.run_installer(PPU_DEADLINE=str(EPOCH + 3))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "installed").exists())
        timeouts = self.calls("timeout")
        self.assertTrue(any("3s" in call["args"] and "apt-get" in call["args"] for call in timeouts))

    def test_busy_lock_is_bounded_without_deleting_other_lock(self):
        generation = self.download_once()
        self.fresh_job(offline=True, lock_busy=True)
        (generation.parent / ".lock").mkdir()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cache lock busy", result.stderr)
        self.assertEqual(int((self.root / "clock").read_text()), EPOCH + 15)
        self.assertTrue((generation.parent / ".lock").exists())

    def test_deadline_while_waiting_for_lock(self):
        self.fixture(lock_busy=True)
        result = self.run_installer(PPU_DEADLINE=str(EPOCH + 2))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deadline", result.stderr)
        self.assertFalse(self.calls("apt-get"))

    def test_readonly_nas_and_backfill_failure_are_warnings(self):
        self.fixture(cache_readonly=True)
        result = self.run_installer()
        self.success(result)
        self.assertIn("not writable", result.stderr)
        self.assertFalse(self.generations())
        self.fresh_job(cache_readonly=False, backfill_fail=True)
        result = self.run_installer()
        self.success(result)
        self.assertIn("backfill failed", result.stderr)
        self.assertFalse(self.generations())

    def test_readonly_nas_still_allows_authenticated_hit(self):
        self.download_once()
        self.fresh_job(cache_readonly=True, offline=True)
        result = self.run_installer()
        self.success(result)
        self.assertIn("offline cache hit", result.stdout)
        self.assertFalse(any("update" in c["args"] for c in self.calls("apt-get")))

    def test_policy_restored_on_install_failure(self):
        self.fixture(install_fail=True)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.policy.read_bytes(), self.original_policy)
        self.assertEqual(self.policy.stat().st_mode & 0o777, 0o751)
        self.assertTrue((self.root / "policy-observed").exists())
        self.assertFalse(self.generations())
        self.assertEqual(self.config.read_text(), "# user sshd configuration\nPort 2222\n")

    def test_policy_symlink_preserved_and_absent_policy_removed(self):
        target = self.root / "user-policy"
        target.write_bytes(self.original_policy)
        self.policy.unlink()
        self.policy.symlink_to(target)
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertTrue(self.policy.is_symlink())
        self.assertEqual(os.readlink(self.policy), str(target))
        self.policy.unlink()
        self.fresh_job(offline=True)
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertFalse(self.policy.exists())

    def test_missing_local_keyring_and_unsupported_helper_fail_clearly(self):
        (self.system / "usr/share/keyrings/ubuntu-archive-keyring.gpg").unlink()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("keyring unavailable", result.stderr)
        (self.system / "usr/share/keyrings/ubuntu-archive-keyring.gpg").write_text("local distro trust root\n")
        self.fixture(helper_unsupported=True)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verification", result.stderr)
        self.assertFalse((self.root / "installed").exists())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fake":
        raise SystemExit(fake_command(sys.argv[2], sys.argv[3:]))
    unittest.main()
