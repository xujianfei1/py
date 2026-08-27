"""
本地视频播放器 - 播放缓存的抖音视频 (PySide6, 现代深色 UI)

依赖: PySide6
启动: python video_player.py [视频目录]   (默认 E:/抖音)
打包: pyinstaller --onefile --noconsole --name video_player video_player.py

功能:
  - 扫描目录下所有 mp4/flv/webm/mov/m4v 视频, 双击列表播放, 单击画面暂停/继续
  - 外挂音轨/字幕: 控制栏 "音轨/字幕" 按钮选择, 同名文件自动挂载
    音轨用第二播放器与画面对时同步; 字幕支持 SRT/ASS/SSA (自动识别编码)
  - 列表带视频缩略图 (后台懒生成 + 磁盘缓存), 新看/未看有标记:
    看过的封面变暗 + 对勾徽章, 封面底部显示观看进度条
  - 观看历史: 侧栏 "视频库 / 历史" 切换, 按时间倒序, 可清空
  - 控制栏悬浮在画面上自动隐藏; 断点续播; 播完自动连播
  - 记住目录、音量、倍速、上次播放的视频、每个视频的观看进度

快捷键:
  空格 播放/暂停   ←/→ 快退/快进5秒   ↑/↓ 音量
  Ctrl+←/→ 上一个/下一个   F 全屏   M 静音   Esc 退出全屏
"""

import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
from bisect import bisect_right
from datetime import datetime
from pathlib import Path

# QVideoWidget 的 D3D 合成在部分显卡上黑屏, 故固定用 FFmpeg 后端 + QVideoSink 软件自绘
os.environ.setdefault('QT_MEDIA_BACKEND', 'ffmpeg')

from PySide6.QtCore import (QByteArray, QEventLoop, QObject, QPoint,
                            QProcess, QPropertyAnimation, QRect, QRectF,
                            QMutex, QSize, Qt, QThread, QTimer, QUrl,
                            QVariantAnimation, Signal)
from PySide6.QtGui import (QAction, QActionGroup, QColor, QFont, QIcon,
                           QImage, QPainter, QPainterPath, QPixmap, QPen,
                           QShortcut, QKeySequence)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QGraphicsOpacityEffect, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QPushButton, QSlider, QSplitter, QStackedWidget, QStyle,
    QStyledItemDelegate, QVBoxLayout, QWidget,
)

# PyInstaller 打包后 __file__ 在临时解压目录, 配置/缓存须跟随 exe 所在位置
if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / 'video_player_config.json'
HISTORY_FILE = APP_DIR / 'video_player_history.json'
THUMB_DIR = APP_DIR / 'thumb_cache'


def _find_tool(name):
    """定位 ffmpeg/ffprobe: PATH -> winget 安装位置"""
    p = shutil.which(name)
    if p:
        return p
    import glob
    pat = str(Path.home() / 'AppData/Local/Microsoft/WinGet/Packages/'
              f'Gyan.FFmpeg*/ffmpeg-*/bin/{name}.exe')
    g = sorted(glob.glob(pat))
    return g[-1] if g else None


FFMPEG = _find_tool('ffmpeg')
FFPROBE = _find_tool('ffprobe')
CACHE_DIR = Path(os.environ.get('LOCALAPPDATA', str(APP_DIR))) / 'VideoPlayerCache'
DEFAULT_DIR = r'E:\抖音'
VIDEO_EXTS = {'.mp4', '.mkv', '.flv', '.webm', '.mov', '.m4v', '.avi',
              '.ts', '.m2ts', '.wmv', '.mpg', '.mpeg', '.rmvb', '.rm'}
AUDIO_EXTS = {'.mka', '.m4a', '.aac', '.ac3', '.eac3', '.dts', '.mp3',
              '.flac', '.wav', '.opus', '.ogg', '.wma', '.thd'}
SUB_EXTS = {'.srt', '.ass', '.ssa'}
SPEEDS = ['0.5×', '0.75×', '1.0×', '1.25×', '1.5×', '2.0×', '3.0×']
WATCHED_RATIO = 0.92     # 播放超过 92% 视为已看完
HISTORY_MAX = 500
ROLE_PATH = Qt.UserRole
ROLE_TIME = Qt.UserRole + 2
ROLE_TITLE = Qt.UserRole + 3
ROLE_AUTHOR = Qt.UserRole + 4

# ---------------- 配色 (抖音品牌色: 红 + 青, 呼应视频库内容) ----------------
BG = '#0a0d14'        # 窗口底
SIDEBAR = '#0d1119'   # 侧栏
CARD = '#151b28'      # 输入框/悬浮面
BORDER = '#1f2937'
HOVER = '#1b2433'
TEXT = '#e8edf4'
MUTED = '#8494ab'
ACCENT = '#fe2c55'     # 抖音红
ACCENT2 = '#25f4ee'    # 抖音青
ACCENT_DIM = '#c2183f'
ICON_FG = '#c7d2e2'

STYLE = f"""
QMainWindow, QWidget {{ background: {BG}; color: {TEXT}; font-size: 13px; }}
QSplitter::handle {{ background: {BG}; width: 1px; }}

QLineEdit {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 9px;
    padding: 8px 12px; color: {TEXT}; selection-background-color: {ACCENT_DIM};
}}
QLineEdit:focus {{ border-color: {ACCENT_DIM}; }}

QListWidget {{ background: {SIDEBAR}; border: none; outline: none; }}
QScrollBar:vertical {{
    background: transparent; width: 8px; margin: 4px 2px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {MUTED}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}

QToolTip {{
    background: {CARD}; color: {TEXT}; border: 1px solid {BORDER};
    padding: 4px 8px; border-radius: 6px;
}}

QPushButton {{ background: transparent; border: none; border-radius: 8px; }}
QPushButton:hover {{ background: {HOVER}; }}

QComboBox {{
    background: rgba(15, 20, 30, 190); color: {TEXT};
    border: 1px solid rgba(255, 255, 255, 45); border-radius: 8px;
    padding: 4px 8px;
}}
QComboBox:hover {{ background: rgba(30, 40, 58, 210); }}
QComboBox::drop-down {{ border: none; width: 16px; }}
QComboBox QAbstractItemView {{
    background: {CARD}; color: {TEXT}; border: 1px solid {BORDER};
    selection-background-color: {ACCENT_DIM}; outline: none;
}}
"""


