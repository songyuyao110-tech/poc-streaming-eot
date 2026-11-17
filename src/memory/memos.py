"""记忆落地：MemOS Cloud（memos）真写真查。

口径（wiki `四模块职责与接口契约` §1 记忆行 / §2 接口 3 / §4 开闸分流，落本仓）：

- **写**：只在对话中枢把这句**收住**之后（判停给「说完」/ 静音窗收口），由
  `gate/turn_session._confirm` 在收住那一刻调 `write(turn)`；`turn` 带**这句完整原话 + 这一刻的场景**。
  话还没收住不写；**说完但不回答（只进上下文）的话也写**（那一支同样走收住，不存在「不接就不记」）。
- **查**：**Agent 发起请求那一刻**（`gate/turn_session._start_llm` 提交决策之前）调
  `recall(t_ms, limit, query=本句原话)`，把当时查到的记忆放进 `decide` 的 context；
  不是开闸那一刻查、也不是会话开始时一次性预取。
- **控制**：我们能决定的只有「送什么进 memos」——收住的原话、当刻的场景、席位标签。**它记什么、抽成
  什么结构不由我们指定**：云端会把原话自己归纳成 `memory_key`/`memory_value`（人名、家属关系、偏好、
  计划都抽过），也会把没有可记内容的车控指令原样留成一条。**本模块不做「只留人名和席位」这类本地
  过滤**（那样只是假装可控）——能不能收窄是产品要再拍的，见交付 DETAIL 的探索记录。

凭据纪律（本仓铁律）：

- 只从**进程环境** `MEMOS_API_KEY` 读；没设时按 `MEMOS_ENV_FILE`（默认指本机已有那一份 `.env`）**只读**
  就地取值。**不复制、不写入任何新文件、不进 git、不打印**；任何日志 / 事件 / 回执里只出现「来源标签」
  （`env` / `file` / `missing`），**永远不出现密钥值**（含前后几位）。
- 外部调用失败一律降级不抛：写失败只记回执，查失败返回空列表并标 `status`；记忆不许拖垮判停/开闸。

开关（默认开；没 key 时自动退化成 `NullMemoryStore`）：

- `POC_MEMORY_ENABLED=0` / `MEMOS_DISABLED=1` → 关（主链照常跑，只是不写真查）
- `POC_MEMORY_USER`（默认 `poc-eot-cabin`）：记忆挂在哪个用户空间（跨会话可查）
- `MEMOS_BASE_URL` / `MEMOS_ENV_FILE` / `MEMOS_TIMEOUT_S` / `MEMOS_RECALL_TIMEOUT_MS` / `MEMOS_SEARCH_MODE`
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

from shared.text import normalize

DEFAULT_BASE_URL = "https://memos.memtensor.cn/api/openmem/v1"
DEFAULT_KEY_FILE = "~/Agents/AIOS Framework/.env"
DEFAULT_USER_ID = "poc-eot-cabin"
SEAT_LABEL = {"D": "主驾", "P": "副驾"}
SCENE_PREFIX = "当时场景："
MEMORY_TAG = "poc-streaming-eot"

# 写是后台线程发出去的（收住那一刻只排队，不把判停/开闸的线程挂在云端往返上）。
# 一个进程一份小池就够：一条一版字最多一次写，短时并发也只有两席。
_WRITE_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="memory-write")

# 回执只留最近这么多条（给事件 / 页面看，不落盘无限长）
_KEEP_RECEIPTS = 64


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class MemoryHttpError(RuntimeError):
    """云端调用失败：只带类别与状态码，绝不带密钥或请求头。"""

    def __init__(self, kind: str, detail: str = "", status: int | None = None):
        super().__init__(kind + ("：" + detail if detail else ""))
        self.kind = kind
        self.detail = detail
        self.status = status


def resolve_api_key(environ: dict | None = None, key_file: str | None = None) -> tuple[str, str]:
    """取 memos API key，返回 `(key, 来源标签)`；来源只用于显示，不暴露值。

    先读进程环境，再按 `MEMOS_ENV_FILE`（默认本机已有那份 `.env`）**只读**就地取值。
    任何情况下都不打印、不落盘；缺就返回 `("", "missing")`，由 `build_store` 退化成空实现。
    """
    env = os.environ if environ is None else environ
    key = (env.get("MEMOS_API_KEY") or "").strip()
    if key:
        return key, "env"
    path = Path(os.path.expanduser(key_file or env.get("MEMOS_ENV_FILE") or DEFAULT_KEY_FILE))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", "missing"
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "MEMOS_API_KEY" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() != "MEMOS_API_KEY":
            continue
        value = value.strip().strip('"').strip("'")
        if value:
            return value, "file"
    return "", "missing"


class _HttpTransport:
    """最小 HTTP POST（stdlib）：Authorization: Token <key>，只发 JSON，回错归类不抛细节。"""

    def __init__(self, api_key: str, base_url: str, timeout_s: float = 5.0):
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def post(self, path: str, payload: dict, timeout_s: float | None = None) -> dict:
        url = self.base_url + path
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urlrequest.Request(
            url, data=body, method="POST",
            headers={"Authorization": "Token " + self._api_key, "Content-Type": "application/json"})
        try:
            with urlrequest.urlopen(request, timeout=timeout_s or self.timeout_s) as response:
                raw = response.read().decode("utf-8", "replace")
        except urlerror.HTTPError as error:
            status = getattr(error, "code", None)
            kind = "auth_failed" if status in (401, 403) else "http_error"
            raise MemoryHttpError(kind, "HTTP " + str(status), status) from None
        except urlerror.URLError as error:
            reason = getattr(error, "reason", None)
            timeout_like = "timed out" in str(reason).lower()
            raise MemoryHttpError("timeout" if timeout_like else "network_error",
                                  type(reason).__name__) from None
        except TimeoutError:
            raise MemoryHttpError("timeout", "socket timeout") from None
        except OSError as error:
            raise MemoryHttpError("network_error", type(error).__name__) from None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise MemoryHttpError("parse_error", "响应不是 JSON") from None
        if not isinstance(parsed, dict):
            raise MemoryHttpError("parse_error", "响应不是对象") from None
        if parsed.get("code") not in (None, 0):
            raise MemoryHttpError("service_error", str(parsed.get("message") or "")[:120])
        return parsed


def _clip(text: str, limit: int = 160) -> str:
    body = (text or "").replace("\n", " ").strip()
    return body if len(body) <= limit else body[:limit].rstrip("，。；、,. ;") + "…"


def _scene_brief(scene: dict | None) -> dict:
    """场景只用「那一刻的那句话 + 版本」，不把整份快照塞进这里（快照另有事件可查）。"""
    scene = scene or {}
    summary = (scene.get("summary") or "").strip()
    return {"summary": summary, "revision": scene.get("revision"),
            "updated_at_ms": scene.get("updated_at_ms"), "missing": not bool(summary)}


class MemosMemoryStore:
    """memos 真写真查（一个会话一份，两席共用同一份用户记忆空间）。"""

    enabled = True

    def __init__(self, *, session_id: str, user_id: str = DEFAULT_USER_ID,
                 api_key: str = "", key_source: str = "missing",
                 base_url: str = DEFAULT_BASE_URL, timeout_s: float = 5.0,
                 recall_timeout_ms: int | None = None, search_mode: str = "fast",
                 transport=None, clock=None, pool=None):
        self.session_id = session_id
        self.user_id = user_id
        self.key_source = key_source
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.recall_timeout_ms = int(_env_int("MEMOS_RECALL_TIMEOUT_MS", 1200)
                                     if recall_timeout_ms is None else recall_timeout_ms)
        self.search_mode = search_mode
        self.transport = transport or _HttpTransport(api_key, base_url, timeout_s)
        self.clock = clock or (lambda: int(time.monotonic() * 1000))
        self._pool = pool or _WRITE_POOL
        self._lock = threading.RLock()
        self._last_key: tuple[str, str] | None = None
        self.writes: list[dict] = []
        self.recalls: list[dict] = []
        self.requests: list[dict] = []
        self.counters = {"writes": 0, "writes_ok": 0, "writes_error": 0, "writes_skipped": 0,
                         "recalls": 0, "recalls_ok": 0, "recalls_error": 0, "requests": 0,
                         "write_ms_last": None, "recall_ms_last": None, "recall_hits_last": 0}

    # ---------- 写：收住那一刻 ----------
    def write(self, turn: dict) -> dict:
        """收住那一刻交一刀：`turn` = 这一刻的原话 + 这一刻的场景（两条路由都写）。

        返回回执（给事件 / 页面看）：真正发出去是后台线程，这里只排队并立刻返回 `status=queued`。
        """
        text = str((turn or {}).get("text") or "").strip()
        seat = str((turn or {}).get("seat") or "D").upper()
        scene = _scene_brief((turn or {}).get("scene"))
        t_ms = (turn or {}).get("t_ms")
        receipt = {"status": "queued", "seat": seat, "text": _clip(text),
                   "scene": scene, "t_ms": t_ms, "version": (turn or {}).get("version"),
                   "latency_ms": None, "task_id": None, "error": None}
        if not text:
            receipt["status"] = "skipped"
            receipt["reason"] = "empty_text"
            self._record_write(receipt)
            return receipt
        key = (seat, normalize(text))
        with self._lock:
            if key == self._last_key:
                # 同一句连写两次：memos 不会自己去重（实测会落两条近乎一样的记忆），这里只挡「原话一模一样」。
                receipt["status"] = "skipped"
                receipt["reason"] = "duplicate_text"
                self._record_write(receipt)
                return receipt
            self._last_key = key
            self.counters["writes"] += 1
        payload = self._write_payload(text, seat, scene)
        self._record_write(receipt)
        try:
            self._pool.submit(self._do_write, receipt, payload)
        except RuntimeError as error:      # 池已关（进程收尾）：写这一刀就此记失败，不抛
            receipt["status"] = "error"
            receipt["error"] = {"kind": "pool_closed", "detail": type(error).__name__}
            self._note_write(receipt)
        return receipt

    def _write_payload(self, text: str, seat: str, scene: dict) -> dict:
        """交给 memos 的两条消息：这句原话（`主驾说：“…”`）+ 当时场景（明确标成场景，不冒充用户原话）。

        送法（2026-11-11 用户拍板，见 `tmp/poc-neixinxi-name-land-验收.md` 第 2 条）：原话一律写成
        `主驾说：“<原话>”` / `副驾说：“<原话>”`，不再用「席位 + 冒号」的旧前缀。
        旧前缀里的那个冒号会让 memos 以为「主驾这个人就是被叫到的张先生」（实测错席），
        加一个「说」字把说话人和被称呼的人分开，错席在输入侧就不会产生（`poc-memos-say-name` 亲测）。
        只有**收住**的话才走到这里，所以右引号总是补上（不收住的半句根本不写）。
        """
        messages = [{"role": "user",
                     "content": SEAT_LABEL.get(seat, seat) + "说：“" + text + "”"}]
        if scene.get("summary"):
            messages.append({"role": "assistant",
                             "content": SCENE_PREFIX + scene["summary"]})
        tags = ["seat:" + seat, MEMORY_TAG]
        if scene.get("revision") is not None:
            tags.append("scene_rev:" + str(scene["revision"]))
        return {"messages": messages, "user_id": self.user_id,
                "conversation_id": self.session_id, "tags": tags}

    def _do_write(self, receipt: dict, payload: dict) -> None:
        began = time.monotonic()
        try:
            body = self.transport.post("/add/message", payload)
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            receipt["status"] = "ok"
            receipt["task_id"] = data.get("task_id")
        except MemoryHttpError as error:
            receipt["status"] = "error"
            receipt["error"] = {"kind": error.kind, "detail": error.detail, "status": error.status}
        except Exception as error:      # noqa: BLE001 - 记忆失败不许拖垮主链，但必须如实记账
            receipt["status"] = "error"
            receipt["error"] = {"kind": type(error).__name__, "detail": str(error)[:120]}
        receipt["latency_ms"] = round((time.monotonic() - began) * 1000, 1)
        self._note_write(receipt)

    def _record_write(self, receipt: dict) -> None:
        with self._lock:
            self.writes.append(receipt)
            del self.writes[:-_KEEP_RECEIPTS]
            if receipt.get("status") == "skipped":
                self.counters["writes_skipped"] += 1

    def _note_write(self, receipt: dict) -> None:
        with self._lock:
            status = receipt.get("status")
            if status == "ok":
                self.counters["writes_ok"] += 1
            elif status == "error":
                self.counters["writes_error"] += 1
            if receipt.get("latency_ms") is not None:
                self.counters["write_ms_last"] = receipt["latency_ms"]

    # ---------- 查：Agent 发起请求那一刻 ----------
    def recall(self, t_ms: int | None = None, limit: int = 5, query: str = "") -> list[str]:
        """发起请求那一刻按这句原话查当时能查到的记忆（**同步、有上限**，失败就空列表）。"""
        text = (query or "").strip()
        receipt = {"status": "skipped", "reason": "empty_query", "t_ms": t_ms, "count": 0,
                   "items": [], "latency_ms": None, "error": None}
        if not text:
            return self._note_recall(receipt)
        top_k = max(1, int(limit))
        began = time.monotonic()
        try:
            body = self.transport.post(
                "/search/memory",
                {"query": text, "mode": self.search_mode, "user_id": self.user_id, "top_k": top_k},
                timeout_s=self.recall_timeout_ms / 1000.0)
            items = self._parse_items(body, top_k)
            receipt.update(status="ok", items=items, count=len(items))
        except MemoryHttpError as error:
            receipt.update(status="error", reason=error.kind,
                           error={"kind": error.kind, "detail": error.detail, "status": error.status})
        except Exception as error:      # noqa: BLE001 - 同上：查失败不该影响这次决策
            receipt.update(status="error", reason=type(error).__name__,
                           error={"kind": type(error).__name__, "detail": str(error)[:120]})
        receipt["latency_ms"] = round((time.monotonic() - began) * 1000, 1)
        return self._note_recall(receipt)

    def _parse_items(self, body: dict, limit: int) -> list[str]:
        """把云端三种列表压成「一段一段可读的记忆」（不带 id / 不带分数字段）。"""
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        candidates: list[tuple[float, str]] = []

        def push(item: dict, text: str, score: float = 0.0):
            clipped = _clip(text, 200)
            if clipped:
                candidates.append((score, clipped))

        for item in data.get("memory_detail_list") or []:
            if isinstance(item, dict):
                push(item, item.get("memory_value") or item.get("memory_key") or "",
                     float(item.get("relativity") or 0.0))
        for item in data.get("preference_detail_list") or []:
            if isinstance(item, dict):
                push(item, item.get("preference") or item.get("memory_value") or "",
                     float(item.get("relativity") or 0.0))
        for item in data.get("event_detail_list") or []:
            if isinstance(item, dict):
                push(item, item.get("memory_value") or item.get("memory_key") or "",
                     float(item.get("relativity") or 0.0))
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        seen: list[str] = []
        for _, text in candidates:
            if text in seen:
                continue
            seen.append(text)
        return seen[:limit]

    def _note_recall(self, receipt: dict) -> list[str]:
        with self._lock:
            self.recalls.append({key: value for key, value in receipt.items() if key != "items"})
            del self.recalls[:-_KEEP_RECEIPTS]
            self.counters["recalls"] += 1
            if receipt.get("status") == "ok":
                self.counters["recalls_ok"] += 1
                self.counters["recall_hits_last"] = receipt.get("count") or 0
            elif receipt.get("status") == "error":
                self.counters["recalls_error"] += 1
            if receipt.get("latency_ms") is not None:
                self.counters["recall_ms_last"] = receipt["latency_ms"]
            self.last_recall = receipt
        return list(receipt.get("items") or [])

    # ---------- 对账：这一次请求带了什么 ----------
    def remember_request(self, request: dict) -> dict:
        """Agent 接手时把「这一刻的场景 + 查到的记忆」记成一次请求（内存里留最近若干条，便于对账）。"""
        request = request or {}
        scene = _scene_brief(request.get("scene"))
        receipt = {"t_ms": request.get("t_ms"), "seat": request.get("seat"),
                   "text": _clip(request.get("text") or "", 120),
                   "scene": scene, "memory": [_clip(item, 200) for item in (request.get("memory") or [])],
                   "memory_count": len(request.get("memory") or [])}
        with self._lock:
            self.requests.append(receipt)
            del self.requests[:-_KEEP_RECEIPTS]
            self.counters["requests"] += 1
        return receipt

    def status(self) -> dict:
        """页面 / 排查用：只报「开没开、挂在谁、几条成功几条失败」，**不含任何密钥信息**。"""
        with self._lock:
            return {"enabled": True, "kind": "memos", "session_id": self.session_id,
                    "user_id": self.user_id, "base_url": self.base_url,
                    "key_source": self.key_source, "search_mode": self.search_mode,
                    "recall_timeout_ms": self.recall_timeout_ms,
                    **dict(self.counters)}

    def close(self) -> None:
        """本会话不再写：池是进程级共享的，这里不清空，留回执给对账。"""
        return None


class NullMemoryStore:
    """记忆没开（没有 key / 显式关掉）：主链照常跑，写查都明确回「没开」。"""

    enabled = False

    def __init__(self, reason: str = "memos 未启用", *, session_id: str = "",
                 user_id: str = DEFAULT_USER_ID):
        self.reason = reason
        self.session_id = session_id
        self.user_id = user_id
        self.writes: list[dict] = []
        self.recalls: list[dict] = []
        self.requests: list[dict] = []

    def _receipt(self, **extra) -> dict:
        receipt = {"status": "disabled", "reason": self.reason, "latency_ms": None,
                   "error": None, **extra}
        self.writes.append(receipt)
        del self.writes[:-8]
        return receipt

    def write(self, turn: dict) -> dict:
        return self._receipt(seat=(turn or {}).get("seat"), text=_clip((turn or {}).get("text") or ""),
                             scene=_scene_brief((turn or {}).get("scene")),
                             t_ms=(turn or {}).get("t_ms"), version=(turn or {}).get("version"))

    def recall(self, t_ms: int | None = None, limit: int = 5, query: str = "") -> list[str]:
        self.recalls.append({"status": "disabled", "reason": self.reason, "t_ms": t_ms, "count": 0})
        del self.recalls[:-8]
        self.last_recall = self.recalls[-1]
        return []

    def remember_request(self, request: dict) -> dict:
        receipt = {"t_ms": (request or {}).get("t_ms"), "text": _clip((request or {}).get("text") or "", 120),
                   "memory_count": 0, "status": "disabled"}
        self.requests.append(receipt)
        del self.requests[:-8]
        return receipt

    def status(self) -> dict:
        return {"enabled": False, "kind": "null", "session_id": self.session_id,
                "user_id": self.user_id, "reason": self.reason}

    def close(self) -> None:
        return None


def build_store(*, session_id: str = "", user_id: str | None = None,
                enabled: bool | None = None, environ: dict | None = None,
                transport=None, **kwargs) -> MemosMemoryStore | NullMemoryStore:
    """按环境装配一份记忆：有 key 且没被关掉 → memos 真实现；否则空实现（不抛、不猜）。"""
    env = os.environ if environ is None else environ
    if enabled is None:
        enabled = not (str(env.get("POC_MEMORY_ENABLED", "1")) == "0"
                       or str(env.get("MEMOS_DISABLED", "0")) == "1")
    user = user_id or env.get("POC_MEMORY_USER") or DEFAULT_USER_ID
    if not enabled:
        return NullMemoryStore("记忆被显式关掉（POC_MEMORY_ENABLED=0 / MEMOS_DISABLED=1）",
                               session_id=session_id, user_id=user)
    api_key, source = resolve_api_key(env, kwargs.pop("key_file", None))
    if not api_key:
        return NullMemoryStore("memos 未启用：没有 MEMOS_API_KEY（进程环境与 MEMOS_ENV_FILE 都没有）",
                               session_id=session_id, user_id=user)
    return MemosMemoryStore(session_id=session_id, user_id=user, api_key=api_key,
                            key_source=source,
                            base_url=env.get("MEMOS_BASE_URL", DEFAULT_BASE_URL),
                            timeout_s=_env_float("MEMOS_TIMEOUT_S", 5.0),
                            search_mode=env.get("MEMOS_SEARCH_MODE", "fast"),
                            transport=transport, **kwargs)
