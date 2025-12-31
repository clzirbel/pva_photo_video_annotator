import sys, json, shutil
from pathlib import Path
from datetime import datetime
from bisect import bisect_right
import requests
from tinytag import TinyTag
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton,
    QTextEdit, QVBoxLayout, QHBoxLayout, QComboBox, QSlider, QFileDialog, QMessageBox, QLineEdit)
from PySide6.QtCore import Qt, QTimer, QUrl, QPoint
from PySide6.QtGui import QPixmap, QImage, QFont
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PIL import Image, ExifTags, ImageOps

SUPPORTED_IMAGES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp"}
SUPPORTED_VIDEOS = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".webm", ".m4v", ".3gp"}
JSON_NAME = "annotations.json"
TRASH_DIR = "set_aside"
DEFAULT_FONT_SIZE = 14
DEFAULT_IMAGE_TIME = 5  # seconds per image

def get_file_creation_time(path):
    """Get file creation time, cross-platform compatible."""
    try:
        stat = path.stat()
        # Try birthtime first (macOS and some Linux filesystems)
        if hasattr(stat, 'st_birthtime'):
            return stat.st_birthtime
        # Fall back to ctime (creation time on Windows, metadata change time on Linux)
        return stat.st_ctime
    except:
        return 0

def get_exif_rotation(path):
    """Get EXIF rotation in degrees. Handles all EXIF orientation values."""
    try:
        img = Image.open(path)
        exif = img._getexif()
        if not exif: return 0
        for k, v in ExifTags.TAGS.items():
            if v == "Orientation":
                orientation = exif.get(k, 1)
                # Map EXIF orientation to rotation in degrees
                # Note: Values 2,4,5,7 involve flips; those are now handled by ImageOps.exif_transpose
                # This function returns the "base" rotation for display purposes
                orientation_to_degrees = {
                    1: 0,      # Normal
                    2: 0,      # Flip horizontal (handled by exif_transpose)
                    3: 180,    # Rotate 180°
                    4: 0,      # Flip vertical (handled by exif_transpose)
                    5: 90,     # Flip + rotate 90° CCW (handled by exif_transpose)
                    6: 270,    # Rotate 90° CW
                    7: 270,    # Flip + rotate 90° CW (handled by exif_transpose)
                    8: 90      # Rotate 90° CCW
                }
                return orientation_to_degrees.get(orientation, 0)
    except:
        return 0
    return 0

def get_exif_gps(path):
    """Extract latitude and longitude from EXIF data. Returns (lat, lon) or None."""
    try:
        img = Image.open(path)
        exif = img._getexif()
        if not exif: return None

        gps_ifd = None
        for tag, value in exif.items():
            if ExifTags.TAGS.get(tag) == "GPSInfo":
                gps_ifd = value
                break

        if not gps_ifd: return None

        gps_data = {}
        for tag, value in gps_ifd.items():
            gps_tag = ExifTags.GPSTAGS.get(tag, tag)
            gps_data[gps_tag] = value

        def get_decimal_from_dms(dms):
            d, m, s = dms
            return d + (m / 60.0) + (s / 3600.0)

        lat = get_decimal_from_dms(gps_data["GPSLatitude"]) if "GPSLatitude" in gps_data else None
        lon = get_decimal_from_dms(gps_data["GPSLongitude"]) if "GPSLongitude" in gps_data else None

        if "GPSLatitudeRef" in gps_data and gps_data["GPSLatitudeRef"] == "S":
            lat = -lat
        if "GPSLongitudeRef" in gps_data and gps_data["GPSLongitudeRef"] == "W":
            lon = -lon

        return (lat, lon) if lat and lon else None
    except: return None

def reverse_geocode_nominatim(lat, lon):
    """Reverse geocode using OpenStreetMap Nominatim API. Returns formatted address or None."""
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}"
        headers = {"User-Agent": "PVA-Photo-Video-Annotator/1.0"}
        response = requests.get(url, timeout=2, headers=headers)
        if response.status_code == 200:
            data = response.json()
            address = data.get("address", {})
            # Build address as City, State, Country
            city = address.get("city") or address.get("town") or address.get("village")
            state = address.get("state")
            country = address.get("country")

            parts = []
            if city: parts.append(city)
            if state: parts.append(state)
            if country: parts.append(country)

            result = ", ".join(parts) if parts else None
            return result
    except requests.Timeout:
        pass
    except Exception as e:
        pass
    return None

