# -*- coding: utf-8 -*-
"""
太一卡厄斯天云_溯流之手  v1.0
--------------------------------
Agent17 专用存档修改器。图形界面，免安装。

安全机制：
  · 写入前自动备份到 game/saves/_ag17_backups/时间戳/
  · 写入前做变量集重合度校验（防止改错游戏）
  · 写入后自动重签名 + 验签 + 读回核对
"""
import os
import sys
import time
import threading
import traceback
import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

if getattr(sys, "frozen", False):
    HERE = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    APP_DIR = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = HERE

for p in (HERE, os.path.join(HERE, "lib")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ag17core as C

APP_TITLE = "太一卡厄斯天云_溯流之手 v1.0"
FONT = ("Microsoft YaHei UI", 9)


def _excepthook(t, v, tb):
    txt = "".join(traceback.format_exception(t, v, tb))
    try:
        with open(os.path.join(APP_DIR, "error.log"), "a", encoding="utf-8") as f:
            f.write("\n[%s]\n%s\n" % (datetime.datetime.now(), txt))
    except Exception:
        pass
    try:
        messagebox.showerror("出错了", txt[-1500:])
    except Exception:
        pass


sys.excepthook = _excepthook


# ================================================================ 主窗口

class App(tk.Tk):
    def __init__(self):
        tk.Tk.__init__(self)
        self.title(APP_TITLE)
        self.geometry("1240x860")
        self.minsize(1050, 700)

        self.gamedir = None
        self.saves = []            # [{slot,path,mtime,day,time,gold,loc,key,...}]
        self.store_cache = {}      # path -> store
        self.keys = []
        self.pending = []          # [(kind, ident, newval, label)]
        self._row_meta = {}        # tree -> {iid: extra}

        self._build_style()
        self._build_ui()

        self.after(120, self.boot)

    # ------------------------------------------------------------ 样式
    def _build_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("vista")
        except Exception:
            pass
        st.configure(".", font=FONT)
        st.configure("Treeview", font=FONT, rowheight=23)
        st.configure("Treeview.Heading", font=FONT)
        st.configure("TNotebook.Tab", font=FONT, padding=(12, 6))
        st.configure("Title.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
        st.configure("Hint.TLabel", font=("Microsoft YaHei UI", 8), foreground="#888")
        st.configure("Go.TButton", font=("Microsoft YaHei UI", 10, "bold"))

    # ------------------------------------------------------------ 布局
    def _build_ui(self):
        top = ttk.Frame(self, padding=(10, 8, 10, 4))
        top.pack(fill="x")

        ttk.Label(top, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        self.lbl_game = ttk.Label(top, text="· Agent17 专用 ·  正在查找游戏…", style="Hint.TLabel")
        self.lbl_game.pack(side="left", padx=(14, 0))
        ttk.Button(top, text="重新扫描", command=self.boot).pack(side="right")
        ttk.Button(top, text="手动指定游戏", command=self.pick_game).pack(side="right", padx=(0, 6))

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 4))

        # ---------------- 左：存档列表 + 待修改清单
        left = ttk.Frame(body)
        body.add(left, weight=1)

        box = ttk.LabelFrame(left, text=" 存档列表 ", padding=6)
        box.pack(fill="both", expand=True)

        cols = ("slot", "time", "day", "gold", "loc")
        self.tv_saves = ttk.Treeview(box, columns=cols, show="headings", height=8,
                                     selectmode="extended")
        for c, t, w, a in (
            ("slot", "槽位", 90, "w"), ("time", "保存时间", 120, "w"),
            ("day", "天数", 52, "center"), ("gold", "金钱", 78, "e"),
            ("loc", "位置", 130, "w"),
        ):
            self.tv_saves.heading(c, text=t)
            self.tv_saves.column(c, width=w, anchor=a, stretch=(c in ("time", "loc")))
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tv_saves.yview)
        self.tv_saves.configure(yscrollcommand=sb.set)
        self.tv_saves.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tv_saves.bind("<<TreeviewSelect>>", lambda e: self.refresh_tabs())

        opt = ttk.Frame(left)
        opt.pack(fill="x", pady=(6, 0))
        self.var_same = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="同时改「同一时刻」的所有存档",
                        variable=self.var_same).pack(side="left")
        ttk.Button(opt, text="全选", width=6, command=lambda: self.tv_saves.selection_set(self.tv_saves.get_children())).pack(side="right")
        ttk.Button(opt, text="反选", width=6, command=self.invert_sel).pack(side="right", padx=(0, 4))
        self.var_filter = tk.StringVar(value="全部")
        cb = ttk.Combobox(opt, textvariable=self.var_filter, width=7, state="readonly",
                          values=["全部", "仅手动", "仅自动"])
        cb.pack(side="right", padx=(0, 8))
        cb.bind("<<ComboboxSelected>>", lambda e: self.render_saves())

        pbox = ttk.LabelFrame(left, text=" 待修改清单 ", padding=6)
        pbox.pack(fill="both", expand=True, pady=(8, 0))
        pcols = ("target", "var", "old", "new")
        self.tv_pend = ttk.Treeview(pbox, columns=pcols, show="headings", height=6)
        for c, t, w, a in (("target", "目标", 90, "w"), ("var", "变量", 190, "w"),
                           ("old", "当前值", 90, "e"), ("new", "新值", 90, "e")):
            self.tv_pend.heading(c, text=t)
            self.tv_pend.column(c, width=w, anchor=a, stretch=(c == "var"))
        psb = ttk.Scrollbar(pbox, orient="vertical", command=self.tv_pend.yview)
        self.tv_pend.configure(yscrollcommand=psb.set)
        self.tv_pend.pack(side="left", fill="both", expand=True)
        psb.pack(side="right", fill="y")

        prow = ttk.Frame(pbox)
        prow.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(prow, text="移除选中", command=self.remove_pending).pack(side="left")
        ttk.Button(prow, text="清空清单", command=self.clear_pending).pack(side="left", padx=4)
        self.lbl_pend = ttk.Label(prow, text="0 项", style="Hint.TLabel")
        self.lbl_pend.pack(side="right")

        # ---------------- 右：标签页 + 日志
        right = ttk.Frame(body)
        body.add(right, weight=3)

        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)

        self._tab_values()
        self._tab_containers()
        self._tab_allvars()

        lbox = ttk.LabelFrame(right, text=" 日志 ", padding=4)
        lbox.pack(fill="both", expand=False, pady=(8, 0))
        self.txt_log = tk.Text(lbox, height=9, font=("Consolas", 9), wrap="word",
                               relief="flat", background="#1e1e1e", foreground="#d4d4d4",
                               insertbackground="#d4d4d4")
        lsb = ttk.Scrollbar(lbox, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=lsb.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        self.txt_log.tag_configure("ok", foreground="#4ec9b0")
        self.txt_log.tag_configure("err", foreground="#f48771")
        self.txt_log.tag_configure("warn", foreground="#dcdcaa")
        self.txt_log.tag_configure("info", foreground="#9cdcfe")

        # ---------------- 底部按钮
        bot = ttk.Frame(self, padding=(10, 4, 10, 10))
        bot.pack(fill="x")
        ttk.Button(bot, text="应用修改", style="Go.TButton",
                   command=self.do_apply).pack(side="left")
        ttk.Button(bot, text="还原备份", command=self.do_restore).pack(side="left", padx=6)
        self.lbl_status = ttk.Label(bot, text="就绪", style="Hint.TLabel")
        self.lbl_status.pack(side="right")

    def invert_sel(self):
        cur = set(self.tv_saves.selection())
        all_ = self.tv_saves.get_children()
        self.tv_saves.selection_set([i for i in all_ if i not in cur])

    # ============================================================ 标签页

    def _mk_tree(self, parent, cols):
        fr = ttk.Frame(parent, padding=8)
        tv = ttk.Treeview(fr, columns=[c[0] for c in cols], show="headings")
        for key, title, w, anchor in cols:
            tv.heading(key, text=title)
            tv.column(key, width=w, anchor=anchor, stretch=(key in ("name", "desc", "var")))
        sb = ttk.Scrollbar(fr, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return fr, tv

    def _tab_values(self):
        fr = ttk.Frame(self.nb)
        self.nb.add(fr, text=" 金钱与数值 ")

        top = ttk.Frame(fr, padding=(8, 8, 8, 0))
        top.pack(fill="x")
        ttk.Label(top, text="勾选一个变量，填新值，再点「加入待修改」。").pack(side="left")

        box, tv = self._mk_tree(fr, [("cat", "分类", 60, "w"), ("name", "名称", 150, "w"),
                                     ("var", "变量名", 200, "w"), ("cur", "当前值", 90, "e"),
                                     ("desc", "说明", 260, "w")])
        box.pack(fill="both", expand=True)
        self.tv_vals = tv
        tv.bind("<Double-1>", lambda e: self._focus_new())

        bar = ttk.Frame(fr, padding=(8, 6, 8, 8))
        bar.pack(fill="x")
        ttk.Label(bar, text="新值").pack(side="left")
        self.var_newval = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.var_newval, width=14)
        ent.pack(side="left", padx=(6, 10))
        self.ent_newval = ent
        self.mode = tk.StringVar(value="set")
        ttk.Radiobutton(bar, text="设为", variable=self.mode, value="set").pack(side="left")
        ttk.Radiobutton(bar, text="增加", variable=self.mode, value="add").pack(side="left", padx=(0, 10))
        ttk.Button(bar, text="加入待修改", command=self.add_from_values).pack(side="left")

    def _tab_containers(self):
        self.cont_tabs = {}
        for spec in C.CONTAINERS:
            fr = ttk.Frame(self.nb)
            self.nb.add(fr, text=" %s " % spec["name"])

            cols = [(spec["id_field"], "标识", 170, "w")]
            for f, label, _t in spec["fields"]:
                cols.append((f, label, 90, "e"))
            box, tv = self._mk_tree(fr, cols)
            box.pack(fill="both", expand=True)
            self.cont_tabs[spec["name"]] = (spec, tv)

            bar = ttk.Frame(fr, padding=(8, 6, 8, 8))
            bar.pack(fill="x")
            ttk.Label(bar, text="改字段").pack(side="left")
            vf = tk.StringVar(value=spec["fields"][0][0])
            cb = ttk.Combobox(bar, textvariable=vf, width=12, state="readonly",
                              values=[f for f, _l, _t in spec["fields"]])
            cb.pack(side="left", padx=(6, 10))
            ttk.Label(bar, text="新值").pack(side="left")
            vv = tk.StringVar()
            ttk.Entry(bar, textvariable=vv, width=12).pack(side="left", padx=(6, 10))
            ttk.Button(bar, text="加入待修改",
                       command=lambda s=spec, t=tv, f=vf, v=vv: self.add_from_container(s, t, f, v)
                       ).pack(side="left")
            ttk.Label(bar, text="（双击某行可快速填入该行标识）",
                      style="Hint.TLabel").pack(side="left", padx=(12, 0))
            tv.bind("<Double-1>", lambda e, t=tv: self._quick_pick_container(t))
            spec["_vf"] = vf
            spec["_vv"] = vv

    def _tab_allvars(self):
        fr = ttk.Frame(self.nb)
        self.nb.add(fr, text=" 全部变量 ")

        top = ttk.Frame(fr, padding=(8, 8, 8, 0))
        top.pack(fill="x")
        ttk.Label(top, text="搜索").pack(side="left")
        self.var_search = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.var_search, width=28)
        e.pack(side="left", padx=6)
        e.bind("<Return>", lambda ev: self.fill_allvars())
        ttk.Button(top, text="搜索", command=self.fill_allvars).pack(side="left")
        ttk.Button(top, text="显示全部", command=lambda: (self.var_search.set(""), self.fill_allvars())).pack(side="left", padx=6)
        self.lbl_allcount = ttk.Label(top, text="", style="Hint.TLabel")
        self.lbl_allcount.pack(side="left", padx=10)

        box, tv = self._mk_tree(fr, [("var", "变量名", 260, "w"), ("type", "类型", 80, "w"),
                                     ("cur", "当前值", 420, "w")])
        box.pack(fill="both", expand=True)
        self.tv_all = tv
        tv.bind("<Double-1>", lambda e: self._focus_new2())

        bar = ttk.Frame(fr, padding=(8, 6, 8, 8))
        bar.pack(fill="x")
        ttk.Label(bar, text="新值").pack(side="left")
        self.var_newval2 = tk.StringVar()
        ttk.Entry(bar, textvariable=self.var_newval2, width=16).pack(side="left", padx=(6, 10))
        self.mode2 = tk.StringVar(value="set")
        ttk.Radiobutton(bar, text="设为", variable=self.mode2, value="set").pack(side="left")
        ttk.Radiobutton(bar, text="增加", variable=self.mode2, value="add").pack(side="left", padx=(0, 10))
        ttk.Button(bar, text="加入待修改", command=self.add_from_allvars).pack(side="left")

    def _focus_new(self):
        self.ent_newval.focus_set()
        self.ent_newval.select_range(0, "end")

    def _focus_new2(self):
        pass

    # ============================================================ 启动 / 扫描

    def boot(self):
        self.log("正在查找 Agent17 …", "info")
        self.gamedir = C.find_game()
        if not self.gamedir:
            self._set_game("未找到游戏，请手动指定")
            self.log("没找到 Agent17。请点「手动指定游戏」，选到游戏目录"
                     "（里面有 game 文件夹的那一层）。", "warn")
            return
        exe = C.find_exe(self.gamedir)
        self._set_game(exe or self.gamedir)
        self.log("游戏目录：%s" % self.gamedir, "ok")
        self.keys = C.find_signing_keys()
        if self.keys:
            self.log("读到本机签名私钥 %d 个，改完可以正常读档。" % len(self.keys), "ok")
        else:
            self.log("警告：没找到签名私钥（RenPy\\tokens\\security_keys.txt）。"
                     "请先运行一次游戏再改，否则改完可能读不了档。", "err")
        self.scan_saves()

    def pick_game(self):
        d = filedialog.askdirectory(title="选到含 game 文件夹的那一层（即 Agent17.exe 所在目录）")
        if not d:
            return
        if not os.path.isdir(os.path.join(d, "game", "saves")):
            messagebox.showwarning("目录不对", "这个目录下没有 game\\saves，请选 Agent17.exe 所在的目录。")
            return
        self.gamedir = d
        self._set_game(C.find_exe(d) or d)
        self.keys = C.find_signing_keys()
        self.scan_saves()

    def scan_saves(self):
        if not self.gamedir:
            return
        self.store_cache.clear()
        paths = C.find_saves(self.gamedir)
        self.saves = []
        if not paths:
            self.tv_saves.delete(*self.tv_saves.get_children())
            self.log("这个游戏目录下没有存档。", "warn")
            return
        self.log("找到 %d 个存档，正在读取…" % len(paths), "info")
        self.set_status("读取存档中…")
        for p in paths:
            self.saves.append(C.save_summary(p))
        self.render_saves()
        self.set_status("就绪")
        self.log("读取完成。默认选中最近保存的存档。", "ok")
        self.refresh_tabs()

    def render_saves(self):
        mode = self.var_filter.get()
        self.tv_saves.delete(*self.tv_saves.get_children())
        n = 0
        for s in self.saves:
            slot = s.get("slot", "")
            is_auto = slot.startswith("auto-")
            if mode == "仅手动" and is_auto:
                continue
            if mode == "仅自动" and not is_auto:
                continue
            self.tv_saves.insert("", "end", iid=s["path"], values=(
                slot,
                time.strftime("%m-%d %H:%M", time.localtime(s.get("mtime", 0))),
                s.get("day", ""),
                ("{:,}".format(s["gold"]) if isinstance(s.get("gold"), int) else ""),
                s.get("loc", ""),
            ))
            n += 1
        kids = self.tv_saves.get_children()
        if kids:
            self.tv_saves.selection_set(kids[0])
        if n != len(self.saves):
            self.log("筛选后显示 %d / %d 个存档。" % (n, len(self.saves)), "info")

    def set_status(self, s):
        self.lbl_status.configure(text=s)

    def _set_game(self, text):
        self.lbl_game.configure(text="· Agent17 专用 ·  %s" % text)

    def log(self, msg, tag="info"):
        ts = time.strftime("%H:%M:%S")
        self.txt_log.insert("end", "[%s] %s\n" % (ts, msg), tag)
        self.txt_log.see("end")

    # ============================================================ 读 store

    def current_store(self):
        sel = self.tv_saves.selection()
        if not sel:
            return None, None
        path = sel[0]
        if path not in self.store_cache:
            try:
                self.store_cache[path] = C.load_store(path)
            except Exception as e:
                self.log("读取 %s 失败：%s" % (os.path.basename(path), e), "err")
                self.store_cache[path] = None
        return path, self.store_cache[path]

    def refresh_tabs(self):
        path, store = self.current_store()
        if store is None:
            return
        self.fill_values(store)
        self.fill_containers(store)
        self.fill_allvars()

    # ------------------------------------------------------------ 填充

    def _fmt(self, v):
        if isinstance(v, bool):
            return "真" if v else "假"
        if v is None:
            return "（空）"
        s = str(v)
        return s if len(s) <= 90 else s[:90] + "…"

    def fill_values(self, store):
        tv = self.tv_vals
        tv.delete(*tv.get_children())
        for cat, var, desc in C.COMMON_VARS:
            v = store.get(var, "—")
            tv.insert("", "end", iid=var, values=(cat, var.replace("store.", ""), var,
                                                  self._fmt(v), desc))

    def fill_containers(self, store):
        for name, (spec, tv) in self.cont_tabs.items():
            tv.delete(*tv.get_children())
            data = C.read_container(store, spec["var"])
            if isinstance(data, dict):
                data = [dict(v, **{spec["id_field"]: k}) for k, v in data.items()]
            if not isinstance(data, list):
                continue
            for el in data:
                if not isinstance(el, dict):
                    continue
                kid = el.get(spec["id_field"])
                if kid is None:
                    continue
                vals = [self._fmt(kid)]
                for f, _l, _t in spec["fields"]:
                    vals.append(self._fmt(el.get(f, "—")))
                tv.insert("", "end", iid="%s|%s" % (spec["name"], kid), values=vals)

    def fill_allvars(self):
        path, store = self.current_store()
        if store is None:
            return
        kw = self.var_search.get().strip().lower()
        tv = self.tv_all
        tv.delete(*tv.get_children())
        n = 0
        for k in sorted(store):
            if kw and kw not in k.lower():
                continue
            v = store[k]
            tn = type(v).__name__
            if tn in ("Probe",) or tn not in ("int", "bool", "float", "str", "NoneType", "list", "dict", "set", "tuple"):
                continue
            if isinstance(v, (list, dict, set, tuple)):
                continue
            tv.insert("", "end", iid=k, values=(k, tn, self._fmt(v)))
            n += 1
            if n >= 1500:
                break
        self.lbl_allcount.configure(text="%d 个" % n)

    # ============================================================ 加入待修改

    def _parse(self, text, cur):
        t = text.strip()
        if t == "":
            return None, "请填新值"
        if isinstance(cur, bool) or t in ("真", "假", "True", "False", "true", "false"):
            if t in ("真", "True", "true", "1"):
                return True, None
            if t in ("假", "False", "false", "0"):
                return False, None
            return None, "这里需要填「真」或「假」"
        if isinstance(cur, str):
            return t, None
        try:
            return int(t), None
        except ValueError:
            try:
                return float(t), None
            except ValueError:
                return t, None

    def _push(self, kind, ident, newval, label, oldval):
        for i, item in enumerate(self.pending):
            if item[0] == kind and item[1] == ident:
                self.pending[i] = (kind, ident, newval, label, oldval)
                self.refresh_pending()
                self.log("已更新待修改项：%s → %r" % (label, newval), "info")
                return
        self.pending.append((kind, ident, newval, label, oldval))
        self.refresh_pending()
        self.log("已加入待修改：%s  %r → %r" % (label, oldval, newval), "ok")

    def refresh_pending(self):
        self.tv_pend.delete(*self.tv_pend.get_children())
        for i, (kind, ident, nv, label, ov) in enumerate(self.pending):
            target, _sep, rest = label.partition("|")
            self.tv_pend.insert("", "end", iid=str(i),
                                values=(target.strip(), rest.strip(), self._fmt(ov), self._fmt(nv)))
        self.lbl_pend.configure(text="%d 项" % len(self.pending))

    def remove_pending(self):
        for iid in self.tv_pend.selection():
            try:
                self.pending.pop(int(iid))
            except Exception:
                pass
        self.refresh_pending()

    def clear_pending(self):
        self.pending = []
        self.refresh_pending()

    def add_from_values(self):
        sel = self.tv_vals.selection()
        if not sel:
            return messagebox.showinfo("提示", "先在上面选一个变量。")
        path, store = self.current_store()
        var = sel[0]
        cur = store.get(var)
        nv, err = self._parse(self.var_newval.get(), cur)
        if err:
            return messagebox.showwarning("新值不对", err)
        if self.mode.get() == "add":
            if isinstance(cur, (int, float)) and not isinstance(cur, bool):
                nv = cur + nv
            else:
                return messagebox.showwarning("不能加", "这个变量不是数字，只能用「设为」。")
        self._push("var", var, nv, "%s | %s" % ("存档", var), cur)

    def _quick_pick_container(self, tv):
        sel = tv.selection()
        if sel:
            self.clipboard_clear()
            self.clipboard_append(sel[0].split("|", 1)[1])

    def add_from_container(self, spec, tv, vf, vv):
        sel = tv.selection()
        if not sel:
            return messagebox.showinfo("提示", "先在列表里选一行。")
        path, store = self.current_store()
        kid = sel[0].split("|", 1)[1]
        field = vf.get()
        data = C.read_container(store, spec["var"])
        cur = None
        if isinstance(data, list):
            for el in data:
                if isinstance(el, dict) and el.get(spec["id_field"]) == kid:
                    cur = el.get(field)
                    break
        elif isinstance(data, dict):
            one = data.get(kid)
            if isinstance(one, dict):
                cur = one.get(field)
        nv, err = self._parse(vv.get(), cur)
        if err:
            return messagebox.showwarning("新值不对", err)
        ident = (spec["var"], spec["id_field"], kid, field)
        label = "%s | %s.%s" % (spec["name"], kid, field)
        self._push("container", ident, nv, label, cur)

    def add_from_allvars(self):
        sel = self.tv_all.selection()
        if not sel:
            return messagebox.showinfo("提示", "先在下面选一个变量。")
        path, store = self.current_store()
        var = sel[0]
        cur = store.get(var)
        nv, err = self._parse(self.var_newval2.get(), cur)
        if err:
            return messagebox.showwarning("新值不对", err)
        if self.mode2.get() == "add":
            if isinstance(cur, (int, float)) and not isinstance(cur, bool):
                nv = cur + nv
            else:
                return messagebox.showwarning("不能加", "这个变量不是数字，只能用「设为」。")
        self._push("var", var, nv, "%s | %s" % ("存档", var), cur)

    # ============================================================ 应用

    def do_apply(self):
        if not self.pending:
            return messagebox.showinfo("提示", "待修改清单是空的。先在上面加几项。")
        sel = list(self.tv_saves.selection())
        if not sel:
            return messagebox.showinfo("提示", "先在左上角选存档。")

        # 地盘校验：只允许写「当前游戏目录」里的存档
        root = os.path.normcase(os.path.abspath(self.gamedir or ""))
        outside = [p for p in sel if not os.path.normcase(os.path.abspath(p)).startswith(root)]
        if outside:
            return messagebox.showerror(
                "拒绝写入",
                "下面这些存档不在当前游戏目录里，已拒绝写入：\n\n%s\n\n"
                "（这是防止改错游戏的保护措施）" % "\n".join(outside))

        # 文件必须还在
        gone = [p for p in sel if not os.path.exists(p)]
        if gone:
            return messagebox.showerror("文件不存在", "这些存档找不到了，请点「重新扫描」：\n\n%s"
                                        % "\n".join(gone))

        if not self.keys:
            if not messagebox.askyesno("缺少签名私钥",
                                       "没找到本机签名私钥，改完可能读不了档。\n"
                                       "请先运行一次游戏再回来改。\n\n仍要继续吗？"):
                return

        # 「同一时刻」的其他存档
        if self.var_same.get():
            keys_of = {}
            for s in self.saves:
                keys_of.setdefault(s.get("key"), []).append(s["path"])
            extra = []
            for p in sel:
                for s in self.saves:
                    if s["path"] == p:
                        for q in keys_of.get(s.get("key"), []):
                            if q not in sel and q not in extra:
                                extra.append(q)
            if extra:
                names = ", ".join(os.path.basename(x) for x in extra)
                if not messagebox.askyesno("确认",
                                           "除了选中的存档，还发现 %d 个「同一时刻」的存档：\n\n%s\n\n"
                                           "一并修改吗？\n（建议一并改，否则读档后数值会变回去）"
                                           % (len(extra), names)):
                    extra = []
                sel = sel + extra

        if not messagebox.askyesno("最后确认",
                                   "即将修改 %d 个存档，共 %d 项改动。\n\n"
                                   "写入前会自动备份，可随时「还原备份」。\n\n继续吗？"
                                   % (len(sel), len(self.pending))):
            return

        self.set_status("写入中…")
        self.log("─" * 60, "info")
        ok_n = 0
        for p in sel:
            self.log("▶ %s" % os.path.basename(p), "info")
            try:
                lines = C.apply_edits(p, self.pending, self.keys)
            except Exception as e:
                self.log("[×] 出错：%s" % e, "err")
                continue
            for ln in lines:
                tag = "ok" if ln.startswith("[✓]") else ("err" if ln.startswith("[×]") else "warn")
                self.log("   " + ln, tag)
            if any(ln.startswith("[✓]") and "读回" not in ln and "备份" not in ln for ln in lines):
                ok_n += 1
        self.set_status("就绪")
        self.store_cache.clear()
        self.log("─" * 60, "info")
        self.log("完成：%d / %d 个存档已写入。" % (ok_n, len(sel)), "ok")
        self.scan_saves()
        messagebox.showinfo("完成", "已写入 %d 个存档。\n\n进游戏读档确认即可。\n"
                                    "若不满意，点「还原备份」可以退回。" % ok_n)

    # ============================================================ 还原

    def do_restore(self):
        sel = list(self.tv_saves.selection())
        if not sel:
            return messagebox.showinfo("提示", "先选一个存档，还原它所在目录的备份。")
        backs = C.list_backups(sel[0])
        if not backs:
            return messagebox.showinfo("没有备份", "这个目录下还没有备份。")
        latest = backs[0]
        bdir = os.path.join(C.backup_dir(sel[0]), latest)
        files = sorted(os.listdir(bdir))
        if not messagebox.askyesno("还原备份",
                                   "将把备份 %s 里的 %d 个存档还原回去：\n\n%s\n\n"
                                   "当前文件会被覆盖（覆盖前会再备份一次）。继续吗？"
                                   % (latest, len(files), "\n".join(files))):
            return
        targets = []
        for fn in files:
            for d in C.mirror_dirs(sel[0]):
                p = os.path.join(d, fn)
                if os.path.exists(p):
                    targets.append(p)
        C.backup_saves(targets, tag="before_restore_" + time.strftime("%Y%m%d_%H%M%S"))
        n = 0
        for fn in files:
            for d in C.mirror_dirs(sel[0]):
                p = os.path.join(d, fn)
                if os.path.exists(p):
                    try:
                        import shutil
                        shutil.copy2(os.path.join(bdir, fn), p)
                        n += 1
                    except Exception as e:
                        self.log("[×] 还原 %s 失败：%s" % (fn, e), "err")
        self.log("已还原 %d 个文件（来自备份 %s）" % (n, latest), "ok")
        self.scan_saves()
        messagebox.showinfo("完成", "已还原 %d 个文件。" % n)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