# ---------------- SVG 图标 ----------------
_SVGS = {
    'play': '<path fill="{c}" d="M8.3 5.1v13.8c0 .9.98 1.44 1.73.96l10.7-6.9a1.15 1.15 0 0 0 0-1.92L10.03 4.14c-.75-.48-1.73.05-1.73.96z"/>',
    'pause': '<rect fill="{c}" x="6.6" y="5" width="3.7" height="14" rx="1.3"/><rect fill="{c}" x="13.7" y="5" width="3.7" height="14" rx="1.3"/>',
    'prev': '<rect fill="{c}" x="5.6" y="5.4" width="2.5" height="13.2" rx="1.2"/><path fill="{c}" d="M18.6 6.1v11.8c0 .94-1.07 1.47-1.8.9l-7.4-5.9a1.14 1.14 0 0 1 0-1.8l7.4-5.9c.73-.57 1.8-.04 1.8.9z"/>',
    'next': '<rect fill="{c}" x="15.9" y="5.4" width="2.5" height="13.2" rx="1.2"/><path fill="{c}" d="M5.4 6.1v11.8c0 .94 1.07 1.47 1.8.9l7.4-5.9a1.14 1.14 0 0 0 0-1.8L7.2 5.2c-.73-.57-1.8-.04-1.8.9z"/>',
    'volume': '<path fill="{c}" d="M4.5 9.3v5.4c0 .5.4.9.9.9h2.9l4 3.9c.6.57 1.6.15 1.6-.7V5.2c0-.85-1-1.27-1.6-.7l-4 3.9H5.4a.9.9 0 0 0-.9.9z"/><path stroke="{c}" stroke-width="2" stroke-linecap="round" fill="none" d="M16.8 9.2a4.2 4.2 0 0 1 0 5.6M19.4 6.8a7.6 7.6 0 0 1 0 10.4"/>',
    'volume-low': '<path fill="{c}" d="M4.5 9.3v5.4c0 .5.4.9.9.9h2.9l4 3.9c.6.57 1.6.15 1.6-.7V5.2c0-.85-1-1.27-1.6-.7l-4 3.9H5.4a.9.9 0 0 0-.9.9z"/><path stroke="{c}" stroke-width="2" stroke-linecap="round" fill="none" d="M16.8 9.2a4.2 4.2 0 0 1 0 5.6"/>',
    'mute': '<path fill="{c}" d="M4.5 9.3v5.4c0 .5.4.9.9.9h2.9l4 3.9c.6.57 1.6.15 1.6-.7V5.2c0-.85-1-1.27-1.6-.7l-4 3.9H5.4a.9.9 0 0 0-.9.9z"/><path stroke="{c}" stroke-width="2" stroke-linecap="round" fill="none" d="m16.4 9.6 5 4.8m0-4.8-5 4.8"/>',
    'fullscreen': '<path stroke="{c}" stroke-width="2.1" stroke-linecap="round" fill="none" d="M4 9.2V6a2 2 0 0 1 2-2h3.2M14.8 4H18a2 2 0 0 1 2 2v3.2M20 14.8V18a2 2 0 0 1-2 2h-3.2M9.2 20H6a2 2 0 0 1-2-2v-3.2"/>',
    'fullscreen-exit': '<path stroke="{c}" stroke-width="2.1" stroke-linecap="round" fill="none" d="M9.2 4v3.2a2 2 0 0 1-2 2H4M20 9.2h-3.2a2 2 0 0 1-2-2V4M14.8 20v-3.2a2 2 0 0 1 2-2H20M4 14.8h3.2a2 2 0 0 1 2 2V20"/>',
    'folder': '<path fill="{c}" d="M3.5 6.3c0-1 .8-1.8 1.8-1.8h4.1c.5 0 1 .22 1.33.6l1.2 1.4h7.77c1 0 1.8.8 1.8 1.8v8.9c0 1.5-1.2 2.7-2.7 2.7H6.2c-1.5 0-2.7-1.2-2.7-2.7z"/>',
    'search': '<circle stroke="{c}" stroke-width="2.1" fill="none" cx="11" cy="11" r="6.4"/><path stroke="{c}" stroke-width="2.1" stroke-linecap="round" d="m16.3 16.3 4 4"/>',
    'film': '<rect stroke="{c}" stroke-width="1.8" fill="none" x="4" y="5.5" width="16" height="13" rx="2"/><path stroke="{c}" stroke-width="1.8" d="M8 5.5v13M16 5.5v13M4 12h16M4 8.7h4M4 15.3h4M16 8.7h4M16 15.3h4"/>',
    'library': '<path fill="{c}" d="M4 4.5A1.5 1.5 0 0 1 5.5 3H9a1.5 1.5 0 0 1 1.5 1.5V8A1.5 1.5 0 0 1 9 9.5H5.5A1.5 1.5 0 0 1 4 8zM13.5 3H17a1.5 1.5 0 0 1 1.5 1.5V8A1.5 1.5 0 0 1 17 9.5h-3.5A1.5 1.5 0 0 1 12 8V4.5A1.5 1.5 0 0 1 13.5 3zM4 12.5A1.5 1.5 0 0 1 5.5 11H9a1.5 1.5 0 0 1 1.5 1.5V17A1.5 1.5 0 0 1 9 18.5H5.5A1.5 1.5 0 0 1 4 17zM13.5 11H17a1.5 1.5 0 0 1 1.5 1.5V17A1.5 1.5 0 0 1 17 18.5h-3.5A1.5 1.5 0 0 1 12 17v-5.5A1.5 1.5 0 0 1 13.5 11z"/>',
    'history': '<path fill="none" stroke="{c}" stroke-width="2.1" stroke-linecap="round" d="M3.5 12a8.5 8.5 0 1 0 2.5-6L3.7 8.2"/><path fill="{c}" d="M3.2 3.6v4.8H8z"/><path fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" d="M12 8v4.5l3 1.8"/>',
    'trash': '<path fill="none" stroke="{c}" stroke-width="2" stroke-linecap="round" d="M4.5 6.5h15M9.5 6V4.8c0-.7.6-1.3 1.3-1.3h2.4c.7 0 1.3.6 1.3 1.3V6M6.5 6.5l.8 11.2c.06.9.8 1.6 1.7 1.6h6c.9 0 1.64-.7 1.7-1.6l.8-11.2M10 10.5v5M14 10.5v5"/>',
    'track': '<circle fill="{c}" cx="7" cy="17" r="2.7"/><circle fill="{c}" cx="17" cy="15.5" r="2.7"/><path stroke="{c}" stroke-width="2.2" fill="none" d="M9.7 17V6.8l9.6-2.1v10.8"/>',
    'cc': '<rect fill="none" stroke="{c}" stroke-width="2" x="3" y="5" width="18" height="14" rx="2.5"/><path stroke="{c}" stroke-width="2" stroke-linecap="round" d="M6.5 10.5h11M6.5 14.5h6"/>',
}


def make_icon(name, color=ICON_FG, size=64):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
           + _SVGS[name].format(c=color) + '</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    renderer.render(p)
    p.end()
    return QIcon(pm)


def icon_pixmap(name, color=ICON_FG, size=22):
    return make_icon(name, color, size * 3).pixmap(size, size)


def enable_dark_title_bar(win):
    """Windows 10 19041+ / 11: 系统标题栏切深色"""
    try:
        hwnd = int(win.winId())
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE 新旧取值
            val = ctypes.c_int(1)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), 4) == 0:
                break
    except Exception:
        pass


def parse_name(path):
    """文件名格式: {id}_{作者}_{标题}.mp4 -> (作者, 标题)"""
    parts = Path(path).stem.split('_', 2)
    if len(parts) == 3:
        return parts[1], parts[2]
    return '', Path(path).stem


def fmt_time(ms):
    s = round(ms / 1000)
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f'{h}:{m:02d}:{sec:02d}' if h else f'{m:02d}:{sec:02d}'


def fmt_ts(t):
    dt = datetime.fromtimestamp(t)
    now = datetime.now()
    days = (now.date() - dt.date()).days
    if days <= 0:
        return dt.strftime('%H:%M')
    if days == 1:
        return '昨天'
    if days < 7:
        return f'{days}天前'
    return dt.strftime('%m-%d')


# ---------------- 字幕解析 (SRT / ASS / SSA) ----------------
def _ts_srt(t):
    t = t.replace(',', '.')
    h, m, rest = t.split(':')
    s, ms = rest.split('.')
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms.ljust(3, '0'))


def _ts_ass(t):
    h, m, rest = t.split(':')
    s, cs = rest.split('.')
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(cs.ljust(2, '0')) * 10


def parse_subtitle(path):
    """解析字幕文件 -> [(开始ms, 结束ms, 文本)], 兼容 UTF-8 / GBK"""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return []
    text = None
    for enc in ('utf-8-sig', 'utf-8', 'gb18030'):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, ValueError):
            continue
    if text is None:
        return []
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    ext = Path(path).suffix.lower()
    cues = []
    if ext == '.srt':
        for block in re.split(r'\n\s*\n', text):
            lines = [l for l in block.split('\n') if l.strip()]
            if not lines:
                continue
            head_i = 0 if '-->' in lines[0] else (1 if len(lines) > 1 else -1)
            if head_i < 0:
                continue
            m = re.search(
                r'(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*'
                r'(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})', lines[head_i])
            if not m:
                continue
            try:
                s, e = _ts_srt(m.group(1)), _ts_srt(m.group(2))
            except ValueError:
                continue
            body = '\n'.join(lines[head_i + 1:]).strip()
            if body:
                cues.append((s, e, body))
    else:  # ass / ssa
        in_events = False
        text_idx = None
        for line in text.split('\n'):
            ln = line.strip()
            low = ln.lower()
            if low.startswith('['):
                in_events = low.startswith('[events]')
                continue
            if not in_events:
                continue
            if low.startswith('format:'):
                fields = [x.strip().lower() for x in ln[7:].split(',')]
                text_idx = fields.index('text') if 'text' in fields else None
                continue
            if not low.startswith('dialogue:'):
                continue
            body = ln[9:].strip()
            keep = text_idx if text_idx is not None else 9
            parts = body.split(',', keep) if keep > 0 else [body]
            if len(parts) < 4:
                continue
            try:
                s, e = _ts_ass(parts[1]), _ts_ass(parts[2])
            except (ValueError, IndexError):
                continue
            txt = parts[-1]
            txt = re.sub(r'\{[^}]*\}', '', txt)          # 去掉 {\...} 特效标签
            txt = txt.replace('\\N', '\n').replace('\\n', '\n')
            txt = txt.replace('\\h', ' ').strip().lstrip(',').strip()
            if txt:
                cues.append((s, e, txt))
    cues.sort(key=lambda c: c[0])
    return cues


class Fader(QObject):
    """控件透明度淡入淡出; 淡出结束后自动 hide"""

    def __init__(self, widget):
        super().__init__(widget)
        self.widget = widget
        self.eff = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(self.eff)
        self.anim = QPropertyAnimation(self.eff, b'opacity', widget)
        self.anim.setDuration(200)
        self.anim.finished.connect(self._on_finished)

    def to(self, value):
        if value > 0 and not self.widget.isVisible():
            self.widget.show()
            self.eff.setOpacity(0.0)
        self.anim.stop()
        self.anim.setStartValue(self.eff.opacity())
        self.anim.setEndValue(value)
        self.anim.start()

    def _on_finished(self):
        if self.eff.opacity() < 0.02:
            self.widget.hide()


class FfmpegJob(QProcess):
    """后台 ffmpeg 任务: 解析 stderr 里的 time= 汇报进度"""
    progress = Signal(float)          # 0.0 ~ 1.0
    done = Signal(bool, str)          # 成功与否, 输出路径

    def __init__(self, args, total_ms, out_path, tag, parent=None):
        super().__init__(parent)
        self._total = max(1, int(total_ms))
        self._out = out_path
        self.tag = tag
        self.err_tail = ''
        self.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.readyReadStandardOutput.connect(self._on_out)
        self.finished.connect(self._on_finished)
        # args[0] 是 ffmpeg 程序路径; start(program, 其余参数) 不能重复传
        self.start(args[0], args[1:])

    def _on_out(self):
        data = bytes(self.readAllStandardOutput()).decode('utf-8', 'ignore')
        if 'Error' in data or 'error' in data or 'Invalid' in data:
            self.err_tail = (self.err_tail + data)[-800:]
        m = re.findall(r'time=(\d+):(\d+):(\d+(?:\.\d+)?)', data)
        if m:
            h, mi, s = m[-1]
            ms = (int(h) * 3600 + int(mi) * 60 + float(s)) * 1000
            self.progress.emit(min(1.0, ms / self._total))

    def _on_finished(self, code, _status):
        try:
            ok = code == 0 and Path(self._out).stat().st_size > 0
        except OSError:
            ok = False
        if not ok:
            print(f'[ffmpeg:{self.tag}] 失败 exit={code} '
                  f'err={self.err_tail[-300:]}')
        self.done.emit(ok, self._out)


