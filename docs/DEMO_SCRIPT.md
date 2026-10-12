# Demo video script (target: 4 minutes)

Record the browser UI (`python ui.py`). Make sure the API key is never visible. Narrate in your own words.

| Time | On screen | Say (roughly) |
|---|---|---|
| 0:00 | UI idle, both panels visible | "This is an AI worker. I give it a goal in plain English and it operates a computer to do it. On the right is a simulated company: a vendor portal and an ERP." |
| 0:25 | Click **Vendor portal**, show the unsorted invoice list | "The invoices aren't sorted, and the highest invoice number isn't the newest. The ERP also wants different date and amount formats." |
| 0:50 | Paste the Globex task, choose model, click **Run** | "I only state the goal. I don't tell it the steps." |
| 1:00 | Live feed scrolling | "It opens the portal, compares dates, and notes key facts in memory." |
| 1:40 | (Optional) ask it to delete an invoice → approval card → click **Approve** | "Destructive actions like delete are paused by code, not just the prompt, until I approve." |
| 2:00 | If the ERP error (HTTP 500 / format error) appears, point to it | "The ERP saved the bill but returned an error. Watch: it re-reads the ERP before retrying, so it doesn't create a duplicate." |
| 2:30 | Result card + ground-truth table | "It finished, an independent check agreed, and the database holds exactly one correct bill." |
| 3:00 | New run, **Pay the latest Globex invoice** (or deny the approval) | "When the system can't do something, or I deny approval, it stops and says so rather than pretending." |
| 3:30 | Terminal: `python -m unittest tests.test_harness` → OK | "Eleven automated tests check the safety rules: gates, retries, approvals." |
| 3:50 | README on screen | "Architecture, decisions, limitations and next steps are in the README." |

## Quota-friendly plan
- Do the main run once (about 9–12 model calls). Set *Max model calls* to ~15 as a safety cap.
- The denied-approval and "pay" runs need only a few calls each. If quota is tight, record them in a second session.
- Edit out waiting time (rate-limit pauses) in the video editor, or mention that the free tier pauses the agent.
- Do not use "Scripted replay" in the video; it is not the AI agent.
