# KubeAgent — 3-Minute Hackathon Demo Script

**Track:** HackGlobal 2026 · **Target runtime:** 3:00 · **Format:** screen
capture of the running app, with a terminal pane.

**Setup before you hit record**

- `docker compose up --build` is already running; dashboard open at
  `http://localhost:8000/` on a clean, zoomed-in browser (125%), single window.
- `LLM_API_KEY` set if you want the live reasoning text (otherwise the
  rule-based path still produces the same UX).
- Terminal scrolled to a fresh prompt, large font, no shell history visible
  (no keys, no URLs with tokens).
- Do **not** pre-create an incident: the whole demo depends on triggering it
  live. If the network flops, keep a recorded take of this exact flow as backup.
- Close Slack/notifications; turn on Do Not Disturb.

---

## 0:00 – 0:45 · The Hook — the 3 AM on-call nightmare

**Visual:** Dark dashboard, incident feed empty. Optional 5-second overlay of a
blurred phone alarm at 3:04 AM.

**Narration (≈105 words):**

> "It's 3:04 AM. Your phone wakes you up: `payment-api` is crash-looping in
> production.
>
> You don't know why. So you do what every on-call engineer does — you open a
> laptop, SSH into the cluster, run `kubectl describe`, then `kubectl logs
> --previous`, scroll a runbook that hasn't been updated in eight months, and
> try to reconstruct what happened while half asleep.
>
> Twenty minutes later you find it: a memory leak that's been quietly burning
> since the last deploy. Another ten minutes to fix it. Forty minutes of customer
> impact — and the post-mortem still isn't written.
>
> That loop is the problem we set out to delete. This is KubeAgent: an
> autonomous SRE agent that detects, diagnoses and fixes Linux and Kubernetes
> incidents in under thirty seconds."

**On-screen text (lower third):** `KubeAgent — Autonomous SRE Agent`

---

## 0:45 – 1:45 · Live Chaos Injection — watch it think

**Visual:** Switch to the dashboard. Terminal types the injection command.

**Action (silent, typed on screen):**

```bash
curl -s -X POST http://localhost:8000/api/v1/incidents/simulate \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"OOMKilled_Payment_Service"}' | jq '.incident.id'
```

**Narration (≈120 words), timed to what appears on screen:**

> "We inject the failure the same way production would see it — an alert coming
> in. This is the OOMKilled scenario: a payment service restarting in a loop with
> exit code 137.
>
> The incident appears instantly, and the agent starts its run. Watch the
> pipeline: it's collecting evidence the way an engineer would — pod status,
> previous container logs, node health, host metrics.
>
> On the right, the event stream is its reasoning, live over server-sent events.
> It correlates the exit code with the memory trend and the restart count, and
> lands on a root cause: the container exceeded its memory limit at 6 restarts.
>
> No dashboard hopping. No SSH. One run, six bounded tool calls, about ten
> seconds."

**On-screen text:** `Evidence → Root cause → Proposed remediation`

**If asked "is the LLM guessing?" (judge Q&A):** every claim cites an evidence
item collected by a typed tool call; the model chooses which tools to call and
how to interpret them, and cannot execute anything at this stage.

---

## 1:45 – 2:30 · Human-in-the-Loop Approval & Instant Post-Mortem

**Visual:** The approval banner on the dashboard; click **Approve**.

**Narration (≈100 words):**

> "Now the important part: it stops.
>
> KubeAgent does not get to touch production on its own. The incident is parked
> in `AWAITING_APPROVAL`, with the exact commands it wants to run laid out — here,
> restarting the deployment and clearing the wedged pod. I'm the SRE. I read it,
> and I approve. One click.
>
> That approval is the only path that executes remediation. Behind the scenes it
> hits the Kubernetes API with a service account scoped to read pods and nodes and
> to patch deployments — nothing else. No Secrets, no exec, no RBAC edits.
>
> The incident flips to resolved, and the cluster health tiles recover."

**Action:** click **View Post-Mortem Report**; the report opens in the modal,
scroll it slowly past the timeline and MTTR rows, then down to the
recommendations table.

**Narration (continued, ≈55 words):**

> "And the post-mortem is already written — the thing that normally takes an hour
> on a Sunday. A unique report ID, the full timeline from alert to recovery,
> root cause with evidence, every command executed, a measured MTTR — 14.2
> seconds here — and preventive recommendations for the next time this signature
> appears. Downloadable as Markdown for your incident tracker."

**On-screen text:** `Approval-gated · Measured MTTR · Auto-generated RCA`

---

## 2:30 – 3:00 · Architecture on Vultr VKE and Roadmap

**Visual:** Cut to the Mermaid architecture (README) or a clean slide with the
same flow, then a shot of the deployment manifest tree.

**Narration (≈95 words):**

> "Under the hood it's a FastAPI service: ingestion on the left, a DeepSeek
> reasoning loop with typed Linux and Kubernetes tools, a human approval gate,
> remediation, and the post-mortem generator.
>
> It runs on Vultr Kubernetes Engine as a single hardened pod — non-root,
> read-only filesystem, least-privilege RBAC, `/healthz` probes, and a Vultr
> managed load balancer in front. Simulation mode means you can demo it today
> without touching a real cluster.
>
> Next: persistent incident history, policy checks before remediation, Slack
> approvals, and multi-cluster fleet triage. KubeAgent — because nobody should
> debug production at 3 AM with a runbook from last year. Thank you."

**Final frame:** repo URL + `MIT licensed` + team name.

---

## Timing cheat sheet

| Clock | Beat | Must-land moment |
| --- | --- | --- |
| 0:00 | Hook | The 40-minute manual loop |
| 0:45 | Injection | Incident appears, pipeline lights up |
| 1:10 | Reasoning | Live SSE evidence stream |
| 1:45 | Gate | Visible stop at `AWAITING_APPROVAL` |
| 2:00 | Approval | One click → resolved |
| 2:10 | Report | Post-mortem modal + MTTR |
| 2:30 | Architecture | FastAPI + DeepSeek + VKE |
| 2:50 | Close | Roadmap + repo URL |

## Judge Q&A crib sheet

- **"What if the agent is wrong?"** It only proposes; a human approves. The
  command list is visible before anything runs, and `AUTO_REMEDIATE=false` ships
  as the default.
- **"Is the data real?"** In the demo it's the deterministic chaos simulator so
  the run is reproducible; in production the same code path reads the live
  cluster through the scoped service account and in-cluster credentials.
- **"Why DeepSeek?"** Strong tool-calling at low latency and cost per incident;
  the provider layer is OpenAI-compatible, so swapping models is one env var.
- **"What stops runaway spend or loops?"** `AGENT_MAX_STEPS` bounds the tool loop
  (default 6), and each incident is a single bounded run.
- **"How is MTTR computed?"** From the incident's event log — creation timestamp
  to the resolution event — not from a human's memory.
- **"Can I run it read-only?"** Remove the pod `delete` and deployment
  `patch`/`update` rules from the ClusterRole; analysis and post-mortems still
  work.

## Production / fallback notes

- If the live demo fails: cut to the pre-recorded take, keep narrating in the
  same order. Do not debug on stage.
- Keep the terminal font ≥18pt and the browser at 125% so judges on a stream can
  read the event feed.
- Never show a real `LLM_API_KEY` on screen; blur it if it appears in an env dump.
