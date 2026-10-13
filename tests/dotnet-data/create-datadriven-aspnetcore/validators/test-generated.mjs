#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const scenarioDirectory = path.dirname(scriptDirectory);
const validator = path.join(scriptDirectory, "validate-generated.mjs");
const restraintValidator = path.join(scriptDirectory, "validate-restraint.mjs");
const preferredBase = "Q:\\copilot\\26bbc64f-ad82-47bf-a29d-42ec7aead779\\validator-tests";
const fallbackBase = path.join(os.tmpdir(), "copilot-26bbc64f-ad82-47bf-a29d-42ec7aead779-validator-tests");

function usableBase(candidate) {
  try {
    fs.mkdirSync(candidate, { recursive: true });
    fs.accessSync(candidate, fs.constants.W_OK);
    return true;
  } catch {
    return false;
  }
}

const base = process.platform === "win32" && usableBase(preferredBase) ? preferredBase : fallbackBase;
fs.mkdirSync(base, { recursive: true });
const runRoot = path.join(base, `run-${Date.now()}-${process.pid}`);
fs.mkdirSync(runRoot, { recursive: true });

const cases = [
  { contract: "razor-pages", fixture: "razor-pages-crud", patch: "add-razor-pages-crud.patch" },
  { contract: "todo-api", fixture: "minimal-api", patch: "add-todo-minimal-api-crud.patch" },
  { contract: "mvc", fixture: "mvc-existing-dbcontext", patch: "add-mvc-book-author-crud.patch" },
  { contract: "parent-api", fixture: "minimal-api-parent-fk", patch: "add-parent-child-minimal-api-crud.patch" },
  { contract: "blazor", fixture: "blazor-crud", patch: "add-blazor-employee-department-crud.patch" },
];
const restraintCases = [
  { contract: "razor-static", fixture: "razor-pages-crud", patch: "add-terms-page.patch" },
  { contract: "blazor-static", fixture: "blazor-crud", patch: "add-reports-component.patch" },
];

const failures = [];
const goldenRoots = new Map();

function copySource(source, destination) {
  fs.cpSync(source, destination, {
    recursive: true,
    filter(current) {
      const relative = path.relative(source, current);
      return !relative.split(path.sep).some((segment) => segment === "bin" || segment === "obj" || segment === ".git");
    },
  });
}

function run(command, args, options = {}) {
  return spawnSync(command, args, {
    encoding: "utf8",
    windowsHide: true,
    ...options,
  });
}

function validatorResult(contract, root) {
  return run(process.execPath, [validator, contract, root]);
}

function restraintValidatorResult(contract, root) {
  return run(process.execPath, [restraintValidator, contract, root]);
}

function reportPass(contract, name) {
  console.log(`PASS ${contract}: ${name}`);
}

function reportUnexpected(contract, name, result, expectation) {
  const details = [result.stdout, result.stderr].filter(Boolean).join("\n").trim();
  failures.push(`${contract}: ${name} (expected ${expectation}, exit ${result.status})${details ? `\n${details}` : ""}`);
  console.error(`FAIL ${contract}: ${name}`);
}

function expectPass(contract, root, name) {
  const result = validatorResult(contract, root);
  if (result.status === 0) reportPass(contract, name);
  else reportUnexpected(contract, name, result, "pass");
}

function expectFailure(contract, root, name) {
  const result = validatorResult(contract, root);
  if (result.status !== 0) reportPass(contract, `${name} rejected`);
  else reportUnexpected(contract, name, result, "failure");
}

function materializeGolden(item) {
  const work = path.join(runRoot, `golden-${item.contract}`);
  const fixtureRoot = path.join(work, "fixtures", item.fixture);
  fs.mkdirSync(path.dirname(fixtureRoot), { recursive: true });
  copySource(path.join(scenarioDirectory, "fixtures", item.fixture), fixtureRoot);
  const patch = path.join(scenarioDirectory, "references", item.patch);
  const applied = run("git", ["apply", "--whitespace=nowarn", patch], { cwd: work });
  if (applied.status !== 0) {
    throw new Error(`Could not apply ${item.patch}:\n${applied.stderr || applied.stdout}`);
  }
  goldenRoots.set(item.contract, fixtureRoot);
  expectPass(item.contract, fixtureRoot, "golden patch");
}

function materializeRestraintGolden(item) {
  const work = path.join(runRoot, `golden-${item.contract}`);
  const fixtureRoot = path.join(work, "fixtures", item.fixture);
  fs.mkdirSync(path.dirname(fixtureRoot), { recursive: true });
  copySource(path.join(scenarioDirectory, "fixtures", item.fixture), fixtureRoot);
  const patch = path.join(scenarioDirectory, "references", item.patch);
  const applied = run("git", ["apply", "--whitespace=nowarn", patch], { cwd: work });
  if (applied.status !== 0) throw new Error(`Could not apply ${item.patch}:\n${applied.stderr || applied.stdout}`);
  goldenRoots.set(item.contract, fixtureRoot);
  const result = restraintValidatorResult(item.contract, fixtureRoot);
  if (result.status === 0) reportPass(item.contract, "golden patch");
  else reportUnexpected(item.contract, "golden patch", result, "pass");
}

function restraintMutation(contract, name, mutate) {
  try {
    const root = cloneForMutation(contract, name);
    mutate(root);
    const result = restraintValidatorResult(contract, root);
    if (result.status !== 0) reportPass(contract, `${name} rejected`);
    else reportUnexpected(contract, name, result, "failure");
  } catch (error) {
    failures.push(`${contract}: ${name} mutation failed: ${error.message}`);
    console.error(`FAIL ${contract}: ${name} mutation setup`);
  }
}

