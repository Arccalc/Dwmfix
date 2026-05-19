import sys
import os
import winreg
import win32gui
import win32api
import win32con
from PyQt6 import QtCore, QtGui, QtWidgets

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
    def __init__(self, target_monitor_idx):
        super().__init__()
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint |
            QtCore.Qt.WindowType.WindowStaysOnTopHint |
            QtCore.Qt.WindowType.Tool |
            QtCore.Qt.WindowType.WindowTransparentForInput |
            QtCore.Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowOpacity(0.01) # 1% видимости для DWM слоя (по умолчанию)
        
        self.base_width = 200
        self.base_height = 10
        self.boost_size = 400
        self.is_boosted = False
        self.target_monitor_idx = target_monitor_idx
        
        self.setFixedSize(self.base_width, self.base_height)
        
        self.offset = 0
        self.render_timer = QtCore.QTimer(self)
        self.render_timer.timeout.connect(self.repaint)
        
        self.move_to_monitor()

    def set_visibility(self, visible):
        if visible:
            self.setWindowOpacity(0.9)
        else:
            self.setWindowOpacity(0.01)

    def set_boost(self, enabled):
        self.is_boosted = enabled
        if enabled:
            self.setFixedSize(self.boost_size, self.boost_size)
        else:
            self.setFixedSize(self.base_width, self.base_height)
        self.move_to_monitor()

    def move_to_monitor(self):
        try:
            screens = QtGui.QGuiApplication.screens()
            if self.target_monitor_idx < len(screens):
                rect = screens[self.target_monitor_idx].geometry()
                x = rect.x() + rect.width() // 2 - self.width() // 2
                y = rect.y() + rect.height() // 2 - self.height() // 2
                self.move(x, y)
            else:
                self.move(0, 0)
        except:
            self.move(0, 0)

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

