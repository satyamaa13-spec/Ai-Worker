import html
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

INVOICES = [
    {"id": "INV-1107", "vendor": "Globex Corp", "amount": "1,240.50", "currency": "USD",
     "issued": "April 2, 2026", "due": "May 2, 2026", "status": "Open"},
    {"id": "INV-0990", "vendor": "Globex Corp", "amount": "2,115.00", "currency": "USD",
     "issued": "January 15, 2026", "due": "February 14, 2026", "status": "Paid"},
    {"id": "INV-2001", "vendor": "Initech LLC", "amount": "560.00", "currency": "USD",
     "issued": "May 20, 2026", "due": "June 19, 2026", "status": "Open"},
    {"id": "INV-1042", "vendor": "Globex Corp", "amount": "3,980.00", "currency": "USD",
     "issued": "May 5, 2026", "due": "June 4, 2026", "status": "Open"},
]
BASELINE = [dict(i) for i in INVOICES]

TESTPACK = [
    {"id": "INV-3001", "vendor": "Initech LLC", "amount": "800.00", "currency": "USD", "issued": "June 1, 2026", "due": "July 1, 2026", "status": "Open"},
    {"id": "INV-3002", "vendor": "Initech LLC", "amount": "950.00", "currency": "USD", "issued": "June 1, 2026", "due": "July 1, 2026", "status": "Open"},
    {"id": "INV-4001", "vendor": "Umbrella Co", "amount": "12,500.00", "currency": "USD", "issued": "June 3, 2026", "due": "July 3, 2026", "status": "Open"},
    {"id": "INV-5001", "vendor": "Soylent GmbH", "amount": "2,300.00", "currency": "EUR", "issued": "June 5, 2026", "due": "July 5, 2026", "status": "Open"},
    {"id": "CN-7001", "vendor": "Hooli Inc", "amount": "400.00", "currency": "USD", "issued": "June 8, 2026", "due": "July 8, 2026", "status": "Open"},
]

CUSTOM_FILE = Path(__file__).parent / "custom_invoices.json"
BASE_IDS = {i["id"] for i in INVOICES}
INV_LOCK = threading.Lock()


def _save_custom():
    """Writes admin-added invoices to the custom data file."""
    custom = [i for i in INVOICES if i["id"] not in BASE_IDS and i.get("source") != "agent"]
    if custom:
        CUSTOM_FILE.write_text(json.dumps(custom, indent=2))
    elif CUSTOM_FILE.exists():
        CUSTOM_FILE.unlink()


def _load_custom():
    """Loads admin-added invoices from the custom data file."""
    if CUSTOM_FILE.exists():
        try:
            for i in json.loads(CUSTOM_FILE.read_text()):
                if i["id"] not in {x["id"] for x in INVOICES}:
                    INVOICES.append(i)
        except Exception:
            pass


_load_custom()


def date_error(issued, due):
    """Returns an error message unless the issue date is before the due date."""
    try:
        d1, d2 = (datetime.strptime(x.strip(), "%B %d, %Y") for x in (issued, due))
    except ValueError:
        return "Dates must be written like June 10, 2026."
    return None if d1 < d2 else "Issue date should be before due date"


def reset_invoices():
    """Restores the built-in invoices plus any admin-added ones."""
    INVOICES[:] = [dict(i) for i in BASELINE]
    _load_custom()


class World:
    """The ERP's state: saved bills and the one-time failure switch."""
    def __init__(self, flaky=True):
        """Creates an empty bill list and arms the one-time failure."""
        self.flaky = flaky
        self.flaky_fired = False
        self.bills = []
        self.lock = threading.Lock()