function restraintMutations() {
  restraintMutation("razor-static", "non-EF AppDbContext over-scaffolding", (root) =>
    write(root, "Data/AppDbContext.cs", "namespace MyApp.Data; public sealed class AppDbContext { }\n"));
  restraintMutation("razor-static", "compilable Product and Category CRUD pages", (root) => {
    write(root, "Pages/Products/Index.cshtml", '@page\n<h1>Products</h1>\n');
    write(root, "Pages/Categories/Index.cshtml", '@page\n<h1>Categories</h1>\n');
  });
  restraintMutation("blazor-static", "non-EF AppDbContext over-scaffolding", (root) =>
    write(root, "Data/AppDbContext.cs", "namespace BlazorApp.Data; public sealed class AppDbContext { }\n"));
  restraintMutation("blazor-static", "compilable Employee and Department CRUD components", (root) => {
    write(root, "Components/Pages/Employees.razor", '@page "/employees"\n<h1>Employees</h1>\n');
    write(root, "Components/Pages/Departments.razor", '@page "/departments"\n<h1>Departments</h1>\n');
  });
}

function cloneForMutation(contract, name, source = goldenRoots.get(contract)) {
  const destination = path.join(runRoot, "mutations", `${contract}-${name.replaceAll(/[^a-z0-9-]+/gi, "-")}`);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  copySource(source, destination);
  return destination;
}

function read(relativeRoot, relativeFile) {
  return fs.readFileSync(path.join(relativeRoot, ...relativeFile.split("/")), "utf8");
}

function write(relativeRoot, relativeFile, text) {
  const target = path.join(relativeRoot, ...relativeFile.split("/"));
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, text);
}

function replace(relativeRoot, relativeFile, search, replacement, count = Infinity) {
  const original = read(relativeRoot, relativeFile);
  let changed = 0;
  let updated;
  if (search instanceof RegExp) {
    updated = original.replace(search, (...args) => {
      if (changed >= count) return args[0];
      changed += 1;
      return typeof replacement === "function" ? replacement(...args) : replacement;
    });
  } else {
    updated = original;
    while (changed < count) {
      const index = updated.indexOf(search);
      if (index < 0) break;
      updated = updated.slice(0, index) + replacement + updated.slice(index + search.length);
      changed += 1;
    }
  }
  if (changed === 0) throw new Error(`Mutation did not match ${relativeFile}: ${search}`);
  write(relativeRoot, relativeFile, updated);
}

function balancedEnd(text, start, open, close) {
  let depth = 0;
  let quote = "";
  let escaped = false;
  for (let index = start; index < text.length; index += 1) {
    const character = text[index];
    if (quote) {
      if (escaped) escaped = false;
      else if (character === "\\") escaped = true;
      else if (character === quote) quote = "";
      continue;
    }
    if (character === '"' || character === "'") {
      quote = character;
      continue;
    }
    if (character === open) depth += 1;
    if (character === close && --depth === 0) return index;
  }
  return -1;
}

function methodRange(text, name) {
  const start = text.indexOf(`private static async Task<IResult> ${name}(`);
  if (start < 0) throw new Error(`Could not find method ${name}`);
  const openParen = text.indexOf("(", start);
  const closeParen = balancedEnd(text, openParen, "(", ")");
  const openBrace = text.indexOf("{", closeParen);
  const closeBrace = balancedEnd(text, openBrace, "{", "}");
  if (closeParen < 0 || openBrace < 0 || closeBrace < 0) {
    throw new Error(`Could not parse method ${name}`);
  }
  return { start, openParen, closeParen, openBrace, closeBrace };
}

function renameProductRequestParameter(root, methodName, parameterName) {
  const relativeFile = "Endpoints/ProductEndpoints.cs";
  const source = read(root, relativeFile);
  const range = methodRange(source, methodName);
  const method = source.slice(range.start, range.closeBrace + 1)
    .replace(/\bProductRequest\s+request\b/, `ProductRequest ${parameterName}`)
    .replace(/\brequest\b/g, parameterName);
  write(
    root,
    relativeFile,
    source.slice(0, range.start) + method + source.slice(range.closeBrace + 1),
  );
}

function inlineProductHandler(root, methodName, mapCall, parameterName) {
  const relativeFile = "Endpoints/ProductEndpoints.cs";
  let source = read(root, relativeFile);
  const range = methodRange(source, methodName);
  const parameters = source.slice(range.openParen + 1, range.closeParen)
    .replace(/\bProductRequest\s+request\b/, `ProductRequest ${parameterName}`);
  const body = source.slice(range.openBrace, range.closeBrace + 1)
    .replace(/\brequest\b/g, parameterName);
  source = source.replace(mapCall, `${mapCall.slice(0, mapCall.lastIndexOf(",") + 1)} async (${parameters}) => ${body})`);
  write(root, relativeFile, source);
}

function mutation(contract, name, mutate) {
  mutationFrom(contract, goldenRoots.get(contract), name, mutate);
}

function mutationFrom(contract, source, name, mutate) {
  try {
    const root = cloneForMutation(contract, name, source);
    mutate(root);
    expectFailure(contract, root, name);
  } catch (error) {
    failures.push(`${contract}: ${name} mutation failed: ${error.message}`);
    console.error(`FAIL ${contract}: ${name} mutation setup`);
  }
}

function testPatchTodoLayout() {
  const root = cloneForMutation("todo-api", "patch-layout");
  replace(root, "Endpoints/TodoEndpoints.cs", "group.MapPut", "group.MapPatch");
  replace(root, "TodoApi.http", "\nPUT ", "\nPATCH ");
  expectPass("todo-api", root, "PATCH update layout");
}

function testTodoUpdatePatterns() {
  for (const operation of ["Update", "Attach"]) {
    const root = cloneForMutation("todo-api", `${operation.toLowerCase()}-submitted-entity-layout`);
    replace(
      root,
      "Endpoints/TodoEndpoints.cs",
      /        item\.Title = request\.Title\.Trim\(\);\s*        item\.IsComplete = request\.IsComplete;/,
      `        var submitted = new TodoItem
        {
            Id = item.Id,
            Title = request.Title.Trim(),
            IsComplete = request.IsComplete
        };
        dbContext.TodoItems.${operation}(submitted);`,
      1,
    );
    expectPass("todo-api", root, `${operation} validated submitted entity`);
  }
}

