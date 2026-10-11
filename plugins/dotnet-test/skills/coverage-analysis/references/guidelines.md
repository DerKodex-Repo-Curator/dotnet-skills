# Explicit Risk/CRAP Analysis

Read this file only when the user explicitly asks for project-wide risk
hotspots, CRAP scores, complexity-weighted priorities, or refactoring safety.
Do not read it for supplied-excerpt interpretation, plateau diagnosis, or target
arithmetic alone.

## Compute the risk data

Resolve the scripts relative to this skill's `SKILL.md`:

```powershell
& "<skill-directory>/scripts/Compute-CrapScores.ps1" `
    -CoberturaPath @(<all Cobertura paths>) `
    -CrapThreshold <crap_threshold> `
    -TopN <top_n>

& "<skill-directory>/scripts/Extract-MethodCoverage.ps1" `
    -CoberturaPath @(<all Cobertura paths>) `
    -CoverageThreshold <line_threshold> `
    -BranchThreshold <branch_threshold> `
    -Filter below-threshold
```

`Compute-CrapScores.ps1` emits aggregate line/branch coverage, method counts,
flagged counts, and sorted hotspots. `Extract-MethodCoverage.ps1` emits every
representable below-threshold method. Use both for explicit project-wide risk
work. The commands above retain legacy output; do not switch consumers to a
new schema implicitly.

### Uncertainty and opt-in JSON v1

When the caller explicitly selects structured uncertainty-aware output, append
`-OutputFormat json-v1` to either command. Consume one stdout JSON object with
`schema_version: "1"`, `tool`, `tool_version`, `status`, `scope`,
`inputs` (paths and SHA-256 hashes), `diagnostics` and `data`. Validate the
version/status before interpreting `data.summary` or `data.methods`.
Do not reuse an unrelated eval recorder's JSON as this shipping contract.

`data.summary` separates `overall_line_coverage` /
`overall_line_coverage_status`, `overall_branch_coverage` /
`overall_branch_coverage_status`, `method_line_coverage` and `total_methods`.
Its `coverage_scope` is `class-lines`, `report` or `method-lines-only`; the last
does not establish whole-project coverage. CRAP adds `flagged_methods` and
`unknown_crap_methods`. `data.hotspots` contains only known-score methods;
`data.methods` retains unknown rows with `CrapScore: null`.

- Exit `0` / `complete` means the requested analysis completed, not that every
  metric is exact. Exit `3` / `partial` requires surfacing the diagnostics and
  missing evidence; exit `2` / `error` is an input, dependency or tool failure.
- Legacy output is retained only when it can represent the result safely.
  Unrepresentable unknown/incomplete results fail explicitly with exit `3`,
  not a success-shaped payload. Do not silently retry another format and report
  success, or treat an empty payload as no risky methods.
- Methods retain their existing PascalCase metrics and add
  `LineCoverageStatus`, `BranchCoverageStatus` and `ComplexityStatus`:
  `exact`, `bounded`, `unknown` or `not-applicable`. Unavailable numeric values
  and `CrapScore` are `null`, never invented zero coverage or complexity one.
  A known method with no branches may have total branches zero and
  `not-applicable`; this differs from missing branch evidence.
- Bounded branch results expose `CoveredBranchesLower`/`CoveredBranchesUpper`
  and `BranchCoverageLower`/`BranchCoverageUpper`. Do not report the upper
  bound as measured coverage. Without stable branch identities, counter totals
  from separate runs do not prove which branches overlap.
- Keep aggregate and method denominators separate. Fully covered reported
  methods do not prove the entire module is fully covered. Respect input
  identities, scope and explicit truncation metadata before comparing reports.
- `scope.source_revision` is null: input hashes bind the supplied report bytes,
  not a verified source revision or proof that those reports describe the
  current checkout. Establish that association separately before reusing results.

Preserve analyzer exit codes through launchers. When invoking these scripts
through `pwsh -Command`, explicitly propagate `$LASTEXITCODE` (for example,
append `; exit $LASTEXITCODE`) rather than allowing the outer host to collapse
all nonzero outcomes to exit `1`.

Rank only supported numeric risk evidence. Keep unknown/partial methods visible
as unresolved work, not low-risk results; exclude them from exact numeric
denominators and state the excluded count. A threshold inside a reported range
is unresolved. Do not cast null to zero or claim a threshold passed when
required evidence is missing.

CRAP is:

`CRAP(m) = complexity² × (1 − lineCoverage)³ + complexity`

A method at 100% coverage therefore has CRAP equal to its complexity. Use 30 as
the default flagged threshold; treat 15–30 as moderate rather than catastrophic.

## Scale the output

- Show the top 3 actual risk hotspots by default.
- Exclude fully covered low-risk methods from the hotspot table.
- State how many additional methods exceeded the threshold instead of listing
  them all.
- Honor a user-supplied count up to 10. Exceed 10 only when explicitly requested.
- For five or fewer below-threshold members, name all of them. For larger sets,
  show the top hotspots and summarize the remaining count and range.
- Give 1–3 recommendations ordered by expected risk reduction.

## Prioritize

- **HIGH** — both CRAP and coverage exceed their risk thresholds.
- **MED** — either CRAP or coverage exceeds its threshold.
- **LOW** — below coverage threshold but complexity is at most 2.

Prefer complex uncovered critical paths (authentication, payment, data access,
error handling). Deprioritize trivial getters, generated code, migrations, and
configuration glue.

Do not project a CRAP reduction from an arbitrary target without showing the
assumption. Recalculate with the stated projected method coverage.

Do not generate tests during analysis. Recommend focused test cases;
implementation is a separate follow-up.

## Style

- Lead with the risk verdict, not setup narration.
- Quantify recommendations only from actual line/coverage evidence.
- Use one compact hotspot table. Do not append the full report template unless
  the user requested a report.
