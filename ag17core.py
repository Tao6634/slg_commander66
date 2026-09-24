# -*- coding: utf-8 -*-
"""
Agent17 存档读写引擎
-------------------
只做 Agent17 这一个游戏。所有写入都走「字节级 patch + 重签名 + 双镜像 + 备份」。

设计原则：
  1. 绝不重新序列化整个存档 —— 只替换 pickle 字节流里那一段值。
  2. 写前必备份、写前查重合度、写后验签 + 读回验证。
  3. 镜像目录按「游戏名前缀」匹配，绝不按文件名匹配（曾因此误覆盖过别的游戏）。
"""
import os
import io
import re
import sys
import glob
import time
import base64
import shutil
import struct
import pickle
import zipfile
import functools
import pickletools

SAVE_SUFFIX = "-LT1.save"
SKIP_OPS = ("MEMOIZE", "BINPUT", "LONG_BINPUT", "PUT")

GAME_TOKEN = "Agent17"


# ============================================================ 反序列化

_CACHE = {}


class Probe(object):
    """万能占位对象：游戏自定义类在这里被还原成普通属性字典。"""

    def __init__(self, *a, **k):
        self.__dict__["_args"] = a

    def __setstate__(self, s):
        if isinstance(s, dict):
            self.__dict__.update(s)
        else:
            self.__dict__["_state"] = s

    def __setitem__(self, k, v):
        self.__dict__.setdefault("_items", {})[k] = v

    def __getitem__(self, k):
        return self.__dict__.get("_items", {})[k]

    def append(self, v):
        self.__dict__.setdefault("_seq", []).append(v)

    def extend(self, v):
        self.__dict__.setdefault("_seq", []).extend(v)

    def add(self, v):
        self.__dict__.setdefault("_set", set()).add(v)

    def __hash__(self):
        return id(self)

    def __eq__(self, o):
        return self is o

    def __getattr__(self, n):
        if n.startswith("__") and n.endswith("__"):
            raise AttributeError(n)
        return Probe()

    def __call__(self, *a, **k):
        return Probe()

    def __len__(self):
        return 0

    def __iter__(self):
        return iter(())

    def __repr__(self):
        return "<%s>" % type(self).__name__


class PermissiveUnpickler(pickle.Unpickler):
    def find_class(self, mod, name):
        try:
            return super().find_class(mod, name)
        except Exception:
            pass
        if name not in _CACHE:
            _CACHE[name] = type(str(name), (Probe,), {})
        return _CACHE[name]


def unwrap(v, depth=0):
    """把 Probe 对象递归还原成 dict / list / 标量。"""
    if depth > 6:
        return "..."
    if isinstance(v, Probe):
        d = v.__dict__
        if "_seq" in d:
            return [unwrap(x, depth + 1) for x in d["_seq"]]
        if "_items" in d:
            return dict((k, unwrap(x, depth + 1)) for k, x in d["_items"].items())
        if "_set" in d:
            return set(repr(x) for x in d["_set"])
        if "_state" in d:
            return unwrap(d["_state"], depth + 1)
        return dict((k, unwrap(x, depth + 1)) for k, x in d.items() if k != "_args")
    if isinstance(v, list):
        return [unwrap(x, depth + 1) for x in v]
    if isinstance(v, dict):
        return dict((k, unwrap(x, depth + 1)) for k, x in v.items())
    return v


def read_log(save_path):
    with zipfile.ZipFile(save_path) as z:
        return z.read("log")


def load_store(save_path):
    obj = PermissiveUnpickler(io.BytesIO(read_log(save_path))).load()
    return obj[0] if isinstance(obj, tuple) else obj


def load_save(save_path):
    """返回 (store, meta)；meta 含保存时间、天数、金钱、位置等。"""
    import json as _json
    with zipfile.ZipFile(save_path) as z:
        names = set(z.namelist())
        log = z.read("log")
        meta = {}
        if "json" in names:
            try:
                meta = _json.loads(z.read("json").decode("utf-8"))
            except Exception:
                meta = {}
    obj = PermissiveUnpickler(io.BytesIO(log)).load()
    store = obj[0] if isinstance(obj, tuple) else obj
    return store, meta