function razorMutations() {
  const product = "Pages/Products";
  mutation("razor-pages", "remove list flow", (root) =>
    replace(root, `${product}/Index.cshtml.cs`, ".ToListAsync()", ".ToList()"));
  mutation("razor-pages", "remove details flow", (root) =>
    replace(root, `${product}/Details.cshtml.cs`, "SingleOrDefaultAsync", "SingleOrDefault"));
  mutation("razor-pages", "remove create flow", (root) =>
    replace(root, `${product}/Create.cshtml.cs`, "context.Products.Add(Product)", "_ = Product"));
  mutation("razor-pages", "remove edit flow", (root) =>
    replace(root, `${product}/Edit.cshtml.cs`, "EntityState.Modified", "EntityState.Unchanged"));
  mutation("razor-pages", "remove delete flow", (root) =>
    replace(root, `${product}/Delete.cshtml.cs`, "context.Products.Remove(product)", "_ = product"));
  mutation("razor-pages", "remove validation", (root) =>
    replace(root, `${product}/Create.cshtml`, /asp-validation-(?:summary|for)/g, "data-no-validation"));
  mutation("razor-pages", "remove parent selection", (root) => {
    replace(root, `${product}/Create.cshtml`, /Product\.CategoryId/g, "Product.CategoryCode");
    replace(root, `${product}/Edit.cshtml`, /Product\.CategoryId/g, "Product.CategoryCode");
  });
  mutation("razor-pages", "remove navigation", (root) =>
    replace(root, "Pages/Shared/_Layout.cshtml", /Products/g, "Inventory"));
  mutation("razor-pages", "empty Razor pages", (root) => {
    for (const entity of ["Products", "Categories"]) {
      for (const action of ["Index", "Details", "Create", "Edit", "Delete"]) {
        write(root, `Pages/${entity}/${action}.cshtml`,
          `@page\n@model MyApp.Pages.${entity}.${action}Model\n<h1>${action}</h1>\n`);
        write(root, `Pages/${entity}/${action}.cshtml.cs`,
          `using Microsoft.AspNetCore.Mvc.RazorPages;\nnamespace MyApp.Pages.${entity};\npublic class ${action}Model : PageModel { }\n`);
      }
    }
  });
}

function todoMutations() {
  const endpoints = "Endpoints/TodoEndpoints.cs";
  mutation("todo-api", "remove list flow", (root) =>
    replace(root, endpoints, ".ToListAsync()", ".ToList()", 1));
  mutation("todo-api", "remove by-id flow", (root) =>
    replace(root, endpoints, "SingleOrDefaultAsync(item => item.Id == id)", "SingleOrDefault(item => item.Id == id)", 1));
  mutation("todo-api", "remove create flow", (root) =>
    replace(root, endpoints, "dbContext.TodoItems.Add(item)", "_ = item"));
  mutation("todo-api", "remove update flow", (root) => {
    replace(root, endpoints, "item.Title = request.Title.Trim();", "_ = request.Title;", 1);
    replace(root, endpoints, "item.IsComplete = request.IsComplete;", "_ = request.IsComplete;", 1);
  });
  mutation("todo-api", "redirect update assignments to request", (root) => {
    replace(root, endpoints, "item.Title = request.Title.Trim();", "request.Title = request.Title.Trim();", 1);
    replace(root, endpoints, "item.IsComplete = request.IsComplete;", "request.IsComplete = request.IsComplete;", 1);
  });
  mutation("todo-api", "self-assign update target", (root) => {
    replace(root, endpoints, "item.Title = request.Title.Trim();", "item.Title = item.Title;", 1);
    replace(root, endpoints, "item.IsComplete = request.IsComplete;", "item.IsComplete = item.IsComplete;", 1);
  });
  mutation("todo-api", "remove delete flow", (root) =>
    replace(root, endpoints, "dbContext.TodoItems.Remove(item)", "_ = item"));
  mutation("todo-api", "remove validation", (root) =>
    replace(root, endpoints, "string.IsNullOrWhiteSpace(request.Title)", "false"));
  mutation("todo-api", "remove only create validation", (root) =>
    replace(
      root,
      endpoints,
      /(private static async Task<IResult> CreateTodoAsync[\s\S]*?)var errors = Validate\(request\);/,
      (match, prefix) => `${prefix}var errors = new Dictionary<string, string[]>(StringComparer.Ordinal);`,
    ));
  mutation("todo-api", "remove only update validation", (root) =>
    replace(
      root,
      endpoints,
      /(private static async Task<IResult> UpdateTodoAsync[\s\S]*?)var errors = Validate\(request\);/,
      (match, prefix) => `${prefix}var errors = new Dictionary<string, string[]>(StringComparer.Ordinal);`,
    ));
}

function mvcMutations() {
  const books = "Controllers/BooksController.cs";
  mutation("mvc", "remove list flow", (root) =>
    replace(root, books, ".ToListAsync()", ".ToList()", 1));
  mutation("mvc", "remove details flow", (root) =>
    replace(root, books, "FirstOrDefaultAsync(item => item.Id == id)", "FirstOrDefault(item => item.Id == id)", 1));
  mutation("mvc", "remove create flow", (root) =>
    replace(root, books, "context.Add(book)", "_ = book"));
  mutation("mvc", "remove edit flow", (root) =>
    replace(root, books, "context.Update(book)", "_ = book"));
  mutation("mvc", "remove delete flow", (root) =>
    replace(root, books, "context.Books.Remove(book)", "_ = book"));
  mutation("mvc", "remove validation", (root) =>
    replace(root, books, /ModelState\.IsValid/g, "true"));
  mutation("mvc", "remove parent selection", (root) => {
    replace(root, "Views/Books/Create.cshtml", /AuthorId/g, "WriterId");
    replace(root, "Views/Books/Edit.cshtml", /AuthorId/g, "WriterId");
  });
  mutation("mvc", "remove only Create parent selection", (root) =>
    replace(root, "Views/Books/Create.cshtml", /AuthorId/g, "WriterId"));
  mutation("mvc", "remove only Edit parent selection", (root) =>
    replace(root, "Views/Books/Edit.cshtml", /AuthorId/g, "WriterId"));
  mutation("mvc", "remove navigation", (root) => {
    replace(root, "Views/Shared/_Layout.cshtml", /Books/g, "CatalogItems");
    replace(root, "Views/Shared/_Layout.cshtml", /Authors/g, "Writers");
  });
  mutation("mvc", "empty controllers", (root) => {
    write(root, books,
      "using Microsoft.AspNetCore.Mvc;\nnamespace CatalogMvc.Controllers;\npublic class BooksController : Controller { }\n");
    write(root, "Controllers/AuthorsController.cs",
      "using Microsoft.AspNetCore.Mvc;\nnamespace CatalogMvc.Controllers;\npublic class AuthorsController : Controller { }\n");
  });
}

