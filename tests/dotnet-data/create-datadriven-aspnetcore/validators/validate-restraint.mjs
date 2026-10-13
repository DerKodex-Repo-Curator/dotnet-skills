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
} else {
  forbidPath(/(?:^|\/)Components\/Pages\/(?:Employees?|Departments?)(?:[./]|\/|$)/i, "Employee or Department CRUD components are forbidden");
  const reports = files.find((file) => /(?:^|\/)Components\/Pages\/Reports\.razor$/i.test(file.rel))?.text ?? "";
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
