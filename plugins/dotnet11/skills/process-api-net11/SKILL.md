---
name: process-api-net11
description: >
  Provides guidance on the new System.Diagnostics.Process APIs introduced in .NET 11:
  Process.Run, Process.RunAndCaptureText, Process.StartAndForget, Process.ReadAllText/Bytes/Lines,
  KillOnParentExit, InheritedHandles, and StartDetached.
  USE FOR: starting, orchestrating, or capturing output from external processes in applications targeting net11.0 or later.
  DO NOT USE FOR: applications targeting .NET 10 or earlier, or basic pre-.NET 11 Process.Start usage without new APIs.
license: MIT
---

# Process API Improvements — .NET 11

New APIs added to `System.Diagnostics.Process` in .NET 11 simplify process management, eliminate boilerplate, and prevent common deadlock patterns when capturing output in .NET 11 or later applications.

## When to Use

- Running or orchestrating external processes in a .NET 11 (or later) project.
- Needing to start a process, wait for it to exit, and capture its output/error streams without risking deadlocks (`Process.RunAndCaptureText[Async]`).
- Wanting to ensure child processes are automatically terminated when the parent process exits (`KillOnParentExit`).
- Requiring lightweight, low-overhead process creation via `SafeProcessHandle` on supported platforms.
- Requiring fine-grained control over handle inheritance (`InheritedHandles`) or starting detached processes (`StartDetached`).

## When Not to Use

- The project targets .NET 10 or earlier — these APIs are not available before .NET 11.
- The default `Process.Start()` is sufficient and does not require output capturing or advanced lifecycle rules.

## Target Framework

```xml
<TargetFramework>net11.0</TargetFramework>
```

## Capturing Output Without Pipe Buffer Deadlocks

Redirecting standard output and standard error creates two separate pipes. Each pipe has a finite buffer. If the child fills a pipe, its next write blocks until the parent reads data from that pipe.

### Do Not Wait for Exit Before Reading

With both streams redirected, this code can deadlock:

```csharp
ProcessStartInfo startInfo = new("dotnet", "--help")
{
    RedirectStandardOutput = true,
    RedirectStandardError = true
};

using Process process = new() { StartInfo = startInfo };
process.Start();
process.WaitForExit();

string output = process.StandardOutput.ReadToEnd();
string error = process.StandardError.ReadToEnd();
```

The parent waits for the child to exit without draining either pipe. If the child fills either buffer, it blocks waiting for the parent to read, so neither process can make progress. Small outputs may hide this bug; it is not safe to assume the output will fit in the buffers.

### Do Not Read Standard Output and Standard Error Sequentially

Moving the reads before the wait does not fix the problem:

```csharp
process.Start();

string output = process.StandardOutput.ReadToEnd();
string error = process.StandardError.ReadToEnd();

process.WaitForExit();
```

Here the parent reads stdout until EOF before it starts draining stderr. If the child fills stderr while stdout remains open, the child blocks writing stderr and the parent blocks waiting for stdout to close. Replacing these reads with sequentially awaited `ReadToEndAsync` calls has the same problem.

**Drain both redirected streams concurrently, before waiting for process exit.** The following .NET 11 APIs do this for you. They prevent this pipe-buffer deadlock, not every possible hang: a descendant that retains an inherited pipe writer can still delay EOF.

### Capture Text or Bytes From a Started Process

Use `Process.ReadAllText` or `Process.ReadAllTextAsync` to capture both streams as text. They drain stdout and stderr concurrently and return a tuple containing both strings. Text is decoded using `ProcessStartInfo.StandardOutputEncoding` and `StandardErrorEncoding`, or the defaults when unspecified.

**Use full-output capture only when the output size is trusted and bounded.** `ReadAllText[Async]` and `ReadAllBytes[Async]` read until EOF and buffer all captured output in the parent process's memory, without an output-size limit. An untrusted child can produce unlimited output and exhaust the parent's memory, causing an out-of-memory failure. A timeout or cancellation token is not a memory limit. For untrusted output, use a bounded capture strategy that drains both streams concurrently, enforces an explicit byte limit, and terminates the child when that limit is exceeded.