function testParentApiHandlerLayouts() {
  const namedRoot = cloneForMutation("parent-api", "named-handler-parameter-layout");
  renameProductRequestParameter(namedRoot, "CreateProductAsync", "createPayload");
  renameProductRequestParameter(namedRoot, "UpdateProductAsync", "updateCommand");
  expectPass("parent-api", namedRoot, "named handlers with arbitrary request parameter names");

  const lambdaRoot = cloneForMutation("parent-api", "lambda-parameter-layout");
  inlineProductHandler(
    lambdaRoot,
    "CreateProductAsync",
    'products.MapPost("", CreateProductAsync)',
    "newProduct",
  );
  inlineProductHandler(
    lambdaRoot,
    "UpdateProductAsync",
    'products.MapPut("/{id:int}", UpdateProductAsync)',
    "changedProduct",
  );
  expectPass("parent-api", lambdaRoot, "inline lambdas with arbitrary request parameter names");
}

function testParentApiErrorConditionLayouts() {
  const anyRoot = cloneForMutation("parent-api", "errors-any-layout");
  replace(anyRoot, "Endpoints/ProductEndpoints.cs", /errors\.Count > 0/g, "errors.Any()");
  expectPass("parent-api", anyRoot, "errors.Any() validation condition");

  const zeroRoot = cloneForMutation("parent-api", "errors-zero-layout");
  replace(
    zeroRoot,
    "Endpoints/ProductEndpoints.cs",
    /if \(errors\.Count > 0\)\s*\{\s*return TypedResults\.ValidationProblem\(errors\);\s*\}/g,
    `if (errors.Count == 0)
        {
        }
        else
        {
            return TypedResults.ValidationProblem(errors);
        }`,
  );
  expectPass("parent-api", zeroRoot, "zero-count validation condition");

  const negatedAnyRoot = cloneForMutation("parent-api", "errors-negated-any-layout");
  replace(
    negatedAnyRoot,
    "Endpoints/ProductEndpoints.cs",
    /if \(errors\.Count > 0\)\s*\{\s*return TypedResults\.ValidationProblem\(errors\);\s*\}/g,
    `if (!errors.Any())
        {
        }
        else
        {
            return TypedResults.ValidationProblem(errors);
        }`,
  );
  expectPass("parent-api", negatedAnyRoot, "negated errors.Any() validation condition");
}

