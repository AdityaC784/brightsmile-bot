# Demo video — 3-minute shot list

Record the screen at 1080p with your voice over it. Before recording, open the live site once so
it's awake (free hosting sleeps), and keep two browser windows side by side: the **chat**
(https://brightsmile-bot.onrender.com/) and the **staff page** (`/staff`, logged in).
Use made-up details (e.g. "Test Patient", `+91 90000 00000`), never real ones.

| Time | Show | Say (short) |
|---|---|---|
| 0:00–0:15 | README top: what it is + the results table | "A support and lead-capture chatbot for a dental clinic, with an eval gate in CI." |
| 0:15–0:45 | Chat: *"How much is a molar root canal?"* → hover a citation | "Answers come only from the clinic's documents, with citations, and every reply is checked before it's shown." |
| 0:45–1:00 | Chat: *"Can you tell me if my tooth is infected?"* | "It won't diagnose; it says an exam is needed and gives the red-flag signs." |
| 1:00–1:15 | Chat: *"My face is swelling fast and it's hard to swallow"* | "Emergencies get first aid and where to go, even when the clinic is closed." |
| 1:15–1:45 | Chat: *"I'd like to book a cleaning"* → consent text → *"Yes, Test Patient, +91 90000 00000, test@example.com"* | "Leads are saved only after the patient sees the consent text and agrees. Phone is required, email optional." |
| 1:45–2:00 | Staff page → **Leads** → the new row (phone and email in their own columns) → **Back to chats** | "Staff see leads in a table and can delete them." |
| 2:00–2:25 | Chat: *"Can I talk to someone?"* → staff page shows the chat → staff replies → reply appears in the chat → **End chat** | "Handoff: staff reply live; the bot steps back and still answers emergencies." |
| 2:25–2:50 | GitHub: the closed bad PR #1 → eval-gate comment (`docs/img/bad-pr-gate-comment.png`) | "This PR broke emergency replies. Unit tests passed, but the eval gate blocked the merge: safety dropped below 100 %." |
| 2:50–3:00 | `make review` in the terminal, one candidate | "Bad answers from real chats become new test cases; I write the correct answers myself." |

After recording: upload (YouTube unlisted or similar), then replace *"link coming"* in the
README's first lines with the link, and delete the test lead from the staff page.
