"""本地大模型调用层（LM Studio 的 OpenAI 兼容接口）。

只用标准库 ``urllib``，不依赖 requests。包含 token 估算，用于判断是否需要分块。
"""

from __future__ import annotations

import json
import logging
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import LlmCfg

log = logging.getLogger("qqdigest.llm")


# --------------------------------------------------------------------------
# token 估算
# --------------------------------------------------------------------------


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF  # CJK 统一表意
        or 0x3400 <= code <= 0x4DBF  # 扩展 A
        or 0x3000 <= code <= 0x303F  # CJK 标点
        or 0xFF00 <= code <= 0xFFEF  # 全角
        or 0x3040 <= code <= 0x30FF  # 假名
    )


def estimate_tokens(text: str) -> int:
    """估算 token 数。

    在本机实测校准：约 900 字的中文混合文本对应 750 prompt tokens，
    即约 0.83 token/字符。此处对中文按 1.0、非中文按 1/3.5 计算并留出余量，
    属于**偏保守**的估计（宁可早分块，也不要撑爆上下文）。
    """
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        elif unicodedata.category(ch).startswith("C"):
            continue  # 控制字符不计数
        else:
            other += 1
    return int(cjk * 1.05 + other / 3.2) + 8


# --------------------------------------------------------------------------
# 调用
# --------------------------------------------------------------------------


@dataclass
class LlmResult:
    content: str
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    elapsed_sec: float
    model: str

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LlmError(RuntimeError):
    """本地模型调用失败。"""


class LlmClient:
    """LM Studio / OpenAI 兼容接口的极简客户端。"""

    def __init__(self, cfg: LlmCfg) -> None:
        self.cfg = cfg
        self._url = f"{cfg.base_url}/chat/completions"

    # ------------------------------------------------------------------ 底层
    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_sec) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise LlmError(f"HTTP {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise LlmError(
                f"无法连接本地模型服务 {self.cfg.base_url}：{exc.reason}。"
                "请确认 LM Studio 已启动并开启了本地服务器"
            ) from exc
        except Exception as exc:
            raise LlmError(f"调用本地模型异常：{exc}") from exc

    # ------------------------------------------------------------------ 公开
    def chat(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LlmResult:
        """单轮对话，带重试。"""
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

        last_err: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            started = time.time()
            try:
                data = self._post(payload)
            except LlmError as exc:
                last_err = exc
                log.warning("模型调用失败（第 %d 次）：%s", attempt + 1, exc)
                if attempt < self.cfg.max_retries:
                    time.sleep(2.0)
                continue

            elapsed = time.time() - started
            choices = data.get("choices") or []
            if not choices:
                last_err = LlmError(f"响应中没有 choices：{str(data)[:300]}")
                continue
            message = choices[0].get("message") or {}
            content = (message.get("content") or "").strip()
            usage = data.get("usage") or {}
            details = usage.get("completion_tokens_details") or {}
            finish = choices[0].get("finish_reason")

            result = LlmResult(
                content=content,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                reasoning_tokens=int(details.get("reasoning_tokens") or 0),
                elapsed_sec=elapsed,
                model=str(data.get("model") or self.cfg.model),
            )
            log.info(
                "模型返回：%.1fs，prompt=%d，completion=%d（其中 reasoning=%d），finish=%s",
                result.elapsed_sec,
                result.prompt_tokens,
                result.completion_tokens,
                result.reasoning_tokens,
                finish,
            )

            if not content:
                # 实测成因：思维链把 max_tokens 吃光。给出可操作的提示。
                last_err = LlmError(
                    f"模型返回空正文（completion={result.completion_tokens}，"
                    f"reasoning={result.reasoning_tokens}，finish={finish}）。"
                    "通常是 max_tokens 被思维链占满：请提高 llm.max_tokens（建议 3000+），"
                    "或保持 llm.no_think = true"
                )
                log.warning("%s", last_err)
                continue
            if finish == "length":
                log.warning("输出被 max_tokens 截断，简报可能不完整")
            return result

        raise LlmError(str(last_err) if last_err else "未知的模型调用失败")

    # ------------------------------------------------------------------ 体检
    def list_models(self) -> list[str]:
        url = f"{self.cfg.base_url}/models"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise LlmError(f"读取模型列表失败：{exc}") from exc
        return [str(m.get("id")) for m in (data.get("data") or [])]
