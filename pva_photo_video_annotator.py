import sys, json, shutil, re, calendar
from pathlib import Path
from datetime import datetime
from bisect import bisect_right
import requests
import os
from tinytag import TinyTag
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton,
    QTextEdit, QVBoxLayout, QHBoxLayout, QComboBox, QSlider, QFileDialog, QMessageBox, QLineEdit, QProgressDialog)
from PySide6.QtCore import Qt, QTimer, QUrl, QPoint, QLoggingCategory
from PySide6.QtGui import QPixmap, QImage, QFont, QColor, QTextCursor
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PIL import Image, ExifTags, ImageOps
try:
    from hachoir.parser import createParser
    from hachoir.metadata import extractMetadata
    HACHOIR_AVAILABLE = True
except ImportError:
    HACHOIR_AVAILABLE = False
try:
    from pymediainfo import MediaInfo
    MEDIAINFO_AVAILABLE = True
except ImportError:
    MEDIAINFO_AVAILABLE = False

SUPPORTED_IMAGES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp"}
SUPPORTED_VIDEOS = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".webm", ".m4v", ".3gp"}
JSON_NAME = "annotations.json"
TRASH_DIR = "set_aside"
DEFAULT_FONT_SIZE = 14
DEFAULT_IMAGE_TIME = 5  # seconds per image
DATETIME_FMT = "%Y/%m/%d %H:%M:%S"
LEGACY_DATETIME_FMT = "%Y-%m-%d %H:%M:%S"

def format_creation_timestamp(ts):
    """Format Unix timestamp to display/save format."""
    local_dt = datetime.fromtimestamp(ts)
    return local_dt.strftime(DATETIME_FMT)

def parse_creation_value(value):
    """Parse stored creation time value (string or number) into Unix timestamp."""
    if value is None:
        return None
    # Numeric timestamp
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        for fmt in (DATETIME_FMT, LEGACY_DATETIME_FMT):
            try:
                return datetime.strptime(value.strip(), fmt).timestamp()
            except ValueError:
                continue
        # Last resort: try ISO
        try:
            return datetime.fromisoformat(value.strip()).timestamp()
        except Exception:
            pass
        # Or numeric string
        try:
            return float(value)
        except ValueError:
            return None
    return None

def parse_datetime_string(dt_str):
    """Parse various datetime string forms into timestamp. Returns None if unparsed.
    Handles both naive (local) and UTC-marked timestamps correctly by converting
    UTC times to epoch using calendar.timegm()."""
    if not dt_str:
        return None
    if isinstance(dt_str, (list, tuple)) and dt_str:
        dt_str = dt_str[0]
    s = str(dt_str).strip()

    # Detect if this is a UTC timestamp (before we strip the indicators)
    is_utc = False
    original_s = s
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
        is_utc = True
    elif s.endswith(' UTC'):
        is_utc = True
        s = s[:-4]
    elif s.lower().startswith('utc '):
        is_utc = True
        s = s[4:]

    # Try ISO first
    try:
        dt_obj = datetime.fromisoformat(s)
        # If timezone-aware, .timestamp() handles conversion correctly
        # If naive and is_utc=True, use calendar.timegm to treat as UTC
        if dt_obj.tzinfo is not None:
            # Timezone-aware datetime
            return dt_obj.timestamp()
        elif is_utc:
            # Naive datetime but marked as UTC
            result = calendar.timegm(dt_obj.timetuple())
            return result
        else:
            # Naive datetime, assume local
            return dt_obj.timestamp()
    except Exception:
        pass

    # Try common fallback formats
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            dt_obj = datetime.strptime(s, fmt)
            # If UTC was detected, interpret naive datetime as UTC time
            # and convert to epoch using calendar.timegm (UTC conversion)
            # This ensures the timestamp represents the correct moment in time
            if is_utc:
                result = calendar.timegm(dt_obj.timetuple())
                return result
            else:
                # Naive datetime assumed to be local time (like EXIF)
                return dt_obj.timestamp()
        except ValueError:
            continue
    return None

def parse_filename_datetime(path):
    """Try to infer datetime from filename patterns like PXL_YYYYMMDD_HHMMSS.*"""
    name = path.name
    # Pattern with date and time
    m = re.search(r"(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})", name)
    if m:
        y, mo, d, h, mi, s = m.groups()
        try:
            return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s)).timestamp()
        except ValueError:
            pass
    # Pattern with date only
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", name)
    if m:
        y, mo, d = m.groups()
        try:
            return datetime(int(y), int(mo), int(d), 0, 0, 0).timestamp()
        except ValueError:
            pass
    return None

def get_exif_datetime(path):
    """Extract DateTimeOriginal from EXIF data as a string (naive local time).
    Returns the string directly without any timezone conversion.
    Format: "YYYY/MM/DD HH:MM:SS" or 0 if not found."""
    try:
        if path.suffix.lower() not in SUPPORTED_IMAGES:
            return 0
        img = Image.open(path)
        exif = img._getexif()
        if not exif:
            return 0
        # Look for DateTimeOriginal (tag 36867) - the actual photo taken date
        datetime_original = exif.get(36867)
        if datetime_original:
            # EXIF datetime format: "YYYY:MM:DD HH:MM:SS"
            # Convert to our display format, preserving the literal time
            return datetime_original.replace(":", "/", 2)  # Only replace first 2 colons
    except:
        pass
    return 0

