#load "../../plugins/dotnet-msbuild/hooks/binlog-preflight.fsx"

open System
open System.Diagnostics
open System.Text.Json
open ``Binlog-preflight``

let mutable assertions = 0
let check condition name =
    assertions <- assertions + 1
    if not condition then failwith name

let result packages problems sourceProblems =
    sprintf """{"version":2,"problems":%s,"searchResult":[{"sourceName":"mirror","packages":%s,"problems":%s}]}"""
        problems packages sourceProblems

let missing = result "[]" "[]" "[]"
let available = result """[{"id":"microsoft.aitools.binlogmcp","version":"3.0.4-preview"}]""" "[]" "[]"
let problem = """[{"problemType":"Error","text":"401 https://user:secret@mirror.invalid/?token=secret"}]"""
let contains (text: string) (message: string option) = message |> Option.exists (fun value -> value.Contains(text))

check (diagnose (Completed(0, available)) = None) "Available exact prerelease package should be silent."
check (diagnose (Completed(0, "credential warning\n" + available)) = None) "Ignore non-JSON credential-provider warnings."
check (diagnose (Completed(0, missing)) |> contains "configured feeds did not provide") "Exit zero with empty packages is not success."
check (diagnose (Completed(0, missing)) |> contains "source mapping") "Absence must not claim equivalence with dnx."
check (diagnose (Completed(0, missing)) |> contains "not a startup test") "Search must not claim startup failed."

for json in [
    result "[]" "[]" problem;
    result "[]" problem "[]";
    """{"problems":""" + problem + "}";
    "credential warning\n" + result "[]" "[]" problem;
    result """[{"id":"Microsoft.AITools.BinlogMcp","version":"3.0.4"}]""" "[]" problem
] do
    let message = diagnose (Completed(0, json))
    check (message |> contains "NuGet reported") "Problems must be reported despite exit zero or another feed providing the package."
    check (not (message |> contains "configured feeds did not provide")) "Problems are not package absence."
    check (not (message |> contains "secret")) "Do not expose credentials."

for json in [
    "";
    available + "\ntrailing junk";
    "{}";
    """{"searchResult":[]}""";
    """{"searchResult":[{"packages":null}]}""";
    """{"searchResult":[{"packages":[],"problems":{}}]}""";
    """{"problems":{},"searchResult":[{"packages":[]}]}""";
    result """[{"id":"Other.Package","version":"1.0.0"}]""" "[]" "[]";
    result """[{"id":"Microsoft.AITools.BinlogMcp"}]""" "[]" "[]";
    result """[{"id":"Microsoft.AITools.BinlogMcp","version":null}]""" "[]" "[]";
    """{"searchResult":[{"packages":[{"id":"Microsoft.AITools.BinlogMcp","version":"1.0"}]},{"packages":null}]}"""
] do
    let message = diagnose (Completed(0, json))
    check (message |> contains "could not be verified") "Unsupported or malformed output must be explicit."
    check (not (message |> contains "configured feeds did not provide")) "Unsupported output is not package absence."

check (diagnose (Completed(1, missing)) |> contains "could not be verified") "Nonzero exit with empty results is not confirmed absence."
check (diagnose TimedOut |> contains "5-second") "Probe timeout must be concrete."
check (not (diagnose TimedOut |> contains "configured feeds did not provide")) "Timeout is not absence."
check (diagnose CouldNotStart |> contains ".NET 10+") "Missing dotnet must be intelligible."
check (diagnose CouldNotOwnProcessTree |> contains "process-tree cleanup") "Windows ownership failure must be explicit."
check (diagnose CouldNotOwnProcessTree |> contains "probe was not started") "Do not launch an unowned Windows probe."
check (not (diagnose CouldNotOwnProcessTree |> contains "configured feeds did not provide")) "Ownership failure is not absence."
check (Runtime.InteropServices.Marshal.SizeOf<WindowsProcessJob.IoCounters>() = 48) "Windows IO_COUNTERS layout."
check (Runtime.InteropServices.Marshal.SizeOf<WindowsProcessJob.BasicLimits>() = (if IntPtr.Size = 8 then 64 else 48)) "Windows basic job limits layout."
check (Runtime.InteropServices.Marshal.SizeOf<WindowsProcessJob.ExtendedLimits>() = (if IntPtr.Size = 8 then 144 else 112)) "Windows extended job limits layout."
for host in [ "copilot"; "claude" ] do
    let message = diagnose (Completed(0, missing)) |> Option.get
    use output = JsonDocument.Parse(serialize host message)
    let context =
        if host = "claude" then
            let specific = output.RootElement.GetProperty("hookSpecificOutput")
            check (specific.GetProperty("hookEventName").GetString() = "SessionStart") "Claude event name."
            specific.GetProperty("additionalContext").GetString()
        else output.RootElement.GetProperty("additionalContext").GetString()
    for expected in [ "approved feed/mirror"; "organizational policy permits"; "binlog-mcp"; "Do not change NuGet configuration" ] do
        check (context.Contains(expected)) "Actionable policy-preserving recovery."

let unavailable = ProcessStartInfo("dotnet-msbuild-test-no-such-executable")
check (runProbe unavailable = CouldNotStart) "Process start failure must not throw."
check (searchArguments = [ "package"; "search"; "Microsoft.AITools.BinlogMcp"; "--exact-match"; "--prerelease"; "--format"; "json" ]) "No config/source override or localized table parser."
let input = JsonSerializer.Serialize {| cwd = Environment.CurrentDirectory |}
let inputPath = IO.Path.GetTempFileName()
try
    IO.File.WriteAllText(inputPath, input, Text.UTF8Encoding(false))
    check (workspace inputPath = Environment.CurrentDirectory) "Use the UTF-8 session workspace."
    IO.File.WriteAllText(inputPath, input, Text.UTF8Encoding(true))
    check (workspace inputPath = Environment.CurrentDirectory) "Accept a UTF-8 input BOM."
finally
    IO.File.Delete(inputPath)
printfn "Passed %d preflight assertions." assertions