# ============================================================ 路径

def appdata_renpy():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or ""
        if not base:
            profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
            base = os.path.join(profile, "AppData", "Roaming")
        return os.path.join(base, "RenPy")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/RenPy")
    return os.path.expanduser("~/.renpy")


def game_token(gamedir):
    name = os.path.basename(os.path.normpath(gamedir))
    m = re.match(r"[A-Za-z][A-Za-z0-9_]{2,}", name)
    return m.group(0) if m else None


def mirror_dirs(save_path):
    """
    返回这个存档需要同步写入的所有目录。
    ⚠️ 只按「游戏名前缀」匹配镜像目录 —— 绝不按文件名匹配。
    """
    d = os.path.dirname(os.path.abspath(save_path))
    if os.path.basename(d) != "saves":
        return [d]
    dirs = [d]
    gamedir = os.path.dirname(os.path.dirname(d))
    token = game_token(gamedir)
    base = appdata_renpy()
    if token and os.path.isdir(base):
        for c in sorted(os.listdir(base)):
            full = os.path.join(base, c)
            if os.path.isdir(full) and c.lower().startswith(token.lower()):
                dirs.append(full)
    return list(dict.fromkeys(dirs))


def find_saves(gamedir):
    """列出游戏目录里所有存档，按修改时间倒序。"""
    saves = os.path.join(gamedir, "game", "saves")
    if not os.path.isdir(saves):
        return []
    out = []
    for fn in os.listdir(saves):
        if fn.endswith(SAVE_SUFFIX):
            p = os.path.join(saves, fn)
            out.append(p)
    out.sort(key=lambda p: -os.path.getmtime(p))
    return out


def slot_name(save_path):
    b = os.path.basename(save_path)
    if b.endswith(SAVE_SUFFIX):
        b = b[: -len(SAVE_SUFFIX)]
    return b


def find_game():
    """自动找 Agent17 的游戏目录（含 game/saves 的那一层）。"""
    cands = []
    for drive in ("D:", "E:", "C:", "F:"):
        for pat in (
            drive + r"\games\*\*\*",
            drive + r"\games\*",
            drive + r"\*\Agent17*",
            drive + r"\*\*\Agent17*",
        ):
            cands.extend(glob.glob(pat))
    seen = set()
    for c in cands:
        if not os.path.isdir(c):
            continue
        base = os.path.basename(os.path.normpath(c))
        if not base.lower().startswith("agent17"):
            continue
        saves = os.path.join(c, "game", "saves")
        if os.path.isdir(saves) and c not in seen:
            seen.add(c)
            return c
    return None


def find_exe(gamedir):
    if not gamedir or not os.path.isdir(gamedir):
        return None
    for fn in os.listdir(gamedir):
        if fn.lower().endswith(".exe") and fn.lower().startswith("agent17"):
            return os.path.join(gamedir, fn)
    for fn in os.listdir(gamedir):
        if fn.lower().endswith(".exe"):
            return os.path.join(gamedir, fn)
    return None


# ============================================================ 签名

def find_signing_keys():
    out = []
    for kf in glob.glob(os.path.join(appdata_renpy(), "tokens", "security_keys.txt")):
        try:
            for line in open(kf, encoding="utf-8"):
                p = line.strip().split(None, 2)
                if len(p) >= 2 and p[0] == "signing-key":
                    out.append(base64.b64decode(p[1]))
        except Exception:
            pass
    return out


def sign_data(data, keys):
    import ecdsa

    out = ""
    for k in keys:
        sk = ecdsa.SigningKey.from_der(k)
        vk = sk.verifying_key
        sig = sk.sign(data)
        out += "signature %s %s\n" % (
            base64.b64encode(vk.to_der()).decode("ascii"),
            base64.b64encode(sig).decode("ascii"),
        )
    return out


