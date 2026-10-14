param()

$ErrorActionPreference = 'Stop'

function Write-Diagnostic([string] $Reason) {
    $message = "dotnet-msbuild binlog MCP preflight: $Reason Package availability could not be verified. Tell the user. " +
        'Recovery options: make Microsoft.AITools.BinlogMcp available through an approved feed/mirror; add nuget.org only if organizational policy permits; ' +
        'or install the tool from an approved feed and register its binlog-mcp executable as an MCP server. Do not change NuGet configuration or bypass feed policy automatically.'
    @{ additionalContext = $message } | ConvertTo-Json -Compress
}

$dotnet = Get-Command dotnet -CommandType Application -ErrorAction SilentlyContinue
if (-not $dotnet) {
    Write-Diagnostic 'dotnet was not found on the host PATH. Install the .NET 10+ SDK and restart the host.'
    exit 0
}

$child = $null
$outputPath = $null
try {
    $inputJson = [Console]::In.ReadToEnd()
    $workspace = ($inputJson | ConvertFrom-Json).cwd
    if (-not $workspace -or -not [IO.Directory]::Exists($workspace)) {
        Write-Diagnostic "The hook could not use the session's workspace directory."
        exit 0
    }
    $outputPath = Join-Path ([IO.Path]::GetTempPath()) ("binlog-preflight-" + [guid]::NewGuid().ToString('N') + '.json')
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $dotnet.Source
    $info.Arguments = 'fsi --exec "' + (Join-Path $PSScriptRoot 'binlog-preflight.fsx') + '" copilot "' + $outputPath + '"'
    $info.WorkingDirectory = $workspace
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $child = New-Object Diagnostics.Process
    $child.StartInfo = $info
    [void]$child.Start()
    $stdout = $child.StandardOutput.ReadToEndAsync()
    $stderr = $child.StandardError.ReadToEndAsync()
    $child.StandardInput.Write($inputJson)
    $child.StandardInput.Close()
    if (-not $child.WaitForExit(8000)) {
        # Kill only this launcher's process tree, including SDK and credential-provider children.
        $stopFailed = & {
            $ErrorActionPreference = 'Continue'
            & taskkill.exe /PID $child.Id /T /F 2>&1 | Out-Null
            $LASTEXITCODE -ne 0
        }
        if ($stopFailed -and -not $child.HasExited) {
            Write-Diagnostic 'The 8-second diagnostic launcher budget expired, and its process could not be stopped. This does not establish that MCP startup failed.'
        } else {
            Write-Diagnostic 'The 8-second diagnostic launcher budget expired; this does not establish that MCP startup failed.'
        }
    } elseif ($child.ExitCode -ne 0) {
        Write-Diagnostic 'The diagnostic script could not run with the selected SDK. Check dotnet --info and workspace global.json; the .NET 10+ SDK with dotnet fsi is required.'
    } elseif (-not [IO.File]::Exists($outputPath)) {
        Write-Diagnostic 'The selected SDK did not produce a diagnostic payload. Check dotnet --info and workspace global.json.'
    } else {
        $output = [IO.File]::ReadAllText($outputPath).Trim()
        if ($output) {
            # Re-serialize only the documented output, never SDK banners or subprocess stderr.
            $payload = $output | ConvertFrom-Json
            if ($payload.additionalContext -isnot [string]) {
                throw [FormatException]::new('Invalid hook output.')
            }
            @{ additionalContext = $payload.additionalContext } | ConvertTo-Json -Compress
        }
    }
} catch [System.ComponentModel.Win32Exception] {
    Write-Diagnostic 'dotnet could not be started. Check the .NET 10+ SDK installation and host PATH.'
} catch [System.IO.IOException] {
    Write-Diagnostic 'The diagnostic launcher encountered an I/O error.'
} catch [System.ArgumentException] {
    Write-Diagnostic 'The diagnostic launcher received an invalid workspace or output payload.'
} catch [System.FormatException] {
    Write-Diagnostic 'The selected SDK did not return a valid diagnostic payload. Check dotnet --info and workspace global.json.'
} finally {
    if ($child) { $child.Dispose() }
    if ($outputPath -and [IO.File]::Exists($outputPath)) { Remove-Item -LiteralPath $outputPath -Force }
}
exit 0
