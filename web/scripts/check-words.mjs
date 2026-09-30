// Fails if a banned technical term appears inside a string literal or template literal in web/src/*.ts.
// technical.ts (the "Technical details" disclosure) and api.ts (wire types/URLs) are exempt.
// Template `${...}` expressions are stripped first, so identifiers such as a.admiralty are fine.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = fileURLToPath(new URL("../src/", import.meta.url));
const EXEMPT = new Set(["technical.ts", "api.ts"]);
// Case-sensitive acronyms plus case-insensitive words.
const BANNED = [/\b(MAD|NIC|NACp|H3)\b/, /\b(z-score|sigma|ratio|counterfactual|admiralty)\b/i];

// string literal scanner: "...", '...', `...` (escape aware). Skips comments and import lines.
function literals(src) {
  const out = [];
  let i = 0;
  let line = 1;
  while (i < src.length) {
    const c = src[i];
    if (c === "\n") line++;
    if (c === "/" && src[i + 1] === "/") {
      while (i < src.length && src[i] !== "\n") i++;
      continue;
    }
    if (c === "/" && src[i + 1] === "*") {
      const e = src.indexOf("*/", i + 2);
      for (const ch of src.slice(i, e)) if (ch === "\n") line++;
      i = e + 2;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") {
      const q = c;
      const start = line;
      let j = i + 1;
      let text = "";
      let depth = 0;
      while (j < src.length) {
        const d = src[j];
        if (d === "\n") line++;
        if (q === "`" && d === "$" && src[j + 1] === "{") {
          depth++;
          j += 2;
          continue;
        }
        if (depth > 0) {
          if (d === "{") depth++;
          if (d === "}") depth--;
          j++;
          continue;
        }
        if (d === "\\") {
          j += 2;
          continue;
        }
        if (d === q) break;
        text += d;
        j++;
      }
      const prev = src.slice(Math.max(0, i - 40), i);
      if (!/(from|import)\s*$/.test(prev)) out.push({ text, line: start });
      i = j + 1;
      continue;
    }
    i++;
  }
  return out;
}

let bad = 0;
for (const f of readdirSync(SRC).filter((n) => n.endsWith(".ts") && !EXEMPT.has(n))) {
  for (const { text, line } of literals(readFileSync(join(SRC, f), "utf8"))) {
    if (BANNED.some((re) => re.test(text))) {
      console.error(`${f}:${line}: banned term in "${text.slice(0, 70)}"`);
      bad++;
    }
  }
}
if (bad) {
  console.error(`check:words failed: ${bad} string(s). Use plain words from labels.ts, or move raw terms to technical.ts.`);
  process.exit(1);
}
console.log("check:words ok");