def verify_save(save_path, keys):
    import ecdsa

    try:
        with zipfile.ZipFile(save_path) as z:
            log = z.read("log")
            try:
                sigtxt = z.read("signatures").decode("utf-8")
            except KeyError:
                return False, "无 signatures"
    except Exception as e:
        return False, "读取失败: %s" % e
    vks = set()
    for k in keys:
        try:
            vks.add(base64.b64encode(ecdsa.SigningKey.from_der(k).verifying_key.to_der()).decode())
        except Exception:
            pass
    for line in sigtxt.splitlines():
        p = line.strip().split(None, 2)
        if len(p) == 3 and p[0] == "signature":
            key = base64.b64decode(p[1])
            sig = base64.b64decode(p[2])
            if base64.b64encode(key).decode() not in vks:
                continue
            try:
                if ecdsa.VerifyingKey.from_der(key).verify(sig, log):
                    return True, "通过"
            except Exception as e:
                return False, str(e)
    return False, "签名不匹配"


# ============================================================ 字节级 patch

def encode_value(v):
    if isinstance(v, bool):
        return b"\x88" if v else b"\x89"
    if isinstance(v, int):
        if 0 <= v < 256:
            return b"K" + bytes([v])
        if 0 <= v < 65536:
            return b"M" + struct.pack("<H", v)
        if -2147483648 <= v < 2147483648:
            return b"J" + struct.pack("<i", v)
        return b"\x8a" + struct.pack("<q", v)
    if isinstance(v, str):
        b = v.encode("utf-8")
        if len(b) < 256:
            return b"\x8c" + bytes([len(b)]) + b
        return b"\x8d" + struct.pack("<I", len(b)) + b
    raise ValueError("不支持的类型: %r" % type(v))


def _ops(log):
    return list(pickletools.genops(io.BytesIO(log)))


_SIMPLE_STR_OPS = ("SHORT_BINUNICODE", "BINUNICODE", "UNICODE", "BINUNICODE8")
_SIMPLE_INT_OPS = ("BININT", "BININT1", "BININT2", "LONG", "INT", "LONG1", "LONG4")


def _scan(log):
    """
    走一遍 pickle 操作码，并跟踪 memo 表。
    返回 (ops, memo_created)：
      ops          = ((opname, arg, pos, resolved), ...)
                     resolved 对 BINGET/LONG_BINGET/GET 会解析成它引用的真实值
                     —— pickle 会把重复出现的字符串压缩成 memo 引用（如 'pKey' -> BINGET 29）。
      memo_created = {memo索引: 被 memo 的那个值的起始 op 下标}
                     —— 用于反查「提前生成、后面只放引用」的对象（如 99 个 ActorData）。
    """
    ops = list(pickletools.genops(io.BytesIO(log)))
    memo = {}
    memo_created = {}
    nxt = 0
    out = []
    last_push = None
    last_push_i = None
    for idx, (op, arg, pos) in enumerate(ops):
        name = op.name
        resolved = arg
        pushed = None

        if name in _SIMPLE_STR_OPS:
            pushed = arg
        elif name in _SIMPLE_INT_OPS:
            pushed = int(arg)
        elif name in ("BINFLOAT", "FLOAT"):
            pushed = float(arg)
        elif name == "NEWTRUE":
            pushed = True
        elif name == "NEWFALSE":
            pushed = False
        elif name == "NONE":
            pushed = None
        elif name in ("SHORT_BINBYTES", "BINBYTES", "BINBYTES8"):
            pushed = arg
        elif name in ("BINGET", "LONG_BINGET", "GET"):
            pushed = memo.get(arg)
            if pushed is not None:
                resolved = pushed
        elif name in ("BINPUT", "LONG_BINPUT", "PUT"):
            memo[arg] = last_push
            if last_push_i is not None:
                memo_created[arg] = last_push_i
            nxt = max(nxt, arg + 1)
        elif name == "MEMOIZE":
            memo[nxt] = last_push
            if last_push_i is not None:
                memo_created[nxt] = last_push_i
            nxt += 1

        out.append((name, arg, pos, resolved))
        last_push = pushed
        last_push_i = idx
    return tuple(out), memo_created


