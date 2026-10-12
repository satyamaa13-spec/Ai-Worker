import argparse
import json
import re
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sandbox.server import INVOICES, reset_invoices, start_server
from worker.agent import Agent
from worker.browser import Browser
from worker.llm import AnthropicLLM, GeminiLLM, ScriptedLLM
from worker.tools import build_tools

ROOT = Path(__file__).parent
WORKSPACE = ROOT / "sandbox" / "workspace"
LOCK = threading.Lock()
USERS = {"Satyam": "12345678"}
SESSIONS = set()


class Cancelled(Exception):
    """Raised inside the run when the user presses Stop."""
    pass


class WebIO:
    """Browser version of the terminal I/O: collects events and waits for approvals and answers from the page."""

    def __init__(self, auto_approve=False):
        """Sets up the event list and the waiting state for one run."""
        self.events, self.pending, self.auto_approve, self.cancelled = [], None, auto_approve, False
        self._ev, self._answer = threading.Event(), None

    def log(self, kind, data):
        """Records an event for the page to show, or stops the run if it was cancelled."""
        if self.cancelled:
            raise Cancelled()
        self.events.append({"kind": kind, **data})

    def _wait(self, pending):
        """Blocks the worker thread until the page answers or the run is cancelled."""
        self._ev.clear()
        self._answer, self.pending = None, pending
        self._ev.wait()
        self.pending = None
        if self.cancelled:
            raise Cancelled()
        return self._answer

    def ask(self, question):
        """Shows a question to the user and returns their typed answer."""
        self.events.append({"kind": "ask", "question": question})
        a = str(self._wait({"type": "ask", "text": question}))
        self.events.append({"kind": "answer", "text": a})
        return a

    def approve(self, description):
        """Shows an approval card and returns True if the user approves (or auto-approve is on)."""
        if self.auto_approve:
            self.events.append({"kind": "note", "text": f"auto-approved: {description}"})
            return True
        self.events.append({"kind": "approval", "text": description})
        ok = bool(self._wait({"type": "approve", "text": description}))
        self.events.append({"kind": "note", "text": "approved by user" if ok else "DENIED by user"})
        return ok

    def answer(self, value):
        """Receives the user's answer from the page and wakes the waiting worker."""
        self._answer = value
        self._ev.set()

    def cancel(self):
        """Marks the run as cancelled and wakes any wait so the run can stop."""
        self.cancelled = True
        self._ev.set()


class Job:
    """Holds the current run state and the shared simulated company."""
    def __init__(self):
        """Creates an empty job with no run and no sandbox yet."""
        self.id = 0
        self.status = "idle"
        self.io = WebIO()
        self.srv = self.world = self.base = self.run_dir = None

    def new_sandbox(self):
        """Starts the simulated company server once, when the app starts."""
        self.srv, self.world, self.base = start_server(flaky=True)

    def reset_data(self):
        """Restores the demo invoices and clears all ERP bills."""
        reset_invoices()
        with self.world.lock:
            self.world.bills.clear()
        self.world.flaky_fired = False


JOB = Job()
JOB.new_sandbox()

FIXED = {"vendor": "Globex Corp", "invoice_number": "INV-1042", "amount": "3980.00", "currency": "USD", "due_date": "2026-06-04"}


def replay_script(base):
    """Returns a fixed list of tool calls that previews the UI without any AI."""
    return [
        ("[replay] opening portal", [("open_page", {"url": f"{base}/portal/invoices"})]),
        ("[replay] opening newest invoice", [("open_page", {"url": f"{base}/portal/invoice/INV-1042"})]),
        ("[replay] pinning facts", [("remember", {"key": "invoice", "value": "INV-1042, 3,980.00 USD, due June 4, 2026"})]),
        ("[replay] opening ERP form", [("open_page", {"url": f"{base}/erp/bills/new"})]),
        ("[replay] first attempt with portal formatting", [("submit_form", {"form_index": 0, "values": {**FIXED, "amount": "3,980.00", "due_date": "June 4, 2026"}})]),
        ("[replay] fixing formats", [("submit_form", {"form_index": 0, "values": FIXED})]),
        ("[replay] verifying in ERP", [("open_page", {"url": f"{base}/erp/bills"})]),
        ("[replay] done", [("finish", {"status": "done", "summary": "Bill INV-1042 entered (scripted replay, not AI).",
                                       "evidence": ["ERP list shows INV-1042 3980.00 USD due 2026-06-04"]})]),
    ]


