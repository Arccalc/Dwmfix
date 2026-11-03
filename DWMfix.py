import sys
import os
import json
import ctypes
import hashlib
import math
import random
import shutil
import threading
import winreg
import win32gui
import win32api
import win32con
import webbrowser
import tempfile
from ctypes import wintypes
from PyQt6 import QtCore, QtGui, QtWidgets, QtNetwork

UPDATE_API_URL = "https://api.github.com/repos/Arccalc/Dwmfix/releases/latest"
RELEASE_PAGE_URL = "https://github.com/Arccalc/Dwmfix/releases/latest"
INSTALLER_URL_PREFIX = "https://github.com/Arccalc/Dwmfix/releases/download/"
UPDATE_TEMP_PREFIX = "dwmfix-update-"

def get_resource_path(relative_path):
    """Возвращает абсолютный путь к ресурсу, учитывая упаковку PyInstaller."""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def load_cookie_pixmap(size):
    """The logo cookie alone, square and without the soft glow around it."""
    image = QtGui.QImage(get_resource_path("cookielogo.png"))
    if image.isNull():
        body = QtGui.QPixmap(size, size)
        body.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(body)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor("#dd8b42"))
        painter.drawEllipse(0, 0, size, size)
        painter.end()
        return body
    mask = QtGui.QBitmap.fromImage(image.createAlphaMask(QtCore.Qt.ImageConversionFlag.ThresholdAlphaDither))
    full = QtGui.QPixmap.fromImage(image)
    full.setMask(mask)
    return full.copy(QtGui.QRegion(mask).boundingRect()).scaled(
        size,
        size,
        QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
        QtCore.Qt.TransformationMode.SmoothTransformation,
    )


def screen_fingerprint(screen):
    """Stable-ish identity for a monitor across sleep/reconnect.

    Index and sometimes \\\\.\\DISPLAYn can change when a panel powers off/on.
    Prefer serial/model/name + size; fall back to whatever Qt exposes.
    """
    g = screen.geometry()
    return {
        "name": (screen.name() or "").strip(),
        "manufacturer": (screen.manufacturer() or "").strip(),
        "model": (screen.model() or "").strip(),
        "serial": (screen.serialNumber() or "").strip(),
        "w": int(g.width()),
        "h": int(g.height()),
    }


def fingerprint_key(fp):
    if not fp:
        return ""
    if fp.get("serial"):
        return f"serial:{fp['serial']}"
    if fp.get("model"):
        return f"model:{fp['model']}:{fp.get('w')}x{fp.get('h')}"
    if fp.get("name"):
        return f"name:{fp['name']}:{fp.get('w')}x{fp.get('h')}"
    return f"size:{fp.get('w')}x{fp.get('h')}"


def score_screen_match(fp, screen):
    """Higher score = better match to stored preferred monitor."""
    if not fp:
        return 0
    other = screen_fingerprint(screen)
    score = 0
    if fp.get("serial") and other["serial"] and fp["serial"] == other["serial"]:
        score += 100
    if fp.get("model") and other["model"] and fp["model"] == other["model"]:
        score += 40
    if fp.get("manufacturer") and other["manufacturer"] and fp["manufacturer"] == other["manufacturer"]:
        score += 15
    if fp.get("name") and other["name"] and fp["name"] == other["name"]:
        score += 25
    if fp.get("w") == other["w"] and fp.get("h") == other["h"]:
        score += 10
    return score


def resolve_screen(preferred_fp, preferred_idx, allow_fallback=True):
    """Find the preferred screen without blindly clamping index to primary.

    allow_fallback=False: return nothing when the saved monitor is unplugged.
    A missing panel must not borrow the laptop.
    """
    screens = list(QtGui.QGuiApplication.screens())
    if not screens:
        return None, -1

    if preferred_fp:
        best_i = -1
        best_score = 0
        for i, screen in enumerate(screens):
            s = score_screen_match(preferred_fp, screen)
            if s > best_score:
                best_score = s
                best_i = i
        # Require a meaningful match (not only resolution coincidence alone)
        if best_i >= 0 and best_score >= 25:
            return screens[best_i], best_i
        if not allow_fallback:
            return None, -1

    if allow_fallback and 0 <= preferred_idx < len(screens):
        return screens[preferred_idx], preferred_idx

    if not allow_fallback:
        return None, -1

    # Preferred offline: prefer a non-primary screen if any remain
    primary = QtGui.QGuiApplication.primaryScreen()
    for i, screen in enumerate(screens):
        if primary is None or screen is not primary:
            return screen, i

    return screens[0], 0


def claim_screen_indexes(active_fps, screens):
    """One saved monitor claims at most one connected screen.

    An unplugged entry claims nothing, so its line is not drawn on the laptop.
    """
    used = set()
    claimed = {}
    for fp in active_fps:
        if not isinstance(fp, dict):
            continue
        best_i = -1
        best_score = 0
        for i, screen in enumerate(screens):
            if i in used:
                continue
            score = score_screen_match(fp, screen)
            if score > best_score:
                best_score = score
                best_i = i
        if best_i >= 0 and best_score >= 25:
            used.add(best_i)
            claimed[best_i] = fp
    return claimed


def screen_label(screen, index):
    fp = screen_fingerprint(screen)
    pretty = fp["model"] or fp["name"] or f"Display {index + 1}"
    if pretty.startswith("\\\\.\\"):
        pretty = f"Display {index + 1}"
    rect = screen.geometry()
    ratio = screen.devicePixelRatio()
    w = int(rect.width() * ratio)
    h = int(rect.height() * ratio)
    primary = QtGui.QGuiApplication.primaryScreen()
    suffix = " [Primary]" if primary is not None and screen is primary else ""
    return f"{pretty} ({w}x{h}){suffix}"


def fallback_interval_ms(refresh_hz):
    """Период запасного таймера, если кадр монитора недоступен."""
    try:
        hz = float(refresh_hz)
    except (TypeError, ValueError):
        return 16
    if hz < 40.0:
        return 16
    return max(1, int(round(1000.0 / hz)))


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [
        ("DeviceName", ctypes.c_wchar * 32),
        ("DesktopCoordinates", _RECT),
        ("AttachedToDesktop", wintypes.BOOL),
        ("Rotation", ctypes.c_uint),
        ("Monitor", ctypes.c_void_p),
    ]


_HRESULT = ctypes.c_long
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_dxgi = ctypes.WinDLL("dxgi", use_last_error=True)
_ole32 = ctypes.WinDLL("ole32", use_last_error=True)

_MonitorFromWindow = _user32.MonitorFromWindow
_MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_MonitorFromWindow.restype = ctypes.c_void_p

_GetMonitorInfoW = _user32.GetMonitorInfoW
_GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
_GetMonitorInfoW.restype = wintypes.BOOL

_CoInitializeEx = _ole32.CoInitializeEx
_CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_CoInitializeEx.restype = _HRESULT

_CoUninitialize = _ole32.CoUninitialize
_CoUninitialize.argtypes = []
_CoUninitialize.restype = None

_IID_IDXGIFactory = _GUID(
    0x7B7166EC, 0x21C7, 0x44AE,
    (ctypes.c_ubyte * 8)(0xB2, 0x1A, 0xC9, 0xAE, 0x32, 0x1A, 0xE3, 0x69),
)
_CreateDXGIFactory = _dxgi.CreateDXGIFactory
_CreateDXGIFactory.argtypes = [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
_CreateDXGIFactory.restype = _HRESULT

# RPC_E_CHANGED_MODE: поток уже в другом апартаменте, свой CoUninitialize не нужен.
_RPC_E_CHANGED_MODE = 0x80010106


def _monitor_from_window(hwnd):
    if not hwnd:
        return 0
    return int(_MonitorFromWindow(hwnd, 2) or 0)


def _monitor_rect(hmonitor):
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not _GetMonitorInfoW(ctypes.c_void_p(int(hmonitor)), ctypes.byref(info)):
        return None
    rect = info.rcMonitor
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def _vt(ptr, index, restype, argtypes):
    obj = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))
    table = ctypes.cast(obj[0], ctypes.POINTER(ctypes.c_void_p))
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(table[index])


def _release(ptr):
    if ptr:
        _vt(ptr, 2, ctypes.c_ulong, [])(ptr)