def _value_span(ops, i_key):
    """
    i_key 是键 opcode 的下标 -> 返回 (值起始字节, 值结束字节, 值 opcode 下标)。
    值区间 = [本 op 起点, 下一个 op 起点)，因此只对标量值（数字/开关/文字）成立。
    """
    j = i_key + 1
    while j < len(ops) and ops[j][0] in SKIP_OPS:
        j += 1
    if j >= len(ops):
        return None
    return ops[j][2], ops[j + 1][2], j


@functools.lru_cache(maxsize=4)
def _scan_cached(log):
    return _scan(log)


def _scan_ops(log):
    """带缓存的 _scan：同一份 log 只扫一次。"""
    return _scan_cached(log)[0]


def _memo_created(log):
    return _scan_cached(log)[1]


def _fix_frames(ops, out, vpos, delta):
    frames = [(pos, pos + 9, arg) for op, arg, pos in ops if op.name == "FRAME"]
    hit = None
    for fpos, dstart, flen in frames:
        if dstart <= vpos < dstart + flen:
            hit = (fpos, flen)
            break
    if hit is None and frames:
        hit = (frames[-1][0], frames[-1][2])
    if hit is not None:
        fpos, flen = hit
        out[fpos + 1: fpos + 9] = struct.pack("<Q", flen + delta)
    return out


def patch_spans(log, spans):
    """
    spans: [(start, end, new_value), ...]
    返回新 log 字节。按 start 倒序执行以免偏移错乱；同步累加所属 FRAME 的长度。
    """
    ops = _ops(log)
    frames = [(pos, pos + 9, arg) for op, arg, pos in ops if op.name == "FRAME"]
    out = bytearray(log)
    for start, end, newval in sorted(spans, key=lambda x: -x[0]):
        enc = encode_value(newval)
        delta = len(enc) - (end - start)
        out[start:end] = enc
        for fpos, dstart, flen in frames:
            if dstart <= start < dstart + flen:
                cur = struct.unpack("<Q", bytes(out[fpos + 1: fpos + 9]))[0]
                out[fpos + 1: fpos + 9] = struct.pack("<Q", cur + delta)
                break
    return bytes(out)


def locate_var(log, keyname):
    """定位顶层 store 变量的值区间。返回 (start, end) 或 None。"""
    ops = _scan_ops(log)
    for i, (name, arg, pos, res) in enumerate(ops):
        if name in _SIMPLE_STR_OPS and res == keyname:
            sp = _value_span(ops, i)
            if sp:
                return sp[0], sp[1]
    return None


def container_region(log, var):
    """
    返回某个容器变量「值」所占的 op 区间 [a, b)。
    区域上界 = 下一个顶层 store.xxx 键出现的位置。
    """
    ops = _scan_ops(log)
    n = len(ops)
    for i, (name, arg, pos, res) in enumerate(ops):
        if name in _SIMPLE_STR_OPS and res == var:
            j = i + 1
            while j < n and ops[j][0] in SKIP_OPS:
                j += 1
            k = j + 1
            while k < n:
                n2, a2, p2, r2 = ops[k]
                if n2 in _SIMPLE_STR_OPS and isinstance(r2, str) and r2.startswith("store."):
                    break
                k += 1
            return j, k
    return None


