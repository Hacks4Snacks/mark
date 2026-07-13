# Collections

A **collection** groups related conversations so a long-running effort — *"the
auth refactor"*, *"learning Rust"*, *"everything about repo X"* — reads as one
place instead of scattered sessions.

Collections are **auto-updating** and **non-destructive**: they follow a saved
search, but always honour the sessions you pin or exclude by hand. They live in
the same local SQLite database — nothing leaves your machine.

## How a collection stays current

Each collection can carry a **rule** — a saved search and its filters (query,
repo, topic, source, date range). Whenever Mark indexes new sessions, any that
match the rule flow into the collection automatically. You never have to re-add
them. Rules with multiple topics require every selected topic, matching the
search sidebar's **match all** behavior.

Filter-only and keyword rules include **every** matching conversation. Semantic
and hybrid rules are relevance-ranked and use Mark's server-owned top-500 cap;
the collection shows this policy explicitly instead of silently truncating its
membership. Manual pins and exclusions still override either policy.

On top of the rule you keep two manual overrides that **stick across re-syncs**:

- **Pin** a session the rule missed → it stays in, always.
- **Exclude** a session the rule caught → it stays out, always.

A collection can be:

- **Auto-updating** — has a rule (and may also have manual pins/exclusions).
- **Manual** — no rule; it's exactly the sessions you added by hand.

## Creating a collection

### From a search (auto-updating)

1. Run any search or set filters in the list view.
2. Click **▦ Save as collection** in the list header.
3. Name it. The current query + filters become its rule.

### Empty, then add by hand (manual)

1. Open **Collections** in the top bar → **New collection**.
2. Give it a name, icon, and colour.

### From a single conversation

On any conversation, use **＋ Collection** to add it to (or remove it from) any
collection. This creates a pin (or exclusion) that overrides the rule.

## The Collections page

Open **Collections** from the top bar to see every group as a card. The toolbar
lets you:

- **Filter** collections by name.
- Switch between **All / Auto-updating / Manual**.
- **Sort** by pinned-first, name, most sessions, or recently updated.

An aggregate line summarises across all collections.

## Collection overview

Open a collection to see its members plus a rolled-up **overview**:

- Total **spend** and **time** across its sessions.
- **Files touched** and **topics** covered.
- The **date span** from first to last activity.

This is the fastest way to answer *"how much did the auth refactor cost me, and
what did it touch?"*

Large member lists load in bounded pages while the displayed count and overview
continue to cover the collection's complete resolved membership.

## Ask a collection

If the optional local **Ask** feature is enabled (`MARK_ENABLE_ASK=1`; it's off
by default — see [Ask your history](ask.md)), you can scope a question to **just
one collection**. Answers are then drawn only
from that group's conversations — handy for *"summarise everything I learned in
the Rust collection."*

## Tips

- A collection's rule is just a saved search — anything you can do in
  [search and filtering](searching.md) you can bottle into a collection.
- Pins and exclusions are durable. Re-scanning, renaming, or editing the rule
  never discards them.
- Deleting a collection never deletes the underlying sessions.
