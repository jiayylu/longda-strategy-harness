/** Extract one complete, unambiguous JSON object without changing any values/code. */
export function modelJson(text: string): unknown {
  const raw = text.trim();
  try {
    return JSON.parse(raw);
  } catch {}
  const blocks = [...raw.matchAll(/```json\s*\n?([\s\S]*?)```/g)];
  if (blocks.length) {
    if (blocks.length !== 1) throw Error("Ambiguous JSON fenced blocks");
    return JSON.parse(blocks[0][1]);
  }
  if (raw.includes("```")) throw Error("Incomplete or non-JSON fenced output");
  // Some providers prepend a visible explanation to a tool request. Parse its
  // balanced object exactly, respecting escaped quotes/braces in source strings.
  // Multiple objects or unfinished objects are rejected, never guessed/repaired.
  const docs: unknown[] = [];
  let depth = 0,
    start = -1,
    quoted = false,
    escaped = false;
  for (let i = 0; i < raw.length; i++) {
    const c = raw[i];
    if (depth === 0) {
      if (c === "{") {
        start = i;
        depth = 1;
      }
      continue;
    }
    if (quoted) {
      if (escaped) escaped = false;
      else if (c === "\\") escaped = true;
      else if (c === '"') quoted = false;
      continue;
    }
    if (c === '"') quoted = true;
    else if (c === "{") depth++;
    else if (c === "}" && --depth === 0)
      docs.push(JSON.parse(raw.slice(start, i + 1)));
  }
  if (depth !== 0 || docs.length !== 1)
    throw Error("Expected one complete, unambiguous JSON object");
  return docs[0];
}
