"""Catalogue of no-code actions ("keywords").

Pure metadata - no browser imports - so Studio can render forms and validate tests, and the
runner (qaforge.nocode.runner) implements each keyword. Adding a keyword = add metadata here
plus one function in the runner.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

RULES = ["equals", "equals_ci", "decimal", "contains", "regex", "exists", "one_of"]


@dataclass
class Opt:
    name: str
    label: str
    kind: str = "text"  # text | bool | number | select | rows | kv | app | role | flow | mapping | factory | spec
    required: bool = False
    choices: list[str] | None = None
    help: str = ""
    default: Any = None


@dataclass
class Keyword:
    name: str
    label: str
    group: str
    help: str
    target: str | None = None  # None | "element" | "page" | "grid"
    value: str | None = None  # label for the value field, None = not used
    value_required: bool = False
    save_as: str | None = None  # None | "optional" | "required"
    options: list[Opt] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_ROWS_HELP = "One line per field: what to read, what it must be, how to compare."

KEYWORDS: list[Keyword] = [
    # -- session ------------------------------------------------------------
    Keyword("open_app", "Open app and sign in", "Session",
            "Opens the application in a fresh browser and signs in through Okta as the chosen role.",
            options=[Opt("app", "Application", "app", required=True),
                     Opt("role", "Sign in as", "role", help="Leave empty for apps without sign-in"),
                     Opt("as", "Session name", help="Only needed when one test uses the same app twice")]),
    Keyword("switch_app", "Switch to app", "Session", "Continue in an app session opened earlier in this test.",
            options=[Opt("session", "Session name", required=True)]),
    # -- navigate -----------------------------------------------------------
    Keyword("open_page", "Open page", "Navigate",
            "Goes to the page's address and waits until it has loaded. Pages without an address are "
            "only waited for (reach them by clicking).", target="page"),
    Keyword("goto_url", "Go to address", "Navigate", "Opens an address. A path like /payments uses the app's address.",
            value="Address or path", value_required=True),
    # -- interact -----------------------------------------------------------
    Keyword("click", "Click", "Interact", "Clicks the element.", target="element"),
    Keyword("fill", "Type into", "Interact", "Clears the field and types the value.", target="element",
            value="Text to type", value_required=True,
            options=[Opt("sensitive", "Hide value in evidence", "bool",
                         help="Use for passwords, PINs, card numbers. ${secret:NAME} values are always hidden.")]),
    Keyword("select", "Choose option", "Interact", "Chooses an option in a dropdown.", target="element",
            value="Option", value_required=True,
            options=[Opt("by", "Match option by", "select", choices=["label", "value"], default="label")]),
    Keyword("check", "Tick", "Interact", "Ticks a checkbox or radio button.", target="element"),
    Keyword("uncheck", "Untick", "Interact", "Unticks a checkbox.", target="element"),
    Keyword("press", "Press key", "Interact", "Presses a key while the element has focus.", target="element",
            value="Key, e.g. Enter or Tab", value_required=True),
    Keyword("upload", "Upload file", "Interact", "Attaches a file from testdata/files.", target="element",
            value="File name in testdata/files", value_required=True),
    # -- check --------------------------------------------------------------
    Keyword("expect_visible", "Check shown", "Check", "Passes when the element is visible.", target="element"),
    Keyword("expect_hidden", "Check not shown", "Check", "Passes when the element is hidden or absent.",
            target="element"),
    Keyword("expect_text", "Check text", "Check", "Passes when the element shows the text.", target="element",
            value="Expected text", value_required=True,
            options=[Opt("exact", "Whole text must match", "bool")]),
    Keyword("expect_value", "Check field value", "Check", "Passes when an input holds the value.", target="element",
            value="Expected value", value_required=True),
    Keyword("expect_url", "Check address", "Check", "Passes when the page address matches (regular expression).",
            value="Address pattern", value_required=True),
    Keyword("assert_value", "Compare values", "Check", "Compares any value with an expected value.",
            value="Value", value_required=True,
            options=[Opt("expected", "Expected", required=False),
                     Opt("rule", "Compare as", "select", choices=RULES, default="equals")]),
    Keyword("verify_values", "Check saved data", "Check",
            "Checks fields of saved data (for example a grid row read earlier) against expected values.",
            options=[Opt("source", "Saved data name", required=True),
                     Opt("rows", "Fields to check", "rows", required=True, help=_ROWS_HELP)]),
    # -- capture & data -----------------------------------------------------
    Keyword("capture_text", "Remember text", "Data", "Reads the text an element shows and remembers it.",
            target="element", save_as="required"),
    Keyword("capture_value", "Remember field value", "Data", "Reads an input's value and remembers it.",
            target="element", save_as="required"),
    Keyword("read_grid_row", "Read table row", "Data",
            "Finds a table row by column values and remembers the whole row.", target="grid",
            value="Find row where", value_required=True, save_as="required"),
    Keyword("generate_data", "Generate test data", "Data", "Creates synthetic, format-valid data.",
            save_as="required",
            options=[Opt("factory", "Kind of data", "factory", required=True),
                     Opt("overrides", "Fixed values", "kv", help="One per line: field=value")]),
    Keyword("set_value", "Remember value", "Data", "Remembers a value under a name for later steps.",
            value="Value", value_required=True, save_as="required"),
    Keyword("mark_created", "Record in ledger", "Data",
            "Records data the app has confirmed as created, so later tests can reuse it.",
            options=[Opt("key", "Saved data name", required=True),
                     Opt("entity", "Record type", required=True, help="e.g. beneficiary, customer")]),
    # -- downstream ---------------------------------------------------------
    Keyword("verify_event", "Check Kafka event", "Downstream",
            "Waits for the event and checks its fields against what the test entered.",
            options=[Opt("topic", "Topic", required=True),
                     Opt("key", "Message key", help="Usually a reference captured earlier, e.g. ${txn_ref}"),
                     Opt("mapping", "Saved field check", "mapping"),
                     Opt("rows", "Or fields to check", "rows", help=_ROWS_HELP),
                     Opt("timeout_s", "Wait up to (seconds)", "number")]),
    Keyword("expect_no_event", "Check no Kafka event", "Downstream",
            "Passes when no matching event is published within the time window.",
            options=[Opt("topic", "Topic", required=True),
                     Opt("key", "Message key"),
                     Opt("field", "Event field"), Opt("equals", "Equals"),
                     Opt("within_s", "Watch for (seconds)", "number", default=10)]),
    # -- reuse --------------------------------------------------------------
    Keyword("use_steps", "Use reusable steps", "Reuse",
            "Runs the steps of a reusable test here, sharing this test's browser and saved data.",
            options=[Opt("spec", "Reusable steps", "spec", required=True)]),
    Keyword("run_flow", "Run business flow", "Reuse",
            "Runs a business flow written by the automation team (for example 'add beneficiary').",
            save_as="optional",
            options=[Opt("flow", "Flow", "flow", required=True),
                     Opt("args", "Inputs", "kv", help="One per line: input=value, e.g. payee=${payee}")]),
    # -- utility ------------------------------------------------------------
    Keyword("checkpoint", "Evidence screenshot", "Utility", "Adds a labelled screenshot to the evidence.",
            value="Label", value_required=True),
    Keyword("note", "Add note", "Utility", "Adds a note to the evidence report.", value="Note",
            value_required=True),
    Keyword("wait", "Wait", "Utility",
            "Pauses (maximum 30 seconds). Prefer 'Check shown' - it waits automatically.",
            value="Seconds", value_required=True),
]

BY_NAME: dict[str, Keyword] = {k.name: k for k in KEYWORDS}
GROUPS = list(dict.fromkeys(k.group for k in KEYWORDS))


def validate_step(step: dict[str, Any]) -> list[str]:
    """Human-readable problems with one step (empty list = fine)."""
    kw = BY_NAME.get(step.get("action", ""))
    if kw is None:
        return [f"Unknown action '{step.get('action')}'"]
    problems = []
    if kw.target and not (step.get("target") or step.get("locator")):
        problems.append(f"'{kw.label}' needs a {'page' if kw.target == 'page' else 'screen element'}")
    if kw.target == "page" and step.get("locator"):
        problems.append("'Open page' needs a page, not a locator")
    if kw.value_required and step.get("value") in (None, ""):
        problems.append(f"'{kw.label}' needs: {kw.value}")
    if kw.save_as == "required" and not step.get("save_as"):
        problems.append(f"'{kw.label}' needs a name to remember the result under")
    opts = step.get("options") or {}
    for o in kw.options:
        if o.required and opts.get(o.name) in (None, "", [], {}):
            problems.append(f"'{kw.label}' needs: {o.label}")
    if kw.name == "verify_event" and not (opts.get("mapping") or opts.get("rows")):
        problems.append("'Check Kafka event' needs a saved field check or fields to check")
    return problems