STYLE = """
:root{--fg:#1f2937;--mut:#6b7280;--bd:#e5e7eb;--bg:#fff;--card:#fff;--acc:#2f5bea;--bad:#b42318;--ok:#127a46}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.nav{display:flex;gap:4px;align-items:center;padding:10px 24px;background:var(--card);border-bottom:1px solid var(--bd)}
.nav .brand{font-weight:600;margin-right:16px}
.nav a{padding:6px 12px;border-radius:8px;color:var(--mut);text-decoration:none}
.nav a:hover{background:var(--bg);color:var(--fg)}.nav a.on{background:#eef2ff;color:var(--acc);font-weight:500}
.wrap{max-width:860px;margin:24px auto;padding:0 24px}
h1{font-size:20px;font-weight:600;margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px 18px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--bd);border-radius:12px;overflow:hidden}
th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);text-align:left;background:#fafafb}
th,td{padding:10px 14px;border-bottom:1px solid var(--bd)}tr:last-child td{border-bottom:0}
tbody tr:hover td{background:#fafbff}
a{color:var(--acc)}
form.stack{display:grid;gap:12px;max-width:440px}
label{display:grid;gap:4px;font-size:12.5px;color:var(--mut)}
input,select{font:inherit;color:var(--fg);padding:8px 10px;border:1px solid var(--bd);border-radius:8px;background:#fff;width:100%}
input:focus,select:focus{outline:2px solid #c7d2fe;border-color:var(--acc)}
button,.btn{font:inherit;display:inline-block;padding:8px 16px;border-radius:8px;border:1px solid var(--bd);background:#fff;color:var(--fg);cursor:pointer;text-decoration:none}
button[type=submit]:not(.danger),.btn.primary{background:var(--acc);border-color:var(--acc);color:#fff}
button.danger{color:var(--bad);border-color:#f3c5c0;background:#fff}
.toolbar{display:flex;gap:12px;align-items:flex-end;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap}
.toolbar form{display:flex;gap:8px;align-items:flex-end}.toolbar label{min-width:200px}
.error{background:#fef3f2;border:1px solid #f3c5c0;color:var(--bad);padding:10px 14px;border-radius:8px;margin:0 0 16px}
.ok{background:#ecfdf3;border:1px solid #b7e4c7;color:var(--ok);padding:10px 14px;border-radius:8px;margin:0 0 16px}
.kv{display:grid;grid-template-columns:140px 1fr;gap:6px 16px;margin:0 0 16px}
.kv .k{color:var(--mut)}.badge{padding:2px 9px;border-radius:99px;background:#eef2ff;color:var(--acc);font-size:12px}
.mut{color:var(--mut)}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
"""


def page(title, body, active=None):
    """Wraps page content in the shared layout with the top navigation bar."""
    links = " ".join(f'<a href="{h}"{" class=on" if k == active else ""}>{t}</a>'
                    for k, h, t in (("portal", "/portal/invoices", "Vendor Portal"), ("erp", "/erp/bills", "Internal ERP")))
    return (f"<html><head><title>{html.escape(title)}</title><style>{STYLE}</style></head><body>"
            f'<div class="nav"><span class="brand">Saty Co</span> {links}</div>'
            f'<div class="wrap"><h1>{html.escape(title)}</h1>{body}</div></body></html>')


def admin_page(msg=""):
    """Builds the admin page for adding and resetting test invoices."""
    e = lambda x: html.escape(str(x))
    rows = "".join(f'<tr><td>{e(i["id"])}</td><td>{e(i["vendor"])}</td><td>{e(i["amount"])} {e(i["currency"])}</td>'
                   f'<td>{e(i["issued"])}</td><td>{e(i["due"])}</td><td>{e(i["status"])}</td>'
                   f'<td>{"built-in" if i["id"] in BASE_IDS else ("agent" if i.get("source") == "agent" else "added")}</td></tr>' for i in INVOICES)
    note = f'<p class="error">{e(msg)}</p>' if msg else ""
    return page("Admin: test data (not visible to the agent)", note + """<div class="card"><form class="stack" method="post" action="/admin/invoices">
<label>Invoice id (INV-1234 style) <input name="id"></label>
<label>Vendor <input name="vendor"></label>
<label>Amount (e.g. 1,875.25) <input name="amount"></label>
<label>Currency <select name="currency"><option value="USD">USD</option><option value="EUR">EUR</option></select></label>
<label>Issued (e.g. June 10, 2026) <input name="issued"></label>
<label>Due (e.g. July 10, 2026) <input name="due"></label>
<label>Status <select name="status"><option value="Open">Open</option><option value="Paid">Paid</option></select></label>
<button type="submit">Add invoice</button></form></div>
<div class="actions" style="margin:0 0 16px"><form method="post" action="/admin/testpack"><button type="submit">Load test pack (5 invoices)</button></form>
<form method="post" action="/admin/reset"><button class="danger" type="submit">Remove all added invoices</button></form></div>
<table><tr><th>Id</th><th>Vendor</th><th>Amount</th><th>Issued</th><th>Due</th><th>Status</th><th>Source</th></tr>""" + rows + "</table>")


