import sys
import os
import json
import winreg
import win32gui
import win32api
import win32con
import webbrowser
import tempfile
from PyQt6 import QtCore, QtGui, QtWidgets, QtNetwork

def get_resource_path(relative_path):
    """Возвращает абсолютный путь к ресурсу, учитывая упаковку PyInstaller."""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


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


def resolve_screen(preferred_fp, preferred_idx):
    """Find the preferred screen without blindly clamping index to primary."""
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

    if 0 <= preferred_idx < len(screens):
        return screens[preferred_idx], preferred_idx

    # Preferred offline: prefer a non-primary screen if any remain
    primary = QtGui.QGuiApplication.primaryScreen()
    for i, screen in enumerate(screens):
        if primary is None or screen is not primary:
            return screen, i

    return screens[0], 0


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
        checked_texts = []
        for i in range(self.count()):
            item = self.model().item(i)
            if item and item.checkState() == QtCore.Qt.CheckState.Checked:
                txt = item.text()
                if "Display" in txt:
                    parts = txt.split(" ")
                    if len(parts) > 1:
                        checked_texts.append(parts[1])
                else:
                    checked_texts.append(str(i))
        
        if not checked_texts:
            return "None"
        elif len(checked_texts) == 1:
            return f"Display {checked_texts[0]}"
        else:
            return f"Displays: {', '.join(checked_texts)}"

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

        self.setFixedSize(self.base_width, self.base_height)

        self.offset = 0
        self.render_timer = QtCore.QTimer(self)
        self.render_timer.timeout.connect(self.repaint)

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

    def set_boost(self, enabled):
        self.is_boosted = enabled
        if enabled:
            self.setFixedSize(self.boost_size, self.boost_size)
        else:
            self.setFixedSize(self.base_width, self.base_height)
        self.move_to_monitor()

    def move_to_monitor(self):
        try:
            screen, idx = resolve_screen(self.target_monitor_fp, self.target_monitor_idx)
            if screen is None:
                self.move(0, 0)
                return

            # Keep index in sync when display order changes after reconnect
            if self.target_monitor_fp and score_screen_match(self.target_monitor_fp, screen) >= 25:
                self.target_monitor_idx = idx
                # Refresh fingerprint fields in case name/size updated
                self.target_monitor_fp = screen_fingerprint(screen)

            rect = screen.geometry()
            # Keep custom drag position only if it still lands on the target screen
            if self.custom_pos is not None and rect.contains(self.custom_pos):
                if self.x() != self.custom_pos.x() or self.y() != self.custom_pos.y():
                    self.move(self.custom_pos)
                return

            if self.custom_pos is not None and not rect.contains(self.custom_pos):
                # Topology shifted — drop stale absolute coords
                self.custom_pos = None

            x = rect.x() + rect.width() // 2 - self.width() // 2
            y = rect.y() + rect.height() // 2 - self.height() // 2
            if self.x() != x or self.y() != y:
                self.move(x, y)
        except Exception:
            self.move(0, 0)

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

    def paintEvent(self, event):
        if not self.render_timer.isActive():
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
    update_available = QtCore.pyqtSignal(str, str)  # (version, download_url)

    def __init__(self, current_version):
        super().__init__()
        self.current_version = current_version

    def run(self):
        try:
            import urllib.request
            import json
            
            url = "https://api.github.com/repos/Arccalc/Dwmfix/releases/latest"
            req = urllib.request.Request(url, headers={"User-Agent": "DWMfix-Updater"})
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode('utf-8'))
                    tag_name = data.get("tag_name", "")
                    online_ver = tag_name.strip().lstrip("v")
                    
                    if self.is_newer(online_ver, self.current_version):
                        assets = data.get("assets", [])
                        download_url = None
                        for asset in assets:
                            if asset.get("name") == "DWMfix.zip":
                                download_url = asset.get("browser_download_url")
                                break
                        if not download_url and assets:
                            download_url = assets[0].get("browser_download_url")
                            
                        if download_url:
                            self.update_available.emit(online_ver, download_url)
        except Exception:
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
            return online > current

