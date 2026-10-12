import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from sandbox.server import start_server
from worker.agent import Agent
from worker.browser import Browser
from worker.llm import ScriptedLLM
from worker.tools import build_tools

WS = Path(__file__).parent.parent / "sandbox" / "workspace"


class IO:
    """Fake user channel that records logs and approvals."""
    def __init__(self, approve=True):
        """Stores the approval answer and empty log lists."""
        self.approve_answer, self.approvals, self.logs = approve, [], []

    def log(self, kind, data):
        """Records an event."""
        self.logs.append((kind, data))

    def ask(self, q):
        """Returns a fixed answer to any question."""
        return "n/a"

    def approve(self, d):
        """Records the approval request and returns the configured answer."""
        self.approvals.append(d)
        return self.approve_answer


def build(script, flaky=True, approve=True, verdict='{"achieved": true, "reason": "ok"}'):
    """Starts a sandbox and an agent driven by a scripted model."""
    srv, world, base = start_server(flaky=flaky)
    mem, io = {}, IO(approve)
    tools = build_tools(Browser({urlparse(base).netloc}), WS, mem, io)
    agent = Agent(ScriptedLLM(script(base), verdict), tools, mem, io, run_dir=tempfile.mkdtemp(), max_steps=20)
    return srv, world, io, agent


FIXED = {"vendor": "Globex Corp", "invoice_number": "INV-1042", "amount": "3980.00", "currency": "USD", "due_date": "2026-06-04"}


def happy(base):
    """Scripted turns for entering the newest Globex invoice as an ERP bill."""
    return [
        ("open portal", [("open_page", {"url": f"{base}/portal/invoices"})]),
        ("open newest", [("open_page", {"url": f"{base}/portal/invoice/INV-1042"})]),
        ("pin", [("remember", {"key": "invoice", "value": "INV-1042 3,980.00 due June 4, 2026"})]),
        ("erp form", [("open_page", {"url": f"{base}/erp/bills/new"})]),
        ("first try, portal formatting", [("submit_form", {"form_index": 0, "values": {**FIXED, "amount": "3,980.00", "due_date": "June 4, 2026"}})]),
        ("fix formats", [("submit_form", {"form_index": 0, "values": FIXED})]),
        ("finish too early", [("finish", {"status": "done", "summary": "saved", "evidence": ["x"]})]),
        ("verify", [("open_page", {"url": f"{base}/erp/bills"})]),
        ("finish", [("finish", {"status": "done", "summary": "Bill INV-1042 entered.", "evidence": ["ERP list shows INV-1042 3980.00 USD due 2026-06-04"]})]),
    ]


class HarnessTests(unittest.TestCase):
    """Tests for recovery, verification and input handling."""
    def test_recovers_from_validation_error_and_commit_then_500(self):
        """Agent recovers from a format error and a save-then-error without duplicating the bill."""
        srv, world, io, agent = build(happy)
        r = agent.run("enter newest Globex invoice")
        srv.shutdown(); srv.server_close()
        self.assertEqual(r.status, "done")
        self.assertTrue(r.verified)
        self.assertEqual(world.bills, [FIXED])
        self.assertEqual(io.approvals, [])
        self.assertTrue(any("REJECTED: you sent" in d["text"] for k, d in io.logs if k == "note"))
        self.assertIn("invoice", r.memory)

    def test_verifier_can_reject_a_false_claim(self):
        """A verifier that disagrees stops a false done claim."""
        def script(base):
            """Scripted turns for this test."""
            return [("claim", [("finish", {"status": "done", "summary": "done", "evidence": []})]),
                    ("give up", [("finish", {"status": "failed", "summary": "could not", "evidence": []})])]
        srv, world, io, agent = build(script, verdict='{"achieved": false, "reason": "no bill exists"}')
        r = agent.run("enter bill")
        srv.shutdown(); srv.server_close()
        self.assertEqual(r.status, "failed")
        self.assertTrue(any("independent verifier" in d["text"] for k, d in io.logs if k == "note"))

    def test_bad_inputs_are_observations_not_crashes_and_sandbox_is_enforced(self):
        """Bad tool input and blocked URLs come back as observations, not crashes."""
        def script(base):
            """Scripted turns for this test."""
            return [("bad host", [("open_page", {"url": "http://evil.example.com/"})]),
                    ("bad path", [("read_file", {"path": "../../etc/passwd"})]),
                    ("bad form", [("submit_form", {"form_index": 9})]),
                    ("stop", [("finish", {"status": "failed", "summary": "n/a", "evidence": []})])]
        srv, world, io, agent = build(script)
        agent.run("x")
        srv.shutdown(); srv.server_close()
        errs = [d["text"] for k, d in io.logs if k == "observation"]
        self.assertTrue(all(e.startswith("ERROR") for e in errs[:3]), errs)