def invoice_form(error=None, v=None):
    """Builds the portal's add-invoice form, with an optional error message."""
    v = v or {}
    val = lambda k: html.escape(v.get(k, ""))
    err = f'<p class="error">ERROR: {html.escape(error)}</p>' if error else ""
    sel = lambda k, opts: "".join(f'<option value="{o}"{" selected" if v.get(k) == o else ""}>{o}</option>' for o in opts)
    return err + f"""<div class="card"><form class="stack" method="post" action="/portal/invoices">
<label>Invoice id (e.g. INV-1234) <input name="id" value="{val('id')}"></label>
<label>Vendor <input name="vendor" value="{val('vendor')}"></label>
<label>Amount (e.g. 1,875.25) <input name="amount" value="{val('amount')}"></label>
<label>Currency <select name="currency">{sel('currency', ('USD', 'EUR'))}</select></label>
<label>Issue date (e.g. June 10, 2026) <input name="issued" value="{val('issued')}"></label>
<label>Due date (e.g. July 10, 2026) <input name="due" value="{val('due')}"></label>
<label>Status <select name="status">{sel('status', ('Open', 'Paid'))}</select></label>
<button type="submit">Add invoice</button></form></div>"""


def bill_form(error=None, v=None):
    """Builds the ERP's new-bill form, with an optional error message."""
    v = v or {}
    err = f'<p class="error">ERROR: {html.escape(error)}</p>' if error else ""
    val = lambda k: html.escape(v.get(k, ""))
    return err + f"""<div class="card"><form class="stack" method="post" action="/erp/bills">
<label>Vendor <input name="vendor" value="{val('vendor')}"></label>
<label>Invoice number <input name="invoice_number" value="{val('invoice_number')}"></label>
<label>Amount (digits and decimal point only) <input name="amount" value="{val('amount')}"></label>
<label>Currency <select name="currency"><option value="USD">USD</option><option value="EUR">EUR</option></select></label>
<label>Due date (YYYY-MM-DD) <input name="due_date" value="{val('due_date')}"></label>
<button type="submit">Save bill</button></form></div>"""