```csharp
ProcessStartInfo startInfo = new("dotnet", "--help")
{
    RedirectStandardOutput = true,
    RedirectStandardError = true
};

using Process process = new() { StartInfo = startInfo };
process.Start();

(string output, string error) = process.ReadAllText();
ProcessExitStatus status = process.WaitForExitStatus();
```

For asynchronous reading, replace the last two lines with:

```csharp
(string output, string error) = await process.ReadAllTextAsync();
ProcessExitStatus status = await process.WaitForExitStatusAsync();
```

Use `Process.ReadAllBytes` or `Process.ReadAllBytesAsync` instead when the output is binary or you need the original bytes without text decoding. With the same redirection setup, replace the last two lines of the synchronous example with:

```csharp
(byte[] output, byte[] error) = process.ReadAllBytes();
ProcessExitStatus status = process.WaitForExitStatus();
```

The asynchronous equivalent is:

```csharp
(byte[] output, byte[] error) = await process.ReadAllBytesAsync();
ProcessExitStatus status = await process.WaitForExitStatusAsync();
```

These instance methods read to EOF; they do not replace waiting for process exit. A child can close its standard streams and keep running. After reading, use `WaitForExitStatus` or await `WaitForExitStatusAsync` to wait for termination and obtain the exit status. See [Waiting for Process Exit](#waiting-for-process-exit) for cancellation and timeout policies.

### Process Output Line by Line With ReadAllLines[Async]

Use `Process.ReadAllLines` or `Process.ReadAllLinesAsync` to read stdout and stderr as one sequence of `ProcessOutputLine` values, interleaved as lines become available. `Content` contains the line's text, and `StandardError` identifies its source. Enable both output and error redirection before starting the process. Consume the sequence with `foreach` or `await foreach` while the process runs; merely obtaining the enumerable does not drain output.

Prefer these methods when the interleaving of output and error matters. Capturing stdout and stderr separately and then concatenating the strings loses that interleaving. For example, a child might print:

```text
OUT: A
ERR: B
OUT: C
```

Concatenating captured stdout followed by captured stderr instead produces:

```text
OUT: A
OUT: C
ERR: B
```

The line APIs let you process both streams together instead of grouping all stdout before all stderr. They report the order in which lines become available to the reader, not a guaranteed reconstruction of the child's exact write order across two separate pipes.

Also prefer line-by-line reading when you only need to search for specific text: inspect each line rather than allocating two potentially large strings containing the complete output. With the same process-start and redirection setup as above:

```csharp
bool found = false;
await foreach (ProcessOutputLine line in process.ReadAllLinesAsync())
{
    if (line.Content.Contains("needle", StringComparison.Ordinal))
    {
        found = true;
    }
}
ProcessExitStatus status = await process.WaitForExitStatusAsync();
```

After finding a match, keep draining if the child should run to completion. If it is long-running and no longer needed, kill it and wait for termination instead. Do not simply stop enumeration and wait for a still-running child: it may block on a full pipe.

Line-by-line reading avoids retaining the entire output, but it is not a strict memory bound for untrusted output: a single arbitrarily long line can still exhaust memory.

### Start, Capture Text, and Wait for Exit in One Call

When you only need to start a process, capture its text output and error, and wait for its exit status, prefer `Process.RunAndCaptureText`:

```csharp
ProcessTextOutput result = Process.RunAndCaptureText("dotnet", ["--help"]);
```

Use `Process.RunAndCaptureTextAsync` when asynchronous execution is needed:

```csharp
ProcessTextOutput result = await Process.RunAndCaptureTextAsync("dotnet", ["--help"]);
```

Both methods combine starting the process, draining both streams, and waiting for exit. Read `result.StandardOutput`, `result.StandardError`, and `result.ExitStatus`; no separate `Process` instance or wait is needed. They also accept `ProcessStartInfo` for custom configuration, so needing configuration alone is not a reason to use the instance readers. For those overloads, enable both `RedirectStandardOutput` and `RedirectStandardError` yourself.

`RunAndCaptureText[Async]` also buffers all captured output in memory without an output-size limit. The same trusted, bounded-output requirement applies; do not use it to capture potentially unlimited output from an untrusted process.

### Run and Wait Without Capturing Output

Use `Process.Run` or `Process.RunAsync` when you need to start a process and wait for its exit status without capturing its output. By default, the child inherits the parent's standard handles, so output remains visible. Pass `silent: true` to redirect all three standard handles (stdin, stdout, and stderr) to the null device (`NUL` on Windows, `/dev/null` on Unix). Writes are discarded and reads receive EOF, without creating pipes that the parent must drain.

```csharp
ProcessExitStatus status = Process.Run("dotnet", ["build"], silent: true);
```

The asynchronous equivalent is:

```csharp
ProcessExitStatus status = await Process.RunAsync("dotnet", ["build"], silent: true);
```

For custom `ProcessStartInfo`, use `File.OpenNullHandle()` and assign `StandardInputHandle`, `StandardOutputHandle`, and `StandardErrorHandle` directly:

```csharp
using System.Diagnostics;
using System.IO;
using Microsoft.Win32.SafeHandles;

using SafeFileHandle nullHandle = File.OpenNullHandle();
ProcessStartInfo startInfo = new("dotnet", ["build"])
{
    StandardInputHandle = nullHandle,
    StandardOutputHandle = nullHandle,
    StandardErrorHandle = nullHandle
};

ProcessExitStatus status = Process.Run(startInfo);
```

In asynchronous code, use `await Process.RunAsync(startInfo)` instead. The child receives its own copy of the handle during process creation. Because `Run[Async]` does not expose a separate start-completed boundary, keep the `using` scope around the call, as shown. You can redirect only selected standard handles to the null device and leave the others inherited; do not enable the corresponding `RedirectStandardInput`, `RedirectStandardOutput`, or `RedirectStandardError` flags.

**Never redirect output to pipes merely to suppress it.** This common anti-pattern creates stdout and stderr pipes without consuming them:

```csharp
ProcessStartInfo psi = new("dotnet", "build")
{
    RedirectStandardInput = true,
    RedirectStandardOutput = true,
    RedirectStandardError = true,
};

using Process process = Process.Start(psi)!;
process.WaitForExit();
```

If either output pipe fills, the child blocks writing while the parent waits for exit. Redirected stdin can also leave the child waiting for input or EOF that the parent never supplies. This pattern **must be avoided**: use `silent: true` or null handles to discard output, and the capture or line-reading APIs when output is needed. Replacing the wait with an asynchronous or status wait does not fix undrained pipes.

## Waiting for Process Exit

### Wait for Termination, Not Output EOF

Prefer the .NET 11 status-wait methods when waiting for a process to terminate. They return its exit status without waiting for stdout or stderr to reach EOF.

The existing parameterless `Process.WaitForExit()` and `Process.WaitForExitAsync()` also wait for EOF from redirected streams being read through `BeginOutputReadLine` or `BeginErrorReadLine`. A grandchild can inherit a pipe's write handle and keep it open after the child exits, leaving these waits stuck even though the child has terminated. See [dotnet/runtime#51277](https://github.com/dotnet/runtime/issues/51277). Finite-timeout `WaitForExit` overloads do not wait for those readers to finish; do not assume all overloads have the same output-completion behavior.

The new status waits do not read or drain output. For redirected streams, use `ReadAllText[Async]`, `ReadAllBytes[Async]`, or `ReadAllLines[Async]` to drain stdout and stderr concurrently while the process runs.

```csharp
// Process instance methods
public ProcessExitStatus WaitForExitStatus()
public bool TryWaitForExitStatus(TimeSpan timeout, out ProcessExitStatus? exitStatus)
public Task<ProcessExitStatus> WaitForExitStatusAsync(CancellationToken cancellationToken = default)
```

Use `WaitForExitStatus` for synchronous waiting and `WaitForExitStatusAsync` in asynchronous code. `TryWaitForExitStatus` returns `false` if the timeout expires without terminating the process; only use its `exitStatus` output when it returns `true`.

### Do Not Use Process.ExitCode as a Wait

`Process.ExitCode` is a synchronous property, not an asynchronous completion operation. It checks process state and throws `InvalidOperationException` if the process is still running. Reading all output does not prove that it has exited. Do not poll this property or use it instead of waiting for termination.

**Never use `.Wait()`, `.Result`, or `.GetAwaiter().GetResult()` to synchronously block on an asynchronous process wait: sync-over-async can deadlock and blocks the calling thread.** Do not call synchronous process-wait methods from asynchronous code either.

In asynchronous code, await `WaitForExitStatusAsync` directly and read the returned object's properties:

```csharp
ProcessExitStatus status = await process.WaitForExitStatusAsync();
int exitCode = status.ExitCode;
```

`status.ExitCode` is an already-obtained value, unlike the process-state checks performed by `process.ExitCode`. `ProcessExitStatus` also exposes `Canceled` and the terminating `Signal` on Unix. Interpret exit codes using the external command's contract.

### Choose Whether Cancellation or Timeout Kills the Process

Canceling `Process.WaitForExitStatusAsync(cancellationToken)` stops the wait with `OperationCanceledException`; it does not kill the process. Use that policy only when the child is intentionally allowed to continue and its lifetime is managed elsewhere.

When managing the child's lifetime with a cancellation token, prefer `SafeProcessHandle.WaitForExitOrKillOnCancellationAsync`, available through `process.SafeHandle`. It kills the process when cancellation is requested and then waits for termination, rather than abandoning a running child. For a synchronous timeout with the same cleanup policy, use `WaitForExitOrKillOnTimeout`.

```csharp
// SafeProcessHandle instance methods
public Task<ProcessExitStatus> WaitForExitOrKillOnCancellationAsync(CancellationToken cancellationToken)
public ProcessExitStatus WaitForExitOrKillOnTimeout(TimeSpan timeout)
```

For a started process and a caller-provided cancellation token:

```csharp
ProcessExitStatus status =
    await process.SafeHandle.WaitForExitOrKillOnCancellationAsync(cancellationToken);
```

For a synchronous timeout:

```csharp
ProcessExitStatus status =
    process.SafeHandle.WaitForExitOrKillOnTimeout(TimeSpan.FromSeconds(30));
```

Both return the exit status after termination, with `Canceled` set when the process was killed due to cancellation or timeout. They do not merely throw a cancellation or timeout exception, and the timeout is the time before killing, not a bound on the subsequent termination wait. These methods terminate the associated process, not an entire descendant tree.

If also capturing redirected output, start the kill-on-cancellation wait before awaiting output capture so cancellation can kill a child even while a read is pending. Manage output-read cancellation separately: process termination still does not guarantee EOF if another process retains a pipe writer.

## Redirecting Standard Handles

Use `ProcessStartInfo.StandardInputHandle`, `StandardOutputHandle`, and `StandardErrorHandle` to connect a child's standard streams directly to `SafeFileHandle` instances. This supports process-to-process piping, writing output directly to a file, providing file input, discarding output, and breaking inheritance of the parent's terminal or pipe handles. Unlike `RedirectStandardOutput = true`, assigning a handle does not create a stream for the parent to read through `process.StandardOutput`.

**When output only needs to go to a file or another process, redirect handles directly instead of capturing it in the parent and writing it out again.** Direct file-handle redirection or process-to-process piping is the most efficient approach for these uses: it avoids unnecessary parent-side buffering, copying, and text decoding/re-encoding. Only read output into the parent when it needs to inspect or transform the data.

These properties require `UseShellExecute = false`, the default in modern .NET. Leave the corresponding `RedirectStandardInput`, `RedirectStandardOutput`, or `RedirectStandardError` flag set to `false`; setting both a handle and its redirection flag throws.

| Destination or source | API |
|-----------------------|-----|
| Connected pipe ends for process piping | `SafeFileHandle.CreateAnonymousPipe(out SafeFileHandle readPipe, out SafeFileHandle writePipe)` |
| File input or output | `File.OpenHandle(...)` |
| EOF on input or discarded output/error | `File.OpenNullHandle()` |
| Explicit access to the parent's standard handles | `Console.OpenStandardInputHandle()`, `Console.OpenStandardOutputHandle()`, `Console.OpenStandardErrorHandle()` |

`CreateAnonymousPipe` also accepts `asyncRead` and `asyncWrite` flags for advanced scenarios. Only enable asynchronous handles when the code using that end supports them; child programs normally expect synchronous standard handles.

### Pipe One Process Into Another and Write to a File

On Unix with `ls` and `grep` installed, this is equivalent to:

```sh
ls /usr/bin | grep zip > output.txt
```

The processes exchange bytes through the pipe directly, without the parent reading, buffering, or copying the output:

```csharp
using System.Diagnostics;
using System.IO;
using Microsoft.Win32.SafeHandles;

SafeFileHandle.CreateAnonymousPipe(out SafeFileHandle readPipe, out SafeFileHandle writePipe);

using (readPipe)
using (writePipe)
using (SafeFileHandle outputFile = File.OpenHandle("output.txt", FileMode.Create, FileAccess.Write))
{
    ProcessStartInfo producer = new("ls", ["/usr/bin"])
    {
        StandardOutputHandle = writePipe
    };

    ProcessStartInfo consumer = new("grep", ["zip"])
    {
        StandardInputHandle = readPipe,
        StandardOutputHandle = outputFile
    };

    using Process producerProcess = Process.Start(producer)!;
    writePipe.Dispose();

    using Process consumerProcess = Process.Start(consumer)!;
    readPipe.Dispose();
    outputFile.Dispose();

    ProcessExitStatus producerStatus = await producerProcess.WaitForExitStatusAsync();
    ProcessExitStatus consumerStatus = await consumerProcess.WaitForExitStatusAsync();
}
```

Start both processes before waiting for either to exit so the consumer can drain the producer's pipe. After each successful start, the child owns its copy of the supplied handles; dispose the parent's copies when no longer needed. In particular, retaining the parent's write end would prevent the consumer from reaching EOF even after the producer exits. Disposing these parent handles does not close the children's copies.

The handle-creation helpers create non-inheritable handles. **Do not manually enable inheritance**: `Process` handles passing the specified standard handles to the child. This avoids unnecessarily exposing them to other processes started concurrently.

## Controlling Handle Inheritance

**A pipe reaches EOF only when all handles to its write end have been closed.** Reading both output streams concurrently prevents pipe-buffer deadlocks, but it cannot force EOF while another process still owns a writer. There are two distinct inheritance problems to consider.

### Accidental Inheritance by a Sibling Process

If two threads start children concurrently, an inheritable handle intended for one child can leak into the other:

```log
Parent Process
|- Thread A                          |- Thread B
|                                    |
|  1. Create an inheritable pipe     |  1. Create no pipes, just inherit parent standard handles
|    (read_A, write_A)               |
|                                    |
|  2. Start Child A                  |  2. Start Child B
|     inherits: the pipe             |     inherits: parent standard handles, read_A and write_A (accidental!)
|                                    |
|  3. Close write_A (parent copy)    |  3. Keeps running and holds inherited write_A open
|                                    |
|  4. Child A exits                  |
|                                    |
|  5. Read from read_A               |
|     ┌──────────────────────┐       |
|     │  HANGS FOREVER!      │       |
|     │  write_A is still    │       |
|     │  open in Child B     │       |
|     └──────────────────────┘       |
```

Child B need not write anything to the pipe to keep it open. If it is long-running, the parent's read can remain pending indefinitely after Child A has exited.

.NET creates file handles as non-inheritable by default and coordinates process creation with a global lock on Windows to prevent its temporary inheritable handles from leaking between cooperating starts. Do not undermine these protections by creating inheritable handles yourself: avoid `FileShare.Inheritable`, `HandleInheritability.Inheritable`, Windows P/Invoke handles with `bInheritHandle = true`, or Unix descriptors without `FD_CLOEXEC` unless inheritance is explicitly required and coordinated. Native process-start code does not automatically participate in .NET's lock.

### Restrict Inheritance With ProcessStartInfo.InheritedHandles

Use `InheritedHandles` to specify which additional handles a child may inherit, instead of exposing all inheritable handles in the parent:

| Value | Child inherits |
|-------|----------------|
| `null` (default) | Standard handles plus other inheritable handles, preserving the existing behavior |
| `[]` | Only standard input, output, and error handles |
| Explicit non-empty list | Standard handles plus the listed handles |

For a child that needs no additional handles:

```csharp
ProcessStartInfo startInfo = new("dotnet", ["build"])
{
    InheritedHandles = []
};

using Process process = Process.Start(startInfo)!;
ProcessExitStatus status = await process.WaitForExitStatusAsync();
```

Apply the restriction to each relevant child start: restricting Child A does not prevent an unrestricted Child B from inheriting other handles. Standard handles are included automatically; do not add them to the list. Only `SafeFileHandle` and `SafePipeHandle` instances are supported, and a non-null list requires `UseShellExecute = false` and an empty `UserName`.

**Do not manually enable inheritance on handles passed in this list.** The runtime manages their inheritance flags during process creation. Do not pass the same handle in concurrent process starts; serialize starts sharing that handle or supply separate handles.

On Windows, explicit lists allow concurrent starts under a shared reader lock rather than serializing them under the default exclusive writer lock. This is not lock-free, and it does not coordinate arbitrary native process-start APIs. On Unix systems without native support for restricting inheritance, process creation with a non-null list throws `PlatformNotSupportedException`; do not silently fall back to `null` when restricted inheritance is required.

### Inheritance by a Grandchild Process

Even with the parent's inheritance restricted, the child receives its standard handles and can pass them to its own children:

```log
Parent Process                 Child Process                Grandchild Process
|                              |                            |
|  1. Create pipe              |                            |
|     (read, write)            |                            |
|                              |                            |
|  2. Start Child              |                            |
|     inherits: write          |                            |
|                              |                            |
|  3. Close write              |  1. Start Grandchild       |
|     (parent copy)            |     inherits: write        |
|                              |                            |
|  4. Read from read...        |  2. Child exits            |
|     ┌──────────────────┐     |     closes write           |
|     │  HANGS!          │     |                            |  Still running
|     │  write is still  │     |                            |  keeps write open
|     │  open in         │     |                            |
|     │  Grandchild      │     |                            |
|     └──────────────────┘     |                            |
```

**`InheritedHandles = []` does not prevent a child from passing its standard handles to a grandchild.** The parent cannot control how an arbitrary child launches descendants. When you control that code, give an independent grandchild its own standard handles, for example null handles or a log file, instead of inheriting the child's output pipe.

For an arbitrary child, use `WaitForExitStatus[Async]` to observe its termination independently of output EOF. If complete output capture can remain pending on descendant-held writers, define an explicit output-draining cancellation or timeout policy; do not assume killing or waiting for the immediate child closes every writer. Stopping reads before EOF can lose remaining output, so make that tradeoff explicit.

## New APIs

### Types

Before using the new convenience methods, note the following types. Use the framework types directly:

| Type | Kind | Read-only properties |
|------|------|----------------------|
| `ProcessExitStatus` | Sealed class | `int ExitCode`, `bool Canceled`, `PosixSignal? Signal` |
| `ProcessTextOutput` | Sealed class | `ProcessExitStatus ExitStatus`, `string StandardOutput`, `string StandardError`, `int ProcessId` |
| `ProcessOutputLine` | Readonly struct | `string Content`, `bool StandardError` |

`ProcessExitStatus` describes the outcome of a completed process. Interpret `ExitCode` using the external command's contract; there is no universal `Success` property. `ProcessTextOutput` contains the exit status, captured output and error text, and process ID. `ProcessOutputLine` identifies one output line and whether it came from standard error.

### High-Level Convenience APIs

#### Static Methods

The `ProcessStartInfo` overloads below require `UseShellExecute = false`, which is the default for .NET.

##### `Process.Run` / `Process.RunAsync`
Starts a process and waits for it to exit, returning the exit status. Does not capture standard output or error. Passing `silent: true` redirects standard input, output, and error to the null device, providing EOF on input and discarding output and error without creating pipes to drain. On timeout or cancellation, the process is killed.
```csharp
public static ProcessExitStatus Run(string fileName, IEnumerable<string>? arguments = null, bool silent = false, TimeSpan? timeout = null)
public static Task<ProcessExitStatus> RunAsync(string fileName, IEnumerable<string>? arguments = null, bool silent = false, CancellationToken cancellationToken = default)
public static ProcessExitStatus Run(ProcessStartInfo startInfo, TimeSpan? timeout = null)
public static Task<ProcessExitStatus> RunAsync(ProcessStartInfo startInfo, CancellationToken cancellationToken = default)
```

##### `Process.RunAndCaptureText` / `Process.RunAndCaptureTextAsync`
Use when starting a process, capturing both streams as text, and waiting for its exit status is the complete operation. The method drains both streams concurrently before completing, avoiding the pipe-buffer deadlocks described above. On timeout or cancellation, the process is killed.

When using the `ProcessStartInfo` overloads, the caller is responsible for setting `RedirectStandardOutput = true` and `RedirectStandardError = true` on `ProcessStartInfo` (because BCL APIs cannot modify the input arguments they were given).
```csharp
public static ProcessTextOutput RunAndCaptureText(string fileName, IEnumerable<string>? arguments = null, TimeSpan? timeout = null)
public static Task<ProcessTextOutput> RunAndCaptureTextAsync(string fileName, IEnumerable<string>? arguments = null, CancellationToken cancellationToken = default)
public static ProcessTextOutput RunAndCaptureText(ProcessStartInfo startInfo, TimeSpan? timeout = null)
public static Task<ProcessTextOutput> RunAndCaptureTextAsync(ProcessStartInfo startInfo, CancellationToken cancellationToken = default)
```

##### `Process.StartAndForget`
There is a common misconception that when a process is disposed, it's also being killed. This is not the case, as `Process.Dispose` only releases the resources associated with the process, but does not kill it.

To make it easier to start a process without the need to worry about disposing it, `Process.StartAndForget` was introduced. The method starts an executable, returns its ID, and immediately releases all handle resources associated with it. Standard handles not supplied through `StandardInputHandle`, `StandardOutputHandle`, or `StandardErrorHandle` go to the null device by default.

`StartAndForget` throws `InvalidOperationException` if `UseShellExecute` or any `RedirectStandardInput`, `RedirectStandardOutput`, or `RedirectStandardError` flag is true. Use a capture or streaming API if output needs to be read.
```csharp
public static int StartAndForget(string fileName, IEnumerable<string>? arguments = null)
public static int StartAndForget(ProcessStartInfo startInfo)
```

#### Instance Methods
These methods drain stdout and stderr concurrently on an already-started `Process`, avoiding pipe-buffer deadlocks. Choose `ReadAllText[Async]` for decoded text, `ReadAllBytes[Async]` for raw bytes, or `ReadAllLines[Async]` for incremental line processing. Use `WaitForExitStatus` or await `WaitForExitStatusAsync` afterward to wait for termination and obtain the exit status; see [Waiting for Process Exit](#waiting-for-process-exit).

*Note: Calling these methods requires `RedirectStandardOutput = true` and `RedirectStandardError = true` on the `ProcessStartInfo` passed to `Process.Start`.*

```csharp
public (string StandardOutput, string StandardError) ReadAllText(TimeSpan? timeout = null)
public Task<(string StandardOutput, string StandardError)> ReadAllTextAsync(CancellationToken cancellationToken = default)
public (byte[] StandardOutput, byte[] StandardError) ReadAllBytes(TimeSpan? timeout = null)
public Task<(byte[] StandardOutput, byte[] StandardError)> ReadAllBytesAsync(CancellationToken cancellationToken = default)
public IEnumerable<ProcessOutputLine> ReadAllLines(TimeSpan? timeout = null)
public IAsyncEnumerable<ProcessOutputLine> ReadAllLinesAsync(CancellationToken cancellationToken = default)
```

Example of deconstructing the tuple return:
```csharp
(string stdout, string stderr) = await process.ReadAllTextAsync();
```

### ProcessStartInfo Properties

#### `KillOnParentExit`
Ensures that the spawned child process is terminated when the current (parent) process exits (including fatal crash and being force killed). Works across Windows, Linux, and Android.
```csharp
public bool KillOnParentExit { get; set; }
```

In cross-platform code, guard the property assignment with a supported-platform check, not only the assigned value. If automatic teardown is required, fail explicitly on an unsupported platform instead of starting an unprotected child process.
`KillOnParentExit` requires `UseShellExecute = false`.

#### `InheritedHandles`
Provides precise control over which handles (file descriptors) are inherited by the child process, preventing accidental resource leaks.
- Standard handles (`stdin`, `stdout`, `stderr`) are always included (no need to add them to the list).
- Setting the list to an empty list means only standard handles get inherited.
- Only `SafeFileHandle` and `SafePipeHandle` instances are allowed as of today.
- On Windows, explicit lists use a shared reader lock, allowing concurrent starts instead of serializing them under an exclusive writer lock.
- Concurrent process starts must not pass the same handle in `InheritedHandles`: the runtime temporarily changes its inheritance flags. Serialize starts that share a handle, or use separate handles for each concurrent start.
- Do not enable inheritance on the handles before passing them; other process-start APIs could then inherit them unintentionally.
- On Unix systems without native handle-inheritance control, a non-null list causes process creation to throw `PlatformNotSupportedException`.
- A non-null list requires `UseShellExecute = false` and cannot be combined with a non-empty `UserName`.
```csharp
public IList<SafeHandle>? InheritedHandles { get; set; }
```

See [Controlling Handle Inheritance](#controlling-handle-inheritance) for sibling-inheritance races, the difference between `null` and an empty list, and why restricting inheritance does not prevent grandchild-held standard handles from delaying EOF.

#### `StartDetached`
Starts the process detached from the parent's terminal or job session, ensuring it survives the parent's exit.
```csharp
public bool StartDetached { get; set; }
```

`StartDetached` requires `UseShellExecute = false`.

---

## Examples

### 1. One-Line Run and Capture Output

Run a process and safely read all output text without stream deadlock risks. For simple CLI operations that don't need cancellation or async scalability, prefer the synchronous overload:

```csharp
using System;
using System.Diagnostics;

// Run 'git status' and capture output
ProcessTextOutput result = Process.RunAndCaptureText("git", ["status"]);

if (result.ExitStatus.ExitCode == 0)
{
    Console.WriteLine($"Git Output: {result.StandardOutput}");
}
else
{
    Console.WriteLine($"Failed with exit code: {result.ExitStatus.ExitCode}");
    Console.WriteLine($"Error: {result.StandardError}");
}
```

### 2. Auto-Killing Child Processes on Parent Exit

On Windows, Linux, and Android, ensure a long-running background worker process is killed when the main application terminates. On other platforms, this example stops before starting the child process:

```csharp
using System;
using System.Diagnostics;

ProcessStartInfo startInfo = new("dotnet", ["run", "--project", "BackgroundWorker.csproj"]);

if (OperatingSystem.IsWindows() || OperatingSystem.IsLinux() || OperatingSystem.IsAndroid())
{
    startInfo.KillOnParentExit = true;
}
else
{
    throw new PlatformNotSupportedException("This example requires Windows, Linux, or Android.");
}

using Process process = Process.Start(startInfo)!;
// The background worker is now tied to this process's lifecycle
```

### 3. Read All Lines From Output

Start a process and read its output lines safely:

```csharp
using System;
using System.Diagnostics;
using System.Threading.Tasks;

ProcessStartInfo startInfo = new("ping", ["127.0.0.1"])
{
    RedirectStandardOutput = true,
    RedirectStandardError = true
};

using Process process = Process.Start(startInfo)!;
// Read all output lines safely and asynchronously
await foreach (ProcessOutputLine line in process.ReadAllLinesAsync())
{
    string prefix = line.StandardError ? "[Err]" : "[Out]";
    Console.WriteLine($"{prefix} > {line.Content}");
}
ProcessExitStatus status = await process.WaitForExitStatusAsync();
```

### 4. Read Entire Output Deadlock-Free (Tuple Return)

When managing a `Process` instance yourself, read both stdout and stderr into a deconstructed tuple, then wait for process exit. If starting, capturing text, and waiting is all you need, prefer `Process.RunAndCaptureText[Async]`, which also accepts custom `ProcessStartInfo`.

```csharp
using System;
using System.Diagnostics;
using System.Threading.Tasks;

ProcessStartInfo startInfo = new("git", ["diff"])
{
    RedirectStandardOutput = true,
    RedirectStandardError = true
};

using Process process = Process.Start(startInfo)!;
(string stdout, string stderr) = await process.ReadAllTextAsync();
ProcessExitStatus status = await process.WaitForExitStatusAsync();

Console.WriteLine($"Diff output: {stdout}");
Console.Error.WriteLine(stderr);
```

### 5. Start and Forget (Fire & Forget)

Launch a helper executable without holding onto system handle structures:

```csharp
using System;
using System.Diagnostics;

// Fire and forget, getting back only the process ID
int pid = Process.StartAndForget("notepad.exe");
Console.WriteLine($"Notepad started with PID: {pid}");
```
