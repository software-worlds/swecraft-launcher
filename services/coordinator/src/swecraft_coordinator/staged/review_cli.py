#!/usr/bin/env python3
"""In-turn peer-review CLI, staged into /app/tools of a review turn.

Self-contained (stdlib only) so it runs inside any agent image, including
frozen historical ones. State persists in a JSON file between invocations,
so the agent can answer a question, leave to check the forge or its memory,
and resume where it left off. The host re-validates everything after the
turn; this CLI is ergonomics and early feedback, not the trust boundary.

Serves both review kinds, chosen by `survey.json`'s `kind`: peer turns
select their own targets from the roster; society turns answer one survey
about the run with no selection step.

Flow (peer):
  review_cli.py start                     roster + instructions
  review_cli.py select <target> --reason  choose an audience member
  review_cli.py skip-peers --reason       declare no meaningful peers
  review_cli.py next                      the next unanswered question
  review_cli.py answer <field> <value>    answer it (validated immediately)
  review_cli.py insufficient <target> --reason
  review_cli.py status                    progress overview
  review_cli.py submit                    assemble and write /app/result.json
"""

import argparse
import json
import os
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
STATE = Path(os.environ.get("REVIEW_STATE", "/app/review-state.json"))
RESULT = Path(os.environ.get("REVIEW_RESULT", "/app/result.json"))


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def survey():
    return load_json(TOOLS / "survey.json")


def roster():
    return load_json(TOOLS / "roster.json")


def state():
    if STATE.is_file():
        return load_json(STATE)
    return {
        "selections": [],
        "skip_reason": None,
        "answers": {},
        "insufficient": {},
    }


def save(st):
    STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")


def fail(message):
    print(f"error: {message}")
    sys.exit(1)


def questions():
    return survey()["questions"]


def kind():
    return survey().get("kind", "peer")


SOCIETY_SUBJECT = "society"


def subjects(st):
    if kind() == "society":
        return [SOCIETY_SUBJECT]
    return [s["target"] for s in st["selections"]]


def peer_only():
    if kind() != "peer":
        fail("this command applies to peer reviews only")


def required_fields(st, target):
    if target in st["insufficient"]:
        return []
    answers = st["answers"].get(target, {})
    required = [q["path"] for q in questions() if q.get("required", True)]
    # An occurrence answered "yes" makes its description mandatory.
    for q in questions():
        path = q["path"]
        if path.endswith(".occurred") and answers.get(path) is True:
            description = path[: -len(".occurred")] + ".description"
            if description not in required and question_by_path(description):
                required.append(description)
    return required


def unanswered(st):
    for target in subjects(st):
        answers = st["answers"].get(target, {})
        for path in required_fields(st, target):
            if path not in answers:
                yield target, path


def question_by_path(path):
    for q in questions():
        if q["path"] == path:
            return q
    return None


def parse_value(question, raw, as_json):
    if as_json:
        try:
            value = json.loads(raw)
        except ValueError as error:
            fail(f"invalid JSON value: {error}")
    else:
        value = raw
    question_kind = question["type"]
    if question_kind == "rating":
        try:
            value = int(value)
        except (TypeError, ValueError):
            fail(f"{question['path']} expects an integer rating")
        if not question["min"] <= value <= question["max"]:
            fail(
                f"{question['path']} must be between {question['min']} and "
                f"{question['max']}"
            )
    elif question_kind == "choice":
        if value not in question["choices"]:
            fail(
                f"{question['path']} must be one of: " + ", ".join(question["choices"])
            )
    elif question_kind == "multi_choice":
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        unknown = [item for item in value if item not in question["choices"]]
        if unknown:
            fail(
                f"{question['path']} has unknown choice(s): "
                + ", ".join(unknown)
                + "; valid: "
                + ", ".join(question["choices"])
            )
        if question.get("max_items") and len(value) > question["max_items"]:
            fail(f"{question['path']} allows at most {question['max_items']}")
        if not value:
            fail(f"{question['path']} needs at least one choice")
    elif question_kind == "list_text":
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            fail(f"{question['path']} expects one or more non-empty strings")
        if question.get("max_items") and len(value) > question["max_items"]:
            fail(f"{question['path']} allows at most {question['max_items']}")
    elif question_kind == "bool_or_null":
        mapping = {"true": True, "false": False, "null": None, "unsure": None}
        if isinstance(value, str):
            if value.lower() not in mapping:
                fail(f"{question['path']} expects true, false, or null")
            value = mapping[value.lower()]
        elif not (value is None or isinstance(value, bool)):
            fail(f"{question['path']} expects true, false, or null")
    else:  # text
        if not isinstance(value, str) or not value.strip():
            fail(f"{question['path']} expects non-empty text")
    return value


