# dotnet-msbuild

Skills for MSBuild failure diagnosis, performance optimization, code quality, and modernization.
The plugin also exposes the `binlog` MCP server to supported hosts.

Files under `agents/` are GitHub Copilot custom-agent definitions. Codex plugin installs expose the
skills and the Codex-compatible `binlog` MCP declaration, but do not install those `.agent.md` files
as native Codex agents.

## Binlog startup diagnostics

Copilot CLI 1.0.97 and Claude Code receive assistant-visible session-start context when a bounded
NuGet preflight cannot find `Microsoft.AITools.BinlogMcp` or cannot verify its availability.
The MCP command itself is unchanged. The probe uses the session's workspace directory and
inherited SDK/NuGet settings; it never adds feeds or rewrites configuration.

The hook supports Windows, macOS, and Linux with the .NET 10+ SDK, using its bundled
`dotnet fsi` interpreter (no package restore or extra JSON parser). Copilot uses PowerShell
on Windows and Bash elsewhere. Claude uses its documented Bash hook shell; native Windows
Claude therefore requires Git Bash and delegates to the PowerShell launcher. Missing `dotnet`,
an incompatible workspace `global.json`, feed errors, and timeouts produce diagnostic context
rather than a missing-package claim.
These host-specific hooks do not claim diagnostic support for Codex, VS Code, or Cursor.

An exact, prerelease-inclusive JSON package search has a 5-second budget inside an 8-second
launcher budget and a 10-second host timeout. Successful availability checks are silent.
The Unix launcher isolates the helper in its own process group and cleans up that group,
including package-search and credential-provider descendants, on timeout or exit.
The Windows launcher terminates its owned process tree instead.
Search does **not** apply package source mapping, unlike `dnx`, and does not verify tool
download, authentication to mapped sources, runtime compatibility, caches, or MCP initialization.
An empty search establishes only that the searched feeds did not provide a listed package,
not that every possible `dnx` launch must fail. No raw feed names, URLs, credentials, or
subprocess errors are injected into assistant context. Run the reported search command locally
to inspect detailed feed errors.

Recovery options are to make the package available in an approved feed/mirror, add nuget.org
only if organizational policy permits, or install the tool from an approved feed and register
its `binlog-mcp` executable as an MCP server.

Hook schemas follow the [Copilot hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference)
and [Claude hooks reference](https://code.claude.com/docs/en/hooks#sessionstart-decision-control).

Run the focused regressions with `python eng/hooks/test_binlog_preflight.py` from the repository
root. They use real SDK searches against isolated workspace configurations, process-tree cleanup
fixtures, and structured-result tests, not the skill evaluator's overridden MCP launch.
The CI matrix targets Windows, macOS, and Linux; native macOS/Linux runs were not performed locally.
Live assistant delivery was verified
with Copilot CLI 1.0.97 on Windows; Claude's output schema and shell path have regression coverage,
but its live-session delivery has not been verified.
Copilot CLI 1.0.68 did not load the plugin hook in that check; older CLI releases are not covered.
