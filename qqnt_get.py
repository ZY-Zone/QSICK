#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""获取 QQ NT —— 下载 QQ NT 安装包并解压到 .\\runtime\\

纯标准库实现，不依赖 7z.exe / tar.exe / py7zr。
QQ 安装包其实是「PE 壳 + 内嵌 7z 流」，7z 流用的是 LZMA + BCJ2 组合压缩，
本脚本自带 7z 容器解析器、LZMA 解码（走标准库 lzma）与 BCJ2 解码（按 LZMA SDK
Bcj2.c 逐行移植），因此解压完全由 Python 自己完成。

用法:
    获取QQnt.bat                      常规：下载 -> 校验 -> 解压到 runtime\\
    python qqnt_get.py --force        强制重新下载
    python qqnt_get.py --delete-source 解压完删掉安装包（省 300MB）
    python qqnt_get.py --source X.exe 直接使用本地安装包，不联网
    python qqnt_get.py --dir D:\\out  解压到指定目录（默认 runtime\\）
    python qqnt_get.py --no-flatten   不把 Files\\ 里的内容上提一层
"""

from __future__ import annotations

import argparse
import hashlib
import lzma
import os
import re
import shutil
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

# ---------------------------------------------------------------- 常量

INSTALLER_NAME = "QQ_9.9.33_260813_x64_01.exe"
GITHUB_URL = (
    "https://github.com/Rodert/qq-versions/releases/download/"
    "qq-packages-20260813-1d08f1d4/QQ_9.9.33_260813_x64_01.exe"
)
# 连 GitHub 最多等这么久（秒）。
#
# 5 秒够一次 DNS 解析加 TLS 握手，不够把一个卡死的连接拖成"程序没反应"。超时即判定这条路走不通，
# 转去自己的服务器——用户宁可换个源，也不愿意盯着一个不动的窗口猜它在干什么。
GITHUB_TIMEOUT = 5.0
# 兜底：项目自己的发布服务器（由 runtime_fetch 下载并解包，带 descriptor + sha256）。
# 这里只存地址，真正的下载逻辑复用 runtime_fetch，不复制第二份。
SERVER_BASE_URL = "http://49.235.134.245/qq_got/QQNT-9.9.33-52230"
EXPECTED_SHA256 = "b25c0d3ce9df764074a9118d0ded927e1b2d7ebf60e306112e8df18a040ec492"
SEVENZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"

CHUNK = 1 << 20
EMIT_CHUNK = 1 << 22


# ---------------------------------------------------------------- 输出

def _init_console() -> None:
    """编码交给运行环境（.bat 里是 chcp 65001，直接运行则是本地代码页），
    这里只保证写不出去的字符不会让脚本崩掉。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def log(msg: str = "") -> None:
    print(msg, flush=True)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return "%.2f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%.2f GB" % n


def script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 下载

def _request(url: str, offset: int = 0) -> urllib.request.Request:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) qqnt-get/1.0",
        "Accept": "*/*",
    }
    if offset:
        headers["Range"] = "bytes=%d-" % offset
    return urllib.request.Request(url, headers=headers)


def _download_one(url: str, dest: str, expect: int | None, timeout: float = 60) -> None:
    """从 url 下载到 dest，支持断点续传。失败抛异常。

    ``timeout`` 是 urllib 的套接字超时：连接建立和数据读取都受它约束，所以传 5 秒时，一路卡住的
    连接会在 5 秒后抛出来，而不是让整个启动挂在这里。
    """
    have = os.path.getsize(dest) if os.path.exists(dest) else 0
    if expect and have > expect:
        os.remove(dest)
        have = 0
    req = _request(url, have)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if have and resp.status != 206:
            have = 0  # 服务器不支持续传，从头来
        total = expect
        cl = resp.headers.get("Content-Length")
        if cl is not None and cl.isdigit():
            total = have + int(cl) if resp.status == 206 else int(cl)
        mode = "ab" if have else "wb"
        done = have
        started = time.time()
        last = 0.0
        with open(dest, mode) as fh:
            while True:
                block = resp.read(CHUNK)
                if not block:
                    break
                fh.write(block)
                done += len(block)
                now = time.time()
                if now - last > 0.3:
                    last = now
                    speed = done / max(now - started, 1e-6)
                    if total:
                        pct = done * 100.0 / total
                        log("\r    下载中 %5.1f%%  %s / %s  %s/s" %
                            (pct, human(done), human(total), human(speed)))
                    else:
                        log("\r    下载中 %s  %s/s" % (human(done), human(speed)))
    if total and os.path.getsize(dest) != total:
        raise IOError("大小不符: 期望 %d 实际 %d" % (total, os.path.getsize(dest)))


