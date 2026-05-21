## ADDED Requirements

The conversion step produces two artifacts per notebook with distinct downstream consumers:
- **PDF + per-page PNGs** are inputs to the drawing-interpretation step (raster vision).
- **Raw `.rm` page files** (left in place in staging) are the input to the handwriting-ocr step (vector strokes). MyScript iink Cloud does not accept raster images; OCR reads strokes directly from `.rm` files.

### Requirement: Convert .rm notebook files to PDF
The system SHALL use `rmrl` to convert each downloaded `.rm` notebook into a single multi-page PDF. The PDF SHALL preserve page order and apply the notebook's template background if available.

#### Scenario: Successful conversion of a multi-page notebook
- **WHEN** a notebook directory containing one or more `.rm` page files and a `.content` manifest is passed to the conversion step
- **THEN** the system SHALL produce a single PDF with one page per `.rm` file in the correct order

#### Scenario: Notebook with no pages
- **WHEN** a notebook directory contains a `.content` manifest but no `.rm` page files
- **THEN** the system SHALL skip conversion and log a warning, producing no PDF output

#### Scenario: Unsupported .rm format version
- **WHEN** `rmrl` encounters a `.rm` file with a format version it cannot parse
- **THEN** the system SHALL log an error for that notebook, skip it, and continue processing other notebooks

### Requirement: Rasterize PDF pages to PNG for downstream processing
The system SHALL rasterize each page of the generated PDF to a PNG image at a minimum resolution of 150 DPI. One PNG file SHALL be produced per page, named `page_<N>.png` (zero-padded, e.g., `page_001.png`).

#### Scenario: Single-page notebook
- **WHEN** a notebook PDF has exactly one page
- **THEN** the system SHALL produce exactly one PNG file named `page_001.png`

#### Scenario: Multi-page notebook
- **WHEN** a notebook PDF has N pages
- **THEN** the system SHALL produce N PNG files named `page_001.png` through `page_<N>.png` in the correct order

### Requirement: Conversion outputs stored in per-notebook staging directory
The system SHALL store all conversion outputs (PDF and PNGs) in a per-notebook subdirectory of the local staging area, identified by the notebook's UUID.

#### Scenario: Staging directory layout
- **WHEN** conversion completes for a notebook with UUID `<uuid>`
- **THEN** the staging directory SHALL contain `<uuid>/notebook.pdf` and `<uuid>/pages/page_001.png` (and subsequent pages)
