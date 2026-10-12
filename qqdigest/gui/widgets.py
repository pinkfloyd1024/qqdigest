"""GUI 通用控件。

只用标准库 ``tkinter`` / ``ttk``，不引入任何第三方依赖。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Iterable
from tkinter import messagebox, ttk

PAD = 8


#: 自带滚动能力的控件：滚轮落在它们身上时，应该滚它们自己，而不是外层页面。
#: （实测踩坑：把滚轮无条件绑到页面级，会导致鼠标停在白名单列表框上滚动时
#:   整个页面跟着滚，而列表纹丝不动——正是用户报的那个问题。）
_SELF_SCROLLING = (tk.Listbox, tk.Text, ttk.Treeview)


class ScrollableFrame(ttk.Frame):
    """可纵向滚动的容器。把内容放进 ``.body``。

    滚轮策略：
    - 指针落在**自带滚动的控件**（列表框、文本框、树）上 → 交给它们自己滚；
    - 指针落在本页其它任何位置（包括各种输入框、按钮、空白处）→ 滚本页；
    - 指针不在本页范围内 → 完全不动。

    早期实现用 canvas 的 ``<Enter>``/``<Leave>`` 来开关绑定，但表单控件是 canvas
    里的**独立子控件**，指针一移进子控件就会触发 ``<Leave>`` 解绑，滚轮随之失效。
    现在改成常驻绑定 + 按指针位置判断，行为才稳定。
    """

    def __init__(self, master: tk.Misc, **kw) -> None:
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)

        self.canvas.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        self.scroll.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.body = ttk.Frame(self.canvas, padding=PAD)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")

        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        # 常驻绑定：具体滚谁由指针位置决定（见 _on_wheel）
        self.bind_all("<MouseWheel>", self._on_wheel, add="+")
        self.bind("<Destroy>", self._on_destroy, add="+")

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self:
            try:
                self.unbind_all("<MouseWheel>")
            except Exception:
                pass

    def _on_body_configure(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def scrollable(self) -> bool:
        """内容是否真的超出了视口（用于决定要不要提示用户可滚动）。"""
        region = self.canvas.bbox("all")
        if not region:
            return False
        return (region[3] - region[1]) > self.canvas.winfo_height()

    def _on_wheel(self, event: tk.Event) -> None:
        # 优先用事件自带的坐标（就是滚轮发生的位置）；拿不到时回落到真实指针位置
        x = getattr(event, "x_root", None)
        y = getattr(event, "y_root", None)
        if x is None or y is None:
            x, y = self.winfo_pointerxy()
        if not (
            self.winfo_rootx() <= x < self.winfo_rootx() + self.winfo_width()
            and self.winfo_rooty() <= y < self.winfo_rooty() + self.winfo_height()
        ):
            return  # 指针不在本页，别抢别页的滚轮

        target = self.winfo_containing(x, y)
        while target is not None:
            if isinstance(target, _SELF_SCROLLING) and target is not self.canvas:
                return  # 交给列表框/文本框自己滚
            if target is self:
                break
            target = getattr(target, "master", None)

        if not self.scrollable():
            return
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class Hint(ttk.Label):
    def __init__(self, master: tk.Misc, text: str, wraplength: int = 720) -> None:
        super().__init__(master, text=text, style="Hint.TLabel", wraplength=wraplength, justify="left")


class ListEditor(ttk.Frame):
    """字符串列表编辑器：用于群白名单、黑名单、关键词等。

    ``get()`` 返回当前列表，``set(items)`` 覆盖。
    """

    def __init__(
        self,
        master: tk.Misc,
        *,
        height: int = 6,
        width: int = 34,
        placeholder: str = "输入后按回车或点「添加」",
        validator: Callable[[str], str | None] | None = None,
    ) -> None:
        super().__init__(master)
        self._validator = validator
        self._placeholder = placeholder

        self.listbox = tk.Listbox(
            self, height=height, width=width, selectmode=tk.EXTENDED, exportselection=False
        )
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)

        self.entry = ttk.Entry(self)
        self.entry.bind("<Return>", lambda _e: self.add_from_entry())

        btns = ttk.Frame(self)
        ttk.Button(btns, text="添加", width=7, command=self.add_from_entry).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(btns, text="删除选中", width=9, command=self.remove_selected).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(btns, text="批量粘贴", width=9, command=self.bulk_paste).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(btns, text="上移", width=5, command=lambda: self.move(-1)).grid(row=0, column=3, padx=(0, 4))
        ttk.Button(btns, text="下移", width=5, command=lambda: self.move(1)).grid(row=0, column=4)

        self.listbox.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 4))
        btns.grid(row=2, column=0, columnspan=2, sticky="w")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        # 鼠标停在列表框上时，滚轮应该滚**列表**而不是整个页面。
        # Tk 的 Listbox 默认不响应滚轮，所以这里补上；用 return "break"
        # 阻止事件继续冒泡到外层的 ScrollableFrame。
        self.listbox.bind("<MouseWheel>", self._on_list_wheel, add="+")
        self.listbox.bind("<Button-4>", self._on_list_wheel, add="+")  # X11 上滚
        self.listbox.bind("<Button-5>", self._on_list_wheel, add="+")  # X11 下滚

    def _on_list_wheel(self, event: tk.Event) -> str:
        delta = getattr(event, "delta", 0)
        if delta:
            self.listbox.yview_scroll(int(-delta / 120), "units")
        elif getattr(event, "num", None) == 4:
            self.listbox.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            self.listbox.yview_scroll(1, "units")
        return "break"

    # ---------------------------------------------------------------- 数据
    def get(self) -> list[str]:
        return [self.listbox.get(i) for i in range(self.listbox.size())]

    def set(self, items: Iterable[str]) -> None:
        self.listbox.delete(0, tk.END)
        seen: set[str] = set()
        for item in items:
            text = str(item).strip()
            if text and text not in seen:
                seen.add(text)
                self.listbox.insert(tk.END, text)

    # ---------------------------------------------------------------- 操作
    def add(self, value: str) -> bool:
        text = value.strip()
        if not text:
            return False
        if self._validator:
            err = self._validator(text)
            if err:
                messagebox.showwarning("取值不合法", err, parent=self)
                return False
        if text in self.get():
            return False
        self.listbox.insert(tk.END, text)
        return True

    def add_from_entry(self) -> None:
        if self.add(self.entry.get()):
            self.entry.delete(0, tk.END)

    def remove_selected(self) -> None:
        for idx in reversed(self.listbox.curselection()):
            self.listbox.delete(idx)

    def move(self, delta: int) -> None:
        selection = list(self.listbox.curselection())
        if not selection:
            return
        target = selection[0] + delta
        if not (0 <= target < self.listbox.size()):
            return
        value = self.listbox.get(selection[0])
        self.listbox.delete(selection[0])
        self.listbox.insert(target, value)
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(target)

    def bulk_paste(self) -> None:
        """弹窗批量导入：每行一条，自动去重与去空。"""
        top = tk.Toplevel(self)
        top.title("批量粘贴")
        top.transient(self.winfo_toplevel())
        top.grab_set()
        top.geometry("460x320")

        ttk.Label(
            top, text="每行一条，支持逗号/顿号分隔。会自动去重、去空行。", padding=(PAD, PAD, PAD, 0)
        ).pack(anchor="w")
        text = tk.Text(top, wrap="word", height=12)
        text.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        text.focus_set()

        def confirm() -> None:
            raw = text.get("1.0", tk.END)
            parts: list[str] = []
            for line in raw.splitlines():
                for piece in line.replace("，", ",").replace("、", ",").split(","):
                    piece = piece.strip()
                    if piece:
                        parts.append(piece)
            added = sum(1 for p in parts if self.add(p))
            top.destroy()
            messagebox.showinfo("批量粘贴", f"新增 {added} 条（重复或非法已忽略）", parent=self)

        bar = ttk.Frame(top, padding=(PAD, 0, PAD, PAD))
        bar.pack(fill="x")
        ttk.Button(bar, text="确定导入", command=confirm).pack(side="right")
        ttk.Button(bar, text="取消", command=top.destroy).pack(side="right", padx=(0, 6))


class FormGrid(ttk.Frame):
    """两列表单：左列标签，右列控件。``add_*`` 系列方法返回创建的控件。"""

    def __init__(self, master: tk.Misc, label_width: int = 22) -> None:
        super().__init__(master)
        self._row = 0
        self._label_width = label_width
        self.columnconfigure(1, weight=1)
        # 第 2 列放说明文字。控件与说明必须是 FormGrid 的**平级子控件**，
        # 不能用一个中间 Frame 承载（见 add_widget 的注释）。
        self.columnconfigure(2, weight=1)

    def _label(self, text: str, hint: str = "") -> None:
        lbl = ttk.Label(self, text=text, width=self._label_width, anchor="w")
        lbl.grid(row=self._row, column=0, sticky="nw", padx=(0, 10), pady=3)
        if hint:
            lbl.configure(text=text)

    def add_entry(self, text: str, *, width: int = 40, hint: str = "") -> ttk.Entry:
        self._label(text)
        frame = ttk.Frame(self)
        widget = ttk.Entry(frame, width=width)
        widget.grid(row=0, column=0, sticky="w")
        if hint:
            ttk.Label(frame, text=hint, style="Hint.TLabel").grid(row=0, column=1, padx=(8, 0), sticky="w")
        frame.grid(row=self._row, column=1, sticky="w", pady=3)
        self._row += 1
        return widget

    def add_spin(
        self, text: str, *, from_: float, to: float, width: int = 10, hint: str = ""
    ) -> tk.Spinbox:
        self._label(text)
        frame = ttk.Frame(self)
        widget = tk.Spinbox(frame, from_=from_, to=to, width=width)
        widget.grid(row=0, column=0, sticky="w")
        if hint:
            ttk.Label(frame, text=hint, style="Hint.TLabel").grid(row=0, column=1, padx=(8, 0), sticky="w")
        frame.grid(row=self._row, column=1, sticky="w", pady=3)
        self._row += 1
        return widget

    def add_check(self, text: str, *, hint: str = "") -> tk.BooleanVar:
        self._label(text)
        var = tk.BooleanVar()
        frame = ttk.Frame(self)
        ttk.Checkbutton(frame, variable=var, text="启用").grid(row=0, column=0, sticky="w")
        if hint:
            ttk.Label(frame, text=hint, style="Hint.TLabel").grid(row=0, column=1, padx=(8, 0), sticky="w")
        frame.grid(row=self._row, column=1, sticky="w", pady=3)
        self._row += 1
        return var

    def add_combo(self, text: str, values: list[str], *, width: int = 20, hint: str = "") -> ttk.Combobox:
        self._label(text)
        frame = ttk.Frame(self)
        widget = ttk.Combobox(frame, values=values, width=width, state="readonly")
        widget.grid(row=0, column=0, sticky="w")
        if hint:
            ttk.Label(frame, text=hint, style="Hint.TLabel").grid(row=0, column=1, padx=(8, 0), sticky="w")
        frame.grid(row=self._row, column=1, sticky="w", pady=3)
        self._row += 1
        return widget

    def add_widget(self, text: str, widget: tk.Widget, *, hint: str = "") -> tk.Widget:
        """把已经创建好的控件放进表单。

        ⚠️ 这里**绝对不能**用一个中间 Frame 再 ``widget.grid(in_=frame)``。
        原因：``in_`` 只改变几何管理器，控件的**父容器仍然是 FormGrid**，
        而 Tk 按创建顺序堆叠同级窗口——后创建的 Frame 会盖在这个控件上面。
        结果就是控件"存在、mapped、尺寸正常"，但屏幕上什么也看不见，
        反而显示成一块空白（ListEditor 就踩过这个坑：白名单/黑名单看起来无法输入）。
        所以这里让控件与说明文字直接平铺在 FormGrid 的第 1、2 列。
        """
        self._label(text)
        widget.grid(row=self._row, column=1, sticky="nw", pady=3)
        if hint:
            ttk.Label(self, text=hint, style="Hint.TLabel", wraplength=380, justify="left").grid(
                row=self._row, column=2, padx=(10, 0), sticky="nw", pady=3
            )
        self._row += 1
        return widget

    def add_text_area(
        self, text: str, *, height: int = 8, hint: str = ""
    ) -> tk.Text:
        """多行文本框，带滚动条，整体占满一行的宽度。"""
        self._label(text)
        frame = ttk.Frame(self)
        widget = tk.Text(frame, height=height, wrap="word", undo=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
        widget.configure(yscrollcommand=scroll.set)
        widget.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        if hint:
            ttk.Label(frame, text=hint, style="Hint.TLabel").grid(
                row=1, column=0, columnspan=3, sticky="w", pady=(4, 0)
            )
        frame.grid(row=self._row, column=1, sticky="nsew", pady=3)
        self.rowconfigure(self._row, weight=1)
        self._row += 1
        return widget

    def add_separator(self) -> None:
        ttk.Separator(self, orient="horizontal").grid(
            row=self._row, column=0, columnspan=3, sticky="ew", pady=(10, 8)
        )
        self._row += 1

    def add_note(self, text: str) -> None:
        ttk.Label(self, text=text, style="Hint.TLabel", wraplength=760, justify="left").grid(
            row=self._row, column=0, columnspan=3, sticky="w", pady=(2, 6)
        )
        self._row += 1

    def add_title(self, text: str) -> None:
        ttk.Label(self, text=text, style="Section.TLabel").grid(
            row=self._row, column=0, columnspan=3, sticky="w", pady=(6, 6)
        )
        self._row += 1


def setup_styles(root: tk.Misc) -> None:
    """统一字体与样式，保证中文显示正常。"""
    import tkinter.font as tkfont

    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=9)
        except Exception:
            pass
    try:
        tkfont.nametofont("TkFixedFont").configure(family="Consolas", size=9)
    except Exception:
        pass

    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except Exception:
        pass
    style.configure("Section.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
    style.configure("Hint.TLabel", foreground="#666666")
    style.configure("Ok.TLabel", foreground="#1a7f37")
    style.configure("Warn.TLabel", foreground="#b7791f")
    style.configure("Err.TLabel", foreground="#c0392b")
    style.configure("Title.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
    style.configure("Mono.TLabel", font=("Consolas", 9))