def download_installer(dest: str, expect_size: int | None = None,
                       timeout: float = GITHUB_TIMEOUT) -> str:
    """纯 GitHub 直连下载，只有这一个地址，不经过任何镜像或代理。

    这里曾经挂着一串 gh-proxy 镜像（ghfast.top、gh-proxy.com……），按顺序在失败时逐个换。
    去掉它们有两个理由：

    * 镜像会把安装包经第三方转发一次。安装包本身有 sha256 校验，篡改能被发现，但"把 300 MB
      的软件分发给谁的服务器"这件事本身不该顺手做掉；
    * 它们的可用性不受控。真正需要的兜底是**我们自己的服务器**，那是我们知道它是什么、
      由谁维护的地址，而不是十几个陌生域名。
    """
    log("  直连 GitHub（%.0f 秒超时）" % timeout)
    try:
        _download_one(GITHUB_URL, dest, expect_size, timeout)
    except Exception as exc:                          # noqa: BLE001
        log("")
        if os.path.exists(dest) and os.path.getsize(dest) == 0:
            os.remove(dest)
        raise RuntimeError("GitHub 直连失败: %s: %s" % (type(exc).__name__, exc)) from exc
    log("")
    return GITHUB_URL


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- 7z 容器

class _Cursor:
    """在内存里顺序读 7z 头部结构。"""

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.d = data
        self.p = pos

    def byte(self) -> int:
        v = self.d[self.p]
        self.p += 1
        return v

    def take(self, n: int) -> bytes:
        v = self.d[self.p:self.p + n]
        if len(v) != n:
            raise ValueError("7z 头部被截断")
        self.p += n
        return v

    def num(self) -> int:
        first = self.byte()
        mask = 0x80
        val = 0
        for i in range(8):
            if (first & mask) == 0:
                return val | ((first & (mask - 1)) << (8 * i))
            val |= self.byte() << (8 * i)
            mask >>= 1
        return val

    def bits(self, n: int) -> list[int]:
        out: list[int] = []
        b = 0
        mask = 0
        for _ in range(n):
            if mask == 0:
                b = self.byte()
                mask = 0x80
            out.append(1 if (b & mask) else 0)
            mask >>= 1
        return out


class _SliceReader:
    """从文件的一段固定区间里顺序读。"""

    def __init__(self, path: str, offset: int, size: int) -> None:
        self.fh = open(path, "rb")
        self.fh.seek(offset)
        self.left = size
        self.size = size

    def read(self, n: int) -> bytes:
        if self.left <= 0:
            return b""
        data = self.fh.read(min(n, self.left))
        self.left -= len(data)
        return data

    def close(self) -> None:
        try:
            self.fh.close()
        except Exception:
            pass


class _LzmaReader:
    """把一个 LZMA/LZMA2 压缩流包装成顺序读取器。

    内部用 bytearray + 游标，只在需要补数据时才压缩一次缓冲区；
    否则 BCJ2 每次只读 4 字节就复制整个缓冲区，会退化到几乎卡死。
    """

    def __init__(self, inner: _SliceReader, filters: list, out_size: int) -> None:
        self.inner = inner
        self.dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
        self.buf = bytearray()
        self.pos = 0
        self.left = out_size
        self.done = False

    def read(self, n: int) -> bytes:
        while len(self.buf) - self.pos < n and not self.done and self.left > 0:
            block = self.inner.read(CHUNK)
            if not block:
                self.done = True
                break
            out = self.dec.decompress(block)
            if out:
                if self.pos:
                    del self.buf[:self.pos]
                    self.pos = 0
                self.buf += out
        end = min(self.pos + n, len(self.buf))
        out = bytes(self.buf[self.pos:end])
        self.pos = end
        self.left -= len(out)
        if self.pos > (8 << 20):          # 偶尔收缩，避免一直增长
            del self.buf[:self.pos]
            self.pos = 0
        return out

    def close(self) -> None:
        self.inner.close()


_MARKER_RE = re.compile(rb"[\xe8\xe9]|\x0f[\x80-\x8f]")