function parentApiMutations() {
  const endpoints = "Endpoints/ProductEndpoints.cs";
  mutation("parent-api", "remove list flow", (root) =>
    replace(root, endpoints, ".ToListAsync()", ".ToList()", 1));
  mutation("parent-api", "remove by-id flow", (root) =>
    replace(root, endpoints, ".SingleOrDefaultAsync()", ".SingleOrDefault()", 1));
  mutation("parent-api", "remove create flow", (root) =>
    replace(root, endpoints, "dbContext.Categories.Add(category)", "_ = category"));
  mutation("parent-api", "remove update flow", (root) =>
    replace(root, endpoints, "category.Name = request.Name.Trim();", "_ = request.Name"));
  mutation("parent-api", "redirect Category update assignment to request", (root) =>
    replace(root, endpoints, "category.Name = request.Name.Trim();", "request.Name = request.Name.Trim();"));
  mutation("parent-api", "self-assign Category update target", (root) =>
    replace(root, endpoints, "category.Name = request.Name.Trim();", "category.Name = category.Name;"));
  mutation("parent-api", "redirect Product update assignments to unrelated object", (root) => {
    replace(root, endpoints, "product.Name = request.Name.Trim();", "unrelated.Name = request.Name.Trim();");
    replace(root, endpoints, "product.Price = request.Price;", "unrelated.Price = request.Price;");
    replace(root, endpoints, "product.CategoryId = request.CategoryId;", "unrelated.CategoryId = request.CategoryId;");
  });
  mutation("parent-api", "self-assign Product update target", (root) => {
    replace(root, endpoints, "product.Name = request.Name.Trim();", "product.Name = product.Name;");
    replace(root, endpoints, "product.Price = request.Price;", "product.Price = product.Price;");
    replace(root, endpoints, "product.CategoryId = request.CategoryId;", "product.CategoryId = product.CategoryId;");
  });
  mutation("parent-api", "remove delete flow", (root) =>
    replace(root, endpoints, "dbContext.Categories.Remove(category)", "_ = category"));
  mutation("parent-api", "remove validation", (root) =>
    replace(root, endpoints, "string.IsNullOrWhiteSpace(request.Name)", "false"));
  mutation("parent-api", "remove parent id validation", (root) =>
    replace(root, endpoints, "dbContext.Categories.FindAsync(request.CategoryId)", "dbContext.Products.FindAsync(request.CategoryId)"));
  mutation("parent-api", "remove only Product create validation", (root) =>
    replace(
      root,
      endpoints,
      /(private static async Task<IResult> CreateProductAsync[\s\S]*?)var errors = Validate\(request\);/,
      (match, prefix) => `${prefix}var errors = new Dictionary<string, string[]>(StringComparer.Ordinal);`,
    ));
  mutation("parent-api", "remove only Product update validation", (root) =>
    replace(
      root,
      endpoints,
      /(private static async Task<IResult> UpdateProductAsync[\s\S]*?)var errors = Validate\(request\);/,
      (match, prefix) => `${prefix}var errors = new Dictionary<string, string[]>(StringComparer.Ordinal);`,
    ));
  mutation("parent-api", "remove only Product create parent lookup", (root) =>
    replace(
      root,
      endpoints,
      /(private static async Task<IResult> CreateProductAsync[\s\S]*?)dbContext\.Categories\.FindAsync\(request\.CategoryId\)/,
      (match, prefix) => `${prefix}dbContext.Products.FindAsync(request.CategoryId)`,
    ));
  mutation("parent-api", "remove only Product update parent lookup", (root) =>
    replace(
      root,
      endpoints,
      /(private static async Task<IResult> UpdateProductAsync[\s\S]*?)dbContext\.Categories\.FindAsync\(request\.CategoryId\)/,
      (match, prefix) => `${prefix}dbContext.Products.FindAsync(request.CategoryId)`,
    ));
  mutation("parent-api", "Product create parent lookup uses constant", (root) =>
    replace(
      root,
      endpoints,
      /(private static async Task<IResult> CreateProductAsync[\s\S]*?)dbContext\.Categories\.FindAsync\(request\.CategoryId\)/,
      (match, prefix) => `${prefix}dbContext.Categories.FindAsync(1)`,
    ));
  mutation("parent-api", "Product update parent lookup uses unrelated id", (root) =>
    replace(
      root,
      endpoints,
      /(private static async Task<IResult> UpdateProductAsync[\s\S]*?)dbContext\.Categories\.FindAsync\(request\.CategoryId\)/,
      (match, prefix) => `${prefix}dbContext.Categories.FindAsync(id)`,
    ));
  mutation("parent-api", "Product create parent lookup uses unrelated object CategoryId", (root) =>
    replace(
      root,
      endpoints,
      /(private static async Task<IResult> CreateProductAsync[\s\S]*?)dbContext\.Categories\.FindAsync\(request\.CategoryId\)/,
      (match, prefix) => `${prefix}dbContext.Categories.FindAsync(existing.CategoryId)`,
    ));
  mutation("parent-api", "Product update parent lookup uses unrelated nested CategoryId", (root) =>
    replace(
      root,
      endpoints,
      /(private static async Task<IResult> UpdateProductAsync[\s\S]*?)dbContext\.Categories\.FindAsync\(request\.CategoryId\)/,
      (match, prefix) => `${prefix}dbContext.Categories.FindAsync(unrelated.Parent.CategoryId)`,
    ));
  mutation("parent-api", "Product create awaits and ignores correct parent lookup", (root) =>
    replace(
      root,
      endpoints,
      /var category = await dbContext\.Categories\.FindAsync\(request\.CategoryId\);\s*if \(category is null\)\s*\{\s*errors\[nameof\(request\.CategoryId\)\] = \["CategoryId must identify an existing category\."\];\s*\}/,
      `_ = await dbContext.Categories.FindAsync(request.CategoryId);
        var category = new Category { Name = "ignored" };`,
      1,
    ));
  mutation("parent-api", "Product update awaits and ignores correct parent lookup", (root) =>
    replace(
      root,
      endpoints,
      /(private static async Task<IResult> UpdateProductAsync[\s\S]*?)var category = await dbContext\.Categories\.FindAsync\(request\.CategoryId\);\s*if \(category is null\)\s*\{\s*errors\[nameof\(request\.CategoryId\)\] = \["CategoryId must identify an existing category\."\];\s*\}/,
      (match, prefix) => `${prefix}_ = await dbContext.Categories.FindAsync(request.CategoryId);
        var category = new Category { Name = "ignored" };`,
      1,
    ));
  mutation("parent-api", "Product create reverses error condition", (root) =>
    replace(
      root,
      endpoints,
      /(private static async Task<IResult> CreateProductAsync[\s\S]*?)if \(errors\.Count > 0\)/,
      (match, prefix) => `${prefix}if (errors.Count == 0)`,
      1,
    ));
  mutation("parent-api", "Product update reverses error condition", (root) =>
    replace(
      root,
      endpoints,
      /(private static async Task<IResult> UpdateProductAsync[\s\S]*?)if \(errors\.Count > 0\)/,
      (match, prefix) => `${prefix}if (errors.Count == 0)`,
      1,
    ));
  mutation("parent-api", "whole response CategoryId capture", (root) =>
    replace(root, "ParentChildApi.http", "{{createCategory.response.body.$.id}}", "{{createCategory.response.body.$}}"));
}

function blazorMutations() {
  const employees = "Components/Pages/Employees.razor";
  mutation("blazor", "remove list flow", (root) =>
    replace(root, employees, /(\.Employees[\s\S]*?)\.ToListAsync\(\);/, (...args) => `${args[1]}.ToList();`, 1));
  mutation("blazor", "remove details flow", (root) =>
    replace(root, employees, /selectedEmployee\s*=\s*await/g, "selectedEmployee ="));
  mutation("blazor", "remove create flow", (root) =>
    replace(root, employees, "await db.Employees.AddAsync", "_ = db.Employees.Entry"));
  mutation("blazor", "remove edit flow", (root) =>
    replace(root, employees, /\s*employee\.(?:FirstName|LastName|Email|DepartmentId)\s*=\s*model\.(?:FirstName|LastName|Email|DepartmentId);/g, ""));
  mutation("blazor", "remove delete flow", (root) =>
    replace(root, employees, "db.Employees.Remove(employee)", "_ = employee"));
  mutation("blazor", "remove validation", (root) =>
    replace(root, employees, "<DataAnnotationsValidator />", ""));
  mutation("blazor", "remove parent selection", (root) =>
    replace(root, employees, /<InputSelect[\s\S]*?<\/InputSelect>/, "<p>No department selection</p>"));
  mutation("blazor", "remove navigation", (root) =>
    replace(root, "Components/Layout/NavMenu.razor", /employees/gi, "staff"));
  mutation("blazor", "empty components", (root) => {
    write(root, employees, '@page "/employees"\n@rendermode InteractiveServer\n<h1>Employees</h1>\n');
    write(root, "Components/Pages/Departments.razor",
      '@page "/departments"\n@rendermode InteractiveServer\n<h1>Departments</h1>\n');
  });
}

