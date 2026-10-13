#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";

const [contract, rootArg] = process.argv.slice(2);
const contracts = new Set(["razor-static", "blazor-static"]);
if (!contracts.has(contract) || !rootArg) {
  console.error("Usage: node validate-restraint.mjs <razor-static|blazor-static> <root>");
  process.exit(2);
}

const root = path.resolve(rootArg);
const files = [];
const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const manifestPath = path.join(scriptDirectory, "static-fixture-baseline.json");
const ignoredDirectories = new Set(["bin", "obj", ".git", "node_modules"]);

let manifest;
try {
  manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
} catch (error) {
  console.error(`Could not load static fixture baseline ${manifestPath}: ${error.message}`);
  process.exit(2);
}

const baseline = manifest?.contracts?.[contract];
if (manifest?.schemaVersion !== 1 || manifest?.algorithm !== "sha256" || !Array.isArray(baseline?.files)) {
  console.error(`Static fixture baseline does not define ${contract} with schema version 1 and SHA-256 files`);
  process.exit(2);
}

function visit(directory) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    if (entry.isDirectory() && ignoredDirectories.has(entry.name)) continue;
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) visit(full);
    else if (entry.isFile()) files.push({
      rel: path.relative(root, full).replaceAll("\\", "/"),
      bytes: fs.readFileSync(full),
      text: fs.readFileSync(full, "utf8"),
    });
  }
}

visit(root);
const failures = [];
const scopeRules = {
  "razor-static": {
    mutableBaseline: new Set(["Pages/Shared/_Layout.cshtml"]),
    allowedNew: new Set(["Pages/Terms.cshtml", "Pages/Terms.cshtml.cs"]),
  },
  "blazor-static": {
    mutableBaseline: new Set(["Components/Layout/NavMenu.razor"]),
    allowedNew: new Set([
      "Components/Pages/Reports.razor",
      "Components/Pages/Reports.razor.css",
    ]),
  },
};