class _Bcj2Decoder:
    """BCJ2 解码器，按 LZMA SDK 的 Bcj2.c 移植。

    与 C 版一致：只有 (0xE8)、(0xE9)、(0x0F 0x8x) 会被当作候选，
    再由 RC 流里的一个二进制符号决定是否真的做了地址转换。
    """

    TOP = 1 << 24
    PROB_BITS = 11
    PROB_TOTAL = 1 << 11
    MOVE_BITS = 5

    def __init__(self, main, call, jump, rc, out_size: int, sink) -> None:
        self.main = main
        self.call = call
        self.jump = jump
        self.rc = rc
        self.out_size = out_size
        self.sink = sink

    def run(self) -> int:
        probs = [self.PROB_TOTAL >> 1] * 258
        code = 0
        for _ in range(5):
            code = ((code << 8) | self.rc.read(1)[0]) & 0xFFFFFFFF
        rng = 0xFFFFFFFF
        out_pos = 0
        prev = 0            # 上一个输出字节
        buf = b""
        i = 0
        eof = False
        mask = 0xFFFFFFFF
        pending = bytearray()          # 攒够再写盘，避免每几个字节一次 IO

        def push(data: bytes) -> None:
            pending.extend(data)
            if len(pending) >= EMIT_CHUNK:
                self.sink.write(bytes(pending))
                pending.clear()

        while out_pos < self.out_size:
            if rng < self.TOP:
                rng = (rng << 8) & mask
                code = ((code << 8) | self.rc.read(1)[0]) & mask

            # 保证至少有 2 字节前瞻，避免把 0x0F 0x8x 拆到两块里
            while not eof and len(buf) - i < 2:
                block = self.main.read(EMIT_CHUNK)
                if not block:
                    eof = True
                    break
                buf = buf[i:] + block
                i = 0
            if i >= len(buf):
                break

            m = _MARKER_RE.search(buf, i)
            marker_at = -1
            if m is not None:
                p0 = m.start()
                marker_at = p0 + 1 if buf[p0] == 0x0F else p0
            # C 版判定 0x0F 0x8x 用的是滚动窗口 v。刚做过地址转换时 v 的高字节是
            # 操作数的最后一个字节，不在 main 流里，所以紧跟其后的 0x8x 同样是候选。
            if (prev == 0x0F and 0x80 <= buf[i] <= 0x8F
                    and (marker_at < 0 or i < marker_at)):
                marker_at = i
            if marker_at < 0:
                end = len(buf)
                if not eof and buf[end - 1] == 0x0F:
                    end -= 1          # 留给下一块判断 0x0F 0x8x
                if end > i:
                    seg = buf[i:end]
                    push(seg)
                    out_pos += len(seg)
                    prev = seg[-1]
                    i = end
                continue

            seg = buf[i:marker_at + 1]
            before = prev
            push(seg)
            out_pos += len(seg)
            prev = seg[-1]
            i = marker_at + 1
            v = ((seg[-2] << 24) | seg[-1]) if len(seg) >= 2 else \
                (((before << 24) | seg[0]) & 0xFFFFFFFF)

            c = ((v + 0x17) >> 6) & 1
            # 索引范围是 2 + 256 = 258 个槽位：E8 用 prev+2，最大 257，不能再截断。
            idx = ((0 - c) & ((v >> 24) & 0xFF)) + c + ((v >> 5) & 1)
            ttt = probs[idx]
            bound = (rng >> self.PROB_BITS) * ttt
            if code < bound:
                rng = bound
                probs[idx] = (ttt + ((self.PROB_TOTAL - ttt) >> self.MOVE_BITS)) & 0xFFFF
                continue                       # 没转换，就是普通字节
            rng -= bound
            code -= bound
            probs[idx] = (ttt - (ttt >> self.MOVE_BITS)) & 0xFFFF

            cj = ((v + 0x57) >> 6) & 1         # 0 -> CALL 流, 1 -> JUMP 流
            raw = (self.call if cj == 0 else self.jump).read(4)
            val = (struct.unpack(">I", raw)[0] - ((out_pos + 4) & mask)) & mask
            push(struct.pack("<I", val))
            out_pos += 4
            prev = (val >> 24) & 0xFF

        if pending:
            self.sink.write(bytes(pending))
        return out_pos


