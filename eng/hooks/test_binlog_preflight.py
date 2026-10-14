#!/usr/bin/env python3
"""Exercise shipped hooks with real SDK/NuGet settings, independently of skill evaluations."""

import http.server
import json
import os
from pathlib import Path
import shutil
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
SH = shutil.which("sh")
if os.name == "nt":
    git_sh = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "sh.exe"
    if git_sh.exists():
        SH = str(git_sh)


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
        self.assertIsNotNone(SH, "The supported Claude Bash shell must provide sh.")
        for host in ("copilot", "claude"):
            yield host, [SH, str(self.plugin / "hooks" / "session-start.sh"), host]

    def run_hook(self, command, env=None, cwd=None):
        started = time.monotonic()
        result = subprocess.run(
            command, cwd=cwd or self.workspace, env=env or self.env,
            input=json.dumps({"cwd": str(self.workspace), "source": "startup"}),
            text=True, capture_output=True, timeout=12)
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
""" + f"& '{launcher}'"
        command = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]
        message = self.context("copilot", self.run_hook(command))
        self.assertIn("8-second diagnostic launcher budget expired", message)
        self.assertNotIn("configured feeds did not provide", message)

    def test_hook_declarations_and_unchanged_mcp_launch(self):
        copilot = json.loads((PLUGIN / "hooks" / "copilot.json").read_text())
        self.assertEqual(copilot["version"], 1)
        hook = copilot["hooks"]["sessionStart"][0]
        self.assertEqual(hook["timeoutSec"], 10)
        self.assertIn("${PLUGIN_ROOT}", hook["powershell"])
        self.assertIn("${PLUGIN_ROOT}", hook["bash"])
        claude = json.loads((PLUGIN / "hooks" / "claude.json").read_text())
        hook = claude["hooks"]["SessionStart"][0]["hooks"][0]
        self.assertEqual(hook["timeout"], 10)
        self.assertEqual(hook["shell"], "bash")
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", hook["command"])
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