def start_job(task, provider, model, auto_approve, max_calls=None, verifier=True):
    """Validates the request, then starts the agent on a background thread."""
    with LOCK:
        if JOB.status == "running":
            return False, "A run is already in progress."
        JOB.world.flaky_fired = False
        JOB.id += 1
        JOB.io = io = WebIO(auto_approve)
        JOB.status = "running"
        JOB.run_dir = ROOT / "runs" / time.strftime("%Y%m%d-%H%M%S")
        base, run_dir = JOB.base, JOB.run_dir
    context = (f"Vendor portal (invoices): {base}/portal/invoices\n"
               f"Internal ERP (bills): {base}/erp/bills\n"
               f"The vendor portal can also add invoices ({base}/portal/invoices/new) and delete them (button on each invoice page).\n"
               "Workspace files (inbox notes) via list_dir/read_file.")

    def work():
        """Builds the browser, tools and model, runs the agent and records how the run ended."""
        try:
            memory = {}
            tools = build_tools(Browser({urlparse(base).netloc}), WORKSPACE, memory, io)
            llm = ScriptedLLM(replay_script(base)) if provider == "replay" else (GeminiLLM if provider == "gemini" else AnthropicLLM)(model or None)
            res = Agent(llm, tools, memory, io, context, 40, run_dir, max_llm_calls=max_calls, llm_verifier=verifier).run(task)
            io.events.append({"kind": "result", **res.__dict__})
            JOB.status = "finished"
        except Cancelled:
            io.events.append({"kind": "error", "text": "Stopped by user."})
            JOB.status = "stopped"
        except Exception as e:
            io.events.append({"kind": "error", "text": f"{type(e).__name__}: {e}"})
            JOB.status = "failed"

    threading.Thread(target=work, daemon=True).start()
    return True, ""


class Handler(BaseHTTPRequestHandler):
    """Handles all web requests for the app."""
    def log_message(self, *a):
        """Silences the default request logging."""
        pass

    def _json(self, obj, code=200):
        """Sends a JSON response with the given status code."""
        data = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        """Returns True if the request carries a valid login cookie."""
        m = re.search(r"wsid=(\w+)", self.headers.get("Cookie", ""))
        return bool(m and m.group(1) in SESSIONS)

    def _html(self, name):
        """Sends one of the HTML pages from the ui folder."""
        data = (ROOT / "ui" / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        """Serves the login or agent page and the state API."""
        u = urlparse(self.path)
        if u.path == "/":
            self._html("index.html" if self._authed() else "login.html")
        elif u.path.startswith("/api/") and not self._authed():
            self._json({"error": "login required"}, 401)
        elif u.path == "/api/state":
            since = int(parse_qs(u.query).get("since", ["0"])[0])
            evs = JOB.io.events
            self._json({"run_id": JOB.id, "status": JOB.status, "events": evs[since:], "next": len(evs),
                        "pending": JOB.io.pending, "sandbox_url": JOB.base, "bills": list(JOB.world.bills), "invoices": [dict(i) for i in INVOICES],
                        "run_dir": str(JOB.run_dir) if JOB.run_dir else None})
        else:
            self.send_error(404)

    def do_POST(self):
        """Handles login, logout, run, answer, reset and stop requests."""
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/login":
            if USERS.get(body.get("username", "")) == body.get("password"):
                sid = secrets.token_hex(16)
                SESSIONS.add(sid)
                data = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", f"wsid={sid}; Path=/; HttpOnly; SameSite=Lax")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json({"ok": False, "error": "Invalid username or password."}, 401)
            return
        if self.path == "/api/logout":
            m = re.search(r"wsid=(\w+)", self.headers.get("Cookie", ""))
            SESSIONS.discard(m.group(1) if m else None)
            self._json({"ok": True})
            return
        if not self._authed():
            self._json({"error": "login required"}, 401)
            return
        if self.path == "/api/run":
            ok, err = start_job(body.get("task", "").strip(), body.get("provider", "gemini"), body.get("model", ""), bool(body.get("auto_approve")),
                                int(body["max_calls"]) if body.get("max_calls") else None, bool(body.get("verifier", True)))
            self._json({"ok": ok, "error": err}, 200 if ok else 409)
        elif self.path == "/api/answer":
            JOB.io.answer(body.get("value"))
            self._json({"ok": True})
        elif self.path == "/api/reset":
            if JOB.status == "running":
                self._json({"ok": False, "error": "Stop the current run first."}, 409)
            else:
                JOB.reset_data()
                self._json({"ok": True})
        elif self.path == "/api/stop":
            JOB.io.cancel()
            self._json({"ok": True})
        else:
            self.send_error(404)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}"
    print(f"Worker UI running at {url}  (Ctrl+C to stop)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