class SevenZip:
    """最小可用的 7z 读取器：支持 Copy / LZMA / LZMA2 / BCJ2。"""

    def __init__(self, path: str, offset: int = 0) -> None:
        self.path = path
        self.fh = open(path, "rb")
        self.fh.seek(offset)
        head = self.fh.read(32)
        if head[:6] != SEVENZIP_MAGIC:
            raise ValueError("不是 7z 流")
        _magic, _ver, _shcrc, nho, nhs, _nhcrc = struct.unpack("<6sHIQQI", head)
        self.base = offset + 32
        self.fh.seek(self.base + nho)
        raw_header = self.fh.read(nhs)
        cur = _Cursor(raw_header)
        pid = cur.byte()
        if pid == 0x17:
            enc = self._read_streams_info(cur)
            header = self._folder_bytes(enc, 0)
            cur = _Cursor(header)
            pid = cur.byte()
        if pid != 0x01:
            raise ValueError("7z 主头部 ID 异常: 0x%02X" % pid)
        self.header = self._read_header(cur)

    # ---- 解析 ----
    def _read_pack_info(self, cur: _Cursor):
        pack_pos = cur.num()
        n = cur.num()
        sizes: list[int] = []
        while True:
            t = cur.byte()
            if t == 0x00:
                break
            if t == 0x09:
                sizes = [cur.num() for _ in range(n)]
            elif t == 0x0A:
                all_def = cur.byte()
                defined = [1] * n if all_def else cur.bits(n)
                for d in defined:
                    if d:
                        cur.take(4)
            else:
                raise ValueError("PackInfo 未知字段 0x%02X" % t)
        return pack_pos, sizes, n

    def _read_folder(self, cur: _Cursor):
        ncoders = cur.num()
        coders = []
        tot_in = tot_out = 0
        for _ in range(ncoders):
            flags = cur.byte()
            cid = cur.take(flags & 0x0F).hex()
            nin = nout = 1
            if flags & 0x10:
                nin = cur.num()
                nout = cur.num()
            props = b""
            if flags & 0x20:
                props = cur.take(cur.num())
            coders.append(dict(id=cid, nin=nin, nout=nout, props=props,
                               inb=tot_in, outb=tot_out))
            tot_in += nin
            tot_out += nout
        binds = [(cur.num(), cur.num()) for _ in range(tot_out - 1)]
        npacked = tot_in - len(binds)
        bound = {i for (i, _o) in binds}
        unbound = [g for g in range(tot_in) if g not in bound]
        save = cur.p
        packed = [cur.num() for _ in range(npacked)]
        # packed[i] 是「第 i 个 pack 流喂给哪个 in-stream」。
        # 7-Zip 26 在该数组等于自然顺序时会省略它，故校验失败就回退。
        if (sorted(packed) != sorted(unbound) or cur.p >= len(cur.d)
                or cur.d[cur.p] != 0x0C):
            cur.p = save
            packed = list(unbound)
        return dict(coders=coders, binds=binds, packed=packed,
                    tin=tot_in, tout=tot_out)

    def _read_unpack_info(self, cur: _Cursor, npack: int):
        if cur.byte() != 0x0B:
            raise ValueError("缺少 kFolder")
        nf = cur.num()
        cur.byte()                      # external
        folders = [self._read_folder(cur) for _ in range(nf)]
        if cur.byte() != 0x0C:
            raise ValueError("缺少 kCodersUnpackSize")
        usizes = [[cur.num() for _ in range(f["tout"])] for f in folders]
        while True:
            t = cur.byte()
            if t == 0x00:
                break
            if t == 0x0A:
                all_def = cur.byte()
                defined = [1] * nf if all_def else cur.bits(nf)
                for d in defined:
                    if d:
                        cur.take(4)
            else:
                raise ValueError("UnpackInfo 未知字段 0x%02X" % t)
        return folders, usizes

    def _read_substreams(self, cur: _Cursor, folders, usizes):
        nf = len(folders)
        nums = [1] * nf
        t = cur.byte()
        if t == 0x0D:
            nums = [cur.num() for _ in range(nf)]
            t = cur.byte()
        sizes: list[list[int]] = []
        if t == 0x09:
            for i, folder in enumerate(folders):
                nn = nums[i]
                if nn == 0:
                    sizes.append([])
                    continue
                total = usizes[i][folder["tout"] - 1]
                part = [cur.num() for _ in range(nn - 1)]
                part.append(total - sum(part))
                sizes.append(part)
            t = cur.byte()
        else:
            sizes = [[usizes[i][folders[i]["tout"] - 1]] for i in range(nf)]
        crcs = None
        if t == 0x0A:
            nd = sum(nums)
            all_def = cur.byte()
            defined = [1] * nd if all_def else cur.bits(nd)
            crcs = [struct.unpack("<I", cur.take(4))[0] if d else None for d in defined]
            t = cur.byte()
        if t != 0x00:
            raise ValueError("SubStreamsInfo 结尾异常")
        return nums, sizes, crcs

    def _read_streams_info(self, cur: _Cursor):
        si = dict(packpos=0, psizes=[], npack=0, folders=[], usizes=[],
                  nums=[], ssizes=[], scrcs=None)
        while True:
            t = cur.byte()
            if t == 0x00:
                break
            if t == 0x06:
                si["packpos"], si["psizes"], si["npack"] = self._read_pack_info(cur)
            elif t == 0x07:
                si["folders"], si["usizes"] = self._read_unpack_info(cur, si["npack"])
            elif t == 0x08:
                si["nums"], si["ssizes"], si["scrcs"] = self._read_substreams(
                    cur, si["folders"], si["usizes"])
            else:
                raise ValueError("StreamsInfo 未知字段 0x%02X" % t)
        return si

    def _read_files_info(self, cur: _Cursor):
        n = cur.num()
        props: dict = {}
        while True:
            t = cur.num()
            if t == 0x00:
                break
            size = cur.num()
            if t == 0x0E:
                props["empty"] = cur.bits(n)
            elif t in (0x0F, 0x10):
                props["emptyfile" if t == 0x0F else "anti"] = cur.bits(size)
            elif t == 0x11:
                cur.byte()              # external
                raw = cur.take(size - 1)
                names = []
                i = 0
                while i < len(raw):
                    j = i
                    while j + 1 < len(raw) and not (raw[j] == 0 and raw[j + 1] == 0):
                        j += 2
                    names.append(raw[i:j].decode("utf-16le", "replace"))
                    i = j + 2
                while names and names[-1] == "":
                    names.pop()
                props["names"] = names
            elif t in (0x12, 0x13, 0x14):
                all_def = cur.byte()
                defined = [1] * n if all_def else cur.bits(n)
                cur.byte()
                props["mtime" if t == 0x14 else "time"] = [
                    cur.take(8) if d else None for d in defined]
            elif t == 0x15:
                all_def = cur.byte()
                defined = [1] * n if all_def else cur.bits(n)
                cur.byte()
                props["attrs"] = [cur.take(4) if d else None for d in defined]
            elif t == 0x18:
                all_def = cur.byte()
                defined = [1] * n if all_def else cur.bits(n)
                cur.byte()
                for d in defined:
                    if d:
                        cur.take(8)
            else:
                cur.take(size)
        return n, props

    def _read_header(self, cur: _Cursor):
        out = {"main": None, "files": None}
        while True:
            t = cur.byte()
            if t == 0x00:
                break
            if t == 0x02:
                while True:
                    pid = cur.num()
                    sz = cur.num()
                    if pid == 0:
                        break
                    cur.take(sz)
            elif t == 0x03:
                self._read_streams_info(cur)
            elif t == 0x04:
                out["main"] = self._read_streams_info(cur)
            elif t == 0x05:
                out["files"] = self._read_files_info(cur)
            else:
                raise ValueError("Header 未知字段 0x%02X" % t)
        return out

    # ---- 编码器图 ----
    def _lzma_filters(self, coder) -> list:
        props = coder["props"]
        if coder["id"] == "030101":
            b = props[0]
            lc = b % 9
            rest = b // 9
            lp = rest % 5
            pb = rest // 5
            return [{"id": lzma.FILTER_LZMA1, "dict_size": struct.unpack("<I", props[1:5])[0],
                     "lc": lc, "lp": lp, "pb": pb}]
        if coder["id"] == "21":
            return [{"id": lzma.FILTER_LZMA2,
                     "dict_size": struct.unpack("<I", props[1:5])[0]}]
        raise ValueError("不支持的压缩方法 %s" % coder["id"])

    def _raw_pack(self, si, folder, global_in):
        pi = folder["packed"].index(global_in)
        offset = self.base + si["packpos"] + sum(si["psizes"][:pi])
        return offset, si["psizes"][pi]

    def _reader_for_in(self, si, fidx, folder, bind_map, global_in):
        if global_in in bind_map:
            return self._reader_for_out(si, fidx, folder, bind_map, bind_map[global_in])
        offset, size = self._raw_pack(si, folder, global_in)
        return _SliceReader(self.path, offset, size)

    def _reader_for_out(self, si, fidx, folder, bind_map, out_idx):
        coder = next(c for c in folder["coders"]
                     if c["outb"] <= out_idx < c["outb"] + c["nout"])
        size = si["usizes"][fidx][out_idx]
        ins = [self._reader_for_in(si, fidx, folder, bind_map, coder["inb"] + j)
               for j in range(coder["nin"])]
        if coder["id"] in ("030101", "21"):
            return _LzmaReader(ins[0], self._lzma_filters(coder), size)
        if coder["id"] == "00":
            return ins[0]
        raise ValueError("不支持的编码器 %s" % coder["id"])

    def _folder_bytes(self, si, fidx: int) -> bytes:
        folder = si["folders"][fidx]
        bind_map = {i: o for (i, o) in folder["binds"]}
        used = set(bind_map.values())
        main_out = next(o for o in range(folder["tout"]) if o not in used)
        reader = self._reader_for_out(si, fidx, folder, bind_map, main_out)
        want = si["usizes"][fidx][main_out]
        chunks = []
        got = 0
        while got < want:
            block = reader.read(EMIT_CHUNK)
            if not block:
                break
            chunks.append(block)
            got += len(block)
        reader.close()
        data = b"".join(chunks)
        if len(data) != want:
            raise ValueError("头部流解压长度不符 (%d != %d)" % (len(data), want))
        return data

    # ---- 文件清单 ----
    def entries(self):
        main = self.header["main"]
        n, fi = self.header["files"]
        names = fi["names"]
        empty = fi.get("empty", [0] * n)
        attrs = fi.get("attrs", [None] * n)
        sizes: list[int] = []
        for group in main["ssizes"]:
            sizes.extend(group)
        out = []
        k = 0
        for i in range(n):
            attr = struct.unpack("<I", attrs[i])[0] if attrs[i] else 0
            if empty[i]:
                out.append(dict(name=names[i], size=0, dir=bool(attr & 0x10), attr=attr))
            else:
                out.append(dict(name=names[i], size=sizes[k], dir=False, attr=attr))
                k += 1
        if k != len(sizes):
            raise ValueError("文件数(%d)与子流数(%d)不符" % (k, len(sizes)))
        return out

    def crcs(self):
        main = self.header["main"]
        crcs: list = []
        for group in main["ssizes"]:
            crcs.extend([None] * len(group))
        if main.get("scrcs"):
            crcs = main["scrcs"]
        return crcs

    def extract(self, out_dir: str, progress=None, verify: bool = True):
        entries = self.entries()
        main = self.header["main"]
        crcs = self.crcs()
        os.makedirs(out_dir, exist_ok=True)

        for e in entries:
            path = os.path.join(out_dir, e["name"].replace("/", os.sep))
            if e["dir"]:
                os.makedirs(path, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(path) or out_dir, exist_ok=True)

        # 子流（有数据的文件）按清单顺序排列；空文件没有子流，不能占 CRC 位。
        payload = [e for e in entries if not e["dir"] and e["size"] > 0]
        for i, e in enumerate(payload):
            e["crc"] = crcs[i] if i < len(crcs) else None
        total = sum(e["size"] for e in payload) or 1

        st = dict(fh=None, cur=None, written=0, crc=0, done=0, bad=0, pos=0)

        def open_next():
            if st["pos"] >= len(payload):
                raise ValueError("7z 数据比文件清单长")
            e = payload[st["pos"]]
            st["pos"] += 1
            st["cur"] = e
            st["fh"] = open(os.path.join(out_dir, e["name"].replace("/", os.sep)), "wb")
            st["written"] = 0
            st["crc"] = 0

        def write(data: bytes) -> None:
            while data:
                if st["fh"] is None:
                    open_next()
                cur = st["cur"]
                take = min(cur["size"] - st["written"], len(data))
                st["fh"].write(data[:take])
                st["crc"] = zlib.crc32(data[:take], st["crc"])
                st["written"] += take
                st["done"] += take
                data = data[take:]
                if st["written"] >= cur["size"]:
                    st["fh"].close()
                    st["fh"] = None
                    if verify and cur["crc"] is not None and st["crc"] != cur["crc"]:
                        st["bad"] += 1
                        log("\r      CRC 不符: %s" % cur["name"])
                    if progress:
                        progress(st["done"], total)

        class _Sink:
            def write(self, data):
                write(data)

        sink = _Sink()

        for fidx, folder in enumerate(main["folders"]):
            bind_map = {i: o for (i, o) in folder["binds"]}
            used = set(bind_map.values())
            main_out = next(o for o in range(folder["tout"]) if o not in used)
            coder = next(c for c in folder["coders"]
                         if c["outb"] <= main_out < c["outb"] + c["nout"])
            want = main["usizes"][fidx][main_out]
            if coder["id"] == "0303011b":
                readers = [self._reader_for_in(main, fidx, folder, bind_map,
                                               coder["inb"] + j) for j in range(4)]
                got = _Bcj2Decoder(readers[0], readers[1], readers[2], readers[3],
                                   want, sink).run()
                for r in readers:
                    r.close()
            else:
                reader = self._reader_for_out(main, fidx, folder, bind_map, main_out)
                got = 0
                while got < want:
                    block = reader.read(EMIT_CHUNK)
                    if not block:
                        break
                    sink.write(block)
                    got += len(block)
                reader.close()
            if got != want:
                raise ValueError("解压长度不符: %d != %d" % (got, want))

        if st["fh"] is not None:
            st["fh"].close()
        if st["pos"] != len(payload):
            raise ValueError("有 %d 个文件没拿到数据" % (len(payload) - st["pos"]))
        return entries, st["bad"]

    def close(self) -> None:
        try:
            self.fh.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 定位内嵌 7z

