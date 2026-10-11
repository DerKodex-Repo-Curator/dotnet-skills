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
below-threshold method represented by the report. Use both for explicit
project-wide risk work. Use only the parameters shown above; the bundled
scripts expose legacy output, not a versioned uncertainty envelope.

### Evidence limits of the bundled scripts

Check the source report as well as the command's result before ranking risk:

- The scripts substitute complexity one when the report omits complexity and
  can emit zero coverage when method line data is absent. Those fallback
  numbers are not measurements. Report missing inputs as unknown rather than
  presenting the computed score as evidence.
- `Extract-MethodCoverage.ps1` adds covered branch counters from overlapping
  reports, capped at the total. Without stable branch identities, that does
  not establish which branches were covered; do not claim exact merged branch
  coverage or threshold clearance from those counters.
- Aggregate and method denominators are different. Fully covered reported
  methods do not prove that the entire module is covered. Verify overlapping
  report inputs and scope before interpreting aggregate totals.
- The legacy output does not attest completeness or source revision. Retain
  the command, source report paths and known revision association separately.
  A successful exit or an empty result is not proof that no risky code exists.

Keep unsupported or missing metrics visible as unresolved evidence, not
low-risk results. Do not invent precision, silently switch output formats or
reuse an unrelated eval recorder's schema. Versioned output and stronger
uncertainty handling must be documented together with their executable support.

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
