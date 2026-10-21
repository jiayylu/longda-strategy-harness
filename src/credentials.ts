/** Validate before any workers start; never echo the token or its fragments. */
export function validateToken(raw: string | undefined): string {
  if (!raw)
    throw Error(
      "API credential missing. Use start.py hidden input or ANTHROPIC_AUTH_TOKEN.",
    );
  if (/[^\x21-\x7e]/.test(raw))
    throw Error(
      "API credential contains whitespace, a control character or a non-ASCII character. Paste only the token in hidden input; no Chinese punctuation, quotes or command text. Token is not displayed.",
    );
  if (/^Bearer /i.test(raw) || /["'`]/.test(raw))
    throw Error(
      "API credential must contain only the token, without quotes or a Bearer prefix. Token is not displayed.",
    );
  return raw;
}