# ---------------- 缩略图后台生成 (按文件路径索引, 各列表共享) ----------------
class ThumbWorker(QThread):
    """逐个抓取视频开头处的画面帧; 队列可按可见项优先重排"""
    thumb_ready = Signal(str, QImage)   # path, image

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mutex = QMutex()
        self._queue = []          # [path_str]
        self._stopped = False

    def add(self, path):
        self._mutex.lock()
        if path not in self._queue:
            self._queue.append(path)
        self._mutex.unlock()

    def prioritize(self, visible_paths):
        self._mutex.lock()
        self._queue.sort(key=lambda pth: 0 if pth in visible_paths else 1)
        self._mutex.unlock()

    def clear(self):
        self._mutex.lock()
        self._queue = []
        self._mutex.unlock()

    def stop(self):
        self._stopped = True
        self.clear()

    def _grab(self, path):
        """独立事件循环内完成: 载入 -> 播放跳过片头黑帧 -> 抓一帧 (超时5s放弃)"""
        result = None
        loop = QEventLoop()
        state = {'got': False, 'pos': 0}

        player = QMediaPlayer()
        audio = QAudioOutput()
        audio.setVolume(0)
        player.setAudioOutput(audio)
        sink = QVideoSink()
        player.setVideoSink(sink)

        def on_frame(frame):
            nonlocal result
            if state['got'] or not frame.isValid() or frame.width() == 0:
                return
            dur = player.duration()
            early = 0 < dur < 1500 and state['pos'] > 200
            if state['pos'] >= 500 or early:
                state['got'] = True
                result = frame.toImage()
                loop.quit()

        def on_status(st):
            if st == QMediaPlayer.LoadedMedia and not state['got']:
                player.play()

        sink.videoFrameChanged.connect(on_frame)
        player.positionChanged.connect(lambda pos: state.update(pos=pos))
        player.mediaStatusChanged.connect(on_status)
        QTimer.singleShot(5000, loop.quit)

        player.setSource(QUrl.fromLocalFile(path))
        loop.exec()

        player.stop()
        player.deleteLater()
        audio.deleteLater()
        sink.deleteLater()
        return result

    def run(self):
        while not self._stopped:
            self._mutex.lock()
            path = self._queue.pop(0) if self._queue else None
            self._mutex.unlock()
            if path is None:
                self.msleep(120)
                continue
            img = self._grab(path)
            if self._stopped:
                break
            if img is not None:
                self.thumb_ready.emit(path, img)


class ThumbStore:
    """缩略图缓存: 内存 + 磁盘, 以文件路径为键"""

    def __init__(self, worker, on_update):
        self.worker = worker
        self.on_update = on_update
        self.cache = {}       # path -> QPixmap | None(生成中)
        self.fade = {}        # path -> 淡入进度
        worker.thumb_ready.connect(self._on_ready)

    def _disk_path(self, path):
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = 0
        h = hashlib.md5(f'{path}|{size}'.encode()).hexdigest()
        return THUMB_DIR / f'{h}.jpg'

    def get(self, path):
        pm = self.cache.get(path)
        if pm is not None:
            return pm
        disk = self._disk_path(path)
        if disk.exists():
            img = QImage(str(disk))
            if not img.isNull():
                pm = QPixmap.fromImage(img)
                self.cache[path] = pm
                return pm
        if path not in self.cache:
            self.cache[path] = None
            self.worker.add(path)
        return None

    def _on_ready(self, path, img):
        # 统一裁成 134x76@2x 的封面比例, 横竖屏都美观
        img2 = img.scaled(268, 152, Qt.KeepAspectRatioByExpanding,
                          Qt.SmoothTransformation)
        x = (img2.width() - 268) // 2
        y = (img2.height() - 152) // 2
        cropped = img2.copy(x, y, 268, 152)
        self.cache[path] = QPixmap.fromImage(cropped)
        try:
            THUMB_DIR.mkdir(exist_ok=True)
            cropped.save(str(self._disk_path(path)), 'JPG', 82)
        except OSError:
            pass
        self._start_fade(path)
        self.on_update()

    def _start_fade(self, path):
        self.fade[path] = 0.0
        anim = QVariantAnimation(self.worker)
        anim.setDuration(260)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.valueChanged.connect(lambda v, pth=path: self._fade_step(pth, v))
        anim.finished.connect(lambda pth=path: self.fade.pop(pth, None))
        anim.start()

    def _fade_step(self, path, value):
        if path in self.fade:
            self.fade[path] = float(value)
            self.on_update()


# ---------------- 列表绘制 ----------------
class VideoDelegate(QStyledItemDelegate):
    """视频库/历史共用; 看过的封面变暗+对勾徽章, 底部红色观看进度条"""

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.playing_path = None
        self.anim = 0
        self.progress_provider = None    # path -> (frac|None, watched)
        self._film = icon_pixmap('film', MUTED, 24)

    def sizeHint(self, option, index):
        return QSize(0, 96)

    def paint(self, p, option, index):
        path = index.data(ROLE_PATH)
        if not path:
            return
        t = index.data(ROLE_TITLE)
        if t is not None:
            title, author = t, index.data(ROLE_AUTHOR) or ''
        else:
            author, title = parse_name(path)
        ts = index.data(ROLE_TIME)
        frac, watched = (None, False)
        if self.progress_provider:
            frac, watched = self.progress_provider(path)
        playing = self.playing_path == path
        p.setRenderHint(QPainter.Antialiasing)

        r = option.rect.adjusted(8, 6, -8, -6)
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)
        if selected or hover:
            bg = QPainterPath()
            bg.addRoundedRect(QRectF(r), 10, 10)
            if selected:
                p.fillPath(bg, QColor(254, 44, 85, 30))
            else:
                p.fillPath(bg, QColor(255, 255, 255, 8))

        # 封面
        tr = QRect(r.x() + 6, r.y() + (r.height() - 76) // 2, 134, 76)
        tp = QPainterPath()
        tp.addRoundedRect(QRectF(tr), 8, 8)
        p.save()
        p.setClipPath(tp)
        pm = self.store.get(path)
        if pm is not None:
            alpha = self.store.fade.get(path)
            if alpha is not None:
                p.setOpacity(max(0.05, alpha))
            if watched:
                p.setOpacity(0.42)
            p.drawPixmap(tr, pm)
            p.setOpacity(1.0)
        else:
            p.fillRect(tr, QColor('#111927'))
            p.setOpacity(0.55)
            p.drawPixmap(tr.center().x() - 12, tr.center().y() - 12,
                         self._film)
            p.setOpacity(1.0)
        # 未看完的画观看进度条
        if frac is not None and not watched and frac > 0.01:
            bw = max(6, int((tr.width() - 6) * min(1.0, frac)))
            bar = QRect(tr.x() + 3, tr.bottom() - 6, bw, 3)
            bp = QPainterPath()
            bp.addRoundedRect(QRectF(bar), 1.5, 1.5)
            p.fillPath(bp, QColor(ACCENT))
        p.restore()
        # 封面描边: 常态极淡, 选中红色
        p.setPen(QPen(QColor(ACCENT) if selected
                      else QColor(255, 255, 255, 22), 1.6 if selected else 1))
        p.drawPath(tp)

        # 已看完: 右上角对勾徽章
        if watched:
            cx, cy = tr.right() - 14, tr.top() + 14
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 150))
            p.drawEllipse(QPoint_cx_cy(cx, cy), 9, 9)
            p.setPen(QPen(QColor('#ffffff'), 2, Qt.SolidLine, Qt.RoundCap))
            check = QPainterPath()
            check.moveTo(cx - 4, cy)
            check.lineTo(cx - 1, cy + 3)
            check.lineTo(cx + 4, cy - 3)
            p.drawPath(check)

        # 文本
        tx = tr.right() + 11
        tw = r.right() - 4 - tx
        f = QFont()
        f.setPixelSize(13)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(QColor(ACCENT if playing else (MUTED if watched else TEXT)))
        fm = p.fontMetrics()
        avail = tw - (56 if ts else 0)
        p.drawText(QRect(tx, tr.y() + 14, avail, 20),
                   Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(title, Qt.ElideRight, avail))
        f2 = QFont()
        f2.setPixelSize(11)
        p.setFont(f2)
        p.setPen(QColor(MUTED))
        p.drawText(QRect(tx, tr.y() + 38, tw - (56 if ts else 0), 16),
                   Qt.AlignLeft | Qt.AlignVCenter, author)
        if ts:
            p.drawText(QRect(r.right() - 60, tr.y() + 16, 56, 16),
                       Qt.AlignRight | Qt.AlignVCenter, fmt_ts(ts))

        # 正在播放: 封面左下角胶囊均衡器 (四条圆角柱)
        if playing:
            pw, ph = 42, 18
            px, py = tr.x() + 6, tr.bottom() - ph - 6
            pill = QPainterPath()
            pill.addRoundedRect(QRectF(px, py, pw, ph), 9, 9)
            p.fillPath(pill, QColor(0, 0, 0, 160))
            p.setPen(Qt.NoPen)
            for k in range(4):
                h = 4 + 9 * abs(math.sin(self.anim * 0.85 + k * 0.85))
                bar = QRectF(px + 7 + k * 7.5, py + ph / 2 - h / 2, 3, h)
                bar_path = QPainterPath()
                bar_path.addRoundedRect(bar, 1.5, 1.5)
                p.fillPath(bar_path, QColor('#ffffff'))