def print_question(target, path):
    q = question_by_path(path)
    print(f"target: {target}")
    print(f"field:  {path}  ({q['type']})")
    print(f"\n{q['prompt']}")
    for anchor in q.get("anchors", []):
        print(f"  {anchor}")
    for note in q.get("guidance", []):
        print(f"  note: {note}")
    if q.get("choices"):
        print("  choices: " + "; ".join(q["choices"]))
    print(
        f"\nanswer with: review_cli.py answer {path} <value> "
        f"[--target {target}] [--json]"
    )


def nest(flat):
    out = {}
    for path, value in flat.items():
        node = out
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return out


def cmd_start(_):
    st = state()
    if kind() == "society":
        identity = survey()["identity"]
        total = len(required_fields(st, SOCIETY_SUBJECT))
        print(
            f"You are {identity['agent']}, reviewing the society and its "
            f"infrastructure for run {identity['run_id']} "
            f"({total} questions)."
        )
        print(
            "\nGround every answer in what actually happened to you during "
            "the run — your memory, your repository history, and the forge "
            "record — not in generic expectations."
        )
        print("\nCommands: next; answer <field> <value>; status; submit.")
        save(st)
        return
    ros = roster()
    print(f"You are {ros['evaluator']}, reviewing peers for run {ros['run_id']}.")
    print(
        "\nChoose who to review: agents whose work materially affected yours "
        "this run (or whose work you materially used or depended on). Review "
        "only agents you have real evidence about — your memory, your "
        "repository history, and the forge record are your sources. "
        f"Select up to {ros['max_targets']}; selecting fewer, or none, is a "
        "legitimate answer when interactions were not meaningful."
    )
    print("\nRoster:")
    for name in ros["agents"]:
        marker = " (you)" if name == ros["evaluator"] else ""
        print(f"  {name}{marker}")
    print(
        "\nCommands: select <target> --reason '...'; skip-peers --reason "
        "'...'; next; answer <field> <value>; insufficient <target> "
        "--reason '...'; status; submit."
    )
    save(st)


def cmd_select(args):
    peer_only()
    ros, st = roster(), state()
    if st["skip_reason"]:
        print("note: clearing the earlier skip-peers declaration")
    if args.target == ros["evaluator"]:
        fail("you cannot review yourself")
    if args.target not in ros["agents"]:
        fail(f"unknown agent: {args.target}")
    if any(s["target"] == args.target for s in st["selections"]):
        fail(f"{args.target} is already selected")
    if len(st["selections"]) >= ros["max_targets"]:
        fail(f"at most {ros['max_targets']} targets")
    st["skip_reason"] = None
    st["selections"].append({"target": args.target, "reason": args.reason})
    save(st)
    print(f"selected {args.target} ({len(st['selections'])} total)")


def cmd_unselect(args):
    peer_only()
    st = state()
    st["selections"] = [s for s in st["selections"] if s["target"] != args.target]
    st["answers"].pop(args.target, None)
    st["insufficient"].pop(args.target, None)
    save(st)
    print(f"unselected {args.target}")


def cmd_skip_peers(args):
    peer_only()
    st = state()
    if st["selections"]:
        fail("targets are already selected; unselect them first")
    st["skip_reason"] = args.reason
    save(st)
    print("recorded: no meaningful interactions. Run submit to finish.")


def cmd_next(_):
    st = state()
    if kind() == "peer":
        if st["skip_reason"]:
            print("no peers selected (skip-peers declared); run submit")
            return
        if not st["selections"]:
            print("no targets selected yet; run start for instructions")
            return
    for target, path in unanswered(st):
        print_question(target, path)
        return
    print("all questions answered; run status to double-check, then submit")


