"""AST-based dependency slicer. See DESIGN.md § Architecture overview, step 1.

Given a set of `@mcu.export`/`@mcu.loop` functions, walks referenced names
transitively (module-level assignments, class defs, helper functions, local
imports) to produce the minimal set of source needed on the MCU.

Scope note: this module only decides *which top-level statements* are
needed and renders them back out, preserving `@mcu.export`/`@mcu.loop`
decorator syntax as written. Making those decorator names resolve on-device
(a runtime shim) and generating `@pc.export` proxy stubs are later concerns
(chunk 4, chunk 6, chunk 10's bundling step) — not this module's job.

`ruff` is invoked as a post-slice cleanup pass (unused-import stripping)
after this module produces the bundle — not used for the slicing itself.

Known limitation: module-level assignment targets must be a single `Name`
(`x = ...`, `x: T = ...`). Tuple/list-unpacking targets (`x, y = ...`) bind
nothing and are silently excluded — not currently supported.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

_MCU_DECORATOR_ATTRS = {"export", "loop"}
_MCU_NAMESPACE = "mcu"


@dataclass(frozen=True)
class SliceResult:
    """The minimal source subset needed to run the MCU-bound code."""

    source: str
    exported_names: frozenset[str]


def _tether_import_aliases(tree: ast.Module) -> dict[str, str]:
    """Map local identifier -> canonical name ('mcu' or 'pc') for whatever
    was imported from the top-level `tether` package, honoring `as`
    aliasing (e.g. `from tether import mcu as m` -> {"m": "mcu"}).
    """
    aliases: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and _is_pc_only_tether_module(node.module)
        ):
            for alias in node.names:
                if alias.name in ("mcu", "pc"):
                    aliases[alias.asname or alias.name] = alias.name
    return aliases


def _is_mcu_decorator(decorator: ast.expr, aliases: dict[str, str]) -> bool:
    # Matches both bare `@mcu.export` and called `@mcu.loop(interval_ms=100)`,
    # resolved through whatever local name `mcu` was imported/aliased as.
    node = decorator.func if isinstance(decorator, ast.Call) else decorator
    return (
        isinstance(node, ast.Attribute)
        and node.attr in _MCU_DECORATOR_ATTRS
        and isinstance(node.value, ast.Name)
        and aliases.get(node.value.id) == _MCU_NAMESPACE
    )


def _referenced_names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _is_pc_only_tether_module(module: str) -> bool:
    # Exact "tether" or a dotted submodule ("tether.decorators"). Deliberately
    # NOT a bare prefix match — "tether_runtime" (the distinct, legitimate
    # MCU-side package) must not get caught by this.
    return module == "tether" or module.startswith("tether.")


def _bound_names(node: ast.stmt) -> list[str]:
    """Names this top-level statement binds, for building the dependency index."""
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        # PC-only `tether` package — never installed on the MCU (only
        # tether_runtime is). Excluded whole-statement rather than per-alias:
        # ast.unparse always renders every alias in a multi-name import
        # verbatim, so a statement mixing tether with something legitimate
        # (`import tether, os`) can't be partially included without
        # reconstructing a synthetic node. Deliberately not doing that —
        # mixing a project's own package with an unrelated import on one
        # line is a style ruff/isort already discourage, so this is an
        # acceptable, documented limitation rather than a real gap.
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if module and _is_pc_only_tether_module(module):
            return []
        if any(_is_pc_only_tether_module(alias.name) for alias in node.names):
            return []
        return [alias.asname or alias.name.split(".")[0] for alias in node.names]
    return []


def _resolve_local_module(module: str, base_dir: Path) -> Path | None:
    candidate = base_dir / f"{module.replace('.', '/')}.py"
    return candidate if candidate.is_file() else None


def _collect_bindings(
    tree: ast.Module,
    base_dir: Path | None,
    bindings: dict[str, ast.stmt],
    visited_files: set[Path],
) -> None:
    """Populate `bindings` (name -> defining node) from `tree`, recursively
    following `from <local module> import ...` into sibling files under
    `base_dir`. Bare `import module` + attribute access, and non-local
    imports, are not followed — they fall through to being registered as a
    plain includable import statement via `_bound_names`.

    Known limitation: this is a single flat namespace across every file
    pulled in, matching the eventual single-file MCU bundle — if two local
    files define the same top-level name, the later-processed one wins.
    """
    for node in tree.body:
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and base_dir is not None
            and not _is_pc_only_tether_module(node.module)
        ):
            local_path = _resolve_local_module(node.module, base_dir)
            if local_path is not None:
                if local_path not in visited_files:
                    visited_files.add(local_path)
                    sub_tree = ast.parse(local_path.read_text())
                    _collect_bindings(sub_tree, local_path.parent, bindings, visited_files)
                for alias in node.names:
                    local_name = alias.asname or alias.name
                    if alias.name in bindings:
                        bindings[local_name] = bindings[alias.name]
                continue

        for name in _bound_names(node):
            bindings[name] = node


def _topological_order(
    preferred_order: list[ast.stmt],
    included: dict[ast.stmt, None],
    bindings: dict[str, ast.stmt],
) -> list[ast.stmt]:
    """DFS-based topological sort: a node's dependencies always render
    before it, regardless of which file (entry or a followed local import)
    either came from. `preferred_order` is only a tiebreak/iteration order
    for nodes that aren't constrained relative to each other by any
    dependency edge.
    """
    visited: dict[ast.stmt, None] = {}
    ordered: list[ast.stmt] = []

    def visit(node: ast.stmt) -> None:
        if node in visited:
            return
        visited[node] = None
        for name in _referenced_names(node):
            dep = bindings.get(name)
            if dep is not None and dep in included and dep is not node:
                visit(dep)
        ordered.append(node)

    for node in preferred_order:
        visit(node)
    return ordered


def slice_mcu_bound(source: str, *, base_dir: Path | None = None) -> SliceResult:
    tree = ast.parse(source)
    # ast.AST nodes have no custom __eq__/__hash__, so they're already
    # identity-keyable directly — no need to wrap lookups in id(node).
    entry_index = {node: i for i, node in enumerate(tree.body)}

    bindings: dict[str, ast.stmt] = {}
    _collect_bindings(tree, base_dir, bindings, visited_files=set())

    aliases = _tether_import_aliases(tree)
    exported: list[ast.FunctionDef] = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(_is_mcu_decorator(dec, aliases) for dec in node.decorator_list)
    ]

    included: dict[ast.stmt, None] = {}  # dict-as-ordered-set, dedupe across files
    pending = list(exported)
    while pending:
        node = pending.pop()
        if node in included:
            continue
        included[node] = None
        for name in _referenced_names(node):
            dep = bindings.get(name)
            if dep is not None:
                pending.append(dep)

    # Real dependency edges (computed above via _referenced_names) always
    # win; entry-file textual order is only the tiebreak/iteration order for
    # otherwise-unconstrained nodes, so load-time side effects (e.g. an
    # assignment depending on another assignment, whether same-file or
    # pulled in from a followed local import) always render in a safe order.
    entry_nodes_first = sorted(
        (n for n in included if n in entry_index),
        key=lambda n: entry_index[n],
    )
    other_nodes = [n for n in included if n not in entry_index]
    ordered = _topological_order(other_nodes + entry_nodes_first, included, bindings)

    rendered = "\n\n".join(ast.unparse(node) for node in ordered)
    return SliceResult(
        source=rendered,
        exported_names=frozenset(node.name for node in exported),
    )