def _find_output(factory, hmonitor):
    """Выход DXGI этого монитора. Чужие выходы сразу отпускаются."""
    target = _monitor_rect(hmonitor)
    found = None
    for adapter_index in range(8):
        adapter = ctypes.c_void_p()
        hr = _vt(factory, 7, _HRESULT, [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)])(
            factory, adapter_index, ctypes.byref(adapter)
        )
        if hr != 0 or not adapter.value:
            break
        try:
            for output_index in range(8):
                output = ctypes.c_void_p()
                hr = _vt(adapter, 7, _HRESULT, [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)])(
                    adapter, output_index, ctypes.byref(output)
                )
                if hr != 0 or not output.value:
                    break
                desc = _DXGI_OUTPUT_DESC()
                hr = _vt(output, 7, _HRESULT, [ctypes.POINTER(_DXGI_OUTPUT_DESC)])(
                    output, ctypes.byref(desc)
                )
                rect = (
                    int(desc.DesktopCoordinates.left),
                    int(desc.DesktopCoordinates.top),
                    int(desc.DesktopCoordinates.right),
                    int(desc.DesktopCoordinates.bottom),
                )
                same = hr == 0 and (
                    int(desc.Monitor or 0) == int(hmonitor) or (target is not None and rect == target)
                )
                if same and found is None:
                    found = output
                    break
                _release(output)
        finally:
            _release(adapter)
        if found is not None:
            break
    return found


class _OutputVBlank:
    """Ждёт гашение одного монитора. Режим, гамму и профиль не трогает."""

    def __init__(self, hmonitor):
        self._output = None
        self._uninit = False
        self._open(int(hmonitor or 0))

    def ok(self):
        return self._output is not None

    def wait(self):
        output = self._output
        if not output:
            return False
        return _vt(output, 10, _HRESULT, [])(output) == 0

    def close(self):
        output = self._output
        self._output = None
        if output:
            _release(output)
        if self._uninit:
            self._uninit = False
            _CoUninitialize()

    def _open(self, hmonitor):
        if not hmonitor:
            return
        hr = _CoInitializeEx(None, 0)
        if hr < 0 and (hr & 0xFFFFFFFF) != _RPC_E_CHANGED_MODE:
            return
        self._uninit = hr >= 0
        factory = ctypes.c_void_p()
        hr = _CreateDXGIFactory(ctypes.byref(_IID_IDXGIFactory), ctypes.byref(factory))
        if hr != 0 or not factory.value:
            return
        try:
            self._output = _find_output(factory, hmonitor)
        finally:
            _release(factory)


class VBlankPacer(QtCore.QObject):
    """Один импульс на каждое обновление того монитора, где лежит окно."""

    fired = QtCore.pyqtSignal()
    failed = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gen = 0
        self._stop = None
        self._lock = threading.Lock()
        self.pending = threading.Event()

    def start(self, hwnd):
        self.stop()
        self._gen += 1
        gen = self._gen
        stop = threading.Event()
        self._stop = stop
        threading.Thread(
            target=self._run,
            args=(int(hwnd), stop, gen),
            daemon=True,
            name="dwmfix-vblank",
        ).start()

    def stop(self):
        self._gen += 1
        stop = self._stop
        if stop is not None:
            stop.set()
        with self._lock:
            self.pending.clear()

    def _emit(self, signal, stop, gen):
        with self._lock:
            if stop.is_set() or gen != self._gen:
                return
            if signal is self.fired:
                self.pending.set()
            try:
                signal.emit()
            except RuntimeError:
                return

    def _run(self, hwnd, stop, gen):
        waiter = None
        try:
            hmon = _monitor_from_window(hwnd)
            waiter = _OutputVBlank(hmon)
            if not waiter.ok():
                self._emit(self.failed, stop, gen)
                return
            while not stop.is_set() and gen == self._gen:
                if not waiter.wait():
                    self._emit(self.failed, stop, gen)
                    return
                if stop.is_set() or gen != self._gen or self.pending.is_set():
                    continue
                self._emit(self.fired, stop, gen)
        except Exception:
            self._emit(self.failed, stop, gen)
        finally:
            if waiter is not None:
                waiter.close()


class CheckableComboBox(QtWidgets.QComboBox):
    """Выпадающий список с чекбоксами для выбора нескольких мониторов."""
    itemChecked = QtCore.pyqtSignal(int, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModel(QtGui.QStandardItemModel(self))
        self.view().pressed.connect(self.handle_item_pressed)

    def handle_item_pressed(self, index):
        item = self.model().itemFromIndex(index)
        if item.checkState() == QtCore.Qt.CheckState.Checked:
            item.setCheckState(QtCore.Qt.CheckState.Unchecked)
        else:
            item.setCheckState(QtCore.Qt.CheckState.Checked)
        
        is_checked = (item.checkState() == QtCore.Qt.CheckState.Checked)
        self.itemChecked.emit(index.row(), is_checked)
        self.update() # Перерисовываем для обновления отображаемого текста

    def hidePopup(self):
        # Если кликнули внутри выпадающего списка (курсор мыши над ним), не закрываем
        view = self.view()
        if view and (view.underMouse() or view.viewport().underMouse()):
            return
        super().hidePopup()

    def get_display_text(self):
        # Name of the checked monitor as in the list ("Mi 27 NFGL", "Display 2"), without
        # the resolution; a bare list index used to show up for monitors named by model.
        names = []
        for i in range(self.count()):
            item = self.model().item(i)
            if item and item.checkState() == QtCore.Qt.CheckState.Checked:
                names.append(item.text().split(" (")[0])

        if not names:
            return "None"
        elif len(names) == 1:
            return names[0]
        else:
            return f"{names[0]} +{len(names) - 1}"

    def paintEvent(self, event):
        painter = QtWidgets.QStylePainter(self)
        option = QtWidgets.QStyleOptionComboBox()
        self.initStyleOption(option)
        option.currentText = self.get_display_text()
        painter.drawComplexControl(QtWidgets.QStyle.ComplexControl.CC_ComboBox, option)
        painter.drawControl(QtWidgets.QStyle.ControlElement.CE_ComboBoxLabel, option)

class StealthWorker(QtWidgets.QWidget):
    """Графически активное окно для принудительной стимуляции DWM на конкретном мониторе."""
    # idx, pos, fingerprint_key — key stabilizes identity across display reconnects
    positionChanged = QtCore.pyqtSignal(int, QtCore.QPoint, str)

    def __init__(self, target_monitor_idx, target_monitor_fp=None):
        super().__init__()
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint |
            QtCore.Qt.WindowType.WindowStaysOnTopHint |
            QtCore.Qt.WindowType.Tool |
            QtCore.Qt.WindowType.WindowTransparentForInput |
            QtCore.Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowOpacity(0.01)  # 1% видимости для DWM слоя (по умолчанию)

        self.base_width = 200
        self.base_height = 10
        self.boost_size = 400
        self.is_boosted = False
        self.target_monitor_idx = target_monitor_idx
        self.target_monitor_fp = target_monitor_fp
        if self.target_monitor_fp is None:
            screens = QtGui.QGuiApplication.screens()
            if 0 <= target_monitor_idx < len(screens):
                self.target_monitor_fp = screen_fingerprint(screens[target_monitor_idx])

        self.custom_pos = None
        self.draggable = False
        self.on_target = False

        self.setFixedSize(self.base_width, self.base_height)

        self.offset = 0
        self._pacing = False
        self._pace_hmon = 0
        self._pacer = VBlankPacer(self)
        self._pacer.fired.connect(self._on_vblank)
        self._pacer.failed.connect(self._on_pace_failed)
        self.render_timer = QtCore.QTimer(self)
        self.render_timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self.render_timer.timeout.connect(self._on_vblank)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop_pacing)

        self.move_to_monitor()

    def fp_key(self):
        return fingerprint_key(self.target_monitor_fp)

    def update_opacity(self):
        if self.draggable:
            self.setWindowOpacity(0.9)
        else:
            self.setWindowOpacity(0.01)

    def set_draggable(self, enabled):
        self.draggable = enabled
        if enabled:
            # Разрешаем взаимодействие для перетаскивания
            self.setWindowFlags(self.windowFlags() & ~QtCore.Qt.WindowType.WindowTransparentForInput)
            self.setCursor(QtCore.Qt.CursorShape.SizeAllCursor)
        else:
            # Возвращаем полную прозрачность для кликов
            self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowType.WindowTransparentForInput)
            self.unsetCursor()
        self.update_opacity()
        self.show()
        self._follow_monitor()

    def set_boost(self, enabled):
        self.is_boosted = enabled
        if enabled:
            self.setFixedSize(self.boost_size, self.boost_size)
        else:
            self.setFixedSize(self.base_width, self.base_height)
        self.move_to_monitor()

    def _pin_to_screen(self, screen, x, y):
        try:
            if self.windowHandle() is None:
                self.create()
            handle = self.windowHandle()
            if handle is not None and handle.screen() is not screen:
                handle.setScreen(screen)
        except Exception:
            pass
        if self.x() != x or self.y() != y:
            self.move(x, y)

    def move_to_monitor(self):
        self.on_target = False
        try:
            screen, idx = resolve_screen(
                self.target_monitor_fp,
                self.target_monitor_idx,
                allow_fallback=False,
            )
            if screen is None:
                self.hide()
                return

            self.on_target = True
            self.target_monitor_idx = idx
            self.target_monitor_fp = screen_fingerprint(screen)

            rect = screen.geometry()
            # Keep custom drag position only if it still lands on the target screen
            if self.custom_pos is not None and not rect.contains(self.custom_pos):
                self.custom_pos = None

            if self.custom_pos is not None:
                x = self.custom_pos.x()
                y = self.custom_pos.y()
            else:
                x = rect.x() + rect.width() // 2 - self.width() // 2
                y = rect.y() + rect.height() // 2 - self.height() // 2

            # The whole line stays on this screen, including across the shared edge
            x = max(rect.x(), min(x, rect.x() + rect.width() - self.width()))
            y = max(rect.y(), min(y, rect.y() + rect.height() - self.height()))
            self._pin_to_screen(screen, x, y)
        except Exception:
            self.on_target = False
            self.hide()
        finally:
            if self.on_target:
                self._follow_monitor()

    def mousePressEvent(self, event):
        if self.draggable and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.draggable and event.buttons() == QtCore.Qt.MouseButton.LeftButton:
            new_pos = event.globalPosition().toPoint() - self.drag_pos
            self.move(new_pos)
            self.custom_pos = new_pos
            self.positionChanged.emit(self.target_monitor_idx, new_pos, self.fp_key())
            event.accept()

    def mouseReleaseEvent(self, event):
        if self.draggable and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._follow_monitor()
        super().mouseReleaseEvent(event)

    def is_pacing(self):
        return self._pacing

    def _refresh_hz(self):
        try:
            screen, _idx = resolve_screen(self.target_monitor_fp, self.target_monitor_idx)
            if screen is None:
                return 0.0
            return float(screen.refreshRate() or 0.0)
        except Exception:
            return 0.0

    def start_pacing(self):
        self._pacing = True
        self.render_timer.stop()
        hwnd = int(self.winId() or 0)
        self._pace_hmon = _monitor_from_window(hwnd) if hwnd else 0
        if hwnd:
            self._pacer.start(hwnd)
        else:
            self._on_pace_failed()

    def stop_pacing(self):
        self._pacing = False
        self._pace_hmon = 0
        self.render_timer.stop()
        self._pacer.stop()

    def _follow_monitor(self):
        if not self._pacing:
            return
        hwnd = int(self.winId() or 0)
        hmon = _monitor_from_window(hwnd) if hwnd else 0
        if hmon and self._pace_hmon and hmon != self._pace_hmon:
            self.start_pacing()

    def _on_vblank(self):
        self._pacer.pending.clear()
        if self._pacing:
            self.repaint()

    def _on_pace_failed(self):
        if not self._pacing or self.render_timer.isActive():
            return
        self.render_timer.start(fallback_interval_ms(self._refresh_hz()))

    def paintEvent(self, event):
        if not self._pacing:
            return

        painter = QtGui.QPainter(self)
        self.offset = (self.offset + 4) % 360

        if self.is_boosted:
            gradient = QtGui.QConicalGradient(QtCore.QPointF(self.rect().center()), float(self.offset))
            for i in range(13):
                hue = (self.offset + (i * 30)) % 360
                gradient.setColorAt(i / 12, QtGui.QColor.fromHsv(hue, 200, 255))
            painter.fillRect(self.rect(), gradient)
        else:
            gradient = QtGui.QLinearGradient(0, 0, self.width(), self.height())
            color1 = QtGui.QColor.fromHsv(self.offset, 200, 255)
            color2 = QtGui.QColor.fromHsv((self.offset + 180) % 360, 200, 255)
            gradient.setColorAt(0, color1)
            gradient.setColorAt(1, color2)
            painter.fillRect(self.rect(), gradient)

