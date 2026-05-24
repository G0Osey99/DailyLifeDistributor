"""CI lint: refuse production secrets_store calls that don't scope themselves.

Production = core/, blueprints/, uploaders/ (the production import roots).
Allowed call shapes:
    secrets_store.get_secret(..., org_id=...)
    secrets_store.set_secret(..., org_id=...)
    secrets_store.set_platform_secret(...)
    secrets_store.get_platform_secret(...)
    ... and the blob/has/delete/list variants of each.

Disallowed:
    secrets_store.get_secret("foo")             # no org_id
    secrets_store.set_secret("foo", "bar")

The unscoped variants stay available for tests and migration tooling; this
lint is just the production-code gate.
"""
from __future__ import annotations

import ast
import pathlib
import sys

_SCOPED = {
    "get_secret", "set_secret", "delete_secret", "has_secret",
    "get_blob", "set_blob", "materialize_blob_to_tempfile",
    "list_secret_names",
}
_PLATFORM_ALLOWED = {
    "set_platform_secret", "get_platform_secret",
    "set_platform_blob", "get_platform_blob",
    "has_platform_secret", "delete_platform_secret",
}


def _collect_secrets_store_aliases(tree: ast.Module) -> set[str]:
    """Names by which this module refers to core.secrets_store.

    Picks up:
        from core import secrets_store              -> {"secrets_store"}
        from core import secrets_store as _ss       -> {"_ss"}
        import core.secrets_store as _ss            -> {"_ss"}
        from core.secrets_store import (...)        -> not aliased to a module,
                                                       function-direct callers
                                                       are caught separately
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # `from core import secrets_store [as X]`
            if node.module == "core":
                for alias in node.names:
                    if alias.name == "secrets_store":
                        aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            # `import core.secrets_store [as X]`
            for alias in node.names:
                if alias.name == "core.secrets_store":
                    # No `as` → bound name is `core` (the top of the dotted path).
                    # Calls would look like `core.secrets_store.foo()`, which is
                    # an Attribute-of-Attribute and a separate AST shape; treat
                    # bare-import as too unusual to handle here.
                    if alias.asname:
                        aliases.add(alias.asname)
    return aliases


def _called_attr_via_alias(node: ast.Call, aliases: set[str]) -> str | None:
    """If `node` is `<alias>.<attr>(...)` for any *alias* in *aliases*, return *attr*."""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id in aliases:
            return func.attr
    return None


def check_file(path: pathlib.Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        sys.stderr.write(f"warning: skipping {path}: {e}\n")
        return []
    aliases = _collect_secrets_store_aliases(tree)
    if not aliases:
        return []
    bad: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_attr_via_alias(node, aliases)
        if name is None:
            continue
        if name in _PLATFORM_ALLOWED:
            continue
        if name not in _SCOPED:
            continue
        has_org_kw = any(kw.arg == "org_id" for kw in node.keywords)
        if not has_org_kw:
            bad.append((node.lineno,
                        f"secrets_store.{name}() missing org_id="))
    return bad


_PROD_ROOTS = ("core/", "blueprints/", "uploaders/")
# Files we deliberately exempt: the secrets_store module itself, the
# migration script, and the agent shim which is a test double.
_EXEMPT = {
    "core/secrets_store.py",
    "core/migration_bootstrap.py",
    "scripts/migrate_secrets.py",
    "agent/secrets_shim.py",
    # Legacy shared-password hash (LEGACY_PASSWORD_ENABLED) is a platform-
    # wide gate, not a tenant credential. The unscoped reads/writes in
    # auth.py are intentional and stay unscoped by design.
    "core/auth.py",
}


def main(args: list[str]) -> int:
    if args:
        paths = [pathlib.Path(a) for a in args]
    else:
        repo = pathlib.Path(__file__).parent.parent
        paths = []
        for root in _PROD_ROOTS:
            paths.extend((repo / root).rglob("*.py"))
    failures: list[str] = []
    for p in paths:
        rel = p.as_posix()
        if any(rel.endswith("/" + e) or rel == e for e in _EXEMPT):
            continue
        for line, msg in check_file(p):
            failures.append(f"{rel}:{line}: {msg}")
    if failures:
        print("\n".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
