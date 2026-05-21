## Why

Handwritten notes on a reMarkable 1 tablet are trapped in a proprietary format on a device that rarely gets reviewed — they need to flow automatically into Obsidian (the long-term knowledge vault) with handwriting transcribed and drawings interpreted, so nothing is lost between capture and recall. The reMarkable 1's open Linux environment makes a fully self-hosted, automated pipeline viable without relying on reMarkable's official cloud.

## What Changes

- New pipeline that watches for new/changed `.rm` notebooks on the reMarkable 1 via SSH/rclone
- Conversion of raw `.rm` files into an intermediate format (PDF/SVG) using community tooling
- OCR step to transcribe handwritten text using MyScript API (or rmfakecloud with custom MyScript key for self-hosted operation)
- AI vision step to interpret drawings and produce textual descriptions
- Upload of raw assets (original PDFs/PNGs) to S3-compatible object storage
- Generation of structured Markdown notes (with transcribed text + drawing descriptions embedded) and sync into one or more Obsidian vaults

## Capabilities

### New Capabilities
- `rm-sync`: Detect and pull new/changed notebooks from the reMarkable 1 over SSH or rclone; store raw `.rm` files locally for processing
- `notebook-conversion`: Convert `.rm` proprietary format files into PDF and/or PNG/SVG using community tooling (e.g., `rmrl`, `rM2svg`)
- `handwriting-ocr`: Send converted notebook pages through handwriting recognition (MyScript API) and return plain text transcriptions
- `drawing-interpretation`: Send notebook page images through an AI vision model to produce natural-language descriptions of diagrams and sketches
- `asset-storage`: Upload raw and converted assets (PDFs, PNGs) to an S3-compatible bucket with structured key paths
- `obsidian-sync`: Generate structured Markdown notes embedding OCR text and drawing descriptions, then write them into the configured Obsidian vault directory

### Modified Capabilities
- None — this is a greenfield project

## Impact

- **New dependencies**: `rclone` (sync from device), `rmrl` or `rM2svg` (`.rm` conversion), MyScript API or `rmfakecloud` (OCR), an AI vision provider API (drawing interpretation), AWS S3 or compatible (asset storage)
- **External systems**: reMarkable 1 tablet (SSH access required), S3 bucket, Obsidian vault directory (local or synced via iCloud/Syncthing)
- **Configuration surface**: SSH credentials for the tablet, MyScript API key, S3 credentials + bucket name, Obsidian vault path(s)
- **No existing code affected** — new project
