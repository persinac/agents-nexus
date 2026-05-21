import logging
from pathlib import Path

log = logging.getLogger(__name__)


def to_pdf(staging_dir: Path, uuid: str) -> Path | None:
    """Convert a notebook to PDF using rmrl.

    Output is written to staging_dir/<uuid>/notebook.pdf.
    Returns the path on success, or None if the notebook is empty or conversion failed.
    """
    nb_dir = staging_dir / uuid
    if not nb_dir.is_dir() or not any(nb_dir.glob("*.rm")):
        log.warning("Notebook %s has no .rm pages — skipping conversion", uuid)
        return None

    pdf_path = nb_dir / "notebook.pdf"

    try:
        import rmrl

        # Pass a str, not a Path: rmrl.get_source() treats any object with
        # `open`/`exists` as already-a-Source and skips the FSSource wrapping —
        # which incorrectly matches pathlib.Path and breaks downstream calls.
        stream = rmrl.render(str(staging_dir / uuid))
        pdf_path.write_bytes(stream.read())
        log.info("Converted %s → %s", uuid, pdf_path)
        return pdf_path
    except Exception as exc:
        log.error("rmrl conversion failed for %s: %s", uuid, exc)
        return None


def to_pngs(pdf_path: Path) -> list[Path]:
    """Rasterize a PDF to per-page PNGs at 150 DPI.

    PNGs are written to pdf_path.parent/pages/page_001.png ... page_NNN.png.
    Returns the list of PNG paths in page order.
    """
    from pdf2image import convert_from_path

    pages_dir = pdf_path.parent / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    try:
        images = convert_from_path(str(pdf_path), dpi=150)
    except Exception as exc:
        log.error("PDF rasterization failed for %s: %s", pdf_path, exc)
        return []

    paths: list[Path] = []
    for i, img in enumerate(images, start=1):
        png_path = pages_dir / f"page_{i:03d}.png"
        img.save(str(png_path), "PNG")
        log.debug("Rasterized page %d → %s", i, png_path)
        paths.append(png_path)

    return paths
