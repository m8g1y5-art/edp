"""读视频 / 图片序列、按时间取帧、调用 ffmpeg 编码预览。"""
import os
import subprocess
from pathlib import Path

import cv2
import numpy as np

try:
    cv2.setLogLevel(2)  # 只显示错误：16 位深度视频每次打开都会打一条“BGR conversion turned OFF”警告
except Exception:
    pass

IMG_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".webm", ".mpg", ".mpeg"}


def imread(p, flags=cv2.IMREAD_UNCHANGED):
    """支持中文路径的读图"""
    data = np.fromfile(str(p), dtype=np.uint8)
    return cv2.imdecode(data, flags)


def imwrite(p, img, ext=".jpg", q=85):
    ok, buf = cv2.imencode(ext, img, [cv2.IMWRITE_JPEG_QUALITY, q] if ext == ".jpg" else [])
    if ok:
        buf.tofile(str(p))


def _short_path(p):
    """Windows 上 OpenCV 打不开含中文的视频路径时，退回 8.3 短路径"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 1024):
            return buf.value
    except Exception:
        pass
    return str(p)


def open_video(p, raw=False):
    """raw=True：不转成 8 位彩色，按原始像素格式返回（16 位深度视频 gray16le 读出来就是 uint16 毫米值）"""
    args = (cv2.CAP_FFMPEG, [cv2.CAP_PROP_CONVERT_RGB, 0]) if raw else ()
    cap = cv2.VideoCapture(str(p), *args)
    if not cap.isOpened() and os.name == "nt" and not str(p).isascii():
        cap = cv2.VideoCapture(_short_path(p), *args)
    return cap


def is_depth16(p):
    """视频是不是无损 16 位深度（如 ffv1 gray16le 的 .mkv）。普通方式读会被转成 8 位彩色，真实距离就丢了"""
    try:
        cap = open_video(p, raw=True)
        ok, fr = cap.read()
        cap.release()
        return bool(ok and fr is not None and fr.dtype == np.uint16 and fr.ndim == 2)
    except Exception:
        return False


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


class Stream:
    """统一的帧来源。kind: rgb / depth；src: video / seq。
    ts：每帧时间戳（秒，可能是绝对时间），没有就按帧率推算。"""

    def __init__(self, d):
        self.d = d
        self.kind = d["kind"]
        self.src = d["src"]
        self.path = Path(d["path"])
        self.files = d.get("files")  # seq 的文件名列表（相对 path）
        self.n = d["n"]
        self.fps = d.get("fps") or 30.0
        self._cap = None
        self._pos = -1
        self._last = None
        ts = d.get("ts")
        self.ts = np.asarray(ts, dtype=float) if ts is not None and len(ts) == self.n else np.arange(self.n) / self.fps

    def t_rel(self):
        return self.ts - self.ts[0]

    def index_at(self, t_abs):
        i = int(np.searchsorted(self.ts, t_abs))
        i = min(max(i, 0), self.n - 1)
        if i > 0 and abs(self.ts[i - 1] - t_abs) < abs(self.ts[i] - t_abs):
            i -= 1
        return i

    def read(self, i):
        i = int(min(max(i, 0), self.n - 1))
        if self.src == "seq":
            return imread(self.path / self.files[i])
        if self._cap is None:
            self._open()
        if i == self._pos and self._last is not None:
            return self._last
        if self._pos < i <= self._pos + 300:  # 往前不远：顺序解码比跳转快得多（跳转要从关键帧重解）
            for _ in range(i - self._pos - 1):
                self._cap.grab()
        elif i != self._pos + 1:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, fr = self._cap.read()
        self._pos = i if ok else -1
        self._last = fr if ok else None
        return self._last

    def _open(self):
        """深度视频先试原始 16 位读法；读出来是 uint16 就一直用它，否则退回普通读法"""
        self._pos, self._last = -1, None
        if self.kind == "depth" and self.d.get("dtype") in (None, "uint16"):
            cap = open_video(self.path, raw=True)
            ok, fr = cap.read()
            if ok and fr is not None and fr.dtype == np.uint16 and fr.ndim == 2:
                self.d["dtype"] = "uint16"
                self._cap, self._pos, self._last = cap, 0, fr
                return
            cap.release()
        self._cap = open_video(self.path)

    def metric(self):
        """有没有真实距离（毫米 uint16 或米 float）。8 位深度视频只有相对值"""
        if self.src == "video" and self._cap is None:
            self._open()
        if self.src == "seq" and "dtype" not in self.d:
            img = self.read(0)
            self.d["dtype"] = str(img.dtype) if img is not None else None
        return self.d.get("dtype") in ("uint16", "float32", "float64")

    def close(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None


def probe_video(p):
    cap = open_video(p)
    if not cap.isOpened():
        return None
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0) or 30.0
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cc = int(cap.get(cv2.CAP_PROP_FOURCC))
    codec = "".join(chr((cc >> 8 * i) & 0xFF) for i in range(4)).strip("\x00").lower()
    ok, fr = cap.read()
    cap.release()
    if not ok:
        return {"n": n, "fps": fps, "w": w, "h": h, "codec": codec, "ok": False}
    chroma = _chroma(fr)
    return {"n": n, "fps": fps, "w": w, "h": h, "codec": codec, "ok": True, "gray": chroma < 3.0}


def _chroma(fr):
    if fr is None or fr.ndim == 2:
        return 0.0
    lab = cv2.cvtColor(fr, cv2.COLOR_BGR2LAB).astype(np.int16)
    return float((np.abs(lab[..., 1] - 128) + np.abs(lab[..., 2] - 128)).mean())


def depth_to_m(img):
    """深度图转米。uint16 默认毫米；float 默认已是米。8 位彩色/灰度深度视频只能给相对值。"""
    if img is None:
        return None
    if img.ndim == 3:
        img = img[..., 0]
    if img.dtype == np.uint16:
        d = img.astype(np.float32) / 1000.0
        d[img >= 65000] = 0
        return d
    if img.dtype in (np.float32, np.float64):
        return img.astype(np.float32)
    return img.astype(np.float32) / 255.0  # 相对值


def colorize_depth(d_m, lo=0.2, hi=2.0):
    v = np.clip((d_m - lo) / max(hi - lo, 1e-6), 0, 1)
    c = cv2.applyColorMap((255 - v * 255).astype(np.uint8), cv2.COLORMAP_TURBO)
    c[d_m <= 0] = (30, 30, 30)
    return c


def small_gray(fr, w=160):
    """运动能量用的小灰度图"""
    if fr is None:
        return None
    if fr.ndim == 3:
        fr = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
    h = int(fr.shape[0] * w / fr.shape[1])
    return cv2.resize(fr.astype(np.float32), (w, h), interpolation=cv2.INTER_AREA)


class Encoder:
    """把一帧帧图像编码成浏览器能播放的 H.264 mp4"""

    def __init__(self, out, w, h, fps):
        self.w, self.h = w - w % 2, h - h % 2
        self.p = subprocess.Popen(
            [ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{self.w}x{self.h}",
             "-r", f"{fps:.4f}", "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NOWIN)

    def write(self, img):
        if img.shape[1] != self.w or img.shape[0] != self.h:
            img = cv2.resize(img, (self.w, self.h))
        self.p.stdin.write(np.ascontiguousarray(img).tobytes())

    def close(self):
        self.p.stdin.close()
        err = self.p.stderr.read().decode(errors="ignore")
        self.p.wait()
        if self.p.returncode != 0:
            raise RuntimeError("ffmpeg 编码失败：" + err[-300:])


_NOWIN = 0x08000000 if hasattr(subprocess, "STARTUPINFO") else 0  # Windows 上不弹黑窗


def transcode(src, out, max_w=1280):
    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(src), "-an", "-vf", f"scale='min({max_w},iw)':-2",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, creationflags=_NOWIN)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg 转码失败：" + r.stderr.decode(errors="ignore")[-300:])
