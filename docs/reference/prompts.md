# Prompts

The coordinator hands every turn one rendered prompt. Three Jinja templates
under `services/coordinator/src/swecraft_coordinator/prompts/` produce them:

| template | turn | shape |
| --- | --- | --- |
| `bootstrap.md` | the one-time orientation turn | its own template: a different turn with a different contract |
| `wake.md` | a single-task wake | the wake contract, one `{% block %}` per section that a variant may replace |
| `long_wake.md` | a long wake (`wake_mode: long-wake`) | `{% extends "wake.md" %}` overriding eight blocks: intro, working memory, checking-state lead, choosing work, boundaries, notes as you go, ending lead, final step |

Rendering is strict: an undefined variable fails the turn rather than
printing nothing.

## What varies a prompt

Every axis enters a template as a variable; the templates hold sentences
and the `{% if %}` blocks that choose between them, never the logic that
builds a phrase. `swecraft_coordinator.repositories.prompt_variables` is the one
place that turns configuration into those variables.

| axis | comes from | values | variables | sections it touches |
| --- | --- | --- | --- | --- |
| turn | which command ran | bootstrap, wake | the template itself | all |
| wake shape | `wake_mode` | `single-task`, `long-wake` | `WAKE_BUDGET`; the `long_wake.md` blocks | intro, working memory, checking-state lead, choosing work, boundaries, notes, ending |
| repository layout | the agent profile (`home_repo`, `write_repos`) | one repository; a maintained home plus further repositories; a dedicated home | `MAINTAINED`, `MAINTAINED_NOUN`, `CHECKOUTS`, `SEVERAL`, `DEDICATED_HOME`, `WORK_REPOS`, `HOME_REPO`, `DEFAULT_BRANCH` | identity line, charter, checking state, implement, the staged-checkouts paragraph, the bootstrap survey |
| collaboration | `prompt.collaboration` | `shared`, `isolated` | `ISOLATED` | checking state, what you can do, the data warning, converse, adopt sibling releases |
| collaboration check | `nudges.collaboration.below_rate` (the forge record of recent wakes) or `every_wakes` (a cadence); see [nudges](nudges.md) | a rate below target; a cadence in wakes | `COLLABORATION_CHECK`, `COLLABORATION_RATE`, `COLLABORATION_REACHED`, `COLLABORATION_WINDOW`, `COLLABORATION_TARGET` | the collaboration-check section before choosing work (`nudges/collaboration_check.md`) |
| budget | `budget.wakes`, `budget.ledger` and `prompt.budget` | a number of wakes; a file on the board; `stated`, `silent` | `BUDGETED` (False when nothing is stated), `BUDGET_WAKES`, `WAKES_USED`, `WAKES_LEFT`, `LEDGER_REPO`, `LEDGER_PATH` (empty without a ledger) | the budget section, before choosing work |
| last wake | scheduler state | outcome, summary, an unclean finish | `LAST_OUTCOME`, `LAST_SUMMARY`, `LAST_FINISHED`, `LAST_UNCERTAIN`, `LAST_ERROR` | charter |
| memory branch | `memory_branch` | | `MEMORY_BRANCH` | charter, bootstrap |

The collaboration axis is nearly subtractive: an isolated wake is the
shared wake with its sibling-facing passages removed (the notifications
step, forge-wide read access, other repositories as data, asking another
maintainer, adopting sibling releases) plus one sentence stating the
boundary: other repositories may be private or inaccessible by design, so
a 404 is not a fault. The agent is not told that it is isolated; the
world's repository visibility, not the prompt, is what keeps siblings out
of reach (see [worlds](worlds.md)).

Context management on long wakes (pi's compaction) is a harness setting,
`context` on the pi agent, and its
instructions are the runner's system prompt, not these templates; the wake
prompt only tells the agent that its conversation is condensed as it grows.

## Adding an axis

1. Give it one setting: under `prompt:` in `coordinator.yaml` when it only
   changes what the agent is told; elsewhere when the runtime enforces it.
2. Turn the setting into a variable in `prompt_variables` (a boolean or a
   rendered phrase), and document it in that function's docstring.
3. Reference the variable from `wake.md`, and from `bootstrap.md` when the
   orientation turn is affected. A variant of the whole wake shape is a new
   template that extends `wake.md`, not a copy.
4. Add the row above and a test in `tests/test_prompt_templates.py` that
   renders every layout with the axis on and off.

## Checking what an agent reads

`tests/test_prompt_templates.py` renders every layout and treatment through
the real `prompt_variables`, so a change to a template is visible as a diff
in what the tests pin. A one-off rendering for a configuration is two lines
from the coordinator package: build the variables with `prompt_variables`
and pass them to `swecraft_coordinator.prompts.render`.
