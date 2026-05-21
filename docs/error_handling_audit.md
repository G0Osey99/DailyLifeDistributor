# Error-handling audit — open issues

Generated 2026-05-01. All identified items have been addressed.

## HIGH — done

## MEDIUM — done

(M33 was already correctly handled by an existing `deadline` + `!r.ok` early
return in the title-poll loop; no fix required.)

## LOW — done

(L9 was already safe — `_is_retryable_http_error` already wraps `int(status)`
in `try/except (TypeError, ValueError)`; no fix required.)

---

See git log for the per-issue commits and rationale; each fix is annotated
inline with the issue id (e.g. `# H3:`, `// M30:`).
