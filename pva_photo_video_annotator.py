import sys, json, shutil, re, calendar
from pathlib import Path
from datetime import datetime
from bisect import bisect_right
import requests
import os
from tinytag import TinyTag
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton,
    QTextEdit, QVBoxLayout, QHBoxLayout, QComboBox, QSlider, QFileDialog, QMessageBox, QLineEdit, QProgressDialog)
from PySide6.QtCore import Qt, QTimer, QUrl, QPoint, QLoggingCategory, QRect
from PySide6.QtGui import QPixmap, QImage, QFont, QColor, QTextCursor, QPainter, QPen
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
PVA_DATA_DIR = "pva_data"  # Directory to store annotations and backups
TRASH_DIR = "discarded"  # Use "set_aside" if it exists for backward compatibility
DEFAULT_FONT_SIZE = 14
DEFAULT_IMAGE_TIME = 5  # seconds per image
DATETIME_FMT = "%Y/%m/%d %H:%M:%S"
LEGACY_DATETIME_FMT = "%Y-%m-%d %H:%M:%S"

def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller."""
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = Path(sys._MEIPASS)
    except Exception:
        base_path = Path(__file__).parent
    return base_path / relative_path

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
    Extracts timezone-aware creation date when available (all video formats).
    Returns tuple (epoch, display_string, has_timezone, tz_label) where:
      - epoch: Unix timestamp (UTC) for sorting
      - display_string: Wall-clock time as recorded by camera (local time)
      - has_timezone: True if timezone info was found and used, False if using fallback
      - tz_label: A human-readable timezone offset like "+07:00" when available, else None
    """
    if not MEDIAINFO_AVAILABLE:
        return (0, "", False, None)
    try:
        mi = MediaInfo.parse(path)
    except Exception:
        mi = None

    candidates = []  # list of tuples (source, raw_value, parsed_ts)

    def format_offset(tzinfo_obj):
        """Return +HH:MM or -HH:MM from tzinfo.utcoffset()."""
        try:
            offset = tzinfo_obj.utcoffset(None)
            if offset is None:
                return None
            total_minutes = int(offset.total_seconds() // 60)
            sign = "+" if total_minutes >= 0 else "-"
            total_minutes = abs(total_minutes)
            hours, minutes = divmod(total_minutes, 60)
            return f"{sign}{hours:02d}:{minutes:02d}"
        except Exception:
            return None

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
        # Example: "2025-11-28T09:12:31+0700" (already has timezone!)
        # This works for both .mov and .mp4 files with QuickTime metadata
        qt_date = data.get('comapplequicktimecreationdate')
        if qt_date:
            # Add with special marker so we handle it separately with timezone awareness
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
        # Special handling for timezone-aware dates (both MOV and MP4)
        if source == 'comapplequicktimecreationdate' and isinstance(raw, str):
            try:
                # Format: "2025-11-28T09:12:31+0700" - ISO 8601 with timezone offset
                # datetime.fromisoformat() handles the +0700 timezone correctly
                dt_aware = datetime.fromisoformat(str(raw))
                # Extract the local/wall-clock time (what the camera showed)
                local_time = dt_aware.replace(tzinfo=None)
                display = local_time.strftime("%Y/%m/%d %H:%M:%S")
                # Epoch is the UTC moment equivalent to this local time
                correct_epoch = dt_aware.timestamp()
                tz_label = format_offset(dt_aware.tzinfo)
                return (correct_epoch, display, True, tz_label)  # True = timezone was found
            except Exception:
                pass

        # Standard path for other timestamps (no timezone info)
        if ts:
            display = datetime.fromtimestamp(ts).strftime("%Y/%m/%d %H:%M:%S")
            return (ts, display, False, None)  # False = no timezone info

    # No valid creation time found
    return (0, "", False, None)

def get_file_creation_time(path):
    """Get file creation time with proper timezone handling.
    For images: EXIF is naive local time (extracted as wall-clock)
    For videos: MediaInfo contains timezone-aware QuickTime dates (extract wall-clock from tz)
    For filesystem: timestamps are UTC, use as fallback
    Returns tuple: (sortable_epoch, display_string, has_timezone, tz_label)
      - sortable_epoch: Unix timestamp for sorting
      - display_string: Wall-clock time (camera's local time)
      - has_timezone: True if timezone info was found, False if using fallback
      - tz_label: human-readable tz offset like "+07:00" when known, else None
    """
    try:
        suffix = path.suffix.lower()

        # For images: get EXIF datetime (naive local time, assume camera's local timezone)
        if suffix in SUPPORTED_IMAGES:
            exif_str = get_exif_datetime(path)
            if exif_str and exif_str != 0:
                # Parse it to get an epoch for sorting (treating string as naive/local)
                dt_obj = datetime.strptime(exif_str, DATETIME_FMT)
                sort_epoch = dt_obj.timestamp()
                return (sort_epoch, exif_str, False, None)  # EXIF has no tz info, needs inference

        # For videos: get MediaInfo metadata with timezone extraction
        if suffix in SUPPORTED_VIDEOS:
            video_result = get_video_creation_time(path)

            # get_video_creation_time returns (epoch, display_string, has_timezone)
            if isinstance(video_result, tuple) and len(video_result) == 4:
                video_epoch, display, has_tz, tz_label = video_result
                if video_epoch > 0:  # Valid result found
                    return (video_epoch, display, has_tz, tz_label)

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
        return (earliest, display, False, None)  # False because filesystem has no tz info
    except:
        return (0, "", False, None)


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
                # Emit sliderMoved signal to trigger position and annotation updates
                self.sliderMoved.emit(value)
        return super().mousePressEvent(event)

class CropImageLabel(QLabel):
    """Custom label for handling crop selection on images."""
    crop_selected = None  # Signal-like attribute, will be set by parent

    def __init__(self, alignment=None, parent=None):
        super().__init__(parent)
        if alignment:
            self.setAlignment(alignment)
        self.crop_mode = False
        self.crop_start = None
        self.crop_rect = None
        self.original_pixmap = None
        self.setMouseTracking(True)

    def mousePressEvent(self, event):
        if self.crop_mode and self.original_pixmap:
            self.crop_start = event.position()
            self.crop_rect = None
            self.update()

    def mouseMoveEvent(self, event):
        if self.crop_mode and self.crop_start and self.original_pixmap:
            # Create rectangle from start to current position
            self.crop_rect = (self.crop_start, event.position())
            self.update()

    def mouseReleaseEvent(self, event):
        if self.crop_mode and self.crop_start and self.original_pixmap:
            # Finalize crop
            end_pos = event.position()

            # Check if a meaningful rectangle was drawn
            if abs(end_pos.x() - self.crop_start.x()) > 5 and abs(end_pos.y() - self.crop_start.y()) > 5:
                if callable(self.crop_selected):
                    # Get the actual pixmap being displayed
                    pixmap = self.pixmap()
                    if pixmap:
                        # The label is aligned center, so we need to account for offsets
                        label_rect = self.contentsRect()
                        pix_width = pixmap.width()
                        pix_height = pixmap.height()

                        # Calculate the centered position of the pixmap in the label
                        pix_x = (label_rect.width() - pix_width) / 2
                        pix_y = (label_rect.height() - pix_height) / 2

                        # Convert label coordinates to image coordinates
                        x1 = int((self.crop_start.x() - pix_x) * self.original_pixmap.width() / pix_width)
                        y1 = int((self.crop_start.y() - pix_y) * self.original_pixmap.height() / pix_height)
                        x2 = int((end_pos.x() - pix_x) * self.original_pixmap.width() / pix_width)
                        y2 = int((end_pos.y() - pix_y) * self.original_pixmap.height() / pix_height)

                        # Clamp to image bounds
                        x1 = max(0, min(x1, self.original_pixmap.width()))
                        y1 = max(0, min(y1, self.original_pixmap.height()))
                        x2 = max(0, min(x2, self.original_pixmap.width()))
                        y2 = max(0, min(y2, self.original_pixmap.height()))

                        # Ensure coordinates are in order
                        crop_coords = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
                        self.crop_selected(crop_coords)

            self.crop_start = None
            self.crop_rect = None
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        # Draw crop selection rectangle
        if self.crop_mode and self.crop_rect:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)

            # Calculate width and height properly
            x1 = min(self.crop_rect[0].x(), self.crop_rect[1].x())
            y1 = min(self.crop_rect[0].y(), self.crop_rect[1].y())
            x2 = max(self.crop_rect[0].x(), self.crop_rect[1].x())
            y2 = max(self.crop_rect[0].y(), self.crop_rect[1].y())

            width = x2 - x1
            height = y2 - y1

            # Draw semi-transparent blue rectangle
            color = QColor(0, 0, 255, 50)
            painter.fillRect(int(x1), int(y1), int(width), int(height), color)

            # Draw blue border
            painter.setPen(QPen(QColor(0, 0, 255), 2))
            painter.drawRect(int(x1), int(y1), int(width), int(height))