class UpdateCheckThread(QtCore.QThread):
    update_available = QtCore.pyqtSignal(str, str, str)  # (version, installer_url, sha256)

    def __init__(self, current_version):
        super().__init__()
        self.current_version = current_version

    def run(self):
        self.remove_old_installers()
        try:
            import urllib.request
            import json

            req = urllib.request.Request(UPDATE_API_URL, headers={"User-Agent": "DWMfix-Updater"})
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode('utf-8'))
                    tag_name = data.get("tag_name", "")
                    online_ver = tag_name.strip().lstrip("v")

                    if self.is_newer(online_ver, self.current_version):
                        installer_url, sha256 = self.find_installer(data.get("assets", []))
                        self.update_available.emit(online_ver, installer_url, sha256)
        except Exception:
            pass

    def find_installer(self, assets):
        """Only an installer from this repository with a known checksum is offered for installing."""
        for asset in assets:
            name = asset.get("name", "")
            url = asset.get("browser_download_url", "")
            digest = asset.get("digest") or ""
            if (
                name.startswith("DWMfix-Setup-")
                and name.endswith(".exe")
                and url.startswith(INSTALLER_URL_PREFIX)
                and digest.startswith("sha256:")
            ):
                return url, digest[len("sha256:"):]
        return "", ""

    def remove_old_installers(self):
        try:
            temp_dir = tempfile.gettempdir()
            for name in os.listdir(temp_dir):
                if name.startswith(UPDATE_TEMP_PREFIX):
                    shutil.rmtree(os.path.join(temp_dir, name), ignore_errors=True)
        except OSError:
            pass

    def is_newer(self, online, current):
        try:
            o_parts = [int(p) for p in online.split(".")]
            c_parts = [int(p) for p in current.split(".")]
            max_len = max(len(o_parts), len(c_parts))
            o_parts += [0] * (max_len - len(o_parts))
            c_parts += [0] * (max_len - len(c_parts))
            return o_parts > c_parts
        except Exception:
            return False

class DownloadUpdateThread(QtCore.QThread):
    progress = QtCore.pyqtSignal(int)
    downloaded = QtCore.pyqtSignal(str)  # installer path
    error = QtCore.pyqtSignal(str)

    def __init__(self, download_url, sha256):
        super().__init__()
        self.download_url = download_url
        self.sha256 = sha256

    def run(self):
        try:
            import urllib.request
            req = urllib.request.Request(self.download_url, headers={"User-Agent": "DWMfix-Updater"})
            with urllib.request.urlopen(req, timeout=30) as response:
                if not response.geturl().startswith("https://"):
                    raise ValueError("The download was redirected to an insecure address.")
                total_size = int(response.info().get('Content-Length', 0))
                bytes_so_far = 0
                chunk_size = 1024 * 64
                digest = hashlib.sha256()

                # A new private folder: nothing else can have prepared a file at this path.
                folder = tempfile.mkdtemp(prefix=UPDATE_TEMP_PREFIX)
                installer = os.path.join(folder, "DWMfix-Setup.exe")
                with open(installer, 'wb') as f:
                    while True:
                        if self.isInterruptionRequested():
                            return
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        digest.update(chunk)
                        bytes_so_far += len(chunk)
                        if total_size > 0:
                            percent = int(bytes_so_far * 100 / total_size)
                            self.progress.emit(percent)

            if digest.hexdigest() != self.sha256.lower():
                shutil.rmtree(folder, ignore_errors=True)
                raise ValueError("The downloaded file failed its checksum test and was deleted.")
            if not self.isInterruptionRequested():
                self.downloaded.emit(installer)
        except Exception as e:
            self.error.emit(str(e))

class _CookieCanvas(QtWidgets.QWidget):
    """Paints the cookie over the status box; the mouse goes through to KofiButton."""

    def __init__(self, owner, parent):
        super().__init__(parent)
        self._owner = owner
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        self._owner.paint_on(self)


