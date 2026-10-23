# Nudges

A nudge is an intervention on a running agent: a message with a trigger
and a channel, delivered on some wakes and absent on the others. That
makes it a different kind of setting from `prompt:`, which describes the
standing situation the same way on every wake (whether siblings exist,
whether the budget is stated), and from `budget:` and `wake_budget:`, which
the runtime enforces. Nudges live under `nudges:` in `coordinator.yaml`,
one entry per nudge:

```yaml
nudges:
  collaboration:                            # the collaboration check, on either condition
    below_rate: {target: 0.3, window: 10}   # when fewer than 30% of the last 10 completed wakes reached out
    every_wakes: 3                          # every third wake, whatever the record
  finalize:                                 # the end-of-wake notice of a long wake
    at: 0.97                                # fraction of the wake budget at which it is sent
```

Every trigger defaults to off, and a run without a `nudges:` section
behaves exactly as before.

## Channels, and which harness has them

| channel | what it is | harness |
| --- | --- | --- |
| wake prompt | a section in the prompt the coordinator renders at the start of a wake | every harness (claude-code, codex, pi, any Harbor agent) |
| finalize notice | the message that tells a long wake to wrap up | pi only, long wakes only |

For claude-code and codex the coordinator can shape the starting prompt
and the deadline, nothing in between. The pi harness also has a mid-wake
channel (the notices its long-wake extension steers into the run); no
nudge uses it yet.

## The collaboration nudge

One section, "## Collaboration check", placed in the wake prompt before
the choice of work, on any wake where either of two independent
conditions holds. The check asks the agent to take stock of how it works
with the other maintainers, record the answer in the notebook under
`Collaboration`, and if the answer is no, weigh one concrete step of that
kind (a question to a maintainer whose library it depends on, a review
they are waiting for, a release of its own they could use) against the
wake's other candidates. It asks and weighs; it orders nothing, so a no
stays an honest answer and the forge record stays the measure.

**`below_rate`: the forge's record.** Every wake leaves
`output/forge-actions.jsonl`, the runner's ledger of the forge writes the
agent made, each classified as an action (`issue.comment`, `pr.review`,
`release.create`, ...) from a revisable table. A wake *reached out* when at
least one row is a communication act (an issue or pull request created,
commented, edited, reviewed, merged or closed; a comment edit) on a
repository outside the agent's own set (its write repositories plus its
home). Binary per wake and unweighted: one comment and one full review
count the same; reading another repository does not count; releases,
tags, labels and CI dispatches do not count; nor do reactions, on
purpose: once the prompt states the rate, a reaction is the cheapest way
to satisfy it. Before rendering a wake's
prompt the coordinator counts reached-out wakes among the agent's last
`window` *completed* wakes (failed and interrupted wakes neither help nor
hurt), and only once `window` of them exist: the rate is always over a
full window, so the first wakes of a run are never judged on a handful.
When the count is below `target × window`, the wake carries the check
with the record stated in place of "since your last check": "over your
last 10 wakes you worked with the other maintainers in 2 of them, by the
forge's record; this society aims for at least 4 in 10". The number shown
is the smallest count that meets the rate, so a triggered wake never reads
as already satisfied.

**`every_wakes`: a cadence.** Every Nth wake the agent has started carries
the check, whatever the record says.

The two are independent: set one, the other, or both (a wake where both
hold states the record). Two things the measure misses: answering a
sibling's question posted on the agent's *own* repository (the row is on
its own repository, and the ledger does not carry who the counterpart
was), and quality. Calibrate `target` on a run whose collaboration you
want to match: on the opus six-maintainer cells (18.1, 18.3), whose
talkers reach out in 0.6 to 0.8 of their wakes and whose silent third
never does, `target: 0.3` over `window: 10` singles out the silent third
and little else, and the window's width hardly matters because the
agents under target stay under it. On a society that never reaches out
at all (two of our runs had not one cross-repository
act in 600 wakes each), `below_rate` holds on every wake once the window
is full, so there `every_wakes` is the only knob that shapes the dose,
and the question the run answers is whether a nudge creates any
collaboration at all.

The nudge is refused under `prompt.collaboration: isolated`, where there
are no other maintainers to speak of.

## The finalize nudge

`finalize.at` places the end-of-wake notice of a long wake: at that
fraction of the wake budget the pi harness tells the agent to stop starting
new work, finish or park what is in flight, write its notes, and reply
without a tool call (see [runner](runner.md#long-wakes)). It used to be
`wake_budget.finalize_at`, which is still accepted as the old spelling
(setting both to different values is an error). `hard_stop_at` stays with
the budget: it is enforcement, not a message. The lines added to the notice
over time (your notes are committed for you) are nudge content, which is
the sign the category is real.

## The record

Each wake that carried the check is marked in the coordinator's state
database, table `wake_treatments`, as `collaboration-check` with the
condition that brought it: `every 3 wakes`, or `2 of 10 reached out`. The
mark is kept beside the wake rather than on it, so the wake's own record
is unchanged, and nudged wakes can be counted against what came of
them.

## Where the pieces live

| piece | where |
| --- | --- |
| configuration and validation | `swecraft_coordinator.config` (`Nudges`, `CollaborationNudge`, `BelowRate`) |
| the measure, the two conditions, the prompt variables, the record | `swecraft_coordinator.collaboration` |
| the section | `prompts/nudges/collaboration_check.md`, included by `wake.md` (and so by `long_wake.md`) |
| the finalize notice | `services/runner/src/swecraft_runner/pi/long-wake.ts` |
| the prompt variables, on the axis matrix | [prompts](prompts.md) |