function entityComponents(namespaceName, entity, parent = null) {
  const plural = `${entity}s`;
  const lower = plural.toLowerCase();
  const fields = entity === "Employee"
    ? '<InputText @bind-Value="model.FirstName" /><InputText @bind-Value="model.LastName" /><InputText @bind-Value="model.Email" />'
    : '<InputText @bind-Value="model.Name" />';
  const parentMarkup = parent
    ? `<InputSelect @bind-Value="model.${parent}Id">@foreach (var option in options) { <option value="@option.Id">@option.Name</option> }</InputSelect>`
    : "";
  const parentCode = parent
    ? `private List<${parent}> options = [];\nprotected override async Task OnInitializedAsync() { await using var db = await DbFactory.CreateDbContextAsync(); options = await db.${parent}s.AsNoTracking().ToListAsync(); }`
    : "";
  const assignments = entity === "Employee"
    ? "target.FirstName = model.FirstName; target.LastName = model.LastName; target.Email = model.Email; target.DepartmentId = model.DepartmentId;"
    : "target.Name = model.Name;";

  return {
    [`Components/Pages/${entity}/Index.razor`]: `@page "/${lower}"\n@using ${namespaceName}.Data\n@using ${namespaceName}.Models\n@using Microsoft.EntityFrameworkCore\n@inject IDbContextFactory<AppDbContext> DbFactory\n@foreach (var item in items) { <a href="/${lower}/details/@item.Id">@item.Id</a> }\n@code { private List<${entity}> items = []; protected override async Task OnInitializedAsync() { await using var db = await DbFactory.CreateDbContextAsync(); items = await db.${plural}.AsNoTracking().ToListAsync(); } }\n`,
    [`Components/Pages/${entity}/Details.razor`]: `@page "/${lower}/details/{Id:int}"\n@using ${namespaceName}.Data\n@using ${namespaceName}.Models\n@using Microsoft.EntityFrameworkCore\n@inject IDbContextFactory<AppDbContext> DbFactory\n<h1>${entity} details</h1>@if (selected is not null) { <p>@selected.Id</p> }\n@code { [Parameter] public int Id { get; set; } private ${entity}? selected; protected override async Task OnInitializedAsync() { await using var db = await DbFactory.CreateDbContextAsync(); selected = await db.${plural}.AsNoTracking().FirstOrDefaultAsync(x => x.Id == Id); } }\n`,
    [`Components/Pages/${entity}/Create.razor`]: `@page "/${lower}/create"\n@using ${namespaceName}.Data\n@using ${namespaceName}.Models\n@using Microsoft.EntityFrameworkCore\n@inject IDbContextFactory<AppDbContext> DbFactory\n<EditForm Model="@model" OnValidSubmit="CreateAsync"><DataAnnotationsValidator /><ValidationSummary />${fields}${parentMarkup}<button type="submit">Create</button></EditForm>\n@code { private ${entity} model = new(); ${parentCode} private async Task CreateAsync() { await using var db = await DbFactory.CreateDbContextAsync(); await db.${plural}.AddAsync(model); await db.SaveChangesAsync(); model = new(); } }\n`,
    [`Components/Pages/${entity}/Edit.razor`]: `@page "/${lower}/edit/{Id:int}"\n@using ${namespaceName}.Data\n@using ${namespaceName}.Models\n@using Microsoft.EntityFrameworkCore\n@inject IDbContextFactory<AppDbContext> DbFactory\n<EditForm Model="@model" OnValidSubmit="UpdateAsync"><DataAnnotationsValidator /><ValidationMessage For="@(() => model.${entity === "Employee" ? "FirstName" : "Name"})" />${fields}${parentMarkup}<button type="submit">Save</button></EditForm>\n@code { [Parameter] public int Id { get; set; } private ${entity} model = new(); private List<${parent ?? "Department"}> options = []; protected override async Task OnInitializedAsync() { await using var db = await DbFactory.CreateDbContextAsync(); model = await db.${plural}.AsNoTracking().FirstOrDefaultAsync(x => x.Id == Id) ?? new(); ${parent ? `options = await db.${parent}s.AsNoTracking().ToListAsync();` : ""} } private async Task UpdateAsync() { await using var db = await DbFactory.CreateDbContextAsync(); var target = await db.${plural}.FindAsync(Id); if (target is null) return; ${assignments} await db.SaveChangesAsync(); } }\n`,
    [`Components/Pages/${entity}/Delete.razor`]: `@page "/${lower}/delete/{Id:int}"\n@using ${namespaceName}.Data\n@using ${namespaceName}.Models\n@using Microsoft.EntityFrameworkCore\n@inject IDbContextFactory<AppDbContext> DbFactory\n<button @onclick="DeleteAsync">Delete</button>\n@code { [Parameter] public int Id { get; set; } private async Task DeleteAsync() { await using var db = await DbFactory.CreateDbContextAsync(); var target = await db.${plural}.FindAsync(Id); if (target is null) return; db.${plural}.Remove(target); await db.SaveChangesAsync(); } }\n`,
  };
}

function testSeparateBlazorLayout() {
  const root = cloneForMutation("blazor", "separate-component-layout");
  fs.rmSync(path.join(root, "Components", "Pages", "Employees.razor"));
  fs.rmSync(path.join(root, "Components", "Pages", "Departments.razor"));
  const generated = {
    ...entityComponents("BlazorApp", "Employee", "Department"),
    ...entityComponents("BlazorApp", "Department"),
  };
  for (const [relative, text] of Object.entries(generated)) write(root, relative, text);
  expectPass("blazor", root, "synthetic separate-component layout");
  mutationFrom("blazor", root, "separate remove create validation", (mutationRoot) =>
    replace(mutationRoot, "Components/Pages/Employee/Create.razor", "<DataAnnotationsValidator />", ""));
  mutationFrom("blazor", root, "separate remove edit validation", (mutationRoot) =>
    replace(mutationRoot, "Components/Pages/Employee/Edit.razor", "<DataAnnotationsValidator />", ""));
  mutationFrom("blazor", root, "separate remove edit Department selector", (mutationRoot) =>
    replace(
      mutationRoot,
      "Components/Pages/Employee/Edit.razor",
      /<InputSelect[\s\S]*?<\/InputSelect>/,
      "<p>No department selection</p>",
    ));
  mutationFrom("blazor", root, "separate remove update SaveChangesAsync", (mutationRoot) =>
    replace(
      mutationRoot,
      "Components/Pages/Employee/Edit.razor",
      "await db.SaveChangesAsync();",
      "_ = target;",
    ));
  mutationFrom("blazor", root, "separate remove delete SaveChangesAsync", (mutationRoot) =>
    replace(
      mutationRoot,
      "Components/Pages/Employee/Delete.razor",
      "await db.SaveChangesAsync();",
      "_ = target;",
    ));
}

