"""OneBot 11 接入层。

包含三部分：
1. :func:`segment_text` —— 把 OneBot 消息段数组（或 CQ 码字符串）拍平成可读文本；
2. :func:`parse_event` —— 把 OneBot 事件转换成一条本地消息记录，并按配置过滤；
3. :class:`ReportServer` / :class:`OneBotClient` —— 事件上报服务端与 API 调用客户端。

传输方式选用 **HTTP 上报 + HTTP API**（而非 WebSocket），这样整个程序可以只用
Python 标准库实现，零第三方依赖。NapCat 侧需要分别配置「HTTP 上报」与「HTTP 服务端」。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import Config, FilterCfg, OneBotCfg

log = logging.getLogger("qqdigest.onebot")

# 非文本消息段的中文占位符
_SEGMENT_LABEL = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "rps": "[猜拳]",
    "dice": "[骰子]",
    "poke": "[戳一戳]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "markdown": "[卡片]",
    "forward": "[合并转发]",
    "reply": "[回复]",
    "music": "[音乐]",
    "location": "[位置]",
}

#: 纯占位/无信息量的内容：整条消息只有这些东西时直接丢弃
_NOISE_LABELS = {
    "[表情]",
    "[图片]",
    "[语音]",
    "[视频]",
    "[猜拳]",
    "[骰子]",
    "[戳一戳]",
    "[卡片]",
    "[音乐]",
    "[位置]",
    "[回复]",
    "[合并转发]",
}

#: 内容里混有占位符时的紧凑写法（保留"发过一张图"这个事实，但不再占篇幅）
_COMPACT_LABEL = {
    "[图片]": "[图]",
    "[表情]": "[表情]",
    "[语音]": "[语音]",
    "[视频]": "[视频]",
    "[卡片]": "[卡片]",
    "[合并转发]": "[转发]",
    "[回复]": "",
    "[戳一戳]": "",
    "[猜拳]": "",
    "[骰子]": "",
    "[音乐]": "",
    "[位置]": "[位置]",
}

#: 纯语气词/纯刷屏，没有信息量。
#:
#: ⚠️ 这里只放**确定无信息量**的东西。曾经把「在吗」「收到」「好的」也列进来，
#: 结果实测把"在吗"这种**等我回复的提问**整条丢掉了——宁可多留一条噪音，
#: 也不要误杀一条可能需要我行动的消息。
_NOISE_TEXTS = {
    # 注意：不要收录单独的 "?" / "？" —— 那往往是在追问，属于等我回应
    "。", "，", ",", ".", "!", "！", "~", "～", "、", "：", ":", ";", "；",
    "1", "11", "111", "1111", "…", "...", "。。", "。。。",
    "嗯", "嗯嗯", "哦", "噢", "啊", "哈", "哈哈", "哈哈哈", "哈哈哈哈",
    "额", "呃", "顶", "+1", "草", "笑死", "6",
}

_CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")


def clean_text(text: str, *, strip_placeholders: bool = True) -> str:
    """清理消息正文：剔除纯占位内容、压缩混合占位符、去掉纯语气词。

    目标：喂给模型的上下文里不要塞满 `[图片]` `[表情]` 这类无法理解的东西。

    规则：
    - 整条只有占位符/语气词 -> 返回空串（调用方应据此丢弃整条消息）；
    - 混有文字的 -> 把占位符换成紧凑写法（`[图片]` → `[图]`），保留"发过图"这个事实；
    - ``strip_placeholders=False`` 时原样返回。
    """
    if not text:
        return ""
    if not strip_placeholders:
        return text.strip()

    cleaned = text
    for long_form, short_form in _COMPACT_LABEL.items():
        cleaned = cleaned.replace(long_form, short_form)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # 去掉只剩标点的残留
    if not cleaned or cleaned in _NOISE_TEXTS:
        return ""
    # 只有占位符（可能多个）也算空
    only_placeholders = re.sub(r"\[[^\]]{1,8}\]", "", cleaned).strip()
    if not only_placeholders:
        return ""
    return cleaned


# --------------------------------------------------------------------------
# 消息段 -> 文本
# --------------------------------------------------------------------------


def _unescape_cq(value: str) -> str:
    return (
        value.replace("&#44;", ",")
        .replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&amp;", "&")
    )


def _cq_to_segments(raw: str) -> list[dict[str, Any]]:
    """把 CQ 码字符串解析为消息段数组（兼容 NapCat 的 string 消息格式）。"""
    segments: list[dict[str, Any]] = []
    pos = 0
    for m in _CQ_RE.finditer(raw):
        if m.start() > pos:
            segments.append({"type": "text", "data": {"text": raw[pos : m.start()]}})
        cq_type = m.group(1)
        data: dict[str, Any] = {}
        for pair in m.group(2).lstrip(",").split(","):
            if not pair:
                continue
            if "=" in pair:
                k, v = pair.split("=", 1)
                data[k.strip()] = _unescape_cq(v)
        segments.append({"type": cq_type, "data": data})
        pos = m.end()
    if pos < len(raw):
        segments.append({"type": "text", "data": {"text": raw[pos:]}})
    return segments


def normalize_message(message: Any) -> list[dict[str, Any]]:
    """把 ``message`` 字段统一成消息段数组。"""
    if isinstance(message, list):
        return [s for s in message if isinstance(s, dict)]
    if isinstance(message, str):
        return _cq_to_segments(message)
    return []


def segment_text(message: Any) -> tuple[str, list[str], bool]:
    """拍平消息段。

    :return: ``(文本, 出现过的非文本段类型列表, 是否 @ 了自己)``
    """
    text_parts: list[str] = []
    other_types: list[str] = []
    at_self = False

    for seg in normalize_message(message):
        seg_type = str(seg.get("type", ""))
        data = seg.get("data") or {}
        if not isinstance(data, dict):
            data = {}

        if seg_type == "text":
            text_parts.append(str(data.get("text", "")))
        elif seg_type == "at":
            qq = str(data.get("qq", ""))
            name = str(data.get("name", "")).strip()
            if qq == "all":
                text_parts.append("@全体成员")
            else:
                text_parts.append(f"@{name or qq}")
        elif seg_type == "file":
            filename = str(data.get("file") or data.get("name") or "未知文件")
            text_parts.append(f"[文件: {filename}]")
            other_types.append("file")
        else:
            text_parts.append(_SEGMENT_LABEL.get(seg_type, f"[{seg_type}]"))
            other_types.append(seg_type)

    text = "".join(text_parts).strip()
    return text, other_types, at_self


def _mark_at_self(message: Any, self_id: str) -> bool:
    for seg in normalize_message(message):
        if str(seg.get("type")) != "at":
            continue
        data = seg.get("data") or {}
        if str(data.get("qq", "")) == self_id:
            return True
    return False


# --------------------------------------------------------------------------
# 事件 -> 本地记录
# --------------------------------------------------------------------------


def parse_event(
    event: dict[str, Any],
    cfg: Config,
) -> tuple[dict[str, Any] | None, str]:
    """把 OneBot 事件转换为本地消息记录。

    :return: ``(记录或None, 原因说明)``。记录为 None 表示该事件被忽略/过滤。
    """
    post_type = str(event.get("post_type", ""))
    if post_type in ("meta_event", "notice", "request"):
        return None, f"忽略非消息事件 post_type={post_type}"
    if post_type not in ("message", "message_sent"):
        return None, f"忽略未知事件 post_type={post_type!r}"

    self_id = str(event.get("self_id") or cfg.onebot.self_id)
    message_type = str(event.get("message_type", ""))
    sender = event.get("sender") or {}
    if not isinstance(sender, dict):
        sender = {}

    # 方向：message_sent 一定是自己发出；message 用 sender.user_id 判断
    outgoing = post_type == "message_sent" or str(sender.get("user_id", "")) == self_id
    direction = 1 if outgoing else 0

    if message_type == "private":
        chat_type = "private"
        if outgoing:
            # NapCat 的 message_sent 事件里，user_id 放的是**发送者自己**，
            # 真正的对端在扩展字段 peerUin（或部分实现的 target_id）。
            # 实测原始事件：
            #   "post_type":"message_sent","user_id":10000001,
            #   "sender":{"user_id":10000001},"peerUin":"10000002"
            # 若只按标准 OneBot 取 user_id，会把"我发出去的"错记成"自己发给自己"。
            peer_id = str(
                event.get("peerUin") or event.get("target_id") or event.get("peer_id") or ""
            ).strip()
            if not peer_id or peer_id == self_id:
                peer_id = str(event.get("user_id", "")).strip()
            # message_sent 不携带对端昵称（sender 是自己），留空由后续填充
            peer_name = str(event.get("peerName") or "").strip()
        else:
            peer_id = str(event.get("user_id", "")).strip()
            peer_name = str(sender.get("nickname") or "").strip()
    elif message_type == "group":
        peer_id = str(event.get("group_id", ""))
        peer_name = str(event.get("group_name") or "").strip()
        chat_type = "group"
    else:
        return None, f"忽略不支持的消息类型 message_type={message_type!r}"

    if not peer_id or peer_id == "0":
        return None, "事件缺少有效的 user_id/group_id"
    if outgoing and chat_type == "private" and peer_id == self_id:
        return None, (
            "message_sent 私聊事件缺少对端标识（peerUin/target_id 都为空），"
            "无法判断这条消息发给了谁，已跳过"
        )

    # ---- 过滤 ----
    if not _passes_filter(chat_type, peer_id, cfg.filter):
        return None, f"按配置过滤：chat_type={chat_type} peer_id={peer_id}"

    text, other_types, _ = segment_text(event.get("message"))
    if not text and not other_types:
        return None, "空消息，忽略"
    if not text:
        text = "".join(_SEGMENT_LABEL.get(t, f"[{t}]") for t in other_types)

    # 剔除纯占位内容（[图片]/[表情]/纯语气词…），避免污染上下文
    raw_text = text
    text = clean_text(text, strip_placeholders=cfg.filter.strip_placeholders)
    if not text:
        return None, f"无有效内容，已剔除（原文 {raw_text[:40]!r}）"

    msg_id = event.get("message_id")
    if msg_id is None:
        # 极少数实现不带 message_id，用时间去重兜底
        msg_id = f"gen-{event.get('time', int(time.time()))}-{peer_id}-{abs(hash(text)) % 10**8}"

    at_self = _mark_at_self(event.get("message"), self_id)
    highlight = 1 if (at_self or (chat_type == "private" and "?" in text)) else 0
    important = 1 if matches_important(text, peer_id, cfg) else 0

    content_type = "text" if not other_types else "+".join(sorted(set(other_types)))

    record = {
        "msg_id": str(msg_id),
        "ts": int(event.get("time") or time.time()),
        "direction": direction,
        "chat_type": chat_type,
        "peer_id": peer_id,
        "peer_name": peer_name,
        "sender_id": str(sender.get("user_id") or ""),
        "sender_name": str(sender.get("card") or sender.get("nickname") or "").strip(),
        "content_type": content_type,
        "text": text,
        "highlight": highlight,
        "important": important,
        "muted": 0,
        "collected_at": int(time.time()),
    }
    return record, "ok"


def matches_important(text: str, peer_id: str, cfg: Config) -> bool:
    """判断消息是否命中「重要」规则。

    命中条件（满足任一即可）：
    1. 会话对象（好友 QQ 号 / 群号）在 ``important.peers`` 中；
    2. 正文包含 ``important.keywords`` 中的任意一个关键词（大小写不敏感）。
    """
    imp = cfg.important
    if peer_id and peer_id in imp.peers:
        return True
    if not imp.keywords:
        return False
    haystack = (text or "").lower()
    return any(kw.lower() in haystack for kw in imp.keywords if kw)


def _passes_filter(chat_type: str, peer_id: str, f: FilterCfg) -> bool:
    if peer_id in f.peer_blacklist:
        return False
    if chat_type == "private":
        return f.collect_private
    if chat_type == "group":
        if not f.collect_group:
            return False
        if f.group_whitelist and peer_id not in f.group_whitelist:
            return False
        return True
    return False


# --------------------------------------------------------------------------
# 事件上报服务端
# --------------------------------------------------------------------------

EventHandler = Callable[[dict[str, Any]], None]


def _port_in_use(host: str, port: int) -> bool:
    """探测端口上是否已经有服务在监听并接受连接。"""
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


class _ReportHandler(BaseHTTPRequestHandler):
    server_version = "qqdigest/1.0"

    # 通过类属性注入（HTTPServer 实例上挂载）
    cfg: Config
    on_event: EventHandler

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        log.debug("report %s", fmt % args)

    # ------------------------------------------------------------- 工具
    def _authorized(self) -> bool:
        token = self.cfg.onebot.report_token
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {token}":
            return True
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        for pair in query.split("&"):
            if pair.startswith("token=") and pair[6:] == token:
                return True
        return False

    def _respond(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------- 请求体
    def _read_body(self) -> bytes:
        """读取请求体，**同时支持 Content-Length 与 chunked 分块传输**。

        这一点很关键：NapCat 用 Node 的 http 客户端上报事件，未设置
        ``Content-Length`` 时会退化为 ``Transfer-Encoding: chunked``。
        ``BaseHTTPRequestHandler`` 不会自动解码分块，如果只按 Content-Length
        读取，就会读到空body并回 400 —— 表现为"NapCat 一直上报失败"。
        （这个坑是接真实 NapCat 时才暴露出来的。）
        """
        encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in encoding:
            return self._read_chunked()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _read_chunked(self) -> bytes:
        """按 HTTP/1.1 分块编码逐块读取，读到 0 长度块为止。"""
        chunks: list[bytes] = []
        while True:
            size_line = self.rfile.readline(65536)
            if not size_line:
                break
            size_token = size_line.strip().split(b";", 1)[0]
            try:
                size = int(size_token, 16)
            except ValueError:
                break
            if size <= 0:
                # 读掉 trailer 直到空行
                while True:
                    trailer = self.rfile.readline(65536)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            chunks.append(self.rfile.read(size))
            self.rfile.read(2)  # 块尾的 CRLF
        return b"".join(chunks)

    # ------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/"):
            self._respond(200, {"status": "ok", "service": "qqdigest report server"})
        else:
            self._respond(404, {"status": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path != self.cfg.onebot.report_path:
            self._respond(404, {"status": "wrong path", "expected": self.cfg.onebot.report_path})
            return
        if not self._authorized():
            log.warning("上报请求鉴权失败，来源 %s", self.client_address[0])
            self._respond(401, {"status": "unauthorized"})
            return
        try:
            raw = self._read_body()
        except Exception as exc:
            log.warning("读请求体失败：%s", exc)
            self._respond(400, {"status": "bad body"})
            return
        if not raw:
            self._respond(400, {"status": "empty body"})
            return
        try:
            event = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            log.warning("上报内容不是合法 JSON：%s", exc)
            self._respond(400, {"status": "bad json"})
            return
        if not isinstance(event, dict):
            self._respond(400, {"status": "expect object"})
            return
        try:
            self.on_event(event)
        except Exception:
            log.exception("处理上报事件时出错")
        # OneBot 11 要求返回 retcode=0，否则部分实现会重试
        self._respond(200, {"status": "ok", "retcode": 0})


class ReportServer:
    """接收 NapCat HTTP 上报的轻量服务端（仅监听本机）。"""

    def __init__(self, cfg: Config, on_event: EventHandler) -> None:
        self.cfg = cfg
        # 绑定前先探测端口：Windows 上 SO_REUSEADDR 允许两个进程绑同一端口，
        # 于是新进程"启动成功"却收不到任何请求，连接全被旧进程接走。
        # 那种情况下你以为在看新代码的行为，其实是在看一个僵尸进程 —— 极难排查。
        # 这里主动探测并明确报错，把"幽灵进程"问题变成一句清晰的提示。
        if cfg.onebot.report_port != 0 and _port_in_use(
            cfg.onebot.report_host, cfg.onebot.report_port
        ):
            raise OSError(
                f"{cfg.onebot.report_host}:{cfg.onebot.report_port} 上已经有服务在响应："
                "很可能已经有一个 qqdigest 实例（后端或 watch）在运行。"
                "请先停掉它再启动新的；否则新实例虽然能绑定端口，却收不到任何上报事件。"
            )

        handler = type(
            "BoundReportHandler",
            (_ReportHandler,),
            {"cfg": cfg, "on_event": staticmethod(on_event)},
        )
        self._httpd = ThreadingHTTPServer(
            (cfg.onebot.report_host, cfg.onebot.report_port), handler
        )
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}{self.cfg.onebot.report_path}"

    def start_background(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="report-server", daemon=True
        )
        self._thread.start()
        log.info("事件上报服务已启动：%s", self.address)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread = None


# --------------------------------------------------------------------------
# OneBot API 客户端
# --------------------------------------------------------------------------


class OneBotError(RuntimeError):
    """调用 OneBot API 失败。"""


class OneBotClient:
    """调用 NapCat 的 OneBot 11 HTTP 接口。"""

    def __init__(self, cfg: OneBotCfg) -> None:
        self.cfg = cfg

    def call(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.cfg.api_base}/{action}"
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.cfg.api_token:
            headers["Authorization"] = f"Bearer {self.cfg.api_token}"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.api_timeout_sec) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise OneBotError(f"{action} HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise OneBotError(
                f"{action} 无法连接 {self.cfg.api_base}：{exc.reason}。"
                "请确认 NapCat 已启动且开启了 HTTP 服务端"
            ) from exc
        except Exception as exc:
            raise OneBotError(f"{action} 调用异常：{exc}") from exc

        if isinstance(data, dict) and data.get("status") == "failed":
            raise OneBotError(f"{action} 失败：{data.get('message') or data.get('wording')}")
        return data if isinstance(data, dict) else {}

    def get_login_info(self) -> dict[str, Any]:
        return self.call("get_login_info")

    def get_status(self) -> dict[str, Any]:
        return self.call("get_status")

    def send_private_msg(self, user_id: str, text: str) -> int | None:
        """发送私聊文本消息，返回 message_id（若无则 None）。"""
        resp = self.call(
            "send_private_msg",
            {"user_id": int(user_id), "message": [{"type": "text", "data": {"text": text}}]},
        )
        data = resp.get("data") or {}
        mid = data.get("message_id")
        return int(mid) if mid is not None else None