def element_starts(log, var, id_field="pKey"):
    """
    找出容器里每个元素的「起始 op 下标」。
    两种情况都支持：
      1. 元素内联写在容器区域内  -> 取 id_field 键的位置
      2. 元素提前生成、区域里只放 memo 引用（如 99 个 ActorData）-> 用 memo_created 反查
    """
    ops = _scan_ops(log)
    memoc = _memo_created(log)
    reg = container_region(log, var)
    if not reg:
        return []
    a, b = reg

    # 两条路都走一遍，然后选「命中更多」的那条。
    #   内联：元素就地写在容器区域里（id_field 键直接出现）
    #   引用：元素提前生成，这里只放 memo 引用（如 99 个 ActorData）
    # 单看某一条会误判，所以两条都算、比数量。
    inline = [k for k in range(a, b)
              if isinstance(ops[k][3], str) and ops[k][3] == id_field]

    refs = []
    for k in range(a, b):
        n, ar, p, r = ops[k]
        if n in ("BINGET", "LONG_BINGET", "GET"):
            t = memoc.get(ar)
            # 只认「指向对象实例化」的引用（NEWOBJ），指向字符串的引用会被过滤掉
            if t is not None and ops[t][0] == "NEWOBJ" and t not in refs:
                refs.append(t)
    refs.sort()

    def _has_id(start, end):
        for j in range(start, min(end, len(ops))):
            if isinstance(ops[j][3], str) and ops[j][3] == id_field:
                return True
        return False

    good_refs = []
    for i, t in enumerate(refs):
        e = refs[i + 1] if i + 1 < len(refs) else min(t + 250, b)
        if _has_id(t, e):
            good_refs.append(t)

    return good_refs if len(good_refs) > len(inline) else inline


def list_elements(log, var, id_field="pKey"):
    """列出某个容器里的元素：[(主键值, 元素 op 起始, 元素 op 结束), ...]"""
    ops = _scan_ops(log)
    starts = element_starts(log, var, id_field)
    out = []
    for i, st in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else min(st + 400, len(ops))
        for j in range(st, min(end, len(ops))):
            if isinstance(ops[j][3], str) and ops[j][3] == id_field:
                sp = _value_span(ops, j)
                if sp:
                    out.append((ops[sp[2]][3], st, end))
                break
    return out


def locate_container_field(log, var, id_field, id_value, field):
    """
    定位容器里某个元素的字段。
    例：var='store.g_InventoryItem', id_field='pKey', id_value='phone', field='count'
    返回 (start, end) 或 None。
    """
    ops = _scan_ops(log)
    for keyval, st, end in list_elements(log, var, id_field):
        if keyval != id_value:
            continue
        for j in range(st, min(end, len(ops))):
            if isinstance(ops[j][3], str) and ops[j][3] == id_field:
                for m in range(j + 1, min(end, len(ops))):
                    if isinstance(ops[m][3], str) and ops[m][3] == field:
                        sp2 = _value_span(ops, m)
                        if sp2:
                            return sp2[0], sp2[1]
                        break
                break
    return None


# ============================================================ 备份 / 写入

def backup_dir(save_path):
    d = os.path.dirname(os.path.abspath(save_path))
    return os.path.join(d, "_ag17_backups")


def backup_saves(paths, tag=None):
    """把一批存档备份到 _ag17_backups/时间戳/，返回备份目录。"""
    if not paths:
        return None
    stamp = tag or time.strftime("%Y%m%d_%H%M%S")
    base = backup_dir(paths[0])
    dest = os.path.join(base, stamp)
    os.makedirs(dest, exist_ok=True)
    for p in paths:
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(dest, os.path.basename(p)))
    return dest


def list_backups(save_path):
    base = backup_dir(save_path)
    if not os.path.isdir(base):
        return []
    return sorted([d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))], reverse=True)


def build_save(src, dst, new_log, sig):
    """把新 log + 新签名写回 zip，其余条目原样保留。"""
    tmp = dst + ".tmp"
    with zipfile.ZipFile(src, "r") as zin:
        items = [(it, zin.read(it.filename)) for it in zin.infolist()]
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zo:
        for it, data in items:
            if it.filename == "log":
                zo.writestr(it, new_log)
            elif it.filename == "signatures":
                zo.writestr(it, sig)
            else:
                zo.writestr(it, data)
    os.replace(tmp, dst)


def check_compatible(store, dst_path):
    """写入前安全闸：变量集重合度 < 60% 拒绝写入。"""
    if not os.path.exists(dst_path):
        return True, 1.0
    try:
        other = load_store(dst_path)
    except Exception:
        return True, 1.0
    a, b = set(store), set(other)
    if not a:
        return True, 1.0
    ratio = len(a & b) / float(len(a))
    return (ratio >= 0.6), ratio