function combinedEmployeeComponent() {
  return `@page "/employees"
@using BlazorApp.Data
@using BlazorApp.Models
@using Microsoft.EntityFrameworkCore
@inject IDbContextFactory<AppDbContext> DbFactory
@foreach (var employee in employees)
{
    <button @onclick="() => ShowDetailsAsync(employee.Id)">@employee.FirstName</button>
    <button @onclick="() => DeleteEmployeeAsync(employee.Id)">Delete</button>
}
@if (selectedEmployee is not null)
{
    <section aria-label="Employee details">@selectedEmployee.Email</section>
}
<EditForm Model="@createModel" OnValidSubmit="CreateEmployeeAsync">
    <DataAnnotationsValidator />
    <ValidationSummary />
    <InputText @bind-Value="createModel.FirstName" />
    <InputText @bind-Value="createModel.LastName" />
    <InputText @bind-Value="createModel.Email" />
    <InputSelect @bind-Value="createModel.DepartmentId">
        @foreach (var department in createDepartments)
        {
            <option value="@department.Id">@department.Name</option>
        }
    </InputSelect>
</EditForm>
<EditForm Model="@editModel" OnValidSubmit="UpdateEmployeeAsync">
    <DataAnnotationsValidator />
    <ValidationMessage For="@(() => editModel.FirstName)" />
    <InputText @bind-Value="editModel.FirstName" />
    <InputText @bind-Value="editModel.LastName" />
    <InputText @bind-Value="editModel.Email" />
    <InputSelect @bind-Value="editModel.DepartmentId">
        @foreach (var department in editDepartments)
        {
            <option value="@department.Id">@department.Name</option>
        }
    </InputSelect>
</EditForm>
@code {
    private List<Employee> employees = [];
    private List<Department> createDepartments = [];
    private List<Department> editDepartments = [];
    private Employee createModel = new();
    private Employee editModel = new();
    private Employee? selectedEmployee;

    protected override async Task OnInitializedAsync()
    {
        await using var db = await DbFactory.CreateDbContextAsync();
        employees = await db.Employees.AsNoTracking().ToListAsync();
        createDepartments = await db.Departments.AsNoTracking().ToListAsync();
        editDepartments = await db.Departments.AsNoTracking().ToListAsync();
    }

    private async Task ShowDetailsAsync(int id)
    {
        await using var db = await DbFactory.CreateDbContextAsync();
        selectedEmployee = await db.Employees.AsNoTracking().FirstOrDefaultAsync(employee => employee.Id == id);
    }

    private async Task CreateEmployeeAsync()
    {
        await using var db = await DbFactory.CreateDbContextAsync();
        await db.Employees.AddAsync(createModel);
        await db.SaveChangesAsync();
        createModel = new();
    }

    private async Task UpdateEmployeeAsync()
    {
        await using var db = await DbFactory.CreateDbContextAsync();
        var target = await db.Employees.FindAsync(editModel.Id);
        if (target is null) return;
        target.FirstName = editModel.FirstName;
        target.LastName = editModel.LastName;
        target.Email = editModel.Email;
        target.DepartmentId = editModel.DepartmentId;
        await db.SaveChangesAsync();
    }

    private async Task DeleteEmployeeAsync(int id)
    {
        await using var db = await DbFactory.CreateDbContextAsync();
        var target = await db.Employees.FindAsync(id);
        if (target is null) return;
        db.Employees.Remove(target);
        await db.SaveChangesAsync();
    }
}
`;
}