class DownloadUpdateThread(QtCore.QThread):
    progress = QtCore.pyqtSignal(int)
    finished = QtCore.pyqtSignal(str)  # zip path
    error = QtCore.pyqtSignal(str)

    def __init__(self, download_url):
        super().__init__()
        self.download_url = download_url

    def run(self):
        try:
            import urllib.request
            req = urllib.request.Request(self.download_url, headers={"User-Agent": "DWMfix-Updater"})
            with urllib.request.urlopen(req) as response:
                total_size = int(response.info().get('Content-Length', 0))
                bytes_so_far = 0
                chunk_size = 1024 * 64
                
                temp_zip = os.path.join(tempfile.gettempdir(), "dwmfix_update.zip")
                with open(temp_zip, 'wb') as f:
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        bytes_so_far += len(chunk)
                        if total_size > 0:
                            percent = int(bytes_so_far * 100 / total_size)
                            self.progress.emit(percent)
                
                self.finished.emit(temp_zip)
        except Exception as e:
            self.error.emit(str(e))

class KofiButton(QtWidgets.QPushButton):
    """Печенька на месте прежней кнопки 26×26, подпись слева от неё."""

    _TEXT = "News & Support"
    _SLOT = 22
    _GAP = 5
    _RIGHT = 2
    _HEIGHT = 26

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setToolTip("Ko-fi: news, updates and support")
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setAutoFillBackground(False)
        self.setStyleSheet(
            "QPushButton, QPushButton:hover {"
            "background-color: transparent;"
            "border: none;"
            "padding: 0px;"
            "margin: 0px;"
            "}"
        )
        self._font = QtGui.QFont("Segoe UI")
        self._font.setPixelSize(11)
        self._font.setWeight(QtGui.QFont.Weight.Normal)
        self.setFont(self._font)

        logo_path = get_resource_path("cookielogo.png")
        self._cookie = QtGui.QPixmap()
        if os.path.exists(logo_path):
            self._cookie = QtGui.QPixmap(logo_path).scaled(
                self._SLOT,
                self._SLOT,
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
        self._opaque = self._opaque_box(self._cookie)
        self._ink = QtGui.QFontMetrics(self._font).tightBoundingRect(self._TEXT)
        width, _pm_x, _pm_y, _text_x, _baseline = self._places()
        self.setFixedSize(width, self._HEIGHT)

    def _opaque_box(self, pm):
        if pm.isNull():
            return (0, 0, self._SLOT - 1, self._SLOT - 1)
        img = pm.toImage().convertToFormat(QtGui.QImage.Format.Format_ARGB32)
        left = top = right = bottom = None
        for y in range(img.height()):
            for x in range(img.width()):
                if img.pixelColor(x, y).alpha() > 80:
                    if top is None:
                        top = y
                    bottom = y
                    if left is None or x < left:
                        left = x
                    if right is None or x > right:
                        right = x
        if top is None:
            return (0, 0, max(0, pm.width() - 1), max(0, pm.height() - 1))
        return (left, top, right, bottom)

    def _places(self):
        ink = self._ink
        op_left, op_top, _op_right, op_bottom = self._opaque
        text_x = -ink.x() if ink.x() < 0 else 0
        visual_right = text_x + ink.x() + ink.width()
        pm = self._cookie
        pm_w = pm.width() if not pm.isNull() else self._SLOT
        pm_h = pm.height() if not pm.isNull() else self._SLOT
        pm_x = visual_right + self._GAP - op_left
        pm_y = (self._HEIGHT - self._SLOT) // 2 + (self._SLOT - pm_h) // 2
        width = pm_x + pm_w + self._RIGHT
        opaque_cy = pm_y + (op_top + op_bottom) / 2.0
        # Середина заглавных, без хвоста «p»: иначе надпись сидит выше печеньки.
        cap = QtGui.QFontMetrics(self._font).capHeight()
        baseline = opaque_cy + cap / 2.0
        return int(width), pm_x, pm_y, text_x, baseline

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, True)
        _width, pm_x, pm_y, text_x, baseline = self._places()
        if not self._cookie.isNull():
            painter.drawPixmap(pm_x, pm_y, self._cookie)
        painter.setFont(self._font)
        painter.setPen(QtGui.QColor("#00c8ff" if self.underMouse() else "#d1d1d1"))
        painter.drawText(QtCore.QPointF(text_x, baseline), self._TEXT)

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()