def get_video_creation_time(path):
    """Extract creation time for videos using MediaInfo (QuickTime/MP4 metadata).
    For MOV files: Uses timezone-aware creation date when available.
    Returns tuple (epoch, display_string) for MOV files, or just epoch for other videos.
    """
    if not MEDIAINFO_AVAILABLE:
        return 0
    try:
        mi = MediaInfo.parse(path)
    except Exception:
        mi = None

    is_mov = ".mov" in str(path).lower()

    candidates = []  # list of tuples (source, raw_value, parsed_ts)

    def add_candidate(label, raw):
        ts = parse_datetime_string(raw)
        candidates.append((label, raw, ts))
        return ts

    def candidate_times(track):
        data = track.to_data() if hasattr(track, 'to_data') else {}

        def add_field(key, label=None):
            val = data.get(key)
            if val:
                add_candidate(label or key, val)

        # QuickTime specific - check for the creation date with timezone info FIRST
        # Example: "2025-11-25T17:41:26+0700" (already has timezone!)
        # Handle this WITHOUT calling parse_datetime_string to avoid debug spam
        qt_date = data.get('comapplequicktimecreationdate')
        if qt_date and is_mov:
            # For MOV: add with special marker so we handle it separately
            candidates.append(('comapplequicktimecreationdate', qt_date, None))

        add_field('com.apple.quicktime.creationdate', 'qt_creationdate')

        # Generic creation_time
        add_field('creation_time', 'creation_time')
        add_field('creation_time-eng', 'creation_time-eng')
        # EXIF-like
        add_field('date_time_original', 'date_time_original')
        add_field('datetimeoriginal', 'datetimeoriginal')
        # Other common date fields
        add_field('encoded_date', 'encoded_date'); add_field('encoded_date-eng', 'encoded_date-eng')
        add_field('tagged_date', 'tagged_date'); add_field('tagged_date-eng', 'tagged_date-eng')
        add_field('recorded_date', 'recorded_date'); add_field('recorded_date-eng', 'recorded_date-eng')
        # MediaInfo provided lists
        for attr in ["other_creation_date", "other_recorded_date", "other_encoded_date", "other_tagged_date"]:
            val = getattr(track, attr, None)
            if val:
                add_candidate(attr, val)

    # Collect from MediaInfo
    if mi and mi.tracks:
        for track in mi.tracks:
            if track.track_type not in ("General", "Video"):
                continue
            candidate_times(track)

    # Filename-derived candidate
    filename_ts = parse_filename_datetime(path)
    candidates.append(("filename", path.name, filename_ts))

    # Filesystem timestamps as last resort
    try:
        st = path.stat()
        fs_candidates = [
            ("fs_birthtime", getattr(st, 'st_birthtime', None)),
            ("fs_mtime", st.st_mtime),
            ("fs_ctime", st.st_ctime),
        ]
        for lbl, raw in fs_candidates:
            candidates.append((lbl, raw, raw if raw else None))
    except Exception:
        pass

    # Choose first parsed in order we collected
    for source, raw, ts in candidates:
        # Special handling for MOV timezone-aware dates
        if source == 'comapplequicktimecreationdate' and isinstance(raw, str):
            try:
                # Format: "2025-11-25T17:41:26+0700" - already has timezone!
                # Extract the local time (what was recorded) and epoch
                dt_aware = datetime.fromisoformat(str(raw))
                local_time = dt_aware.replace(tzinfo=None)
                display = local_time.strftime("%Y/%m/%d %H:%M:%S")
                correct_epoch = dt_aware.timestamp()
                return (correct_epoch, display)
            except Exception:
                pass

        # Standard path for other timestamps
        if ts:
            # For MOV files, return tuple (epoch, display_string)
            if is_mov and source != 'comapplequicktimecreationdate':
                # Format the timestamp for MOV files
                display = datetime.fromtimestamp(ts).strftime("%Y/%m/%d %H:%M:%S")
                return (ts, display)
            # For other videos, return just the epoch
            return ts

    return 0

def get_file_creation_time(path):
    """Get file creation time with proper timezone handling.
    For images: EXIF is naive local time (return as string directly)
    For videos: MediaInfo contains UTC times (return as epoch)
    For filesystem: timestamps are UTC, convert to local display
    Returns tuple: (sortable_epoch, display_string)
    """
    try:
        suffix = path.suffix.lower()

        # For images: get EXIF datetime (naive local time stored as string)
        if suffix in SUPPORTED_IMAGES:
            exif_str = get_exif_datetime(path)
            if exif_str and exif_str != 0:
                # Parse it to get an epoch for sorting (treating string as naive/local)
                dt_obj = datetime.strptime(exif_str, DATETIME_FMT)
                sort_epoch = dt_obj.timestamp()
                return (sort_epoch, exif_str)

        # For videos: get MediaInfo metadata (UTC times converted to epoch)
        if suffix in SUPPORTED_VIDEOS:
            video_result = get_video_creation_time(path)

            # MOV files return (epoch, display_string) tuple
            if isinstance(video_result, tuple) and len(video_result) == 2:
                video_epoch, display = video_result
                return (video_epoch, display)

            # Other video formats return just the epoch
            if video_result > 0:
                # video_epoch is already UTC-converted epoch via calendar.timegm()
                # Display using fromtimestamp (converts UTC to local for display)
                display = datetime.fromtimestamp(video_result).strftime(DATETIME_FMT)
                return (video_result, display)

        # Fall back to filesystem timestamps (these are stored in UTC)
        stat = path.stat()
        times = []

        # Collect all available timestamps
        if hasattr(stat, 'st_birthtime'):
            times.append(stat.st_birthtime)
        times.append(stat.st_mtime)
        times.append(stat.st_ctime)

        # Return the earliest timestamp
        earliest = min(times)
        # Convert UTC epoch to local time for display
        display = datetime.fromtimestamp(earliest).strftime(DATETIME_FMT)
        return (earliest, display)
    except:
        return (0, "")


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

def parse_iso6709(iso_str):
    """Parse ISO 6709 format: +DD.DDDD+DDD.DDDD[+DDD.DDD]/
    Returns (lat, lon) or None."""
    if not iso_str:
        return None
    try:
        # Remove trailing /
        iso_str = str(iso_str).rstrip('/')
        # Pattern: +/-latitude +/-longitude [+/-altitude]
        # Use regex to extract the three parts
        m = re.match(r'([+-]?\d+\.?\d*)([+-]\d+\.?\d*)([+-]?\d+\.?\d*)?', iso_str)
        if m:
            lat = float(m.group(1))
            lon = float(m.group(2))
            return (lat, lon)
    except:
        pass
    return None

