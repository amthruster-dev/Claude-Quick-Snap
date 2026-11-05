import sys
import io
import json
import base64
import threading
import os

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton,
    QTextEdit, QLineEdit, QSystemTrayIcon, QMenu, QMainWindow, QMessageBox,
    QRubberBand
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


# ---------- Region selector overlay ----------
class RegionSelector(QWidget):
    """Full-screen transparent overlay for dragging a capture rectangle."""
    region_selected = pyqtSignal(QPixmap)

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
            return  # too small, treat as cancel

        # crop from the full-resolution pixmap, accounting for device pixel ratio
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


# ---------- Result window ----------
class ResultWindow(QWidget):
    def __init__(self, pixmap: QPixmap):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(520, 600)
        self.pixmap = pixmap

        layout = QVBoxLayout(self)

        # thumbnail preview
        thumb = QLabel()
        scaled = pixmap.scaledToWidth(480, Qt.TransformationMode.SmoothTransformation)
        if scaled.height() > 240:
            scaled = pixmap.scaledToHeight(240, Qt.TransformationMode.SmoothTransformation)
        thumb.setPixmap(scaled)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(thumb)

        prompt_row = QHBoxLayout()
        self.prompt_input = QLineEdit()
        self.prompt_input.setPlaceholderText("Ask Claude about this screenshot...")
        self.prompt_input.returnPressed.connect(self.send_to_claude)
        prompt_row.addWidget(self.prompt_input)

        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send_to_claude)
        prompt_row.addWidget(self.send_btn)
        layout.addLayout(prompt_row)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Claude's response will appear here...")
        layout.addWidget(self.output)

        self.status = QLabel("")
        self.status.setStyleSheet("color: gray;")
        layout.addWidget(self.status)

        self.prompt_input.setFocus()
        self.worker = None

    def send_to_claude(self):
        api_key = get_api_key()
        if not api_key:
            QMessageBox.warning(
                self, APP_NAME,
                "No API key set. Right-click the tray icon and choose 'Set API Key'."
            )
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


# ---------- Tray application ----------
class QuickSnapApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        self.tray = QSystemTrayIcon()
        self.tray.setIcon(self._make_icon())
        self.tray.setToolTip(APP_NAME)

        menu = QMenu()

        snap_full_action = QAction("Snap Full Screen", menu)
        snap_full_action.triggered.connect(self.snap_full_screen)
        menu.addAction(snap_full_action)

        snap_region_action = QAction("Snap Region (drag to select)", menu)
        snap_region_action.triggered.connect(self.snap_region)
        menu.addAction(snap_region_action)

        menu.addSeparator()

        set_key_action = QAction("Set API Key", menu)
        set_key_action.triggered.connect(self.set_api_key_dialog)
        menu.addAction(set_key_action)

        menu.addSeparator()

        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.app.quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

        self.selector = None
        self.result_windows = []

        if not get_api_key():
            self.tray.showMessage(
                APP_NAME,
                "No API key set yet. Right-click the tray icon to add one.",
                QSystemTrayIcon.MessageIcon.Information,
                5000,
            )

    def _make_icon(self):
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

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.snap_region()

    def snap_full_screen(self):
        screen = QGuiApplication.primaryScreen()
        pixmap = screen.grabWindow(0)
        self.open_result_window(pixmap)

    def snap_region(self):
        screen = QGuiApplication.primaryScreen()
        self.selector = RegionSelector(screen)
        self.selector.region_selected.connect(self.open_result_window)
        self.selector.showFullScreen()

    def open_result_window(self, pixmap: QPixmap):
        win = ResultWindow(pixmap)
        self.result_windows.append(win)
        win.show()
        win.raise_()
        win.activateWindow()

    def set_api_key_dialog(self):
        from PyQt6.QtWidgets import QInputDialog
        current = get_api_key()
        text, ok = QInputDialog.getText(
            None, APP_NAME, "Enter your Anthropic API key:",
            QLineEdit.EchoMode.Password, current
        )
        if ok and text.strip():
            set_api_key(text.strip())
            QMessageBox.information(None, APP_NAME, "API key saved.")

    def run(self):
        sys.exit(self.app.exec())


if __name__ == "__main__":
    QuickSnapApp().run()