class BudgetTests(unittest.TestCase):
    """Tests for the model-call budget."""
    def test_hard_call_budget_stops_run_and_counts_calls(self):
        """The run stops when the model-call cap is reached."""
        srv, world, io, agent = build(happy)
        agent.max_llm_calls = 4
        r = agent.run("enter newest Globex invoice")
        srv.shutdown(); srv.server_close()
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.llm_calls, 4)
        self.assertIn("budget", r.summary)

    def test_skipping_llm_verifier_saves_a_call_but_keeps_read_after_write_gate(self):
        """Skipping the verifier saves a call but the read-after-write rule still applies."""
        srv, world, io, agent = build(happy)
        agent.llm_verifier = False
        r = agent.run("enter newest Globex invoice")
        srv.shutdown(); srv.server_close()
        self.assertEqual(r.status, "done")
        self.assertFalse(r.verified)
        self.assertEqual(r.llm_calls, 9)
        self.assertTrue(any("REJECTED: you sent" in d["text"] for k, d in io.logs if k == "note"))
        self.assertEqual(len(world.bills), 1)


class InvoiceEditTests(unittest.TestCase):
    """Tests for adding and deleting portal invoices."""
    NEW = {"id": "INV-6001", "vendor": "Wayne Enterprises", "amount": "1,500.00", "currency": "USD",
           "issued": "June 12, 2026", "due": "July 12, 2026", "status": "Open"}

    def _login(self, base):
        """Returns the opening scripted step that loads the portal."""
        return [("open", [("open_page", {"url": f"{base}/portal/invoices"})])]

    def test_agent_can_add_then_delete_invoice_with_approval_for_delete(self):
        """Agent adds an invoice and deletes one, and the delete asks for approval."""
        import sandbox.server as sv

        def script(base):
            """Scripted turns for this test."""
            return self._login(base) + [
                ("form", [("open_page", {"url": f"{base}/portal/invoices/new"})]),
                ("add", [("submit_form", {"form_index": 0, "values": self.NEW})]),
                ("check", [("open_page", {"url": f"{base}/portal/invoices"})]),
                ("detail", [("open_page", {"url": f"{base}/portal/invoice/INV-0990"})]),
                ("delete", [("submit_form", {"form_index": 0})]),
                ("check2", [("open_page", {"url": f"{base}/portal/invoices"})]),
                ("finish", [("finish", {"status": "done", "summary": "ok", "evidence": ["INV-6001 listed", "INV-0990 gone"]})])]
        srv, world, io, agent = build(script)
        r = agent.run("add INV-6001, delete INV-0990")
        srv.shutdown(); srv.server_close()
        ids = {i["id"] for i in sv.INVOICES}
        self.assertEqual(r.status, "done")
        self.assertIn("INV-6001", ids)
        self.assertNotIn("INV-0990", ids)
        self.assertTrue(any("delete" in d.lower() for d in io.approvals))

    def test_denied_delete_keeps_invoice_and_a_new_server_starts_from_baseline(self):
        """A denied delete keeps the invoice, and a new server starts from the original data."""
        import sandbox.server as sv

        def script(base):
            """Scripted turns for this test."""
            return self._login(base) + [
                ("detail", [("open_page", {"url": f"{base}/portal/invoice/INV-0990"})]),
                ("delete", [("submit_form", {"form_index": 0})]),
                ("stop", [("finish", {"status": "blocked", "summary": "denied", "evidence": []})])]
        srv, world, io, agent = build(script, approve=False)
        agent.run("delete INV-0990")
        srv.shutdown(); srv.server_close()
        self.assertIn("INV-0990", {i["id"] for i in sv.INVOICES})
        sv.INVOICES.append({**self.NEW, "source": "agent"})
        srv2, _, _ = start_server()
        srv2.shutdown(); srv2.server_close()
        self.assertNotIn("INV-6001", {i["id"] for i in sv.INVOICES})


class DeleteSafetyTests(unittest.TestCase):
    """Tests for delete safety."""
    def test_list_shows_status_and_approval_names_the_record(self):
        """The list shows status and the approval message names the invoice."""
        def script(base):
            """Scripted turns for this test."""
            return [("list", [("open_page", {"url": f"{base}/portal/invoices"})]),
                    ("detail", [("open_page", {"url": f"{base}/portal/invoice/INV-1042"})]),
                    ("delete", [("submit_form", {"form_index": 0})]),
                    ("stop", [("finish", {"status": "blocked", "summary": "denied", "evidence": []})])]
        import sandbox.server as sv
        srv, world, io, agent = build(script, approve=False)
        agent.run("delete the paid invoice")
        srv.shutdown(); srv.server_close()
        listing = next(d["text"] for k, d in io.logs if k == "observation")
        self.assertIn("INV-0990", listing)
        self.assertRegex(listing, r"INV-0990.*Paid")
        self.assertRegex(listing, r"INV-1042.*Open")
        self.assertTrue(any("Status: Open" in d and "INV-1042" in d for d in io.approvals), io.approvals)
        self.assertIn("INV-1042", {i["id"] for i in sv.INVOICES})