function sha256(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

function validateFileScope() {
  const rules = scopeRules[contract];
  const actualByPath = new Map(files.map((file) => [file.rel, file]));
  const baselineByPath = new Map();

  for (const entry of baseline.files) {
    if (typeof entry?.path !== "string" || !/^[a-f0-9]{64}$/.test(entry?.sha256 ?? "")) {
      failures.push("static fixture baseline contains an invalid file entry");
      continue;
    }
    if (baselineByPath.has(entry.path)) {
      failures.push(`static fixture baseline contains a duplicate path: ${entry.path}`);
      continue;
    }
    baselineByPath.set(entry.path, entry.sha256);
    const actual = actualByPath.get(entry.path);
    if (!actual) {
      failures.push(`baseline file is missing (deleted or renamed): ${entry.path}`);
    } else if (!rules.mutableBaseline.has(entry.path) && sha256(actual.bytes) !== entry.sha256) {
      failures.push(`baseline file changed outside allowed navigation: ${entry.path}`);
    }
  }

  for (const file of files) {
    if (!baselineByPath.has(file.rel) && !rules.allowedNew.has(file.rel)) {
      failures.push(`unrelated added file is forbidden: ${file.rel}`);
    }
  }
}

validateFileScope();
const joined = files.map((file) => file.text).join("\n");
const project = files.find((file) => file.rel.endsWith(".csproj"))?.text ?? "";
const program = files.find((file) => file.rel === "Program.cs")?.text ?? "";

function forbidPath(pattern, description) {
  if (files.some((file) => pattern.test(file.rel))) failures.push(description);
}

function forbidText(text, pattern, description) {
  if (pattern.test(text)) failures.push(description);
}

function requireArtifact(pattern, description) {
  const file = files.find((candidate) => pattern.test(candidate.rel));
  if (!file) failures.push(`${description} is missing`);
  else if (!file.text.trim()) failures.push(`${description} is empty`);
  return file?.text ?? "";
}

function activeRazor(text) {
  return text
    .replace(/@\*[\s\S]*?\*@/g, "")
    .replace(/<!--[\s\S]*?-->/g, "");
}

function hasEntityPrefixedRazorPage(relativePath) {
  const match = relativePath.match(/(?:^|\/)Pages\/(?:.*\/)?([^/]+)\.cshtml(?:\.cs)?$/);
  if (!match) return false;
  const prefix = match[1].match(/^(Products?|Categor(?:y|ies))/i)?.[0];
  if (!prefix) return false;
  const suffix = match[1].slice(prefix.length);
  return suffix === "" ||
    /^(?:Index|Details|Create|Edit|Delete)$/i.test(suffix) ||
    /^[A-Z0-9_.-]/.test(suffix);
}

function hasEntityPrefixedBlazorComponent(relativePath) {
  const match = relativePath.match(/(?:^|\/)Components\/Pages\/(?:.*\/)?([^/]+)\.razor(?:\.cs)?$/);
  if (!match) return false;
  const prefix = match[1].match(/^(Employees?|Departments?)/i)?.[0];
  if (!prefix) return false;
  const suffix = match[1].slice(prefix.length);
  return suffix === "" ||
    /^(?:Index|Details|Create|Edit|Delete)$/i.test(suffix) ||
    /^[A-Z0-9_.-]/.test(suffix);
}

forbidPath(/(?:^|\/)Migrations(?:\/|$)/i, "migrations are forbidden");
forbidPath(/(?:^|\/)[^/]*DbContext[^/]*\.cs$/i, "DbContext source is forbidden");
forbidPath(/(?:^|\/)(?:Controllers|Endpoints)(?:\/|$)/i, "controller or endpoint scaffolding is forbidden");
forbidPath(/\.http$/i, "HTTP workflow files are forbidden");
forbidText(project, /EntityFrameworkCore|Microsoft\.Data\.Sqlite/i, "data-access packages are forbidden");
forbidText(joined, /\b(?:DbContext|DbSet)\b|AddDbContext(?:Factory)?\s*<|UseSqlite\s*\(|EnsureCreated\s*\(/, "data-access code is forbidden");

if (contract === "razor-static") {
  forbidPath(
    /(?:^|\/)Pages\/(?:[^/]+\/)*(?:Products?|Categor(?:y|ies))(?:\/|$)/i,
    "Product or Category CRUD pages are forbidden",
  );
  if (files.some((file) => hasEntityPrefixedRazorPage(file.rel))) {
    failures.push("Product or Category entity-prefixed Razor Page files are forbidden");
  }
  const terms = activeRazor(requireArtifact(
    /(?:^|\/)Pages\/Terms\.cshtml$/,
    "Pages/Terms.cshtml",
  ));
  const termsModel = requireArtifact(
    /(?:^|\/)Pages\/Terms\.cshtml\.cs$/,
    "Pages/Terms.cshtml.cs",
  );
  if (!/^\s*@page(?:\s|$)/m.test(terms)) failures.push("Terms.cshtml must contain an active @page directive");
  if (!/^\s*@model\s+(?:global::)?MyApp\.Pages\.TermsModel\s*$/m.test(terms)) {
    failures.push("Terms.cshtml must reference MyApp.Pages.TermsModel");
  }
  if (!/\bclass\s+TermsModel\b[^:{]*:\s*(?:(?:global::)?Microsoft\.AspNetCore\.Mvc\.RazorPages\.)?PageModel\b/.test(termsModel)) {
    failures.push("Terms.cshtml.cs must define TermsModel as a PageModel");
  }
} else {
  forbidPath(
    /(?:^|\/)Components\/Pages\/(?:[^/]+\/)*(?:Employees?|Departments?)(?:\/|$)/i,
    "Employee or Department CRUD components are forbidden",
  );
  if (files.some((file) => hasEntityPrefixedBlazorComponent(file.rel))) {
    failures.push("Employee or Department entity-prefixed Blazor component files are forbidden");
  }
  const reports = activeRazor(requireArtifact(
    /(?:^|\/)Components\/Pages\/Reports\.razor$/,
    "Components/Pages/Reports.razor",
  ));
  const navigation = activeRazor(requireArtifact(
    /(?:^|\/)Components\/Layout\/NavMenu\.razor$/,
    "Components/Layout/NavMenu.razor",
  ));
  if (!/^\s*@page\s+["']\/reports["']\s*$/m.test(reports)) {
    failures.push('Reports.razor must contain an active @page "/reports" directive');
  }
  if (!/<PageTitle>\s*Reports\s*<\/PageTitle>/i.test(reports)) {
    failures.push("Reports.razor must define a Reports PageTitle");
  }
  if (!/<h1\b[^>]*>\s*Reports\s*<\/h1>/i.test(reports)) {
    failures.push("Reports.razor must contain a Reports heading");
  }
  const reportsContent = reports
    .replace(/^\s*@page[^\r\n]*$/gm, "")
    .replace(/<PageTitle>[\s\S]*?<\/PageTitle>/gi, "")
    .replace(/<h1\b[^>]*>[\s\S]*?<\/h1>/gi, "")
    .trim();
  if (!reportsContent) failures.push("Reports.razor must contain page content");
  if (!/<NavLink\b[^>]*\bhref\s*=\s*["']\/?reports["'][^>]*>/i.test(navigation)) {
    failures.push("NavMenu.razor must link to /reports");
  }
  forbidText(reports, /@rendermode\b/i, "the static Reports component must not add a render mode");
  forbidText(program, /AddInteractiveWebAssemblyComponents|AddInteractiveWebAssemblyRenderMode|AddInteractiveAutoRenderMode/i, "new render modes are forbidden");
}

if (failures.length > 0) {
  for (const failure of failures) console.error(failure);
  process.exit(1);
}

console.log(`PASS ${contract}: static-page restraint`);
