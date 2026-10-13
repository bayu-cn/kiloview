#!/bin/sh

if [ -n "$LD_LIBRARY_PATH" ]; then
    export LD_LIBRARY_PATH
fi

launch="${GST_LAUNCH:-gst-launch-1.0}"
echo "gst-launch: $launch"
if [ -x "$launch" ]; then
    echo "gst-launch: ketemu"
else
    echo "PERINGATAN: $launch tidak ketemu"
fi

echo "Device node:"
ls -l /dev/blackmagic /dev/blackmagic* 2>&1 || true

exec python3 /app/app.py
