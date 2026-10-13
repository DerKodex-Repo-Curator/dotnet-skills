---
name: eliminate-redundant-restores
description: "Remove repeated NuGet restores from multi-step .NET builds safely. USE FOR: build/test/publish steps that each restore, Directory.Build.rsp with -restore, where --no-restore or -restore:false is safe, NETSDK1004/NETSDK1047. DO NOT USE FOR: slow first restore, only one restore, version or lock errors."
license: MIT
---

# Eliminate Redundant Restores

A no-op restore still evaluates every project and checks restore state, so each extra restore in a multi-step build costs seconds to tens of seconds in a large repo. The cure is easy to get wrong: `--no-restore` on a step that no earlier restore actually covered turns a slow build into a failed one. This skill finds which invocations restore, proves which are already covered, and changes only those.

## When to Use

- A pipeline or script runs several `dotnet`/`msbuild` steps and the log shows restore output in more than one of them
- A restore step exists, yet later steps still print `Determining projects to restore...`
- A `Directory.Build.rsp` or `MSBuild.rsp` contains `-restore`
- Someone asks where `--no-restore` or `-restore:false` can safely be added
- `NETSDK1004` or `NETSDK1047` appeared after `--no-restore` was added

## When Not to Use

- The first restore is slow (feed latency, missing package cache): that is caching, not redundancy
- There is only one restore across all invocations: nothing to deduplicate
- Restore errors about versions, lock files or auditing: fix the restore itself
- Unranked "why is my build slow": start with `build-perf-baseline`, then `build-perf-diagnostics`

## Inputs

Discover these in the working directory; do not ask the user for paths.

- CI definitions (`*.yml`, `.github/workflows`, `azure-pipelines*.yml`, `Jenkinsfile`) and build scripts (`build.cmd`, `build.sh`, `build.ps1`, `eng/`)
- `Directory.Build.rsp`, `MSBuild.rsp`, `Directory.Build.props`/`.targets`, `NuGet.config`
- Restore evidence if present: step logs or a binlog

## Workflow

### Step 1: Confirm restore is repeated and worth removing

- Log evidence: `Determining projects to restore...` or `All projects are up-to-date for restore.` once per invocation.
- Binlog evidence: restore evaluations carry the global properties `MSBuildRestoreSessionId` and `ExcludeRestorePackageImports=true`; the Target Performance Summary lists `Restore` with N calls.
- Quantify each no-op restore. If there is one restore, or the repeated ones total about a second, report "nothing worth removing" and stop without editing.

### Step 2: Inventory every invocation in execution order

| Invocation | Restores? |
|---|---|
| `dotnet build`, `run`, `test`, `publish`, `pack` | Yes, unless `--no-restore` |
| `dotnet test`, `publish`, `pack` with `--no-build` | No implicit restore, but an rsp `-restore` can still enable restore; pass `--no-restore` explicitly to override it |
| `dotnet msbuild`, `msbuild.exe`, CI MSBuild tasks | Only with `-restore` or `-t:Restore`, **or when `Directory.Build.rsp`/`MSBuild.rsp` contains `-restore`**: every invocation under that directory inherits it, even steps that look build-only |
| `dotnet restore`, `nuget restore`, `msbuild -t:Restore` | Explicit restore |
| CI build task with a "restore packages" option | Check its default |

An explicit `--no-restore` overrides an rsp `-restore` for `dotnet build` and `dotnet test`. Do not rely on `--no-build` alone when an rsp enables restore. `dotnet msbuild` and `msbuild.exe` need an explicit `-restore:false`.

### Step 3: Decide per invocation

Keep the first restore. Skip a later one only when all three checks pass:

| Check | If it fails |
|---|---|
| **Same project set.** A restore covers its entry project or solution and that project's ProjectReference closure. It does not cover a project in another solution or `.slnf`, a tool built from another path, or a project an orchestrating `.proj` builds through the `MSBuild` task (`-restore` on the orchestrator leaves those unrestored and they fail with NETSDK1004) | Keep restore on that step, or add the project to the explicit restore step |
| **Same restore inputs.** `-r`/`--runtime`, `TargetFramework(s)`, `RuntimeIdentifier(s)`, `SelfContained`, `PublishAot`, package sources (`--source`, `--configfile`, `RestoreSources`), `RestorePackagesPath`, and any `-p:` value a PackageReference condition reads. A mismatch fails with NETSDK1047 (`Assets file ... doesn't have a target for 'net10.0/linux-x64'`) | Give the explicit restore the same inputs (for example `dotnet restore -r linux-x64`) or keep restore on the later step. `Configuration` alone rarely changes restore; check props for conditions on it |
| **Nothing changes packages in between.** A step that generates or edits a project, runs `dotnet add package`, rewrites `Directory.Packages.props` or `NuGet.config` invalidates the earlier restore | Move the explicit restore after that step |

### Step 4: Apply the smallest change

- One explicit restore carrying every restore input, then `--no-restore`, `-restore:false` or `--no-build` on later steps.
- Several `dotnet test` steps over one solution: build once, then `dotnet test --no-build` on each. Add `--no-restore` explicitly if an rsp enables restore. `--no-restore` alone still rebuilds.
- Scope the change to CI and scripts. Leave `-restore` in `Directory.Build.rsp`, because developers rely on it; pass `-restore:false` per invocation instead.
- Never remove the first restore. If it is a `--locked-mode` gate, keep it there.
- Do not bundle static graph restore or package caching into this change.

```bash
# One restore carries every input later steps use
dotnet restore MyApp.slnx -r linux-x64
dotnet build MyApp.slnx -c Release --no-restore
dotnet test MyApp.slnx -c Release --no-build
dotnet publish src/Api -c Release -r linux-x64 --no-restore
# msbuild invocations need the explicit switch if an rsp file adds -restore
dotnet msbuild Packaging.proj -t:Pack -restore:false
```

### Step 5: Validate and report truthfully

1. Run the changed pipeline or script end to end from a clean state (no `obj/`, no `project.assets.json`). A project that was never restored only fails there. If you cannot run it, say "not validated" and list the steps and projects to confirm.
2. Success evidence: every later step passed; `Restore` calls (binlog) or `Determining projects to restore` lines fell from N to the expected count; for a time claim, before/after medians of at least three runs.
3. Report a restore failure (NETSDK1004, NETSDK1047) separately from a build failure, and never claim success after either.

## Output

A findings table, then one verdict line.

| Step | Restores today (explicit/implicit/rsp) | Covered by an earlier restore? | Change | Evidence |
|---|---|---|---|---|

`Verdict: removed N of M restores, first restore kept` or `Verdict: no redundant restores`.

## Common Pitfalls

| Pitfall | Solution |
|---|---|
| Adding `--no-restore` to every command | Apply the Step 3 checks per step |
| Missing that an rsp adds `-restore` to "build-only" msbuild steps | Search for `*.rsp` before concluding a step does not restore |
| Deleting `-restore` from `Directory.Build.rsp` | Override per CI invocation; the rsp serves developers |
| Orchestrator `.proj` restored, children not | Restore each child explicitly, or keep restore on the step that builds it |
| Publishing with `-r` after a restore without `-r` | Restore with the same runtime identifier |
| A generator step edits PackageReferences after restore | Restore after the generator |
