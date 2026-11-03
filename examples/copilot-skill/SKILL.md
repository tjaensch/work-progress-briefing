---
name: work-progress-briefing
description: Check where my work stands right now (plan, what changed since I last looked, what is waiting on me, drafts), answer follow-ups, and snooze things. Use when I type /work-progress-briefing or ask for status, "what changed", "what's next", a stand-up, or an end-of-day or weekly summary.
---

Use the `work-briefing` command line tool. Its files are in `$WPB_HOME/out/` (default
`~/.local/share/work-progress-briefing/out/`). Read `latest.json` for facts; do not re-query GitHub yourself.

1. Run `work-briefing`. If it prints "Refresh failed", tell me the error and that anything below is older data; do not
   summarise as if it were current. Exit code 2 means retrying will not help (for example I am not logged in to `gh`).
2. Read `latest.json` and summarise in at most 10 lines, in this order:
   - what changed since my last check (`delta.events` with `new_since_last_run` true; items with `attention` first),
   - progress today (`progress_today`),
   - the plan (`plan`, in order, with the next step for each),
   - anything waiting on me (`incoming`),
   - any source that failed (`health` entries with status `failed`).
3. Answer follow-ups from that file. `items[].score_parts` says why something ranks where it does.

Reports (read-only, no network): for "what did I do yesterday / today / this week" run
`work-briefing report standup`, `report eod` or `report week` and show the output. A week report lists what the tool saw
change and never hours; do not add hours or detail it does not contain.

Snoozing changes only local state, so no confirmation is needed, but tell me what you did:
`work-briefing snooze ID [--days N | --until DATE | --until-change] [--reason TEXT]`, `work-briefing dismiss ID`
(until it changes), `work-briefing unsnooze ID`, `work-briefing snoozes`. Valid ids are the keys of `snooze_fingerprints`
in `latest.json`; pick the narrowest one that matches what I asked for, and say which. Refresh afterwards.

Posting is the only thing that writes anywhere (comments, status changes), and it needs my explicit yes:
- Drafts are in `drafts` in `latest.json`. Show me the exact comment text and any status change (`transition`).
- Wait for me to say yes in chat. Never post, comment or change a status on your own.
- Only after my yes, run `work-briefing apply <draft id> --yes`. If it refuses because the status changed since the
  draft was made, tell me and run the briefing again instead of retrying.
- If I want different wording, change the `comment` of that draft in the newest `drafts-*.json` file first, then show me
  the result again.