def cmd_answer(args):
    st = state()
    q = question_by_path(args.field)
    if q is None:
        fail(f"unknown field: {args.field}")
    if kind() == "society":
        target = SOCIETY_SUBJECT
    else:
        target = args.target
        if target is None:
            pending = list(unanswered(st))
            matching = [t for t, path in pending if path == args.field]
            if not matching:
                matching = [t for t in subjects(st) if t not in st["insufficient"]]
            if len(matching) != 1:
                fail("ambiguous target; pass --target <agent>")
            target = matching[0]
        if target not in subjects(st):
            fail(f"{target} is not a selected target")
        if target in st["insufficient"]:
            fail(f"{target} is marked insufficient_evidence; answers not accepted")
    value = parse_value(q, args.value, args.json)
    st["answers"].setdefault(target, {})[args.field] = value
    save(st)
    remaining = sum(1 for t, _ in unanswered(st) if t == target)
    print(f"recorded {args.field} for {target}; {remaining} remaining for them")


def cmd_insufficient(args):
    peer_only()
    st = state()
    if not any(s["target"] == args.target for s in st["selections"]):
        fail(f"{args.target} is not a selected target")
    st["insufficient"][args.target] = args.reason
    st["answers"].pop(args.target, None)
    save(st)
    print(f"{args.target} marked insufficient_evidence")


def cmd_status(_):
    st = state()
    if kind() == "peer" and st["skip_reason"]:
        print(f"skip-peers: {st['skip_reason']}")
        return
    for target in subjects(st):
        if target in st["insufficient"]:
            print(f"{target}: insufficient_evidence")
            continue
        total = len(required_fields(st, target))
        done = sum(
            1
            for path in required_fields(st, target)
            if path in st["answers"].get(target, {})
        )
        print(f"{target}: {done}/{total} answered")


def cmd_submit(_):
    st = state()
    if kind() == "society":
        pending = list(unanswered(st))
        if pending:
            fail(
                f"{len(pending)} unanswered question(s); run next "
                f"(first: {pending[0][1]})"
            )
        identity = survey()["identity"]
        result = dict(identity)
        result.update(nest(st["answers"].get(SOCIETY_SUBJECT, {})))
        RESULT.write_text(json.dumps(result, indent=1), encoding="utf-8")
        print(
            f"wrote {RESULT}. Final step: write a one-line summary to "
            "/app/response.md — the turn fails without it."
        )
        return
    ros = roster()
    pending = list(unanswered(st))
    if not st["skip_reason"] and not st["selections"]:
        fail("select targets or declare skip-peers first")
    if pending:
        fail(
            f"{len(pending)} unanswered question(s); run next "
            f"(first: {pending[0][1]} for {pending[0][0]})"
        )
    reviews = []
    for selection in st["selections"]:
        target = selection["target"]
        base = {
            "run_id": ros["run_id"],
            "evaluator_agent": ros["evaluator"],
            "target_agent": target,
        }
        if target in st["insufficient"]:
            base["insufficient_evidence"] = True
            base["insufficient_evidence_reason"] = st["insufficient"][target]
        else:
            base.update(nest(st["answers"][target]))
        reviews.append(base)
    result = {
        "run_id": ros["run_id"],
        "evaluator_agent": ros["evaluator"],
        "selections": st["selections"],
        "reviews": reviews,
    }
    if st["skip_reason"]:
        result["no_meaningful_interactions_reason"] = st["skip_reason"]
    RESULT.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(
        f"wrote {RESULT} ({len(reviews)} review(s)). Final step: write a "
        "one-line summary to /app/response.md — the turn fails without it."
    )


def main():
    parser = argparse.ArgumentParser(prog="review_cli.py")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start").set_defaults(func=cmd_start)
    p = sub.add_parser("select")
    p.add_argument("target")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_select)
    p = sub.add_parser("unselect")
    p.add_argument("target")
    p.set_defaults(func=cmd_unselect)
    p = sub.add_parser("skip-peers")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_skip_peers)
    sub.add_parser("next").set_defaults(func=cmd_next)
    p = sub.add_parser("answer")
    p.add_argument("field")
    p.add_argument("value")
    p.add_argument("--target")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_answer)
    p = sub.add_parser("insufficient")
    p.add_argument("target")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_insufficient)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("submit").set_defaults(func=cmd_submit)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
