# PVA Photo and Video Annotator

A simple yet powerful tool for viewing, organizing, and annotating photos and videos. The PVA Photo and Video Annotator lets you browse through media files in a folder, add detailed annotations to images and videos, skip unwanted segments, and organize your media collection with ease.

## Features

- **Browse Media**: View photos and videos from any folder with a clean, intuitive interface
- **Organize Media**: Quickly hide unwanted files or move them to a trash folder
- **Image Annotations**: Add text descriptions to photos
- **Video Annotations**: Add time-stamped text annotations throughout videos
- **Skip Video Segments**: Mark parts of videos to be automatically skipped during playback
- **Location Tagging**: Automatically extract GPS coordinates from photos and reverse-geocode them to city/state/country, or add locations manually
- **Volume Control**: Adjust video volume on a per-file basis
- **Image Rotation**: Rotate photos that have incorrect orientation
- **Persistent Storage**: All annotations and metadata are saved to a JSON file

## Getting Started

When you launch the application, select a folder containing your photos and videos. The program will read any existing annotations from an `annotations.json` file in that folder, or create one if it doesn't exist.

## Basic Navigation

### Next and Previous Buttons

Use the **Next** and **Previous** buttons to navigate through your media files one at a time.

- **Next**: Moves to the next media file in the folder. If you're at the last file, it wraps back to the first.
- **Previous**: Moves to the previous media file. If you're at the first file, it wraps back to the last.

When you navigate to a video, it will automatically start playing.

### Slideshow

Click the **Slideshow** button to automatically cycle through your media files:

- **Images**: Each image displays for the configured amount of time (default: 5 seconds), or longer if you've added text annotations (read time is factored in)
- **Videos**: Each video plays to completion before automatically advancing to the next file
- **Stop Slideshow**: Click the button again to stop the slideshow at any time

The slideshow is perfect for reviewing your entire photo/video collection quickly.

## Organizing Your Media

### Rotate Images

Click the **Rotate** button (only available for images) to rotate a photo clockwise:

- First click: 90° rotation
- Second click: 180° rotation
- Third click: 270° rotation
- Fourth click: Returns to 0° (original orientation, removes from file)

The rotation preference is saved with the image and will be remembered the next time you view it.

### Skip Files

Click the **Skip** button to hide a media file from view. The file is marked as skipped in the annotations and will be automatically passed over when navigating.

- The file is not deleted
- It's stored in the `annotations.json` with `"skip": true`
- Use this for files you want to ignore without permanently deleting them

### Trash Files

Click the **Trash** button to move a file to a `Trash` subfolder in your media directory.

- The file is physically moved (not just marked as skipped)
- You can recover files by moving them back out of the Trash folder manually
- Use this when you're sure you don't want a file

## Location Information

The application automatically extracts GPS coordinates from photo metadata using EXIF data. When coordinates are found, it performs a reverse geocoding lookup using OpenStreetMap's Nominatim service to determine the city, state, and country.

### Automatic Location Detection

When you view a photo with GPS data:

1. The app extracts latitude and longitude from the photo's EXIF metadata
2. It automatically looks up the address (with a 2-second timeout)
3. The location is stored as `automated_text` in the JSON file
4. The location appears in the location dropdown

### Manual Location Entry

You can also manually set or override the location:

1. Click on the location dropdown at the top
2. Either type a new location or select a previously used location from the list
3. The location is saved as `manual_text` in the JSON file
4. If both manual and automated locations exist, the manual location is displayed

The dropdown shows all unique locations (both manual and automated) across all files in your collection, making it easy to maintain consistency.

## Image Annotations

For images, you can add and edit a text description:

1. Click in the text box at the bottom of the screen
2. Type your annotation (e.g., "Family picnic in the park" or technical details about the photo)
3. Click elsewhere or press tab to save

The text is automatically saved to the JSON file under the `text` field for that image.

## Video Annotations

Videos support more sophisticated annotation: you can add multiple text annotations at different time points, and mark segments to be automatically skipped.

### Playing Videos

When you navigate to a video:

1. The video begins playing automatically
2. The volume is set to whatever you previously selected (or 100% if new)
3. Use **Play/Pause** to control playback
4. Use **Replay** to restart from the beginning
5. Click on the progress slider to jump to a specific time, or hover over it to see timestamps

### Adding Annotations

To add a text annotation at a specific time:

1. Pause the video at the desired time point
2. Click **Add annotation**
3. Type your text (e.g., "Golden retriever enters the scene")
4. Click elsewhere or press tab to save
5. The annotation is saved with the current video timestamp

You can add as many annotations as you want throughout the video, each with its own timestamp.

### Editing Annotations

To edit an existing annotation:

1. Pause the video at any time within an annotated segment (the annotation text will appear in the text box)
2. Click **Edit annotation**
3. Modify the text
4. Click elsewhere to save

The text is updated while the timestamp remains unchanged.

### Removing Annotations

To delete an annotation:

1. Pause the video at the time of the annotation you want to remove
2. Click **Remove annotation**
3. The annotation is deleted

### Skipping Video Segments

To mark a segment of the video to be automatically skipped during playback:

1. Pause the video at the point where you want skipping to begin
2. Click **Skip until next annotation**
3. The video marks this point and jumps to the next annotation (or end of video)
4. During playback, when this skip point is reached, the video automatically jumps to the next annotation

This is useful for removing unwanted sections (background noise, false starts, etc.) without deleting the original video file.

## Volume Control

For videos, use the **Volume** button (showing the current volume level) to adjust playback volume:

- Click to cycle through: 100% → 80% → 60% → 40% → 20% → 0% → back to 100%
- The volume preference is saved per video file
- When you return to a video, it plays at the previously saved volume level

Use this to mute videos with bad background noise or reduce volume for quieter content.

## Storage and Formats

All annotations, metadata, and preferences are stored in a JSON file (`annotations.json`) in your media folder. The JSON format makes it easy to:

- Back up your annotations
- Share annotations with others
- Edit annotations manually if needed
- Use the annotations with other tools

Each media file gets an entry in the JSON with:
- `text`: Text annotation (images) or time-stamped annotations (videos)
- `skip`: Whether the file should be skipped (images) or skip points (videos)
- `rotation`: Image rotation in degrees (0, 90, 180, or 270)
- `volume`: Video volume level (0-100)
- `location`: GPS coordinates and location information
- `annotations`: Array of time-stamped annotations (videos)

## Tips and Tricks

- **Keyboard Navigation**: Use the arrow keys (→ and ←) to navigate between files quickly
- **Batch Organization**: Use Next/Previous to go through all files, pressing Skip on unwanted ones
- **Slideshow for Review**: Use Slideshow mode to review your entire collection at once
- **Location Dropdowns**: The location dropdown shows all previously used locations, making it easy to tag files consistently
- **Timestamps**: Hover over the video progress bar to see the exact timestamp at any point
- **Volume Adjustments**: You can change volume while a video is playing; the change applies immediately
