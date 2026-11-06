import sys
import io
import base64

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton,
    QTextEdit, QLineEdit, QSystemTrayIcon, QMenu, QMessageBox,
    QRubberBand, QInputDialog
)
from PyQt6.QtGui import QScreen, QPixmap, QPainter, QColor, QAction, QIcon, QGuiApplication
from PyQt6.QtCore import Qt, QRect, QPoint, QSize, QThread, pyqtSignal, QSettings

import anthropic

APP_NAME = "Claude Quick Snap"
ORG_NAME = "QuickSnap"


# ---------- Settings ----------
def get_settings():
    return QSettings(ORG_NAME, APP_NAME)


def get_api_key():
    return get_settings().value("api_key", "", type=str)


def set_api_key(key):
    get_settings().setValue("api_key", key)


def make_icon():
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#CC785C"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("white"))
    font = p.font()
    font.setBold(True)
    font.setPointSize(26)
    p.setFont(font)
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "C")
    p.end()
    return QIcon(pix)


# ---------- Region selector overlay ----------
class RegionSelector(QWidget):
    """Full-screen transparent overlay for dragging a capture rectangle."""
    region_selected = pyqtSignal(QPixmap)
    cancelled = pyqtSignal()

    def __init__(self, screen: QScreen):
        super().__init__()
        self.screen = screen
        geo = screen.geometry()
        self.setGeometry(geo)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setWindowOpacity(0.35)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setStyleSheet("background-color: black;")

        self.origin = QPoint()
        self.rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self)

        # grab full screenshot now, before dimming
        self.full_pixmap = screen.grabWindow(0)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            self.close()

    def mousePressEvent(self, event):
        self.origin = event.position().toPoint()
        self.rubber_band.setGeometry(QRect(self.origin, QSize()))
        self.rubber_band.show()

    def mouseMoveEvent(self, event):
        if not self.origin.isNull():
            rect = QRect(self.origin, event.position().toPoint()).normalized()
            self.rubber_band.setGeometry(rect)

    def mouseReleaseEvent(self, event):
        rect = QRect(self.origin, event.position().toPoint()).normalized()
        self.rubber_band.hide()
        self.close()

        if rect.width() < 5 or rect.height() < 5:
            self.cancelled.emit()
            return

        dpr = self.full_pixmap.devicePixelRatio()
        crop_rect = QRect(
            int(rect.x() * dpr), int(rect.y() * dpr),
            int(rect.width() * dpr), int(rect.height() * dpr)
        )
        cropped = self.full_pixmap.copy(crop_rect)
        self.region_selected.emit(cropped)


# ---------- Claude API worker ----------
class ClaudeWorker(QThread):
    result_ready = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, image_bytes, prompt, api_key):
        super().__init__()
        self.image_bytes = image_bytes
        self.prompt = prompt
        self.api_key = api_key

    def run(self):
        try:
            client = anthropic.Anthropic(api_key=self.api_key)
            b64 = base64.standard_b64encode(self.image_bytes).decode("utf-8")
            message = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1024,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": b64,
                                },
                            },
                            {
                                "type": "text",
                                "text": self.prompt or "What's in this screenshot?",
                            },
                        ],
                    }
                ],
            )
            text = "".join(
                block.text for block in message.content if block.type == "text"
            )
            self.result_ready.emit(text)
        except Exception as e:
            self.error.emit(str(e))


