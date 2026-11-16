"""Object repository: what pages, elements, flows and data factories exist.

Two views of the same page-object files:
  * static  (read_pages)   - parses apps/*/pages/*.py with `ast`, never executes them. Gives
                             Studio the exact locator source of every element, its line number,
                             and whether the file is generated (editable in Studio) or hand-written.
  * runtime (load_page_class, load_flow) - imports the classes so no-code steps use the very same
                             Locator objects coded tests use.

References used by no-code tests:
    page     retail_banking.AddBeneficiaryPage
    element  retail_banking.AddBeneficiaryPage.account
    flow     retail_banking.PaymentFlows.add_beneficiary
"""
from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import sys
import typing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from qaforge.nocode.locator_expr import LocatorExprError, canonical

GENERATED_MARK = "# qaforge:generated"
_FRAMEWORK_METHODS = {"open", "wait_until_loaded", "loaded_anchor", "sensitive_locators", "tid", "url"}


@dataclass
class ElementInfo:
    name: str
    kind: str  # locator | component | other
    locator: str | None  # canonical Playwright expression (page.get_by_...)
    source: str  # exact source text in the POM file
    line: int
    component: str | None = None
    sensitive: bool = False
    fallbacks: list[str] = field(default_factory=list)


@dataclass
class PageInfo:
    app: str
    class_name: str
    ref: str
    module: str
    file: str
    title: str
    path: str | None
    generated: bool
    loaded_anchor: str | None
    elements: list[ElementInfo] = field(default_factory=list)
    methods: list[dict[str, str]] = field(default_factory=list)
    doc: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- static view
class _SelfTidToPage(ast.NodeTransformer):
    """self.tid("x") -> page.get_by_test_id("x");  self.page.X -> page.X"""

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        f = node.func
        if (isinstance(f, ast.Attribute) and f.attr == "tid" and isinstance(f.value, ast.Name)
                and f.value.id == "self"):
            node.func = ast.Attribute(value=ast.Name(id="page", ctx=ast.Load()), attr="get_by_test_id",
                                      ctx=ast.Load())
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        if (isinstance(node.value, ast.Attribute) and node.value.attr == "page"
                and isinstance(node.value.value, ast.Name) and node.value.value.id == "self"):
            node.value = ast.Name(id="page", ctx=ast.Load())
        return node


def _friendly(expr: ast.AST, self_attrs: dict[str, str]) -> str | None:
    """Turn a POM expression into a canonical page.* locator string, resolving self.<attr> chains."""
    src = ast.unparse(_SelfTidToPage().visit(ast.parse(ast.unparse(expr), mode="eval")).body)
    for attr, loc in sorted(self_attrs.items(), key=lambda kv: -len(kv[0])):
        if src.startswith(f"self.{attr}."):
            src = loc + src[len(f"self.{attr}"):]
            break
    try:
        return canonical(src)
    except LocatorExprError:
        return None


def _str_const(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_page_class(cls: ast.ClassDef) -> bool:
    return any((isinstance(b, ast.Name) and b.id.endswith("Page")) or
               (isinstance(b, ast.Attribute) and b.attr.endswith("Page")) for b in cls.bases)


def read_page_file(file: Path, app: str, root: Path) -> list[PageInfo]:
    source = file.read_text(encoding="utf-8")
    tree = ast.parse(source)
    generated = source.lstrip().startswith(GENERATED_MARK)
    module = ".".join(file.relative_to(root).with_suffix("").parts)
    pages = []
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef) and _is_page_class(n)]:
        info = PageInfo(app=app, class_name=cls.name, ref=f"{app}.{cls.name}", module=module,
                        file=str(file.relative_to(root)), title=cls.name, path=None, generated=generated,
                        loaded_anchor=None, doc=ast.get_docstring(cls) or "")
        self_attrs: dict[str, str] = {}
        sensitive_names: set[str] = set()
        for node in cls.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id == "path":
                    info.path = _str_const(node.value)
                elif node.targets[0].id == "title":
                    info.title = _str_const(node.value) or cls.name
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name == "__init__":
                for stmt in ast.walk(node):
                    if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1):
                        continue
                    t = stmt.targets[0]
                    if not (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self"):
                        continue
                    value = stmt.value
                    el = ElementInfo(name=t.attr, kind="other", locator=None, source=ast.unparse(value),
                                     line=stmt.lineno)
                    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                            and value.func.attr == "chain" and isinstance(value.func.value, ast.Name)
                            and value.func.value.id == "self" and value.args):
                        locs = [_friendly(a, self_attrs) for a in value.args]
                        if locs[0]:
                            el.kind, el.locator = "locator", locs[0]
                            el.fallbacks = [x for x in locs[1:] if x]
                            self_attrs[t.attr] = locs[0]
                    elif (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.args
                            and value.func.id[:1].isupper()):
                        el.kind, el.component = "component", value.func.id
                        el.locator = _friendly(value.args[0], self_attrs)
                    else:
                        loc = _friendly(value, self_attrs)
                        if loc:
                            el.kind, el.locator = "locator", loc
                            self_attrs[t.attr] = loc
                    info.elements.append(el)
            elif node.name == "loaded_anchor":
                ret = next((r for r in ast.walk(node) if isinstance(r, ast.Return) and r.value is not None), None)
                if ret is not None:
                    src = ast.unparse(ret.value)
                    info.loaded_anchor = (src[5:] if src.startswith("self.") and src[5:].isidentifier()
                                          else _friendly(ret.value, self_attrs) or src)
            elif node.name == "sensitive_locators":
                for n in ast.walk(node):
                    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self":
                        sensitive_names.add(n.attr)
            elif not node.name.startswith("_") and node.name not in _FRAMEWORK_METHODS:
                args = [a.arg for a in node.args.args[1:]]
                info.methods.append({"name": node.name, "args": ", ".join(args),
                                     "doc": (ast.get_docstring(node) or "").split("\n")[0]})
        for el in info.elements:
            el.sensitive = el.name in sensitive_names
        pages.append(info)
    return pages