def QPoint_cx_cy(x, y):
    from PySide6.QtCore import QPoint
    return QPoint(x, y)


# ---------------- 视频画面 ----------------
class VideoCanvas(QWidget):
    """QVideoSink 软件自绘; 单击暂停/继续, 双击全屏"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._img = None
        self.error = ''
        self.show_placeholder = True
        self._sub = ''
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(260)
        self._click_timer.timeout.connect(self._single_click)

    def set_frame(self, frame):
        if frame.isValid():
            self._img = frame.toImage()
            self.update()

    def set_subtitle(self, text):
        if text != self._sub:
            self._sub = text
            self.update()

    def set_error(self, msg):
        self.error = msg
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), 12, 12)
        p.setClipPath(clip)
        p.fillRect(self.rect(), Qt.black)
        if self._img is not None and not self._img.isNull():
            iw, ih = self._img.width(), self._img.height()
            scale = min(self.width() / iw, self.height() / ih)
            tw, th = int(iw * scale), int(ih * scale)
            p.drawImage(QRect((self.width() - tw) // 2,
                              (self.height() - th) // 2, tw, th), self._img)
            if self._sub:
                self._draw_subtitle(p)
            return
        p.setPen(QColor(MUTED))
        if self.error:
            f = QFont()
            f.setPixelSize(14)
            p.setFont(f)
            p.drawText(self.rect().adjusted(0, -10, 0, -10),
                       Qt.AlignCenter, f'⚠ {self.error}')
            return
        if self.show_placeholder:
            icon = icon_pixmap('film', '#31405c', 44)
            p.drawPixmap((self.width() - 44) // 2,
                         (self.height() - 44) // 2 - 26, icon)
            f = QFont()
            f.setPixelSize(13)
            p.setFont(f)
            p.drawText(self.rect().adjusted(0, 24, 0, 24), Qt.AlignCenter,
                       '双击左侧列表开始播放 · 单击画面暂停 / 双击全屏')

    def _draw_subtitle(self, p):
        lines = [l for l in self._sub.split('\n') if l.strip()][-3:]
        f = QFont()
        f.setPixelSize(max(15, int(self.height() / 24)))
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        fm = p.fontMetrics()
        maxw = self.width() - 80
        wrapped = []
        for line in lines:
            if fm.horizontalAdvance(line) <= maxw:
                wrapped.append(line)
                continue
            cur = ''
            for ch in line:
                if cur and fm.horizontalAdvance(cur + ch) > maxw:
                    wrapped.append(cur)
                    cur = ch
                else:
                    cur += ch
            if cur:
                wrapped.append(cur)
        wrapped = wrapped[-4:]
        lh = int(fm.height() * 1.3)
        overlay = self.window().overlay
        bottom_gap = 108 if (overlay.isVisible() and
                             not self.window().isFullScreen()) else 26
        y = self.height() - bottom_gap - len(wrapped) * lh
        for line in wrapped:
            if not line:
                y += lh
                continue
            x = (self.width() - fm.horizontalAdvance(line)) // 2
            tp = QPainterPath()
            tp.addText(x, y + fm.ascent(), f, line)
            p.setPen(QPen(QColor(0, 0, 0, 210), 4, Qt.SolidLine,
                          Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(QColor('#ffffff'))
            p.drawPath(tp)
            y += lh

    def mousePressEvent(self, event):
        w = self.window()
        if hasattr(w, 'wake_controls'):
            w.wake_controls()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._click_timer.start()   # 等待 260ms 排除双击

    def mouseMoveEvent(self, event):
        w = self.window()
        if hasattr(w, 'wake_controls'):
            w.wake_controls()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w = self.window()
        if hasattr(w, '_relayout_video_children'):
            w._relayout_video_children()

    def mouseDoubleClickEvent(self, event):
        self._click_timer.stop()
        self.window().toggle_fullscreen()

    def _single_click(self):
        w = self.window()
        if hasattr(w, 'toggle_play'):
            w.toggle_play()
            w.wake_controls()


class SeekSlider(QSlider):
    """点击进度条直接跳转到点击位置 (默认只能翻页)"""

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and \
                self.maximum() > self.minimum():
            frac = event.position().x() / max(1, self.width())
            val = self.minimum() + int((self.maximum() - self.minimum())
                                       * min(1.0, max(0.0, frac)))
            self.setValue(val)
            self.sliderMoved.emit(val)
        super().mousePressEvent(event)


class ControlsOverlay(QWidget):
    """画面底部渐变遮罩上的控制栏"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setMouseTracking(True)
        row1 = QHBoxLayout()
        row1.setContentsMargins(4, 0, 4, 0)
        row1.setSpacing(10)
        lf = QFont()
        lf.setPixelSize(11)
        self.pos_label = QLabel('00:00')
        self.dur_label = QLabel('00:00')
        for lb in (self.pos_label, self.dur_label):
            lb.setFont(lf)
            lb.setStyleSheet('color: rgba(255,255,255,215); background:none;')
        self.seek = SeekSlider(Qt.Horizontal)
        self.seek.setRange(0, 0)
        self.seek.setObjectName('seek')
        row1.addWidget(self.pos_label)
        row1.addWidget(self.seek, 1)
        row1.addWidget(self.dur_label)

        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(6)

        def flat_btn(icon_name, tip, size=34, icon_size=18):
            b = QPushButton()
            b.setIcon(make_icon(icon_name))
            b.setIconSize(QSize(icon_size, icon_size))
            b.setFixedSize(size, size)
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.NoFocus)
            b.setCursor(Qt.PointingHandCursor)
            return b

        self.prev_btn = flat_btn('prev', '上一个 (Ctrl+←)')
        self.play_btn = flat_btn('play', '播放/暂停 (空格)', 40, 22)
        self.play_btn.setIcon(make_icon('play', '#ffffff'))
        self.play_btn.setStyleSheet(
            f'QPushButton {{ background: {ACCENT}; border-radius: 20px; }}'
            f'QPushButton:hover {{ background: #ff4d6b; }}'
            f'QPushButton:pressed {{ background: {ACCENT_DIM}; }}')
        self.next_btn = flat_btn('next', '下一个 (Ctrl+→)')
        self.speed_box = QComboBox()
        self.speed_box.addItems(SPEEDS)
        self.speed_box.setFocusPolicy(Qt.NoFocus)
        self.speed_box.setToolTip('倍速')
        self.mute_btn = flat_btn('volume', '静音 (M)')
        self.vol = SeekSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(80)
        self.vol.setFixedWidth(92)
        self.vol.setObjectName('vol')
        self.vol.setFocusPolicy(Qt.NoFocus)
        self.track_btn = flat_btn('track', '外挂音轨')
        self.sub_btn = flat_btn('cc', '外挂字幕')
        self.fs_btn = flat_btn('fullscreen', '全屏 (F)')
        for w in (self.prev_btn, self.play_btn, self.next_btn):
            row2.addWidget(w)
        row2.addStretch()
        row2.addWidget(self.speed_box)
        row2.addWidget(self.mute_btn)
        row2.addWidget(self.vol)
        row2.addWidget(self.sub_btn)
        row2.addWidget(self.track_btn)
        row2.addWidget(self.fs_btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 4, 16, 10)
        root.setSpacing(6)
        root.addLayout(row1)
        root.addLayout(row2)
        self.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                height: 4px; background: rgba(255,255,255,70);
                border-radius: 2px;
            }}
            QSlider::groove:horizontal:hover {{ height: 5px; }}
            QSlider#seek::sub-page:horizontal {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                            stop:0 {ACCENT}, stop:1 {ACCENT2});
                border-radius: 2px;
            }}
            QSlider#vol::sub-page:horizontal {{
                background: {ACCENT}; border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                width: 12px; margin: -4px 0; background: white;
                border-radius: 6px;
            }}
            QSlider::handle:horizontal:hover {{ width: 14px; margin: -5px 0; }}
            QPushButton {{ border-radius: 8px; }}
            QPushButton:hover {{ background: rgba(255,255,255,30); }}
        """)

    def paintEvent(self, event):
        from PySide6.QtGui import QLinearGradient
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0, QColor(0, 0, 0, 0))
        g.setColorAt(0.45, QColor(0, 0, 0, 130))
        g.setColorAt(1, QColor(0, 0, 0, 215))
        p.fillRect(self.rect(), g)


# ---------------- 主窗口 ----------------
class VideoPlayer(QMainWindow):

    def __init__(self):
        super().__init__()
        self.cfg = self._load_cfg()
        self.folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
            self.cfg.get('folder') or DEFAULT_DIR)
        self.paths = []
        self.current = -1
        self.current_path = ''        # 当前播放文件 (字符串路径)
        self._seeking = False
        self._hist_mode = False
        self._resume_path = None
        self._last_flush = -1
        self._seen_max_dur = 0
        self.progress_data = dict(self.cfg.get('progress') or {})
        self.history = self._load_history()

        self.worker = ThumbWorker(self)
        self.store = ThumbStore(self.worker, self._thumbs_updated)
        self.worker.start()
        self.delegates = []

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        # 外挂音轨专用播放器 (与画面播放器对时同步)
        self.audio2 = QMediaPlayer(self)
        self.audio2_out = QAudioOutput(self)
        self.audio2.setAudioOutput(self.audio2_out)
        self.ext_audio = None          # 外挂音轨路径
        self.sub_path = None           # 外挂字幕路径
        self.sub_cues = []
        self.sub_starts = []
        self._muted = False
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(300)
        self._sync_timer.timeout.connect(self._sync_external)
        self._sync_timer.start()
        # ffmpeg 后台任务 (内嵌音轨抽取 / HDR 色彩修正)
        self._jobs = []
        self._probes = set()
        self._embedded_audio = []      # [(stream_index, 标签)]
        self._emb_sel = None           # 已选中的内嵌音轨流号
        self._job_texts = {}           # tag -> 状态文本
        self._title_text = ''
        self._sdr_pair = None          # (原片路径, SDR 缓存路径)
        self._pending_seek_ms = None

        self.setWindowTitle('本地视频播放器')
        self.resize(1360, 850)
        self.setMinimumSize(1020, 640)
        self._build_ui()
        self._bind_keys()
        self._connect_player()

        vol = int(self.cfg.get('volume', 80))
        self.overlay.vol.setValue(vol)
        self.change_volume(vol)
        speed = float(self.cfg.get('speed', 1.0))
        idx = next((i for i, s in enumerate(SPEEDS)
                    if abs(float(s.rstrip('×')) - speed) < 0.01), 2)
        self.overlay.speed_box.setCurrentIndex(idx)

        self._eq_timer = QTimer(self)
        self._eq_timer.timeout.connect(self._tick_eq)
        self._eq_timer.start(350)

        self._prio_timer = QTimer(self)
        self._prio_timer.setSingleShot(True)
        self._prio_timer.setInterval(150)
        self._prio_timer.timeout.connect(self._update_thumb_priority)

        self.load_folder(self.folder)

    # ---------- 界面 ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        self.split = QSplitter(Qt.Horizontal)
        root.addWidget(self.split)

        # ---- 左侧栏 ----
        side = QWidget()
        side.setStyleSheet(f'background: {SIDEBAR};')
        side.setMinimumWidth(300)
        side.setMaximumWidth(480)
        lv = QVBoxLayout(side)
        lv.setContentsMargins(14, 14, 8, 12)
        lv.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(4)
        folder_btn = QPushButton()
        folder_btn.setIcon(make_icon('folder'))
        folder_btn.setIconSize(QSize(18, 18))
        folder_btn.setFixedSize(34, 34)
        folder_btn.setToolTip('打开目录')
        folder_btn.setCursor(Qt.PointingHandCursor)
        folder_btn.clicked.connect(self.pick_folder)
        head.addWidget(folder_btn)
        head.addSpacing(2)

        self.btn_lib = QPushButton('视频库')
        self.btn_hist = QPushButton('历史')
        for b in (self.btn_lib, self.btn_hist):
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(self._on_tab_clicked)
            head.addWidget(b)
        self.btn_lib.setChecked(True)
        self._style_tabs()
        head.addStretch()
        self.count_label = QLabel('')
        cf = QFont()
        cf.setPixelSize(11)
        self.count_label.setFont(cf)
        self.count_label.setStyleSheet(f'color: {MUTED}; background:none;')
        head.addWidget(self.count_label)
        lv.addLayout(head)

        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索作者 / 标题...')
        self.search.addAction(icon_pixmap('search', MUTED, 16),
                              QLineEdit.LeadingPosition)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search)
        lv.addWidget(self.search)

        # 历史模式工具条 (默认隐藏)
        self.hist_bar = QWidget()
        hb = QHBoxLayout(self.hist_bar)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(8)
        clear_btn = QPushButton(' 清空历史')
        clear_btn.setIcon(make_icon('trash', '#f87171'))
        clear_btn.setIconSize(QSize(15, 15))
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet(
            'QPushButton { color: #f87171; padding: 5px 10px;'
            ' border: 1px solid rgba(248,113,113,60); border-radius: 8px; }'
            'QPushButton:hover { background: rgba(248,113,113,28); }')
        clear_btn.clicked.connect(self._clear_history)
        hb.addWidget(clear_btn)
        hb.addStretch()
        self.hist_hint = QLabel('按观看时间倒序')
        hf = QFont()
        hf.setPixelSize(11)
        self.hist_hint.setFont(hf)
        self.hist_hint.setStyleSheet(f'color: {MUTED}; background:none;')
        hb.addWidget(self.hist_hint)
        self.hist_bar.hide()
        lv.addWidget(self.hist_bar)

        # 两个列表页
        self.stack = QStackedWidget()
        self.list = QListWidget()
        self.history_list = QListWidget()
        for lw in (self.list, self.history_list):
            d = VideoDelegate(self.store)
            d.progress_provider = self._watch_info
            self.delegates.append(d)
            lw.setItemDelegate(d)
            lw.setVerticalScrollMode(QListWidget.ScrollPerPixel)
            lw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            lw.setMouseTracking(True)
            lw.viewport().setAttribute(Qt.WA_Hover, True)
            lw.verticalScrollBar().valueChanged.connect(
                lambda _: self._prio_timer.start())
        self.list.itemDoubleClicked.connect(
            lambda item: self.play_path(item.data(ROLE_PATH)))
        self.history_list.itemDoubleClicked.connect(
            lambda item: self.play_path(item.data(ROLE_PATH)))
        self.stack.addWidget(self.list)
        self.stack.addWidget(self.history_list)
        lv.addWidget(self.stack, 1)
        self.split.addWidget(side)

        # ---- 右侧: 画面 + 悬浮控制 ----
        self.video = VideoCanvas()
        self.split.addWidget(self.video)
        self.split.setSizes([380, 980])
        self.split.setStretchFactor(1, 1)

        self.title_label = QLabel(self.video)
        self.title_label.setStyleSheet(
            'color: white; font-size: 13px; font-weight: 600;'
            'padding: 12px 16px 0 16px;'
            'background: qlineargradient(x1:0, y1:0, x2:0, y2:1,'
            ' stop:0 rgba(0,0,0,150), stop:1 rgba(0,0,0,0));'
            'border-top-left-radius: 12px; border-top-right-radius: 12px;')
        self.title_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.title_label.hide()
        self.overlay = ControlsOverlay(self.video)
        self.overlay.installEventFilter(self)
        self.overlay_fader = Fader(self.overlay)
        self.title_fader = Fader(self.title_label)

    def _on_tab_clicked(self):
        # 以被点击的按钮为准, 不读勾选状态 (可勾选按钮二次点击会自行取消勾选)
        self._switch_view(self.sender() is self.btn_hist)

    def _style_tabs(self):
        for b, active in ((self.btn_lib, not self._hist_mode),
                          (self.btn_hist, self._hist_mode)):
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {'#1d2534' if active else 'transparent'};
                    color: {TEXT if active else MUTED};
                    padding: 5px 12px; font-weight: 600;
                    border-bottom: 2px solid
                        {'#fe2c55' if active else 'transparent'};
                    border-radius: 8px 8px 0 0;
                }}""")

    def _switch_view(self, hist_mode):
        self._hist_mode = hist_mode
        self.btn_lib.setChecked(not hist_mode)
        self.btn_hist.setChecked(hist_mode)
        self._style_tabs()
        self.stack.setCurrentIndex(1 if hist_mode else 0)
        self.search.setVisible(not hist_mode)
        self.hist_bar.setVisible(hist_mode)
        if hist_mode:
            self._rebuild_history()
        self._update_count()
        self._prio_timer.start()

    def _update_count(self):
        if self._hist_mode:
            self.count_label.setText(f'{len(self.history)} 条记录')
        else:
            self.count_label.setText(f'{len(self.paths)} 个视频')

    def _rebuild_history(self):
        self.history_list.clear()
        for entry in reversed(self.history):
            it = QListWidgetItem()
            it.setData(ROLE_PATH, entry['path'])
            it.setData(ROLE_TIME, entry.get('t', 0))
            it.setData(ROLE_TITLE, entry.get('title', ''))
            it.setData(ROLE_AUTHOR, entry.get('author', ''))
            it.setToolTip(f"{entry.get('title', '')}\n{entry.get('author', '')}")
            self.history_list.addItem(it)
        self._update_count()

    def _clear_history(self):
        self.history = []
        self._save_history()
        self._rebuild_history()

    def eventFilter(self, obj, event):
        if obj is self.overlay and event.type() in (
                event.Type.Enter, event.Type.MouseMove):
            self.wake_controls()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._prio_timer.start()

    def _relayout_video_children(self):
        w, h = self.video.width(), self.video.height()
        self.overlay.setGeometry(0, h - 96, w, 96)
        self.title_label.setGeometry(0, 0, w, 46)

    # ---------- 播放器接线 ----------
    def _connect_player(self):
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self.video.set_frame)
        self.player.positionChanged.connect(self.on_position)
        self.player.durationChanged.connect(self.on_duration)
        self.player.mediaStatusChanged.connect(self.on_status)
        self.player.errorOccurred.connect(self.on_error)
        # 状态真正切到播放后才启动自动隐藏定时器 (play() 是异步的)
        self.player.playbackStateChanged.connect(self._on_play_state)

        o = self.overlay
        o.prev_btn.clicked.connect(self.prev_video)
        o.play_btn.clicked.connect(self.toggle_play)
        o.next_btn.clicked.connect(self.next_video)
        o.fs_btn.clicked.connect(self.toggle_fullscreen)
        o.mute_btn.clicked.connect(self.toggle_mute)
        o.vol.valueChanged.connect(self.change_volume)
        o.speed_box.currentTextChanged.connect(self.change_speed)
        o.track_btn.clicked.connect(self._show_audio_menu)
        o.sub_btn.clicked.connect(self._show_sub_menu)
        o.seek.sliderPressed.connect(lambda: setattr(self, '_seeking', True))
        o.seek.sliderMoved.connect(self._on_seek_moved)
        o.seek.sliderReleased.connect(self._on_seek_released)

    def _bind_keys(self):
        def guarded(fn, allow_in_search=False):
            def f():
                if self.search.hasFocus() and not allow_in_search:
                    return
                fn()
            return f

        def esc():
            if self.search.hasFocus():
                self.search.clear()
                return
            self.exit_fullscreen()

        binds = [
            ('Space', guarded(self.toggle_play)),
            ('Left', guarded(lambda: self.seek(-5000))),
            ('Right', guarded(lambda: self.seek(5000))),
            ('Up', guarded(lambda: self.nudge_volume(5))),
            ('Down', guarded(lambda: self.nudge_volume(-5))),
            ('Ctrl+Left', guarded(self.prev_video)),
            ('Ctrl+Right', guarded(self.next_video)),
            ('F', guarded(self.toggle_fullscreen)),
            ('H', guarded(self._toggle_sdr)),
            ('M', guarded(self.toggle_mute)),
            ('Escape', esc),
        ]
        for key, fn in binds:
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(fn)

    # ---------- 目录与列表 ----------
    def load_folder(self, folder):
        if not folder.is_dir():
            self.count_label.setText(f'目录不存在: {folder}')
            return
        self.folder = folder
        # 递归扫描: 支持 "每部电影一个子文件夹" 的媒体库布局
        files = [p for p in folder.rglob('*')
                 if p.suffix.lower() in VIDEO_EXTS and p.is_file()]
        self.paths = sorted(files, key=lambda p: (str(p.parent).lower(),
                                                  p.name))
        self.list.clear()
        # 每个子文件夹的视频数 (单视频文件夹直接用文件夹名做标题)
        per_dir = {}
        for p in self.paths:
            per_dir.setdefault(str(p.parent), []).append(p)
        for p in self.paths:
            author, title = parse_name(p)
            if p.parent != self.folder:
                dirname = p.parent.name
                if len(per_dir[str(p.parent)]) > 1:
                    title = f'{dirname} · {title or p.stem}'
                else:
                    title, author = dirname, author
            it = QListWidgetItem()
            it.setData(ROLE_PATH, str(p))
            it.setData(ROLE_TITLE, title)
            it.setData(ROLE_AUTHOR, author)
            it.setToolTip(f'{title}\n{author}')
            self.list.addItem(it)
        self._update_count()
        self.setWindowTitle('本地视频播放器')

        cfg = self._load_cfg()
        cfg['folder'] = str(folder)
        self._write_cfg(cfg)
        last = cfg.get('last_file')
        if last:
            for i, p in enumerate(self.paths):
                if str(p) == last:
                    self.list.setCurrentRow(i)
                    break
        self._prio_timer.start()

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, '选择视频目录',
                                             str(self.folder))
        if d:
            self.load_folder(Path(d))

    def _on_search(self, text):
        text = text.strip().lower()
        for i, p in enumerate(self.paths):
            hit = not text or text in p.name.lower()
            self.list.setRowHidden(i, not hit)
        shown = sum(not self.list.isRowHidden(i)
                    for i in range(len(self.paths)))
        self.count_label.setText(f'{shown} 个视频')
        self._prio_timer.start()

    def _active_list(self):
        return self.history_list if self._hist_mode else self.list

    def _visible_paths(self):
        lw = self._active_list()
        vr = lw.viewport().rect()
        rows = []
        for i in range(lw.count()):
            if lw.isRowHidden(i):
                continue
            r = lw.visualItemRect(lw.item(i))
            if r.bottom() >= vr.top() and r.top() <= vr.bottom():
                rows.append(i)
        return {lw.item(i).data(ROLE_PATH) for i in rows}

    def _update_thumb_priority(self):
        if self._hist_mode or self.paths:
            self.worker.prioritize(self._visible_paths())

    def _thumbs_updated(self):
        self.list.viewport().update()
        self.history_list.viewport().update()

    def _tick_eq(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            for d in self.delegates:
                d.anim += 1
            self.list.viewport().update()
            self.history_list.viewport().update()

    def _watch_info(self, path):
        """delegate 进度提供器: path -> (进度比例|None, 是否已看完)"""
        e = self.progress_data.get(path)
        if not e or not e.get('d'):
            return (None, False)
        frac = min(1.0, e.get('p', 0) / e['d'])
        return (frac, bool(e.get('w')) or frac >= WATCHED_RATIO)

    # ---------- 播放控制 ----------
    def play_path(self, path):
        for i, pp in enumerate(self.paths):
            if str(pp) == str(path):
                return self.play_index(i)
        # 文件已不在库中, 直接播放
        self.current = -1
        self._start_playback(Path(path))

    def play_index(self, idx):
        if not (0 <= idx < len(self.paths)):
            return
        self.current = idx
        self.list.setCurrentRow(idx)
        self._start_playback(self.paths[idx])

    def _start_playback(self, path):
        self.current_path = str(path)
        self._resume_path = self.current_path
        self._last_flush = -1
        self._seen_max_dur = 0
        self.player.stop()
        self._reset_external()
        self._sdr_pair = None
        self.video.error = ''
        self.video.show_placeholder = False
        self.player.setSource(QUrl.fromLocalFile(self.current_path))
        self.player.play()
        _, title = parse_name(path)
        self._title_text = title
        self._refresh_title()
        self.setWindowTitle(f'{title} - 本地视频播放器')
        for d in self.delegates:
            d.playing_path = self.current_path
        self._record_history(path)
        self._flush_progress(force=True)
        cfg = self._load_cfg()
        cfg['last_file'] = self.current_path
        self._write_cfg(cfg)
        self._update_viewports()
        self._auto_attach_external()
        self._restore_embedded_track()
        self._probe_video_info()
        self.wake_controls()

    def _refresh_title(self):
        txt = self._title_text
        extra = '  ·  '.join(self._job_texts.values())
        if extra:
            txt = f'{txt}　[{extra}]'
        self.title_label.setText(txt)
        self.setWindowTitle(f'{self._title_text or "本地视频播放器"}'
                            f' - 本地视频播放器')

    def _record_history(self, path):
        path = str(path)
        author, title = parse_name(path)
        self.history = [h for h in self.history if h['path'] != path]
        self.history.append({'path': path, 'title': title,
                             'author': author, 't': time.time()})
        if len(self.history) > HISTORY_MAX:
            del self.history[:-HISTORY_MAX]
        self._save_history()
        if self._hist_mode:
            self._rebuild_history()

    def toggle_play(self):
        st = self.player.playbackState()
        if st == QMediaPlayer.PlayingState:
            self.player.pause()
        elif self.current >= 0 or self.paths:
            if self.current < 0 and self.current_path:
                self.player.play()
            elif self.current < 0:
                first = next((i for i in range(len(self.paths))
                              if not self.list.isRowHidden(i)), -1)
                if first < 0:
                    return
                return self.play_index(first)
            else:
                self.player.play()

    def _visible_or_all(self):
        pool = [i for i in range(len(self.paths))
                if not self.list.isRowHidden(i)]
        return pool or list(range(len(self.paths)))

    def next_video(self):
        pool = self._visible_or_all()
        if not pool:
            return
        nxt = pool[0] if self.current not in pool else pool[
            (pool.index(self.current) + 1) % len(pool)]
        self.play_index(nxt)

    def prev_video(self):
        pool = self._visible_or_all()
        if not pool:
            return
        prv = pool[-1] if self.current not in pool else pool[
            (pool.index(self.current) - 1) % len(pool)]
        self.play_index(prv)

    def seek(self, delta):
        pos = max(0, min(self.player.position() + delta,
                         self.player.duration()))
        self.player.setPosition(pos)
        if self.ext_audio:
            self.audio2.setPosition(pos)

    def change_speed(self, text):
        rate = float(text.rstrip('×'))
        self.player.setPlaybackRate(rate)
        self.audio2.setPlaybackRate(rate)
        cfg = self._load_cfg()
        cfg['speed'] = rate
        self._write_cfg(cfg)

    def change_volume(self, val):
        v = val / 100
        for out in (self.audio, self.audio2_out):
            out.setVolume(v)
            out.setMuted(v == 0)
        self._apply_audio_routing()
        name = 'mute' if val == 0 else ('volume-low' if val < 50 else 'volume')
        self.overlay.mute_btn.setIcon(make_icon(name))

    def nudge_volume(self, delta):
        self.overlay.vol.setValue(max(0, min(
            100, self.overlay.vol.value() + delta)))

    def toggle_mute(self):
        self._muted = not self._muted
        self._apply_audio_routing()
        self.overlay.mute_btn.setIcon(
            make_icon('mute' if self._muted else 'volume'))

    def _on_seek_moved(self, val):
        self.overlay.pos_label.setText(fmt_time(val))

    def _on_seek_released(self):
        self._seeking = False
        self.player.setPosition(self.overlay.seek.value())
        if self.ext_audio:
            self.audio2.setPosition(self.overlay.seek.value())

    def on_position(self, ms):
        if not self._seeking:
            self.overlay.seek.blockSignals(True)
            self.overlay.seek.setValue(int(ms))
            self.overlay.seek.blockSignals(False)
        self.overlay.pos_label.setText(fmt_time(ms))
        self._update_subtitle(ms)
        if abs(ms - self._last_flush) > 4000:
            self._flush_progress(ms)

    def on_duration(self, dur):
        self._seen_max_dur = max(self._seen_max_dur, int(dur))
        self.overlay.seek.setRange(0, int(dur))
        self.overlay.dur_label.setText(fmt_time(dur))
        # 手动跳转 (H 切换 SDR/原片时保持进度)
        if self._pending_seek_ms is not None and dur > 0:
            self.player.setPosition(min(self._pending_seek_ms, int(dur)))
            self._pending_seek_ms = None
            return
        # 断点续播: 上次看过一部分且未看完
        if self._resume_path and dur > 0:
            e = self.progress_data.get(self._resume_path)
            self._resume_path = None
            if e and 3000 < e.get('p', 0) < dur * WATCHED_RATIO:
                self.player.setPosition(e['p'])

    def on_status(self, status):
        if status == QMediaPlayer.EndOfMedia:
            self._flush_progress(force=True, ended=True)
            self.audio2.stop()
            self.next_video()

    def on_error(self, err, msg):
        if err != QMediaPlayer.NoError:
            self.video.set_error(f'无法播放: {msg}')

    def _on_play_state(self, state):
        playing = state == QMediaPlayer.PlayingState
        self.overlay.play_btn.setIcon(
            make_icon('pause' if playing else 'play', '#ffffff'))
        if self.ext_audio:
            if playing:
                self.audio2.setPosition(max(self.audio2.position(),
                                            self.player.position() - 120))
                self.audio2.play()
            else:
                self.audio2.pause()
        self.wake_controls()

    # ---------- 外挂音轨 / 字幕 ----------
    def _reset_external(self):
        self.ext_audio = None
        self.audio2.stop()
        self.audio2.setSource(QUrl())
        self.sub_path = None
        self.sub_cues = []
        self.sub_starts = []
        self.video.set_subtitle('')

    def _auto_attach_external(self):
        """同目录下与视频同名的音轨/字幕自动挂载"""
        vp = Path(self.current_path)
        try:
            for f in sorted(vp.parent.iterdir(), key=lambda x: x.name):
                if f.stem.lower() != vp.stem.lower() or not f.is_file():
                    continue
                ext = f.suffix.lower()
                if ext in SUB_EXTS and not self.sub_cues:
                    self._set_sub(str(f))
                elif ext in AUDIO_EXTS and not self.ext_audio:
                    self._set_ext_audio(str(f))
        except OSError:
            pass

    def _candidates(self, exts):
        """同目录下文件名相近的外挂文件"""
        if not self.current_path:
            return []
        vp = Path(self.current_path)
        stem = vp.stem.lower()
        out = []
        try:
            for f in sorted(vp.parent.iterdir(), key=lambda x: x.name):
                if not f.is_file() or f.suffix.lower() not in exts:
                    continue
                fs = f.stem.lower()
                n = 0
                for a, b in zip(stem, fs):
                    if a != b:
                        break
                    n += 1
                if n >= 6 or fs in stem or stem in fs:
                    out.append(str(f))
        except OSError:
            pass
        return out

    def _menu_style(self):
        return (f'QMenu {{ background: {CARD}; color: {TEXT};'
                f' border: 1px solid {BORDER}; padding: 6px; }}'
                f'QMenu::item {{ padding: 6px 20px; border-radius: 6px; }}'
                f'QMenu::item:selected {{ background: {ACCENT_DIM}; }}'
                f'QMenu::separator {{ height: 1px; background: {BORDER};'
                f' margin: 4px 8px; }}')

    def _use_default_audio(self):
        self._emb_sel = None
        self._set_ext_audio(None)

    def _show_audio_menu(self):
        m = QMenu(self)
        m.setStyleSheet(self._menu_style())
        grp = QActionGroup(m)
        grp.setExclusive(True)
        a0 = QAction('视频内置音轨（默认）', m)
        a0.setCheckable(True)
        a0.setChecked(self.ext_audio is None)
        a0.triggered.connect(self._use_default_audio)
        grp.addAction(a0)
        m.addAction(a0)
        # 内封音轨 (mkv 常见): 选中后 ffmpeg 抽流为外挂音轨
        for idx, label, lang in self._embedded_audio:
            tag = f' [{lang}]' if lang else ''
            a = QAction(f'内嵌 · {label}{tag}', m)
            a.setCheckable(True)
            a.setChecked(self._emb_sel == idx)
            a.triggered.connect(lambda _, i=idx: self._select_embedded_audio(i))
            grp.addAction(a)
            m.addAction(a)
        m.addSeparator()
        for cand in self._candidates(AUDIO_EXTS):
            a = QAction(f'外挂 · {Path(cand).name}', m)
            a.setCheckable(True)
            a.setChecked(self.ext_audio == cand)
            a.triggered.connect(lambda _, p=cand: self._set_ext_audio(p))
            grp.addAction(a)
            m.addAction(a)
        m.addSeparator()
        open_a = QAction('选择音轨文件...', m)
        open_a.triggered.connect(self._pick_audio)
        m.addAction(open_a)
        m.exec(self.overlay.track_btn.mapToGlobal(
            QPoint(0, -m.sizeHint().height() - 8)))

    def _show_sub_menu(self):
        m = QMenu(self)
        m.setStyleSheet(self._menu_style())
        grp = QActionGroup(m)
        grp.setExclusive(True)
        a0 = QAction('关闭字幕', m)
        a0.setCheckable(True)
        a0.setChecked(self.sub_path is None)
        a0.triggered.connect(lambda: self._set_sub(None))
        grp.addAction(a0)
        m.addAction(a0)
        for cand in self._candidates(SUB_EXTS):
            a = QAction(Path(cand).name, m)
            a.setCheckable(True)
            a.setChecked(self.sub_path == cand)
            a.triggered.connect(lambda _, p=cand: self._set_sub(p))
            grp.addAction(a)
            m.addAction(a)
        m.addSeparator()
        open_a = QAction('选择字幕文件...', m)
        open_a.triggered.connect(self._pick_sub)
        m.addAction(open_a)
        m.exec(self.overlay.sub_btn.mapToGlobal(
            QPoint(0, -m.sizeHint().height() - 8)))

    def _pick_audio(self):
        f, _ = QFileDialog.getOpenFileName(
            self, '选择音轨文件', str(self.folder),
            '音频文件 (*' + ' *'.join(sorted(AUDIO_EXTS)) + ');;所有文件 (*.*)')
        if f:
            self._set_ext_audio(f)

    def _pick_sub(self):
        f, _ = QFileDialog.getOpenFileName(
            self, '选择字幕文件', str(self.folder),
            '字幕文件 (*' + ' *'.join(sorted(SUB_EXTS)) + ');;所有文件 (*.*)')
        if f:
            self._set_sub(f)

    def _set_ext_audio(self, path):
        self.ext_audio = path
        self.audio2.stop()
        was_playing = self.player.playbackState() == QMediaPlayer.PlayingState
        if path:
            self.audio2.setSource(QUrl.fromLocalFile(path))
            self.audio2.setPlaybackRate(self.player.playbackRate())
            if was_playing:
                self.audio2.setPosition(self.player.position())
                self.audio2.play()
        else:
            self.audio2.setSource(QUrl())
        self._apply_audio_routing()

    def _set_sub(self, path):
        self.sub_path = path
        self.sub_cues = parse_subtitle(path) if path else []
        self.sub_starts = [c[0] for c in self.sub_cues]
        self.video.set_subtitle('')

    def _apply_audio_routing(self):
        """有外挂音轨时静掉视频内置音轨, 反之静掉外挂播放器"""
        has_ext = bool(self.ext_audio)
        self.audio.setMuted(has_ext or self._muted)
        self.audio2_out.setMuted(not has_ext or self._muted)

    def _sync_external(self):
        """外挂音轨与画面时钟对时: 偏差超过 350ms 强制校正"""
        if not self.ext_audio:
            return
        if self.player.playbackState() != QMediaPlayer.PlayingState:
            return
        if self.audio2.playbackState() != QMediaPlayer.PlayingState:
            self.audio2.play()
            return
        drift = self.audio2.position() - self.player.position()
        if abs(drift) > 350:
            self.audio2.setPosition(self.player.position())

    def _update_subtitle(self, ms):
        if not self.sub_cues:
            return
        i = bisect_right(self.sub_starts, ms) - 1
        text = ''
        if i >= 0:
            s, e, t = self.sub_cues[i]
            if s <= ms < e:
                text = t
        self.video.set_subtitle(text)

    # ---------- ffmpeg 探测 / 内嵌音轨 / HDR ----------
    def _probe_video_info(self):
        """异步 ffprobe: 取视频流颜色信息 + 全部音轨列表"""
        self._embedded_audio = []
        self._emb_sel = None
        self._sdr_pair = None
        if not FFPROBE:
            return
        path = self.current_path
        proc = QProcess(self)
        self._probes.add(proc)

        def done(code, _st):
            self._probes.discard(proc)
            if self.current_path != path:
                return
            try:
                info = json.loads(
                    bytes(proc.readAllStandardOutput())
                    .decode('utf-8', 'ignore'))
            except ValueError:
                return
            self._on_video_info(path, info)
            proc.deleteLater()

        proc.finished.connect(done)
        proc.start(FFPROBE, ['-v', 'error', '-show_streams', '-show_format',
                             '-of', 'json', path])

    def _on_video_info(self, path, info):
        streams = info.get('streams', [])
        total_ms = int(float(info.get('format', {}).get('duration', 0)) * 1000)
        self._probe_total_ms = total_ms
        self._embedded_audio = []
        for s in streams:
            if s.get('codec_type') != 'audio':
                continue
            tags = s.get('tags', {}) or {}
            label = tags.get('title') or tags.get('language') \
                or f'音轨 {s["index"]}'
            lang = tags.get('language', '')
            self._embedded_audio.append((s['index'], label, lang))
        # HDR 检测 -> 自动后台生成 SDR 修正版
        v = next((s for s in streams if s.get('codec_type') == 'video'), {})
        transfer = (v.get('color_transfer') or '').lower()
        if transfer in ('smpte2084', 'arib-std-b67') and FFMPEG:
            self._start_hdr_job(path, total_ms)
        else:
            try:
                self._update_viewports()
            except Exception:
                pass

    def _emb_cache_path(self, src, idx):
        p = Path(src)
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        h = hashlib.md5(f'{src}|{size}'.encode()).hexdigest()[:16]
        return CACHE_DIR / 'audio' / f'{h}_a{idx}.mka'

    def _sdr_cache_path(self, src):
        p = Path(src)
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        h = hashlib.md5(f'{src}|{size}'.encode()).hexdigest()[:16]
        return CACHE_DIR / 'hdr' / f'{h}_sdr.mp4'

    def _restore_embedded_track(self):
        """上次为这部片子选过的内嵌音轨: 缓存存在就直接挂载"""
        e = self.progress_data.get(self.current_path)
        idx = e.get('at') if e else None
        if idx is None:
            return
        cache = self._emb_cache_path(self.current_path, idx)
        if cache.exists():
            self._emb_sel = idx
            self._set_ext_audio(str(cache))

    def _select_embedded_audio(self, idx):
        self._emb_sel = idx
        e = self.progress_data.setdefault(self.current_path, {})
        e['at'] = idx
        self._write_cfg(dict(self.cfg) | {'progress': self.progress_data})
        cache = self._emb_cache_path(self.current_path, idx)
        if cache.exists():
            self._set_ext_audio(str(cache))
            return
        total = getattr(self, '_probe_total_ms', 0) or self.player.duration()
        label = next((l for i, l, _ in self._embedded_audio if i == idx),
                     f'音轨{idx}')
        self._start_extract(self.current_path, idx, total, label)

    def _start_extract(self, src, idx, total_ms, label):
        out = self._emb_cache_path(src, idx)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.stem + '.part' + out.suffix)
        args = [FFMPEG, '-y', '-v', 'warning', '-stats', '-i', src,
                '-map', f'0:{idx}', '-vn', '-c', 'copy', str(tmp)]
        job = FfmpegJob(args, total_ms, str(tmp), 'track', self)
        self._jobs.append(job)
        self._set_job_text('track', f'正在提取「{label}」…')

        def on_prog(v):
            self._set_job_text('track', f'正在提取「{label}」{int(v*100)}%')

        def on_done(ok, _tmp):
            if job in self._jobs:
                self._jobs.remove(job)
            self._set_job_text('track', None)
            if ok:
                tmp.replace(out)
            if ok and self.current_path == src and self._emb_sel == idx:
                self._set_ext_audio(str(out))
                self._set_job_text('track', f'「{label}」已就绪')
                QTimer.singleShot(4000,
                                  lambda: self._set_job_text('track', None))

        job.progress.connect(on_prog)
        job.done.connect(on_done)

    def _start_hdr_job(self, src, total_ms):
        out = self._sdr_cache_path(src)
        if out.exists():
            self._sdr_pair = (src, str(out))
            self._set_job_text('hdr', '色彩修正版已就绪 · 按 H 切换')
            QTimer.singleShot(6000, lambda: self._set_job_text('hdr', None))
            return
        out.parent.mkdir(parents=True, exist_ok=True)
        # 实测参数: GPU libplacebo 色调映射 + 降 1080p + NVENC ≈ 1.8x 实时
        vf = ('libplacebo=colorspace=bt709:tonemapping=bt.2390:peak_detect=false,'
              'scale=1920:1080:flags=bicubic,format=yuv420p')
        attempts = [
            ('NVENC', '-vf', vf,
             '-c:v', 'h264_nvenc', '-preset', 'p5', '-cq', '22'),
            ('CPU', '-vf',
             'zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,'
             'tonemap=hable,zscale=t=bt709:m=bt709:r=tv,'
             'scale=1920:1080:flags=bicubic,format=yuv420p',
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23'),
        ]

        def run(i):
            if i >= len(attempts) or self.current_path != src:
                return
            name, vf_key, vf, *enc = attempts[i]
            args = [FFMPEG, '-y', '-v', 'warning', '-stats', '-i', src,
                    vf_key, vf, *enc,
                    '-c:a', 'aac', '-b:a', '256k', str(out)]
            job = FfmpegJob(args, total_ms, str(out), 'hdr', self)
            self._jobs.append(job)
            self._set_job_text('hdr', f'正在生成色彩修正版({name})…')

            def on_prog(v):
                self._set_job_text('hdr',
                                   f'正在生成色彩修正版({name}) {int(v*100)}%')

            def on_done(ok, outp):
                if job in self._jobs:
                    self._jobs.remove(job)
                if ok:
                    self._set_job_text('hdr', '色彩修正版已就绪 · 按 H 切换')
                    if self.current_path == src:
                        self._sdr_pair = (src, outp)
                    QTimer.singleShot(
                        6000, lambda: self._set_job_text('hdr', None))
                else:
                    out.unlink(missing_ok=True)
                    run(i + 1)

            job.progress.connect(on_prog)
            job.done.connect(on_done)

        run(0)

    def _toggle_sdr(self):
        if not self._sdr_pair:
            return
        orig, sdr = self._sdr_pair
        cur = self.player.source().toLocalFile()
        target = orig if cur.lower() == sdr.lower() else sdr
        pos = self.player.position()
        was_playing = self.player.playbackState() == \
            QMediaPlayer.PlayingState
        self.player.stop()
        self._pending_seek_ms = pos
        self.player.setSource(QUrl.fromLocalFile(target))
        if was_playing:
            self.player.play()
        self._set_job_text('hdr', None)
        self.wake_controls()

    def _set_job_text(self, tag, text):
        if text is None:
            self._job_texts.pop(tag, None)
        else:
            self._job_texts[tag] = text
        self._refresh_title()

    # ---------- 观看进度 ----------
    def _flush_progress(self, pos=None, force=False, ended=False):
        path = self.current_path
        if not path:
            return
        if pos is None:
            pos = self.player.position()
        dur = self.player.duration()
        self._last_flush = pos
        # 用已见过的最大时长, 避免加载期瞬时小时长误标"已看完"
        final_dur = max(dur, self._seen_max_dur)
        if final_dur <= 0 and not ended:
            return
        e = self.progress_data.get(path, {})
        e['p'] = int(pos)
        e['d'] = int(final_dur)
        if ended or (final_dur > 0 and pos / final_dur >= WATCHED_RATIO):
            e['w'] = True
        self.progress_data[path] = e
        cfg = self._load_cfg()
        cfg['progress'] = self.progress_data
        self._write_cfg(cfg)
        self._update_viewports()

    def _update_viewports(self):
        self.list.viewport().update()
        self.history_list.viewport().update()

    # ---------- 控制栏显隐 ----------
    def wake_controls(self):
        self.overlay_fader.to(1.0)
        self.title_fader.to(1.0)
        self.video.setCursor(Qt.ArrowCursor)
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            if not hasattr(self, '_hide_timer'):
                self._hide_timer = QTimer(self)
                self._hide_timer.setSingleShot(True)
                self._hide_timer.setInterval(2600)
                self._hide_timer.timeout.connect(self._hide_controls)
            self._hide_timer.start()
        else:
            if hasattr(self, '_hide_timer'):
                self._hide_timer.stop()

    def _hide_controls(self):
        if self.player.playbackState() != QMediaPlayer.PlayingState:
            return
        if self.overlay.underMouse():
            self.wake_controls()
            return
        self.overlay_fader.to(0.0)
        self.title_fader.to(0.0)
        self.video.setCursor(Qt.BlankCursor)

    # ---------- 全屏 ----------
    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self.split.widget(0).show()
        else:
            self.split.widget(0).hide()
            self.showFullScreen()
        self.overlay.fs_btn.setIcon(make_icon(
            'fullscreen-exit' if self.isFullScreen() else 'fullscreen'))
        self.wake_controls()

    def exit_fullscreen(self):
        if self.isFullScreen():
            self.toggle_fullscreen()

    # ---------- 配置/历史 ----------
    def _load_cfg(self):
        try:
            return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def _write_cfg(self, cfg):
        try:
            CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False),
                                   encoding='utf-8')
        except OSError:
            pass

    def _load_history(self):
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding='utf-8'))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_history(self):
        try:
            HISTORY_FILE.write_text(
                json.dumps(self.history[-HISTORY_MAX:], ensure_ascii=False),
                encoding='utf-8')
        except OSError:
            pass

    def closeEvent(self, event):
        self._flush_progress(force=True)
        self._save_history()
        for j in self._jobs:
            j.kill()
        self.worker.stop()
        self.worker.wait(6000)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setFont(QFont('Microsoft YaHei UI', 10))
    app.setStyleSheet(STYLE)
    win = VideoPlayer()
    win.show()
    enable_dark_title_bar(win)
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
