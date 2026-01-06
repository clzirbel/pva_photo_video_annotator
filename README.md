![Latest Release](https://img.shields.io/github/v/release/clzirbel/pva_photo_video_annotator)
![Platforms](https://img.shields.io/badge/platform-Windows%20|%20macOS%20|%20Linux-blue)
![License](https://img.shields.io/github/license/clzirbel/pva_photo_video_annotator)

# PVA Photo and Video Annotator

The PVA Photo and Video Annotator is a desktop application for organizing and viewing images and videos in a folder, making it easy to click through media files, add detailed annotations to images and videos, skip unwanted segments of videos, and view the results as a slideshow.

## Video, features, executable downloads, screenshots

[See the GitHub pages for the deployment](https://clzirbel.github.io/pva_photo_video_annotator)

## Walkthrough video

[4-minute video walkthrough](https://bgsu.instructuremedia.com/embed/68ebf54f-985d-4450-8f6e-eb938aced467)


## Run as Python Project

PVA is written entirely in Python using Python packages.

### Python Requirements

- **Python 3.8 or higher** (tested with Python 3.11)
- All required packages are listed in `requirements.txt`

### Python Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/yourusername/pva_photo_video_annotator.git
   cd pva_photo_video_annotator
   ```

2. **Install dependencies:**

   **For Python 3.11+ (standard installation):**
   ```bash
   pip install -r requirements.txt
   ```

   **For Python 3.8 or 3.9:**
   ```bash
   python -m pip install -r requirements.txt
   ```

   **For multiple Python versions on the same system:**

   If you have multiple Python versions installed, you can specify which version to use:

   ```bash
   # For Python 3.8
   python3.8 -m pip install -r requirements.txt

   # For Python 3.9
   python3.9 -m pip install -r requirements.txt

   # For Python 3.10
   python3.10 -m pip install -r requirements.txt

   # For Python 3.11
   python3.11 -m pip install -r requirements.txt
   ```

   **Using a virtual environment (recommended):**
   ```bash
   # Create a virtual environment
   python -m venv venv

   # Activate it
   # On Windows:
   venv\Scripts\activate
   # On macOS/Linux:
   source venv/bin/activate

   # Install dependencies
   pip install -r requirements.txt
   ```

3. **Run the application:**
   ```bash
   python pva_photo_video_annotator.py
   ```

### Python Dependencies

The application requires the following Python packages:

- **PySide6** (≥6.4.0): Qt framework for the GUI
- **requests** (≥2.28.0): HTTP library for location reverse-geocoding
- **tinytag** (≥1.9.0): Audio/video metadata extraction (for video duration)
- **Pillow** (≥9.0.0): Image processing library

## Credits

- Craig L. Zirbel; design, testing, debugging, images
- ChatGPT; initial code version, icon
- Cladue; code revisions
- GitHub; executable versions
- ElevenLabs; text to speech for audio in demonstration video
- Canvas Studio; screen capture for demonstration video