def find_payload(path: str) -> tuple[int, int]:
    """在 PE 里找出体积最大、结构自洽的 7z 流。返回 (偏移, 长度)。"""
    size = os.path.getsize(path)
    best = None
    overlap = 8
    with open(path, "rb") as fh:
        offset = 0
        tail = b""
        while True:
            block = fh.read(1 << 23)
            if not block:
                break
            view = tail + block
            base = offset - len(tail)
            pos = 0
            while True:
                idx = view.find(SEVENZIP_MAGIC, pos)
                if idx < 0:
                    break
                pos = idx + 1
                at = base + idx
                fh.seek(at)
                head = fh.read(32)
                if len(head) < 32:
                    continue
                _m, _v, shcrc, nho, nhs, nhcrc = struct.unpack("<6sHIQQI", head)
                if zlib.crc32(head[12:32]) & 0xFFFFFFFF != shcrc:
                    continue
                total = 32 + nho + nhs
                if at + total > size:
                    continue
                fh.seek(at + 32 + nho)
                nh = fh.read(nhs)
                if zlib.crc32(nh) & 0xFFFFFFFF != nhcrc:
                    continue
                if best is None or total > best[1]:
                    best = (at, total)
            tail = view[-overlap:]
            offset += len(block)
    if best is None:
        raise ValueError("在 %s 里找不到 7z 数据流" % os.path.basename(path))
    return best