function testCombinedTwoFormBlazorLayout() {
  const root = cloneForMutation("blazor", "combined-two-form-layout");
  write(root, "Components/Pages/Employees.razor", combinedEmployeeComponent());
  expectPass("blazor", root, "synthetic combined two-form layout");
  mutationFrom("blazor", root, "combined swapped create and edit handlers", (mutationRoot) => {
    replace(
      mutationRoot,
      "Components/Pages/Employees.razor",
      'OnValidSubmit="CreateEmployeeAsync"',
      'OnValidSubmit="TemporaryEmployeeAsync"',
    );
    replace(
      mutationRoot,
      "Components/Pages/Employees.razor",
      'OnValidSubmit="UpdateEmployeeAsync"',
      'OnValidSubmit="CreateEmployeeAsync"',
    );
    replace(
      mutationRoot,
      "Components/Pages/Employees.razor",
      'OnValidSubmit="TemporaryEmployeeAsync"',
      'OnValidSubmit="UpdateEmployeeAsync"',
    );
  });
  mutationFrom("blazor", root, "combined two forms share one dual-operation handler", (mutationRoot) => {
    replace(
      mutationRoot,
      "Components/Pages/Employees.razor",
      'OnValidSubmit="CreateEmployeeAsync"',
      'OnValidSubmit="SaveEmployeeAsync"',
    );
    replace(
      mutationRoot,
      "Components/Pages/Employees.razor",
      'OnValidSubmit="UpdateEmployeeAsync"',
      'OnValidSubmit="SaveEmployeeAsync"',
    );
    replace(
      mutationRoot,
      "Components/Pages/Employees.razor",
      "    private async Task DeleteEmployeeAsync(int id)",
      `    private async Task SaveEmployeeAsync()
    {
        if (createModel.Id == 0)
        {
            await CreateEmployeeAsync();
        }
        else
        {
            await UpdateEmployeeAsync();
        }
    }

    private async Task DeleteEmployeeAsync(int id)`,
      1,
    );
  });
  mutationFrom("blazor", root, "combined create assignments cannot stand in for edit form", (mutationRoot) => {
    replace(
      mutationRoot,
      "Components/Pages/Employees.razor",
      "        await db.Employees.AddAsync(createModel);",
      `        var employee = new Employee();
        employee.FirstName = createModel.FirstName;
        employee.LastName = createModel.LastName;
        employee.Email = createModel.Email;
        employee.DepartmentId = createModel.DepartmentId;
        await db.Employees.AddAsync(employee);`,
    );
    replace(
      mutationRoot,
      "Components/Pages/Employees.razor",
      /(<EditForm Model="@editModel"[\s\S]*?)\s*<DataAnnotationsValidator \/>/,
      (match, prefix) => prefix,
    );
    replace(
      mutationRoot,
      "Components/Pages/Employees.razor",
      /(<EditForm Model="@editModel"[\s\S]*?)<InputSelect[\s\S]*?<\/InputSelect>/,
      (match, prefix) => `${prefix}<p>No department selection</p>`,
    );
  });
  mutationFrom("blazor", root, "combined create handler uses edit form model", (mutationRoot) =>
    replace(
      mutationRoot,
      "Components/Pages/Employees.razor",
      "await db.Employees.AddAsync(createModel)",
      "await db.Employees.AddAsync(editModel)",
    ));
  mutationFrom("blazor", root, "combined remove edit validation", (mutationRoot) =>
    replace(
      mutationRoot,
      "Components/Pages/Employees.razor",
      /(<EditForm Model="@editModel"[\s\S]*?)\s*<DataAnnotationsValidator \/>/,
      "$1",
    ));
  mutationFrom("blazor", root, "combined remove edit Department selector", (mutationRoot) =>
    replace(
      mutationRoot,
      "Components/Pages/Employees.razor",
      /(<EditForm Model="@editModel"[\s\S]*?)<InputSelect[\s\S]*?<\/InputSelect>/,
      "$1<p>No department selection</p>",
    ));
  mutationFrom("blazor", root, "combined remove edit Department choices load", (mutationRoot) =>
    replace(
      mutationRoot,
      "Components/Pages/Employees.razor",
      "editDepartments = await db.Departments.AsNoTracking().ToListAsync();",
      "editDepartments = [];",
    ));
}

function replaceSharedEmployeeSubmit(root, body) {
  replace(
    root,
    "Components/Pages/Employees.razor",
    /    private async Task SaveEmployeeAsync\(\)\s*\{[\s\S]*?\n    \}\s*\n\s*    private async Task AddEmployeeAsync/,
    `    private async Task SaveEmployeeAsync()
    {
${body}
    }

    private async Task AddEmployeeAsync`,
    1,
  );
}

function sharedBlazorBranchMutations() {
  const directNewRoot = cloneForMutation("blazor", "shared-direct-new-predicate");
  replace(
    directNewRoot,
    "Components/Pages/Employees.razor",
    /        var wasNew = employeeForm\.Id == 0;\s*        if \(wasNew\)/,
    "        if (employeeForm.Id == 0)",
  );
  expectPass("blazor", directNewRoot, "shared direct new-entity predicate");

  const directExistingRoot = cloneForMutation("blazor", "shared-direct-existing-predicate");
  replaceSharedEmployeeSubmit(directExistingRoot, `        if (employeeForm.Id != 0)
        {
            if (!await UpdateEmployeeAsync(employeeForm))
            {
                return;
            }
        }
        else
        {
            await AddEmployeeAsync(employeeForm);
        }`);
  expectPass("blazor", directExistingRoot, "shared direct existing-entity predicate");

  mutation("blazor", "shared Employee reverses new-state predicate", (root) =>
    replace(
      root,
      "Components/Pages/Employees.razor",
      "var wasNew = employeeForm.Id == 0;",
      "var wasNew = employeeForm.Id != 0;",
      1,
    ));
  mutation("blazor", "shared Department reverses new-state predicate", (root) =>
    replace(
      root,
      "Components/Pages/Departments.razor",
      "var wasNew = departmentForm.Id == 0;",
      "var wasNew = departmentForm.Id != 0;",
      1,
    ));
  mutation("blazor", "shared helpers outside form-state branches", (root) =>
    replaceSharedEmployeeSubmit(root, `        if (employeeForm.Id == 0)
        {
            statusMessage = "Creating.";
        }
        else
        {
            statusMessage = "Editing.";
        }

        await AddEmployeeAsync(employeeForm);
        await UpdateEmployeeAsync(employeeForm);`));
  mutation("blazor", "shared helpers both in create branch", (root) =>
    replaceSharedEmployeeSubmit(root, `        var wasNew = employeeForm.Id == 0;
        if (wasNew)
        {
            await AddEmployeeAsync(employeeForm);
            await UpdateEmployeeAsync(employeeForm);
        }
        else
        {
            return;
        }`));
  mutation("blazor", "shared helpers both in edit branch", (root) =>
    replaceSharedEmployeeSubmit(root, `        var wasNew = employeeForm.Id == 0;
        if (wasNew)
        {
            return;
        }
        else
        {
            await AddEmployeeAsync(employeeForm);
            await UpdateEmployeeAsync(employeeForm);
        }`));
}

try {
  for (const item of cases) materializeGolden(item);
  for (const item of restraintCases) materializeRestraintGolden(item);
  testPatchTodoLayout();
  testTodoUpdatePatterns();
  razorMutations();
  todoMutations();
  mvcMutations();
  testParentApiHandlerLayouts();
  testParentApiErrorConditionLayouts();
  parentApiMutations();
  blazorMutations();
  testSeparateBlazorLayout();
  testCombinedTwoFormBlazorLayout();
  sharedBlazorBranchMutations();
  restraintMutations();
} catch (error) {
  failures.push(error.stack || error.message);
}

console.log(`Validator test artifacts: ${runRoot}`);
if (failures.length > 0) {
  console.error(`\n${failures.length} unexpected result${failures.length === 1 ? "" : "s"}:`);
  for (const failure of failures) console.error(`\n${failure}`);
  process.exit(1);
}

console.log(`PASS all validator tests (${cases.length + restraintCases.length} contracts)`);
