"""Extract candidate correction windows from a claude.ai data export.

Same finders as the transcript pass, different container. Chat has no tool calls,
so the "long output" threshold is measured on prose alone.

Usage: python3 tools/extract_chat.py <path-to-unzipped-export-dir>
Writes: findings/candidates_chat.jsonl
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import redact  # noqa: E402
from common import (  # noqa: E402
    CORRECTION_PHRASE,
    NEGATION_LEAD,
    PROFANITY,
    USER_ROLE,
    ensure_findings_dir,
    positional_args,
    is_user_turn,
    write_jsonl,
)

SHORT_PROMPT = 400
LONG_OUTPUT = 2000
BACK = 3
FORWARD = 3
CLIP = 1800


def message_text(msg):
    text = msg.get("text") or ""
    if text:
        return text
    out = []
    for block in msg.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(block.get("text") or "")
    return "\n".join(out)


def flags_for(text, out_size):
    flags = []
    if PROFANITY.search(text):
        flags.append("profanity")
    if NEGATION_LEAD.search(text):
        flags.append("negation_lead")
    if CORRECTION_PHRASE.search(text):
        flags.append("correction_phrase")
    if len(text) < SHORT_PROMPT and out_size > LONG_OUTPUT:
        flags.append("short_after_long")
    return flags


def main():
    args = positional_args()
    if not args:
        sys.exit("usage: extract_chat.py <unzipped-export-dir>")
    export = args[0]
    with open(os.path.join(export, "conversations.json")) as fh:
        convs = json.load(fh)

    candidates = []
    stats = {"conversations": 0, "human_msgs": 0, "flagged": 0}

    for conv in convs:
        msgs = conv.get("chat_messages") or []
        if not msgs:
            continue
        # Redact whole messages and the title before any clip: a cut secret matches no rule.
        msgs, _ = redact.redact_until_clean(msgs)
        named, _ = redact.redact_until_clean([{"name": conv.get("name")}])
        conv = {**conv, "name": named[0]["name"] if named else None}
        if not msgs:
            continue
        stats["conversations"] += 1
        turns = [
            {
                "role": USER_ROLE if m.get("sender") == "human" else "claude",
                "text": message_text(m)[:CLIP],
                "full_len": len(message_text(m)),
                "ts": m.get("created_at"),
                "uuid": m.get("uuid"),
            }
            for m in msgs
        ]

        for i, turn in enumerate(turns):
            if not is_user_turn(turn):
                continue
            stats["human_msgs"] += 1
            out_size = 0
            for prev in reversed(turns[:i]):
                if is_user_turn(prev):
                    break
                out_size += prev["full_len"]
            flags = flags_for(turn["text"], out_size)
            if not flags:
                continue
            stats["flagged"] += 1

            following = turns[i + 1 :]
            next_user = next(
                (j for j, t in enumerate(following) if is_user_turn(t)), None
            )
            candidates.append(
                {
                    # Message uuid, never position - see extract_transcripts.
                    "id": f"{conv.get('uuid', '?')[:8]}:{turn.get('uuid', '')[:12]}",
                    "source": "chat",
                    "conversation": conv.get("name"),
                    "ts": turn["ts"],
                    "turn": i,
                    "flags": flags,
                    "prompt": turn["text"][:4000],
                    "preceding_output_chars": out_size,
                    "preceding_tool_calls": 0,
                    "turns_before_next_prompt": next_user,
                    "thread_ended_here": next_user is None,
                    "window": turns[max(0, i - BACK) : i + FORWARD + 1],
                }
            )

    out = os.path.join(ensure_findings_dir(), "candidates_chat.jsonl")
    write_jsonl(out, candidates)
    print(json.dumps(stats))
    print(f"wrote {len(candidates)} candidates -> {out}")


if __name__ == "__main__":
    main()
