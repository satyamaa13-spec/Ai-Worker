import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

from .browser import Browser, ToolError


@dataclass
class ToolResult:
    """What a tool returns: text, success flag, and whether it read or wrote."""
    text: str
    ok: bool = True
    wrote: bool = False
    read: bool = False


@dataclass
class Tool:
    """A tool the model can call: name, description, input schema, function and risk rule."""
    name: str
    description: str
    schema: dict
    fn: Callable[[dict], ToolResult]
    risk: Optional[Callable[[dict], Optional[Tuple[str, str]]]] = None

    def spec(self):
        """Returns the tool description in the format the model API expects."""
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


def _obj(props, required):
    """Builds a JSON schema for an object with the given properties."""
    return {"type": "object", "properties": props, "required": required}


def build_tools(browser: Browser, workspace: Path, memory: dict, io):
    """Creates the tools the model may use, bound to one browser and workspace."""
    root = workspace.resolve()

    def safe(p):
        """Resolves a workspace path and rejects paths that leave the workspace."""
        full = (root / p).resolve()
        if root != full and root not in full.parents:
            raise ToolError("Path escapes the workspace.")
        return full

    def page_result(snap, write=False):
        """Wraps a page snapshot as a tool result."""
        return ToolResult(snap.render(), ok=snap.status < 400, wrote=write, read=not write)

    def open_page(a):
        """Opens a URL in the browser."""
        return page_result(browser.open(a["url"]))

    def click_link(a):
        """Follows a numbered link on the current page."""
        return page_result(browser.click(int(a["index"])))

    def submit_form(a):
        """Submits a numbered form on the current page."""
        idx = int(a["form_index"])
        method = browser.snap.forms[idx]["method"] if browser.snap and 0 <= idx < len(browser.snap.forms) else None
        return page_result(browser.submit(idx, a.get("values") or {}), write=(method == "post"))

    def submit_risk(a):
        """Decides whether a form submission needs approval and describes it."""
        try:
            form = browser.snap.forms[int(a["form_index"])]
        except Exception:
            return None
        if form["method"] != "post":
            return None
        vals = {f["name"]: f["value"] for f in form["fields"]}
        vals.update({k: str(v) for k, v in (a.get("values") or {}).items()})
        if re.search(r"delete|remove|pay|transfer|cancel", form["action"], re.I):
            facts = "; ".join(l for l in (browser.snap.text.splitlines() if browser.snap else [])
                              if re.match(r"(Invoice number|Vendor|Amount|Status):", l))
            shown = f" | record on this page: {facts}" if facts else ""
            return f"Irreversible-looking action: POST {form['action']}{shown}", f"{form['action']}|{sorted(vals.items())}"
        return None

    def list_dir(a):
        """Lists files in the workspace."""
        d = safe(a.get("path", "."))
        if not d.is_dir():
            raise ToolError("Not a directory.")
        return ToolResult("\n".join(sorted(str(p.relative_to(root)) + ("/" if p.is_dir() else "") for p in d.rglob("*"))) or "(empty)", read=True)

    def read_file(a):
        """Reads a text file from the workspace."""
        f = safe(a["path"])
        if not f.is_file():
            raise ToolError("No such file.")
        return ToolResult(f.read_text(errors="replace")[:6000], read=True)

    def remember(a):
        """Saves a fact in working memory."""
        memory[a["key"]] = a["value"]
        return ToolResult(f"Saved note '{a['key']}'.")

    def ask_user(a):
        """Asks the user a question and returns the answer."""
        return ToolResult(f"User answered: {io.ask(a['question'])}")

    S = lambda d: {"type": "string", "description": d}
    tools = [
        Tool("open_page", "Open a URL in the browser. Returns page text, numbered links and numbered forms.",
             _obj({"url": S("Absolute or relative URL")}, ["url"]), open_page),
        Tool("click_link", "Follow a numbered link from the current page.",
             _obj({"index": {"type": "integer"}}, ["index"]), click_link),
        Tool("submit_form", "Submit a numbered form on the current page. 'values' overrides field values by field name; "
             "unspecified fields keep their defaults. POST submissions change state and may require user approval.",
             _obj({"form_index": {"type": "integer"}, "values": {"type": "object", "additionalProperties": {"type": "string"}}},
                  ["form_index"]), submit_form, risk=submit_risk),
        Tool("list_dir", "List files in the workspace (default: all).", _obj({"path": S("Relative path")}, []), list_dir),
        Tool("read_file", "Read a text file from the workspace.", _obj({"path": S("Relative path")}, ["path"]), read_file),
        Tool("remember", "Pin a fact in working memory. Memory is re-shown every step even after old observations are trimmed.",
             _obj({"key": S("Short name"), "value": S("The fact")}, ["key", "value"]), remember),
        Tool("ask_user", "Ask the user a question when you cannot safely proceed (ambiguity, missing info/access).",
             _obj({"question": S("A specific question")}, ["question"]), ask_user),
    ]
    return {t.name: t for t in tools}
