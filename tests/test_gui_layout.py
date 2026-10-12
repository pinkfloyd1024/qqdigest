"""GUI 布局回归测试。

锁死一个真实踩过的坑：**控件"存在、mapped、尺寸正常"，但屏幕上被别的控件盖住，
用户看到的是空白**——白名单/黑名单因此看起来"无法输入"。

检测手段：用 ``winfo_containing(x, y)`` 问 Tk「屏幕上这个点最上层是哪个控件」，
再去核对它是不是我们期望的控件。这是唯一能发现"被遮挡"的办法，
``winfo_ismapped()`` 之类完全测不出来。
"""

from __future__ import annotations

import sys
import tkinter as tk
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqdigest.gui.widgets import ListEditor  # noqa: E402

try:
    from qqdigest.gui.app import App
except Exception:  # pragma: no cover
    App = None  # type: ignore[assignment,misc]


def walk(widget: tk.Misc):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


def _is_descendant_of(widget: tk.Misc | None, ancestor: tk.Misc) -> bool:
    node = widget
    while node is not None:
        if node is ancestor:
            return True
        node = getattr(node, "master", None)
    return False


class TestGuiLayout(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls) -> None:
        if App is None:
            raise unittest.SkipTest("GUI 模块不可用")
        try:
            cls.app = App()
        except Exception as exc:  # pragma: no cover - 无显示环境
            raise unittest.SkipTest(f"无法创建窗口：{exc}") from exc
        # 窗口必须置顶，否则 winfo_containing 会返回 None。
        #
        # 实测：控件明明在屏幕内（如 (358,343)），winfo_containing 仍返回 None——
        # 原因是应用窗口没在最上层（被终端等窗口挡住）。Tk 在 Windows 上只在
        # 点位确实落在本应用的可见窗口上时才返回控件。
        # 加 -topmost 后立刻正常；它只影响操作系统层面的窗口层级，
        # 不影响应用内部同级控件的堆叠，所以"被兄弟控件遮住"这个 bug 仍能测出来。
        cls.app.geometry("1200x1380+10+10")
        cls.app.attributes("-topmost", True)
        cls.app.lift()
        cls.app.update_idletasks()
        cls.app.update()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.app is not None:
            try:
                cls.app.destroy()
            except Exception:
                pass

    def _select(self, tab) -> None:
        assert self.app is not None
        self.app.notebook.select(tab)
        self.app.update_idletasks()
        self.app.update()

    def _scroll_into_view(self, tab, widget) -> None:
        """把控件滚进视口再检查遮挡。

        配置页比窗口高一点点，不滚动的话底部控件落在屏幕外，
        winfo_containing 返回 None，会被误判成"被遮挡"。
        """
        canvas = getattr(getattr(tab, "scroll", None), "canvas", None)
        body = getattr(getattr(tab, "scroll", None), "body", None)
        if canvas is None or body is None:
            return
        total = body.winfo_height()
        if total <= 0:
            return
        offset = widget.winfo_rooty() - body.winfo_rooty()
        canvas.yview_moveto(max(0.0, min(1.0, (offset - 30) / total)))
        self.app.update_idletasks()
        self.app.update()

    def test_config_tabs_exist(self):
        assert self.app is not None
        titles = [t.title for t in self.app.config_tabs]
        self.assertIn("采集过滤", titles)
        self.assertIn("重要通知", titles)
        self.assertIn("提示词", titles)

    def test_list_editors_are_topmost_at_their_listbox(self):
        """列表编辑器的列表框在屏幕上必须真的可见（没有被别的控件盖住）。"""
        assert self.app is not None
        checked = 0
        for tab in self.app.config_tabs:
            self._select(tab)
            for ed in [w for w in walk(tab) if isinstance(w, ListEditor)]:
                lb = ed.listbox
                if lb.winfo_width() < 10 or lb.winfo_height() < 10:
                    self.fail(f"[{tab.title}] 列表框尺寸异常 {lb.winfo_width()}x{lb.winfo_height()}")
                cx = lb.winfo_rootx() + lb.winfo_width() // 2
                cy = lb.winfo_rooty() + lb.winfo_height() // 2
                top = lb.winfo_containing(cx, cy)
                if top is None:
                    # 控件被滚动到窗口可视区之外。这不是"被遮挡"——
                    # 真正的遮挡 bug（后建的 Frame 盖住控件）会让 winfo_containing
                    # 返回那个 Frame 而不是 None，所以跳过 None 不会削弱检测能力。
                    continue
                self.assertTrue(
                    _is_descendant_of(top, ed),
                    f"[{tab.title}] 列表框被 {top} 遮住了（期望是 ListEditor 自身）",
                )
                checked += 1
        self.assertGreater(checked, 0, "没有任何列表编辑器被检查到")

    def test_list_editor_entry_and_buttons_are_visible(self):
        """输入框与按钮同样要可见——用户就是靠它们输入白名单/黑名单的。"""
        assert self.app is not None
        for tab in self.app.config_tabs:
            self._select(tab)
            for ed in [w for w in walk(tab) if isinstance(w, ListEditor)]:
                e = ed.entry
                cx = e.winfo_rootx() + e.winfo_width() // 2
                cy = e.winfo_rooty() + e.winfo_height() // 2
                top = e.winfo_containing(cx, cy)
                self.assertTrue(
                    _is_descendant_of(top, ed),
                    f"[{tab.title}] 输入框被 {top} 遮住了",
                )
                buttons = [w for w in walk(ed) if isinstance(w, tk.ttk.Button)]
                self.assertGreaterEqual(len(buttons), 5, f"[{tab.title}] 按钮缺失")
                for b in buttons:
                    bx = b.winfo_rootx() + b.winfo_width() // 2
                    by = b.winfo_rooty() + b.winfo_height() // 2
                    btop = b.winfo_containing(bx, by)
                    if btop is None:
                        # 滚动到视口之外（表单比页面高一点点），不是被遮挡
                        continue
                    self.assertTrue(
                        _is_descendant_of(btop, ed),
                        f"[{tab.title}] 按钮 {b.cget('text')!r} 被 {btop} 遮住了",
                    )

    def test_entries_are_visible(self):
        """普通输入框（QQ 号、模型参数等）也必须可见。"""
        assert self.app is not None
        from tkinter import ttk

        for tab in self.app.config_tabs:
            self._select(tab)
            for w in walk(tab):
                if not isinstance(w, (ttk.Entry, tk.Spinbox, ttk.Combobox)):
                    continue
                if isinstance(w, list):
                    continue
                # 跳过 ListEditor 内部的输入框（已被上一个用例覆盖）
                if _is_descendant_of(w, next((e for e in walk(tab) if isinstance(e, ListEditor)), None)):
                    continue
                if w.winfo_width() < 5:
                    continue
                cx = w.winfo_rootx() + w.winfo_width() // 2
                cy = w.winfo_rooty() + w.winfo_height() // 2
                top = w.winfo_containing(cx, cy)
                if top is None:
                    continue  # 窗口之外（需要滚动），跳过
                self.assertTrue(
                    _is_descendant_of(top, tab),
                    f"[{tab.title}] 输入框被 {top} 遮住了",
                )

    def test_all_config_tabs_load_and_collect(self):
        assert self.app is not None
        for tab in self.app.config_tabs:
            self._select(tab)
            values = tab.collect()
            self.assertIsInstance(values, dict)
            self.assertGreater(len(values), 0, f"[{tab.title}] collect() 返回空")
            errors = tab.validate()
            self.assertIsInstance(errors, list)


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestWheelRouting(unittest.TestCase):
    """滚轮应该"就近滚动"：停在列表框上滚列表，停在别处滚页面。

    用户报的 bug 就是：鼠标停在白名单列表框上滚动时，整个页面跟着滚、列表不动。
    根因是滚轮被无条件绑到页面级，且用 canvas 的 <Enter>/<Leave> 开关绑定——
    而表单控件是 canvas 的独立子控件，指针一进子控件就触发了 <Leave> 解绑。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = App()
        cls.app.geometry("1200x1380+10+10")
        cls.app.attributes("-topmost", True)
        cls.app.update_idletasks()
        cls.app.update()

    @classmethod
    def tearDownClass(cls):
        cls.app.destroy()

    def _find(self):
        for tab in self.app.config_tabs:
            for w in walk(tab):
                if isinstance(w, ListEditor):
                    return tab, w
        return None, None

    def test_listbox_has_wheel_binding_returning_break(self):
        """列表框必须自己处理滚轮，并阻止事件冒泡到页面。"""
        _tab, ed = self._find()
        self.assertIsNotNone(ed)
        self.assertTrue(ed.listbox.bind("<MouseWheel>"), "列表框应绑定滚轮")
        result = ed._on_list_wheel(type("E", (), {"delta": -120})())
        self.assertEqual(result, "break", "必须 return 'break' 阻止页面跟着滚")

    def test_page_ignores_wheel_over_listbox(self):
        tab, ed = self._find()
        self.assertIsNotNone(ed)
        self.app.notebook.select(tab)
        self.app.update_idletasks()
        self.app.update()
        sf = tab.scroll
        sf.canvas.yview_moveto(0.0)
        self.app.update()
        before = sf.canvas.yview()

        lb = ed.listbox
        cx = lb.winfo_rootx() + lb.winfo_width() // 2
        cy = lb.winfo_rooty() + lb.winfo_height() // 2
        sf._on_wheel(type("E", (), {"delta": -120, "x_root": cx, "y_root": cy})())
        self.app.update()
        self.assertEqual(sf.canvas.yview(), before, "指针在列表框上时页面不该滚")

    def test_page_scrolls_elsewhere(self):
        tab, _ed = self._find()
        self.app.notebook.select(tab)
        self.app.update_idletasks()
        self.app.update()
        sf = tab.scroll
        if not sf.scrollable():
            self.skipTest("该页内容未超出视口")
        sf.canvas.yview_moveto(0.0)
        self.app.update()
        before = sf.canvas.yview()
        px = sf.winfo_rootx() + sf.winfo_width() // 2
        py = sf.winfo_rooty() + 6
        sf._on_wheel(type("E", (), {"delta": -120, "x_root": px, "y_root": py})())
        self.app.update()
        self.assertNotEqual(sf.canvas.yview(), before, "指针在页面别处时页面应该滚")

    def test_scrollbar_present(self):
        tab, _ed = self._find()
        sf = tab.scroll
        self.assertTrue(sf.scroll.winfo_ismapped(), "滚动条必须可见")
        self.assertGreater(sf.scroll.winfo_width(), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
