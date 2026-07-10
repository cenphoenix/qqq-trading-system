"""Lightweight static audit for project Python files."""

from __future__ import annotations

import ast
import collections
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv-win", "__pycache__", "archive"}


def project_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*.py")
        if not any(part in SKIP_PARTS for part in path.parts)
    ]


def imported_names(tree: ast.AST) -> list[tuple[str, int, str]]:
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.asname or alias.name.split(".")[0], node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                source = f"{node.module or ''}.{alias.name}".strip(".")
                imports.append((alias.asname or alias.name, node.lineno, source))
    return imports


def audit(path: Path) -> list[str]:
    relative = path.relative_to(ROOT)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(relative))
    except (OSError, SyntaxError, UnicodeError) as exc:
        return [f"PARSE {relative}: {exc}"]

    findings = []
    top_names = collections.Counter(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    for name, count in top_names.items():
        if count > 1:
            findings.append(f"DUPLICATE {relative}: top-level {name} x{count}")

    for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        method_names = collections.Counter(
            node.name
            for node in class_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        for name, count in method_names.items():
            if count > 1:
                findings.append(f"DUPLICATE {relative}: {class_node.name}.{name} x{count}")

    used = collections.Counter(
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    )
    exported = {
        value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
        and isinstance(node.value, (ast.List, ast.Tuple))
        for value in node.value.elts
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    }
    for name, line, source in imported_names(tree):
        if source == "__future__.annotations" or name in exported:
            continue
        if name != "*" and not used[name]:
            findings.append(f"UNUSED_IMPORT {relative}:{line}: {source}")
    return findings


def main() -> int:
    findings = []
    for path in project_files():
        findings.extend(audit(path))
    for finding in findings:
        print(finding)
    print(f"Audited {len(project_files())} files; {len(findings)} findings")
    return 1 if any(item.startswith(("PARSE", "DUPLICATE")) for item in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
