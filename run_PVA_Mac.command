#!/bin/bash

# This script makes the PVA Photo and Video Annotator executable on Mac
# Double-click this file to run it, then you can launch the executable

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
EXECUTABLE="$SCRIPT_DIR/PVA_Photo_and_Video_Annotator_Mac"

if [ -f "$EXECUTABLE" ]; then
    chmod +x "$EXECUTABLE"
    echo "✓ Made the executable ready to run!"
    echo "You can now double-click 'PVA_Photo_and_Video_Annotator_Mac' to launch it."
    # Optionally launch the app immediately
    "$EXECUTABLE" &
else
    echo "✗ Could not find 'PVA_Photo_and_Video_Annotator_Mac' in the same folder as this script."
    echo "Make sure both files are in the same folder."
fi
