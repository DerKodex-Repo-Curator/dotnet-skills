#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

const [contract, rootArg] = process.argv.slice(2);
const contracts = new Set(["razor-static", "blazor-static"]);
if (!contracts.has(contract) || !rootArg) {
  console.error("Usage: node validate-restraint.mjs <razor-static|blazor-static> <root>");
  process.exit(2);
}

const root = path.resolve(rootArg);
const files = [];

function visit(directory) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    if (entry.isDirectory() && ["bin", "obj", ".git"].includes(entry.name)) continue;
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) visit(full);
    else files.push({
      rel: path.relative(root, full).replaceAll("\\", "/"),
      bytes: fs.readFileSync(full),
      text: fs.readFileSync(full, "utf8"),
    });
  }
}

visit(root);
const failures = [];
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
    /(?:^|\/)Pages\/(?:Products?|Categor(?:y|ies))(?:\/|$)/i,
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
  forbidPath(/(?:^|\/)Components\/Pages\/(?:Employees?|Departments?)(?:[./]|\/|$)/i, "Employee or Department CRUD components are forbidden");
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
  const programFile = files.find((file) => file.rel === "Program.cs");
  const programHash = programFile
    ? crypto.createHash("sha256").update(programFile.bytes).digest("hex")
    : "";
  if (programHash !== "8ecc472ed8e07e0c2e7a7773b3d06e475577ed722673500d7db9837683dfdb9a") {
    failures.push("Program.cs must remain byte-for-byte identical to the staged interactive host");
  }
  forbidText(program, /AddInteractiveWebAssemblyComponents|AddInteractiveWebAssemblyRenderMode|AddInteractiveAutoRenderMode/i, "new render modes are forbidden");
}

if (failures.length > 0) {
  for (const failure of failures) console.error(failure);
  process.exit(1);
}

console.log(`PASS ${contract}: static-page restraint`);