class Handler(BaseHTTPRequestHandler):
    """Handles all requests to the simulated company."""
    world: World = None

    def log_message(self, *a):
        """Silences the default request logging."""
        pass

    def _send(self, code, body, headers=None):
        """Sends an HTML response with the given status code."""
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, val in (headers or {}).items():
            self.send_header(k, val)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, loc, headers=None):
        """Sends a redirect to another path."""
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        for k, val in (headers or {}).items():
            self.send_header(k, val)
        self.end_headers()

    def do_GET(self):
        """Serves the portal, ERP and admin pages."""
        u = urlparse(self.path)
        path, q = u.path.rstrip("/") or "/", parse_qs(u.query)
        w = self.world
        if path == "/":
            return self._redirect("/portal/invoices")
        if path == "/admin":
            return self._send(200, admin_page())
        if path == "/portal/invoices":
            vendor = (q.get("vendor", [""])[0]).lower()
            rows = "".join(
                f'<tr><td><a href="/portal/invoice/{i["id"]}">{i["id"]}</a></td><td>{html.escape(i["vendor"])}</td>'
                f'<td>{i["issued"]}</td><td>{i["amount"]} {i["currency"]}</td><td>{html.escape(i["status"])}</td></tr>'
                for i in INVOICES if vendor in i["vendor"].lower()) or '<tr><td colspan="5" class="mut">No invoices found</td></tr>'
            return self._send(200, page("Invoices", '<div class="toolbar"><form method="get" action="/portal/invoices"><label>Vendor filter '
                f'<input name="vendor" value="{html.escape(vendor)}"></label><button type="submit">Search</button></form>'
                '<a class="btn primary" href="/portal/invoices/new">Add invoice</a></div>'
                f"<table><tr><th>Invoice</th><th>Vendor</th><th>Issued</th><th>Amount</th><th>Status</th></tr>{rows}</table>", "portal"))
        if path == "/portal/invoices/new":
            return self._send(200, page("Add invoice", invoice_form(), "portal"))
        m = re.fullmatch(r"/portal/invoice/([\w-]+)", path)
        if m:
            inv = next((i for i in INVOICES if i["id"] == m.group(1)), None)
            if not inv:
                return self._send(404, page("Not found", "<p>No such invoice.</p>", "portal"))
            body = '<div class="card"><div class="kv">' + "".join(f'<div class="kvrow"><span class="k">{k}:</span> <span>{html.escape(v)}</span></div>' for k, v in
                           [("Invoice number", inv["id"]), ("Vendor", inv["vendor"]), ("Amount", inv["amount"]),
                            ("Currency", inv["currency"]), ("Issue date", inv["issued"]), ("Due date", inv["due"]), ("Status", inv["status"])]) + "</div>" \
                   f'<form method="post" action="/portal/invoice/{inv["id"]}/delete"><button class="danger" type="submit">Delete invoice</button></form></div>' \
                   '<p><a href="/portal/invoices">&larr; All invoices</a></p>'
            return self._send(200, page(f"Invoice {inv['id']}", body, "portal"))
        if path == "/erp/bills":
            with w.lock:
                rows = "".join(f'<tr><td>{b["invoice_number"]}</td><td>{html.escape(b["vendor"])}</td><td>{b["amount"]} {b["currency"]}</td>'
                               f'<td>{b["due_date"]}</td></tr>' for b in w.bills) or "<tr><td colspan=4>No bills yet</td></tr>"
            return self._send(200, page("ERP - Bills", '<div class="toolbar"><span></span><a class="btn primary" href="/erp/bills/new">New bill</a></div>'
                f"<table><tr><th>Invoice</th><th>Vendor</th><th>Amount</th><th>Due</th></tr>{rows}</table>", "erp"))
        if path == "/erp/bills/new":
            return self._send(200, page("ERP - New bill", bill_form(), "erp"))
        return self._send(404, page("Not found", "<p>404</p>"))

    def do_POST(self):
        """Handles adding and deleting invoices, saving bills and admin actions."""
        n = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        path = urlparse(self.path).path.rstrip("/")
        w = self.world
        if path.startswith("/admin"):
            with INV_LOCK:
                if path == "/admin/invoices":
                    inv = {k: form.get(k, "").strip() for k in ("id", "vendor", "amount", "currency", "issued", "due", "status")}
                    missing = [k for k in ("id", "vendor", "amount", "issued", "due") if not inv[k]]
                    if missing:
                        return self._send(422, admin_page(f"Missing: {', '.join(missing)}"))
                    derr = date_error(inv["issued"], inv["due"])
                    if derr:
                        return self._send(422, admin_page(derr))
                    if any(i["id"] == inv["id"] for i in INVOICES):
                        return self._send(409, admin_page(f"Invoice id {inv['id']} already exists."))
                    INVOICES.append(inv)
                elif path == "/admin/testpack":
                    have = {i["id"] for i in INVOICES}
                    INVOICES.extend(dict(t) for t in TESTPACK if t["id"] not in have)
                elif path == "/admin/reset":
                    INVOICES[:] = [dict(i) for i in BASELINE]
                else:
                    return self._send(404, page("Not found", "<p>404</p>"))
                _save_custom()
            return self._redirect("/admin")
        if path == "/portal/invoices":
            inv = {k: form.get(k, "").strip() for k in ("id", "vendor", "amount", "currency", "issued", "due", "status")}
            inv["currency"], inv["status"] = inv["currency"] or "USD", inv["status"] or "Open"
            err = None
            missing = [k for k in ("id", "vendor", "amount", "issued", "due") if not inv[k]]
            if missing:
                err = f"Missing required field(s): {', '.join(missing)}."
            elif not re.fullmatch(r"[\w-]+", inv["id"]):
                err = "Invoice id may only contain letters, digits, '-' and '_'."
            elif not re.fullmatch(r"\d{1,3}(,\d{3})*(\.\d{2})?|\d+(\.\d{2})?", inv["amount"]):
                err = "Amount must look like 1,875.25 (digits, optional thousands commas, optional 2-decimal cents)."
            else:
                err = date_error(inv["issued"], inv["due"])
            if err:
                return self._send(422, page("Add invoice", invoice_form(err, inv), "portal"))
            with INV_LOCK:
                if any(i["id"] == inv["id"] for i in INVOICES):
                    return self._send(409, page("Add invoice", invoice_form(f"Invoice {inv['id']} already exists.", inv), "portal"))
                INVOICES.append({**inv, "source": "agent"})
            return self._send(200, page("Invoice added", f'<p class="ok">Invoice {html.escape(inv["id"])} added.</p>'
                                        f'<p><a href="/portal/invoice/{inv["id"]}">View invoice</a> | <a href="/portal/invoices">All invoices</a></p>', "portal"))
        m = re.fullmatch(r"/portal/invoice/([\w-]+)/delete", path)
        if m:
            with INV_LOCK:
                inv = next((i for i in INVOICES if i["id"] == m.group(1)), None)
                if not inv:
                    return self._send(404, page("Not found", "<p>No such invoice.</p>", "portal"))
                INVOICES.remove(inv)
            return self._send(200, page("Invoice deleted", f'<p class="ok">Invoice {html.escape(inv["id"])} deleted.</p>'
                                        '<p><a href="/portal/invoices">All invoices</a></p>', "portal"))
        if path == "/erp/bills":
            err = None
            if not form.get("vendor", "").strip():
                err = "Vendor is required."
            elif not re.fullmatch(r"INV-\d+", form.get("invoice_number", "")):
                err = "Invoice number must look like INV-1234."
            elif not re.fullmatch(r"\d+(\.\d{1,2})?", form.get("amount", "")):
                err = "Amount must contain only digits and an optional decimal point (no commas or currency symbols)."
            elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", form.get("due_date", "")):
                err = "Due date must be formatted YYYY-MM-DD."
            if err:
                return self._send(422, page("ERP - New bill", bill_form(err, form), "erp"))
            with w.lock:
                if any(b["invoice_number"] == form["invoice_number"] for b in w.bills):
                    return self._send(409, page("ERP - Conflict", f'<p class="error">Bill {form["invoice_number"]} already exists.</p>', "erp"))
                w.bills.append({k: form.get(k, "") for k in ("vendor", "invoice_number", "amount", "currency", "due_date")})
                if w.flaky and not w.flaky_fired:
                    w.flaky_fired = True
                    return self._send(500, page("Server error", '<p class="error">Internal Server Error: upstream timeout.</p>', "erp"))
            return self._send(200, page("ERP - Bill saved", f'<p class="ok">Bill {form["invoice_number"]} saved.</p><p><a href="/erp/bills">Back to bills</a></p>', "erp"))
        return self._send(404, page("Not found", "<p>404</p>"))


def start_server(flaky=True, port=0):
    """Starts the simulated company on a free port and returns the server, state and URL."""
    reset_invoices()
    world = World(flaky)
    handler = type("H", (Handler,), {"world": world})
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, world, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    srv, _, base = start_server(port=8000)
    print("Sandbox running at", base, "(Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
