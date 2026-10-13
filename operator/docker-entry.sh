#!/bin/sh

found=$(find /host-usr-lib /usr/lib/blackmagic /opt/blackmagic -name 'libDeckLinkAPI.so' 2>/dev/null | head -n 1)
if [ -n "$found" ]; then
    ln -sfn "$found" /usr/lib/libDeckLinkAPI.so
    echo "DeckLink API: $found"
else
    echo "PERINGATAN: libDeckLinkAPI.so tidak ketemu. Keluaran SDI tidak bisa membuka kartu."
fi

echo "Device node:"
ls -l /dev/blackmagic /dev/blackmagic* 2>&1 || true

if gst-inspect-1.0 decklinkvideosink >/dev/null 2>&1; then
    echo "decklinkvideosink: ada"
else
    echo "decklinkvideosink: TIDAK ADA"
fi

exec python3 /app/app.py