class ControlPanel(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.workers = {}  # {monitor_idx: StealthWorker}
        self.active_monitors = set()
        self.is_boosted = False
        self.is_visible = False
        self.is_active = True
        
        self.init_ui()
        self.init_tray()
        
        # Запуск фикса при старте
        self.start_fix()

    def init_ui(self):
        self.setWindowTitle("DWM Aggressive Fixer")
        self.setFixedSize(320, 390)
        
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
        
        status_box = QtWidgets.QFrame()
        status_box.setObjectName("status_box")
        status_layout = QtWidgets.QVBoxLayout(status_box)
        self.status_label = QtWidgets.QLabel("STATUS: INITIALIZING")
        self.status_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("font-size: 15px; font-weight: bold;")
        status_layout.addWidget(self.status_label)
        layout.addWidget(status_box)

        # Выбор мониторов через CheckableComboBox
        mon_layout = QtWidgets.QHBoxLayout()
        mon_label = QtWidgets.QLabel("Target Monitor:")
        self.mon_combo = CheckableComboBox(self)
        self.update_monitor_list()
        self.mon_combo.itemChecked.connect(self.on_monitor_toggled)
        mon_layout.addWidget(mon_label)
        mon_layout.addWidget(self.mon_combo)
        layout.addLayout(mon_layout)

        self.visible_check = QtWidgets.QCheckBox("Show active area (visual check)")
        self.visible_check.stateChanged.connect(self.toggle_visibility)
        layout.addWidget(self.visible_check)

        self.ontop_check = QtWidgets.QCheckBox("Keep Control Panel on top")
        self.ontop_check.stateChanged.connect(self.toggle_ontop)
        layout.addWidget(self.ontop_check)

        self.boost_check = QtWidgets.QCheckBox("Boost Mode (use if stutter persists)")
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

        self.setLayout(layout)

    def init_tray(self):
        self.tray_icon = QtWidgets.QSystemTrayIcon(self)
        pix = QtGui.QPixmap(16, 16)
        pix.fill(QtGui.QColor("#ff4b2b"))
        self.tray_icon.setIcon(QtGui.QIcon(pix))
        
        menu = QtWidgets.QMenu()
        menu.addAction("Restore Panel").triggered.connect(self.show_and_raise)
        menu.addSeparator()
        menu.addAction("Exit Completely").triggered.connect(QtWidgets.QApplication.quit)
        
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(lambda r: self.show_and_raise() if r == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray_icon.show()

    def hide_to_tray(self):
        self.hide()
        if self.tray_icon.isVisible():
            self.tray_icon.showMessage("DWM Fixer", "Active (Constant Mode)", QtWidgets.QSystemTrayIcon.MessageIcon.Information, 1500)

    def update_monitor_list(self):
        self.mon_combo.clear()
        try:
            screens = QtGui.QGuiApplication.screens()
            model = self.mon_combo.model()
            for i, screen in enumerate(screens):
                rect = screen.geometry()
                ratio = screen.devicePixelRatio()
                w = int(rect.width() * ratio)
                h = int(rect.height() * ratio)
                is_primary = (i == 0)
                
                label = f"Display {i} ({w}x{h})"
                if is_primary:
                    label += " [Primary]"
                    
                item = QtGui.QStandardItem(label)
                item.setCheckable(True)
                
                # По умолчанию выбираем второстепенные мониторы. Если монитор один, то выбираем его.
                should_check = not is_primary or len(screens) == 1
                if should_check:
                    item.setCheckState(QtCore.Qt.CheckState.Checked)
                    self.active_monitors.add(i)
                else:
                    item.setCheckState(QtCore.Qt.CheckState.Unchecked)
                    
                model.appendRow(item)
        except Exception:
            model = self.mon_combo.model()
            item = QtGui.QStandardItem("Default Display")
            item.setCheckable(True)
            item.setCheckState(QtCore.Qt.CheckState.Checked)
            model.appendRow(item)
            self.active_monitors.add(0)

    def on_monitor_toggled(self, idx, checked):
        if checked:
            self.active_monitors.add(idx)
            if self.is_active:
                self.start_worker_for_monitor(idx)
        else:
            self.active_monitors.discard(idx)
            self.stop_worker_for_monitor(idx)

    def start_worker_for_monitor(self, idx):
        if idx not in self.workers:
            worker = StealthWorker(idx)
            worker.set_boost(self.is_boosted)
            worker.set_visibility(self.is_visible)
            worker.render_timer.start(16)
            worker.show()
            worker.raise_()
            self.workers[idx] = worker

    def stop_worker_for_monitor(self, idx):
        if idx in self.workers:
            worker = self.workers[idx]
            worker.render_timer.stop()
            worker.hide()
            worker.deleteLater()
            del self.workers[idx]

    def toggle_visibility(self, state):
        self.is_visible = (state == 2)
        for worker in self.workers.values():
            worker.set_visibility(self.is_visible)

    def toggle_boost(self, state):
        self.is_boosted = (state == 2)
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
        if state == 2:
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
        for idx in self.active_monitors:
            self.start_worker_for_monitor(idx)
        self.status_label.setText("STATUS: ACTIVE")
        self.status_label.setStyleSheet("color: #00c8ff; font-weight: bold;")
        self.btn_toggle.setText("STOP FIX")

    def stop_fix(self):
        self.is_active = False
        for idx in list(self.workers.keys()):
            self.stop_worker_for_monitor(idx)
        self.status_label.setText("STATUS: STOPPED")
        self.status_label.setStyleSheet("color: #ff4444; font-weight: bold;")
        self.btn_toggle.setText("START FIX")

    def closeEvent(self, event):
        event.ignore()
        self.hide_to_tray()

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    
    panel = ControlPanel()
    
    if "--startup" not in sys.argv:
        panel.show()
    else:
        if panel.tray_icon.isVisible():
            panel.tray_icon.showMessage("DWM Fixer", "Started with Windows. Active in background.", QtWidgets.QSystemTrayIcon.MessageIcon.Information, 1500)
    
    sys.exit(app.exec())
