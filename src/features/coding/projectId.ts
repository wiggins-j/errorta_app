// Project-ID validation shared by the Create and Import forms.
//
// The single source of truth is the backend Field pattern in
// python/errorta_app/routes/coding.py:
//   project_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]{1,64}$")
// The backend re-validates on submit and returns 422 on mismatch; this mirror
// exists so the UI can warn *before* submit instead of failing silently. Keep
// the two in sync.

export const PROJECT_ID_RE = /^[A-Za-z0-9._-]{1,64}$/;
export const PROJECT_ID_MAX = 64;

/** Short, always-visible hint of what the field accepts. */
export const PROJECT_ID_HINT =
  "Letters, numbers, dot, underscore, hyphen — no spaces (max 64).";

/**
 * Validate a project id the way the backend will. Validates the *trimmed*
 * value (submit trims too, so a stray leading/trailing space is not an error).
 * Returns a human-readable reason when invalid, or `null` when the value is
 * acceptable — an empty value returns `null` (there is nothing to warn about
 * yet; submit stays disabled on empty separately).
 */
export function validateProjectId(raw: string): string | null {
  const value = raw.trim();
  if (value === "") return null;
  if (value.length > PROJECT_ID_MAX) {
    return `Project ID must be ${PROJECT_ID_MAX} characters or fewer.`;
  }
  if (!PROJECT_ID_RE.test(value)) {
    const bad = Array.from(new Set(value)).filter(
      (c) => !/[A-Za-z0-9._-]/.test(c),
    );
    const shown = bad
      .map((c) => (c === " " ? "spaces" : `“${c}”`))
      .join(", ");
    return `Only letters, numbers, dot (.), underscore (_), and hyphen (-) are allowed — remove ${shown}.`;
  }
  return null;
}
