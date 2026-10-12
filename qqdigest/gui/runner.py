"""后端进程管理。

GUI 只负责编辑配置和显示，真正的采集/调度在独立进程里跑
（``python run.py run``）。这样：

- 界面卡顿或被关掉，都不影响消息采集与定时发送；
- 后端崩溃时界面能立刻发现并给出提示；
- 两边共用同一份 ``config.toml``，无需进程间通信。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

log = logging.getLogger("qqdigest.gui.runner")


class BackendRunner:
    """把 ``run.py`` 作为子进程启动 / 停止，并实时回传输出。"""

    def __init__(
        self,
        project_root: Path,
        *,
        on_output: Callable[[str], None],
        on_exit: Callable[[int], None],
    ) -> None:
        self.root = Path(project_root)
        self.on_output = on_output
        self.on_exit = on_exit
        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 状态
    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc and proc.poll() is None else None

    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def owned_by_gui(self) -> bool:
        return self._proc is not None

    # ------------------------------------------------------------------ 启动
    def start(self, args: Sequence[str] = ("run",)) -> tuple[bool, str]:
        if self.is_running():
            return False, "后端已在运行中"

        entry = self.root / "run.py"
        if not entry.is_file():
            return False, f"找不到入口文件：{entry}"

        cmd = [sys.executable, str(entry), *args]
        flags = 0
        if sys.platform == "win32":
            # 不弹控制台窗口
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        env = None
        try:
            import os

            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
        except Exception:
            env = None

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
                env=env,
            )
        except Exception as exc:
            return False, f"启动失败：{exc}"

        with self._lock:
            self._proc = proc
        self.on_output(f"$ {' '.join(cmd)}\n")
        self._reader = threading.Thread(target=self._pump, args=(proc,), daemon=True, name="backend-reader")
        self._reader.start()
        return True, f"后端已启动（PID {proc.pid}）"

    def _pump(self, proc: subprocess.Popen[str]) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self.on_output(line)
        except Exception as exc:  # pragma: no cover - 进程异常退出
            log.debug("读取后端输出出错：%s", exc)
        finally:
            code = proc.wait()
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            self.on_exit(code)

    # ------------------------------------------------------------------ 停止
    def stop(self, timeout: float = 8.0) -> tuple[bool, str]:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False, "后端未在运行"
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
        with self._lock:
            self._proc = None
        return True, "后端已停止"

    # -------------------------------------------------------------- 一次性命令
    def run_oneshot(
        self,
        args: Sequence[str],
        *,
        on_done: Callable[[int], None] | None = None,
    ) -> None:
        """在后台线程里跑一条一次性命令（check / selftest / digest-now…），输出回传到界面。"""
        threading.Thread(
            target=self._oneshot_worker, args=(list(args), on_done), daemon=True, name="oneshot"
        ).start()

    def _oneshot_worker(self, args: list[str], on_done: Callable[[int], None] | None) -> None:
        entry = self.root / "run.py"
        cmd = [sys.executable, str(entry), *args]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        self.on_output(f"\n$ {' '.join(cmd)}\n")
        code = -1
        try:
            import os

            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
                env=env,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                self.on_output(line)
            code = proc.wait()
        except Exception as exc:
            self.on_output(f"[错误] {exc}\n")
        finally:
            if on_done is not None:
                try:
                    on_done(code)
                except Exception:
                    log.exception("一次性命令回调出错")