class PersistenceTests(unittest.TestCase):
    """Tests for data carrying over between prompts."""
    def test_data_carries_over_between_prompts_until_reset(self):
        """Adds and deletes survive later runs until Reset data is used."""
        import time
        import ui
        import sandbox.server as sv
        ui.JOB.reset_data()
        sv.INVOICES.append({**InvoiceEditTests.NEW, "source": "agent"})
        sv.INVOICES[:] = [i for i in sv.INVOICES if i["id"] != "INV-0990"]
        for _ in range(2):
            ok, _err = ui.start_job("x", "replay", "", True)
            self.assertTrue(ok)
            t0 = time.time()
            while ui.JOB.status == "running" and time.time() - t0 < 20:
                time.sleep(0.05)
        ids = {i["id"] for i in sv.INVOICES}
        self.assertIn("INV-6001", ids)
        self.assertNotIn("INV-0990", ids)
        ui.JOB.reset_data()
        self.assertEqual({i["id"] for i in sv.INVOICES}, sv.BASE_IDS)
        self.assertEqual(ui.JOB.world.bills, [])


class DateRuleTests(unittest.TestCase):
    """Tests for the issue-date-before-due-date rule."""
    def _post(self, url, data):
        """Sends a form POST and returns the status code and body."""
        from urllib.error import HTTPError
        from urllib.parse import urlencode
        from urllib.request import urlopen
        try:
            return 200, urlopen(url, urlencode(data).encode()).read().decode()
        except HTTPError as e:
            return e.code, e.read().decode()

    def test_issue_date_must_be_before_due_date_in_portal_and_admin(self):
        """Both the portal form and the admin page reject a bad date order."""
        import sandbox.server as sv
        srv, world, base = start_server()
        b = Browser({urlparse(base).netloc})
        b.open(base + "/portal/invoices")
        bad = {**InvoiceEditTests.NEW, "id": "INV-6002", "issued": "July 12, 2026", "due": "June 12, 2026"}
        b.open(base + "/portal/invoices/new")
        r = b.submit(0, bad)
        self.assertEqual(r.status, 422)
        self.assertIn("Issue date should be before due date", r.text)
        same = {**bad, "issued": "June 12, 2026", "due": "June 12, 2026"}
        self.assertEqual(b.submit(0, same).status, 422)
        code, body = self._post(base + "/admin/invoices", bad)
        self.assertEqual(code, 422)
        self.assertIn("Issue date should be before due date", body)
        self.assertNotIn("INV-6002", {i["id"] for i in sv.INVOICES})
        good = {**bad, "issued": "June 12, 2026", "due": "July 12, 2026"}
        self.assertEqual(b.submit(0, good).status, 200)
        self.assertIn("INV-6002", {i["id"] for i in sv.INVOICES})
        srv.shutdown(); srv.server_close()


class AdminTests(unittest.TestCase):
    """Tests for the admin page."""
    def setUp(self):
        """Points the custom data file at a temporary location."""
        import sandbox.server as sv
        self.sv, self._file = sv, sv.CUSTOM_FILE
        sv.CUSTOM_FILE = Path(tempfile.mkdtemp()) / "custom.json"
        sv.INVOICES[:] = [i for i in sv.INVOICES if i["id"] in sv.BASE_IDS]

    def tearDown(self):
        """Restores the original custom data file path."""
        self.sv.INVOICES[:] = [i for i in self.sv.INVOICES if i["id"] in self.sv.BASE_IDS]
        self.sv.CUSTOM_FILE = self._file

    def _post(self, base, path, data=None):
        """Sends a form POST to the admin page."""
        from urllib.parse import urlencode
        from urllib.request import urlopen
        return urlopen(base + path, urlencode(data or {}).encode()).read().decode()

    def test_admin_adds_loads_pack_resets_and_agent_is_blocked(self):
        """Admin can add, load a test pack and reset, and the agent cannot reach it."""
        srv, world, base = start_server()
        self._post(base, "/admin/invoices", {"id": "INV-9001", "vendor": "Test Co", "amount": "1.00", "currency": "USD",
                                             "issued": "June 1, 2026", "due": "July 1, 2026", "status": "Open"})
        self.assertIn("INV-9001", self._post(base, "/admin/testpack"))
        self.assertTrue(any(i["id"] == "INV-4001" for i in self.sv.INVOICES))
        self.assertTrue(self.sv.CUSTOM_FILE.exists())
        b = Browser({urlparse(base).netloc})
        from worker.browser import ToolError
        with self.assertRaises(ToolError):
            b.open(base + "/admin")
        self._post(base, "/admin/reset")
        self.assertEqual({i["id"] for i in self.sv.INVOICES}, self.sv.BASE_IDS)
        srv.shutdown(); srv.server_close()


if __name__ == "__main__":
    unittest.main()