class ControlPanel(QtWidgets.QWidget):
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
        self.version = "1.2.8"
        self.update_thread = UpdateCheckThread(self.version)
        self.update_thread.update_available.connect(self.on_update_available)
        self.update_thread.start()

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

        # Кнопка Ko-fi: печенька на прежнем месте, подпись слева
        self.btn_coffee = KofiButton(self)
        self.btn_coffee.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl("https://ko-fi.com/pixelcraft404")))

        self.setLayout(layout)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'btn_coffee'):
            x = self.width() - self.btn_coffee.width() - 10
            y = 10
            self.btn_coffee.move(x, y)
            self.btn_coffee.raise_()

    def init_tray(self):
        self.tray_icon = QtWidgets.QSystemTrayIcon(self)
        pix = QtGui.QPixmap(16, 16)
        pix.fill(QtGui.QColor("#ff4b2b"))
        self.tray_icon.setIcon(QtGui.QIcon(pix))
        
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

    def on_update_available(self, online_ver, download_url):
        reply = QtWidgets.QMessageBox.question(
            self,
            "Update Available",
            f"A new version of DWM Fixer ({online_ver}) is available.\nWould you like to download and install it now?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )
        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            if not getattr(sys, 'frozen', False):
                QtWidgets.QMessageBox.information(self, "Update", "Auto-update is only supported for the compiled executable version.")
                return
            self.start_update_download(download_url)

    def start_update_download(self, url):
        self.progress_dialog = QtWidgets.QProgressDialog("Downloading update...", "Cancel", 0, 100, self)
        self.progress_dialog.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        self.progress_dialog.setAutoClose(True)
        self.progress_dialog.show()

        self.download_thread = DownloadUpdateThread(url)
        self.download_thread.progress.connect(self.progress_dialog.setValue)
        self.download_thread.finished.connect(self.install_update)
        self.download_thread.error.connect(self.on_update_error)
        self.download_thread.start()

    def on_update_error(self, err_msg):
        if hasattr(self, 'progress_dialog'):
            self.progress_dialog.close()
        QtWidgets.QMessageBox.critical(self, "Update Error", f"Failed to download update:\n{err_msg}")

    def install_update(self, zip_path):
        if hasattr(self, 'progress_dialog'):
            self.progress_dialog.close()
            
        app_dir = os.path.dirname(sys.executable)
        temp_dir = tempfile.gettempdir()
        extract_dir = os.path.join(temp_dir, "dwmfix_extract")
        bat_path = os.path.join(temp_dir, "dwmfix_updater.bat")
        
        bat_content = f"""@echo off
timeout /t 2 /nobreak > nul
powershell -Command "Expand-Archive -Path '{zip_path}' -DestinationPath '{extract_dir}' -Force"

if exist "{extract_dir}\\DWMfix" (
    xcopy /y /e /q "{extract_dir}\\DWMfix\\*" "{app_dir}"
) else (
    xcopy /y /e /q "{extract_dir}\\*" "{app_dir}"
)

start "" "{os.path.join(app_dir, "DWMfix.exe")}"
del /f /q "{zip_path}"
rmdir /s /q "{extract_dir}"
(goto) 2>nul & del "%~f0"
"""
        try:
            with open(bat_path, "w", encoding="utf-8") as f:
                f.write(bat_content)
                
            import subprocess
            subprocess.Popen(
                [bat_path],
                creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
            )
            QtWidgets.QApplication.quit()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Update Error", f"Failed to start updater:\n{str(e)}")

    def hide_to_tray(self):
        self.hide()
        if self.tray_icon.isVisible():
            self.tray_icon.showMessage("DWM Fixer", "Active (Constant Mode)", QtWidgets.QSystemTrayIcon.MessageIcon.Information, 1500)

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

    def _fp_matches_any_active(self, screen):
        for fp in self.active_fps:
            if score_screen_match(fp, screen) >= 25:
                return True
        return False

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

            for i, screen in enumerate(screens):
                item = QtGui.QStandardItem(screen_label(screen, i))
                item.setCheckable(True)
                item.setData(i, QtCore.Qt.ItemDataRole.UserRole)
                if self._fp_matches_any_active(screen):
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
            _, idx = resolve_screen(fp, 0)
            if idx < 0:
                idx = 0

        worker = StealthWorker(idx, fp)
        worker.set_boost(self.is_boosted)
        pos = self.custom_positions.get(key)
        if pos is None:
            pos = self.custom_positions.get(idx)
        if pos is not None:
            worker.custom_pos = pos
            worker.move(pos)
        worker.set_draggable(self.is_adjusting)
        worker.positionChanged.connect(self.on_worker_position_changed)
        worker.render_timer.start(16)
        worker.show()
        worker.raise_()
        self.workers[key] = worker

    def stop_worker_for_fp(self, fp):
        key = fingerprint_key(fp)
        if key in self.workers:
            worker = self.workers[key]
            worker.render_timer.stop()
            worker.hide()
            worker.deleteLater()
            del self.workers[key]
        # Also stop any worker that currently matches this screen identity
        for wkey, worker in list(self.workers.items()):
            if worker.target_monitor_fp and fingerprint_key(worker.target_monitor_fp) == key:
                worker.render_timer.stop()
                worker.hide()
                worker.deleteLater()
                del self.workers[wkey]

    def reaffirm_workers(self):
        """Re-resolve preferred monitors after sleep/reconnect and keep workers on them."""
        if not self.is_active:
            return

        # Desired keys from saved active fingerprints
        desired_keys = set()
        for fp in list(self.active_fps):
            screen, idx = resolve_screen(fp, 0)
            if screen is None:
                continue
            # Only count as online if we matched the preferred panel, or it's a temporary fallback
            matched = score_screen_match(fp, screen) >= 25
            if matched:
                live_fp = screen_fingerprint(screen)
                # Update stored fp with live fields (name/size may change slightly)
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
                desired_keys.add(fingerprint_key(live_fp))
                self.start_worker_for_fp(live_fp, idx)
            else:
                # Preferred offline — keep worker parked on temporary screen if already running
                key = fingerprint_key(fp)
                desired_keys.add(key)
                if key in self.workers:
                    self.workers[key].move_to_monitor()
                else:
                    self.start_worker_for_fp(fp, idx)

        # Drop workers that are no longer preferred
        for key in list(self.workers.keys()):
            if key not in desired_keys:
                worker = self.workers[key]
                worker.render_timer.stop()
                worker.hide()
                worker.deleteLater()
                del self.workers[key]

        # Nudge remaining workers onto correct geometry
        for worker in self.workers.values():
            worker.move_to_monitor()
            if worker.render_timer.isActive():
                worker.show()
                worker.raise_()

    def toggle_adjustment(self, state):
        self.is_adjusting = (state == 2)
        for worker in self.workers.values():
            worker.set_draggable(self.is_adjusting)

    def toggle_boost(self, state):
        self.is_boosted = (state == 2)
        self.settings.setValue("is_boosted", self.is_boosted)
        for worker in self.workers.values():
            worker.set_boost(self.is_boosted)

    def is_startup_enabled(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
            value, _ = winreg.QueryValueEx(key, "DWMfix")
            winreg.CloseKey(key)
            return True
        except WindowsError:
            return False

    def toggle_startup(self, state):
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        
        if getattr(sys, 'frozen', False):
            exe_path = f'"{sys.executable}" --startup'
        else:
            script_path = os.path.abspath(sys.argv[0])
            exe_path = f'"{sys.executable}" "{script_path}" --startup'
            
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
            if state == 2:
                winreg.SetValueEx(key, "DWMfix", 0, winreg.REG_SZ, exe_path)
                try:
                    approve_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run", 0, winreg.KEY_ALL_ACCESS)
                    winreg.DeleteValue(approve_key, "DWMfix")
                    winreg.CloseKey(approve_key)
                except Exception:
                    pass
            else:
                try:
                    winreg.DeleteValue(key, "DWMfix")
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception:
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
        self.status_label.setText("STATUS: ACTIVE")
        self.status_label.setStyleSheet("color: #00c8ff; font-weight: bold;")
        self.btn_toggle.setText("STOP FIX")

    def stop_fix(self):
        self.is_active = False
        for key in list(self.workers.keys()):
            worker = self.workers[key]
            worker.render_timer.stop()
            worker.hide()
            worker.deleteLater()
            del self.workers[key]
        self.status_label.setText("STATUS: STOPPED")
        self.status_label.setStyleSheet("color: #ff4444; font-weight: bold;")
        self.btn_toggle.setText("START FIX")

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
            panel.tray_icon.showMessage("DWM Fixer", "Started with Windows. Active in background.", QtWidgets.QSystemTrayIcon.MessageIcon.Information, 1500)
    
    sys.exit(app.exec())