def flatten_files_dir(out_dir: str) -> bool:
    """把解出来的 Files\\ 内容上提一层，和官方安装器行为一致。"""
    inner = os.path.join(out_dir, "Files")
    if not os.path.isdir(inner):
        return False
    for name in os.listdir(inner):
        src = os.path.join(inner, name)
        dst = os.path.join(out_dir, name)
        if os.path.exists(dst):
            if os.path.isdir(dst) and os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
                shutil.rmtree(src, ignore_errors=True)
                continue
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            else:
                os.remove(dst)
        shutil.move(src, dst)
    try:
        os.rmdir(inner)
    except OSError:
        shutil.rmtree(inner, ignore_errors=True)
    return True


# ---------------------------------------------------------------- 主流程

def _runtime_fetch():
    """加载 ``qsick.runtime_fetch``，用于服务器源的下载与解包。

    不复制那份逻辑：它已经处理了 descriptor.json、Range 续传、sha256 校验和 tar.gz 布局，再写一份
    只会让两条路慢慢分叉。它就在同一个仓库里（``src/qsick``），按路径加进 sys.path 即可——源码树和
    打包产物（``src`` 紧跟 ``qqnt_get.py``）两种布局都适用。
    """
    here = Path(script_dir())
    for candidate in (here / "src", here.parent / "src"):
        if (candidate / "qsick" / "runtime_fetch.py").is_file():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            break
    try:
        from qsick import runtime_fetch  # noqa: PLC0415 - 故意延后到需要时
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "找不到 qsick.runtime_fetch，无法使用服务器源（需要 src/ 目录）"
        ) from exc
    return runtime_fetch


