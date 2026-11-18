"""内心戏 · 运行日志落盘（只写真发生的事，不补造）。

一条内心戏会话 = 一个 JSONL 文件 + 图片目录：

    var/neixinxi_log/<session_id>.jsonl
    var/neixinxi_log/media/<session_id>/<seq>.png

第一行是会话元信息，之后每行一条事件。事件字段：

    seq     会话内递增序号（同一毫秒时用它定先后）
    at      真的发生时刻（Unix epoch 毫秒，墙钟）
    t_ms    会话内相对毫秒（页面上的时间线用它排先后）
    track   axis | left | right | meta
    kind    utterance / eot / main / mind / run_end
    payload 原始内容（主链事件保留主链事件名与原字段）

页面只读这些记录；没有记录的就标缺失。这里的写入全部来自真实入口：
`_eot_worker`（判停）、`close_utterance` / `say_text`（原话）、
`push_mind_scenes`（读心）、`_formal_worker`（主链闸/工具/Agent）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
_DATA_URI = re.compile(r"^data:image/(png|jpeg|jpg);base64,([A-Za-z0-9+/=\s]+)$")


def _now_ms() -> int:
    return int(time.time() * 1000)


class NeixinxiLogWriter:
    """一个会话一个写者；多线程（driver / 主链 worker）共用一把锁。"""

    def __init__(self, session_id, meta=None, root=None):
        if not _SAFE_ID.match(session_id or ""):
            raise ValueError("bad neixinxi session id")
        self.session_id = session_id
        self.root = Path(root or os.environ.get("NEIXINXI_LOG_DIR")
                         or Path(__file__).resolve().parents[1] / "var" / "neixinxi_log")
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / (session_id + ".jsonl")
        self.media_dir = self.root / "media" / session_id
        self.lock = threading.RLock()
        self.seq = 0
        self.started_at = _now_ms()
        self.utterance_count = 0
        self.mind_count = 0
        self.frame_count = 0
        self.run_count = 0
        self._axis_dedupe = {}
        record = {
            "v": 1, "type": "session", "session_id": session_id,
            "started_at": self.started_at,
            "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at / 1000)),
            "meta": dict(meta or {}),
        }
        with self.lock:
            with self.path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    # ---- 底层 ----
    def _append(self, event):
        with self.lock:
            self.seq += 1
            event["seq"] = self.seq
            event.setdefault("at", _now_ms())
            event.setdefault("t_ms", 0)
            with self.path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            return self.seq

    @staticmethod
    def _safe_payload(payload):
        """只留能 JSON 化的字段；NaN/Infinity 写成 None（不编数）。"""
        def clean(value):
            if isinstance(value, float):
                return value if value == value and abs(value) != float("inf") else None
            if isinstance(value, dict):
                return {str(k): clean(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [clean(v) for v in value]
            if isinstance(value, (str, int, bool)) or value is None:
                return value
            return str(value)
        return clean(payload)

    # ---- 三类真实事件 ----
    def record_utterance(self, seat, text, why, t_ms):
        """一句原话收住（轴上的那句话）。同一席同一句 3 秒内不重复记。"""
        text = (text or "").strip()
        if not text:
            return None
        key = (seat, text)
        now = _now_ms()
        with self.lock:
            last = self._axis_dedupe.get(key)
            if last is not None and now - last < 3000:
                return None
            self._axis_dedupe[key] = now
            self.utterance_count += 1
        return self._append({
            "track": "axis", "kind": "utterance", "at": now, "t_ms": int(t_ms),
            "payload": {"seat": seat, "text": text, "why": why,
                        "truth": "真实输入（文字发送 / 判停收住）"},
        })

    def record_eot(self, seat, text, verdict, source_mode, latency_ms, t_ms):
        verdict = verdict or {}
        scores = {key: verdict.get(key) for key in ("complete", "cut", "wait")}
        return self._append({
            "track": "left", "kind": "eot", "t_ms": int(t_ms),
            "payload": {
                "seat": seat, "text": text,
                "label": verdict.get("label"), "label_cn": verdict.get("label_cn"),
                "scores": scores,
                "confidence": verdict.get("confidence"),
                "margin": verdict.get("margin"),
                "source": verdict.get("source") or source_mode,
                "source_mode": source_mode,
                "error": verdict.get("error"),
                "latency_ms": latency_ms,
                "may_confirm": bool(verdict.get("may_confirm")),
                "truth": "真实判停结果（来源照实记录）",
            },
        })

    def record_mind(self, scene, frame, model, t_ms):
        """一份读心输出 + 它配的那一帧（有就存图，没有就明确没有）。"""
        scene = scene or {}
        payload = {
            "summary": scene.get("summary"),
            "intent": scene.get("intent"),
            "target": scene.get("target"),
            "topic": scene.get("topic"),
            "cues": scene.get("cues") or [],
            "confidence": scene.get("confidence"),
            "basis": scene.get("basis"),
            "visual": scene.get("visual"),
            "revision": scene.get("revision"),
            "source": scene.get("source"),
            "engine": scene.get("engine"),
            "trigger": scene.get("trigger"),
            "model": model,
            "seats": [{"seat": item.get("seat"), "label": item.get("label"),
                       "text": item.get("text") or item.get("settled") or ""}
                      for item in (scene.get("seats") or [])],
            "truth": "真实读心输出（引擎与来源照实记录）",
        }
        seq = self._append({"track": "right", "kind": "mind", "t_ms": int(t_ms),
                            "payload": payload})
        self.mind_count += 1
        if frame and frame.get("image"):
            ref = self._save_frame(seq, frame)
            if ref:
                self._append({
                    "track": "right", "kind": "frame", "t_ms": int(t_ms),
                    "payload": ref,
                })
                self.frame_count += 1
        return seq

    def _save_frame(self, seq, frame):
        match = _DATA_URI.match(str(frame.get("image") or ""))
        if not match:
            return None
        ext = "png" if match.group(1) == "png" else "jpg"
        try:
            raw = base64.b64decode(match.group(2), validate=False)
        except Exception:  # noqa: BLE001 - 存不下就明确记缺失，不让写日志失败
            return None
        if not raw or len(raw) > 2 * 1024 * 1024:
            return None
        self.media_dir.mkdir(parents=True, exist_ok=True)
        name = "f%06d.%s" % (seq, ext)
        try:
            (self.media_dir / name).write_bytes(raw)
        except Exception:  # noqa: BLE001
            return None
        source = str(frame.get("source") or "camera")
        label = {"camera": "摄像头画面", "test_asset": "测试帧（非摄像头）",
                 "browser_screen": "页面画面采集（非摄像头）"}.get(source, source)
        return {"frame_ref": "/api/neixinxi/log/media/%s/%s" % (self.session_id, name),
                "w": frame.get("w"), "h": frame.get("h"),
                "format": frame.get("format") or ext,
                "bytes": len(raw), "source": source, "label": label,
                "truth": "真实保存的这一帧；来源照实标注"}

    def record_run(self, run_id, seat, utterance, page_accept, events,
                   duo_session_id=None, said="", said_kind="", receipt="",
                   error=None, t_ms=0):
        """一趟主链（/api/session/dual/stream）的原样事件 + 关联键 + 缺口。"""
        events = list(events or [])
        names = [item.get("event") for item in events]
        gate = [name for name in names if name in
                ("gate_decision", "gate_open", "gate_context_only")]
        tool = [name for name in names if name in ("tools_scores", "llm_decision")]
        agent = [name for name in names if name in ("au_decided", "au_executed")]
        missing = []
        if not gate:
            missing.append("判答")
        if not tool:
            missing.append("工具披露")
        if not agent:
            missing.append("Agent 对话")
        for index, item in enumerate(events):
            self._append({
                "track": "left", "kind": "main", "at": int(item.get("at") or _now_ms()),
                "t_ms": int(t_ms) + index,
                "payload": {
                    "name": item.get("event"),
                    "data": item.get("data"),
                    "run_id": run_id,
                    "duo_session_id": duo_session_id,
                    "seat": seat,
                    "utterance": utterance,
                    "truth": "主链 SSE 原样事件（未改写字段）",
                },
            })
        self._append({
            "track": "meta", "kind": "run_end", "t_ms": int(t_ms) + len(events) + 1,
            "payload": {
                "run_id": run_id, "seat": seat, "utterance": utterance,
                "page_accept": page_accept, "duo_session_id": duo_session_id,
                "said": said, "said_kind": said_kind, "receipt": receipt,
                "error": error, "missing": missing,
                "event_names": names,
                "truth": "一趟主链的关联与缺口（缺失照实列出，不补造）",
            },
        })
        self.run_count += 1
        return run_id

    def close(self):
        pass


class NeixinxiLogReader:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("NEIXINXI_LOG_DIR")
                         or Path(__file__).resolve().parents[1] / "var" / "neixinxi_log")

    def sessions(self, limit=100):
        rows = []
        if not self.root.exists():
            return rows
        for path in sorted(self.root.glob("*.jsonl"), key=lambda item: item.stat().st_mtime,
                           reverse=True)[:limit]:
            data = self._read(path)
            if not data:
                continue
            rows.append(self._summary(data))
        rows.sort(key=lambda item: item.get("started_at") or 0, reverse=True)
        return rows

    def get(self, session_id):
        if not _SAFE_ID.match(session_id or ""):
            return None
        path = self.root / (session_id + ".jsonl")
        if not path.is_file():
            return None
        return self._read(path)

    def _read(self, path):
        meta, events = None, []
        try:
            with path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("type") == "session" and meta is None:
                        meta = row
                    elif row.get("type") != "session":
                        events.append(row)
        except OSError:
            return None
        if meta is None:
            return None
        events.sort(key=lambda item: (item.get("at") or 0, item.get("seq") or 0))
        return {"session": meta, "events": events}

    def _summary(self, data):
        meta = data["session"]
        events = data["events"]
        kinds = [item.get("kind") for item in events]
        mind_engine = None
        for item in events:
            if item.get("kind") == "mind":
                payload = item.get("payload") or {}
                mind_engine = payload.get("engine") or payload.get("source")
                break
        utterances = [item for item in events if item.get("kind") == "utterance"]
        started = meta.get("started_at") or 0
        last = events[-1].get("at") if events else started
        return {
            "session_id": meta.get("session_id"),
            "started_at": started,
            "created": meta.get("created"),
            "last_at": last,
            "meta": meta.get("meta") or {},
            "event_count": len(events),
            "utterance_count": len(utterances),
            "mind_count": kinds.count("mind"),
            "frame_count": kinds.count("frame"),
            "run_count": kinds.count("run_end"),
            "gate_count": sum(1 for item in events if item.get("kind") == "main"
                              and (item.get("payload") or {}).get("name") in
                              ("gate_decision", "gate_open", "gate_context_only")),
            "tool_count": sum(1 for item in events if item.get("kind") == "main"
                              and (item.get("payload") or {}).get("name") in
                              ("tools_scores", "llm_decision")),
            "agent_count": sum(1 for item in events if item.get("kind") == "main"
                               and (item.get("payload") or {}).get("name") in
                               ("au_decided", "au_executed")),
            "mind_engine": mind_engine,
            "first_text": (utterances[0].get("payload") or {}).get("text") if utterances else "",
        }