# ---------- Main window ----------
class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_icon())
        self.resize(560, 640)

        self.pixmap = None
        self.worker = None
        self.selector = None

        layout = QVBoxLayout(self)

        title = QLabel("Claude Quick Snap")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        btn_row = QHBoxLayout()
        self.snap_region_btn = QPushButton("Snap Region")
        self.snap_region_btn.clicked.connect(self.snap_region)
        btn_row.addWidget(self.snap_region_btn)

        self.snap_full_btn = QPushButton("Snap Full Screen")
        self.snap_full_btn.clicked.connect(self.snap_full_screen)
        btn_row.addWidget(self.snap_full_btn)

        self.api_key_btn = QPushButton("Set API Key")
        self.api_key_btn.clicked.connect(self.set_api_key_dialog)
        btn_row.addWidget(self.api_key_btn)
        layout.addLayout(btn_row)

        self.thumb = QLabel("No screenshot yet. Click 'Snap Region' or 'Snap Full Screen'.")
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setMinimumHeight(220)
        self.thumb.setStyleSheet("border: 1px solid #555; border-radius: 6px; color: #888;")
        layout.addWidget(self.thumb)

        prompt_row = QHBoxLayout()
        self.prompt_input = QLineEdit()
        self.prompt_input.setPlaceholderText("Ask Claude about this screenshot...")
        self.prompt_input.returnPressed.connect(self.send_to_claude)
        prompt_row.addWidget(self.prompt_input)

        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send_to_claude)
        self.send_btn.setEnabled(False)
        prompt_row.addWidget(self.send_btn)
        layout.addLayout(prompt_row)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Claude's response will appear here...")
        layout.addWidget(self.output)

        self.status = QLabel("")
        self.status.setStyleSheet("color: gray;")
        layout.addWidget(self.status)

        if not get_api_key():
            self.status.setText("No API key set. Click 'Set API Key' to get started.")

    # --- capture ---
    def snap_full_screen(self):
        screen = QGuiApplication.primaryScreen()
        pixmap = screen.grabWindow(0)
        self.set_pixmap(pixmap)

    def snap_region(self):
        self.hide()
        screen = QGuiApplication.primaryScreen()
        self.selector = RegionSelector(screen)
        self.selector.region_selected.connect(self.on_region_selected)
        self.selector.cancelled.connect(self.on_region_cancelled)
        self.selector.showFullScreen()

    def on_region_selected(self, pixmap):
        self.show()
        self.raise_()
        self.activateWindow()
        self.set_pixmap(pixmap)

    def on_region_cancelled(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def set_pixmap(self, pixmap):
        self.pixmap = pixmap
        scaled = pixmap.scaledToWidth(520, Qt.TransformationMode.SmoothTransformation)
        if scaled.height() > 220:
            scaled = pixmap.scaledToHeight(220, Qt.TransformationMode.SmoothTransformation)
        self.thumb.setPixmap(scaled)
        self.send_btn.setEnabled(True)
        self.prompt_input.setFocus()

    # --- send ---
    def send_to_claude(self):
        if self.pixmap is None:
            return
        api_key = get_api_key()
        if not api_key:
            QMessageBox.warning(self, APP_NAME, "No API key set. Click 'Set API Key' first.")
            return

        buf = io.BytesIO()
        self.pixmap.save(buf, "PNG")
        image_bytes = buf.getvalue()

        prompt = self.prompt_input.text().strip()
        self.send_btn.setEnabled(False)
        self.status.setText("Sending to Claude...")
        self.output.clear()

        self.worker = ClaudeWorker(image_bytes, prompt, api_key)
        self.worker.result_ready.connect(self.on_result)
        self.worker.error.connect(self.on_error)
        self.worker.start()

    def on_result(self, text):
        self.output.setPlainText(text)
        self.status.setText("Done.")
        self.send_btn.setEnabled(True)

    def on_error(self, err):
        self.output.setPlainText(f"Error: {err}")
        self.status.setText("Error.")
        self.send_btn.setEnabled(True)

    def set_api_key_dialog(self):
        current = get_api_key()
        text, ok = QInputDialog.getText(
            self, APP_NAME, "Enter your Anthropic API key:",
            QLineEdit.EchoMode.Password, current
        )
        if ok and text.strip():
            set_api_key(text.strip())
            self.status.setText("API key saved.")


# ---------- App entry point ----------
class QuickSnapApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(True)

        self.window = MainWindow()

        # optional tray icon for quick access, non-essential
        self.tray = QSystemTrayIcon(make_icon())
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        show_action = QAction("Show Claude Quick Snap", menu)
        show_action.triggered.connect(self.show_window)
        menu.addAction(show_action)
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.app.quit)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

        self.window.show()

    def show_window(self):
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_window()

    def run(self):
        sys.exit(self.app.exec())


if __name__ == "__main__":
    QuickSnapApp().run()