def fetch_from_server(runtime_dir: str) -> int:
    """用项目自己的发布服务器铺好 runtime\\。成功返回 0。"""
    log("")
    log("  转到服务器源：%s" % SERVER_BASE_URL)
    try:
        module = _runtime_fetch()
        module.ensure_runtime(Path(runtime_dir), base_url=SERVER_BASE_URL)
    except Exception as exc:                          # noqa: BLE001
        log("[错误] 服务器源也失败: %s" % exc)
        return 5
    if not module.runtime_ready(Path(runtime_dir)):
        log("[错误] 服务器包已解开，但 runtime\\ 仍不完整")
        return 5
    log("  服务器源完成")
    return 0


def main(argv=None) -> int:
    _init_console()
    here = script_dir()
    ap = argparse.ArgumentParser(
        description="下载 QQ NT 并解压到 runtime\\（纯标准库）")
    ap.add_argument("--dir", default=os.path.join(here, "runtime"),
                    help="解压目标目录，默认 <脚本目录>\\runtime")
    ap.add_argument("--source", default=None,
                    help="直接用本地安装包，不联网")
    ap.add_argument("--server", action="store_true",
                    help="跳过 GitHub，直接用项目发布服务器（获取QQnt.bat 用这个）")
    ap.add_argument("--github-only", action="store_true",
                    help="只走 GitHub，失败即退出，不转服务器源（供 runtime_fetch 编排用）")
    ap.add_argument("--force", action="store_true", help="强制重新下载")
    ap.add_argument("--no-verify", action="store_true", help="跳过 SHA256 校验")
    ap.add_argument("--no-flatten", action="store_true", help="不把 Files\\ 内容上提")
    ap.add_argument("--clean", action="store_true", help="解压前清空目标目录")
    ap.add_argument("--delete-source", action="store_true",
                    help="解压完成后删除安装包（可省 300MB）")
    args = ap.parse_args(argv)

    # 只走服务器源：不碰 GitHub，也就不需要安装包与那套 PE+7z 解包。
    if args.server:
        log("=" * 62)
        log(" 获取 QQ NT（服务器源）")
        log("=" * 62)
        return fetch_from_server(os.path.abspath(args.dir))

    log("=" * 62)
    log(" 获取 QQ NT   (%s)" % INSTALLER_NAME)
    log("=" * 62)

    out_dir = os.path.abspath(args.dir)
    if args.source:
        exe = os.path.abspath(args.source)
        if not os.path.isfile(exe):
            log("[错误] 找不到 %s" % exe)
            return 2
        log("[1/4] 使用本地安装包: %s" % exe)
        log("      %s" % human(os.path.getsize(exe)))
    else:
        exe = os.path.join(here, INSTALLER_NAME)
        log("[1/4] 下载安装包")
        log("      目标: %s" % exe)
        if os.path.exists(exe) and not args.force:
            got = os.path.getsize(exe)
            log("      已存在 (%s)，跳过下载；用 --force 可重新下载" % human(got))
        else:
            if os.path.exists(exe):
                os.remove(exe)
            try:
                used = download_installer(exe)
                log("      来源: %s" % used)
            except Exception as exc:                  # noqa: BLE001
                log("[提示] %s" % exc)
                if args.github_only:
                    # 上层（runtime_fetch.ensure_runtime）自己排了源顺序，这里不该替它做决定。
                    return 3
                # GitHub 5 秒内没连上（或中途失败）就换自己的服务器。
                # 服务器给的是项目压好的 tar.gz，不是安装包，所以这一步不再往下走校验/解包，
                # 直接交出整个流程。
                code = fetch_from_server(out_dir)
                if code == 0:
                    log("")
                    log("[完成] runtime\\ 已就绪：%s" % out_dir)
                return code

    log("[2/4] 校验 SHA256")
    if args.no_verify:
        log("      已跳过")
    else:
        digest = sha256_file(exe)
        if digest == EXPECTED_SHA256:
            log("      OK  %s" % digest)
        elif args.source:
            log("      期望 %s" % EXPECTED_SHA256)
            log("      实际 %s" % digest)
            log("[错误] 本地安装包校验失败，可能不完整或已被改动")
            return 4
        else:
            # 常见于上次下载中断：文件在，但内容不全，这里自动补一次再验。
            log("      实际 %s" % digest)
            log("      与期望值不符（很可能是上次没下完），重新下载一次")
            try:
                used = download_installer(exe)
                log("      来源: %s" % used)
            except Exception as exc:                  # noqa: BLE001
                log("[提示] %s" % exc)
                if args.github_only:
                    return 3
                code = fetch_from_server(out_dir)
                if code == 0:
                    log("")
                    log("[完成] runtime\\ 已就绪：%s" % out_dir)
                return code
            digest = sha256_file(exe)
            if digest != EXPECTED_SHA256:
                log("      期望 %s" % EXPECTED_SHA256)
                log("      实际 %s" % digest)
                log("[错误] 安装包校验失败，可能下载不完整或已被改动")
                return 4
            log("      OK  %s" % digest)

    log("[3/4] 定位并解析内嵌 7z 数据流")
    try:
        offset, length = find_payload(exe)
    except Exception as exc:                          # noqa: BLE001
        log("[错误] %s" % exc)
        return 5
    log("      偏移 0x%X，长度 %s" % (offset, human(length)))
    try:
        archive = SevenZip(exe, offset)
    except Exception as exc:                          # noqa: BLE001
        log("[错误] 7z 头解析失败: %s" % exc)
        return 6
    entries = archive.entries()
    total = sum(e["size"] for e in entries)
    log("      文件 %d 个，解开后 %s" % (len(entries), human(total)))
    for folder in archive.header["main"]["folders"]:
        method = " + ".join(dict.fromkeys(
            {"030101": "LZMA", "21": "LZMA2", "00": "Copy",
             "0303011b": "BCJ2"}.get(c["id"], c["id"]) for c in folder["coders"]))
        log("      压缩方式: %s" % method)

    if args.clean and os.path.isdir(out_dir):
        log("      清空 %s" % out_dir)
        shutil.rmtree(out_dir, ignore_errors=True)

    log("[4/4] 解压到 %s" % out_dir)
    t0 = time.time()
    last = [0.0]

    def progress(done, want):
        now = time.time()
        if now - last[0] < 0.25 and done < want:
            return
        last[0] = now
        pct = done * 100.0 / want
        log("\r      %5.1f%%  %s / %s" % (pct, human(done), human(want)))

    try:
        _entries, bad = archive.extract(out_dir, progress=progress)
    except Exception as exc:                          # noqa: BLE001
        log("")
        log("[错误] 解压失败: %s: %s" % (type(exc).__name__, exc))
        return 7
    finally:
        archive.close()
    log("")
    log("      耗时 %.1f 秒，CRC 校验不符 %d 个" % (time.time() - t0, bad))

    if not args.no_flatten:
        if flatten_files_dir(out_dir):
            log("      已把 Files\\ 内容上提到 %s" % out_dir)

    qq = os.path.join(out_dir, "QQ.exe")
    versions = os.path.join(out_dir, "versions")
    log("-" * 62)
    if os.path.isfile(qq):
        log("完成: %s (%s)" % (qq, human(os.path.getsize(qq))))
    else:
        log("[警告] 目标目录里没有 QQ.exe，请检查解压结果")
    if os.path.isdir(versions):
        for name in sorted(os.listdir(versions)):
            if os.path.isdir(os.path.join(versions, name)):
                log("      版本目录: versions\\%s" % name)

    if args.delete_source and not args.source and os.path.isfile(exe):
        try:
            os.remove(exe)
            log("      已删除安装包 %s" % os.path.basename(exe))
        except OSError as exc:
            log("      [警告] 删除安装包失败: %s" % exc)
    log("=" * 62)
    return 0 if bad == 0 and os.path.isfile(qq) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\n已取消")
        sys.exit(130)