class KofiButton(QtWidgets.QPushButton):
    """A cookie that rolls out on hover and uncovers its label, then takes a bite of itself.

    A click eats it piece by piece, opens the link, and a new cookie pops up.
    The button itself is only the hit area, it grows along with the rolled cookie.
    The timer runs only while something moves, so at rest this costs nothing.
    """

    activated = QtCore.pyqtSignal()

    _TEXT = "News & Support"
    _TEXT_COLOR = "#f2a864"
    _CRUMB_COLORS = ("#dd8b42", "#f2a864", "#4d1f0e")
    _COOKIE = 18
    _COOKIE_LIFT = 0.5  # lines its centre up with the capitals of the label
    _GAP = 6
    _SLOP = 4
    # Each bite: turn from the first bite in degrees, then distance from the centre and size in radii.
    _BITES = (
        (0.0, 1.0, 0.58),
        (58.0, 0.95, 0.55),
        (-58.0, 0.95, 0.55),
        (18.0, 0.32, 0.6),
        (200.0, 0.2, 1.1),
    )
    _BITE_GROW_S = 0.05
    _BITE_EVERY_S = 0.13
    _RESPAWN_WAIT_S = 0.45
    _POP_S = 0.28
    _OPEN_DELAY_MS = 500  # lets the eating show before the browser covers the panel
    _SPRING_OUT = (10.0, 0.6)  # frequency, damping: overshoots and rocks back
    _SPRING_HOME = (10.0, 0.85)
    _INTRO_DELAY_MS = 700
    _INTRO_HOLD_MS = 2200
    _CANVAS_HEIGHT = 112  # down to the bottom of the status box

    def __init__(self, parent):
        super().__init__(parent)
        self.setToolTip("Ko-fi: news, updates and support")
        self.setAccessibleName(self._TEXT)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        self._font = QtGui.QFont("Segoe UI")
        self._font.setPixelSize(12)
        self._font.setWeight(QtGui.QFont.Weight.DemiBold)
        metrics = QtGui.QFontMetricsF(self._font)
        self._text_width = metrics.horizontalAdvance(self._TEXT)
        self._cap_height = metrics.capHeight()
        self._travel = self._text_width + self._GAP

        self._body = load_cookie_pixmap(self._COOKIE * 8)
        self._sprite = None
        self._sprite_dpr = 0.0

        self._right = 0
        self._row = 0  # vertical centre of the label and the hit area
        self._cy = 0.0
        self._hovered = False
        self._intro = False
        self._intro_played = False
        self._p = 0.0  # 0 = at rest, 1 = rolled out
        self._v = 0.0
        self._arrived = False
        self._bite = 0.0  # bites shown, fractional while one grows
        self._bite_goal = 0
        self._bite_axis = 0.0
        self._eating = False
        self._next_bite_in = 0.0
        self._respawn_in = 0.0
        self._pop = 1.0  # 1 = full size, below 1 = a new cookie is popping up
        self._pop_curve = QtCore.QEasingCurve(QtCore.QEasingCurve.Type.OutBack)
        self._crumbs = []

        self._clock = QtCore.QElapsedTimer()
        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._tick)
        self.clicked.connect(self._chomp)

        self._canvas = _CookieCanvas(self, parent)

    def anchor(self, right, top):
        """Place the resting cookie by its right and top edges."""
        self._right = right
        self._row = top + self._COOKIE // 2
        self._cy = self._row - self._COOKIE_LIFT
        self._canvas.setGeometry(0, 0, self.parentWidget().width(), self._CANVAS_HEIGHT)
        self._sync_hit_area()
        self.raise_()
        self._canvas.raise_()

    def _sprite_for(self, dpr):
        if self._sprite is None or self._sprite_dpr != dpr:
            # Twice the screen pixels keeps the edge clean while rotating.
            side = round(self._COOKIE * dpr * 2)
            self._sprite = self._body.scaled(
                side,
                side,
                QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
            self._sprite.setDevicePixelRatio(dpr * 2)
            self._sprite_dpr = dpr
        return self._sprite

    def _cookie_cx(self):
        return self._right - self._COOKIE / 2 - self._travel * self._p

    def _angle(self):
        # A wheel rolling left without slipping turns counter-clockwise.
        return -math.degrees(self._travel * self._p / (self._COOKIE / 2))

    def _target(self):
        # A cookie being eaten stays where it is until the new one has popped up.
        return 1.0 if self._hovered or self._intro or self._eating else 0.0

    def _sync_hit_area(self):
        left = math.floor(self._cookie_cx() - self._COOKIE / 2 - self._SLOP)
        if self._target():
            # Never pull the area out from under a cursor while the cookie rocks back.
            left = min(left, self.x())
        side = self._COOKIE + self._SLOP * 2
        self.setGeometry(left, self._row - side // 2, self._right + self._SLOP - left, side)

    def _animations_on(self):
        enabled = wintypes.BOOL(True)
        SPI_GETCLIENTAREAANIMATION = 0x1042
        ok = ctypes.windll.user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0)
        return not ok or bool(enabled.value)

    def _kick(self):
        if not self._timer.isActive():
            self._clock.restart()
            self._timer.start()

    def _aim(self):
        if self._animations_on():
            self._kick()
            return
        self._p = self._target()
        self._v = 0.0
        self._sync_hit_area()
        self._canvas.update()

    def _tick(self):
        dt = min(self._clock.restart() / 1000.0, 0.033)
        target = self._target()

        freq, damping = self._SPRING_OUT if target else self._SPRING_HOME
        self._v += (freq * freq * (target - self._p) - 2.0 * damping * freq * self._v) * dt
        self._p += self._v * dt
        if self._p < 0.0:
            # Home is a wall: the cookie taps it and bounces a little.
            self._p = 0.0
            self._v *= -0.3
        settled = abs(self._v) < 0.02 and abs(target - self._p) < 0.002
        if settled:
            self._p = target
            self._v = 0.0

        if target and not self._arrived and self._v <= 0.0 and self._p >= 0.9:
            # Top of the overshoot: it has rolled in, so it takes its bite.
            self._arrived = True
            if not self._bite_goal:
                self._take_bite()
        elif self._p < 0.5:
            self._arrived = False

        if self._eating:
            self._eat_step(dt)
        if self._bite < self._bite_goal:
            self._bite = min(float(self._bite_goal), self._bite + dt / self._BITE_GROW_S)
        elif self._bite > 0.0 and settled and not target:
            # The bite heals once the cookie is back home.
            self._bite_goal = 0
            self._bite = max(0.0, self._bite - dt / 0.3)

        for crumb in self._crumbs:
            crumb["vy"] += 420.0 * dt
            crumb["x"] += crumb["vx"] * dt
            crumb["y"] += crumb["vy"] * dt
            crumb["life"] -= dt / 0.6
        self._crumbs = [crumb for crumb in self._crumbs if crumb["life"] > 0.0]

        self._sync_hit_area()
        self._canvas.update()
        # The end of the eating can change where the cookie should go, so ask again.
        retargeted = self._target() != target
        busy = self._eating or self._bite != self._bite_goal or (self._bite > 0.0 and not target)
        if settled and not retargeted and not busy and not self._crumbs:
            self._timer.stop()

    def _eat_step(self, dt):
        total = len(self._BITES)
        if self._pop < 1.0:
            self._pop = min(1.0, self._pop + dt / self._POP_S)
            self._eating = self._pop < 1.0
        elif self._bite_goal < total:
            self._next_bite_in -= dt
            if self._next_bite_in <= 0.0:
                self._take_bite()
                self._next_bite_in = self._BITE_EVERY_S
        elif self._bite >= total:
            self._respawn_in -= dt
            if self._respawn_in <= 0.0:
                self._bite = 0.0
                self._bite_goal = 0
                self._pop = 0.0

    def _take_bite(self):
        if not self._bite_goal:
            # The first bite is always on the top right, wherever the roll left the cookie.
            self._bite = 0.0
            self._bite_axis = math.radians(-45.0 - self._angle())
        turn, reach, _size = self._BITES[self._bite_goal]
        self._bite_goal += 1
        on_screen = self._bite_axis + math.radians(turn + self._angle())
        x = self._cookie_cx() + reach * self._COOKIE / 2 * math.cos(on_screen)
        y = self._cy + reach * self._COOKIE / 2 * math.sin(on_screen)
        for _ in range(11 if self._bite_goal == len(self._BITES) else 5):
            self._crumbs.append({
                "x": x,
                "y": y,
                "vx": random.uniform(-40.0, 55.0),
                "vy": random.uniform(-95.0, -20.0),
                "size": random.choice((1.5, 2.0, 2.5)),
                "color": random.choice(self._CRUMB_COLORS),
                "life": 1.0,
            })

    def _chomp(self):
        if self._eating:
            return
        if not self._animations_on():
            self.activated.emit()
            return
        self._eating = True
        self._arrived = True
        self._next_bite_in = 0.0
        self._respawn_in = self._RESPAWN_WAIT_S
        QtCore.QTimer.singleShot(self._OPEN_DELAY_MS, self.activated.emit)
        self._kick()

    def _bitten(self, sprite):
        out = QtGui.QPixmap(sprite)
        painter = QtGui.QPainter(out)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_DestinationOut)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor("black"))
        radius = self._COOKIE / 2
        center = QtCore.QPointF(radius, radius)
        for i, (turn, reach, size) in enumerate(self._BITES):
            grown = min(1.0, self._bite - i)
            if grown <= 0.0:
                break
            a = self._bite_axis + math.radians(turn)
            mid = center + QtCore.QPointF(math.cos(a), math.sin(a)) * (reach * radius)
            mouth = radius * size * grown
            painter.drawEllipse(mid, mouth, mouth)
            for tooth in (-66.0, -22.0, 22.0, 66.0):  # tooth marks along the edge facing the centre
                t = a + math.pi + math.radians(tooth)
                painter.drawEllipse(mid + QtCore.QPointF(math.cos(t), math.sin(t)) * mouth, mouth * 0.3, mouth * 0.3)
        painter.end()
        return out

    def paint_on(self, widget):
        painter = QtGui.QPainter(widget)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, True)
        cx = self._cookie_cx()
        radius = self._COOKIE / 2

        if self._p > 0.0:
            # The label shows only behind the cookie.
            painter.setClipRect(QtCore.QRectF(cx, 0.0, widget.width() - cx, widget.height()))
            painter.setFont(self._font)
            painter.setPen(QtGui.QColor(self._TEXT_COLOR))
            baseline = self._row + self._cap_height / 2
            painter.drawText(QtCore.QPointF(self._right - self._text_width, baseline), self._TEXT)
            painter.setClipping(False)

        if self._bite < len(self._BITES):
            sprite = self._sprite_for(widget.devicePixelRatioF())
            if self._bite > 0.0:
                sprite = self._bitten(sprite)
            painter.save()
            painter.translate(cx, self._cy)
            painter.rotate(self._angle())
            if self._pop < 1.0:
                scale = self._pop_curve.valueForProgress(self._pop)
                painter.scale(scale, scale)
            painter.drawPixmap(
                QtCore.QRectF(-radius, -radius, self._COOKIE, self._COOKIE),
                sprite,
                QtCore.QRectF(sprite.rect()),
            )
            painter.restore()

        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        for crumb in self._crumbs:
            color = QtGui.QColor(crumb["color"])
            color.setAlphaF(min(1.0, crumb["life"] * 2.0))
            painter.setBrush(color)
            painter.drawRect(QtCore.QRectF(crumb["x"], crumb["y"], crumb["size"], crumb["size"]))

    def paintEvent(self, event):
        pass

    def enterEvent(self, event):
        super().enterEvent(event)
        self._hovered = True
        self._aim()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._hovered = False
        self._aim()

    def showEvent(self, event):
        super().showEvent(event)
        # One roll on the first opening shows that the label is there.
        if not self._intro_played:
            self._intro_played = True
            if self._animations_on():
                QtCore.QTimer.singleShot(self._INTRO_DELAY_MS, self._start_intro)

    def _start_intro(self):
        if self.isVisible():
            self._intro = True
            self._aim()
            QtCore.QTimer.singleShot(self._INTRO_HOLD_MS, self._end_intro)

    def _end_intro(self):
        self._intro = False
        self._aim()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()
        self._hovered = False
        self._intro = False
        self._p = 0.0
        self._v = 0.0
        self._arrived = False
        self._bite = 0.0
        self._bite_goal = 0
        self._eating = False
        self._pop = 1.0
        self._crumbs = []
        self._sync_hit_area()