def read_pages(root: Path) -> list[PageInfo]:
    pages: list[PageInfo] = []
    for app_dir in sorted(p for p in (root / "apps").glob("*") if p.is_dir() and not p.name.startswith("_")):
        for f in sorted((app_dir / "pages").glob("*.py")):
            if f.name == "__init__.py":
                continue
            try:
                pages.extend(read_page_file(f, app_dir.name, root))
            except SyntaxError:
                continue
    return pages


def locator_index(pages: list[PageInfo]) -> dict[str, str]:
    """canonical locator -> element ref; lets recordings and scans reuse existing POM elements."""
    idx: dict[str, str] = {}
    for p in pages:
        for el in p.elements:
            if el.locator and el.kind == "locator":
                idx.setdefault(el.locator, f"{p.ref}.{el.name}")
    return idx


# --------------------------------------------------------------------------- runtime view
def _ensure_root(root: Path) -> None:
    r = str(root.resolve())
    if r not in sys.path:
        sys.path.insert(0, r)


def _import_submodules(package: str) -> list[Any]:
    try:
        pkg = importlib.import_module(package)
    except ModuleNotFoundError:
        return []
    mods = [pkg]
    if hasattr(pkg, "__path__"):
        for m in pkgutil.iter_modules(pkg.__path__):
            mods.append(importlib.import_module(f"{package}.{m.name}"))
    return mods


def load_page_class(ref: str, root: Path | None = None) -> type:
    from qaforge.core.base_page import BasePage

    if root is not None:
        _ensure_root(root)
    app, _, cls_name = ref.partition(".")
    if not app or not cls_name:
        raise LookupError(f"Page reference '{ref}' should look like app.PageClass")
    for mod in _import_submodules(f"apps.{app}.pages"):
        cls = getattr(mod, cls_name, None)
        if inspect.isclass(cls) and issubclass(cls, BasePage):
            return cls
    raise LookupError(f"Page '{cls_name}' not found in apps/{app}/pages")


def split_element_ref(ref: str) -> tuple[str, str]:
    page_ref, _, attr = ref.rpartition(".")
    if not page_ref or "." not in page_ref:
        raise LookupError(f"Element reference '{ref}' should look like app.PageClass.element")
    return page_ref, attr


@dataclass
class FlowInfo:
    ref: str
    app: str
    class_name: str
    method: str
    doc: str
    params: list[dict[str, str]]


def discover_flows(root: Path) -> list[FlowInfo]:
    from qaforge.core.base_page import BaseFlow

    _ensure_root(root)
    flows: list[FlowInfo] = []
    seen: set[str] = set()
    for app_dir in sorted(p for p in (root / "apps").glob("*") if p.is_dir() and not p.name.startswith("_")):
        app = app_dir.name
        modules = _import_submodules(f"apps.{app}") + _import_submodules(f"apps.{app}.flows")
        for mod in modules:
            for _, cls in inspect.getmembers(mod, inspect.isclass):
                if not issubclass(cls, BaseFlow) or cls is BaseFlow or not cls.__module__.startswith(f"apps.{app}"):
                    continue
                for name, fn in inspect.getmembers(cls, inspect.isfunction):
                    ref = f"{app}.{cls.__name__}.{name}"
                    if name.startswith("_") or ref in seen or fn.__qualname__.split(".")[0] != cls.__name__:
                        continue
                    seen.add(ref)
                    try:
                        hints = typing.get_type_hints(fn)
                    except Exception:  # noqa: BLE001
                        hints = {}
                    params = []
                    for p in list(inspect.signature(fn).parameters.values())[1:]:
                        t = hints.get(p.name)
                        params.append({"name": p.name, "type": getattr(t, "__name__", str(t) if t else ""),
                                       "required": "yes" if p.default is inspect.Parameter.empty else "no"})
                    flows.append(FlowInfo(ref, app, cls.__name__, name,
                                          (inspect.getdoc(fn) or "").split("\n")[0], params))
    return flows


def load_flow(ref: str, root: Path | None = None) -> tuple[type, str]:
    if root is not None:
        _ensure_root(root)
    parts = ref.split(".")
    if len(parts) != 3:
        raise LookupError(f"Flow reference '{ref}' should look like app.FlowClass.method")
    app, cls_name, method = parts
    for mod in _import_submodules(f"apps.{app}") + _import_submodules(f"apps.{app}.flows"):
        cls = getattr(mod, cls_name, None)
        if inspect.isclass(cls) and hasattr(cls, method):
            return cls, method
    raise LookupError(f"Flow '{ref}' not found")


def factories() -> dict[str, Any]:
    """Data factories by the name testers see (model name), with their fields."""
    from qaforge.data import factories as f

    out = {}
    for _, cls in inspect.getmembers(f, inspect.isclass):
        if issubclass(cls, f.Factory) and cls is not f.Factory and hasattr(cls, "model"):
            out[cls.model.__name__] = cls
    return out


def factory_fields(name: str) -> list[str]:
    cls = factories()[name]
    return list(cls.model.model_fields)