def load_image(path, rotation):
    img = Image.open(path)

    # Apply EXIF orientation if available (returns None if no EXIF, so use 'or img')
    img = ImageOps.exif_transpose(img) or img

    # Apply user rotation on top of EXIF orientation
    if rotation:
        img = img.rotate(rotation, expand=True)

    # Ensure RGB mode for consistency
    if img.mode != 'RGB':
        img = img.convert("RGB")

    # Convert PIL image to QImage with proper stride alignment
    width, height = img.size
    img_data = img.tobytes()
    bytes_per_line = width * 3  # RGB888 format requires 3 bytes per pixel
    qimg = QImage(img_data, width, height, bytes_per_line, QImage.Format_RGB888)
    # Make a copy to ensure data persistence after PIL image is garbage collected
    return qimg.copy()


def get_video_duration_ms(video_path):
    """Get video duration in milliseconds using tinytag. Returns duration or None."""
    try:
        tag = TinyTag.get(str(video_path), tags=False, duration=True)
        if tag and tag.duration:
            duration_ms = int(tag.duration * 1000)
            return duration_ms
    except Exception:
        pass
    return None

def format_time_ms(ms):
    """Format milliseconds as MM:SS."""
    if ms is None or ms < 0:
        return "00:00"
    total_seconds = ms // 1000
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes:02d}:{seconds:02d}"

class TimestampSlider(QSlider):
    """Custom slider that shows timestamp tooltip on hover/click."""
    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setMouseTracking(True)

    def mouseMoveEvent(self, event):
        # Calculate the value at the mouse position
        if self.maximum() > 0:
            x_pos = event.pos().x()
            width = self.width()
            value = int((x_pos / width) * self.maximum())
            self.setToolTip(format_time_ms(value))
            # Show tooltip immediately
            QApplication.instance().processEvents()
        return super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # Calculate value from click position
            if self.maximum() > 0:
                x_pos = event.pos().x()
                width = self.width()
                value = int((x_pos / width) * self.maximum())
                self.setValue(value)
        return super().mousePressEvent(event)