def apply_edits(save_path, edits, keys, progress=None):
    """
    edits: [('var', 'store.g_gold', 5000),
            ('container', ('store.g_InventoryItem', 'pKey', 'phone', 'count'), 99)]
    返回日志字符串列表。
    """
    log_lines = []

    def say(s):
        log_lines.append(s)
        if progress:
            progress(s)

    store = load_store(save_path)
    raw_log = read_log(save_path)

    # 1. 定位
    spans = []
    for item in edits:
        kind, ident, newval = item[0], item[1], item[2]
        if kind == "var":
            sp = locate_var(raw_log, ident)
            if not sp:
                say("[×] 找不到变量 %s，跳过" % ident)
                continue
            spans.append((sp[0], sp[1], newval))
        else:
            var, id_field, id_value, field = ident
            sp = locate_container_field(raw_log, var, id_field, id_value, field)
            if not sp:
                say("[×] 找不到 %s 里 %s=%s 的 %s，跳过" % (var, id_field, id_value, field))
                continue
            spans.append((sp[0], sp[1], newval))

    if not spans:
        say("[!] 没有可写入的改动")
        return log_lines

    new_log = patch_spans(raw_log, spans)

    # 2. 自检
    try:
        PermissiveUnpickler(io.BytesIO(new_log)).load()
    except Exception as e:
        say("[×] patch 后无法反序列化，已放弃：%s" % e)
        return log_lines

    # 3. 找到所有镜像
    targets = []
    for d in mirror_dirs(save_path):
        p = os.path.join(d, os.path.basename(save_path))
        if os.path.exists(p):
            targets.append(p)
    if not targets:
        targets = [save_path]

    # 4. 安全闸 + 备份
    ok_targets = []
    for t in targets:
        good, ratio = check_compatible(store, t)
        if good:
            ok_targets.append(t)
        else:
            say("[×] 拒绝写入 %s（变量重合度仅 %.0f%%）" % (t, ratio * 100))
    if not ok_targets:
        say("[!] 全部目标未通过安全检查")
        return log_lines

    bdir = backup_saves(ok_targets)
    say("[✓] 已备份 %d 个存档 → %s" % (len(ok_targets), bdir))

    # 5. 写入 + 重签 + 验签
    sig = sign_data(new_log, keys)
    written = []
    for t in ok_targets:
        try:
            build_save(t, t, new_log, sig)
            ok, msg = verify_save(t, keys)
            if ok:
                say("[✓] %s  写入 %d 处  签名已重签" % (os.path.basename(t), len(spans)))
                written.append(t)
            else:
                say("[×] %s  验签失败：%s" % (os.path.basename(t), msg))
        except Exception as e:
            say("[×] %s  写入失败：%s" % (os.path.basename(t), e))

    # 6. 读回验证
    if written:
        bad = 0
        for label, got, want, ok in verify_edits(written[0], edits):
            if ok:
                say("[✓] 读回 %s = %r" % (label, got))
            else:
                bad += 1
                say("[×] 读回 %s = %r（期望 %r）" % (label, got, want))
        if bad:
            say("[!] 有 %d 项没生效，请点「还原备份」回退" % bad)

    return log_lines


def verify_edits(save_path, edits):
    """写完后重新读回，逐个核对改动是否真的生效。"""
    store = load_store(save_path)
    out = []
    for item in edits:
        kind, ident, want = item[0], item[1], item[2]
        if kind == "var":
            got = store.get(ident)
            out.append((ident, got, want, got == want))
        else:
            var, idf, idv, fld = ident
            elems = read_container(store, var)
            got = None
            if isinstance(elems, list):
                for e in elems:
                    if isinstance(e, dict) and e.get(idf) == idv:
                        got = e.get(fld)
                        break
            elif isinstance(elems, dict):
                one = elems.get(idv)
                if isinstance(one, dict):
                    got = one.get(fld)
            out.append(("%s[%s=%s].%s" % (var, idf, idv, fld), got, want, got == want))
    return out


def progress_key(store):
    """判断「同一时刻」用的指纹。"""
    return (
        store.get("store.g_day"),
        store.get("store.g_time"),
        store.get("store.g_week"),
        store.get("store.g_mylocationKey"),
        store.get("store.g_questSwitch"),
    )


