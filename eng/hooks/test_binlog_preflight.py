#!/usr/bin/env python3
"""Exercise shipped hooks with real SDK/NuGet settings, independently of skill evaluations."""

import http.server
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from xml.sax.saxutils import escape
import zipfile

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "dotnet-msbuild"
DOTNET = shutil.which("dotnet")
POWERSHELL = shutil.which("powershell") if os.name == "nt" else None
BASH = shutil.which("bash")
if os.name == "nt":
    git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
    if git_bash.exists():
        BASH = str(git_bash)


class BinlogPreflightTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="binlog hook tests ")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.workspace = self.root / "workspace with spaces"
        self.workspace.mkdir()
        self.plugin = self.root / "plugin with spaces"
        shutil.copytree(PLUGIN / "hooks", self.plugin / "hooks")
        self.feed = self.root / "approved mirror"
        self.feed.mkdir()
        self.configure_feed(str(self.feed))
        self.env = dict(os.environ, DOTNET_CLI_HOME=str(self.root / "dotnet home"),
                        NUGET_PACKAGES=str(self.root / "packages"),
                        NUGET_HTTP_CACHE_PATH=str(self.root / "http cache"),
                        DOTNET_NOLOGO="1", DOTNET_CLI_TELEMETRY_OPTOUT="1")

    def configure_feed(self, source, mapping=""):
        # Inherited from a parent, not a single workspace config scanned by the hook.
        (self.root / "nuget.config").write_text(
            f'<configuration><packageSources><clear/><add key="approved-mirror" '
            f'value="{escape(source, {chr(34): "&quot;"})}" allowInsecureConnections="true"/>'
            f'</packageSources>{mapping}</configuration>', encoding="utf-8")

    def launchers(self):
        if os.name == "nt":
            self.assertIsNotNone(POWERSHELL, "Windows PowerShell is required.")
            yield "copilot", [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                              "-File", str(self.plugin / "hooks" / "session-start.ps1")]
        self.assertIsNotNone(BASH, "The supported hook shell must provide Bash.")
        for host in ("copilot", "claude"):
            yield host, [BASH, str(self.plugin / "hooks" / "session-start.sh"), host]

    def run_hook(self, command, env=None, cwd=None):
        started = time.monotonic()
        result = subprocess.run(
            command, cwd=cwd or self.workspace, env=env or self.env,
            input=json.dumps({"cwd": str(self.workspace), "source": "startup"}, ensure_ascii=False),
            text=True, encoding="utf-8", capture_output=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(time.monotonic() - started, 10, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "", result.stderr)
        return result.stdout.strip()

    def context(self, host, output):
        payload = json.loads(output)
        if host == "claude":
            self.assertEqual(set(payload), {"hookSpecificOutput"})
            specific = payload["hookSpecificOutput"]
            self.assertEqual(specific["hookEventName"], "SessionStart")
            return specific["additionalContext"]
        self.assertEqual(set(payload), {"additionalContext"})
        return payload["additionalContext"]

    def test_structured_outcomes(self):
        self.assertIsNotNone(DOTNET)
        result = subprocess.run(
            [DOTNET, "fsi", "--exec", str(REPO / "eng" / "hooks" / "Test-BinlogPreflight.fsx")],
            cwd=self.workspace, env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("preflight assertions", result.stdout)

    def test_clear_and_inherited_mirror_absence(self):
        for host, command in self.launchers():
            with self.subTest(host=host, command=command[0]):
                message = self.context(host, self.run_hook(command))
                self.assertIn("configured feeds did not provide Microsoft.AITools.BinlogMcp", message)
                for text in ("approved feed/mirror", "organizational policy permits",
                             "binlog-mcp", "source mapping", "not a startup test"):
                    self.assertIn(text, message)
                self.assertNotIn(str(self.workspace), message)
                self.assertNotIn(str(self.feed), message)

    def test_available_exact_package_with_source_mapping_is_silent(self):
        package = self.feed / "Microsoft.AITools.BinlogMcp.3.0.4-preview.nupkg"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Microsoft.AITools.BinlogMcp.nuspec",
                            '<package><metadata><id>Microsoft.AITools.BinlogMcp</id>'
                            '<version>3.0.4-preview</version><authors>Fixture</authors>'
                            '<description>Exact-package fixture</description></metadata></package>')
        # Search ignores this mapping; silence must not be interpreted as healthy dnx startup.
        self.configure_feed(str(self.feed), '<packageSourceMapping><clear/>'
                            '<packageSource key="approved-mirror"><package pattern="Other.*"/>'
                            '</packageSource></packageSourceMapping>')
        for host, command in self.launchers():
            with self.subTest(host=host, command=command[0]):
                self.assertEqual(self.run_hook(command), "")

    def test_utf8_workspace_request(self):
        self.workspace = self.root / "workspace with spaces \u00e9"
        self.workspace.mkdir()
        for host, command in self.launchers():
            with self.subTest(host=host, command=command[0]):
                message = self.context(host, self.run_hook(command))
                self.assertIn("configured feeds did not provide Microsoft.AITools.BinlogMcp", message)

    def test_feed_errors_are_not_absence(self):
        class MissingServiceIndex(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MissingServiceIndex)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.configure_feed(f"http://127.0.0.1:{server.server_port}/v3/index.json")
        try:
            result = subprocess.run(
                [DOTNET, "package", "search", "Microsoft.AITools.BinlogMcp",
                 "--exact-match", "--prerelease", "--format", "json"],
                cwd=self.workspace, env=self.env, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            document = result.stdout[result.stdout.rfind("\n{") + 1:]
            self.assertTrue(json.loads(document)["searchResult"][0]["problems"])
            for host, command in self.launchers():
                with self.subTest(host=host, command=command[0]):
                    message = self.context(host, self.run_hook(command))
                    # SDK/network retries may exhaust the bounded probe before it returns its problems.
                    self.assertRegex(message, "NuGet reported|timed out|budget expired")
                    self.assertIn("could not be verified", message)
                    self.assertNotIn("configured feeds did not provide", message)
        finally:
            server.shutdown()
            server.server_close()

    def test_timeout_is_not_absence(self):
        release = threading.Event()

        class StalledFeed(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                release.wait(15)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StalledFeed)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.configure_feed(f"http://127.0.0.1:{server.server_port}/v3/index.json")
        try:
            for host, command in self.launchers():
                with self.subTest(host=host, command=command[0]):
                    message = self.context(host, self.run_hook(command))
                    self.assertRegex(message, "timed out|budget expired")
                    self.assertIn("could not be verified", message)
                    self.assertNotIn("configured feeds did not provide", message)
        finally:
            release.set()
            server.shutdown()
            server.server_close()

    def test_missing_dotnet(self):
        env = dict(self.env, PATH=str(self.root / "empty path"))
        for host, command in self.launchers():
            with self.subTest(host=host, command=command[0]):
                message = self.context(host, self.run_hook(command, env=env))
                self.assertIn("dotnet was not found", message)
                self.assertIn(".NET 10+", message)

    def test_unsupported_workspace_sdk(self):
        (self.workspace / "global.json").write_text(
            '{"sdk":{"version":"99.0.100","rollForward":"disable"}}', encoding="utf-8")
        for host, command in self.launchers():
            with self.subTest(host=host, command=command[0]):
                message = self.context(host, self.run_hook(command))
                self.assertIn("selected SDK", message)
                self.assertNotIn("configured feeds did not provide", message)

    @unittest.skipUnless(os.name == "nt", "Windows process cleanup")
    def test_windows_timeout_cleanup_race_still_emits_context(self):
        (self.plugin / "hooks" / "binlog-preflight.fsx").write_text(
            "System.Threading.Thread.Sleep(30000)\n", encoding="utf-8")
        launcher = str(self.plugin / "hooks" / "session-start.ps1").replace("'", "''")
        script = r"""
function taskkill.exe {
    param([Parameter(ValueFromRemainingArguments)] [string[]] $Arguments)
    & "$env:SystemRoot\System32\taskkill.exe" @Arguments 2>&1 | Out-Null
    Write-Error 'ERROR: The process was not found.'
    $global:LASTEXITCODE = 1
}
"""
        for host in ("copilot", "claude"):
            with self.subTest(host=host):
                command = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-Command", script + f"& '{launcher}' -HostName {host}"]
                message = self.context(host, self.run_hook(command))
                self.assertIn("8-second diagnostic launcher budget expired", message)
                self.assertNotIn("configured feeds did not provide", message)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell stdin encoding")
    def test_windows_utf8_bom_input_uses_workspace(self):
        launcher = str(self.plugin / "hooks" / "session-start.ps1").replace("'", "''")
        for host in ("copilot", "claude"):
            with self.subTest(host=host):
                command = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                           "[Console]::InputEncoding = [Text.UTF8Encoding]::new($true); "
                           f"& '{launcher}' -HostName {host}"]
                message = self.context(host, self.run_hook(command))
                self.assertIn("configured feeds did not provide Microsoft.AITools.BinlogMcp", message)

    def assert_windows_stream_holder_cleanup(self, force_launcher_timeout):
        hooks = self.plugin / "hooks"
        shutil.copyfile(hooks / "binlog-preflight.fsx", hooks / "core-preflight.fsx")
        provider_pid = self.root / "provider.pid"
        search_pid = self.root / "search.pid"
        provider_path = str(provider_pid).replace("'", "''")
        search_path = str(search_pid).replace("'", "''")
        package_script = f"""
$info = [Diagnostics.ProcessStartInfo]::new()
$info.FileName = '{POWERSHELL.replace("'", "''")}'
$info.Arguments = '-NoProfile -Command "Start-Sleep -Seconds 30"'
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.RedirectStandardInput = $true
$provider = [Diagnostics.Process]::Start($info)
$provider.StandardInput.Close()
[IO.File]::WriteAllText('{provider_path}', $provider.Id.ToString())
[IO.File]::WriteAllText('{search_path}', $PID.ToString())
[Console]::Out.WriteLine('{{"searchResult":[{{"packages":[{{"id":"Microsoft.AITools.BinlogMcp","version":"3.0.4"}}]}}]}}')
exit 0
"""
        # Load the shipped runProbe, but replace only the package-search executable with a fixture.
        (hooks / "binlog-preflight.fsx").write_text(
            '#load "core-preflight.fsx"\nopen System.Diagnostics\nopen System.IO\n'
            'open ``Core-preflight``\n'
            f'let info = ProcessStartInfo({json.dumps(POWERSHELL)})\n'
            'info.ArgumentList.Add("-NoProfile")\ninfo.ArgumentList.Add("-Command")\n'
            f'info.ArgumentList.Add({json.dumps(package_script)})\n'
            'let output = runProbe info |> diagnose |> Option.map (serialize fsi.CommandLineArgs[1]) '
            '|> Option.defaultValue ""\n'
            + ('System.Threading.Thread.Sleep(30000)\n' if force_launcher_timeout else '')
            + 'File.WriteAllText(fsi.CommandLineArgs[2], output)\n',
            encoding="utf-8")

        def running(pid):
            result = subprocess.run(
                [POWERSHELL, "-NoProfile", "-Command",
                 f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ exit 1 }} else {{ exit 0 }}"],
                text=True, capture_output=True, timeout=5)
            self.assertIn(result.returncode, (0, 1), result.stderr)
            return result.returncode == 1

        for host, command in self.launchers():
            with self.subTest(host=host, command=command[0], force_launcher_timeout=force_launcher_timeout):
                sentinel = subprocess.Popen([POWERSHELL, "-NoProfile", "-Command",
                                             "Start-Sleep -Seconds 30"],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    message = self.context(host, self.run_hook(command))
                    if force_launcher_timeout:
                        self.assertIn("8-second diagnostic launcher budget expired", message)
                    else:
                        self.assertRegex(message, "timed out|budget expired")
                    self.assertNotIn("configured feeds did not provide", message)
                    self.assertFalse(running(int(search_pid.read_text())), "Search did not exit.")
                    self.assertFalse(running(int(provider_pid.read_text())), "Stream holder survived.")
                    self.assertIsNone(sentinel.poll(), "Cleanup killed an unrelated process.")
                finally:
                    sentinel.terminate()
                    sentinel.wait(timeout=5)
                    for path in (provider_pid, search_pid):
                        if path.exists():
                            pid = int(path.read_text())
                            if running(pid):
                                os.kill(pid, signal.SIGTERM)
                            path.unlink()

    @unittest.skipUnless(os.name == "nt", "Windows inherited streams and process ownership")
    def test_windows_exited_search_stops_stream_holding_descendant(self):
        self.assert_windows_stream_holder_cleanup(force_launcher_timeout=False)

    @unittest.skipUnless(os.name == "nt", "Windows inherited streams and process ownership")
    def test_windows_launcher_timeout_stops_orphaned_stream_holder(self):
        self.assert_windows_stream_holder_cleanup(force_launcher_timeout=True)

    def assert_unix_tree_cleanup(self, exit_early):
        fake_bin = self.root / "fake sdk"
        fake_bin.mkdir()
        leader_pid = self.root / "leader.pid"
        package_pid = self.root / "package.pid"
        credential_pid = self.root / "credential.pid"
        group_file = self.root / "group.pid"
        package = fake_bin / "package-search"
        package.write_text(
            "#!/bin/bash\ntrap '' TERM\nsleep 30 &\n"
            f"printf '%s\\n' \"$$\" > {shlex.quote(str(package_pid))}\n"
            f"printf '%s\\n' \"$!\" > {shlex.quote(str(credential_pid))}\nwait\n",
            encoding="utf-8")
        dotnet = fake_bin / "dotnet"
        dotnet.write_text(
            "#!/bin/bash\ntrap '' TERM\n"
            f"bash {shlex.quote(str(package))} &\n"
            f"printf '%s\\n' \"$$\" > {shlex.quote(str(leader_pid))}\n"
            f"ps -o pgid= -p \"$$\" > {shlex.quote(str(group_file))}\n"
            f"while [ ! -s {shlex.quote(str(credential_pid))} ]; do sleep 0.05; done\n"
            + ("exit 1\n" if exit_early else "wait\n"), encoding="utf-8")
        dotnet.chmod(0o700)
        env = dict(self.env, PATH=str(fake_bin) + os.pathsep + self.env["PATH"])

        def running(pid):
            status = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], text=True, capture_output=True, check=False)
            return bool(status.stdout.strip()) and not status.stdout.strip().startswith("Z")

        for host, command in self.launchers():
            with self.subTest(host=host, exit_early=exit_early):
                sentinel = subprocess.Popen(["sleep", "30"])
                try:
                    message = self.context(host, self.run_hook(command, env=env))
                    self.assertIn("selected SDK" if exit_early else "budget expired", message)
                    self.assertNotIn("configured feeds did not provide", message)
                    pids = [int(path.read_text()) for path in (leader_pid, package_pid, credential_pid)]
                    self.assertEqual(int(group_file.read_text()), pids[0], "Helper must own its group.")
                    deadline = time.monotonic() + 2
                    while any(running(pid) for pid in pids) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertFalse(any(running(pid) for pid in pids), "Helper descendants survived.")
                    self.assertIsNone(sentinel.poll(), "Cleanup must not kill unrelated processes.")
                finally:
                    sentinel.terminate()
                    sentinel.wait(timeout=5)
                    for path in (credential_pid, package_pid, leader_pid):
                        if path.exists():
                            pid = int(path.read_text())
                            if running(pid):
                                os.kill(pid, signal.SIGKILL)
                            path.unlink()
                    if group_file.exists():
                        group_file.unlink()

    @unittest.skipIf(os.name == "nt", "Native Unix process groups")
    def test_unix_timeout_stops_descendants(self):
        self.assert_unix_tree_cleanup(exit_early=False)

    @unittest.skipIf(os.name == "nt", "Native Unix process groups")
    def test_unix_early_exit_stops_descendants(self):
        self.assert_unix_tree_cleanup(exit_early=True)

    def test_hook_declarations_and_unchanged_mcp_launch(self):
        copilot = json.loads((PLUGIN / "hooks" / "copilot.json").read_text())
        self.assertEqual(copilot["version"], 1)
        hook = copilot["hooks"]["sessionStart"][0]
        self.assertEqual(hook["timeoutSec"], 10)
        self.assertIn("${PLUGIN_ROOT}", hook["powershell"])
        self.assertIn("${PLUGIN_ROOT}", hook["bash"])
        self.assertTrue(hook["bash"].startswith("bash "))
        claude = json.loads((PLUGIN / "hooks" / "claude.json").read_text())
        hook = claude["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertEqual(hook["timeout"], 10)
        self.assertEqual(hook["shell"], "bash")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", hook["command"])
        self.assertTrue(hook["command"].startswith("bash "))
        for relative, expected in (("plugin.json", "./hooks/copilot.json"),
                                   (".claude-plugin/plugin.json", "./hooks/claude.json"),
                                   (".codex-plugin/plugin.json", None)):
            manifest = json.loads((PLUGIN / relative).read_text())
            self.assertEqual(manifest.get("hooks"), expected)
            server = manifest["mcpServers"]["binlog"]
            self.assertEqual(server["command"], "dotnet")
            self.assertEqual(server["args"], ["dnx", "Microsoft.AITools.BinlogMcp", "--yes", "--prerelease"])
        marketplace = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
        server = next(p for p in marketplace["plugins"] if p["name"] == "dotnet-msbuild")["mcpServers"]["binlog"]
        self.assertEqual(server["args"], ["dnx", "Microsoft.AITools.BinlogMcp", "--yes", "--prerelease"])


if __name__ == "__main__":
    unittest.main()