def get_video_gps(path):
    """Extract GPS coordinates from video metadata using hachoir and MediaInfo."""
    gps_candidates = []  # list of (source, lat, lon, raw_values)

    # Try hachoir first
    if HACHOIR_AVAILABLE:
        try:
            parser = createParser(str(path))
            if parser:
                metadata = extractMetadata(parser)
                if metadata:
                    lat = None
                    lon = None
                    hachoir_data = []
                    for line in metadata.exportPlaintext():
                        line_lower = line.lower()
                        hachoir_data.append(line)
                        if 'latitude' in line_lower and ':' in line:
                            try:
                                parts = line.split(':', 1)
                                if len(parts) == 2:
                                    lat_str = parts[1].strip().replace('+', '')
                                    lat = float(lat_str)
                            except:
                                pass
                        if 'longitude' in line_lower and ':' in line:
                            try:
                                parts = line.split(':', 1)
                                if len(parts) == 2:
                                    lon_str = parts[1].strip().replace('+', '')
                                    lon = float(lon_str)
                            except:
                                pass
                    if lat or lon:\
                        gps_candidates.append(('hachoir', lat, lon, hachoir_data))
                    else:
                        gps_candidates.append(('hachoir', None, None, hachoir_data))
                parser.stream._input.close()
        except:
            pass

    # Try MediaInfo next
    if MEDIAINFO_AVAILABLE:
        try:
            mi = MediaInfo.parse(path)
            if mi and mi.tracks:
                mediainfo_data = []
                lat = None
                lon = None
                for track in mi.tracks:
                    data = track.to_data() if hasattr(track, 'to_data') else {}
                    # Collect all metadata
                    for key, val in data.items():
                        if val:
                            mediainfo_data.append(f"{key}: {val}")
                            # Look for GPS fields
                            key_lower = key.lower()
                            # Check for ISO 6709 format (com.apple.quicktime.locationiso6709)
                            if 'iso6709' in key_lower:
                                iso_coords = parse_iso6709(val)
                                if iso_coords:
                                    lat, lon = iso_coords
                            if 'latitude' in key_lower or 'lat' in key_lower:
                                try:
                                    if isinstance(val, (list, tuple)):
                                        lat = float(val[0])
                                    else:
                                        lat = float(val)
                                except:
                                    pass
                            if 'longitude' in key_lower or 'lon' in key_lower:
                                try:
                                    if isinstance(val, (list, tuple)):
                                        lon = float(val[0])
                                    else:
                                        lon = float(val)
                                except:
                                    pass
                if lat or lon or mediainfo_data:
                    gps_candidates.append(('mediainfo', lat, lon, mediainfo_data))
        except:
            pass

    # Return first with actual coordinates
    for _, lat, lon, _ in gps_candidates:
        if lat and lon:
            return (lat, lon)
    return None

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
    """Get video duration in milliseconds using multiple methods for robustness.
    Tries TinyTag first, then falls back to MediaInfo if available.
    Returns duration in milliseconds or None.
    """
    try:
        tag = TinyTag.get(str(video_path), tags=False, duration=True)
        if tag and tag.duration:
            duration_ms = int(tag.duration * 1000)
            return duration_ms
    except Exception:
        pass

    if MEDIAINFO_AVAILABLE:
        try:
            mi = MediaInfo.parse(str(video_path))
            if mi and mi.tracks:
                for track in mi.tracks:
                    if track.track_type == "Video":
                        if hasattr(track, 'duration') and track.duration:
                            return int(track.duration)
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
            x_pos = event.position().x()
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
                x_pos = event.position().x()
                width = self.width()
                value = int((x_pos / width) * self.maximum())
                self.setValue(value)
        return super().mousePressEvent(event)