class ControlPanel(QtWidgets.QWidget):
    # Fix state: status text, its color, tray message
    _STATES = {
        "active": ("STATUS: ACTIVE", "#00c8ff", "Active (Constant Mode)"),
        "stopped": ("STATUS: STOPPED", "#ff4444", "Stopped. The fix is not running."),
        "not_connected": ("STATUS: NOT CONNECTED", "#ffb02e", "Waiting. The selected monitor is not connected."),
        "no_monitor": ("STATUS: NO MONITOR", "#ffb02e", "Waiting. No monitor is selected."),
    }

    def __init__(self, server=None):
        super().__init__()
        self.server = server
        if self.server:
            self.server.newConnection.connect(self.handle_new_connection)

        self.workers = {}  # {fingerprint_key: StealthWorker}
        self.active_fps = []  # list of monitor fingerprints (stable across reconnect)
        self.is_boosted = False
        self.is_active = True
        self.is_adjusting = False
        self._updating_monitors = False

        self.settings = QtCore.QSettings("PixelCraft", "DWMfix")
        self.custom_positions = {}  # {fingerprint_key or legacy idx: QPoint}
        self.load_settings()

        self.init_ui()
        self.init_tray()
        self.bind_display_events()

        # OLED/power-save can reconnect quietly; reaffirm placement periodically
        self.placement_timer = QtCore.QTimer(self)
        self.placement_timer.setInterval(2000)
        self.placement_timer.timeout.connect(self.reaffirm_workers)
        self.placement_timer.start()

        # Проверка обновлений на старте
        self.version = "1.3.0"
        self.update_thread = UpdateCheckThread(self.version)
        self.update_thread.update_available.connect(self.on_update_available)
        self.update_thread.start()

        # Запись автозагрузки указывает на этот exe, а не на старую сборку
        self.ensure_startup_targets_self()

        # Запуск фикса при старте
        self.start_fix()

    def init_ui(self):
        self.setWindowTitle("DWM Aggressive Fixer")
        self.setFixedSize(320, 435)
        if getattr(self, 'is_ontop', False):
            self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        
        self.setStyleSheet("""
            QWidget { background-color: #0c0c0e; color: #d1d1d1; font-family: 'Segoe UI'; }
            QPushButton { 
                background-color: #1a1a1f; border: 1px solid #2a2a2f; 
                padding: 10px; border-radius: 6px; font-weight: bold;
            }
            QPushButton:hover { background-color: #24242b; }
            QComboBox { 
                background-color: #1a1a1f; border: 1px solid #2a2a2f; 
                padding: 8px; border-radius: 6px; font-weight: bold;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #121216;
                border: 1px solid #2a2a2f;
                selection-background-color: #1a1a1f;
                selection-color: #00c8ff;
                outline: none;
            }
            QCheckBox { margin: 5px 0; font-weight: 500; }
            #status_box { background-color: #121216; border-radius: 8px; padding: 12px; margin-bottom: 10px; }
            #status_box QLabel { background: transparent; }
        """)

        layout = QtWidgets.QVBoxLayout()
        
        self.status_box = QtWidgets.QFrame()
        self.status_box.setObjectName("status_box")
        status_layout = QtWidgets.QVBoxLayout(self.status_box)
        self.status_label = QtWidgets.QLabel("STATUS: INITIALIZING")
        self.status_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("font-size: 15px; font-weight: bold;")
        status_layout.addWidget(self.status_label)
        layout.addWidget(self.status_box)

        # Выбор мониторов через CheckableComboBox
        mon_layout = QtWidgets.QHBoxLayout()
        mon_label = QtWidgets.QLabel("Target Monitor:")
        self.mon_combo = CheckableComboBox(self)
        self.update_monitor_list()
        self.mon_combo.itemChecked.connect(self.on_monitor_toggled)
        mon_layout.addWidget(mon_label)
        mon_layout.addWidget(self.mon_combo)
        layout.addLayout(mon_layout)

        self.adjust_check = QtWidgets.QCheckBox("Show && adjust position (drag && drop)")
        self.adjust_check.stateChanged.connect(self.toggle_adjustment)
        layout.addWidget(self.adjust_check)

        self.ontop_check = QtWidgets.QCheckBox("Keep Control Panel on top")
        self.ontop_check.setChecked(getattr(self, 'is_ontop', False))
        self.ontop_check.stateChanged.connect(self.toggle_ontop)
        layout.addWidget(self.ontop_check)

        self.boost_check = QtWidgets.QCheckBox("Boost Mode (use if stutter persists)")
        self.boost_check.setChecked(getattr(self, 'is_boosted', False))
        self.boost_check.stateChanged.connect(self.toggle_boost)
        layout.addWidget(self.boost_check)

        self.startup_check = QtWidgets.QCheckBox("Start with Windows")
        self.startup_check.setChecked(self.is_startup_enabled())
        self.startup_check.stateChanged.connect(self.toggle_startup)
        layout.addWidget(self.startup_check)

        self.btn_toggle = QtWidgets.QPushButton("STOP FIX")
        self.btn_toggle.clicked.connect(self.toggle_fix)
        layout.addWidget(self.btn_toggle)

        btn_hide = QtWidgets.QPushButton("HIDE TO TRAY")
        btn_hide.clicked.connect(self.hide_to_tray)
        layout.addWidget(btn_hide)

        btn_test = QtWidgets.QPushButton("TEST SMOOTHNESS")
        btn_test.clicked.connect(self.toggle_test_window)
        layout.addWidget(btn_test)

        # Ko-fi button: the cookie alone at rest, see KofiButton
        self.btn_coffee = KofiButton(self)
        self.btn_coffee.activated.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl("https://ko-fi.com/pixelcraft404")))

        self.setLayout(layout)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'btn_coffee'):
            # 8 px from the status box edges, the same as its corner radius
            self.btn_coffee.anchor(self.width() - 19, 19)

    def init_tray(self):
        self.tray_icon = QtWidgets.QSystemTrayIcon(self)
        cookie = load_cookie_pixmap(128)
        icon = QtGui.QIcon()
        for side in (16, 20, 24, 32, 40, 48):
            icon.addPixmap(cookie.scaled(
                side,
                side,
                QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            ))
        self.tray_icon.setIcon(icon)
        
        menu = QtWidgets.QMenu()
        menu.addAction("Restore Panel").triggered.connect(self.show_and_raise)
        menu.addAction("Stealth Mode").triggered.connect(self.activate_stealth_mode)
        menu.addSeparator()
        menu.addAction("Exit Completely").triggered.connect(QtWidgets.QApplication.quit)
        
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(lambda r: self.show_and_raise() if r == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray_icon.show()

    def activate_stealth_mode(self):
        self.tray_icon.hide()
        self.hide()

    def restore_from_stealth(self):
        self.tray_icon.show()
        self.show_and_raise()

    def handle_new_connection(self):
        socket = self.server.nextPendingConnection()
        if socket:
            socket.setParent(self)
            socket.readyRead.connect(lambda s=socket: self.read_socket(s))

    def read_socket(self, socket):
        data = socket.readAll().data()
        if data == b"restore":
            self.restore_from_stealth()
        socket.deleteLater()

    def is_installed_copy(self):
        # A copy unpacked from the zip has no uninstaller; the installer would put a second copy elsewhere.
        if not getattr(sys, 'frozen', False):
            return False
        return os.path.exists(os.path.join(os.path.dirname(sys.executable), "unins000.exe"))

    def on_update_available(self, online_ver, installer_url, sha256):
        if self.settings.value("skipped_version") == online_ver:
            return
        can_install = bool(installer_url) and self.is_installed_copy()

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Update Available")
        box.setIcon(QtWidgets.QMessageBox.Icon.Information)
        box.setText(f"A new version of DWM Fixer ({online_ver}) is available.")
        if can_install:
            box.setInformativeText("DWM Fixer will close, install the update and start again.")
        role = QtWidgets.QMessageBox.ButtonRole
        accept = box.addButton("Install update" if can_install else "Open download page", role.AcceptRole)
        skip = box.addButton("Skip this version", role.ActionRole)
        box.addButton("Later", role.RejectRole)
        box.setDefaultButton(accept)
        box.exec()

        clicked = box.clickedButton()
        if clicked == skip:
            self.settings.setValue("skipped_version", online_ver)
        elif clicked == accept:
            if can_install:
                self.start_update_download(installer_url, sha256)
            else:
                self.open_release_page()

    def open_release_page(self):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(RELEASE_PAGE_URL))

    def start_update_download(self, url, sha256):
        self.progress_dialog = QtWidgets.QProgressDialog("Downloading update...", "Cancel", 0, 100, self)
        self.progress_dialog.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        self.progress_dialog.setAutoClose(True)
        self.progress_dialog.show()

        self.download_thread = DownloadUpdateThread(url, sha256)
        self.progress_dialog.canceled.connect(self.download_thread.requestInterruption)
        self.download_thread.progress.connect(self.progress_dialog.setValue)
        self.download_thread.downloaded.connect(self.install_update)
        self.download_thread.error.connect(self.on_update_error)
        self.download_thread.start()

    def on_update_error(self, err_msg):
        if hasattr(self, 'progress_dialog'):
            self.progress_dialog.close()
        reply = QtWidgets.QMessageBox.warning(
            self,
            "Update Error",
            f"The update was not installed:\n{err_msg}\n\nOpen the download page to get it manually?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )
        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            self.open_release_page()

    def install_update(self, installer_path):
        if hasattr(self, 'progress_dialog'):
            self.progress_dialog.close()
        try:
            import subprocess
            # The new copy is started by the installer and must not inherit this process's bundle paths.
            env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
            subprocess.Popen(
                [installer_path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                cwd=os.path.dirname(installer_path),
                env=env,
            )
            QtWidgets.QApplication.quit()
        except OSError as e:
            self.on_update_error(str(e))

    def hide_to_tray(self):
        self.hide()
        if self.tray_icon.isVisible():
            self.tray_icon.showMessage("DWM Fixer", self.status_message(), QtWidgets.QSystemTrayIcon.MessageIcon.Information, 1500)

    def bind_display_events(self):
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        app.screenAdded.connect(self.on_display_topology_changed)
        app.screenRemoved.connect(self.on_display_topology_changed)
        app.primaryScreenChanged.connect(self.on_display_topology_changed)
        for screen in QtGui.QGuiApplication.screens():
            self._hook_screen(screen)

    def _hook_screen(self, screen):
        screen.geometryChanged.connect(self.on_display_topology_changed)
        screen.availableGeometryChanged.connect(self.on_display_topology_changed)

    def on_display_topology_changed(self, *args):
        for screen in QtGui.QGuiApplication.screens():
            try:
                screen.geometryChanged.disconnect(self.on_display_topology_changed)
            except TypeError:
                pass
            try:
                screen.availableGeometryChanged.disconnect(self.on_display_topology_changed)
            except TypeError:
                pass
            self._hook_screen(screen)

        # Defer so Windows finishes rearranging virtual desktop coords
        QtCore.QTimer.singleShot(300, self.refresh_after_display_change)
        QtCore.QTimer.singleShot(1200, self.refresh_after_display_change)

    def refresh_after_display_change(self):
        self.update_monitor_list(preserve_selection=True)
        self.reaffirm_workers()

    def _add_active_fp(self, fp):
        if not fp:
            return
        key = fingerprint_key(fp)
        for existing in self.active_fps:
            if fingerprint_key(existing) == key:
                return
        self.active_fps.append(fp)
        self.save_active_fps()

    def _remove_active_fp_for_screen(self, screen):
        self.active_fps = [
            fp for fp in self.active_fps
            if score_screen_match(fp, screen) < 25
        ]
        self.save_active_fps()

    def save_active_fps(self):
        try:
            self.settings.setValue("active_fps", json.dumps(self.active_fps))
        except Exception:
            pass

    def update_monitor_list(self, preserve_selection=False):
        self._updating_monitors = True
        try:
            self.mon_combo.clear()
            screens = list(QtGui.QGuiApplication.screens())
            model = self.mon_combo.model()

            # First run / empty selection: default to non-primary (or only screen)
            if not self.active_fps and not preserve_selection:
                primary = QtGui.QGuiApplication.primaryScreen()
                for i, screen in enumerate(screens):
                    is_primary = primary is not None and screen is primary
                    if (not is_primary) or len(screens) == 1:
                        self._add_active_fp(screen_fingerprint(screen))

            claimed = claim_screen_indexes(self.active_fps, screens)
            for i, screen in enumerate(screens):
                item = QtGui.QStandardItem(screen_label(screen, i))
                item.setCheckable(True)
                item.setData(i, QtCore.Qt.ItemDataRole.UserRole)
                if i in claimed:
                    item.setCheckState(QtCore.Qt.CheckState.Checked)
                else:
                    item.setCheckState(QtCore.Qt.CheckState.Unchecked)
                model.appendRow(item)

            if not screens:
                item = QtGui.QStandardItem("Default Display")
                item.setCheckable(True)
                item.setCheckState(QtCore.Qt.CheckState.Checked)
                model.appendRow(item)
                if not self.active_fps:
                    self.active_fps = [{
                        "name": "Default Display",
                        "manufacturer": "",
                        "model": "",
                        "serial": "",
                        "w": 0,
                        "h": 0,
                    }]
        except Exception:
            model = self.mon_combo.model()
            item = QtGui.QStandardItem("Default Display")
            item.setCheckable(True)
            item.setCheckState(QtCore.Qt.CheckState.Checked)
            model.appendRow(item)
        finally:
            self._updating_monitors = False
            self.mon_combo.update()

    def on_monitor_toggled(self, idx, checked):
        if self._updating_monitors:
            return
        screens = list(QtGui.QGuiApplication.screens())
        if not (0 <= idx < len(screens)):
            return
        screen = screens[idx]
        fp = screen_fingerprint(screen)
        if checked:
            self._add_active_fp(fp)
            if self.is_active:
                self.start_worker_for_fp(fp, idx)
        else:
            self._remove_active_fp_for_screen(screen)
            self.stop_worker_for_fp(fp)
        self.update_status()

    def load_settings(self):
        # Load boost mode
        val_boost = self.settings.value("is_boosted")
        if val_boost is not None:
            self.is_boosted = (val_boost == 'true' or val_boost is True)

        # Load stays on top mode
        val_ontop = self.settings.value("is_ontop")
        if val_ontop is not None:
            self.is_ontop = (val_ontop == 'true' or val_ontop is True)
        else:
            self.is_ontop = False

        # Preferred target monitors (fingerprints) — survive OLED sleep / index shuffle
        raw_fps = self.settings.value("active_fps")
        if raw_fps:
            try:
                loaded = json.loads(raw_fps)
                if isinstance(loaded, list):
                    self.active_fps = [fp for fp in loaded if isinstance(fp, dict)]
            except Exception:
                pass

        # Custom positions by fingerprint key; keep legacy index keys for old installs
        for idx in range(10):
            x_val = self.settings.value(f"Positions/{idx}_x")
            y_val = self.settings.value(f"Positions/{idx}_y")
            if x_val is not None and y_val is not None:
                try:
                    self.custom_positions[idx] = QtCore.QPoint(int(x_val), int(y_val))
                except (ValueError, TypeError):
                    pass

        self.settings.beginGroup("PositionsByKey")
        for key in self.settings.childKeys():
            if key.endswith("_x"):
                base = key[:-2]
                x_val = self.settings.value(f"{base}_x")
                y_val = self.settings.value(f"{base}_y")
                if x_val is not None and y_val is not None:
                    try:
                        self.custom_positions[base] = QtCore.QPoint(int(x_val), int(y_val))
                    except (ValueError, TypeError):
                        pass
        self.settings.endGroup()

    def on_worker_position_changed(self, idx, pos, fp_key=""):
        if fp_key:
            self.custom_positions[fp_key] = pos
            self.settings.setValue(f"PositionsByKey/{fp_key}_x", pos.x())
            self.settings.setValue(f"PositionsByKey/{fp_key}_y", pos.y())
        # Legacy index keys (backward compatible)
        self.custom_positions[idx] = pos
        self.settings.setValue(f"Positions/{idx}_x", pos.x())
        self.settings.setValue(f"Positions/{idx}_y", pos.y())

    def start_worker_for_fp(self, fp, idx=None):
        key = fingerprint_key(fp)
        if not key:
            return
        if key in self.workers:
            worker = self.workers[key]
            if idx is not None:
                worker.target_monitor_idx = idx
            worker.target_monitor_fp = fp
            worker.move_to_monitor()
            return

        if idx is None:
            _screen, idx = resolve_screen(fp, -1, allow_fallback=False)
            if idx < 0:
                return

        worker = StealthWorker(idx, fp)
        worker.set_boost(self.is_boosted)
        pos = self.custom_positions.get(key)
        if pos is None:
            pos = self.custom_positions.get(idx)
        if pos is not None:
            worker.custom_pos = pos
        worker.move_to_monitor()
        if not worker.on_target:
            worker.hide()
            worker.deleteLater()
            return
        worker.set_draggable(self.is_adjusting)
        worker.positionChanged.connect(self.on_worker_position_changed)
        worker.show()
        worker.start_pacing()
        worker.raise_()
        self.workers[key] = worker

    def stop_worker_for_fp(self, fp):
        key = fingerprint_key(fp)
        if key in self.workers:
            worker = self.workers[key]
            worker.stop_pacing()
            worker.hide()
            worker.deleteLater()
            del self.workers[key]
        # Also stop any worker that currently matches this screen identity
        for wkey, worker in list(self.workers.items()):
            if worker.target_monitor_fp and fingerprint_key(worker.target_monitor_fp) == key:
                worker.stop_pacing()
                worker.hide()
                worker.deleteLater()
                del self.workers[wkey]

    def reaffirm_workers(self):
        """Re-resolve preferred monitors after sleep/reconnect and keep workers on them."""
        if not self.is_active:
            return

        # Линия только на экране, который реально совпал с сохранённым. Отключённый монитор не занимает ноутбук.
        desired_keys = set()
        screens = list(QtGui.QGuiApplication.screens())
        for idx, fp in claim_screen_indexes(self.active_fps, screens).items():
            screen = screens[idx]
            live_fp = screen_fingerprint(screen)
            key_old = fingerprint_key(fp)
            key_new = fingerprint_key(live_fp)
            if key_old != key_new:
                self.active_fps = [
                    live_fp if fingerprint_key(x) == key_old else x
                    for x in self.active_fps
                ]
                self.save_active_fps()
                if key_old in self.workers and key_new not in self.workers:
                    self.workers[key_new] = self.workers.pop(key_old)
                    self.workers[key_new].target_monitor_fp = live_fp
            desired_keys.add(key_new)
            self.start_worker_for_fp(live_fp, idx)

        # Drop workers that are no longer preferred
        for key in list(self.workers.keys()):
            if key not in desired_keys:
                worker = self.workers[key]
                worker.stop_pacing()
                worker.hide()
                worker.deleteLater()
                del self.workers[key]

        # Nudge remaining workers onto correct geometry
        for worker in self.workers.values():
            worker.move_to_monitor()
            if not worker.on_target:
                worker.hide()
            elif worker.is_pacing():
                worker.show()
                worker.raise_()

        self.update_status()

    def toggle_adjustment(self, state):
        self.is_adjusting = (state == 2)
        for worker in self.workers.values():
            worker.set_draggable(self.is_adjusting)

    def toggle_boost(self, state):
        self.is_boosted = (state == 2)
        self.settings.setValue("is_boosted", self.is_boosted)
        for worker in self.workers.values():
            worker.set_boost(self.is_boosted)

    def _startup_command(self):
        if getattr(sys, "frozen", False):
            return f'"{os.path.abspath(sys.executable)}" --startup'
        script_path = os.path.abspath(sys.argv[0])
        return f'"{sys.executable}" "{script_path}" --startup'

    def _read_startup_command(self):
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_READ,
            )
            try:
                value, _ = winreg.QueryValueEx(key, "DWMfix")
            finally:
                winreg.CloseKey(key)
            return value
        except OSError:
            return None

    def _clear_startup_approval(self):
        try:
            approve_key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run",
                0,
                winreg.KEY_SET_VALUE,
            )
            try:
                winreg.DeleteValue(approve_key, "DWMfix")
            finally:
                winreg.CloseKey(approve_key)
        except OSError:
            pass

    def _write_startup_command(self, command):
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        try:
            winreg.SetValueEx(key, "DWMfix", 0, winreg.REG_SZ, command)
        finally:
            winreg.CloseKey(key)
        self._clear_startup_approval()

    def is_startup_enabled(self):
        return self._read_startup_command() is not None

    def ensure_startup_targets_self(self):
        """Если автозагрузка включена, в реестре должен быть путь этого exe."""
        current = self._read_startup_command()
        if not current:
            return
        desired = self._startup_command()
        if current != desired:
            try:
                self._write_startup_command(desired)
            except OSError:
                pass

    def toggle_startup(self, state):
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
            try:
                if state == 2:
                    winreg.SetValueEx(key, "DWMfix", 0, winreg.REG_SZ, self._startup_command())
                    self._clear_startup_approval()
                else:
                    try:
                        winreg.DeleteValue(key, "DWMfix")
                    except FileNotFoundError:
                        pass
            finally:
                winreg.CloseKey(key)
        except OSError:
            pass

    def show_and_raise(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def toggle_ontop(self, state):
        self.is_ontop = (state == 2)
        self.settings.setValue("is_ontop", self.is_ontop)
        if self.is_ontop:
            self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        else:
            self.setWindowFlags(self.windowFlags() & ~QtCore.Qt.WindowType.WindowStaysOnTopHint)
        self.show()

    def toggle_fix(self):
        if self.is_active:
            self.stop_fix()
        else:
            self.start_fix()

    def start_fix(self):
        self.is_active = True
        # Ensure defaults if nothing selected yet
        if not self.active_fps:
            self.update_monitor_list(preserve_selection=False)
        self.reaffirm_workers()
        self.update_status()
        self.btn_toggle.setText("STOP FIX")

    def stop_fix(self):
        self.is_active = False
        for key in list(self.workers.keys()):
            worker = self.workers[key]
            worker.stop_pacing()
            worker.hide()
            worker.deleteLater()
            del self.workers[key]
        self.update_status()
        self.btn_toggle.setText("START FIX")

    def _fix_state(self):
        if not self.is_active:
            return "stopped"
        if any(worker.on_target and worker.is_pacing() for worker in self.workers.values()):
            return "active"
        return "not_connected" if self.active_fps else "no_monitor"

    def update_status(self):
        text, color, _message = self._STATES[self._fix_state()]
        if self.status_label.text() != text:
            self.status_label.setText(text)
            self.status_label.setStyleSheet(f"color: {color}; font-size: 15px; font-weight: bold;")

    def status_message(self):
        return self._STATES[self._fix_state()][2]

    def toggle_test_window(self):
        html_content = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>DWM Fixer - Judder & Smoothness Analyzer</title>
    <style>
        body {
            margin: 0;
            background-color: #0c0c0e;
            color: #d1d1d1;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100vh;
        }
        .container {
            text-align: center;
            width: 90%;
            max-width: 900px;
        }
        h1 {
            font-size: 26px;
            color: #ffffff;
            margin-bottom: 5px;
            font-weight: bold;
        }
        p {
            font-size: 14px;
            color: #8a8a93;
            margin-bottom: 20px;
        }
        .controls {
            margin-bottom: 15px;
            display: flex;
            justify-content: center;
            gap: 15px;
        }
        .btn {
            background-color: #1a1a1f;
            border: 1px solid #2a2a2f;
            color: #d1d1d1;
            padding: 8px 16px;
            border-radius: 6px;
            font-weight: bold;
            cursor: pointer;
            transition: background-color 0.2s;
        }
        .btn:hover {
            background-color: #24242b;
        }
        .btn.active {
            background-color: #00c8ff;
            color: #0c0c0e;
            border-color: #00c8ff;
        }
        canvas {
            background-color: #000000;
            border: 1px solid #1a1a22;
            border-radius: 8px;
            width: 100%;
            height: 400px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.5);
        }
        .kofi-container {
            position: absolute;
            top: 20px;
            left: 20px;
            z-index: 100;
        }
        @keyframes custom-wiggle {
            0% { transform: rotate(0deg) scale(1); }
            20% { transform: rotate(-10deg) scale(1.1); }
            40% { transform: rotate(10deg) scale(1.1); }
            60% { transform: rotate(-10deg) scale(1.1); }
            80% { transform: rotate(10deg) scale(1.1); }
            100% { transform: rotate(0deg) scale(1); }
        }
        .kofiimg, .kofi-img, img[src*="cup-border"] {
            animation: none !important;
        }
        .kofi-button:hover .kofiimg, 
        .kofi-button:hover .kofi-img,
        .kofi-button:hover img[src*="cup-border"],
        a[href*="ko-fi.com"]:hover img {
            animation: custom-wiggle 0.6s ease-in-out !important;
        }
    </style>
