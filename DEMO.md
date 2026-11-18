# Demo day — Wednesday 18 November

Everything below was run and passing today. The timings in the act table are
measured, not estimated — each act was run on its own and timed. The gateway
and Presidio are still up from that session; if the machine has been
restarted, start at step 1.

Run the demo with `demo/scenario.py`. The eight-step manual order this page
used to carry is gone, because one of its steps asked you to kill Presidio
with `Ctrl-C` in a second terminal and then restart it by hand mid-demo. The
scenario script does the same thing by repointing `config.yaml` and reloading
the gateway, and restores it in a `finally` block — verified twice today, no
stray backup file left behind either time.

---

## Before you leave the house (20 minutes)

### 1. Start the services

Only needed if the machine has restarted. Terminal one:

```bash
cd D:/FrameWorkCode/platformAILearning/llm-gateway; $env:PYTHONPATH="D:/FrameWorkCode/platformAILearning/llm-gateway"; $env:MG_API_KEY="demo-secret-key"; $env:ANTHROPIC_API_KEY=[Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY','User'); .\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Terminal two:

```bash
cd D:/FrameWorkCode/platformAILearning/llm-gateway; .\.venv\Scripts\python.exe -m uvicorn classifiers.presidio_service:app --host 127.0.0.1 --port 8001
```

### 2. Pre-warm the models — allow four minutes, and it matters

Cold load is 145s and 107s. A warm run is under twenty seconds, but only
because the weights are still in the operating system's file cache; do not
assume that state. If the first thing the interviewer sees is a two-minute
pause, nothing else on this page helps.

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

45 figures, each checked against the file that produced it. If a number on a
slide no longer matches a run, this fails instead of the interviewer finding
it.

---

## The demo (about 12 minutes)

One command. It pauses between acts, so you drive it with Enter and the
pacing is yours:

```bash
python demo/scenario.py
```

| Act | Scenario | What happens | Measured | The line that lands |
|---|---|---|---|---|
| 0 | — | Context — the migration, the churn moment | 1.6s | "Every migrated household is a churn-risk moment." |
| 1 | — | The rule, read off `policy.yaml` on screen | 1.2s | "Nine lines. Governance you can read aloud." |
| 2 | SC-17 | Who decides what counts as personal data — three candidates, measured | 1.5s | "The gate blocked two of the three. A gate that always passes proves nothing." |
| 3 | SC-02 | A clean question, answered by **cloud** | 5.5s | "No personal data, so cost and latency get a vote." |
| 4 | SC-01 | Same agent, now with an account number, answered **on-premises** | **19.8s** | "A preference can be outvoted. A constraint cannot." |
| 5 | SC-08 | Caller explicitly demands cloud, still answered on-premises | **17.3s** | "Governance a caller can bypass with a field is not governance." |
| 6 | SC-04/05 | **Kill the on-premises model.** Cloud is up, healthy, three times faster | 5.0s | "It takes the outage rather than the breach." |
| 7 | SC-18 | **Kill the classifier**, on a payload with no personal data in it | **19.7s** | "We did not detect data. We could not *prove* its absence. Different things." |
| 8 | SC-12 | The audit trail — 3,618 records, 152 provider exclusions | 1.5s | "Prove it from the log, not the slide." |
| 9 | SC-13 | The money, with the sensitivity table | 1.3s | see the note below — read this one carefully |
| 10 | SC-15 | Swap the model by config, then a bad config is rejected while serving continues | 4.4s | "An SRE can do that at 3am. A release cannot." |
| 11 | — | The tests and the coverage | 1.1s | "174 tests, every one with a discipline marker." |

**Total machine time is about 80 seconds.** The other eleven minutes are you
talking, which means nothing forces your pace except acts 4, 5 and 7 at
roughly twenty seconds each. Have a sentence ready for each of those three
waits. They are long because the brevity instruction was removed from the
demo's system prompt (see F-P15) and the model now generates to its
120-token ceiling.

Jump to a single act with `--act N`, using the numbers in the table above.
`--no-pause` runs straight through, which is for rehearsal, not for the room.

**Do not pass `--fresh`.** It clears `events.jsonl`, and the historical
numbers — 3,618 records, 152 provider exclusions — are better evidence than
a small clean count taken in front of the interviewer.

### Act 9 and the README disagree. Act 9 is the one to say out loud.

Act 9 prints the real ledger — cloud 518 calls (72%), on-premises 206 (28%) —
then a sensitivity table, and then says outright that *cost is the weakest of
the three drivers at this scale*:

| tokens/call | all-cloud per year | at 28% on-prem |
|---|---|---|
| 516 — this demo, short replies | $19,040 | **$5,418** |
| 1,000 | $36,900 | $10,499 |
| 3,000 — realistic with retrieval context | $110,700 | **$31,498** |

`README.md` still carries **~$74K/yr** as the inference saving, and
`verify.py` gate-locks that figure. So if anyone reads the table and then
watches act 9, the two disagree. Say the act 9 version. The discrepancy then
becomes you correcting your own number in public, which reads as rigour;
leave it unspoken and it reads as a number nobody checked.

**If something breaks mid-demo, say so and keep going.** The dashboard has
every figure and the commands that produced them; the story works from the
page alone.

---

## The questions you will get

**"Isn't this just LiteLLM?"**
> The gateway is commodity — LiteLLM, Portkey, Kong AI Gateway, Bedrock. I
> would buy the gateway. What nobody sells is the evidence layer: the corpus,
> the gates, the traceability, the audit trail that answers *why* a model made
> a decision about a customer. That is the part worth building.

**"This is a laptop, not scale."**
> Architecture-correct, scale-simulated. And the laptop is why the most useful
> finding exists: the control plane's ceiling is 2.9 requests a second, and I
> can show you it belongs to the PII classifier rather than the gateway,
> because throughput is flat from one to sixteen concurrent while latency
> rises linearly. On real hardware that number changes; the shape does not.

**"How do you know your quality signals are real?"**
> Because three times this week they were not, and I found each one. Attack
> length, prose length, identifier format — a corpus that presents one surface
> form only ever measures the guard against that form. And this morning, two
> hours before this meeting, my own safety model refused a buffering complaint
> as a violent crime, because the gateway hands it the operator's system prompt
> as if it were customer text. All four are written up with the mechanism, the
> isolation steps and the remediation order.

**"What is not built?"**
> The Kafka data plane, the constrained agent, the containers and the cluster.
> `ARCHITECTURE.md` says so in the sections that describe them, rather than
> leaving you to find out. The migration-regression score — the one genuinely
> DIRECTV-specific idea in the design — exists as a sentence, not as code.

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
