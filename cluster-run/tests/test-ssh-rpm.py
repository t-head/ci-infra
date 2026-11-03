#!/usr/bin/env python3
"""Hermetic signature-oracle fixtures; real RPM signatures and SSH are verified in CI."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parent.parent / "scripts/install-ssh-rpm.sh"
EPOCH = 1900000000
KEYS = {
    "RPM-GPG-KEY-CentOS-7": (
        "https://www.centos.org/keys/RPM-GPG-KEY-CentOS-7",
        "8b48b04b336bd725b9e611c441c65456a4168083c4febc28e88828d8ec14827f"),
    "RPM-GPG-KEY-CentOS-Official": (
        "https://www.centos.org/keys/RPM-GPG-KEY-CentOS-Official",
        "146059788b214d7ba0dd70c1cf21111e594c6cfde201da8a9a88fe7101be8a78"),
    "RPM-GPG-KEY-EPEL-7": (
        "https://mirrors.aliyun.com/epel/RPM-GPG-KEY-EPEL-7",
        "028b9accc59bab1d21f2f3f544df5469910581e728a64fd8c411a725a82300c2"),
    "RPM-GPG-KEY-ALIYUN": (
        "https://mirrors.aliyun.com/alinux/RPM-GPG-KEY-ALIYUN",
        "0809a3c485ad9323901cbd01dc9dde09dc10da21dd57272dd8b0d124d1ee6395"),
    "RPM-GPG-KEY-ALINUX-3": (
        "https://mirrors.aliyun.com/alinux/3/RPM-GPG-KEY-ALINUX-3",
        "b8f79e10cf214927dbc88787e92069e5f032c292f8c3d93c4b9c69026ff90eab"),
}
VERSIONS = {"openssh-server": "7.4p1", "openssh-clients": "7.4p1", "jq": "1.5",
            "openssh": "7.4p1", "oniguruma": "6.8.2"}
REQUIRES = {"openssh-server": ["openssh = 7.4p1-1"],
            "openssh-clients": ["openssh = 7.4p1-1"],
            "jq": ["libonig.so.5()(64bit)"], "openssh": [], "oniguruma": []}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def key_bytes(name):
    return ("test-only pinned public key: " + name + "\n").encode()


def fake_command(tool, args):
    root = Path(os.environ["SSH_RPM_FIXTURE"])
    fixture = json.loads((root / "fixture.json").read_text())
    record = {"tool": tool, "args": args, "bounded": os.environ.get("SSH_RPM_TIMEOUT")}
    if tool in ("yum", "dnf"):
        config_path = Path(args[args.index("-c") + 1])
        config = config_path.read_text()
        fields = dict(re.findall(r"^([a-z_]+)=(.*)$", config, re.MULTILINE))
        repos = (Path(fields["reposdir"]) / "aliyun.repo").read_text()
        record.update(config=config, repos=repos)
    with (root / "calls.jsonl").open("a") as log:
        log.write(json.dumps(record) + "\n")
    if tool == "date":
        assert args == ["+%s"], args
        print((root / "clock").read_text())
        return 0
    if tool == "sleep":
        (root / "clock").write_text(str(int((root / "clock").read_text()) + int(args[0])))
        return 0
    if tool == "id":
        assert args == ["-u"], args
        print(fixture.get("uid", 0))
        return 0
    if tool == "timeout":
        index = next(i for i, arg in enumerate(args) if re.fullmatch(r"\d+s", arg))
        command, duration = args[index + 1:], int(args[index][:-1])
        if fixture.get("timeout_tool") == Path(command[0]).name:
            (root / "clock").write_text(str(int((root / "clock").read_text()) + duration))
            return 124
        return subprocess.run(command, env={**os.environ, "SSH_RPM_TIMEOUT": str(duration)}).returncode
    if tool in ("mkdir", "mktemp", "cp", "mv"):
        nas = str(root / "nas")
        if tool == "mkdir":
            if fixture.get("cache_readonly") and any(arg.startswith(nas) for arg in args):
                return 1
            if fixture.get("lock_busy") and args[-1].endswith("/.lock"):
                return 1
        if tool == "mktemp":
            if fixture.get("root_readonly") and any(".ppu-ssh-write." in a for a in args):
                return 1
            if fixture.get("backfill_fail") and any(".publish." in a for a in args):
                return 1
        if tool == "mv" and any("T" in a for a in args if a.startswith("-")):
            # macOS mv lacks GNU -T; model Linux's replace-not-follow semantics.
            try:
                os.replace(args[-2], args[-1])
                return 0
            except OSError:
                return 1
        result = subprocess.run([os.environ["SSH_RPM_REAL_" + tool.upper()], *args]).returncode
        if tool == "cp" and fixture.get("race_after_copy") and args[-1].endswith("/snapshot"):
            for package in (Path(args[-2]) / "rpms").glob("*.rpm"):
                package.write_bytes(b"NAS changed after local copy")
        return result
    if tool == "curl":
        assert os.environ.get("SSH_RPM_TIMEOUT"), "key bootstrap must be bounded"
        assert "--proto" in args and args[args.index("--proto") + 1] == "=https"
        assert not any(a in args for a in ("-k", "--insecure", "--location", "-L"))
        names = [name for name, (url, _) in KEYS.items() if args[-1] == url]
        assert len(names) == 1, "no alternate/manifest-selected key URLs"
        if fixture.get("offline"):
            return 7
        content = b"wrong vendor key" if fixture.get("bad_key") else key_bytes(names[0])
        Path(args[args.index("--output") + 1]).write_bytes(content)
        return 0
    if tool == "rpmkeys":
        assert os.environ.get("SSH_RPM_TIMEOUT"), "RPM key operations must be bounded"
        if "--import" in args:
            path = Path(args[-1])
            assert "/nas/" not in str(path), "import only local fixed-hash snapshots"
            assert path.name in KEYS and path.read_bytes() == key_bytes(path.name)
            if "--dbpath" in args:
                db = Path(args[args.index("--dbpath") + 1])
                assert db.parent.name.startswith("ppu-ssh-rpm.")
                (db / path.name).write_bytes(path.read_bytes())
            else:
                (root / "system-imports").mkdir(exist_ok=True)
                (root / "system-imports" / path.name).write_bytes(path.read_bytes())
            return 0
        assert "--checksig" in args and "--verbose" in args and "--dbpath" in args
        db = Path(args[args.index("--dbpath") + 1])
        assert db.parent.name.startswith("ppu-ssh-rpm.") and "/nas/" not in str(db)
        data = Path(args[-1]).read_bytes()
        issued = json.loads((root / "issued.json").read_text())
        name = issued.get(digest(data))
        mode = fixture.get("signature_mode")
        if mode == "nokey_zero":
            print("Header V4 RSA/SHA256 Signature, key ID deadbeef: NOKEY")
            print("Payload SHA256 digest: OK")
            return 0
        if mode == "digest_only":
            print("Header SHA256 digest: OK\nPayload SHA256 digest: OK")
            return 0
        if mode == "notok_zero":
            print("Header V4 RSA/SHA256 Signature, key ID deadbeef: NOT OK")
            return 0
        if name is None or not (db / name).is_file():
            print("Header V4 RSA/SHA256 Signature, key ID deadbeef: NOT OK")
            return 1
        print("Header V4 RSA/SHA256 Signature, key ID aabbccdd: OK")
        print("Header SHA256 digest: OK\nPayload SHA256 digest: OK")
        return 0
    if tool == "rpm":
        assert os.environ.get("SSH_RPM_TIMEOUT"), "RPM queries/transactions must be bounded"
        if args[:1] == ["--eval"]:
            if args[1] == "%{_arch}":
                print(fixture.get("arch", "x86_64"))
            elif args[1] == "%{_dbpath}":
                print(root / "system/var/lib/rpm")
            else:
                raise AssertionError(args)
            return 0
        if "--initdb" in args:
            db = Path(args[args.index("--dbpath") + 1])
            assert db.parent.name.startswith("ppu-ssh-rpm.")
            return 0
        if "-qa" in args:
            print("base-system|0|" + fixture.get("base", "1") + "|1|" + fixture.get("arch", "x86_64"))
            for key in (root / "system-imports").glob("*"):
                print("gpg-pubkey|0|" + key.name + "|1|noarch")
            if (root / "installed").exists():
                for name, version in VERSIONS.items():
                    print(f"{name}|0|{version}|1|{fixture.get('arch', 'x86_64')}")
            return 0
        if "-q" in args:
            if not (root / "installed").exists() or args[-1] not in VERSIONS:
                return 1
            name = args[-1]
            version = fixture.get("installed_versions", {}).get(name, VERSIONS[name])
            fmt = args[args.index("--qf") + 1]
            if fmt == "%{VERSION}\\n":
                print(version)
            else:
                assert fmt == "%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\\n", fmt
                print(f"0|{version}|1|{fixture.get('arch', 'x86_64')}")
            return 0
        if "-qp" in args:
            package = json.loads(Path(args[-1]).read_text())
            if "--qf" in args:
                print(f"{package['name']}|0|{package['version']}|1|{package['arch']}")
            elif "--provides" in args:
                print("\n".join(package["provides"]))
            elif "--requires" in args:
                print("\n".join(package["requires"]))
            elif "--list" in args:
                print("\n".join(package.get("files", [])))
            else:
                raise AssertionError(args)
            return 0
        if "-U" in args:
            assert all(a in args for a in ("--test", "--noscripts", "--notriggers"))
            assert not any(a in args for a in ("--oldpackage", "--nodeps", "--nosignature"))
            packages = [json.loads(Path(a).read_text()) for a in args if a.endswith(".rpm")]
            names = {p["name"] for p in packages}
            needed = set(VERSIONS) if not (root / "installed").exists() else set()
            if not needed.issubset(names) or fixture.get("transaction_fail"):
                return 1
            return 0
        raise AssertionError(args)
    if tool in ("yum", "dnf"):
        assert os.environ.get("SSH_RPM_TIMEOUT"), "yum/dnf must be bounded"
        assert "--noplugins" in args and "-y" in args
        for setting in ("gpgcheck=1", "localpkg_gpgcheck=1", "sslverify=1", "plugins=0",
                        "install_weak_deps=False", "obsoletes=0"):
            assert setting in config, setting
        assert all(str(Path(fields[f])).startswith(str(root / "tmp"))
                   for f in ("reposdir", "cachedir", "persistdir", "varsdir", "pluginpath", "pluginconfpath"))
        assert "repo_gpgcheck=0" in repos and "gpgcheck=1" in repos and "sslverify=1" in repos
        assert all(url.startswith("https://mirrors.aliyun.com/")
                   for url in re.findall(r"^baseurl=(.*)$", repos, re.MULTILINE))
        for path in re.findall(r"^gpgkey=file://(.*)$", repos, re.MULTILINE):
            key = Path(path)
            assert key.name in KEYS and key.read_bytes() == key_bytes(key.name)
        assert "--nogpgcheck" not in args and not any("gpgcheck=0" in a for a in args)
        assert "upgrade" not in args and "downgrade" not in args
        if "install" in args:
            assert args[-3:] == ["openssh-server", "openssh-clients", "jq"]
            assert "--setopt=tsflags=test" in args and "--setopt=keepcache=1" in args
            if fixture.get("offline") or fixture.get("download_fail"):
                return 1
            layout = "x86_64/7/actual-repo-id/packages" if fixture.get("yum_cache_layout") else "actual-repo-id/packages"
            cache = Path(fields["cachedir"]) / layout
            cache.mkdir(parents=True, exist_ok=True)
            for source in (root / "repo").glob("*.rpm"):
                if fixture.get("missing_dependency") and source.name.startswith("oniguruma-"):
                    continue
                shutil.copy2(source, cache / source.name)
            return 0
        assert "localinstall" in args and "--cacheonly" in args and "--disablerepo=*" in args
        paths = [Path(a) for a in args if a.endswith(".rpm")]
        assert paths and all("/verified/" in str(p) and "/nas/" not in str(p) for p in paths)
        if "--setopt=tsflags=test" in args:
            return 1 if fixture.get("preflight_fail") else 0
        assert "--setopt=tsflags=noscripts,notriggers" in args, "no RPM script/service/key side effects"
        if fixture.get("install_fail"):
            return 1
        (root / "installed").touch()
        return 0
    if tool == "getent":
        assert args in (["passwd", "sshd"], ["group", "sshd"])
        return 0 if (root / ("sshd-" + args[0])).exists() else 2
    if tool == "useradd":
        assert args[-1] == "sshd" and "-r" in args and "-M" in args
        assert args[args.index("-d") + 1] == str(root / "system/var/empty/sshd")
        assert args[args.index("-s") + 1] == "/sbin/nologin"
        assert (root / "system/var/empty/sshd").is_dir()
        (root / "sshd-passwd").touch()
        return 0
    if tool == "ldconfig":
        assert not args and (root / "installed").exists()
        return 0
    if tool == "ssh":
        assert args == ["-V"]
        print("OpenSSH_7.4p1", file=sys.stderr)
        return 0
    if tool == "jq":
        assert args == ["--version"]
        print("jq-1.5")
        return 0
    if tool in ("sshd", "ssh-keygen", "systemctl", "service", "sudo"):
        raise AssertionError("installer must not invoke " + tool)
    raise AssertionError((tool, args))


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--fake":
    raise SystemExit(fake_command(sys.argv[2], sys.argv[3:]))

import shlex
import tempfile
import unittest


class SSHRPMTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ppu-ssh-rpm-test-")
        self.addCleanup(temp.cleanup)
        # macOS /var is a symlink; production explicitly rejects cache ancestors.
        self.root = Path(temp.name).resolve()
        self.system = self.root / "system"
        for path in ("etc/pki/rpm-gpg", "etc/yum.repos.d", "etc/ssh", "usr/sbin", "var/lib/rpm",
                     "etc/systemd/system/multi-user.target.wants", "var"):
            (self.system / path).mkdir(parents=True, exist_ok=True)
        for name in ("bin", "nas", "tmp", "repo"):
            (self.root / name).mkdir()
        self.driver = self.root / "fake-tool.py"
        self.driver.write_text(Path(__file__).read_text())
        (self.root / "clock").write_text(str(EPOCH))
        self.options = {}
        self.fixture()
        self.set_os()
        (self.system / "etc/yum.conf").write_text("[main]\nplugins=1\ngpgcheck=0\n")
        (self.system / "etc/yum.repos.d/user.repo").write_text(
            "[user]\nbaseurl=https://untrusted.example/\ngpgcheck=0\n")
        (self.system / "etc/ssh/sshd_config").write_text("# user config\nPort 2222\n")
        (self.system / "etc/systemd/system/multi-user.target.wants/sshd.service").write_text("untouched unit\n")
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "TMPDIR", "PYTHONPYCACHEPREFIX")}
        self.env.update(SSH_RPM_FIXTURE=str(self.root), PPU_DEADLINE=str(EPOCH + 120),
                        PATH=str(self.root / "bin") + os.pathsep + os.environ["PATH"],
                        HOME=str(self.root), PYTHONDONTWRITEBYTECODE="1")
        for tool in ("cp", "mv", "mkdir", "mktemp"):
            self.env["SSH_RPM_REAL_" + tool.upper()] = shutil.which(tool)
        for tool in ("date", "sleep", "id", "timeout", "rpm", "rpmkeys", "yum", "dnf", "curl",
                     "ssh", "sshd", "ssh-keygen", "jq", "systemctl", "service", "sudo", "cp", "mv", "mkdir", "mktemp",
                     "getent", "useradd", "ldconfig"):
            self.fake(tool)
        # Fixture rewriting is restricted to absolute paths and pinned digests.
        # Production has no test overrides or user-selected trust/cache/repo URLs.
        script = SCRIPT.read_text()
        for name, (url, pin) in KEYS.items():
            self.assertIn(url, script)
            self.assertIn(pin, script)
            script = script.replace(pin, digest(key_bytes(name)))
        replacements = {
            "/mnt/wl_nas/devops/cache/openssh/rpm": str(self.root / "nas"),
            "/etc/os-release": str(self.system / "etc/os-release"),
            "/etc/pki/rpm-gpg": str(self.system / "etc/pki/rpm-gpg"),
            "/usr/sbin": str(self.system / "usr/sbin"),
            "[ -w /etc ]": "[ -w " + str(self.system / "etc") + " ]",
            "/tmp/ppu-ssh-rpm.": str(self.root / "tmp/ppu-ssh-rpm."),
            "/var/empty": str(self.system / "var/empty"),
            "/sbin/ldconfig": str(self.root / "bin/ldconfig"),
        }
        for old, new in replacements.items():
            script = script.replace(old, new)
        self.installer = self.root / "install-ssh-rpm.sh"
        self.installer.write_text(script)
        self.issue_repo()

    def fake(self, tool):
        path = self.root / "bin" / tool
        path.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                        + shlex.quote(str(self.driver)) + " --fake " + tool + ' "$@"\n')
        path.chmod(0o755)

    def fixture(self, **values):
        self.options.update(values)
        (self.root / "fixture.json").write_text(json.dumps(self.options))

    def set_os(self, name="centos", version="7", stream=False):
        label = "CentOS Stream" if stream else "CentOS Linux"
        if name == "alinux":
            label = "Alibaba Cloud Linux"
        (self.system / "etc/os-release").write_text(f'ID={name}\nVERSION_ID="{version}"\nNAME="{label}"\n')
        self.os_id, self.os_version = name, version

    def vendor_key(self, package):
        if self.os_id == "alinux":
            return "RPM-GPG-KEY-ALIYUN" if self.os_version.startswith("2") else "RPM-GPG-KEY-ALINUX-3"
        if self.os_version.startswith("7"):
            return "RPM-GPG-KEY-EPEL-7" if package in ("jq", "oniguruma") else "RPM-GPG-KEY-CentOS-7"
        return "RPM-GPG-KEY-CentOS-Official"

    def issue_package(self, name, version="1.0", provides=None, requires=None, destination=None, files=None):
        package = {"name": name, "version": version, "arch": self.options.get("arch", "x86_64"),
                   "provides": provides or [name + " = " + version + "-1"], "requires": requires or [],
                   "files": files or []}
        data = json.dumps(package, sort_keys=True).encode()
        path = (destination or self.root / "repo") / f"{name}-{version}-1.{package['arch']}.rpm"
        path.write_bytes(data)
        issued_path = self.root / "issued.json"
        issued = json.loads(issued_path.read_text()) if issued_path.exists() else {}
        issued[digest(data)] = self.vendor_key(name)
        issued_path.write_text(json.dumps(issued))
        return path

    def issue_repo(self):
        for path in (self.root / "repo").iterdir():
            path.unlink()
        for name, version in VERSIONS.items():
            provides = [name + " = " + version + "-1"]
            if name == "oniguruma":
                provides += ["libonig.so.5()(64bit)"]
            self.issue_package(name, version, provides, REQUIRES[name])

    def calls(self, tool=None):
        path = self.root / "calls.jsonl"
        calls = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [c for c in calls if tool is None or c["tool"] == tool]

    def generations(self):
        return sorted((self.root / "nas").glob("**/generation-*"))

    def clean(self):
        self.assertFalse(list((self.root / "tmp").iterdir()), "per-job temp must be removed")
        for pattern in ("**/.lock", "**/.publish.*", "**/.key.*"):
            self.assertFalse(list((self.root / "nas").glob(pattern)), "owned cache staging/locks must be removed")
        self.assertFalse([c for c in self.calls() if c["tool"] in ("sshd", "ssh-keygen", "systemctl", "service", "sudo")])

    def run_installer(self, arguments=(), **env):
        protected = [self.system / "etc/yum.conf", self.system / "etc/yum.repos.d/user.repo",
                     self.system / "etc/ssh/sshd_config",
                     self.system / "etc/systemd/system/multi-user.target.wants/sshd.service"]
        before = {p: (p.read_bytes(), p.stat().st_mode) for p in protected}
        result = subprocess.run([shutil.which("bash"), "--noprofile", "--norc", str(self.installer), *arguments],
                                env={**self.env, **env}, text=True, capture_output=True, timeout=120)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mode) for p in protected},
                         "global repos, SSH config and service enablement must remain untouched")
        self.clean()
        return result

    def success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def rejected(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / "installed").exists())
        self.assertFalse([c for c in self.calls() if c["tool"] in ("yum", "dnf")
                          and "--setopt=tsflags=noscripts,notriggers" in c["args"]])

    def fresh_job(self, **values):
        (self.root / "installed").unlink(missing_ok=True)
        (self.root / "calls.jsonl").unlink(missing_ok=True)
        if (self.root / "system-imports").exists():
            shutil.rmtree(self.root / "system-imports")
        self.fixture(**values)

    def download_once(self):
        result = self.run_installer()
        self.success(result)
        self.assertIn("published authenticated", result.stdout)
        self.assertEqual(len(self.generations()), 1)
        return self.generations()[0]

    def test_centos7_download_and_fresh_offline(self):
        # Use core yum (not dnf or a downloadonly plugin) on CentOS 7.
        (self.root / "bin/dnf").unlink()
        generation = self.download_once()
        calls = self.calls("yum")
        self.assertEqual(len(calls), 3)
        for repo in ("os", "updates", "extras"):
            self.assertIn(f"https://mirrors.aliyun.com/centos-vault/7.9.2009/{repo}/x86_64", calls[0]["repos"])
        self.assertIn("https://mirrors.aliyun.com/epel-archive/7/x86_64", calls[0]["repos"])
        self.assertEqual(len(list((generation / "rpms").glob("*.rpm"))), len(VERSIONS))
        self.assertEqual(len([c for c in self.calls("rpm") if "-qa" in c["args"]]), 1)
        self.assertEqual(len(self.calls("curl")), 2)
        self.fresh_job(offline=True)
        result = self.run_installer()
        self.success(result)
        self.assertIn("authenticated offline RPM cache hit", result.stdout)
        self.assertFalse(self.calls("curl"), "fresh offline image must reuse pinned public key cache")
        self.assertEqual(len(self.calls("yum")), 2)
        self.assertTrue(all("--cacheonly" in c["args"] for c in self.calls("yum")))
        self.assertEqual(self.generations(), [generation])
        self.assertEqual(len([c for c in self.calls("rpm") if "-qa" in c["args"]]), 1)

    def test_signed_file_dependencies_work_without_find(self):
        self.fixture(yum_cache_layout=True)
        tool = self.root / "bin/find"
        tool.write_text("#!/bin/sh\nexit 99\n")
        tool.chmod(0o755)
        self.issue_package("openssh-clients", VERSIONS["openssh-clients"],
                           requires=["openssh = 7.4p1-1", "fipscheck-lib(x86-64) >= 1.3.0"])
        self.issue_package("fipscheck-lib", provides=["fipscheck-lib(x86-64) = 1.4.1-1"],
                           requires=["/usr/bin/fipscheck"])
        self.issue_package("fipscheck", files=["/usr/bin/fipscheck"])
        generation = self.download_once()
        self.assertTrue(list((generation / "rpms").glob("fipscheck-*.rpm")))
        self.fresh_job(offline=True)
        result = self.run_installer()
        self.success(result)
        self.assertIn("authenticated offline RPM cache hit", result.stdout)
        self.assertTrue([c for c in self.calls("rpm") if "--list" in c["args"]])

    def test_sshd_preparation_preserves_existing_account(self):
        self.download_once()
        self.assertEqual(len(self.calls("useradd")), 1)
        self.assertEqual(len(self.calls("ldconfig")), 1)
        self.assertEqual((self.system / "var/empty/sshd").stat().st_mode & 0o777, 0o755)
        (self.root / "calls.jsonl").unlink()
        self.success(self.run_installer())
        self.assertFalse(self.calls("useradd"))
        self.assertFalse(self.calls("ldconfig"))

    def test_release_routing(self):
        cases = [("centos", "8", False, "x86_64", "centos-vault/8.5.2111/BaseOS/x86_64/os"),
                 ("centos", "8", True, "x86_64", "centos-vault/8-stream/BaseOS/x86_64/os"),
                 ("centos", "9", True, "aarch64", "centos-stream/9-stream/AppStream/aarch64/os"),
                 ("alinux", "2.1903", False, "aarch64", "alinux/2.1903/plus/aarch64"),
                 ("alinux", "3.2104", False, "x86_64", "alinux/3/os/x86_64")]
        for name, version, stream, arch, expected in cases:
            with self.subTest(name=name, version=version, stream=stream, arch=arch):
                self.fresh_job(arch=arch)
                self.set_os(name, version, stream)
                self.issue_repo()
                self.success(self.run_installer())
                self.assertIn("https://mirrors.aliyun.com/" + expected, self.calls("dnf")[0]["repos"])
                self.assertNotIn("epel", self.calls("dnf")[0]["repos"].lower())
                self.assertEqual(len([c for c in self.calls("rpm") if "-qa" in c["args"]]), 1)
        self.assertEqual(len(self.generations()), len(cases))

    def test_bad_keys_and_signature_oracles(self):
        for options in ({"bad_key": True}, {"signature_mode": "nokey_zero"},
                        {"signature_mode": "digest_only"}, {"signature_mode": "notok_zero"}):
            with self.subTest(options=options):
                shutil.rmtree(self.root / "nas")
                (self.root / "nas").mkdir()
                self.fresh_job(**{"bad_key": False, "signature_mode": None, **options})
                result = self.run_installer()
                self.rejected(result)
                self.assertFalse(self.generations())
                if options.get("bad_key"):
                    self.assertIn("fixed SHA256", result.stderr)
                    self.assertFalse(self.calls("rpmkeys"))
                else:
                    self.assertIn("failed pinned signatures", result.stderr)

    def test_signed_unrelated_cache_is_rejected(self):
        generation = self.download_once()
        extra = self.issue_package("unrelated-vendor-package", destination=generation / "rpms")
        with (generation / "MANIFEST").open("a") as manifest:
            manifest.write("rpm|" + extra.name + "\n")
        self.fresh_job(offline=True)
        result = self.run_installer()
        self.rejected(result)
        self.assertIn("rejected", result.stderr)
        self.assertFalse([c for c in self.calls("dnf") if "localinstall" in c["args"]])

    def test_failure_cleanup_no_global_changes(self):
        self.fixture(download_fail=True)
        self.rejected(self.run_installer())
        self.assertFalse(self.generations())
        self.fresh_job(download_fail=False, install_fail=True)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / "installed").exists())
        self.assertFalse(self.generations(), "failed installation must not publish COMPLETE")
        self.assertIn("offline RPM dependency installation failed", result.stderr)

    def test_invalid_os_arch_and_identity_fail_without_network(self):
        for name, version, stream, arch in [("centos", "9", False, "x86_64"),
                                            ("centos", "7", False, "aarch64"),
                                            ("alinux", "4", False, "x86_64"),
                                            ("alinux", "3", False, "ppc64le"),
                                            ("alibaba", "3", False, "x86_64"),
                                            ("centos", "8.4.2105", False, "x86_64")]:
            with self.subTest(name=name, version=version, stream=stream, arch=arch):
                self.fresh_job(arch=arch)
                self.set_os(name, version, stream)
                self.rejected(self.run_installer())
                self.assertFalse(self.calls("curl"))
                self.assertFalse(self.calls("dnf"))
        self.fresh_job(arch="x86_64")
        self.set_os("centos", "8")
        with (self.system / "etc/os-release").open("a") as os_release:
            os_release.write('VERSION="CentOS Stream 8"\n')
        result = self.run_installer()
        self.rejected(result)
        self.assertIn("contradictory", result.stderr)

    def test_os_arch_and_baseline_isolation(self):
        self.set_os("centos", "9", True)
        self.issue_repo()
        old = self.download_once()
        for options in ({"base": "different-baseline"}, {"arch": "aarch64"}):
            with self.subTest(options=options):
                self.fresh_job(**{"offline": True, "base": "1", "arch": "x86_64", **options})
                self.rejected(self.run_installer())
                self.assertNotIn("offline RPM cache hit", "".join(c.get("repos", "") for c in self.calls()))
                self.assertEqual(self.generations(), [old])
        self.fresh_job(offline=True, base="1", arch="x86_64")
        self.set_os("alinux", "3")
        self.rejected(self.run_installer())
        self.assertEqual(self.generations(), [old])

    def test_manifest_escape_symlink_and_tampering(self):
        generation = self.download_once()
        original = (generation / "MANIFEST").read_bytes()
        first = next((generation / "rpms").glob("*.rpm"))
        original_rpm = first.read_bytes()
        scenarios = ["escape", "symlink", "tamper", "unknown", "duplicate", "root_symlink"]
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                (generation / "MANIFEST").write_bytes(original)
                first.unlink(missing_ok=True)
                first.write_bytes(original_rpm)
                if scenario == "escape":
                    (generation / "MANIFEST").write_text("rpm|../../outside.rpm\n")
                elif scenario == "symlink":
                    first.unlink()
                    first.symlink_to(self.root / "repo" / first.name)
                elif scenario == "tamper":
                    first.write_bytes(b"changed RPM; untrusted checksum cannot bless it")
                elif scenario == "unknown":
                    (generation / "payload.sh").write_text("touch SHOULD_NOT_RUN\n")
                elif scenario == "duplicate":
                    with (generation / "MANIFEST").open("ab") as manifest:
                        manifest.write(original.splitlines()[0] + b"\n")
                elif scenario == "root_symlink":
                    os.rename(self.root / "nas/centos", self.root / "outside-cache")
                    (self.root / "nas/centos").symlink_to(self.root / "outside-cache", target_is_directory=True)
                self.fresh_job(offline=True)
                self.rejected(self.run_installer())
                self.assertFalse((self.root / "SHOULD_NOT_RUN").exists())
                if scenario == "unknown":
                    (generation / "payload.sh").unlink()
                elif scenario == "root_symlink":
                    (self.root / "nas/centos").unlink()
                    os.rename(self.root / "outside-cache", self.root / "nas/centos")

    def test_local_pinned_key_preference_and_key_cache_poisoning(self):
        for name in ("RPM-GPG-KEY-CentOS-7", "RPM-GPG-KEY-EPEL-7"):
            (self.system / "etc/pki/rpm-gpg" / name).write_bytes(key_bytes(name))
        generation = self.download_once()
        self.assertFalse(self.calls("curl"))
        for key in (self.system / "etc/pki/rpm-gpg").iterdir():
            key.unlink()
        for key in (self.root / "nas/publickeys").glob("*.asc"):
            key.write_bytes(b"NAS key poison")
        self.fresh_job(offline=True)
        self.rejected(self.run_installer())
        self.assertFalse(self.calls("rpmkeys"), "bad cached keys must not be imported")
        self.assertEqual(self.generations(), [generation])

    def test_cache_readonly_lock_busy_and_publication_failure(self):
        generation = self.download_once()
        self.fresh_job(offline=True, cache_readonly=True)
        self.success(self.run_installer())
        self.assertEqual(self.generations(), [generation])
        for options in ({"cache_readonly": True}, {"lock_busy": True}, {"backfill_fail": True}):
            with self.subTest(options=options):
                shutil.rmtree(self.root / "nas")
                (self.root / "nas").mkdir()
                self.fresh_job(**{"offline": False, "cache_readonly": False, "lock_busy": False,
                                  "backfill_fail": False, **options})
                result = self.run_installer()
                self.success(result)
                self.assertIn("WARNING", result.stderr)
                self.assertFalse(self.generations())

    def test_missing_dependency_timeout_and_preflight_failure(self):
        for options in ({"missing_dependency": True}, {"timeout_tool": "dnf"}, {"preflight_fail": True}):
            with self.subTest(options=options):
                self.fresh_job(**{"missing_dependency": False, "timeout_tool": None, "preflight_fail": False, **options})
                self.rejected(self.run_installer())
                self.assertFalse(self.generations())
                (self.root / "clock").write_text(str(EPOCH))

    def test_snapshot_is_immutable_after_copy(self):
        self.download_once()
        self.fresh_job(offline=True, race_after_copy=True)
        result = self.run_installer()
        self.success(result)
        self.assertIn("authenticated offline RPM cache hit", result.stdout)

    def test_reuse_matching_74_without_key_bootstrap(self):
        (self.root / "installed").touch()
        self.fixture(offline=True)
        result = self.run_installer()
        self.success(result)
        self.assertIn("reusing matching OpenSSH >=7.4", result.stdout)
        self.assertFalse(self.calls("curl"))
        self.assertFalse(self.calls("rpmkeys"))
        self.assertFalse(self.calls("dnf"))
        self.assertFalse([c for c in self.calls("rpm") if "-qa" in c["args"]])
        self.fixture(installed_versions={"openssh-clients": "8.0p1"})
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("reusing matching", result.stdout)

    def test_root_deadline_and_arguments_fail_closed(self):
        self.fixture(uid=1000)
        self.rejected(self.run_installer())
        self.fixture(uid=0)
        self.rejected(self.run_installer(arguments=("user",)))
        for deadline in ("", "invalid", str(EPOCH), "999999999999"):
            with self.subTest(deadline=deadline):
                self.rejected(self.run_installer(PPU_DEADLINE=deadline))
        self.fixture(root_readonly=True)
        self.rejected(self.run_installer())
        self.assertFalse(self.calls("dnf"))


if __name__ == "__main__":
    unittest.main()