class PVAnnotator(QWidget):
    def __init__(self,start_path=None):
        super().__init__()
        # Set white background for the main widget
        self.setStyleSheet("QWidget { background-color: white; }")
        # Silence noisy Qt multimedia/ffmpeg logging in the console
        QLoggingCategory.setFilterRules("qt.multimedia.*=false\nqt.multimedia.ffmpeg*=false")
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
        self.position_box=QLineEdit()
        self.position_box.setFixedWidth(120)
        self.position_box.setFont(QFont("Arial",DEFAULT_FONT_SIZE))
        self.position_box.setAlignment(Qt.AlignCenter)
        self.position_box.editingFinished.connect(self.jump_to_position)
        self.skip_btn=QPushButton("Skip")
        self.trash_btn=QPushButton("Set Aside")
        self.rotate_btn=QPushButton("Rotate clockwise")
        self.volume_btn=QPushButton("100% volume")
        self.slide_btn=QPushButton("Slideshow")
        self.image_time_input=QLineEdit()
        self.image_time_input.setFixedWidth(97)
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
            # Fix for Linux/Mac: ensure button text is visible
            if sys.platform.startswith('linux') or sys.platform == 'darwin':
                btn.setStyleSheet("QPushButton { color: black; }")
        for b,f in [(self.prev_btn,self.prev_item),(self.next_btn,self.next_item),
                (self.skip_btn,self.skip_item),(self.trash_btn,self.trash_item),
                (self.rotate_btn,self.rotate_item),(self.volume_btn,self.change_volume),
                (self.slide_btn,self.toggle_slideshow)]: b.clicked.connect(lambda _, func=f: self.handle_button_click(func))

        self.datetime_box=QLineEdit(); self.datetime_box.setFont(QFont("Arial",DEFAULT_FONT_SIZE))
        self.datetime_box.editingFinished.connect(self.update_creation_time)
        self.datetime_box.setReadOnly(False)  # Editable by user
        self.filename_label=QLineEdit(); self.filename_label.setFont(QFont("Arial",DEFAULT_FONT_SIZE))
        self.filename_label.setReadOnly(True)  # Read-only display
        self.filename_label.setAlignment(Qt.AlignLeft)  # Left-justify text
        self.location_combo=QComboBox(); self.location_combo.setEditable(True)
        self.location_combo.setFont(QFont("Arial",DEFAULT_FONT_SIZE))
        self.location_combo.currentTextChanged.connect(self.update_location_text)
        self.text_box=QTextEdit(); self.text_box.setFixedHeight(75)
        self.text_box.setFont(QFont("Arial",DEFAULT_FONT_SIZE))
        self._text_change_in_progress = False

        self.skip_in_progress = False
        self.new_annotation_pending = False
        self.is_editing_annotation_mode = False
        self.text_scroll_timer = QTimer()
        self.text_scroll_timer.timeout.connect(self.scroll_annotation_text)
        self.text_scroll_pos = 0

        self.video_widget=QVideoWidget()
        self.video_widget.setStyleSheet("QVideoWidget { background-color: white; }")
        # Also set palette
        palette = self.video_widget.palette()
        palette.setColor(self.video_widget.backgroundRole(), QColor("white"))
        self.video_widget.setPalette(palette)
        self.video_player=QMediaPlayer()  # Qt6 disables hw accel by default
        self.audio_output=QAudioOutput()
        self.video_player.setAudioOutput(self.audio_output)
        self.video_player.setVideoOutput(self.video_widget)
        self.seek_in_progress = False
        self.video_slider=TimestampSlider()
        self.slider_style_default = """
            QSlider::groove:horizontal { background: #2a82da; height: 6px; border-radius: 3px; }
            QSlider::handle:horizontal { background: #2a82da; border: 1px solid #1c5fa5; width: 14px; margin: -4px 0; border-radius: 3px; }
        """
        self.slider_style_editing = """
            QSlider::groove:horizontal { background: #d9534f; height: 6px; border-radius: 3px; }
            QSlider::handle:horizontal { background: #d9534f; border: 1px solid #c9302c; width: 14px; margin: -4px 0; border-radius: 3px; }
        """
        self.video_slider.setStyleSheet(self.slider_style_default)
        self.video_slider.sliderMoved.connect(lambda pos: (self.video_player.setPosition(pos), self.update_editing_annotation_timestamp(pos)))
        self.video_slider.sliderReleased.connect(lambda: self.update_editing_annotation_timestamp())
        self.video_player.positionChanged.connect(lambda pos: self.update_video_annotation(pos))
        self.video_player.positionChanged.connect(lambda pos: self.video_slider.setValue(pos))
        self.video_player.durationChanged.connect(lambda d: self.video_slider.setMaximum(d))

        self.play_btn=QPushButton("Play/Pause"); self.play_btn.clicked.connect(lambda: self.handle_button_click(self.toggle_play))
        self.replay_btn=QPushButton("Replay"); self.replay_btn.clicked.connect(lambda: self.handle_button_click(self.replay_video))
        self.add_ann_btn=QPushButton("Add annotation"); self.add_ann_btn.clicked.connect(lambda: self.handle_button_click(self.add_annotation))
        self.edit_ann_btn=QPushButton("Edit annotation"); self.edit_ann_btn.clicked.connect(self.toggle_edit_mode)
        self.remove_ann_btn=QPushButton("Remove annotation"); self.remove_ann_btn.clicked.connect(lambda: self.handle_button_click(self.remove_annotation))
        self.skip_ann_btn=QPushButton("Skip segment"); self.skip_ann_btn.clicked.connect(lambda: self.handle_button_click(self.skip_until_next_annotation))
        # Make video button fonts bold
        for btn in [self.play_btn, self.replay_btn, self.add_ann_btn, self.edit_ann_btn, self.remove_ann_btn, self.skip_ann_btn]:
            btn.setFont(bold_font)
            # Fix for Linux/Mac: ensure button text is visible
            if sys.platform.startswith('linux') or sys.platform == 'darwin':
                btn.setStyleSheet("QPushButton { color: black; }")

        # Layout with minimal spacing
        layout=QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.image_label)
        layout.addWidget(self.video_widget)
        layout.addWidget(self.video_slider)
        video_btn_layout=QHBoxLayout()
        video_btn_layout.setSpacing(2)
        for b in [self.play_btn,self.add_ann_btn,self.edit_ann_btn,
                  self.remove_ann_btn,self.skip_ann_btn,self.replay_btn]: video_btn_layout.addWidget(b)
        layout.addLayout(video_btn_layout)
        button_layout=QHBoxLayout()
        button_layout.setSpacing(2)
        for b in [self.prev_btn,self.position_box,self.skip_btn,self.trash_btn,self.rotate_btn,self.volume_btn,self.slide_btn]: button_layout.addWidget(b)
        button_layout.addWidget(self.image_time_input)
        button_layout.addWidget(self.next_btn)
        layout.addLayout(button_layout)
        meta_layout=QHBoxLayout()
        meta_layout.setSpacing(2)
        meta_layout.addWidget(self.datetime_box,3); meta_layout.addWidget(self.filename_label,10); meta_layout.addWidget(self.location_combo,7)
        layout.addLayout(meta_layout)
        layout.addWidget(self.text_box)

        # Live-update the active annotation while typing
        self.text_box.textChanged.connect(self.update_active_annotation_text)

        # Override focus out to commit annotation
        orig_focus_out = self.text_box.focusOutEvent
        def text_focus_out(event):
            # Only call update_text() if not creating a new annotation
            # (new annotations are saved by save_pending_annotation instead)
            # Also avoid writing to the baseline 0.0 annotation while editing another
            # annotation; commit_editing_annotation handles that case instead.
            if not self.new_annotation_pending and not self.is_editing_annotation_mode:
                self.update_text()
            # Do not auto-commit edit mode on focus loss; finish_edit_mode handles it
            self.save_pending_annotation()         # commit new annotation if pending
            orig_focus_out(event)
        self.text_box.focusOutEvent = text_focus_out

        # Show placeholder image (app icon) until a folder is chosen
        self.show_placeholder_image()

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
        self.trash=self.dir/TRASH_DIR
        self.json_path=self.dir/JSON_NAME
        if self.json_path.exists():
            self.data=json.loads(self.json_path.read_text())
        else: self.data={"_settings":{"font_size":DEFAULT_FONT_SIZE,"image_time":DEFAULT_IMAGE_TIME}}
        # Normalize any stored creation times to the new string format
        self.normalize_creation_times()
        self.check_and_prompt_folders()
        # Get all media files
        all_files = list(self.get_all_media_files())
        # Cache creation times for new files (batch operation)
        needs_save = False
        for file_path in all_files:
            if file_path.name not in self.data or "creation_time" not in self.data.get(file_path.name, {}):
                self.get_cached_creation_time(file_path)
                needs_save = True
        if needs_save:
            self.save()
        # Sort using cached creation times
        self.media=sorted(all_files, key=lambda p: self.get_cached_creation_time(p))
        if start_path and start_path.is_file() and start_path in self.media:
            self.index=self.media.index(start_path)
        # Sort video annotations
        for entry in self.data.values():
            if "annotations" in entry and isinstance(entry["annotations"], list):
                entry["annotations"] = sorted(entry["annotations"], key=lambda a: a["time"])

        # Ensure every video has a baseline 0.0 annotation for the "no annotation yet" state
        for media_path in self.media:
            if media_path.suffix.lower() in SUPPORTED_VIDEOS:
                annotations = self.data.setdefault(media_path.name, {}).setdefault("annotations", [])
                if self.ensure_zero_annotation(annotations):
                    pass  # save happens later in show_item
        # Update image time display
        image_time = self.get_image_time()
        time_text = "second" if image_time == 1 else "seconds"
        # Format: show integers without decimal, floats with decimal
        if image_time == int(image_time):
            time_str = str(int(image_time))
        else:
            time_str = str(image_time)
        self.image_time_input.setText(f"{time_str} {time_text}")
        self.show_item()

    # ---------------- Helpers ----------------
    def normalize_creation_times(self):
        """Convert any numeric/legacy creation times to the saved string format."""
        changed = False
        for name, entry in self.data.items():
            if name == "_settings" or not isinstance(entry, dict):
                continue
            for key in ("creation_time", "creation_time_manual"):
                if key in entry:
                    ts = parse_creation_value(entry[key])
                    if ts is not None:
                        formatted = format_creation_timestamp(ts)
                        if entry[key] != formatted:
                            entry[key] = formatted
                            changed = True
        if changed:
            self.save()

    def current(self): return self.media[self.index]

    def get_cached_creation_time(self, file_path):
        """Get creation time from cache or filesystem, updating cache if needed."""
        filename = file_path.name
        entry = self.data.setdefault(filename, {})

        if "creation_time_manual" in entry:
            manual_value = entry["creation_time_manual"]
            if isinstance(manual_value, str):
                ts = parse_creation_value(manual_value)
                if ts is not None:
                    return ts
            if isinstance(manual_value, (int, float)):
                return float(manual_value)

        if "creation_time" in entry and entry["creation_time"] is not None:
            cached_ts = parse_creation_value(entry["creation_time"])
            if cached_ts is not None:
                return cached_ts

        # Only compute if not already cached
        creation_time_tuple = get_file_creation_time(file_path)

        # Handle tuple return (timestamp, display_string) or fallback to old behavior
        if isinstance(creation_time_tuple, tuple):
            creation_time, display_string = creation_time_tuple
        else:
            creation_time = creation_time_tuple
            display_string = None

        # If no valid creation time found, use default date (2100-01-01 00:10:00) to sort files to the end
        if creation_time == 0 or creation_time == "":
            default_date = datetime(2100, 1, 1, 0, 10, 0).timestamp()
            creation_time = default_date
            display_string = format_creation_timestamp(creation_time)
        elif display_string is None:
            # Only format if display_string wasn't already provided by get_file_creation_time()
            display_string = format_creation_timestamp(creation_time)

        entry["creation_time"] = display_string
        return creation_time

    def validate_datetime(self, dt_string):
        """Validate and convert YYYY/MM/DD HH:MM:SS (or legacy YYYY-MM-DD) to Unix timestamp."""
        for fmt in (DATETIME_FMT, LEGACY_DATETIME_FMT):
            try:
                dt_obj = datetime.strptime(dt_string.strip(), fmt)
                return dt_obj.timestamp()
            except ValueError:
                continue
        return None

    def get_relative_path(self, file_path):
        """Get relative path from self.dir for display, e.g., 'France/photo.jpg'."""
        try:
            return str(file_path.relative_to(self.dir))
        except ValueError:
            return file_path.name

    def get_visible_media(self):
        """Return media entries not marked as skipped."""
        return [p for p in self.media if not self.data.get(p.name, {}).get("skip", False)]

    def update_position_display(self):
        visible = self.get_visible_media()
        total = len(visible)
        if total == 0:
            text = "0 of 0"
        else:
            try:
                current_visible_index = visible.index(self.current()) + 1
            except ValueError:
                current_visible_index = 1
            text = f"{current_visible_index} of {total}"
        self.position_box.blockSignals(True)
        self.position_box.setText(text)
        self.position_box.blockSignals(False)

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
        entry = self.data.setdefault(p.name, {})
        location = entry.setdefault("location", {})

        # Skip if we already have automated location data
        if "automated_text" in location:
            return

        # Extract GPS from EXIF (images) or metadata (videos) if not already present
        if "latitude_longitude" not in location:
            # Try image EXIF first
            if p.suffix.lower() in SUPPORTED_IMAGES:
                gps = get_exif_gps(file_path)
            # Try video metadata
            elif p.suffix.lower() in SUPPORTED_VIDEOS:
                gps = get_video_gps(file_path)
            else:
                gps = None

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

        # Use cached creation time for display
        filename = p.name
        entry = self.data.get(filename, {})
        # Prefer manual timestamp when present, even if blank; fall back to auto/derived
        def to_display(raw_value):
            ts_val = parse_creation_value(raw_value)
            if ts_val is not None:
                return format_creation_timestamp(ts_val)
            return str(raw_value) if raw_value is not None else None

        ts = None
        if "creation_time_manual" in entry:
            ts = to_display(entry.get("creation_time_manual"))
        if ts is None and "creation_time" in entry:
            ts = to_display(entry.get("creation_time"))
        if ts is None:
            creation_time = self.get_cached_creation_time(p)
            ts = format_creation_timestamp(creation_time)

        # Update datetime box (editable)
        self.datetime_box.blockSignals(True)
        self.datetime_box.setText(ts)
        self.datetime_box.blockSignals(False)

        # Update filename label (read-only)
        display_path = self.get_relative_path(p)
        self.filename_label.blockSignals(True)
        self.filename_label.setText(display_path)
        self.filename_label.setCursorPosition(0)  # Keep cursor at start to show beginning of path
        self.filename_label.blockSignals(False)

        # Update position display (1-based, non-skipped)
        self.update_position_display()

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
            annotations = self.get_current_video_annotations()
            ann0 = next((a for a in annotations if a.get("time") == 0.0), None)
            self.text_box.setText(ann0.get("text", "") if ann0 else "")

        self.setFocus()
        # Media display
        if p.suffix.lower() in SUPPORTED_IMAGES:
            self.video_widget.hide(); self.video_slider.hide()
            for b in [self.play_btn,self.replay_btn,self.add_ann_btn,self.edit_ann_btn,
                      self.remove_ann_btn,self.skip_ann_btn]: b.hide()
            self.rotate_btn.show()
            self.volume_btn.hide()
            self.image_label.show()
            rot=entry.get("rotation",0)
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
            # Full reset: stop any current playback and reload source
            self.video_player.stop()
            self.video_player.setSource(QUrl())
            self.video_player.setSource(QUrl.fromLocalFile(str(p)))
            # Use a single-shot timer to allow the source to load before playing
            QTimer.singleShot(100, self.video_player.play)

        # Next/Prev labels stay constant now that position box exists
        self.prev_btn.setText("Previous")
        self.next_btn.setText("Next")
        self.save()

    def show_placeholder_image(self):
        """Display the app icon in the media area before any folder is opened."""
        icon_path = Path(__file__).parent / "app_icon.png"
        self.video_widget.hide(); self.video_slider.hide()
        # Keep the controls visible, just show the placeholder image
        self.image_label.show()
        if icon_path.exists():
            pix = QPixmap(str(icon_path))
            self.image_label.setPixmap(pix.scaled(800, 600, Qt.KeepAspectRatio))
        else:
            self.image_label.setText("Select a folder to begin")

    # ---------------- Video Annotation ----------------
    def ensure_zero_annotation(self, annotations):
        """Guarantee a time 0.0 annotation exists so the pre-first-annotation area stays blank."""
        zero_ann = next((a for a in annotations if a.get("time") == 0.0), None)
        added = False
        if zero_ann is None:
            annotations.append({"time": 0.0, "text": ""})
            added = True
        else:
            if "text" not in zero_ann:
                zero_ann["text"] = ""
                added = True
        if added:
            annotations.sort(key=lambda a: a["time"])
        return added

    def get_current_video_annotations(self):
        p = self.current()
        annotations = self.data.setdefault(p.name, {}).setdefault("annotations", [])
        if self.ensure_zero_annotation(annotations):
            self.save()
        return annotations

    def safe_seek(self, pos_ms, play_brief=False):
        """Programmatic seek that keeps slider and frames in sync, avoiding black screens."""
        self.seek_in_progress = True
        try:
            self.video_player.pause()
            self.video_player.setPosition(pos_ms)
            self.video_slider.setValue(pos_ms)

            def finalize():
                self.video_player.pause()
                self.video_slider.setValue(min(pos_ms, self.video_player.duration() or pos_ms))
                self.seek_in_progress = False
                # Refresh annotation text for the new position
                self.update_video_annotation(self.video_player.position())

            if play_brief:
                self.video_player.play()
                QTimer.singleShot(80, finalize)
            else:
                finalize()
        except Exception:
            self.seek_in_progress = False

    def jump_to_end_and_pause(self):
        """Seek to final frame and pause, keeping the last frame visible."""
        dur = self.video_player.duration()
        if dur <= 0:
            return
        # Seek slightly before the exact end to keep a frame available
        target = max(dur - 100, 0)
        self.safe_seek(target, play_brief=True)

    def jump_to_end_and_pause(self):
        """Seek to final frame and pause, keeping the last frame visible."""
        dur = self.video_player.duration()
        if dur <= 0:
            return
        end_pos = max(dur - 1, 0)  # stay within duration; 1 ms before end keeps last frame
        self.video_player.setPosition(end_pos)
        self.video_slider.setValue(end_pos)
        self.video_player.pause()

    def update_video_annotation(self, pos):

        if self.seek_in_progress:
            return

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
            self.text_box.blockSignals(True)
            self.text_box.setText("")
            self.text_box.blockSignals(False)
            return

        annotations.sort(key=lambda a: a["time"])

        active_ann = None
        for i, ann in enumerate(annotations):
            if ann["time"] <= pos_sec:
                active_ann = (i, ann)
            else:
                break

        if not active_ann:
            self.text_box.blockSignals(True)
            self.text_box.setText("")
            self.text_box.blockSignals(False)
            return

        i, ann = active_ann

        # Handle skip annotation
        if ann.get("skip", False):
            playback = self.video_player.playbackState() == QMediaPlayer.PlayingState

            if playback:
                # Skip automatically to next annotation or pause at end
                if i + 1 < len(annotations):
                    next_time = annotations[i + 1]["time"]
                    next_pos = int(next_time * 1000)
                    self.video_player.setPosition(next_pos)
                    self.video_slider.setValue(next_pos)
                    # Continue playing after skip
                else:
                    # Last annotation: just pause here
                    self.video_player.pause()
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
        self.stop_slideshow_if_running()
        p = self.current()
        if p.suffix.lower() not in SUPPORTED_VIDEOS:
            return

        # Use the slider's position (immediately reflects user drag) instead of the player
        # position, which can lag until the media seek completes.
        pos_sec = self.video_slider.value() / 1000.0
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
            next_pos = int(next_ann["time"] * 1000)
            self.video_player.setPosition(next_pos)
            self.video_slider.setValue(next_pos)
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
        self.stop_slideshow_if_running()
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
        self.stop_slideshow_if_running()
        p = self.current()
        if p.suffix.lower() not in SUPPORTED_VIDEOS:
            return

        # Commit any pending new annotation first
        self.save_pending_annotation()
        self.commit_editing_annotation()  # Commit any currently editing annotation

        # Use the slider's value, which reflects the exact position the user sees.
        pos_sec = self.video_slider.value() / 1000.0
        annotations = self.get_current_video_annotations()  # get real list
        annotations.sort(key=lambda a: a["time"])           # sort in-place

        # Pick the active annotation: the last one whose start time is <= position.
        idx = None
        for i, ann in enumerate(annotations):
            if ann["time"] <= pos_sec + 1e-6:  # tolerate tiny float drift
                idx = i
            else:
                break
        if idx is None:
            idx = 0
        self.editing_annotation = annotations[idx]
        self.editing_annotation_idx = idx
        self.text_box.setText(self.editing_annotation.get("text", ""))
        self.text_box.setFocus()
        cursor = self.text_box.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.text_box.setTextCursor(cursor)
        self.text_box.moveCursor(QTextCursor.End)
        self.text_box.ensureCursorVisible()
        self.set_slider_edit_mode(True)
        self.is_editing_annotation_mode = True
        self.edit_ann_btn.setText("Done editing")

    def commit_editing_annotation(self):
        if hasattr(self, "editing_annotation"):
            self.editing_annotation["text"] = self.text_box.toPlainText()
            self.save()
            # Keep index in sync only while editing; remove both markers together
            if hasattr(self, "editing_annotation_idx"):
                del self.editing_annotation_idx
            del self.editing_annotation
            self.set_slider_edit_mode(False)

    def update_editing_annotation_timestamp(self, pos_ms=None):
        """When editing, move the annotation start time to the slider (or player) position."""
        if not hasattr(self, "editing_annotation"):
            return

        p = self.current()
        if p.suffix.lower() not in SUPPORTED_VIDEOS:
            return

        # Prefer the slider value we were given; fall back to the player's position
        if pos_ms is None:
            pos_ms = self.video_player.position()

        pos_sec = pos_ms / 1000.0

        annotations = self.get_current_video_annotations()
        self.editing_annotation["time"] = pos_sec
        annotations.sort(key=lambda a: a["time"])
        self.save()

    def finish_edit_mode(self):
        """End editing: capture time/text, reset visuals."""
        if not self.is_editing_annotation_mode:
            return

        # Ensure latest slider position is written
        self.update_editing_annotation_timestamp()
        # Commit text
        self.commit_editing_annotation()
        # Reset visuals/state
        self.set_slider_edit_mode(False)

    def handle_button_click(self, func):
        """Finish editing (if active) before running a button action."""
        self.finish_edit_mode()
        func()

    def toggle_edit_mode(self):
        """Toggle between entering edit mode and finishing it."""
        self.stop_slideshow_if_running()
        if self.is_editing_annotation_mode:
            self.finish_edit_mode()
        else:
            self.edit_annotation()

    def set_slider_edit_mode(self, editing: bool):
        """Visually indicate edit mode on the slider."""
        self.video_slider.setStyleSheet(self.slider_style_editing if editing else self.slider_style_default)
        self.video_slider.update()
        self.is_editing_annotation_mode = editing
        self.edit_ann_btn.setText("Done editing" if editing else "Edit annotation")

    def _find_active_annotation(self):
        """Return the active annotation object based on the current slider position."""
        annotations = self.get_current_video_annotations()
        pos_sec = self.video_slider.value() / 1000.0
        active = None
        for ann in annotations:
            if ann.get("time", 0.0) <= pos_sec + 1e-6:
                active = ann
            else:
                break
        return active or annotations[0]

    def update_active_annotation_text(self):
        """While typing, pause video and write text into the active annotation."""
        if self._text_change_in_progress:
            return
        self._text_change_in_progress = True

        try:
            p = self.current()
            # Pause video while typing
            if p.suffix.lower() in SUPPORTED_VIDEOS and self.video_player.playbackState() == QMediaPlayer.PlayingState:
                self.video_player.pause()

            # When creating a new annotation, let save_pending_annotation handle persistence
            if self.new_annotation_pending:
                return

            if p.suffix.lower() in SUPPORTED_IMAGES:
                self.data.setdefault(p.name, {})["text"] = self.text_box.toPlainText()
            else:
                # If we're editing a specific annotation, keep using that; otherwise pick active
                if hasattr(self, "editing_annotation"):
                    target = self.editing_annotation
                else:
                    target = self._find_active_annotation()

                target["text"] = self.text_box.toPlainText()

            self.save()
        finally:
            self._text_change_in_progress = False

    # ---------------- Text Box Focus ----------------
    def text_focus_out(self, event):
        """Commit any new or edited annotation when text box loses focus."""
        # Keep edit mode active when focus leaves the text box; only finish via buttons.
        if not self.is_editing_annotation_mode:
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
        self.stop_slideshow_if_running()
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

        active_ann = annotations[active_idx]

        # Never remove the baseline 0.0 annotation; just clear its text
        if active_ann.get("time") == 0.0:
            active_ann["text"] = ""
            self.text_box.blockSignals(True)
            self.text_box.setText("")
            self.text_box.blockSignals(False)
            self.save()
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
            # For videos, write to the active annotation instead of forcing 0.0
            annotations=self.get_current_video_annotations()
            pos_sec = self.video_slider.value() / 1000.0
            active = None
            for ann in annotations:
                if ann.get("time",0.0) <= pos_sec + 1e-6:
                    active = ann
                else:
                    break
            if active is None:
                active = annotations[0]
            active["text"] = self.text_box.toPlainText()
        self.save()

    def update_location_text(self,text):
        p=self.current()
        self.data.setdefault(p.name,{}).setdefault("location",{})["manual_text"]=text
        self.save()

    def update_creation_time(self):
        """Parse and validate the user-edited creation time, immediately update display and resort."""
        p = self.current()
        text = self.datetime_box.text().strip()

        timestamp = self.validate_datetime(text)
        if timestamp is None:
            QMessageBox.warning(self, "Invalid Format", "Please use YYYY/MM/DD HH:MM:SS (e.g., 2024/12/31 14:30:00)")
            creation_time = self.get_cached_creation_time(p)
            ts = format_creation_timestamp(creation_time)
            self.datetime_box.blockSignals(True)
            self.datetime_box.setText(ts)
            self.datetime_box.blockSignals(False)
            return

        entry = self.data.setdefault(p.name, {})
        entry["creation_time_manual"] = text
        self.save()

        self.datetime_box.blockSignals(True)
        self.datetime_box.setText(text)
        self.datetime_box.blockSignals(False)

        self.media = sorted(self.media, key=lambda path: self.get_cached_creation_time(path))
        self.index = self.media.index(p) if p in self.media else 0
        self.update_position_display()

    def update_image_time(self):
        """Parse image time input and save to settings."""
        text = self.image_time_input.text()
        # Extract numbers from the text, split by spaces, take first number
        words = text.split()
        for word in words:
            # Strip out non-numeric characters except decimal point
            num_str = ''.join(c for c in word if c.isdigit() or c == '.')
            if num_str and num_str != '.':
                try:
                    new_time = float(num_str)
                    if new_time > 0:
                        self.data.setdefault("_settings", {})["image_time"] = new_time
                        time_text = "second" if new_time == 1 else "seconds"
                        # Format: show integers without decimal, floats with decimal
                        if new_time == int(new_time):
                            time_str = str(int(new_time))
                        else:
                            time_str = str(new_time)
                        self.image_time_input.setText(f"{time_str} {time_text}")
                        self.save()
                        return
                except ValueError:
                    pass
        # If no valid number found, reset to current value
        current_time = self.get_image_time()
        time_text = "second" if current_time == 1 else "seconds"
        if current_time == int(current_time):
            time_str = str(int(current_time))
        else:
            time_str = str(current_time)
        self.image_time_input.setText(f"{time_str} {time_text}")

    # ---------------- Navigation ----------------
    def jump_to_position(self):
        """Jump to a 1-based position within non-skipped media."""
        visible = self.get_visible_media()
        total = len(visible)
        if total == 0:
            self.update_position_display()
            return

        raw = self.position_box.text().split('of')[0].strip()
        try:
            target = int(raw)
        except ValueError:
            self.update_position_display()
            return

        target = max(1, min(total, target))
        # Update display with clamped value
        self.position_box.blockSignals(True)
        self.position_box.setText(f"{target} of {total}")
        self.position_box.blockSignals(False)

        target_path = visible[target - 1]
        if target_path in self.media:
            self.index = self.media.index(target_path)
            self.show_item()

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
        # Cycle through 0, 270, 180, 90 (clockwise)
        new_rotation=(current_rotation-90)%360
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
        file_parent = p.parent
        trash_dir = file_parent / TRASH_DIR
        trash_dir.mkdir(exist_ok=True)
        shutil.move(str(p), trash_dir / p.name)
        self.media.remove(p)
        self.index = min(self.index, len(self.media) - 1) if self.media else 0
        self.save()
        if self.media:
            self.show_item()

    def stop_slideshow_if_running(self):
        """Stop slideshow if it's currently running and reset button text."""
        if self.slideshow:
            self.slideshow = False
            self.slide_btn.setText("Slideshow")
            self.timer.stop()
            self.text_scroll_timer.stop()

    def get_effective_video_duration_ms(self, video_path):
        """Get the effective duration of a video considering skipped segments.
        Returns the end time of the last non-skipped segment in milliseconds.
        If all segments are skipped or the last segment is skipped, returns the appropriate end time."""
        annotations = self.data.get(video_path.name, {}).get("annotations", [])
        if not annotations:
            return get_video_duration_ms(video_path)

        # Sort annotations by time
        annotations = sorted(annotations, key=lambda a: a["time"])

        # Find the last non-skipped segment
        last_non_skipped_time = 0.0
        for i, ann in enumerate(annotations):
            if not ann.get("skip", False):
                last_non_skipped_time = ann["time"]
                # Check if there's a next annotation (which marks the end of this segment)
                if i + 1 < len(annotations):
                    segment_end_time = annotations[i + 1]["time"]
                else:
                    # This is the last annotation, use full video duration from this point
                    full_duration_ms = get_video_duration_ms(video_path)
                    if full_duration_ms:
                        return full_duration_ms
                    else:
                        return int(ann["time"] * 1000)

        # If we get here, return the time we should wait until
        # If last_non_skipped_time is still 0, all segments are skipped, so return immediately
        if last_non_skipped_time == 0.0 and annotations and annotations[0].get("skip", False):
            # All segments are skipped, return the time of the first annotation
            return int(annotations[0]["time"] * 1000) if annotations[0]["time"] > 0 else 100

        # Check if the last annotation is non-skipped and get when its content ends
        last_ann = annotations[-1]
        if not last_ann.get("skip", False):
            # Last annotation is not skipped
            full_duration_ms = get_video_duration_ms(video_path)
            if full_duration_ms:
                return full_duration_ms
            else:
                return int(last_ann["time"] * 1000)

        # Last annotation is skipped, return when it starts
        return int(last_ann["time"] * 1000)

    def stop_slideshow_if_running(self):
        """Stop slideshow if it's currently running and reset button text."""
        if self.slideshow:
            self.slideshow = False
            self.slide_btn.setText("Slideshow")
            self.timer.stop()
            self.text_scroll_timer.stop()

    def toggle_slideshow(self):
        self.slideshow=not self.slideshow
        self.text_scroll_timer.stop()
        if self.slideshow:
            self.slide_btn.setText("Stop slideshow")
            p=self.current()
            image_time = self.get_image_time()
            image_time_ms = int(image_time * 1000)
            if p.suffix.lower() in SUPPORTED_VIDEOS:
                # For videos, get effective duration considering skipped segments
                # But if image_time <= 1 second, use image_time to allow fast navigation
                if image_time <= 1:
                    self.timer.start(image_time_ms)
                else:
                    dur_ms = self.get_effective_video_duration_ms(p)
                    if dur_ms and dur_ms > 0:
                        self.timer.start(dur_ms)
                    else:
                        self.timer.start(image_time_ms)
            else:
                # For images, use word count timing only if delay > 1 second
                if image_time > 1:
                    duration=max(image_time,len(self.text_box.toPlainText().split())/4)*1000
                    self.timer.start(int(duration))
                    self.start_text_scroll(int(duration))
                else:
                    # Fast navigation mode: fixed delay time, no text scrolling
                    self.timer.start(image_time_ms)
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
        image_time = self.get_image_time()
        image_time_ms = int(image_time * 1000)
        if p.suffix.lower() in SUPPORTED_IMAGES:
            # For images, use word count timing only if delay > 1 second
            if image_time > 1:
                duration=max(image_time,len(self.text_box.toPlainText().split())/4)*1000
                self.timer.start(int(duration))
                self.start_text_scroll(int(duration))
            else:
                # Fast navigation mode: fixed delay time, no text scrolling
                self.timer.start(image_time_ms)
        else:
            # For videos, get effective duration considering skipped segments
            # But if image_time <= 1 second, use image_time to allow fast navigation
            if image_time <= 1:
                self.timer.start(image_time_ms)
            else:
                dur_ms = self.get_effective_video_duration_ms(p)
                if dur_ms and dur_ms > 0:
                    self.timer.start(dur_ms)
                else:
                    self.timer.start(image_time_ms)

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
        self.stop_slideshow_if_running()
        if self.video_player.playbackState()==QMediaPlayer.PlayingState: self.video_player.pause()
        else: self.video_player.play()

    def replay_video(self):
        """Completely reset video and replay from start."""
        self.stop_slideshow_if_running()
        p = self.current()
        if p.suffix.lower() in SUPPORTED_VIDEOS:
            # Full reset: stop, clear source completely, then reload to clear decoder state
            self.video_player.stop()
            self.video_player.setSource(QUrl())  # Clear source first
            QTimer.singleShot(10, lambda: self._replay_video_continue(p))

    def _replay_video_continue(self, p):
        """Second stage of replay: reload and play."""
        self.video_player.setSource(QUrl.fromLocalFile(str(p)))
        QTimer.singleShot(100, lambda: (self.video_player.setPosition(0), self.video_player.play()))

    # ---------------- Keyboard ----------------
    def keyPressEvent(self,event):
        if event.key()==Qt.Key_Right: self.next_item()
        elif event.key()==Qt.Key_Left: self.prev_item()
        else: super().keyPressEvent(event)

if __name__=="__main__":
    # Suppress FFmpeg's stderr output (AAC codec warnings, etc.)
    devnull = open(os.devnull, 'w')
    old_stderr = sys.stderr
    sys.stderr = devnull

    app=QApplication(sys.argv)

    # Restore stderr after Qt initialization
    sys.stderr = old_stderr
    devnull.close()

    start_path=sys.argv[1] if len(sys.argv)>1 else None
    w=PVAnnotator(start_path)
    w.show()
    sys.exit(app.exec())
