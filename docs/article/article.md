# A work briefing that remembers: what I learned building one with an AI agent

*A tool that answers "where do I stand right now?" at any hour, and why the interesting part isn't the AI.*

![The pipeline: sources are normalised, classified, snoozed, scored and turned into a plan. Two lanes branch off: one remembers (snapshot, diff, "since your last check", reports), one never writes by itself (drafts, preview, confirm, post).](figures/fig1-flow.png)

I keep losing the same five minutes. Somewhere between the issue tracker, the pull requests I opened, the ones I was asked to
review, and a pile of comments, there is a short list of things that actually need me today. Finding that list is
a small job. Doing it over and over, at 9 am, again at 11, again after lunch, is where the day leaks away.

So I built a tool that finds the list for me. It began as a *morning* briefing. It stopped being one the first time I ran it
in the afternoon and it told me **what had changed since the last time I looked**. That single capability, a tool that
remembers its previous run, turned out to shape almost everything else about it.

This post is about the handful of design ideas that I think are worth stealing, and about how it was built with an AI coding
agent, including the places where the agent and I got it wrong. The code is a small open-source Python project,
[work-progress-briefing](https://github.com/tjaensch/work-progress-briefing), and it has a demo mode with invented data, so you
can see everything below without an account.

## What it does

Run it and it reads your open issues, your open pull requests and your review requests (through the `gh` command line tool,
so it never handles a token). It turns that into:

- a **ranked plan** for the rest of the day, with the reason for every point in the ranking;
- a **"since your last check"** list, split into things that need you and things that moved forward;
- **drafts** of comments you might want to post, which it will never post on its own;
- **stand-up, end-of-day and weekly reports** built from its own memory.

Here is the plan from the demo, a fictional team working in an invented organisation:

![A terminal showing today's plan: item 1 "Rate limiter drops valid requests", next action "Approved: merge", reason "bucket: ready (+20); due in 2d (+20); quick win (+12)"; then four more items with next actions and reasons.](figures/fig2-plan.png)

Notice the "Why" lines. Every point in a score has a name. When a ranking surprises me, I can see which rule produced it and
change the rule, instead of arguing with a number.

## Six ideas worth stealing

### 1. Remember the last run, and diff against it

The cheapest way to know what changed is to save a snapshot of what you saw last time and compare. Each refresh writes a
small JSON snapshot: for every item, its status, who commented, the state of its pull requests, whether CI is failing. Diffing
two snapshots produces typed changes, and each change is tagged with two booleans: did something **move forward**, and
**does it need me**?

![A terminal showing "Since your last check (18h ago)": four lines marked "needs you" (a review approved, a new comment, CI started failing, a new review request), then neutral changes such as a PR going from OPEN to MERGED, followed by "Progress today".](figures/fig3-since-last-check.png)

Those two tags do a lot of work. "Needs you" is what a notification should be built from. "Moved forward" is what feeds a
progress log. Two flags, one diff.

A small thing I got wrong at first: a merged pull request's review state "changes" from *review required* to *none*, and the
tool dutifully reported it. A merged PR's review state is not news. The fix was one line, and the test that pins it is
worth more than the line.

### 2. Snooze until it matters, not until a date

Most snooze features are a timer. A timer is the wrong tool when what you actually mean is "ignore this until something
happens." So a snooze in this tool can end on a date, **or when a fingerprint of the item changes**, whichever comes
first. The fingerprint is a hash of what a person would call a change: a new comment from someone else, the status moving, a
pull request changing state or review or CI result.

```
$ work-briefing snooze acme-labs/widget-api#5 --days 3 --reason parked
Snoozed acme-labs/widget-api#5 until Sat 2026-03-14
```

One detail makes it feel right: **your own comments don't count as a change.** Otherwise posting a nudge on a snoozed item
would wake it up immediately, which is the opposite of what you wanted.

### 3. Drafts, never writes

The agentic part of this is deliberately dull. The tool *builds* comments and status changes you might want to make. It
posts nothing. To post, you run `apply`, which re-reads the item's current status, shows you the exact text, tells you
whether a status change is actually available right now, and asks.

![A terminal showing "work-briefing apply" for a status update: it prints the comment text, the prompt "Post this?" answered with n, and "Cancelled; nothing was written."](figures/fig4-apply-preview.png)

Two rules keep this honest. If the item's status changed since the draft was made, `apply` refuses and asks you to run the
briefing again, because the draft's wording was written for a world that no longer exists. And if the status change you
asked for isn't available, it posts the comment only and says so, instead of failing halfway.

The text of mismatch drafts is fixed wording, not model output. "Status update: PR … has been merged. This is still marked in
progress; reviewing whether it can move forward." It is boring on purpose: predictable, free, and the same every time.

There is also a rule about *when* such a draft appears. It is offered only while nothing has touched the item since the
merge. Posting the comment touches the item, so the draft doesn't come back. I first built a "no new draft for three
days" cool-down, and replaced it once I realised the better question is not "how long ago did I last comment" but "has anything
happened since the event this draft is about."

### 4. A failure that nobody sees is worse than no tool

A briefing you trust is dangerous when it is quietly stale. If a refresh fails, the tool writes a small status file, puts a
banner at the top of the briefing file (so an editor tab that is already open shows the problem), and distinguishes errors
that retrying can fix from ones it can't, like not being logged in. Files are written to a temp name and renamed, and
anything that changes state takes a lock that disappears with its process, so a crash never leaves a stale lock behind.

None of this is glamorous. It is the difference between a script and something you can rely on at 4:50 pm.

### 5. Use a model sparingly; keep plain code for anything that must be predictable

The buckets (ready, waiting, blocked, stale), the next actions, the status-mismatch checks and the score are ordinary
functions. They are cheap, instant, and testable, and I can explain any output. The public version contains no language
model at all.

In my own setup I do use one for a narrow job: a two-sentence "where I left off" summary of a long comment thread. It runs
with tools switched off, the input is scrubbed of anything that looks like a secret, and answers are cached. The rule of thumb
I landed on: if the output has to be the same every time, or if a wrong answer would be posted somewhere, it does not
belong to a model.

### 6. Reports from the log, with no invented hours

Because the tool already logs what it saw change, a stand-up, an end-of-day check and a weekly summary are just views of the
log. They run instantly, offline, and give the same text for the same input.

![A terminal showing "work-briefing report week": counts of items with activity, PRs merged and comments posted, a line saying "No hours", a note that the event log starts Mon 2026-03-09, then events grouped by item.](figures/fig5-week.png)

Look at the two lines I care about. "No hours" is a promise: the report lists what the tool saw change and does not claim how
long anything took. And the note that "the event log starts Mon" is the tool being honest that the first week is
incomplete, instead of presenting a confident empty report.

## How it was built, and why I trusted it

I built this with an AI coding agent (GitHub Copilot in VS Code). The agent typed most of the code. What made the result
trustworthy was a loop I made it follow for every change, however small:

1. **Plan the item first.** A written list of small items (A, B, C…), each with the design, the files it touches and what
   "done" means. The agent works through one at a time.
2. **Tests next to the change**, with fictional data only.
3. **Break it on purpose.** Copy the project to a scratch folder, make one deliberate mistake in the code (flip a
   comparison, delete a guard), and run the tests. If they still pass, the tests were not testing that line.
4. **Run it for real once**, against the real service, read-only.
5. **Docs, commit, CI.**

Step 3 sounds paranoid. It found real gaps. For example, my tests passed with the "stale" rule's boundary flipped from
"at least seven days" to "more than seven days", because no test sat exactly on the boundary. I added one.

### The mistakes that taught me something

A few of the bugs are the kind that no amount of AI makes go away, and that the loop caught:

- **My safety scanner leaked what it was scanning for.** I wrote a small guard that fails a commit if forbidden words appear
  in files, file names, commit messages, authors, or any patch in history. Its first version reported a hit by printing the
  file name, and the forbidden word was *in the file name*. A test caught it. The report now masks the match.
- **The scanner's history parser broke on its own source.** It split the history log on a marker string, and the scanner's
  own source code contained that marker. Fixed by only treating the marker at the start of a line as a marker.
- **A test that couldn't fail.** I wrote `assert a or b or True` while rushing, and it hid that a report told the user to run
  a command that didn't exist (`--apply` instead of `apply`). Reviewing my own assertions after the green run found it.
- **My mutation harness was broken the first time.** I checked for survival by searching the output for the word
  "passed", but a failing run also prints "1 failed, 5 passed". Every mutation "survived". It looked like my tests were
  worthless. They weren't; the verifier was wrong. A check that produces only good news deserves suspicion.

The common thread: the agent and I both make plausible mistakes, and the loop's job is to make plausible mistakes expensive
to keep.

## Try it

```bash
git clone https://github.com/tjaensch/work-progress-briefing && cd work-progress-briefing
python3 -m work_briefing demo
```

The demo replays four invented "days" through the real GitHub code (only the `gh` command is faked), prints the final
briefing and builds a stand-up and a weekly report from the same history. Commands pointed at the demo folder keep using
demo data and never touch GitHub, including `apply`.

To use it on your own work, install `gh`, run `gh auth login`, then `work-briefing`. To use a different tracker, implement
the four-method `Tracker` interface and return items using the shared status vocabulary
(`todo`, `in_progress`, `in_review`, `blocked`, `done`). The GitHub adapter is about 250 lines and is the reference.

## What I'd do differently

- **Start with the memory.** I added "since your last check" in the second week. It should have been day one. It is the
  feature that made the tool useful at any hour.
- **Write the "why" strings before the score.** Naming each point of a score first made me design rules I could defend.
- **Treat the checks as code.** The scanner and the mutation harness both needed tests of their own.
- **Build for a calendar and email next.** The part of my day this doesn't see is meetings and mail. That's a different
  adapter, not a different tool.

The tool is small: roughly 2,300 lines of Python, 235 tests, no dependencies beyond the standard library. I think that's the
real takeaway. The valuable part of an "AI-assisted" tool is rarely the AI. It is a few ordinary ideas (remember, explain, ask
before writing, fail loudly) and a development loop that makes it hard to lie to yourself.

*The code is MIT licensed. Everything in the demo is fictional.*
