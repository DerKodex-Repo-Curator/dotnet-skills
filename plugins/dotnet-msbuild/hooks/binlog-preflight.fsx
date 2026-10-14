open System
open System.ComponentModel
open System.Diagnostics
open System.IO
open System.Text.Json
open System.Threading.Tasks

let packageId = "Microsoft.AITools.BinlogMcp"
let searchArguments =
    [ "package"; "search"; packageId; "--exact-match"; "--prerelease"; "--format"; "json" ]

let recovery =
    "Tell the user. Recovery options: make Microsoft.AITools.BinlogMcp available through an approved feed/mirror; "
    + "add nuget.org only if organizational policy permits; or install the tool from an approved feed "
    + "and register its binlog-mcp executable as an MCP server. Do not change NuGet configuration or bypass feed policy automatically."

let context reason = "dotnet-msbuild binlog MCP preflight: " + reason + " " + recovery

type ProbeResult =
    | Completed of exitCode: int * stdout: string
    | TimedOut
    | CouldNotStart

let runProbe (startInfo: ProcessStartInfo) =
    startInfo.UseShellExecute <- false
    startInfo.RedirectStandardOutput <- true
    startInfo.RedirectStandardError <- true
    startInfo.CreateNoWindow <- true
    use child = new Process(StartInfo = startInfo)
    try
        if not (child.Start()) then CouldNotStart
        else
            let stdout = child.StandardOutput.ReadToEndAsync()
            let stderr = child.StandardError.ReadToEndAsync()
            if not (child.WaitForExit(5000)) then
                child.Kill(true)
                child.WaitForExit(500) |> ignore
                TimedOut
            elif not (Task.WhenAll(stdout, stderr).Wait(500)) then
                TimedOut
            else
                Completed(child.ExitCode, stdout.Result)
    with
    | :? Win32Exception -> CouldNotStart
    | :? IOException -> CouldNotStart

let unknown =
    "Package availability could not be verified. The selected SDK or NuGet configuration may not support the probe. "
    + "Check dotnet --info and the workspace global.json, then run "
    + "dotnet package search Microsoft.AITools.BinlogMcp --exact-match --prerelease --format json locally for details."

let feedError =
    "NuGet reported feed/authentication/network/configuration problems during exact-package search; "
    + "package availability could not be verified. This is not a missing-package result. "
    + "Check feed access, credentials, connectivity, and NuGet configuration; run "
    + "dotnet package search Microsoft.AITools.BinlogMcp --exact-match --prerelease --format json locally to inspect the errors."

let property name (element: JsonElement) =
    match element.TryGetProperty(name: string) with
    | true, value -> Some value
    | false, _ -> None

let arrayProperty name element =
    match property name element with
    | Some value when value.ValueKind = JsonValueKind.Array -> value.EnumerateArray() |> Seq.toArray
    | _ -> raise (JsonException("Expected an array."))

let hasProblems element =
    match property "problems" element with
    | None -> false
    | Some _ -> arrayProperty "problems" element |> Array.isEmpty |> not

let exactPackage (package: JsonElement) =
    match property "id" package, property "version" package with
    | Some id, Some version when id.ValueKind = JsonValueKind.String && version.ValueKind = JsonValueKind.String ->
        String.Equals(id.GetString(), packageId, StringComparison.OrdinalIgnoreCase)
        && not (String.IsNullOrWhiteSpace(version.GetString()))
    | _ -> raise (JsonException("Expected a package identity."))

let structuredOutput (stdout: string) =
    // Credential providers can prepend warnings. Read the renderer's trailing JSON, not localized logs.
    let start = stdout.LastIndexOf("\n{", StringComparison.Ordinal)
    if start < 0 then stdout else stdout.Substring(start + 1)

let diagnose result =
    match result with
    | TimedOut -> Some(context "The 5-second NuGet probe timed out. Package availability could not be verified; this does not establish that MCP startup failed.")
    | CouldNotStart -> Some(context "dotnet could not be started in the workspace. Check that the .NET 10+ SDK is installed and dotnet is on the host's PATH. Package availability could not be verified.")
    | Completed(exitCode, stdout) ->
        try
            use document = JsonDocument.Parse(structuredOutput stdout)
            let root = document.RootElement
            if hasProblems root then Some(context feedError)
            else
                let sources = arrayProperty "searchResult" root
                if Array.exists hasProblems sources then Some(context feedError)
                elif exitCode <> 0 || sources.Length = 0 then Some(context unknown)
                else
                    let packages = sources |> Array.collect (arrayProperty "packages")
                    let identities = packages |> Array.map exactPackage
                    if Array.contains true identities then None
                    elif packages.Length > 0 then Some(context unknown)
                    else
                        Some(context (
                            "The configured feeds did not provide Microsoft.AITools.BinlogMcp in exact-package search (including prerelease versions). "
                            + "This can explain the binlog MCP connection closing before the server starts. "
                            + "Search is not a startup test: dnx also applies package source mapping and may use cached tools or unlisted versions."))
        with
        | :? JsonException -> Some(context unknown)
        | :? InvalidOperationException -> Some(context unknown)

let serialize host message =
    if host = "claude" then
        JsonSerializer.Serialize {| hookSpecificOutput = {| hookEventName = "SessionStart"; additionalContext = message |} |}
    else
        JsonSerializer.Serialize {| additionalContext = message |}

let workspace (input: string) =
    use document = JsonDocument.Parse(input)
    match property "cwd" document.RootElement with
    | Some cwd when cwd.ValueKind = JsonValueKind.String && Directory.Exists(cwd.GetString()) -> cwd.GetString()
    | _ -> raise (JsonException("Expected a workspace directory."))

let main host outputPath =
    let message =
        try
            let cwd = workspace (Console.In.ReadToEnd())
            Directory.SetCurrentDirectory(cwd)
            if Environment.Version.Major < 10 then Some(context unknown)
            else
                let startInfo = ProcessStartInfo("dotnet", WorkingDirectory = cwd)
                searchArguments |> List.iter startInfo.ArgumentList.Add
                runProbe startInfo |> diagnose
        with
        | :? JsonException
        | :? IOException
        | :? UnauthorizedAccessException -> Some(context "The hook could not use the session's workspace directory. Package availability could not be verified.")
    let output = message |> Option.map (serialize host) |> Option.defaultValue ""
    File.WriteAllText(outputPath, output)

if Path.GetFullPath(fsi.CommandLineArgs[0]) = Path.Combine(__SOURCE_DIRECTORY__, __SOURCE_FILE__) then
    main fsi.CommandLineArgs[1] fsi.CommandLineArgs[2]