class PVAnnotator(QWidget):
    def __init__(self,start_path=None):
        super().__init__()
        # Set white background for the main widget
        self.setStyleSheet("QWidget { background-color: white; }")
        # Silence noisy Qt multimedia/ffmpeg logging in the console
        QLoggingCategory.setFilterRules("qt.multimedia.*=false\nqt.multimedia.ffmpeg*=false")
        self.setWindowTitle("PVA Photo Video Annotator")
        # Set window icon for taskbar
        icon_path = resource_path("app_icon.png")
        if icon_path.exists():
            self.setWindowIcon(QPixmap(str(icon_path)))
        self.setGeometry(QApplication.primaryScreen().availableGeometry())
        self.showMaximized()

        self.dir=None; self.media=[]; self.index=0
        self.data={}; self.slideshow=False
        self.data_changed = False  # Track if data has been modified and needs saving
        self.timer=QTimer(); self.timer.timeout.connect(self.advance_slideshow)
        self.media_to_data_key = {}  # Maps index in self.media to data key (may include ##version)

        # Widgets
        self.image_label=CropImageLabel(alignment=Qt.AlignCenter)
        self.image_label.setStyleSheet("background-color: white;")
        self.image_label.crop_selected = self.apply_crop  # Set the crop callback
        self.prev_btn=QPushButton("Previous")
        self.position_box=QLineEdit()
        self.position_box.setFixedWidth(120)
        self.position_box.setFont(QFont("Arial",DEFAULT_FONT_SIZE))
        self.position_box.setAlignment(Qt.AlignCenter)
        self.position_box.editingFinished.connect(self.jump_to_position)
        self.skip_btn=QPushButton("Skip")
        self.show_skipped_btn=QPushButton("Show Skipped")
        self.show_skipped_mode = False  # Track whether we're in show skipped mode
        self.trash_btn=QPushButton("Discard")
        self.rotate_btn=QPushButton("Rotate clockwise")
        self.duplicate_btn=QPushButton("Duplicate")
        self.crop_btn=QPushButton("Crop")
        self.crop_mode = False  # Track whether we're in crop selection mode
        self.crop_start = None  # Starting point for crop selection
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
        for btn in [self.prev_btn, self.skip_btn, self.show_skipped_btn, self.trash_btn, self.rotate_btn, self.duplicate_btn, self.crop_btn, self.volume_btn, self.slide_btn, self.next_btn]:
            btn.setFont(bold_font)
            # Fix for Linux/Mac: ensure button text is visible
            if sys.platform.startswith('linux') or sys.platform == 'darwin':
                btn.setStyleSheet("QPushButton { color: black; }")
        for b,f in [(self.prev_btn,self.prev_item),(self.next_btn,self.next_item),
                (self.skip_btn,self.skip_item),(self.show_skipped_btn,self.toggle_show_skipped),
                (self.trash_btn,self.trash_item),
                (self.rotate_btn,self.rotate_item),(self.duplicate_btn,self.duplicate_item),(self.crop_btn,self.toggle_crop_mode),
                (self.volume_btn,self.change_volume),(self.slide_btn,self.toggle_slideshow)]: b.clicked.connect(lambda _, func=f: self.handle_button_click(func))

        self.datetime_box=QLineEdit(); self.datetime_box.setFont(QFont("Arial",DEFAULT_FONT_SIZE))
        self.datetime_box.editingFinished.connect(self.update_creation_time)
        self.datetime_box.setReadOnly(False)  # Editable by user
        self.filename_label=QLineEdit(); self.filename_label.setFont(QFont("Arial",DEFAULT_FONT_SIZE))
        self.filename_label.setReadOnly(True)  # Read-only display
        self.filename_label.setAlignment(Qt.AlignLeft)  # Left-justify text
        self.location_combo=QComboBox(); self.location_combo.setEditable(True)
        self.location_combo.setFont(QFont("Arial",DEFAULT_FONT_SIZE))
        self.location_combo.setMaxVisibleItems(15)  # Show up to 15 items before scrolling
        # Make dropdown button wider and add padding to prevent text overlap
        self.location_combo.setStyleSheet("QComboBox::drop-down { width: 50px; } QComboBox { padding-right: 55px; }")
        self.location_combo.currentTextChanged.connect(self.update_location_text)
        # Search controls
        self.search_left_btn=QPushButton("<"); self.search_left_btn.setMaximumWidth(35)
        self.search_left_btn.clicked.connect(lambda: self.search_files(direction=-1))
        self.search_box=QLineEdit(); self.search_box.setPlaceholderText("Search")
        self.search_box.textChanged.connect(lambda: self.search_files(direction=0))
        self.search_right_btn=QPushButton(">"); self.search_right_btn.setMaximumWidth(35)
        self.search_right_btn.clicked.connect(lambda: self.search_files(direction=1))
        self.text_box=QTextEdit(); self.text_box.setFixedHeight(75)
        self.text_box.setFont(QFont("Arial",DEFAULT_FONT_SIZE))
        # Only accept plain text to prevent formatting from pasted content
        self.text_box.setAcceptRichText(False)
        self._text_change_in_progress = False

        self.skip_in_progress = False
        self.new_annotation_pending = False
        self.is_editing_annotation_mode = False
        self.text_scroll_timer = QTimer()
        self.text_scroll_timer.timeout.connect(self.scroll_annotation_text)
        self.text_scroll_pos = 0

        self.video_widget=QVideoWidget()
        self.video_widget.setAutoFillBackground(True)
        self.video_widget.setStyleSheet("QVideoWidget { background-color: white; border: none; }")
        # Set palette for background
        palette = self.video_widget.palette()
        palette.setColor(self.video_widget.backgroundRole(), QColor("white"))
        palette.setColor(self.video_widget.foregroundRole(), QColor("white"))
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
        self.video_player.mediaStatusChanged.connect(self.handle_video_end)

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
        for b in [self.prev_btn,self.position_box,self.skip_btn,self.show_skipped_btn,self.trash_btn,self.rotate_btn,self.duplicate_btn,self.crop_btn,self.volume_btn,self.slide_btn]: button_layout.addWidget(b)
        button_layout.addWidget(self.image_time_input)
        button_layout.addWidget(self.next_btn)
        layout.addLayout(button_layout)
        meta_layout=QHBoxLayout()
        meta_layout.setSpacing(2)
        # Datetime: 2.7 (reduced 10%), filename: 6.4, search: 1.8 (increased 10%), location: 6.3
        meta_layout.addWidget(self.datetime_box, 2.7)
        meta_layout.addWidget(self.filename_label, 6.4)
        meta_layout.addWidget(self.search_left_btn, 0.3)
        meta_layout.addWidget(self.search_box, 1.8)  # 10% larger to compensate for datetime reduction
        meta_layout.addWidget(self.search_right_btn, 0.3)
        meta_layout.addWidget(self.location_combo, 6.3)
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

        # Set up pva_data directory and handle migration
        self.pva_data_dir = self.dir / PVA_DATA_DIR
        self.pva_data_dir.mkdir(exist_ok=True)
        self.json_path = self.pva_data_dir / JSON_NAME

        # Migrate annotations.json from root to pva_data if needed
        old_json_path = self.dir / JSON_NAME
        if old_json_path.exists() and not self.json_path.exists():
            # Move the old file to the new location
            shutil.move(str(old_json_path), str(self.json_path))

        if self.json_path.exists():
            self.data=json.loads(self.json_path.read_text())
        else: self.data={"_settings":{"font_size":DEFAULT_FONT_SIZE,"image_time":DEFAULT_IMAGE_TIME}}
        # Normalize any stored creation times to the new string format
        self.normalize_creation_times()
        self.check_and_prompt_folders()
        # Inform user while we load and compute timestamps
        try:
            self.text_box.blockSignals(True)
            self.text_box.setText("Loading data and checking file creation times")
        finally:
            self.text_box.blockSignals(False)
        # Force UI update so user sees the message
        QApplication.processEvents()
        # Get all media files
        all_files = list(self.get_all_media_files())

        # Build a map of base filenames to their versioned keys
        from collections import defaultdict
        base_to_versions = defaultdict(list)
        for data_key in self.data.keys():
            if data_key != "_settings":
                base = self.get_base_filename(data_key)
                base_to_versions[base].append(data_key)

        # Step 1: Ensure all files have creation_time_utc and local_time_zone (if available)
        needs_save = False
        for file_path in all_files:
            base = self.get_base_filename(file_path.name)
            # Check if this file has versioned entries - if so, skip creating a base entry
            versions = base_to_versions.get(base, [])
            has_versioned_entries = any("##" in v for v in versions)

            # Only process if: no versions exist, OR this exact filename exists in data
            if not has_versioned_entries:
                if file_path.name not in self.data or "creation_time_utc" not in self.data.get(file_path.name, {}):
                    self.get_cached_creation_time(file_path)
                    needs_save = True
        if needs_save:
            self.save()

        # Step 2: Sort all data entries by creation_time_utc for timezone inference
        # Use all keys (including versioned ones), not just physical files
        all_data_keys = [k for k in self.data.keys() if k != "_settings"]
        sorted_keys = sorted(all_data_keys, key=lambda k: self.data.get(k, {}).get("creation_time_utc", 9999999999))

        # Step 3: Infer timezones for files without them
        last_known_tz = None
        for data_key in sorted_keys:
            entry = self.data.get(data_key, {})
            if "local_time_zone" in entry:
                last_known_tz = entry["local_time_zone"]
            elif last_known_tz and "local_time_zone_inferred" not in entry:
                entry["local_time_zone_inferred"] = last_known_tz
                needs_save = True

        if needs_save:
            self.save()

        # Step 4: Compute creation_date_time using actual or inferred timezone.
        # If we only have a wall-clock (no tz), keep that wall-clock untouched.
        save_needed = False
        for data_key in all_data_keys:
            entry = self.data.get(data_key, {})
            utc_epoch = entry.get("creation_time_utc", 0)
            tz_str = entry.get("local_time_zone") or entry.get("local_time_zone_inferred")
            naive_wall_clock = entry.get("creation_local_naive")

            # If we have a wall-clock and a timezone, recompute both UTC and display time from that wall-clock
            if tz_str and naive_wall_clock:
                try:
                    from datetime import timezone, timedelta
                    sign = 1 if tz_str[0] == '+' else -1
                    hours, minutes = map(int, tz_str[1:].split(':'))
                    offset = timedelta(hours=sign*hours, minutes=sign*minutes)
                    tz = timezone(offset)
                    dt_local = datetime.strptime(naive_wall_clock, DATETIME_FMT)
                    dt_local = dt_local.replace(tzinfo=tz)
                    entry["creation_time_utc"] = dt_local.astimezone(timezone.utc).timestamp()
                    entry["creation_date_time"] = naive_wall_clock
                    save_needed = True
                    continue
                except Exception:
                    pass

            # If we have timezone and UTC, compute display time
            if tz_str and utc_epoch:
                try:
                    from datetime import timezone, timedelta
                    sign = 1 if tz_str[0] == '+' else -1
                    hours, minutes = map(int, tz_str[1:].split(':'))
                    offset = timedelta(hours=sign*hours, minutes=sign*minutes)
                    tz = timezone(offset)
                    local_dt = datetime.fromtimestamp(utc_epoch, tz=tz)
                    entry["creation_date_time"] = local_dt.strftime(DATETIME_FMT)
                    save_needed = True
                    continue
                except Exception:
                    pass

            # If no timezone but we have a wall-clock, keep it as-is
            if naive_wall_clock:
                entry.setdefault("creation_date_time", naive_wall_clock)
                continue

            # Fallbacks when we have only UTC
            if utc_epoch:
                entry["creation_date_time"] = format_creation_timestamp(utc_epoch)
                save_needed = True

        if save_needed:
            self.save()

        # Step 5: Sort using creation_date_time (or creation_time_manual if present)
        def sort_key(p):
            entry = self.data.get(p.name, {})
            # Priority: creation_time_manual > creation_date_time
            if "creation_time_manual" in entry:
                ts = parse_creation_value(entry["creation_time_manual"])
                if ts is not None:
                    version_suffix = self.get_version_suffix(p.name)
                    return (ts, version_suffix)
            if "creation_date_time" in entry:
                ts = parse_creation_value(entry["creation_date_time"])
                if ts is not None:
                    version_suffix = self.get_version_suffix(p.name)
                    return (ts, version_suffix)
            version_suffix = self.get_version_suffix(p.name)
            return (9999999999, version_suffix)  # Far future for files with no time

        # Before sorting, expand media list to include all versioned entries
        # Build mapping from base filename to all versioned keys in JSON
        from collections import defaultdict
        base_to_versions = defaultdict(list)
        for data_key in self.data.keys():
            if data_key != "_settings":
                base = self.get_base_filename(data_key)
                base_to_versions[base].append(data_key)

        # Now build the expanded media list with versioned entries
        expanded_media = []
        temp_media_to_data_key = {}

        for file_path in all_files:
            base = self.get_base_filename(file_path.name)
            versions = base_to_versions.get(base, [file_path.name])

            # If no versioned entries exist, use the filename itself
            if not versions or (len(versions) == 1 and versions[0] == file_path.name):
                expanded_media.append(file_path)
                temp_media_to_data_key[len(expanded_media) - 1] = file_path.name
            else:
                # Add file once for each versioned entry
                for version_key in sorted(versions):
                    expanded_media.append(file_path)
                    temp_media_to_data_key[len(expanded_media) - 1] = version_key

        # Sort the expanded media by timestamp and version
        def sort_key_indexed(idx):
            data_key = temp_media_to_data_key[idx]
            entry = self.data.get(data_key, {})
            if "creation_time_manual" in entry:
                ts = parse_creation_value(entry["creation_time_manual"])
                if ts is not None:
                    version_suffix = self.get_version_suffix(data_key)
                    return (ts, version_suffix)
            if "creation_date_time" in entry:
                ts = parse_creation_value(entry["creation_date_time"])
                if ts is not None:
                    version_suffix = self.get_version_suffix(data_key)
                    return (ts, version_suffix)
            version_suffix = self.get_version_suffix(data_key)
            return (9999999999, version_suffix)

        sorted_indices = sorted(range(len(expanded_media)), key=sort_key_indexed)
        self.media = [expanded_media[i] for i in sorted_indices]

        # Build final mapping with sorted indices
        old_to_new = {old_idx: new_idx for new_idx, old_idx in enumerate(sorted_indices)}
        self.media_to_data_key = {old_to_new[i]: temp_media_to_data_key[i] for i in temp_media_to_data_key}

        if start_path and start_path.is_file() and start_path in self.media:
            self.index=self.media.index(start_path)
        # Sort video annotations
        for entry in self.data.values():
            if "annotations" in entry and isinstance(entry["annotations"], list):
                entry["annotations"] = sorted(entry["annotations"], key=lambda a: a["time"])

        # Deduplicate and ensure every video has a baseline 0.0 annotation
        needs_save_after_dedup = False
        for idx, media_path in enumerate(self.media):
            if media_path.suffix.lower() in SUPPORTED_VIDEOS:
                data_key = self.get_data_key(idx)
                annotations = self.data.setdefault(data_key, {}).setdefault("annotations", [])
                # First deduplicate any duplicate timestamps
                if self.deduplicate_annotations(annotations):
                    needs_save_after_dedup = True
                # Then ensure we have a 0.0 annotation
                if self.ensure_zero_annotation(annotations):
                    needs_save_after_dedup = True

        if needs_save_after_dedup:
            self.save()

        # Update image time display
        image_time = self.get_image_time()
        time_text = "second" if image_time == 1 else "seconds"
        # Format: show integers without decimal, floats with decimal
        if image_time == int(image_time):
            time_str = str(int(image_time))
        else:
            time_str = str(image_time)
        self.image_time_input.setText(f"{time_str} {time_text}")
        # Clear loading message before showing item
        try:
            self.text_box.blockSignals(True)
            self.text_box.setText("")
        finally:
            self.text_box.blockSignals(False)

        # Handle duplicate filenames with different timestamps
        self.handle_duplicate_filenames()

        self.show_item()

    def handle_duplicate_filenames(self):
        """Find duplicate filenames and offer to rename them.
        Handles both same and different timestamps."""
        # Group files by exact filename - must be identical
        from collections import defaultdict
        files_by_name = defaultdict(list)

        for file_path in self.media:
            # Use exact filename - no suffix stripping
            # Files must have identical names to be considered duplicates
            files_by_name[file_path.name].append(file_path)

        # Find groups with duplicate filenames
        duplicates_to_handle = []
        for base_name, file_paths in files_by_name.items():
            # Skip if only one file with this name
            if len(file_paths) <= 1:
                continue

            # Check if files are in different locations (different folders OR different paths)
            unique_paths = set(str(p) for p in file_paths)
            if len(unique_paths) <= 1:
                # All paths are the same - not duplicates
                continue

            # Get timestamps for each file
            file_info = []
            for p in file_paths:
                data_key = p.name
                entry = self.data.get(data_key, {})

                # Try to get timestamp from data
                ts = parse_creation_value(entry.get("creation_time_manual"))
                if ts is None:
                    ts = parse_creation_value(entry.get("creation_date_time"))
                if ts is None:
                    # Try to extract directly from file if not in JSON yet
                    self.get_cached_creation_time(p)
                    entry = self.data.get(data_key, {})
                    ts = parse_creation_value(entry.get("creation_date_time"))
                if ts is None:
                    ts = 0

                file_info.append((p, ts))

            # Sort by timestamp
            file_info.sort(key=lambda x: x[1])

            # Check if all timestamps are the same
            timestamps = [ts for _, ts in file_info]
            all_same_timestamp = len(set(timestamps)) == 1 and timestamps[0] != 0

            # Add to list - handle both same and different timestamps
            duplicates_to_handle.append((file_info, all_same_timestamp))

        # Process each duplicate group
        for file_group, same_timestamp in duplicates_to_handle:
            if not self.show_duplicate_rename_dialog(file_group, same_timestamp):
                # User clicked "Skip this step"
                break

    def show_duplicate_rename_dialog(self, file_group, same_timestamp=False):
        """Show dialog for renaming duplicate files. Returns False if user clicked Skip."""
        from datetime import datetime

        # Build message with file list and proposed renames
        if same_timestamp:
            message_lines = ["These files have the same filename and the same timestamp.\n"]
            message_lines.append("They appear to be duplicates of the same file.\n")
            message_lines.append("OK to modify the filenames as shown?\n\n")
        else:
            message_lines = ["These files have the same filename but different timestamps.\n"]
            message_lines.append("OK to modify the filenames as shown?\n\n")

        for idx, (file_path, timestamp) in enumerate(file_group):
            if timestamp > 0:
                time_str = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
            else:
                time_str = "Unknown"

            # Get folder relative to root
            try:
                folder = str(file_path.parent.relative_to(self.dir))
            except ValueError:
                folder = str(file_path.parent)

            if idx == 0:
                proposed_name = file_path.name
            else:
                # Insert _pva_N before extension
                name_parts = file_path.name.rsplit(".", 1)
                if len(name_parts) == 2:
                    proposed_name = f"{name_parts[0]}_pva_{idx}.{name_parts[1]}"
                else:
                    proposed_name = f"{file_path.name}_pva_{idx}"

            message_lines.append(f"  {file_path.name} → {proposed_name}")
            message_lines.append(f"    Folder: {folder}, Time: {time_str}\n")

        message = "\n".join(message_lines)

        # Create custom dialog with three buttons
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Duplicate Filenames")
        dialog.setText(message)

        yes_btn = dialog.addButton("Yes", QMessageBox.AcceptRole)
        no_btn = dialog.addButton("No", QMessageBox.RejectRole)
        skip_btn = dialog.addButton("Skip this step", QMessageBox.DestructiveRole)

        dialog.setDefaultButton(yes_btn)
        result = dialog.exec()
        clicked_button = dialog.clickedButton()

        if clicked_button == yes_btn:
            # Rename files and update data
            self.rename_duplicate_files(file_group)
            return True
        elif clicked_button == no_btn:
            # Skip this group, continue to next
            return True
        else:  # skip_btn
            # Stop processing
            return False

    def rename_duplicate_files(self, file_group):
        """Rename duplicate files with _pva_N suffixes."""
        renamed_map = {}  # Old path -> new path

        # First pass: rename all files except the first
        for idx, (file_path, _) in enumerate(file_group):
            if idx == 0:
                continue  # Keep first file unchanged

            # Build new filename with _pva_N suffix
            name_parts = file_path.name.rsplit(".", 1)
            if len(name_parts) == 2:
                new_name = f"{name_parts[0]}_pva_{idx}.{name_parts[1]}"
            else:
                new_name = f"{file_path.name}_pva_{idx}"

            new_path = file_path.parent / new_name

            # Rename the file
            file_path.rename(new_path)
            renamed_map[file_path] = new_path

            # Update data dict: move entry from old key to new key
            old_key = file_path.name
            if old_key in self.data:
                self.data[new_name] = self.data.pop(old_key)
                self.mark_data_changed()

        # Update self.media list with new paths
        for i, old_path in enumerate(self.media):
            if old_path in renamed_map:
                self.media[i] = renamed_map[old_path]

        # Re-read metadata for renamed files to get separate entries
        for old_path, new_path in renamed_map.items():
            # Force re-read of file metadata
            data_key = new_path.name
            entry = self.data.get(data_key, {})

            # Re-extract creation time if available
            if new_path.suffix.lower() in SUPPORTED_IMAGES:
                gps = get_exif_gps(new_path)
                # The extraction will happen naturally when show_item is called
            elif new_path.suffix.lower() in SUPPORTED_VIDEOS:
                pass  # Video metadata extraction happens on demand

    # ---------------- Helpers ----------------
    def normalize_creation_times(self):
        """Convert any numeric/legacy manual creation times to the saved string format.
        Note: legacy creation_time is left untouched per requirements.
        """
        changed = False
        for name, entry in self.data.items():
            if name == "_settings" or not isinstance(entry, dict):
                continue
            # Only normalize manual override field; leave creation_time untouched
            key = "creation_time_manual"
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
        """Get or compute creation_time_utc and local_time_zone for a file.
        Stores creation_time_utc (epoch) and local_time_zone (if available) in JSON.
        Also stores creation_local_naive when the file only provides a wall-clock time with no timezone.
        Returns the UTC epoch for initial sorting.
        """
        filename = file_path.name
        entry = self.data.setdefault(filename, {})

        # If creation_time_utc is missing OR if no timezone data exists, re-extract
        needs_extraction = (
            "creation_time_utc" not in entry
            or ("local_time_zone" not in entry and "local_time_zone_inferred" not in entry)
        )

        if needs_extraction:
            creation_time_tuple = get_file_creation_time(file_path)

            # Handle tuple return (utc_epoch, display_string, has_timezone, tz_label)
            if isinstance(creation_time_tuple, tuple) and len(creation_time_tuple) == 4:
                utc_epoch, display_string, has_timezone, tz_label = creation_time_tuple
            else:
                utc_epoch = creation_time_tuple if creation_time_tuple else 0
                display_string = ""
                tz_label = None

            # Store UTC epoch (use far-future fallback when absent)
            if utc_epoch == 0 or utc_epoch == "":
                entry["creation_time_utc"] = datetime(2100, 1, 1, 0, 10, 0).timestamp()
            else:
                entry["creation_time_utc"] = utc_epoch

            # Store timezone if available from file metadata
            if tz_label:
                entry["local_time_zone"] = tz_label
                # Remove inferred timezone if we found an actual one
                if "local_time_zone_inferred" in entry:
                    del entry["local_time_zone_inferred"]
            else:
                # Capture the wall-clock time for later use with inferred/known tz
                if display_string and "creation_local_naive" not in entry:
                    entry["creation_local_naive"] = display_string

        return entry["creation_time_utc"]

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
        """Return media entries not marked as skipped (or all media if in show_skipped mode)."""
        if self.show_skipped_mode:
            return self.media
        return [self.media[i] for i in range(len(self.media)) if not self.data.get(self.get_data_key(i), {}).get("skip", False)]

    def get_data_key(self, index=None):
        """Get the data dictionary key for a file, accounting for versioning.

        Args:
            index: Integer index into self.media, or None (uses current self.index)
        """
        if index is None:
            index = self.index

        # Check if we have a versioning mapping
        if hasattr(self, 'media_to_data_key') and index in self.media_to_data_key:
            return self.media_to_data_key[index]

        # Fall back to using the filename
        if index < len(self.media):
            return self.media[index].name

        return ""

    def get_base_filename(self, filename):
        """Strip ##version suffix from filename to get base name."""
        if "##" in filename:
            return filename.split("##")[0]
        return filename

    def get_version_suffix(self, filename):
        """Extract ##version suffix from filename, returns empty string if none."""
        if "##" in filename:
            return "##" + filename.split("##")[1]
        return ""

    def get_next_version_suffix(self, filename):
        """Get the next version suffix for duplicating. Appends 1 or 2 to existing suffix."""
        base = self.get_base_filename(filename)
        current_suffix = self.get_version_suffix(filename)
        if current_suffix:
            # Append 1 and 2 to existing suffix
            return current_suffix + "1", current_suffix + "2"
        else:
            # Create new ##1 and ##2
            return "##1", "##2"

    def normalize_special_chars(self, text):
        """Convert special characters to ASCII equivalents for search matching."""
        import unicodedata
        if not text:
            return text
        # Normalize unicode and remove combining marks (accents)
        nfd = unicodedata.normalize('NFD', text)
        return ''.join(c for c in nfd if unicodedata.category(c) != 'Mn')

    def search_files(self, direction=0):
        """Search files by content in date/time, filename, location, or annotations.
        Handles show_skipped mode and empty search navigation.
        direction: 0 = text search (forward from current), 1 = right arrow, -1 = left arrow.
        """
        if not self.media:
            return

        search_text = self.search_box.text().strip().lower()

        # Special case: empty search in show_skipped mode navigates between skipped files
        if not search_text and self.show_skipped_mode:
            if direction == 0:
                return  # Don't navigate on text change when empty
            elif direction == 1:
                # Navigate forward to next skipped file
                start_idx = (self.index + 1) % len(self.media)
                step = 1
            else:  # direction == -1
                # Navigate backward to previous skipped file
                start_idx = (self.index - 1) % len(self.media)
                step = -1

            for i in range(len(self.media)):
                check_idx = (start_idx + i * step) % len(self.media)
                if self.data.get(self.get_data_key(check_idx), {}).get("skip", False):
                    self.index = check_idx
                    self.show_item()
                    self.search_box.setFocus()
                    return
            return  # No skipped files found

        # Regular search requires text
        if not search_text:
            return

        # For text changes (direction=0), first check if current file matches
        # If so, stay on current file without moving
        if direction == 0:
            if not self.show_skipped_mode and self.data.get(self.get_data_key(self.index), {}).get("skip", False):
                # Current file is skipped, search forward from next
                pass
            else:
                match_info = self._match_file(self.index, search_text)
                if match_info:
                    # Current file matches, stay here
                    self.search_box.setFocus()
                    return

        # Determine search direction and range
        if direction == 0:
            # Text changed: current didn't match, search forward from next position with wrap-around
            start_idx = (self.index + 1) % len(self.media)
            step = 1
            search_range = len(self.media) - 1  # Search all except current (already checked)
        elif direction == 1:
            # Right arrow: search from next file forward (with wrap-around)
            start_idx = (self.index + 1) % len(self.media)
            step = 1
            search_range = len(self.media)
        else:  # direction == -1
            # Left arrow: search from previous file backward (with wrap-around)
            start_idx = (self.index - 1) % len(self.media)
            step = -1
            search_range = len(self.media)

        # Search in specified range
        for i in range(search_range):
            check_idx = (start_idx + i * step) % len(self.media)

            # Skip files marked with skip=true ONLY when not in show_skipped mode
            if not self.show_skipped_mode and self.data.get(self.get_data_key(check_idx), {}).get("skip", False):
                continue
            match_info = self._match_file(check_idx, search_text)
            if match_info:
                self.index = check_idx
                self.show_item()
                # If match was in an annotation, position slider at that annotation's start time
                if match_info.get("annotation_time") is not None:
                    ann_time_ms = int(match_info["annotation_time"] * 1000)
                    self.video_player.setPosition(ann_time_ms)
                    self.video_slider.setValue(ann_time_ms)
                # Restore focus to search box after showing item
                self.search_box.setFocus()
                return

        # No match found - stay on current file

    def _match_file(self, file_idx, search_text):
        """Check if search_text matches any field in a file's metadata.
        Returns a dict with match info (including annotation_time if matched in annotation), or None.
        """
        file_path = self.media[file_idx]
        data_key = self.get_data_key(file_idx)
        entry = self.data.get(data_key, {})

        # Check date/time field
        if search_text in entry.get("creation_date_time", "").lower():
            return {"type": "datetime"}

        # Check filename
        if search_text in file_path.name.lower():
            return {"type": "filename"}

        # Check location (with special character normalization)
        location = entry.get("location", {})
        manual_loc = self.normalize_special_chars(location.get("manual_text", "")).lower()
        if search_text in manual_loc:
            return {"type": "location"}
        automated_loc = self.normalize_special_chars(location.get("automated_text", "")).lower()
        if search_text in automated_loc:
            return {"type": "location"}

        # Check image text annotation
        if file_path.suffix.lower() in SUPPORTED_IMAGES:
            if search_text in entry.get("text", "").lower():
                return {"type": "image_text"}

        # Check video annotations
        if file_path.suffix.lower() in SUPPORTED_VIDEOS:
            annotations = entry.get("annotations", [])
            for ann in annotations:
                if search_text in ann.get("text", "").lower():
                    return {"type": "annotation", "annotation_time": ann.get("time", 0.0)}

        return None


    def update_position_display(self):
        # Count non-skipped items up to and including current index
        if not self.show_skipped_mode:
            current_visible_index = 0
            total = 0
            for idx in range(len(self.media)):
                data_key = self.get_data_key(idx)
                is_skipped = self.data.get(data_key, {}).get("skip", False)
                if not is_skipped:
                    total += 1
                    if idx <= self.index:
                        current_visible_index = total
            text = f"{current_visible_index} of {total}" if total > 0 else "0 of 0"
        else:
            # In show skipped mode, show absolute position
            text = f"{self.index + 1} of {len(self.media)}"
        self.position_box.blockSignals(True)
        self.position_box.setText(text)
        self.position_box.blockSignals(False)

    def mark_data_changed(self):
        """Mark data as changed and save. Convenience method for data modifications."""
        self.data_changed = True
        self.save()

    def save(self):
        """Save data to JSON files only if data has changed."""
        # Only proceed if data has actually changed
        if not self.data_changed:
            return

        # Build a fast lookup set of video filenames for O(1) lookup
        video_names = {p.name for p in self.media if p.suffix.lower() in SUPPORTED_VIDEOS}

        # Clean up fields that should not be written to JSON
        for filename in self.data:
            if filename != "_settings":
                # Remove rotation for videos (rotation only applies to images)
                if filename in video_names:
                    self.data[filename].pop("rotation", None)
                # Remove legacy creation_time field (we use creation_time_utc, creation_date_time, etc.)
                self.data[filename].pop("creation_time", None)

        # Write the main annotations file
        self.json_path.write_text(json.dumps(self.data, indent=2))

        # Create a dated backup
        from datetime import datetime
        today = datetime.now().strftime("%Y_%m_%d")
        backup_filename = f"annotations_{today}.json"
        backup_path = self.pva_data_dir / backup_filename
        backup_path.write_text(json.dumps(self.data, indent=2))

        # Reset the dirty flag after successful save
        self.data_changed = False

    def check_and_prompt_folders(self):
        """Check all folders (recursively) and prompt user if not already set.
        Gracefully skips folders that no longer exist."""
        def scan_folders_recursive(base_path, prefix=""):
            """Recursively scan all subfolders and prompt for each."""
            try:
                for item in sorted(base_path.iterdir()):
                    if item.is_dir() and item.name != TRASH_DIR and item.name != PVA_DATA_DIR:
                        # Check if folder exists and is accessible
                        try:
                            item.stat()  # Check if path exists and is accessible
                        except (OSError, FileNotFoundError):
                            # Folder was moved/deleted - skip it
                            continue

                        # Create folder key: relative path from self.dir
                        try:
                            folder_key = str(item.relative_to(self.dir))
                        except ValueError:
                            folder_key = item.name

                        # Check if we already have a "use" setting for this folder
                        if folder_key not in self.data or "use" not in self.data[folder_key]:
                            # Prompt user with the full path
                            reply = QMessageBox.question(
                                self,
                                "Include Folder?",
                                f"Include files from '{folder_key}' folder?",
                                QMessageBox.Yes | QMessageBox.No
                            )
                            # Save the choice
                            if folder_key not in self.data:
                                self.data[folder_key] = {}
                            self.data[folder_key]["use"] = (reply == QMessageBox.Yes)

                        # Recursively scan subfolders
                        scan_folders_recursive(item, prefix)
            except (OSError, PermissionError):
                # Folder access error - skip and continue
                pass

        scan_folders_recursive(self.dir)
        self.save()

    def get_all_media_files(self):
        """Get all media files from root and included folders (recursively).
        Gracefully handles missing folders by skipping them."""
        files = []

        # Add files from root directory
        try:
            for p in self.dir.iterdir():
                if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGES|SUPPORTED_VIDEOS:
                    files.append(p)
        except (OSError, PermissionError):
            # Root directory access error - skip and continue
            pass

        # Add files from folders marked with use=true, including all subfolders
        def scan_folder_recursive(folder_path):
            """Recursively collect media files from a folder."""
            local_files = []
            try:
                for item in folder_path.iterdir():
                    if item.is_file() and item.suffix.lower() in SUPPORTED_IMAGES|SUPPORTED_VIDEOS:
                        local_files.append(item)
                    elif item.is_dir() and item.name != TRASH_DIR and item.name != PVA_DATA_DIR:
                        # Recursively scan subfolders
                        local_files.extend(scan_folder_recursive(item))
            except (OSError, PermissionError):
                # Folder access error - skip this folder and continue
                pass
            return local_files

        try:
            for item in self.dir.iterdir():
                if item.is_dir() and item.name != TRASH_DIR and item.name != PVA_DATA_DIR:
                    # Check if folder exists and is accessible
                    try:
                        item.stat()  # Check if path exists and is accessible
                    except (OSError, FileNotFoundError):
                        # Folder was moved/deleted - skip it
                        continue

                    # Check if this folder or any of its parent folders is marked to use
                    try:
                        folder_key = str(item.relative_to(self.dir))
                    except ValueError:
                        folder_key = item.name

                    if self.data.get(folder_key, {}).get("use", False):
                        files.extend(scan_folder_recursive(item))
        except (OSError, PermissionError):
            # Root directory access error - skip and continue
            pass

        return files

    # ---------------- Media Display ----------------
    def extract_and_store_location(self, file_path):
        """Extract GPS coordinates from media file and reverse geocode if available."""
        p = self.current()
        data_key = self.get_data_key()
        entry = self.data.setdefault(data_key, {})
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
        p=self.current()
        data_key = self.get_data_key()
        entry=self.data.setdefault(data_key,{"rotation":0,"text":""})

        # Extract location data if available
        self.extract_and_store_location(p)

        # Display the time value used for sorting
        filename = data_key
        entry = self.data.get(data_key, {})

        # Priority: creation_time_manual > creation_date_time
        ts = None
        if "creation_time_manual" in entry:
            manual_val = entry.get("creation_time_manual")
            if isinstance(manual_val, str):
                ts = manual_val
            else:
                epoch = parse_creation_value(manual_val)
                if epoch is not None:
                    ts = format_creation_timestamp(epoch)

        if ts is None and "creation_date_time" in entry:
            ts = entry.get("creation_date_time")

        if ts is None:
            ts = "No date/time"

        # Update datetime box (editable)
        self.datetime_box.blockSignals(True)
        self.datetime_box.setText(ts)
        self.datetime_box.blockSignals(False)

        # Update filename label (read-only) - include version suffix if present
        display_path = self.get_relative_path(p)
        version_suffix = self.get_version_suffix(data_key)
        if version_suffix:
            display_path = display_path + " " + version_suffix
        self.filename_label.blockSignals(True)
        self.filename_label.setText(display_path)
        self.filename_label.setCursorPosition(0)  # Keep cursor at start to show beginning of path
        self.filename_label.blockSignals(False)

        # Update position display (1-based, non-skipped)
        self.update_position_display()

        # Dropdown locations - sorted by distance to current file
        current_loc=entry.get("location",{}).get("manual_text","") or entry.get("location",{}).get("automated_text","")

        # Collect all unique locations and find files that have them
        location_files = {}  # location -> list of (index, file_path)
        for idx, file_path in enumerate(self.media):
            file_data_key = self.get_data_key(idx)
            file_entry = self.data.get(file_data_key, {})
            loc = file_entry.get("location", {}).get("manual_text", "") or file_entry.get("location", {}).get("automated_text", "")
            if loc:  # Only track non-empty locations
                if loc not in location_files:
                    location_files[loc] = []
                location_files[loc].append((idx, file_path))

        # Calculate distance for each location (minimum distance to current file)
        current_idx = self.index
        location_distances = {}  # location -> (min_distance, min_index_at_that_distance)
        for loc, files in location_files.items():
            min_distance = float('inf')
            min_index = float('inf')
            for idx, _ in files:
                distance = abs(idx - current_idx)
                if distance < min_distance or (distance == min_distance and idx < min_index):
                    min_distance = distance
                    min_index = idx
            location_distances[loc] = (min_distance, min_index)

        # Sort locations by distance (descending - most distant first), then by index (ascending)
        # This puts closest locations near the bottom, with current location at absolute bottom
        sorted_locations = sorted(location_distances.items(), key=lambda x: (-x[1][0], x[1][1]))

        # Populate dropdown with sorted locations, then current location at bottom
        self.location_combo.blockSignals(True)
        self.location_combo.clear()

        # Add all other locations (excluding current location to avoid duplicates)
        for loc, _ in sorted_locations:
            if loc != current_loc:
                self.location_combo.addItem(loc)

        # Always add current location at the bottom (or empty string if no location)
        self.location_combo.addItem(current_loc if current_loc else "")

        # Set current index to the last item (current file's location)
        self.location_combo.setCurrentIndex(self.location_combo.count() - 1)
        self.location_combo.blockSignals(False)

        # Text box
        if p.suffix.lower() in SUPPORTED_IMAGES:
            text = entry.get("text","")
            self.text_box.setText(text)
            # If slideshow is active, wrap text and prepare for scrolling
            if self.slideshow:
                self._prepare_text_for_slideshow(text)
        else:
            annotations = self.get_current_video_annotations()
            ann0 = next((a for a in annotations if a.get("time") == 0.0), None)
            text = ann0.get("text", "") if ann0 else ""
            self.text_box.setText(text)
            # If slideshow is active, wrap text and prepare for scrolling
            if self.slideshow:
                self._prepare_text_for_slideshow(text)

        self.setFocus()
        # Media display
        if p.suffix.lower() in SUPPORTED_IMAGES:
            self.video_widget.hide(); self.video_slider.hide()
            for b in [self.play_btn,self.replay_btn,self.add_ann_btn,self.edit_ann_btn,
                      self.remove_ann_btn,self.skip_ann_btn]: b.hide()
            self.rotate_btn.show()
            # Enable Rotate for images unless slideshow is running
            if not self.slideshow:
                self.rotate_btn.setEnabled(True)
                if sys.platform.startswith('linux') or sys.platform == 'darwin':
                    self.rotate_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
                else:
                    self.rotate_btn.setStyleSheet("font-weight: bold;")
            self.duplicate_btn.show()
            # Enable Duplicate for images unless slideshow is running
            if not self.slideshow:
                self.duplicate_btn.setEnabled(True)
                if sys.platform.startswith('linux') or sys.platform == 'darwin':
                    self.duplicate_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
                else:
                    self.duplicate_btn.setStyleSheet("font-weight: bold;")
            self.crop_btn.show()
            self.crop_btn.setEnabled(True)  # Enable crop button for images
            self.volume_btn.show()  # Show volume button for images
            self.volume_btn.setEnabled(False)  # But disable it (grayed out)
            self.volume_btn.setStyleSheet("color: gray;")  # Gray out the text
            self.image_label.show()
            rot=entry.get("rotation",0)
            qimg=load_image(p,rot)
            pix=QPixmap.fromImage(qimg)

            # Store original pixmap for crop selection
            self.image_label.original_pixmap = pix

            # Apply crop if it exists
            crop_coords = entry.get("crop")
            if crop_coords:
                x1, y1, x2, y2 = crop_coords
                cropped_pix = pix.copy(x1, y1, x2-x1, y2-y1)
                self.image_label.setPixmap(cropped_pix.scaled(800,600,Qt.KeepAspectRatio))
                self.crop_btn.setText("Uncrop")
                self.crop_btn.setStyleSheet("background-color: black; color: white; font-weight: bold;")
            else:
                self.image_label.setPixmap(pix.scaled(800,600,Qt.KeepAspectRatio))
                self.crop_btn.setText("Crop")
                if sys.platform.startswith('linux') or sys.platform == 'darwin':
                    self.crop_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
                else:
                    self.crop_btn.setStyleSheet("font-weight: bold;")

            # Update crop button click handler for uncrop if needed
            if crop_coords:
                self.crop_btn.clicked.disconnect()
                self.crop_btn.clicked.connect(self.clear_crop)
            else:
                self.crop_btn.clicked.disconnect()
                self.crop_btn.clicked.connect(lambda: self.handle_button_click(self.toggle_crop_mode))

            self.video_player.stop()
        else:
            self.image_label.hide()
            for b in [self.play_btn,self.replay_btn,self.add_ann_btn,self.edit_ann_btn,
                      self.remove_ann_btn,self.skip_ann_btn]: b.show()
            self.rotate_btn.show()  # Always show Rotate button
            self.rotate_btn.setEnabled(False)  # But disable it for videos (grayed out)
            self.rotate_btn.setStyleSheet("color: gray;")  # Gray out the text
            self.duplicate_btn.show()  # Show Duplicate for videos too
            # Enable Duplicate for videos unless slideshow is running
            if not self.slideshow:
                self.duplicate_btn.setEnabled(True)
                if sys.platform.startswith('linux') or sys.platform == 'darwin':
                    self.duplicate_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
                else:
                    self.duplicate_btn.setStyleSheet("font-weight: bold;")
            self.crop_btn.show()  # Keep visible but disabled
            self.crop_btn.setEnabled(False)  # Disable crop button for videos (grayed out)
            self.volume_btn.show()
            # Enable volume button and restore normal styling for videos
            self.volume_btn.setEnabled(True)
            if sys.platform.startswith('linux') or sys.platform == 'darwin':
                self.volume_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
            else:
                self.volume_btn.setStyleSheet("font-weight: bold;")
            # Apply stored volume
            volume = entry.get("volume", 100)
            self.audio_output.setVolume(volume / 100.0)
            self.volume_btn.setText(f"{volume}% volume")
            # Full reset: stop any current playback and clear source before showing widget
            self.video_player.stop()
            self.video_player.setSource(QUrl())
            # Process events to ensure old video is cleared before showing widget
            QApplication.processEvents()
            # Now show the video widget and slider with cleared state
            self.video_widget.show(); self.video_slider.show()
            # Set new source and play
            self.video_player.setSource(QUrl.fromLocalFile(str(p)))
            # Use a single-shot timer to allow the source to load before playing
            QTimer.singleShot(100, self.video_player.play)

        # Update Skip button text and styling based on whether current file is skipped
        if entry.get("skip", False):
            self.skip_btn.setText("Unskip")
            self.skip_btn.setStyleSheet("background-color: black; color: white; font-weight: bold;")
        else:
            self.skip_btn.setText("Skip")
            # Restore platform-specific default styling
            if sys.platform.startswith('linux') or sys.platform == 'darwin':
                self.skip_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
            else:
                self.skip_btn.setStyleSheet("font-weight: bold;")

        # Next/Prev labels stay constant now that position box exists
        self.prev_btn.setText("Previous")
        self.next_btn.setText("Next")
        self.save()

    def show_placeholder_image(self):
        """Display the app icon in the media area before any folder is opened."""
        icon_path = resource_path("app_icon.png")
        self.video_widget.hide(); self.video_slider.hide()
        # Keep the controls visible, just show the placeholder image
        self.image_label.show()
        if icon_path.exists():
            pix = QPixmap(str(icon_path))
            self.image_label.setPixmap(pix.scaled(800, 600, Qt.KeepAspectRatio))
        else:
            self.image_label.setText("Select a folder to begin")

    # ---------------- Video Annotation ----------------
    def deduplicate_annotations(self, annotations):
        """Remove duplicate annotations at the same timestamp, keeping the one with non-empty skip and text."""
        if not annotations:
            return False

        # Group annotations by time
        from collections import defaultdict
        time_groups = defaultdict(list)
        for ann in annotations:
            time_groups[ann.get("time", 0.0)].append(ann)

        # Check if we have any duplicates
        has_duplicates = any(len(group) > 1 for group in time_groups.values())
        if not has_duplicates:
            return False

        # For each time with duplicates, keep the best one
        kept_annotations = []
        for time_val, group in time_groups.items():
            if len(group) == 1:
                kept_annotations.append(group[0])
            else:
                # Sort by priority: non-empty text, then skip flag, then first one
                def priority(ann):
                    has_text = bool(ann.get("text", "").strip())
                    has_skip = bool(ann.get("skip", False))
                    return (has_text, has_skip)

                best_ann = max(group, key=priority)
                kept_annotations.append(best_ann)

        # Replace the list contents
        annotations.clear()
        annotations.extend(kept_annotations)
        annotations.sort(key=lambda a: a.get("time", 0.0))
        return True

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
        data_key = self.get_data_key()
        annotations = self.data.setdefault(data_key, {}).setdefault("annotations", [])
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
                    self.text_box.setText("Segment skipped")
                    self.text_box.blockSignals(False)
                return
            else:
                # Paused or manual seek: always show "Segment skipped"
                self.text_box.blockSignals(True)
                self.text_box.setText("Segment skipped")
                self.text_box.blockSignals(False)
                return

        # Normal annotation
        self.text_box.blockSignals(True)
        self.text_box.setText(ann.get("text", ""))
        self.text_box.blockSignals(False)

    def handle_video_end(self, status):
        """Handle video reaching the end - reset to first non-skipped segment or beginning."""
        from PySide6.QtMultimedia import QMediaPlayer

        # Only handle EndOfMedia status
        if status != QMediaPlayer.MediaStatus.EndOfMedia:
            return

        # If in slideshow mode, let the slideshow timer handle advancement
        if self.slideshow:
            return

        # Find first non-skipped annotation
        p = self.current()
        if p.suffix.lower() in SUPPORTED_VIDEOS:
            annotations = self.get_current_video_annotations()

            # Find the first non-skipped annotation
            reset_time = 0  # Default to beginning if all are skipped
            if annotations:
                annotations.sort(key=lambda a: a["time"])
                for ann in annotations:
                    if not ann.get("skip", False):
                        reset_time = ann["time"]
                        break

            # Reset to the appropriate position, pause, and show annotation
            reset_pos_ms = int(reset_time * 1000)
            self.video_player.setPosition(reset_pos_ms)
            self.video_player.pause()
            # Update annotation to show the appropriate timestamp annotation
            self.update_video_annotation(reset_pos_ms)

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
            self.mark_data_changed()
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
        cursor.movePosition(QTextCursor.End)
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
            self.mark_data_changed()
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
        self.mark_data_changed()

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
        """Finish editing (if active) and cancel crop mode before running a button action."""
        self.finish_edit_mode()
        self.cancel_crop_mode()  # Cancel crop mode if active
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
        """While typing, update text in the active annotation (but don't save yet).
        Text will be saved when focus leaves the text box."""
        # CRITICAL: Never save wrapped text during slideshow
        # Text box contains wrapped version; we only save original after slideshow ends
        if self.slideshow:
            return

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
                data_key = self.get_data_key()
                self.data.setdefault(data_key, {})["text"] = self.text_box.toPlainText()
            else:
                # If we're editing a specific annotation, keep using that; otherwise pick active
                if hasattr(self, "editing_annotation"):
                    target = self.editing_annotation
                else:
                    target = self._find_active_annotation()

                target["text"] = self.text_box.toPlainText()

            # Mark data as changed so it will be saved when appropriate
            self.data_changed = True
        finally:
            self._text_change_in_progress = False

    # ---------------- Text Box Focus ----------------
    def text_focus_out(self, event):
        """Commit any new or edited annotation when text box loses focus."""
        # Keep edit mode active when focus leaves the text box; only finish via buttons.
        if not self.is_editing_annotation_mode:
            self.commit_editing_annotation()
        self.save_pending_annotation()
        # Save if data was changed during typing
        if self.data_changed:
            self.save()
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
            self.mark_data_changed()
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


    def update_text(self):
        # CRITICAL: Never save wrapped text during slideshow
        # Text box contains wrapped version; we only save original after slideshow ends
        if self.slideshow:
            return

        p=self.current()
        data_key = self.get_data_key()
        if p.suffix.lower() in SUPPORTED_IMAGES:
            self.data.setdefault(data_key,{})["text"]=self.text_box.toPlainText()
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
        self.mark_data_changed()

    def update_location_text(self,text):
        p=self.current()
        data_key = self.get_data_key()
        self.data.setdefault(data_key,{}).setdefault("location",{})["manual_text"]=text
        self.mark_data_changed()

    def update_creation_time(self):
        """Parse and validate the user-edited creation time, immediately update display and resort."""
        p = self.current()
        data_key = self.get_data_key()
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

        entry = self.data.setdefault(data_key, {})
        entry["creation_time_manual"] = text
        self.save()

        self.datetime_box.blockSignals(True)
        self.datetime_box.setText(text)
        self.datetime_box.blockSignals(False)

        # Re-sort media with versioned entries
        def sort_key_indexed(idx):
            key = self.media_to_data_key.get(idx, self.media[idx].name)
            entry = self.data.get(key, {})
            if "creation_time_manual" in entry:
                ts = parse_creation_value(entry["creation_time_manual"])
                if ts is not None:
                    version_suffix = self.get_version_suffix(key)
                    return (ts, version_suffix)
            if "creation_date_time" in entry:
                ts = parse_creation_value(entry["creation_date_time"])
                if ts is not None:
                    version_suffix = self.get_version_suffix(key)
                    return (ts, version_suffix)
            version_suffix = self.get_version_suffix(key)
            return (9999999999, version_suffix)

        # Sort indices
        sorted_indices = sorted(range(len(self.media)), key=sort_key_indexed)

        # Rebuild media and mapping in sorted order
        old_media = self.media[:]
        old_mapping = self.media_to_data_key.copy()

        self.media = [old_media[i] for i in sorted_indices]

        # Create new mapping with sorted indices
        old_to_new = {old_idx: new_idx for new_idx, old_idx in enumerate(sorted_indices)}
        self.media_to_data_key = {old_to_new[old_idx]: old_mapping[old_idx] for old_idx in old_mapping}

        # Find where current file ended up in the new order
        for idx, key in self.media_to_data_key.items():
            if key == data_key:
                self.index = idx
                break

        self.update_position_display()
        self.show_item()

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
                        self.mark_data_changed()
                        # If slideshow is running, update the timer for current item
                        if self.slideshow:
                            self.restart_slideshow_timer()
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

    def restart_slideshow_timer(self):
        """Restart the slideshow timer for the current item with updated delay."""
        if not self.slideshow:
            return

        p = self.current()
        image_time = self.get_image_time()
        image_time_ms = int(image_time * 1000)

        # Stop current timers
        self.timer.stop()
        self.text_scroll_timer.stop()

        if p.suffix.lower() in SUPPORTED_IMAGES:
            text = self.text_box.toPlainText()
            text_lines = text.split('\n')
            explicit_lines = len(text_lines)  # Number of line breaks + 1
            char_count = len(text)

            # Calculate how many display lines are needed
            # User sees ~160 chars per line at current font size
            display_lines = max(1, (char_count + 159) // 160)  # Round up
            num_lines = max(explicit_lines, display_lines)  # Use the larger value

            if char_count < 150 and num_lines <= 1:
                # Less than 150 characters and no line breaks: use delay time only
                self.timer.start(image_time_ms)
            elif char_count <= 300 and num_lines <= 3:
                # Up to 300 characters and up to two line breaks: use max(delay, word_count_formula)
                if image_time > 1:
                    duration = max(image_time, len(text.split()) / 4) * 1000
                    self.timer.start(int(duration))
                else:
                    self.timer.start(image_time_ms)
            else:
                # Beyond that: use scrolling over duration of max(delay_time, char_count/25)
                # But if image_time <= 1 second, use image_time without scrolling
                if image_time <= 1:
                    self.timer.start(image_time_ms)
                else:
                    scroll_steps = max(num_lines - 3, 1)
                    # Total time is max of configured delay or char_count / 25 seconds
                    total_time = max(image_time, char_count / 25)
                    total_duration_ms = int(total_time * 1000)

                    # Distribute time: 1 second initial + scroll + 1 second final
                    initial_pause = 1000
                    final_pause = 1000
                    scroll_duration = total_duration_ms - initial_pause - final_pause

                    # Ensure at least some scroll duration
                    if scroll_duration < 500:
                        scroll_duration = 500
                        initial_pause = max(100, (total_duration_ms - scroll_duration) // 2)
                        final_pause = total_duration_ms - initial_pause - scroll_duration

                    self._text_scroll_complete = False  # Reset flag for this item
                    self.timer.start(total_duration_ms)
                    self.start_text_scroll(initial_pause, scroll_duration, scroll_steps)
        else:
            # For videos, get remaining duration from current position
            # But if image_time <= 1 second, use image_time to allow fast navigation
            if image_time <= 1:
                self.timer.start(image_time_ms)
            else:
                remaining_ms = self.get_remaining_video_duration_ms(p)
                if remaining_ms and remaining_ms > 0:
                    self.timer.start(remaining_ms)
                else:
                    self.timer.start(image_time_ms)

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
            # If slideshow is active, restart timer for new item
            if self.slideshow:
                self.restart_slideshow_timer()

    def next_item(self):
        self.index=(self.index+1)%len(self.media)
        # Skip over any files marked as skip=true ONLY when NOT in show_skipped_mode
        if not self.show_skipped_mode:
            start_index = self.index
            while self.data.get(self.get_data_key(self.index), {}).get("skip", False):
                self.index=(self.index+1)%len(self.media)
                # Prevent infinite loop if all files are skipped
                if self.index == start_index:
                    break
        self.show_item()
        # If slideshow is active, restart timer for new item
        if self.slideshow:
            self.restart_slideshow_timer()

    def prev_item(self):
        self.index=(self.index-1)%len(self.media)
        # Skip over any files marked as skip=true ONLY when NOT in show_skipped_mode
        if not self.show_skipped_mode:
            start_index = self.index
            while self.data.get(self.get_data_key(self.index), {}).get("skip", False):
                self.index=(self.index-1)%len(self.media)
                # Prevent infinite loop if all files are skipped
                if self.index == start_index:
                    break
        if self.slideshow: self.toggle_slideshow()
        self.show_item()

    def skip_item(self):
        p = self.current()
        data_key = self.get_data_key()
        entry = self.data.setdefault(data_key, {})
        current_skip = entry.get("skip", False)
        entry["skip"] = not current_skip  # Toggle skip state
        self.mark_data_changed()
        if not current_skip:  # If we just skipped it
            self.next_item()
        else:  # If we unskipped it, stay on the same item
            self.show_item()

    def rotate_item(self):
        p=self.current()
        data_key = self.get_data_key()
        # Only allow rotation for images
        if p.suffix.lower() not in SUPPORTED_IMAGES:
            return

        entry=self.data.setdefault(data_key,{})
        current_rotation=entry.get("rotation",0)
        # Cycle through 0, 270, 180, 90 (clockwise)
        new_rotation=(current_rotation-90)%360
        # Store rotation only if not 0 (default)
        if new_rotation==0:
            entry.pop("rotation",None)
        else:
            entry["rotation"]=new_rotation
        self.mark_data_changed()
        self.show_item()

    def duplicate_item(self):
        """Create two copies of the current file's metadata with ##1 and ##2 suffixes."""
        p = self.current()
        current_data_key = self.get_data_key()
        base_filename = self.get_base_filename(current_data_key)

        # Get the original entry (deep copy before we modify anything)
        import copy
        original_entry = copy.deepcopy(self.data.get(current_data_key, {}))

        # Generate version suffixes
        suffix1, suffix2 = self.get_next_version_suffix(current_data_key)
        new_key1 = base_filename + suffix1
        new_key2 = base_filename + suffix2

        # Always remove the current entry (whether versioned or not)
        self.data.pop(current_data_key, None)

        # Create both new versioned entries from the original
        self.data[new_key1] = copy.deepcopy(original_entry)
        self.data[new_key2] = copy.deepcopy(original_entry)

        self.mark_data_changed()

        # Now update self.media to include both versions
        # Insert the same Path object at the current position (for the second version)
        current_index = self.index
        self.media.insert(current_index + 1, p)  # Insert second copy after current

        # Update the mapping: shift all indices after current
        new_mapping = {}
        for idx, key in self.media_to_data_key.items():
            if idx < current_index:
                new_mapping[idx] = key
            elif idx == current_index:
                new_mapping[idx] = new_key1
                new_mapping[idx + 1] = new_key2
            else:
                new_mapping[idx + 1] = key  # Shift by one
        self.media_to_data_key = new_mapping

        # Stay on the first version
        self.index = current_index
        self.show_item()


    def toggle_crop_mode(self):
        """Toggle crop mode on/off for images."""
        p=self.current()
        # Only allow cropping for images
        if p.suffix.lower() not in SUPPORTED_IMAGES:
            return

        # Toggle crop mode
        self.crop_mode = not self.crop_mode
        self.image_label.crop_mode = self.crop_mode

        if self.crop_mode:
            self.crop_btn.setText("Cropping")
            self.crop_btn.setStyleSheet("background-color: orange; color: white; font-weight: bold;")
            self.image_label.setCursor(Qt.CrossCursor)
        else:
            entry = self.data.get(p.name, {})
            if entry.get("crop"):
                self.crop_btn.setText("Uncrop")
                self.crop_btn.setStyleSheet("background-color: black; color: white; font-weight: bold;")
            else:
                self.crop_btn.setText("Crop")
                if sys.platform.startswith('linux') or sys.platform == 'darwin':
                    self.crop_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
                else:
                    self.crop_btn.setStyleSheet("font-weight: bold;")
            self.image_label.setCursor(Qt.ArrowCursor)

    def cancel_crop_mode(self):
        """Cancel crop mode without saving."""
        if self.crop_mode:
            self.crop_mode = False
            self.image_label.crop_mode = False
            self.image_label.crop_start = None
            self.image_label.crop_rect = None
            self.image_label.update()
            # Restore button to normal state
            p = self.current()
            entry = self.data.get(p.name, {})
            if entry.get("crop"):
                self.crop_btn.setText("Uncrop")
                self.crop_btn.setStyleSheet("background-color: black; color: white; font-weight: bold;")
            else:
                self.crop_btn.setText("Crop")
                if sys.platform.startswith('linux') or sys.platform == 'darwin':
                    self.crop_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
                else:
                    self.crop_btn.setStyleSheet("font-weight: bold;")
            self.image_label.setCursor(Qt.ArrowCursor)

    def apply_crop(self, crop_coords):
        """Store crop coordinates and exit crop mode."""
        p = self.current()
        data_key = self.get_data_key()
        entry = self.data.setdefault(data_key, {})

        # Store crop as (x1, y1, x2, y2)
        entry["crop"] = crop_coords
        self.mark_data_changed()

        # Exit crop mode and refresh display
        self.crop_mode = False
        self.image_label.crop_mode = False
        self.image_label.setCursor(Qt.ArrowCursor)
        self.crop_btn.setText("Uncrop")
        self.crop_btn.setStyleSheet("background-color: black; color: white; font-weight: bold;")
        self.show_item()

    def clear_crop(self):
        """Remove crop and restore full image."""
        p = self.current()
        data_key = self.get_data_key()
        entry = self.data.get(data_key, {})
        if entry and "crop" in entry:
            del entry["crop"]
            self.mark_data_changed()
            self.crop_btn.setText("Crop")
            if sys.platform.startswith('linux') or sys.platform == 'darwin':
                self.crop_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
            else:
                self.crop_btn.setStyleSheet("font-weight: bold;")
            self.show_item()

    def change_volume(self):
        p=self.current()
        data_key = self.get_data_key()
        # Only allow volume control for videos
        if p.suffix.lower() not in SUPPORTED_VIDEOS:
            return

        entry=self.data.setdefault(data_key,{})
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
        self.mark_data_changed()

    def trash_item(self):
        p=self.current()
        # Stop video playback if it's a video file
        if p.suffix.lower() in SUPPORTED_VIDEOS:
            self.video_player.stop()
            self.video_player.setSource(QUrl())

        file_parent = p.parent
        trash_dir = file_parent / TRASH_DIR
        trash_dir.mkdir(exist_ok=True)
        shutil.move(str(p), trash_dir / p.name)

        # Remove this specific version from data and media
        data_key = self.get_data_key()
        self.data.pop(data_key, None)

        # Remove from media by index (not by Path, which would remove the first occurrence)
        if self.index < len(self.media):
            self.media.pop(self.index)
            # Also remove from mapping
            if hasattr(self, 'media_to_data_key'):
                self.media_to_data_key.pop(self.index, None)
                # Shift indices down for all entries after current
                new_mapping = {}
                for idx, key in self.media_to_data_key.items():
                    if idx > self.index:
                        new_mapping[idx - 1] = key
                    else:
                        new_mapping[idx] = key
                self.media_to_data_key = new_mapping

        self.index = min(self.index, len(self.media) - 1) if self.media else 0
        self.mark_data_changed()

        if self.media:
            # Skip over any files marked as skip=true ONLY when NOT in show_skipped_mode
            if not self.show_skipped_mode:
                start_index = self.index
                while self.data.get(self.get_data_key(self.index), {}).get("skip", False):
                    self.index=(self.index+1)%len(self.media)
                    # Prevent infinite loop if all files are skipped
                    if self.index == start_index:
                        break
            self.show_item()

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

    def get_remaining_video_duration_ms(self, video_path):
        """Get remaining video duration from current position to effective end.
        Returns milliseconds remaining from current playback position."""
        # Get current position
        current_pos_ms = self.video_player.position()

        # Get effective end position
        effective_end_ms = self.get_effective_video_duration_ms(video_path)

        if not effective_end_ms or effective_end_ms <= current_pos_ms:
            return 0

        return effective_end_ms - current_pos_ms

    def stop_slideshow_if_running(self):
        """Stop slideshow if it's currently running and reset button text."""
        if self.slideshow:
            self.slideshow = False
            self.slide_btn.setText("Slideshow")
            # Reset button styling
            if sys.platform.startswith('linux') or sys.platform == 'darwin':
                self.slide_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
            else:
                self.slide_btn.setStyleSheet("font-weight: bold;")
            self.timer.stop()
            self.text_scroll_timer.stop()
            # Restore original text (just in case it was modified during scrolling)
            if hasattr(self, '_original_annotation_text'):
                self.text_box.blockSignals(True)
                self.text_box.setText(self._original_annotation_text)
                self.text_box.blockSignals(False)
            # CRITICAL: Re-enable text box (was disabled during slideshow to prevent saving)
            self.text_box.setReadOnly(False)
            self.text_box.setFocus()  # Restore focus to ensure text box is fully interactive
            # Re-enable Skip and Discard buttons
            self.skip_btn.setEnabled(True)
            if sys.platform.startswith('linux') or sys.platform == 'darwin':
                self.skip_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
            else:
                self.skip_btn.setStyleSheet("font-weight: bold;")
            self.trash_btn.setEnabled(True)
            if sys.platform.startswith('linux') or sys.platform == 'darwin':
                self.trash_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
            else:
                self.trash_btn.setStyleSheet("font-weight: bold;")
            # Re-enable Rotate and Duplicate buttons if appropriate
            p = self.current()
            if p.suffix.lower() in SUPPORTED_IMAGES:
                self.rotate_btn.setEnabled(True)
                if sys.platform.startswith('linux') or sys.platform == 'darwin':
                    self.rotate_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
                else:
                    self.rotate_btn.setStyleSheet("font-weight: bold;")
            self.duplicate_btn.setEnabled(True)
            if sys.platform.startswith('linux') or sys.platform == 'darwin':
                self.duplicate_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
            else:
                self.duplicate_btn.setStyleSheet("font-weight: bold;")
            # Pause video if currently playing one
            if p.suffix.lower() in SUPPORTED_VIDEOS:
                self.video_player.pause()

    def toggle_slideshow(self):
        self.slideshow=not self.slideshow
        self.text_scroll_timer.stop()
        if self.slideshow:
            self.slide_btn.setText("Stop slideshow")
            self.slide_btn.setStyleSheet("background-color: black; color: white; font-weight: bold;")
            # Disable Skip and Discard buttons during slideshow
            self.skip_btn.setEnabled(False)
            self.skip_btn.setStyleSheet("color: gray;")
            self.trash_btn.setEnabled(False)
            self.trash_btn.setStyleSheet("color: gray;")
            # Disable Rotate and Duplicate buttons during slideshow
            self.rotate_btn.setEnabled(False)
            self.rotate_btn.setStyleSheet("color: gray;")
            self.duplicate_btn.setEnabled(False)
            self.duplicate_btn.setStyleSheet("color: gray;")
            # Disable Crop button during slideshow
            self.crop_btn.setEnabled(False)
            self.crop_btn.setStyleSheet("color: gray;")
            # CRITICAL: Disable text box during slideshow to prevent saving
            # No gray background - keep the normal appearance, just read-only
            self.text_box.setReadOnly(True)
            p=self.current()
            image_time = self.get_image_time()
            image_time_ms = int(image_time * 1000)
            if p.suffix.lower() in SUPPORTED_VIDEOS:
                # Start playing the video if not already playing
                if self.video_player.playbackState() != QMediaPlayer.PlayingState:
                    self.video_player.play()
                # For videos, get remaining duration from current position
                # But if image_time <= 1 second, use image_time to allow fast navigation
                if image_time <= 1:
                    self.timer.start(image_time_ms)
                else:
                    remaining_ms = self.get_remaining_video_duration_ms(p)
                    if remaining_ms and remaining_ms > 0:
                        self.timer.start(remaining_ms)
                    else:
                        self.timer.start(image_time_ms)
            else:
                # For images, calculate delay based on text character count and line breaks
                text = self.text_box.toPlainText()
                text_lines = text.split('\n')
                explicit_lines = len(text_lines)  # Number of line breaks + 1
                char_count = len(text)

                if char_count < 150 and explicit_lines <= 1:
                    # Less than 150 characters and no line breaks: use delay time only
                    self.timer.start(image_time_ms)
                elif char_count <= 300 and explicit_lines <= 3:
                    # Up to 300 characters and up to two line breaks: use max(delay, word_count_formula)
                    if image_time > 1:
                        duration=max(image_time,len(text.split())/4)*1000
                        self.timer.start(int(duration))
                    else:
                        self.timer.start(image_time_ms)
                else:
                    # Beyond that: use scrolling over duration of max(delay_time, char_count/25)
                    # But if image_time <= 1 second, use image_time without scrolling
                    if image_time <= 1:
                        self.timer.start(image_time_ms)
                    else:
                        display_lines = max(1, (char_count + 159) // 160)  # Round up using 160 chars per line
                        num_lines = max(explicit_lines, display_lines)  # Use the larger value

                        scroll_steps = max(num_lines - 3, 1)
                        # Total time is max of configured delay or char_count / 25 seconds
                        total_time = max(image_time, char_count / 25)
                        total_duration_ms = int(total_time * 1000)

                        # Distribute time: 1 second initial + scroll + 1 second final
                        initial_pause = 1000
                        final_pause = 1000
                        scroll_duration = total_duration_ms - initial_pause - final_pause

                        # Ensure at least some scroll duration
                        if scroll_duration < 500:
                            scroll_duration = 500
                            initial_pause = max(100, (total_duration_ms - scroll_duration) // 2)
                            final_pause = total_duration_ms - initial_pause - scroll_duration

                        self._text_scroll_complete = False  # Reset flag for this item
                        self.timer.start(total_duration_ms)
                        self.start_text_scroll(initial_pause, scroll_duration, scroll_steps)
        else:
            self.slide_btn.setText("Slideshow")
            # Reset button styling
            if sys.platform.startswith('linux') or sys.platform == 'darwin':
                self.slide_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
            else:
                self.slide_btn.setStyleSheet("font-weight: bold;")
            # Re-enable Skip and Discard buttons
            self.skip_btn.setEnabled(True)
            if sys.platform.startswith('linux') or sys.platform == 'darwin':
                self.skip_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
            else:
                self.skip_btn.setStyleSheet("font-weight: bold;")
            self.trash_btn.setEnabled(True)
            if sys.platform.startswith('linux') or sys.platform == 'darwin':
                self.trash_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
            else:
                self.trash_btn.setStyleSheet("font-weight: bold;")
            # CRITICAL: Re-enable text box (was disabled during slideshow to prevent saving)
            self.text_box.setReadOnly(False)
            self.text_box.setFocus()  # Restore focus to ensure text box is fully interactive
            # Re-enable Rotate and Duplicate buttons if appropriate
            self.timer.stop()
            p=self.current()
            if p.suffix.lower() in SUPPORTED_IMAGES:
                self.rotate_btn.setEnabled(True)
                if sys.platform.startswith('linux') or sys.platform == 'darwin':
                    self.rotate_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
                else:
                    self.rotate_btn.setStyleSheet("font-weight: bold;")
            self.duplicate_btn.setEnabled(True)
            if sys.platform.startswith('linux') or sys.platform == 'darwin':
                self.duplicate_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
            else:
                self.duplicate_btn.setStyleSheet("font-weight: bold;")
            # Re-enable Crop button
            self.crop_btn.setEnabled(True)
            if sys.platform.startswith('linux') or sys.platform == 'darwin':
                self.crop_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
            else:
                self.crop_btn.setStyleSheet("font-weight: bold;")
            # Pause video if currently playing one
            if p.suffix.lower() in SUPPORTED_VIDEOS:
                self.video_player.pause()

    def toggle_show_skipped(self):
        """Toggle show skipped mode on/off."""
        self.show_skipped_mode = not self.show_skipped_mode
        if self.show_skipped_mode:
            self.show_skipped_btn.setText("Done with Skipped")
            self.show_skipped_btn.setStyleSheet("background-color: black; color: white; font-weight: bold;")
            self.search_box.setPlaceholderText("Skipped")
            self.show_item()  # Refresh to update skip button styling
        else:
            self.show_skipped_btn.setText("Show Skipped")
            # Restore platform-specific default styling
            if sys.platform.startswith('linux') or sys.platform == 'darwin':
                self.show_skipped_btn.setStyleSheet("QPushButton { color: black; font-weight: bold; }")
            else:
                self.show_skipped_btn.setStyleSheet("font-weight: bold;")
            self.search_box.setPlaceholderText("Search")
            # If current file is skipped, advance to next unskipped file
            p = self.current()
            if self.data.get(p.name, {}).get("skip", False):
                start_index = self.index
                while True:
                    self.index = (self.index + 1) % len(self.media)
                    if not self.data.get(self.media[self.index].name, {}).get("skip", False):
                        break
                    # Prevent infinite loop if all files are skipped
                    if self.index == start_index:
                        break
            self.show_item()  # Refresh to update skip button styling

    def advance_slideshow(self):
        self.next_item()
        # Now set timer for the newly loaded item
        # Use restart_slideshow_timer() to apply consistent timing logic
        # (which will also handle text wrapping via _prepare_text_for_slideshow)
        self.restart_slideshow_timer()

    def _wrap_text_at_spaces(self, text, max_width=160):
        """Wrap text at the last space before max_width characters.
        Returns a list of wrapped lines that break at word boundaries.
        """
        wrapped_lines = []
        remaining = text

        while remaining:
            if len(remaining) <= max_width:
                wrapped_lines.append(remaining)
                break

            # Find the last space within max_width
            chunk = remaining[:max_width]
            last_space = chunk.rfind(' ')

            if last_space > 0:
                # Break at the last space
                wrapped_lines.append(remaining[:last_space])
                remaining = remaining[last_space + 1:]  # Skip the space
            else:
                # No space found, break at max_width anyway
                wrapped_lines.append(chunk)
                remaining = remaining[max_width:]

        return wrapped_lines

    def _prepare_text_for_slideshow(self, text):
        """Prepare text for scrolling during slideshow without modifying content.
        We keep the original text intact and use cursor positioning to scroll."""
        if not text:
            return

        # Save the original text for restoration when slideshow ends
        self._original_annotation_text = text

        # Analyze text to see if scrolling is needed
        text_lines = text.split('\n')
        explicit_lines = len(text_lines)
        char_count = len(text)

        # Calculate display lines (160 chars per line)
        display_lines = max(1, (char_count + 159) // 160)
        num_lines = max(explicit_lines, display_lines)

        # If text needs scrolling (more than 3 lines), set up for scrolling
        # But keep the text unmodified in the box
        if num_lines > 3:
            # Store scroll info: we'll use line-based scrolling
            self.text_scroll_line_index = 0
            self.text_scroll_total_lines = num_lines

    def start_text_scroll(self, initial_pause_ms, scroll_duration_ms, scroll_steps):
        """Start scrolling text during slideshow by moving cursor, not by modifying text.

        Args:
            initial_pause_ms: Time to pause before starting scroll
            scroll_duration_ms: Total time for scrolling
            scroll_steps: Number of scroll steps
        """
        if not self.slideshow:
            return

        # Use the original saved text (never modified)
        if not hasattr(self, '_original_annotation_text'):
            return

        text = self._original_annotation_text

        if not text:
            return

        # Split text by newlines to get explicit lines
        lines = text.split('\n')
        explicit_lines = len(lines)
        char_count = len(text)

        # Calculate display lines needed (160 chars per line at current font size)
        display_lines = max(1, (char_count + 159) // 160)

        # Total lines to scroll through
        num_lines = max(explicit_lines, display_lines)

        # Only scroll if we have more than 3 lines total
        if num_lines > 3:
            # Store scroll parameters for cursor-based scrolling
            self.text_scroll_line_index = 0
            self.text_scroll_total_lines = num_lines
            scroll_interval = max(900, scroll_duration_ms // scroll_steps) if scroll_steps > 0 else 900
            self.text_scroll_interval = scroll_interval
            self.text_scroll_steps = scroll_steps

            # Start with initial pause before scrolling begins
            QTimer.singleShot(initial_pause_ms, self._start_scrolling_after_delay)

    def scroll_annotation_text(self):
        """Scroll through text during slideshow by scrolling the viewport.
        The original text is never modified - we just scroll the view vertically."""
        if not self.slideshow or not hasattr(self, 'text_scroll_line_index'):
            self.text_scroll_timer.stop()
            return

        # Get the text (original, unmodified)
        text = self._original_annotation_text
        if not text:
            self.text_scroll_timer.stop()
            return

        # Calculate display metrics
        lines = text.split('\n')
        explicit_lines = len(lines)
        char_count = len(text)
        display_lines = max(1, (char_count + 159) // 160)
        num_lines = max(explicit_lines, display_lines)

        # Advance to next line if not at end
        if self.text_scroll_line_index < num_lines - 3:
            self.text_scroll_line_index += 1

            # Use vertical scrollbar to scroll the view
            scrollbar = self.text_box.verticalScrollBar()

            # Estimate scroll position based on which "line" we're viewing
            # Each "line" in scrollbar units should advance based on font height
            # Get font metrics to know line height
            fm = self.text_box.fontMetrics()
            line_height = fm.lineSpacing()

            # Scroll position = current line index * line height
            # But we want to show lines starting from this index
            scroll_amount = self.text_scroll_line_index * line_height
            scrollbar.setValue(scroll_amount)
        else:
            # Last line reached, stop scrolling (final pause is handled by main timer)
            self.text_scroll_timer.stop()
            self._text_scroll_complete = True

    def _text_scroll_complete_handler(self):
        """Called after text scroll completes and 0.5 second pause is done."""
        # This just marks completion; the main slideshow timer will handle advancement
        pass

    def _start_scrolling_after_delay(self):
        """Helper to start scrolling after the 1-second pause."""
        if self.slideshow and hasattr(self, 'text_scroll_interval'):
            self.text_scroll_timer.start(self.text_scroll_interval)
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