def save_summary(save_path):
    """读一个存档的概要，用于列表显示。"""
    try:
        store, meta = load_save(save_path)
    except Exception as e:
        return {"slot": slot_name(save_path), "error": str(e), "mtime": os.path.getmtime(save_path)}
    return {
        "slot": slot_name(save_path),
        "path": save_path,
        "mtime": os.path.getmtime(save_path),
        "saved_at": meta.get("_save_name") or time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(save_path))),
        "day": store.get("store.g_day"),
        "time": store.get("store.g_time"),
        "week": store.get("store.g_week"),
        "gold": store.get("store.g_gold"),
        "loc": store.get("store.g_mylocationKey"),
        "ver": meta.get("_version"),
        "key": progress_key(store),
    }


# ============================================================ 常用变量表

COMMON_VARS = [
    ("金钱", "store.g_gold", "主货币，商店买东西用"),
    ("金钱", "store.luna_gold", "Luna 相关金额"),
    ("金钱", "store.g_prisonGold", "监狱币"),
    ("金钱", "store.player_coins", "赌场筹码"),
    ("金钱", "store.g_frank_money", "Frank 身上的钱"),
    ("金钱", "store.g_amelia_money", "Amelia 身上的钱"),
    ("金钱", "store.g_erica_money", "Erica 身上的钱"),
    ("金钱", "store.g_nora_money", "Nora 身上的钱"),
    ("属性", "store.g_semen_point", "点数"),
    ("属性", "store.g_barbecue_level", "烤肉等级"),
    ("属性", "store.g_table_level", "桌子等级"),
    ("属性", "store.g_find_star_level", "找星星等级"),
    ("属性", "store.g_helen_level", "Helen 等级"),
    ("属性", "store.g_helen_level_max", "Helen 等级上限"),
    ("属性", "store.g_dana_spanking_love1", "Dana 好感 1"),
    ("属性", "store.g_dana_spanking_love2", "Dana 好感 2"),
    ("属性", "store.g_dana_spanking_love3", "Dana 好感 3"),
    ("容量", "store.g_InventoryItemMaxSlot", "背包格数"),
    ("容量", "store.g_InventoryPictueMaxSlot", "相册格数"),
    ("进度", "store.g_day", "天数（改大会让限时任务失败）"),
    ("进度", "store.g_time", "时段"),
    ("进度", "store.g_week", "周数"),
]

# 可改的容器面板：(显示名, 存档变量, 类名, 主键字段, 可改字段, 字段类型)
CONTAINERS = [
    {
        "name": "背包物品",
        "var": "store.g_InventoryItem",
        "cls": "ItemData",
        "id_field": "pKey",
        "fields": [("count", "数量", "int"), ("work", "可用", "int")],
    },
    {
        "name": "角色数据",
        "var": "store.g_InventoryActor",
        "cls": "ActorData",
        "id_field": "pKey",
        "fields": [("max_love", "好感上限", "int"), ("know", "认识", "int"),
                   ("hp", "HP", "int"), ("coom_value", "值", "int"),
                   ("lock", "锁定", "int")],
    },
    {
        "name": "技能等级",
        "var": "store.g_InventorySkill",
        "cls": "SkillData",
        "id_field": "pKey",
        "fields": [("level", "等级", "int"), ("exp", "经验", "int")],
    },
    {
        "name": "相册解锁",
        "var": "store.g_InventoryPicture",
        "cls": "PictureData",
        "id_field": "pKey",
        "fields": [("count", "解锁", "int")],
    },
    {
        "name": "按摩好感",
        "var": "store.g_InventoryMassage",
        "cls": "MassageData",
        "id_field": "pKey",
        "fields": [("heart", "好感", "int")],
    },
    {
        "name": "卡片翻面",
        "var": "store.g_InventoryCard",
        "cls": "CardData",
        "id_field": "pKey",
        "fields": [("front", "已翻面", "bool")],
    },
]


def read_container(store, var):
    v = store.get(var)
    return unwrap(v)
