# Demo day — Thursday 12 November

Everything below was run and passing at 02:40 on the 12th. The gateway and
Presidio are still up from that session; if the machine has been restarted,
start at step 1.

---

## Before you leave the house (20 minutes)

### 1. Start the services

```bash
cd D:/FrameWorkCode/platformAILearning/llm-gateway
$env:PYTHONPATH="D:/FrameWorkCode/platformAILearning/llm-gateway"
$env:MG_API_KEY="demo-secret-key"
$env:ANTHROPIC_API_KEY=[Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY','User')
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

In a second terminal:

```bash
cd D:/FrameWorkCode/platformAILearning/llm-gateway
.\.venv\Scripts\python.exe -m uvicorn classifiers.presidio_service:app --host 127.0.0.1 --port 8001
```

### 2. Pre-warm the models — this takes about four minutes and it matters

Cold load is 145s and 107s. If the first thing the interviewer sees is a
two-minute pause, nothing else on this page helps.

```bash
curl -s -m 300 http://127.0.0.1:8000/v1/chat/completions -H "Authorization: Bearer demo-secret-key" -H "Content-Type: application/json" -d '{"task":"support_draft","messages":[{"role":"user","content":"My account 8841-22719 keeps buffering."}]}'
```

### 3. Preflight — the one that catches a stale process

```bash
python loadtest/preflight.py
```

It must print **PASS**. If it prints FAIL, the gateway you are about to demo
does not have its guards loaded. This has already happened once: a process
left running from earlier in the day answered *"ignore all previous
instructions"* for eight hours while every test passed.

### 4. Confirm nothing has drifted

```bash
python dashboard/verify.py
```

44 figures, each checked against the file that produced it. If a number on a
slide no longer matches a run, this fails instead of the interviewer finding
it.

---

## The demo, in order (about 12 minutes)

Lead with the thing that is hardest to fake, which is the live kill.

| # | Do | Say |
|---|---|---|
| 1 | Send an ordinary message → answered by **cloud** | "No personal data, so cost and latency get a vote. It went to Claude." |
| 2 | Send one with an account number → answered **on-prem** | "Personal data is a *hard constraint*. It filters the provider list before cost or latency are considered. A preference can be outvoted; a constraint cannot." |
| 3 | **Kill Presidio** (`Ctrl-C` in terminal two). Send the same message again | "The classifier is down, so I cannot *prove* the request is safe to send outside. It refuses cloud entirely and degrades to on-prem. Safety without an outage — and the audit log says why." |
| 4 | Restart Presidio. Send an injection attempt → **HTTP 400** | "Blocked, and it names the pattern that fired." |
| 5 | `python redteam/run.py --limit 60` | "A thousand of these, same order every run. 988 held. The twelve that don't are each categorised and either fixed or consciously accepted." |
| 6 | Open the **dashboard**, go to the red team chart | "Run four went *up*. That is a regression I introduced — I put a full stop inside a character class and broke nineteen cases to fix three. It was only caught because the whole corpus re-runs after every change." |
| 7 | Dashboard → Load & NFR | "The load test found two customer-facing defects that a hundred and thirty-three unit tests did not." |
| 8 | `python evals/gate.py --promote regex` → **BLOCKED** | "The gate blocks two of the three candidates. A gate that always passes proves nothing." |

**If something breaks mid-demo, say so and keep going.** The dashboard has
every figure and the commands that produced them; the story works from the
page alone.

---

## The three questions you will get

**"Isn't this just LiteLLM?"**
> The gateway is commodity — LiteLLM, Portkey, Kong AI Gateway, Bedrock. I
> would buy the gateway. What nobody sells is the evidence layer: the corpus,
> the gates, the traceability, the audit trail that answers *why* a model made
> a decision about a customer. That is the part worth building.

**"Your churn model is a toy."**
> Deliberately. It is a stand-in. Swap in yours — the control plane does not
> care what the model is, which is the whole point of the adapter boundary.

**"This is a laptop, not scale."**
> Architecture-correct, scale-simulated. And the laptop is why the most useful
> finding exists: the control plane's ceiling is 2.9 requests a second, and I
> can show you it belongs to the PII classifier rather than the gateway,
> because throughput is flat from one to sixteen concurrent while latency
> rises linearly. On real hardware that number changes; the shape does not.

**"How much does routing on-prem actually save?"**
> About $74,000 a year, and that is the weakest of the three drivers against
> $17.6 billion of revenue. I would rather tell you that than have you find
> it. The cost argument that holds is optionality — when a provider raises
> prices, it is a config line rather than a release.

---

## Links

- **Evidence dashboard** — https://claude.ai/artifact/Fn6t7xpCWLcA4KaTTtnG7C
- **Five-slide ELT deck** — https://claude.ai/artifact/FDGkHjzo2ksJJoFxWcpVnH

Both are private. Share from each page's Share menu if you want to send them
on afterwards.

### Present the deck from your own disk

Double-click `deck/MigrationGuard-ELT.html`. Same five slides, same speaker
notes, no sign-in and no network. **Use this one in the room.** The hosted
deck needs an account, a connection and several megabytes of runtime before
a single slide appears; this is 43 KB and opens instantly.

| Key | Does |
|---|---|
| &larr; &rarr; · space · click | Move between slides |
| `N` | Speaker notes panel |
| `F` | Full screen |
| `1`–`5` | Jump to a slide |

Rebuild it after any change to the deck source:

```bash
python deck/build_local.py
```

The only thing it fetches is the IBM Plex webfont, and it falls back to a
declared stack if there is no network. Three slides measure taller than a
1920&times;1080 canvas when rendered this way, so the file scales the whole
deck down by one constant factor rather than clipping the bottom row — the
text is slightly smaller than the hosted version and nothing is missing.

---

## Freeze

**Stop changing code four hours before you leave.** Every change from here is
a chance to break something you will not have time to find. If you spot
something wrong, write it down and say it out loud in the interview instead —
"I found this last night and here is what I would do about it" is a better
answer than a silent fix.