class PVAnnotator(QWidget):
    def __init__(self,start_path=None):
        super().__init__()
        self.setWindowTitle("PVA Photo Video Annotator")
        self.setGeometry(QApplication.primaryScreen().availableGeometry())
        self.showMaximized()

        self.dir=None; self.media=[]; self.index=0
        self.data={}; self.slideshow=False
        self.timer=QTimer(); self.timer.timeout.connect(self.advance_slideshow)

        # Widgets
        self.image_label=QLabel(alignment=Qt.AlignCenter)
        self.image_label.setStyleSheet("background-color: white;")
        self.prev_btn=QPushButton("Previous")
        self.skip_btn=QPushButton("Skip")
        self.trash_btn=QPushButton("Set Aside")
        self.rotate_btn=QPushButton("Rotate")
        self.volume_btn=QPushButton("100% volume")
        self.slide_btn=QPushButton("Slideshow")
        self.image_time_input=QLineEdit()
        self.image_time_input.setFixedWidth(88)
        self.image_time_input.setFont(QFont("Arial",12))
        self.image_time_input.setAlignment(Qt.AlignLeft)
        self.image_time_input.setText(f"{DEFAULT_IMAGE_TIME} seconds")
        self.image_time_input.editingFinished.connect(self.update_image_time)
        self.next_btn=QPushButton("Next")
        # Make button text bold
        bold_font = QFont()
        bold_font.setBold(True)
        for btn in [self.prev_btn, self.skip_btn, self.trash_btn, self.rotate_btn, self.volume_btn, self.slide_btn, self.next_btn]:
            btn.setFont(bold_font)
        for b,f in [(self.prev_btn,self.prev_item),(self.next_btn,self.next_item),
                    (self.skip_btn,self.skip_item),(self.trash_btn,self.trash_item),
                    (self.rotate_btn,self.rotate_item),(self.volume_btn,self.change_volume),
                    (self.slide_btn,self.toggle_slideshow)]: b.clicked.connect(f)

        self.meta_label=QLabel(); self.meta_label.setFont(QFont("Arial",DEFAULT_FONT_SIZE))
        self.meta_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.location_combo=QComboBox(); self.location_combo.setEditable(True)
        self.location_combo.setFont(QFont("Arial",DEFAULT_FONT_SIZE))
        self.location_combo.currentTextChanged.connect(self.update_location_text)
        self.text_box=QTextEdit(); self.text_box.setFixedHeight(75)
        self.text_box.setFont(QFont("Arial",DEFAULT_FONT_SIZE))

        self.skip_in_progress = False
        self.new_annotation_pending = False
        self.text_scroll_timer = QTimer()
        self.text_scroll_timer.timeout.connect(self.scroll_annotation_text)
        self.text_scroll_pos = 0

        self.video_widget=QVideoWidget(); self.video_widget.setStyleSheet("background-color: white;")
        self.video_player=QMediaPlayer()  # Qt6 disables hw accel by default
        self.audio_output=QAudioOutput()
        self.video_player.setAudioOutput(self.audio_output)
        self.video_player.setVideoOutput(self.video_widget)
        self.video_slider=TimestampSlider()
        self.video_slider.sliderMoved.connect(lambda pos: self.video_player.setPosition(pos))
        self.video_player.positionChanged.connect(lambda pos: self.update_video_annotation(pos))
        self.video_player.positionChanged.connect(lambda pos: self.video_slider.setValue(pos))
        self.video_player.durationChanged.connect(lambda d: self.video_slider.setMaximum(d))

        self.play_btn=QPushButton("Play/Pause"); self.play_btn.clicked.connect(self.toggle_play)
        self.replay_btn=QPushButton("Replay"); self.replay_btn.clicked.connect(self.replay_video)
        self.add_ann_btn=QPushButton("Add annotation"); self.add_ann_btn.clicked.connect(self.add_annotation)
        self.edit_ann_btn=QPushButton("Edit annotation"); self.edit_ann_btn.clicked.connect(self.edit_annotation)
        self.remove_ann_btn=QPushButton("Remove annotation"); self.remove_ann_btn.clicked.connect(self.remove_annotation)
        self.skip_ann_btn=QPushButton("Skip until next annotation"); self.skip_ann_btn.clicked.connect(self.skip_until_next_annotation)
        # Make video button fonts bold
        for btn in [self.play_btn, self.replay_btn, self.add_ann_btn, self.edit_ann_btn, self.remove_ann_btn, self.skip_ann_btn]:
            btn.setFont(bold_font)

        # Layout with minimal spacing
        layout=QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.image_label)
        layout.addWidget(self.video_widget)
        layout.addWidget(self.video_slider)
        video_btn_layout=QHBoxLayout()
        video_btn_layout.setSpacing(2)
        for b in [self.play_btn,self.replay_btn,self.add_ann_btn,self.edit_ann_btn,
                  self.remove_ann_btn,self.skip_ann_btn]: video_btn_layout.addWidget(b)
        layout.addLayout(video_btn_layout)
        button_layout=QHBoxLayout()
        button_layout.setSpacing(2)
        for b in [self.prev_btn,self.skip_btn,self.trash_btn,self.rotate_btn,self.volume_btn,self.slide_btn]: button_layout.addWidget(b)
        button_layout.addWidget(self.image_time_input)
        button_layout.addWidget(self.next_btn)
        layout.addLayout(button_layout)
        meta_layout=QHBoxLayout()
        meta_layout.setSpacing(2)
        meta_layout.addWidget(self.meta_label,3); meta_layout.addWidget(self.location_combo,2)
        layout.addLayout(meta_layout)
        layout.addWidget(self.text_box)

        # Override focus out to commit annotation
        orig_focus_out = self.text_box.focusOutEvent
        def text_focus_out(event):
            # Only call update_text() if not creating a new annotation
            # (new annotations are saved by save_pending_annotation instead)
            if not self.new_annotation_pending:
                self.update_text()
            self.commit_editing_annotation()       # commit edit if editing
            self.save_pending_annotation()         # commit new annotation if pending
            orig_focus_out(event)
        self.text_box.focusOutEvent = text_focus_out

        self.load_directory(start_path)

    # ---------------- Directory ----------------
    def load_directory(self,start_path=None):
        if start_path:
            start_path=Path(start_path)
            self.dir=start_path.parent if start_path.is_file() else start_path
        else:
            d=QFileDialog.getExistingDirectory(self,"Select media directory")
            if not d: sys.exit()
            self.dir=Path(d)
        self.trash=self.dir/TRASH_DIR; self.trash.mkdir(exist_ok=True)
        self.json_path=self.dir/JSON_NAME
        if self.json_path.exists():
            self.data=json.loads(self.json_path.read_text())
        else: self.data={"_settings":{"font_size":DEFAULT_FONT_SIZE,"image_time":DEFAULT_IMAGE_TIME}}
        self.check_and_prompt_folders()
        self.media=sorted([p for p in self.get_all_media_files()],
                          key=lambda p: get_file_creation_time(p))
        if start_path and start_path.is_file() and start_path in self.media:
            self.index=self.media.index(start_path)
        # Sort video annotations
        for entry in self.data.values():
            if "annotations" in entry:
                entry["annotations"]=sorted(entry["annotations"],key=lambda a: a["time"])
        # Update image time display
        image_time = self.get_image_time()
        self.image_time_input.setText(f"{image_time} seconds")
        self.show_item()

    # ---------------- Helpers ----------------
    def current(self): return self.media[self.index]

    def get_relative_path(self, file_path):
        """Get relative path from self.dir for display, e.g., 'France/photo.jpg'."""
        try:
            return str(file_path.relative_to(self.dir))
        except ValueError:
            return file_path.name

    def save(self):
        # Build a fast lookup set of video filenames for O(1) lookup
        video_names = {p.name for p in self.media if p.suffix.lower() in SUPPORTED_VIDEOS}

        # Clean up rotation field for videos (rotation only applies to images)
        for filename in self.data:
            if filename != "_settings" and filename in video_names:
                self.data[filename].pop("rotation", None)

        self.json_path.write_text(json.dumps(self.data,indent=2))

    def check_and_prompt_folders(self):
        """Check all folders in directory and prompt user if not already set."""
        for item in self.dir.iterdir():
            if item.is_dir() and item.name != TRASH_DIR:
                # Check if we already have a "use" setting for this folder
                if item.name not in self.data or "use" not in self.data[item.name]:
                    # Prompt user
                    reply = QMessageBox.question(
                        self,
                        "Include Folder?",
                        f"Include files from '{item.name}' folder?",
                        QMessageBox.Yes | QMessageBox.No
                    )
                    # Save the choice
                    if item.name not in self.data:
                        self.data[item.name] = {}
                    self.data[item.name]["use"] = (reply == QMessageBox.Yes)
        self.save()

    def get_all_media_files(self):
        """Get all media files from root and included folders."""
        files = []
        # Add files from root directory
        for p in self.dir.iterdir():
            if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGES|SUPPORTED_VIDEOS:
                files.append(p)
        # Add files from folders marked with use=true
        for item in self.dir.iterdir():
            if item.is_dir() and item.name != TRASH_DIR:
                if self.data.get(item.name, {}).get("use", False):
                    for p in item.iterdir():
                        if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGES|SUPPORTED_VIDEOS:
                            files.append(p)
        return files

    # ---------------- Media Display ----------------
    def extract_and_store_location(self, file_path):
        """Extract GPS coordinates from media file and reverse geocode if available."""
        p = self.current()
        if p.suffix.lower() not in SUPPORTED_IMAGES:
            return

        entry = self.data.setdefault(p.name, {})
        location = entry.setdefault("location", {})

        # Skip if we already have automated location data
        if "automated_text" in location:
            return

        # Extract GPS from EXIF if not already present
        if "latitude_longitude" not in location:
            gps = get_exif_gps(file_path)
            if not gps:
                return

            lat, lon = gps
            # Round to 5 decimal places (approximately 1.1 meter accuracy)
            lat = round(lat, 5)
            lon = round(lon, 5)
            location["latitude_longitude"] = {"latitude": lat, "longitude": lon}
        else:
            lat = location["latitude_longitude"]["latitude"]
            lon = location["latitude_longitude"]["longitude"]

        # Try reverse geocoding
        address = reverse_geocode_nominatim(lat, lon)
        if address:
            location["automated_text"] = address

        self.save()

    def show_item(self):
        if not self.media: return
        p=self.current(); entry=self.data.setdefault(p.name,{"rotation":0,"text":""})

        # Extract location data if available
        self.extract_and_store_location(p)

        ts=datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d | %H:%M:%S")
        display_path = self.get_relative_path(p)
        self.meta_label.setText(f"{ts} | {display_path}")

        # Dropdown locations
        manual_locations=list({self.data[f].get("location",{}).get("manual_text","") for f in self.data if "location" in self.data[f]})
        auto_locations=list({self.data[f].get("location",{}).get("automated_text","") for f in self.data if "location" in self.data[f]})
        all_locations=list(set([loc for loc in manual_locations + auto_locations if loc]))
        current_loc=entry.get("location",{}).get("manual_text","") or entry.get("location",{}).get("automated_text","")
        self.location_combo.blockSignals(True)
        self.location_combo.clear(); self.location_combo.addItem(current_loc)
        for loc in all_locations:
            if loc!=current_loc: self.location_combo.addItem(loc)
        self.location_combo.setCurrentText(current_loc)
        self.location_combo.blockSignals(False)

        # Text box
        if p.suffix.lower() in SUPPORTED_IMAGES:
            self.text_box.setText(entry.get("text",""))
        else:
            annotations=entry.setdefault("annotations",[])
            ann0=next((a for a in annotations if a["time"]==0.0),None)
            self.text_box.setText(ann0["text"] if ann0 else "")

        self.setFocus()
        # Media display
        if p.suffix.lower() in SUPPORTED_IMAGES:
            self.video_widget.hide(); self.video_slider.hide()
            for b in [self.play_btn,self.replay_btn,self.add_ann_btn,self.edit_ann_btn,
                      self.remove_ann_btn,self.skip_ann_btn]: b.hide()
            self.rotate_btn.show()
            self.volume_btn.hide()
            self.image_label.show()
            rot=entry.get("rotation",get_exif_rotation(p))
            qimg=load_image(p,rot)
            pix=QPixmap.fromImage(qimg)
            self.image_label.setPixmap(pix.scaled(800,600,Qt.KeepAspectRatio))
            self.video_player.stop()
        else:
            self.image_label.hide(); self.video_widget.show(); self.video_slider.show()
            for b in [self.play_btn,self.replay_btn,self.add_ann_btn,self.edit_ann_btn,
                      self.remove_ann_btn,self.skip_ann_btn]: b.show()
            self.rotate_btn.hide()
            self.volume_btn.show()
            # Apply stored volume
            volume = entry.get("volume", 100)
            self.audio_output.setVolume(volume / 100.0)
            self.volume_btn.setText(f"{volume}% volume")
            self.video_player.setSource(QUrl.fromLocalFile(str(p)))
            # Use a single-shot timer to allow the source to load before playing
            QTimer.singleShot(100, self.video_player.play)

        # Next/Prev labels
        self.prev_btn.setText("Jump to last" if self.index==0 else "Previous")
        self.next_btn.setText("Back to first" if self.index==len(self.media)-1 else "Next")
        self.save()

    # ---------------- Video Annotation ----------------
    def get_current_video_annotations(self):
        p=self.current()
        return self.data.setdefault(p.name,{}).setdefault("annotations",[])

    def update_video_annotation(self, pos):

        if hasattr(self, "editing_annotation"):
            # Skip updating the text box while editing
            return

        self.commit_editing_annotation()

        p = self.current()
        if p.suffix.lower() not in SUPPORTED_VIDEOS:
            return

        pos_sec = pos / 1000.0
        annotations = self.get_current_video_annotations()
        if not annotations:
            return

        annotations.sort(key=lambda a: a["time"])

        active_ann = None
        for i, ann in enumerate(annotations):
            if ann["time"] <= pos_sec:
                active_ann = (i, ann)
            else:
                break

        if not active_ann:
            return

        i, ann = active_ann

        # Handle skip annotation
        if ann.get("skip", False):
            playback = self.video_player.playbackState() == QMediaPlayer.PlayingState

            if playback:
                # Skip automatically
                if i + 1 < len(annotations):
                    next_time = annotations[i + 1]["time"]
                    self.video_player.setPosition(int(next_time * 1000))
                else:
                    # Last annotation: jump to video end
                    dur = self.video_player.duration()
                    if dur > 0:
                        self.video_player.setPosition(dur)
                    else:
                        self.video_player.setPosition(self.video_player.position() + 1000)  # 1 sec ahead
                    # Make sure text shows "Segment skipped"
                    self.text_box.blockSignals(True)
                    self.text_box.setText(ann.get("text", "Segment skipped"))
                    self.text_box.blockSignals(False)
                return
            else:
                # Paused or manual seek: show text
                self.text_box.blockSignals(True)
                self.text_box.setText(ann.get("text", "Segment skipped"))
                self.text_box.blockSignals(False)
                return

        # Normal annotation
        self.text_box.blockSignals(True)
        self.text_box.setText(ann.get("text", ""))
        self.text_box.blockSignals(False)


    def skip_until_next_annotation(self):
        p = self.current()
        if p.suffix.lower() not in SUPPORTED_VIDEOS:
            return

        pos_sec = self.video_player.position() / 1000.0
        annotations = self.get_current_video_annotations()

        # Prevent duplicate skip at same timestamp
        for ann in annotations:
            if ann["time"] == pos_sec and ann.get("skip", False):
                return

        # Add skip annotation with text
        annotations.append({
            "time": pos_sec,
            "text": "Segment skipped",
            "skip": True  # Skip annotation - only include when true
        })
        annotations.sort(key=lambda a: a["time"])
        self.save()

        # Jump to next annotation if exists, else pause at end
        next_ann = next((a for a in annotations if a["time"] > pos_sec), None)
        if next_ann:
            self.video_player.setPosition(int(next_ann["time"] * 1000))
        else:
            dur = self.video_player.duration()
            if dur > 0:
                self.video_player.setPosition(dur)
        self.video_player.pause()
        self.update_video_annotation(self.video_player.position())

    def save_pending_annotation(self):
        if not self.new_annotation_pending:
            return
        p = self.current()
        if p.suffix.lower() not in SUPPORTED_VIDEOS:
            self.new_annotation_pending = False
            return
        text = self.text_box.toPlainText().strip()
        if text:
            annotations = self.get_current_video_annotations()
            annotations.append({
                "time": getattr(self, "new_annotation_timestamp", self.video_player.position()/1000.0),
                "text": text
            })
            annotations.sort(key=lambda a: a["time"])
            self.save()
        self.new_annotation_pending = False
        if hasattr(self, "new_annotation_timestamp"):
            delattr(self, "new_annotation_timestamp")

    def add_annotation(self):
        p = self.current()
        if p.suffix.lower() not in SUPPORTED_VIDEOS:
            return
        if self.video_player.playbackState() != QMediaPlayer.PausedState:
            self.video_player.pause()
        self.new_annotation_pending = True
        self.new_annotation_timestamp = self.video_player.position() / 1000.0
        self.text_box.clear()
        self.text_box.setFocus()
        cursor = self.text_box.textCursor()
        cursor.movePosition(cursor.End)
        self.text_box.setTextCursor(cursor)

    def edit_annotation(self):
        p = self.current()
        if p.suffix.lower() not in SUPPORTED_VIDEOS:
            return

        # Commit any pending new annotation first
        self.save_pending_annotation()
        self.commit_editing_annotation()  # Commit any currently editing annotation

        pos_sec = self.video_player.position() / 1000.0
        annotations = self.get_current_video_annotations()  # get real list
        annotations.sort(key=lambda a: a["time"])           # sort in-place

        # Find the annotation immediately before current video position
        times = [a["time"] for a in annotations]
        idx = bisect_right(times, pos_sec) - 1
        if 0 <= idx < len(annotations):
            # Use the real annotation object, not a copy
            self.editing_annotation = annotations[idx]
            self.text_box.setText(self.editing_annotation.get("text", ""))
            self.text_box.setFocus()
            cursor = self.text_box.textCursor()
            cursor.movePosition(cursor.End)
            self.text_box.setTextCursor(cursor)

    def commit_editing_annotation(self):
        if hasattr(self, "editing_annotation"):
            self.editing_annotation["text"] = self.text_box.toPlainText()
            self.save()
            del self.editing_annotation

    # ---------------- Text Box Focus ----------------
    def text_focus_out(self, event):
        """Commit any new or edited annotation when text box loses focus."""
        self.commit_editing_annotation()
        self.save_pending_annotation()
        QTextEdit.focusOutEvent(self.text_box, event)

    def text_focus_in(self, event):
        """Pause video when text box gains focus."""
        p = self.current()
        if p.suffix.lower() in SUPPORTED_VIDEOS:
            self.video_player.pause()
        QTextEdit.focusInEvent(self.text_box, event)


    def remove_annotation(self):
        p = self.current()
        if p.suffix.lower() not in SUPPORTED_VIDEOS:
            return

        self.video_player.pause()
        pos_sec = self.video_player.position() / 1000.0

        annotations = self.get_current_video_annotations()
        if not annotations:
            return

        # Ensure sorted
        annotations.sort(key=lambda a: a["time"])

        # Find active annotation: last one with time <= current time
        active_idx = None
        for i, ann in enumerate(annotations):
            if ann["time"] <= pos_sec:
                active_idx = i
            else:
                break

        if active_idx is None:
            return

        # Remove it
        annotations.pop(active_idx)

        # Determine new position
        if active_idx - 1 >= 0:
            new_time = annotations[active_idx - 1]["time"]
        else:
            new_time = 0.0

        # Seek and pause
        self.video_player.setPosition(int(new_time * 1000))
        self.update_video_annotation(int(new_time * 1000))

        self.save()


    # ---------------- Generic ----------------
    def update_text(self):
        p=self.current()
        if p.suffix.lower() in SUPPORTED_IMAGES:
            self.data.setdefault(p.name,{})["text"]=self.text_box.toPlainText()
        else:
            annotations=self.get_current_video_annotations()
            ann0=next((a for a in annotations if a["time"]==0.0),None)
            if ann0: ann0["text"]=self.text_box.toPlainText()
            else: annotations.append({"time":0.0,"text":self.text_box.toPlainText()})
        self.save()

    def update_location_text(self,text):
        p=self.current()
        self.data.setdefault(p.name,{}).setdefault("location",{})["manual_text"]=text
        self.save()

    def update_image_time(self):
        """Parse image time input and save to settings."""
        text = self.image_time_input.text()
        # Extract numbers from the text, split by spaces, take first number
        words = text.split()
        for word in words:
            # Strip out non-numeric characters
            num_str = ''.join(c for c in word if c.isdigit())
            if num_str:
                try:
                    new_time = int(num_str)
                    if new_time > 0:
                        self.data.setdefault("_settings", {})["image_time"] = new_time
                        self.image_time_input.setText(f"{new_time} seconds")
                        self.save()
                        return
                except ValueError:
                    pass
        # If no valid number found, reset to current value
        current_time = self.get_image_time()
        self.image_time_input.setText(f"{current_time} seconds")

    # ---------------- Navigation ----------------
    def next_item(self):
        self.index=(self.index+1)%len(self.media)
        # Skip over any files marked as skip=true
        start_index = self.index
        while self.data.get(self.media[self.index].name, {}).get("skip", False):
            self.index=(self.index+1)%len(self.media)
            # Prevent infinite loop if all files are skipped
            if self.index == start_index:
                break
        self.show_item()

    def prev_item(self):
        self.index=(self.index-1)%len(self.media)
        # Skip over any files marked as skip=true
        start_index = self.index
        while self.data.get(self.media[self.index].name, {}).get("skip", False):
            self.index=(self.index-1)%len(self.media)
            # Prevent infinite loop if all files are skipped
            if self.index == start_index:
                break
        if self.slideshow: self.toggle_slideshow()
        self.show_item()

    def skip_item(self):
        self.data[self.current().name]["skip"]=True; self.save(); self.next_item()

    def rotate_item(self):
        p=self.current()
        # Only allow rotation for images
        if p.suffix.lower() not in SUPPORTED_IMAGES:
            return

        entry=self.data.setdefault(p.name,{})
        current_rotation=entry.get("rotation",0)
        # Cycle through 0, 90, 180, 270
        new_rotation=(current_rotation+90)%360
        # Store rotation only if not 0 (default)
        if new_rotation==0:
            entry.pop("rotation",None)
        else:
            entry["rotation"]=new_rotation
        self.save()
        self.show_item()

    def change_volume(self):
        p=self.current()
        # Only allow volume control for videos
        if p.suffix.lower() not in SUPPORTED_VIDEOS:
            return

        entry=self.data.setdefault(p.name,{})
        current_volume=entry.get("volume",100)
        # Cycle through 100, 80, 60, 40, 20, 0, then back to 100
        volume_levels=[100,80,60,40,20,0]
        current_idx=volume_levels.index(current_volume) if current_volume in volume_levels else 0
        new_idx=(current_idx+1)%len(volume_levels)
        new_volume=volume_levels[new_idx]

        # Store volume only if not 100 (default)
        if new_volume==100:
            entry.pop("volume",None)
        else:
            entry["volume"]=new_volume

        # Apply volume immediately
        self.audio_output.setVolume(new_volume/100.0)
        self.volume_btn.setText(f"{new_volume}% volume")
        self.save()

    def trash_item(self):
        p=self.current()
        # Create set_aside folder in the same directory as the file
        file_parent = p.parent
        trash_dir = file_parent / TRASH_DIR
        trash_dir.mkdir(exist_ok=True)
        shutil.move(str(p), trash_dir / p.name)
        self.media.remove(p)
        self.save()
        self.show_item()

    def toggle_slideshow(self):
        self.slideshow=not self.slideshow
        self.text_scroll_timer.stop()
        if self.slideshow:
            self.slide_btn.setText("Stop slideshow")
            p=self.current()
            if p.suffix.lower() in SUPPORTED_VIDEOS:
                # For videos, get duration and set timer to that
                dur_ms = get_video_duration_ms(p)
                if dur_ms and dur_ms > 0:
                    self.timer.start(dur_ms)
                else:
                    self.timer.start(self.get_image_time()*1000)
            else:
                # For images, use the image time and start text scrolling if needed
                duration=max(self.get_image_time(),len(self.text_box.toPlainText().split())/4)*1000
                self.timer.start(int(duration))
                self.start_text_scroll(int(duration))
        else:
            self.slide_btn.setText("Slideshow")
            self.timer.stop()
            p=self.current()
            if p.suffix.lower() in SUPPORTED_VIDEOS:
                self.video_player.pause()

    def advance_slideshow(self):
        self.next_item()
        # Now set timer for the newly loaded item
        p=self.current()
        if p.suffix.lower() in SUPPORTED_IMAGES:
            duration=max(self.get_image_time(),len(self.text_box.toPlainText().split())/4)*1000
            self.timer.start(int(duration))
            self.start_text_scroll(int(duration))
        else:
            # For videos, get duration and set timer to that
            dur_ms = get_video_duration_ms(p)
            if dur_ms and dur_ms > 0:
                self.timer.start(dur_ms)
            else:
                self.timer.start(self.get_image_time()*1000)
        if self.index==0: self.timer.start(self.get_image_time()*1000)

    def start_text_scroll(self, duration_ms):
        """Start scrolling text if it has more than 3 lines during slideshow."""
        text = self.text_box.toPlainText()
        lines = text.split('\n')
        if len(lines) > 3 and self.slideshow:
            # Calculate scroll interval: divide duration by number of scroll steps
            scroll_steps = max(len(lines) - 3, 1)
            scroll_interval = max(100, duration_ms // (scroll_steps + 1))
            self.text_scroll_pos = 0
            self.text_scroll_lines = lines
            self.text_scroll_timer.start(scroll_interval)

    def scroll_annotation_text(self):
        """Scroll through multi-line text during slideshow."""
        if not self.slideshow or not hasattr(self, 'text_scroll_lines'):
            self.text_scroll_timer.stop()
            return

        lines = self.text_scroll_lines
        max_visible_lines = 3

        if self.text_scroll_pos + max_visible_lines < len(lines):
            self.text_scroll_pos += 1
            visible_text = '\n'.join(lines[self.text_scroll_pos:self.text_scroll_pos + max_visible_lines])
            self.text_box.blockSignals(True)
            self.text_box.setText(visible_text)
            self.text_box.blockSignals(False)
        else:
            self.text_scroll_timer.stop()

    def get_image_time(self):
        return self.data.get("_settings",{}).get("image_time",DEFAULT_IMAGE_TIME)

    # ---------------- Video Controls ----------------
    def toggle_play(self):
        if self.video_player.playbackState()==QMediaPlayer.PlayingState: self.video_player.pause()
        else: self.video_player.play()

    def replay_video(self):
        self.video_player.setPosition(0); self.video_player.play()

    # ---------------- Keyboard ----------------
    def keyPressEvent(self,event):
        if event.key()==Qt.Key_Right: self.next_item()
        elif event.key()==Qt.Key_Left: self.prev_item()
        else: super().keyPressEvent(event)

if __name__=="__main__":
    app=QApplication(sys.argv)
    start_path=sys.argv[1] if len(sys.argv)>1 else None
    w=PVAnnotator(start_path)
    w.show()
    sys.exit(app.exec())