</head>
<body>
    <div class="kofi-container">
        <script type='text/javascript' src='https://storage.ko-fi.com/cdn/widget/Widget_2.js'></script>
        <script type='text/javascript'>
            kofiwidget2.init('Support me on Ko-fi', '#13131a', 'M2C51ZTNGT');
            kofiwidget2.draw();
        </script>
    </div>

    <div class="container">
        <h1>DWM Fixer - Judder & Smoothness Analyzer</h1>
        <p>Drag this window to your second monitor. Toggling the fix will reveal massive differences in scrolling text clarity at 24 FPS.</p>
        
        <div class="controls">
            <button class="btn" id="btn60" onclick="setFps(60)">60 FPS</button>
            <button class="btn" id="btn30" onclick="setFps(30)">30 FPS</button>
            <button class="btn active" id="btn24" onclick="setFps(24)">24 FPS (Movie Judder Test)</button>
        </div>

        <canvas id="testCanvas"></canvas>
    </div>

    <script>
        const canvas = document.getElementById('testCanvas');
        const ctx = canvas.getContext('2d');
        
        let fpsLimit = 24;
        let lastFrameTime = performance.now();
        let lastTickTime = performance.now();
        
        // Scrolling offsets
        let horizontalX = 0;
        let verticalY = 0;
        
        // Speeds (pixels per second)
        const horizontalSpeed = 350;
        const verticalSpeed = 130;

        // Custom instruction text scrolling from bottom to top
        const credits = [
            "SMOOTHNESS TEST INSTRUCTIONS",
            "----------------------------",
            "1. Drag this window to the second monitor.",
            "2. Toggle the fix in the Control Panel.",
            "",
            "When the fix is ACTIVE,",
            "this text will glide smoothly.",
            "When the fix is STOPPED,",
            "the text will stutter and judder.",
            "",
            "The difference is most visible",
            "when running at 24 FPS.",
            "",
            "DWM FIXER ACTIVE MODE TEST"
        ];

        function setFps(fps) {
            fpsLimit = fps;
            document.getElementById('btn60').classList.toggle('active', fps === 60);
            document.getElementById('btn30').classList.toggle('active', fps === 30);
            document.getElementById('btn24').classList.toggle('active', fps === 24);
        }

        function resize() {
            const rect = canvas.getBoundingClientRect();
            canvas.width = rect.width;
            canvas.height = rect.height;
            verticalY = canvas.height - 100;
            horizontalX = canvas.width; // Start offscreen right
        }
        window.addEventListener('resize', resize);
        resize();

        function draw(time) {
            requestAnimationFrame(draw);
            
            const delta = time - lastFrameTime;
            const targetInterval = 1000 / fpsLimit;
            
            if (delta < targetInterval - 0.5) {
                return; // Limit frame rate
            }
            
            const now = performance.now();
            const realDelta = now - lastTickTime;
            lastTickTime = now;
            
            lastFrameTime = time - (delta % targetInterval);

            // Clear canvas to pure black for maximum text contrast
            ctx.fillStyle = '#000000';
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            
            // Draw horizontal ticker track background (pure black)
            ctx.fillStyle = '#000000';
            ctx.fillRect(0, 20, canvas.width, 80);
            
            // Draw a subtle border separating sections
            ctx.strokeStyle = '#1a1a22';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(0, 110);
            ctx.lineTo(canvas.width, 110);
            ctx.stroke();

            // Calculate scrolling based on real time delta
            const dt = realDelta / 1000;
            
            // 1. Horizontal ticker scroll (Big, Bold text)
            const tickerText = "DWM FIXER JUDDER TEST • DWM FIXER JUDDER TEST • DWM FIXER JUDDER TEST";
            ctx.font = 'bold 36px "Segoe UI", Arial';
            const textWidth = ctx.measureText(tickerText).width;
            
            horizontalX -= horizontalSpeed * dt;
            if (horizontalX < -textWidth) {
                horizontalX = canvas.width;
            }
            
            ctx.fillStyle = '#ffffff';
            ctx.fillText(tickerText, horizontalX, 70);
            
            // 2. Vertical Credit Roll (Centered, medium text)
            ctx.font = 'bold 18px "Segoe UI", Arial';
            ctx.textAlign = 'center';
            
            const creditSpacing = 35;
            const totalCreditsHeight = credits.length * creditSpacing;
            
            verticalY -= verticalSpeed * dt;
            if (verticalY < -totalCreditsHeight) {
                verticalY = canvas.height - 120;
            }
            
            credits.forEach((line, index) => {
                const yPos = verticalY + (index * creditSpacing);
                if (yPos > 120 && yPos < canvas.height - 10) {
                    // Highlight header lines
                    if (index === 0 || index === 2 || index === 9) {
                        ctx.fillStyle = '#00c8ff';
                    } else {
                        ctx.fillStyle = '#d1d1d1';
                    }
                    ctx.fillText(line, canvas.width / 2, yPos);
                }
            });
            ctx.textAlign = 'left'; // Reset alignment
        }

        requestAnimationFrame(draw);
    </script>
</body>
</html>
"""
        try:
            temp_path = os.path.join(tempfile.gettempdir(), "dwmfix_smoothness_test.html")
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            webbrowser.open(temp_path)
        except Exception:
            pass

    def closeEvent(self, event):
        event.ignore()
        self.hide_to_tray()

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    
    server_name = "DWMfix_IPC_Server"
    
    # Try to connect to existing local server to detect running instance
    socket = QtNetwork.QLocalSocket()
    socket.connectToServer(server_name)
    if socket.waitForConnected(500):
        # Already running! Send restore command and exit.
        socket.write(b"restore")
        socket.waitForBytesWritten(500)
        sys.exit(0)
        
    # We are the first instance. Create local server
    server = QtNetwork.QLocalServer()
    server.removeServer(server_name)
    server.listen(server_name)
    
    panel = ControlPanel(server)
    
    if "--startup" not in sys.argv:
        panel.show()
    else:
        if panel.tray_icon.isVisible():
            panel.tray_icon.showMessage("DWM Fixer", f"Started with Windows. {panel.status_message()}", QtWidgets.QSystemTrayIcon.MessageIcon.Information, 1500)
    
    sys.exit(app.exec())
