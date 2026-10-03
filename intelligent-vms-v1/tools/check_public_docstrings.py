#!/usr/bin/env python3
"""Enforce docstrings on true public Python interfaces.

Nested local helpers are implementation details and are intentionally excluded.
The check covers production Python under services/ and tools/.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOTS = (Path("services"), Path("tools"))


def source_violations(source: str, *, filename: str = "<memory>") -> list[str]:
    """Return missing-docstring violations for public interfaces in Python source.

    Args:
        source: Python source text to inspect.
        filename: Display name included in violation messages.

    Returns:
        Stable violation strings for public top-level functions/classes and direct
        public methods of public classes that lack docstrings.

    Raises:
        SyntaxError: If the supplied source is not valid Python.
    """
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_") and ast.get_docstring(node) is None:
                violations.append(f"{filename}:{node.lineno}: public function {node.name} missing docstring")
            continue

        if not isinstance(node, ast.ClassDef) or node.name.startswith("_"):
            continue

        if ast.get_docstring(node) is None:
            violations.append(f"{filename}:{node.lineno}: public class {node.name} missing docstring")

        for child in node.body:
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if child.name.startswith("_"):
                continue
            if ast.get_docstring(child) is None:
                violations.append(
                    f"{filename}:{child.lineno}: public method {node.name}.{child.name} missing docstring"
                )

    return violations


def repository_violations(roots: tuple[Path, ...] = ROOTS) -> list[str]:
    """Scan production roots and return all public-interface docstring violations.

    Args:
        roots: Repository-relative Python source roots.

    Returns:
        Sorted violation messages for all Python files beneath the roots.

    Raises:
        OSError: If a source file cannot be read.
        SyntaxError: If a source file cannot be parsed.
    """
    violations: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            violations.extend(source_violations(source, filename=str(path)))
    return violations


def main() -> int:
    """Run the repository public-interface docstring gate.

    Returns:
        Zero when no violations exist; one after printing all violations.
    """
    violations = repository_violations()
    if violations:
        print("Public-interface docstring violations:")
        for violation in violations:
            print(f"- {violation}")
        return 1

    print("Public-interface docstring gate OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
