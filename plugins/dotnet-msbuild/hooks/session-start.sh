#!/bin/bash

host=$1
diagnostic() {
    # Reasons below are fixed ASCII strings: no feed URLs, credentials, or subprocess output.
    message="dotnet-msbuild binlog MCP preflight: $1 Package availability could not be verified. Tell the user. Recovery options: make Microsoft.AITools.BinlogMcp available through an approved feed/mirror; add nuget.org only if organizational policy permits; or install the tool from an approved feed and register its binlog-mcp executable as an MCP server. Do not change NuGet configuration or bypass feed policy automatically."
    if [ "$host" = claude ]; then
        printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$message"
    else
        printf '{"additionalContext":"%s"}\n' "$message"
    fi
}

if ! command -v dotnet >/dev/null 2>&1; then
    diagnostic 'dotnet was not found on the host PATH. Install the .NET 10+ SDK and restart the host.'
    exit 0
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
case "$OSTYPE" in
    msys*|cygwin*)
        # MSYS process groups do not reliably include native Windows SDK descendants.
        exec powershell.exe -NoProfile -ExecutionPolicy Bypass \
            -File "$(cygpath -w "$script_dir/session-start.ps1")" -HostName "$host"
        ;;
esac

scratch=$(mktemp -d 2>/dev/null) || {
    diagnostic 'The diagnostic launcher could not create its temporary directory.'
    exit 0
}
child=
stop_helper() {
    if [ -n "$child" ]; then
        kill -TERM -- "-$child" 2>/dev/null
        kill -KILL -- "-$child" 2>/dev/null
        wait "$child" 2>/dev/null
        child=
    fi
}
cleanup() {
    stop_helper
    rm -f "$scratch/input" "$scratch/output"
    rmdir "$scratch"
}
trap cleanup EXIT
trap 'diagnostic "The diagnostic launcher was interrupted; this does not establish that MCP startup failed."; exit 0' HUP INT TERM
cat >"$scratch/input"
# Only the script's serialized payload reaches the host, never SDK first-run banners.
# Bash job control gives this asynchronous command its own process group on macOS and Linux.
set -m
dotnet fsi --exec "$script_dir/binlog-preflight.fsx" "$host" "$scratch/output" "$scratch/input" </dev/null >/dev/null 2>&1 &
child=$!
set +m
elapsed=0
while kill -0 "$child" 2>/dev/null && [ "$elapsed" -lt 8 ]; do
    sleep 1
    elapsed=$((elapsed + 1))
done
if kill -0 "$child" 2>/dev/null; then
    stop_helper
    diagnostic 'The 8-second diagnostic launcher budget expired; this does not establish that MCP startup failed.'
elif wait "$child" && [ -f "$scratch/output" ]; then
    cat "$scratch/output"
else
    diagnostic 'The diagnostic script could not run with the selected SDK. Check dotnet --info and workspace global.json; the .NET 10+ SDK with dotnet fsi is required.'
fi
exit 0
