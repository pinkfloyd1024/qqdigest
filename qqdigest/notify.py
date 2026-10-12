"""提醒与通知。

- :func:`toast` —— Windows 10/11 桌面通知，通过 PowerShell 5.1 调用 WinRT API，
  **不需要安装任何第三方模块**（不依赖 BurntToast）。
- :func:`notify` —— 按配置组合多种提醒渠道，任一渠道成功即算成功，
  失败只记录日志，绝不影响主流程。

所有渠道都是「尽力而为」：出问题不会中断消息采集或简报发送。
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .util import clipboard_set

log = logging.getLogger("qqdigest.notify")

#: 已注册的 AppID，用于让桌面通知正常显示（PowerShell 自身的 AppID，稳定可靠）
_TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$nodes = $template.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($template.CreateTextNode($env:QQDIGEST_TOAST_TITLE)) | Out-Null
$nodes.Item(1).AppendChild($template.CreateTextNode($env:QQDIGEST_TOAST_BODY)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:QQDIGEST_TOAST_APPID)
$notifier.Show($toast)
"""


def toast(title: str, body: str, *, timeout: int = 20) -> bool:
    """弹出 Windows 桌面通知。成功返回 True，任何失败返回 False。"""
    if sys.platform != "win32":
        return False
    shell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not shell:
        log.debug("未找到 powershell.exe，跳过桌面通知")
        return False

    try:
        encoded = base64.b64encode(_TOAST_SCRIPT.encode("utf-16-le")).decode("ascii")
    except Exception:
        return False

    env = dict(os.environ)
    env["QQDIGEST_TOAST_TITLE"] = (title or "qqdigest")[:120]
    env["QQDIGEST_TOAST_BODY"] = (body or "")[:400]
    env["QQDIGEST_TOAST_APPID"] = _TOAST_APP_ID

    try:
        proc = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except Exception as exc:
        log.debug("桌面通知调用失败：%s", exc)
        return False

    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        log.debug("桌面通知返回非零（%s）：%s", proc.returncode, err[:200])
        return False
    return True


def append_notice_log(path: Path, title: str, body: str) -> None:
    """把提醒追加写入文本文件，作为最可靠的兜底留痕。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {title}\n{body}\n{'-' * 60}\n")
    except Exception as exc:
        log.debug("写提醒日志失败：%s", exc)


@dataclass
class NotifyResult:
    toast_ok: bool = False
    clipboard_ok: bool = False
    file_ok: bool = False

    @property
    def any_ok(self) -> bool:
        return self.toast_ok or self.clipboard_ok

    def describe(self) -> str:
        done = []
        if self.toast_ok:
            done.append("桌面通知")
        if self.clipboard_ok:
            done.append("剪贴板")
        if self.file_ok:
            done.append("提醒日志")
        return "、".join(done) if done else "无（全部渠道失败）"


def notify(
    title: str,
    body: str,
    *,
    use_toast: bool = True,
    use_clipboard: bool = True,
    log_file: Path | None = None,
) -> NotifyResult:
    """按渠道组合发送提醒。"""
    result = NotifyResult()
    if log_file is not None:
        append_notice_log(log_file, title, body)
        result.file_ok = True
    if use_toast:
        result.toast_ok = toast(title, body)
    if use_clipboard:
        result.clipboard_ok = clipboard_set(f"{title}\n\n{body}")
    return result
