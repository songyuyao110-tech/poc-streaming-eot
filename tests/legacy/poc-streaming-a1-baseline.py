"""A1 regression harness: /api/run and /api/run/stream must behave the same before/after the split."""
import codecs
import json
import sys
import urllib.request

VOLATILE = {"run_id", "result_path", "event_elapsed_ms", "ui_total_latency_ms", "latency_ms",
            "kev_latency_ms", "kev_model_latency_ms", "kev_wall_ms", "kev_stage1_ms", "kev_stage2_ms",
            "assembly_latency_ms", "decision_latency_ms", "kev_stage1", "kev_stage2_batches",
            "kev_stage2_concurrency", "kev_http_pool_size", "kev_stage2"}

SAMPLES = [("导航回家", "core_score"),
           ("我搬家了，先打开家庭地址设置页面，暂时不要开始导航。", "core_score"),
           ("取消当前导航", "core_score"),
           ("打开空调", "core_score")]


def scrub(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in VOLATILE:
                continue
            if key == "decision_response":
                # Model prose varies run to run; compare structure and the chosen call only.
                result[key] = {"model": item.get("model"),
                               "content": [{"type": part.get("type"), "name": part.get("name"), "input": part.get("input")}
                                           for part in item.get("content", []) if part.get("type") == "tool_use"]}
                continue
            result[key] = scrub(item)
        return result
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def post(port, path, body):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(request, timeout=180)


def call_run(port, utterance, tool_set):
    with post(port, "/api/run", {"utterance": utterance, "tool_set": tool_set}) as response:
        return response.status, scrub(json.loads(response.read()))


def call_stream(port, utterance, tool_set):
    events = []
    with post(port, "/api/run/stream", {"utterance": utterance, "tool_set": tool_set}) as response:
        status = response.status
        buffer = ""
        decoder = codecs.getincrementaldecoder("utf-8")()
        while True:
            chunk = response.read(1)
            if not chunk:
                break
            buffer += decoder.decode(chunk)
            while "\n\n" in buffer:
                packet, buffer = buffer.split("\n\n", 1)
                lines = packet.split("\n")
                name = next(line[7:] for line in lines if line.startswith("event: "))
                data = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
                events.append((name, scrub(data)))
    return status, events


if __name__ == "__main__":
    port, out = int(sys.argv[1]), sys.argv[2]
    result = []
    for utterance, tool_set in SAMPLES:
        run_status, run_body = call_run(port, utterance, tool_set)
        stream_status, stream_events = call_stream(port, utterance, tool_set)
        result.append({"utterance": utterance, "tool_set": tool_set,
                       "run_status": run_status, "run": run_body,
                       "stream_status": stream_status,
                       "stream_events": [name for name, _ in stream_events],
                       "stream": stream_events})
        print(f"  ok {tool_set:<12} {utterance[:14]:<16} run={run_status} events={len(stream_events)}",
              file=sys.stderr)
    with open(out, "w") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"wrote {out}")