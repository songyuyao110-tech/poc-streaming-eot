"""A1 verdict: compare two A1 captures (baseline checkout vs changed checkout).

Two levels:
  * strict   - everything, including the LLM's chosen tool and prose;
  * pipeline - the KEV stage (scores, shortlist, schemas, event sequence/payloads), i.e. what the
               refactor was required to preserve. The LLM's choice is stochastic on ambiguous
               utterances even for unchanged code (verified by capturing the baseline twice).
"""
import json
import sys

LLM_KEYS = {"decision", "decision_response", "decision_latency_ms", "latency_ms", "event_elapsed_ms"}
# The refactor's contract is the KEV stage: everything up to and including `llm_start`. After that the
# LLM's choice (and the mock-exec display that quotes it) is stochastic even for unchanged code.
LLM_STAGE_ENDS_AT = "llm_start"


def strip(rows):
    out = []
    for row in rows:
        run = {key: value for key, value in row["run"].items() if key not in LLM_KEYS}
        events = []
        for name, data in row["stream"]:
            events.append((name, {key: value for key, value in data.items() if key not in LLM_KEYS}))
            if name == LLM_STAGE_ENDS_AT:
                break
        out.append({"utterance": row["utterance"], "tool_set": row["tool_set"], "run": run, "events": events})
    return out


def tools(rows):
    return [(row["utterance"], row["run"]["decision"].get("tool")) for row in rows]


if __name__ == "__main__":
    first, second = (json.load(open(path)) for path in sys.argv[1:3])
    print(f"strict   identical: {first == second}")
    print(f"pipeline identical: {strip(first) == strip(second)}  (KEV scores/shortlist/schemas/events)")
    print(f"llm tool choices 1: {tools(first)}")
    print(f"llm tool choices 2: {tools(second)}")