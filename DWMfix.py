import sys
import win32gui
import win32api
import win32con
from PyQt6 import QtCore, QtGui, QtWidgets

class StealthWorker(QtWidgets.QWidget):
    """Графически активное окно для принудительной стимуляции DWM."""
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint |
            QtCore.Qt.WindowType.WindowStaysOnTopHint |
            QtCore.Qt.WindowType.Tool |
            QtCore.Qt.WindowType.WindowTransparentForInput |
            QtCore.Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowOpacity(0.01) # 1% видимости для DWM слоя
        
        self.base_width = 200
        self.base_height = 10
        self.boost_size = 400
        self.is_boosted = False
        
        self.setFixedSize(self.base_width, self.base_height)
        
        self.offset = 0
        self.render_timer = QtCore.QTimer(self)
        # ВАЖНО: Используем repaint() для немедленной отрисовки в обход оптимизаций 24H2
        self.render_timer.timeout.connect(self.repaint)
        
        self.move_to_secondary()

    def set_boost(self, enabled):
        self.is_boosted = enabled
        if enabled:
            self.setFixedSize(self.boost_size, self.boost_size)
        else:
            self.setFixedSize(self.base_width, self.base_height)
        self.move_to_secondary()

    def move_to_secondary(self):
        try:
            monitors = win32api.EnumDisplayMonitors()
            if len(monitors) > 1:
                rect = monitors[1][2]
                x = rect[0] + (rect[2] - rect[0]) // 2 - self.width() // 2
                y = rect[1] + (rect[3] - rect[1]) // 2 - self.height() // 2
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
            # Режим Boost: Многоточечный градиент (высокая энтропия для DWM)
            gradient = QtGui.QConicalGradient(self.rect().center(), self.offset)
            # Добавляем 12 точек смены цвета для создания сложной структуры слоя
            for i in range(13):
                hue = (self.offset + (i * 30)) % 360
                gradient.setColorAt(i / 12, QtGui.QColor.fromHsv(hue, 200, 255))
            painter.fillRect(self.rect(), gradient)
        else:
            # Обычный режим: Стандартный бегущий градиент
            gradient = QtGui.QLinearGradient(0, 0, self.width(), self.height())
            color1 = QtGui.QColor.fromHsv(self.offset, 200, 255)
            color2 = QtGui.QColor.fromHsv((self.offset + 180) % 360, 200, 255)
            gradient.setColorAt(0, color1)
            gradient.setColorAt(1, color2)
            painter.fillRect(self.rect(), gradient)

class ControlPanel(QtWidgets.QWidget):
    def __init__(self, worker):
        super().__init__()
        self.worker = worker
        self.init_ui()
        self.init_tray()
        
        # Запуск фикса при старте
        self.start_fix()

    def init_ui(self):
        self.setWindowTitle("DWM Aggressive Fixer")
        self.setFixedSize(320, 250)
        
        self.setStyleSheet("""
            QWidget { background-color: #0c0c0e; color: #d1d1d1; font-family: 'Segoe UI'; }
            QPushButton { 
                background-color: #1a1a1f; border: 1px solid #2a2a2f; 
                padding: 12px; border-radius: 6px; font-weight: bold;
            }
            QPushButton:hover { background-color: #24242b; }
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

        self.ontop_check = QtWidgets.QCheckBox("Stay on Top (Control Panel)")
        self.ontop_check.stateChanged.connect(self.toggle_ontop)
        layout.addWidget(self.ontop_check)

        self.boost_check = QtWidgets.QCheckBox("BOOST MODE (Complex Rendering)")
        self.boost_check.stateChanged.connect(lambda s: self.worker.set_boost(s == 2))
        layout.addWidget(self.boost_check)

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
        if self.worker.render_timer.isActive():
            self.stop_fix()
        else:
            self.start_fix()

    def start_fix(self):
        self.worker.render_timer.start(16)
        # Принудительный "пинок" в иерархии окон
        self.worker.show()
        self.worker.raise_()
        self.status_label.setText("STATUS: ACTIVE")
        self.status_label.setStyleSheet("color: #00c8ff; font-weight: bold;")
        self.btn_toggle.setText("STOP FIX")

    def stop_fix(self):
        self.worker.render_timer.stop()
        self.worker.hide()
        self.status_label.setText("STATUS: STOPPED")
        self.status_label.setStyleSheet("color: #ff4444; font-weight: bold;")
        self.btn_toggle.setText("START FIX")

    def closeEvent(self, event):
        event.ignore()
        self.hide_to_tray()

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    
    worker = StealthWorker()
    panel = ControlPanel(worker)
    panel.show()
    
    sys.exit(app.exec())
