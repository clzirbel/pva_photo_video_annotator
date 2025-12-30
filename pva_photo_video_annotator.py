import sys
import json
import os
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QTextEdit, QPushButton,
    QVBoxLayout, QHBoxLayout, QFileDialog
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt, QTimer, QUrl


SUPPORTED_IMAGES = {".jpg", ".jpeg", ".png", ".bmp", ".gif"}
SUPPORTED_VIDEOS = {".mp4", ".mov", ".avi", ".mkv"}


class MediaAnnotator(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Media Annotator")

        self.media_files = []
        self.current_index = 0
        self.annotations = {}
        self.base_dir = None
        self.scroll_timer = QTimer()
        self.scroll_timer.timeout.connect(self.scroll_text)

        self.build_ui()

    def build_ui(self):
        self.media_label = QLabel("Open a folder to begin")
        self.media_label.setAlignment(Qt.AlignCenter)

        self.video_widget = QVideoWidget()
        self.video_widget.hide()

        self.player = QMediaPlayer()
        self.audio = QAudioOutput()
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video_widget)

        self.text_edit = QTextEdit()
        self.text_edit.setFixedHeight(120)
        self.text_edit.textChanged.connect(self.save_annotation)

        prev_btn = QPushButton("◀ Previous")
        next_btn = QPushButton("Next ▶")
        open_btn = QPushButton("Open Folder")

        prev_btn.clicked.connect(self.prev_media)
        next_btn.clicked.connect(self.next_media)
        open_btn.clicked.connect(self.open_folder)

        btn_layout = QHBoxLayout()
        btn_layout.addWidget(prev_btn)
        btn_layout.addWidget(open_btn)
        btn_layout.addWidget(next_btn)

        layout = QVBoxLayout()
        layout.addWidget(self.media_label)
        layout.addWidget(self.video_widget)
        layout.addWidget(self.text_edit)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

    # ---------- Folder & Loading ----------

    def open_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Media Folder")
        if not folder:
            return

        self.base_dir = Path(folder)
        self.load_media_files()
        self.load_annotations()

        self.current_index = 0
        self.show_current_media()

    def load_media_files(self):
        self.media_files = [
            f for f in sorted(self.base_dir.iterdir())
            if f.suffix.lower() in SUPPORTED_IMAGES | SUPPORTED_VIDEOS
        ]

    def load_annotations(self):
        self.annotations_path = self.base_dir / "annotations.json"
        if self.annotations_path.exists():
            with open(self.annotations_path, "r", encoding="utf-8") as f:
                self.annotations = json.load(f)
        else:
            self.annotations = {}

    def save_annotations_file(self):
        with open(self.annotations_path, "w", encoding="utf-8") as f:
            json.dump(self.annotations, f, indent=2)

    # ---------- Display ----------

    def show_current_media(self):
        if not self.media_files:
            return

        file = self.media_files[self.current_index]
        suffix = file.suffix.lower()

        self.player.stop()
        self.video_widget.hide()
        self.media_label.show()

        if suffix in SUPPORTED_IMAGES:
            pixmap = QPixmap(str(file))
            self.media_label.setPixmap(
                pixmap.scaled(
                    self.media_label.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
            )
        else:
            self.media_label.hide()
            self.video_widget.show()
            self.player.setSource(QUrl.fromLocalFile(str(file)))
            self.player.play()

        self.load_annotation_text()
        self.start_scroll_if_needed()

    # ---------- Annotation Handling ----------

    def load_annotation_text(self):
        file = self.media_files[self.current_index].name
        text = self.annotations.get(file, {}).get("text", "")
        self.text_edit.blockSignals(True)
        self.text_edit.setPlainText(text)
        self.text_edit.blockSignals(False)

    def save_annotation(self):
        if not self.media_files:
            return

        file = self.media_files[self.current_index].name
        self.annotations.setdefault(file, {})["text"] = self.text_edit.toPlainText()
        self.save_annotations_file()
        self.start_scroll_if_needed()

    # ---------- Navigation ----------

    def next_media(self):
        if self.media_files:
            self.current_index = (self.current_index + 1) % len(self.media_files)
            self.show_current_media()

    def prev_media(self):
        if self.media_files:
            self.current_index = (self.current_index - 1) % len(self.media_files)
            self.show_current_media()

    # ---------- Text Scrolling ----------

    def start_scroll_if_needed(self):
        self.scroll_timer.stop()
        doc_height = self.text_edit.document().size().height()
        box_height = self.text_edit.viewport().height()

        if doc_height > box_height:
            self.scroll_pos = 0
            self.scroll_timer.start(100)  # adjust speed here

    def scroll_text(self):
        scrollbar = self.text_edit.verticalScrollBar()
        if scrollbar.value() < scrollbar.maximum():
            scrollbar.setValue(scrollbar.value() + 1)
        else:
            scrollbar.setValue(0)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MediaAnnotator()
    window.resize(800, 600)
    window.show()
    sys.exit(app.exec())
