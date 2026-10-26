"""Generated page objects - real Python POM files that Studio owns.

Source of truth is a small YAML file next to the Python file:
    apps/<app>/pages/<module>.locators.yaml   <- edited in Studio
    apps/<app>/pages/<module>.py              <- rendered from it, marked '# qaforge:generated'

The rendered file is an ordinary BasePage subclass, so coded flows and tests can import and
use it exactly like a hand-written page object. Hand-written POM files are never modified.
Every locator is parsed by qaforge.nocode.locator_expr before rendering, so only genuine
Playwright locator calls with literal arguments can ever reach the Python file.
"""
from __future__ import annotations

import json
import keyword
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qaforge.nocode.locator_expr import parse, to_python, uses_regex
from qaforge.nocode.registry import GENERATED_MARK

CLASS_RE = re.compile(r"^[A-Z][A-Za-z0-9]{1,60}Page$")
ELEMENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,48}$")
APP_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
RESERVED = {"page", "session", "act", "settings", "title", "path", "url", "open", "tid", "wait_until_loaded",
            "loaded_anchor", "sensitive_locators"}


class PomError(ValueError):
    pass


class ElementDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    locator: str
    description: str = ""
    sensitive: bool = False
    fallbacks: list[str] = Field(default_factory=list)  # ordered backups, used only if the primary fails

    @field_validator("fallbacks")
    @classmethod
    def _fallbacks(cls, v: list[str]) -> list[str]:
        out = []
        for f in v:
            if f and f.strip():
                out.append(to_python(parse(f), "page"))
        return out[:3]

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not ELEMENT_RE.match(v) or keyword.iskeyword(v) or v in RESERVED:
            raise ValueError(f"'{v}' can't be used as an element name (lowercase, digits, underscores; "
                             f"not one of {', '.join(sorted(RESERVED))})")
        return v

    @field_validator("locator")
    @classmethod
    def _locator(cls, v: str) -> str:
        return to_python(parse(v), "page")  # validates + normalises


class GeneratedPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app: str
    class_name: str
    title: str
    path: str | None = None
    loaded_anchor: str | None = None
    description: str = ""
    elements: list[ElementDef] = Field(default_factory=list)

    @field_validator("app")
    @classmethod
    def _app(cls, v: str) -> str:
        if not APP_RE.match(v):
            raise ValueError("App folder name: lowercase letters, digits, underscores")
        return v

    @field_validator("class_name")
    @classmethod
    def _cls(cls, v: str) -> str:
        if not CLASS_RE.match(v):
            raise ValueError("Page name must be CamelCase and end with 'Page', e.g. CustomerSearchPage")
        return v

    @field_validator("path")
    @classmethod
    def _path(cls, v: str | None) -> str | None:
        if v and (not v.startswith("/") or any(ch in v for ch in "\"'\\\n")):
            raise ValueError("Address must start with / and contain no quotes")
        return v or None

    @model_validator(mode="after")
    def _anchor(self) -> GeneratedPage:
        names = [e.name for e in self.elements]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"Element names used twice: {', '.join(sorted(dupes))}")
        if self.loaded_anchor and self.loaded_anchor not in names:
            raise ValueError("'Ready when shown' must be one of the page's elements")
        return self

    @property
    def module(self) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", self.class_name).lower()


def _lit(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def render(p: GeneratedPage) -> str:
    regex = any(uses_regex(parse(x)) for e in p.elements for x in [e.locator, *e.fallbacks])
    lines = [
        f"{GENERATED_MARK} - edit in QA Forge Studio (source: {p.module}.locators.yaml). Manual edits are overwritten.",
        "from __future__ import annotations",
        "",
    ]
    if regex:
        lines.append("import re")
        lines.append("")
    lines += [
        "from playwright.sync_api import Locator",
        "",
        "from qaforge.core.base_page import BasePage",
        "",
        "",
        f"class {p.class_name}(BasePage):",
        f"    {_lit(p.description or p.title)}" if p.description or p.title else "",
        "",
        f"    path = {_lit(p.path or '')}",
        f"    title = {_lit(p.title)}",
        "",
        "    def __init__(self, session) -> None:  # type: ignore[no-untyped-def]",
        "        super().__init__(session)",
    ]
    for e in p.elements:
        comment = f"  # {e.description}" if e.description else ""
        comment = comment.replace("\n", " ")[:100]
        primary = to_python(parse(e.locator), "self.page")
        if e.fallbacks:
            backups = ", ".join(to_python(parse(f), "self.page") for f in e.fallbacks)
            lines.append(f"        self.{e.name} = self.chain({primary}, {backups}, name={_lit(e.name)}){comment}")
        else:
            lines.append(f"        self.{e.name} = {primary}{comment}")
    if not p.elements:
        lines.append("        pass")
    if p.loaded_anchor:
        lines += ["", "    def loaded_anchor(self) -> Locator:", f"        return self.{p.loaded_anchor}"]
    sens = [e.name for e in p.elements if e.sensitive]
    if sens:
        lines += ["", "    def sensitive_locators(self) -> list[Locator]:",
                  f"        return [{', '.join('self.' + n for n in sens)}]"]
    return "\n".join(lines).replace('"""', "") + "\n"


def paths_for(root: Path, app: str, module: str) -> tuple[Path, Path]:
    base = root / "apps" / app / "pages"
    return base / f"{module}.py", base / f"{module}.locators.yaml"


def load_generated(root: Path, app: str, class_name: str) -> GeneratedPage:
    module = re.sub(r"(?<!^)(?=[A-Z])", "_", class_name).lower()
    _, yml = paths_for(root, app, module)
    if not yml.exists():
        raise PomError(f"{class_name} is not a Studio-generated page (it is hand-written code)")
    return GeneratedPage.model_validate(yaml.safe_load(yml.read_text(encoding="utf-8")))


def save_generated(root: Path, page: GeneratedPage, allow_overwrite_code: bool = False) -> Path:
    py, yml = paths_for(root, page.app, page.module)
    if py.exists() and not py.read_text(encoding="utf-8").lstrip().startswith(GENERATED_MARK) \
            and not allow_overwrite_code:
        raise PomError(f"{py.relative_to(root)} is hand-written code; choose another page name")
    for pkg in (root / "apps" / page.app, root / "apps" / page.app / "pages"):
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "__init__.py").touch(exist_ok=True)
    data = page.model_dump(mode="json", exclude_none=True)
    yml.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    source = render(page)
    compile(source, str(py), "exec")  # never write a file that doesn't compile
    tmp = py.with_suffix(".tmp")
    tmp.write_text(source, encoding="utf-8")
    tmp.replace(py)
    return py


def add_elements(root: Path, page: GeneratedPage, new: list[ElementDef]) -> tuple[GeneratedPage, list[str]]:
    """Merge new elements; renames clashes (name_2) and skips exact duplicate locators."""
    existing_names = {e.name for e in page.elements}
    existing_locs = {e.locator for e in page.elements}
    notes = []
    for el in new:
        if el.locator in existing_locs:
            notes.append(f"{el.name}: same locator already on the page - skipped")
            continue
        name, n = el.name, 2
        while name in existing_names:
            name, n = f"{el.name}_{n}", n + 1
        if name != el.name:
            notes.append(f"{el.name} renamed to {name} (name already used)")
        page.elements.append(el.model_copy(update={"name": name}))
        existing_names.add(name)
        existing_locs.add(el.locator)
    return GeneratedPage.model_validate(page.model_dump()), notes
