"""
fact_check_extractor.py
───────────────────────
Service that pulls the background (cover) image and the logo from page 1 of a
project's ``fact_checks`` PDF (``projects.ProjectDocument``) and saves them to
disk inside this app's ``extraction`` folder.

- Background image : the largest image on page 1 (full-bleed hero photo).
- Logo             : the centred embedded image in the upper portion of
                     page 1, saved as a PNG with its soft-mask alpha
                     (transparency) preserved.

The source PDF is fetched over HTTP from its hosted (S3/CDN) URL instead of
going through the boto3/Django storage layer.

Pure extraction with PyMuPDF. No AI, no Celery.
"""

from __future__ import annotations

import logging
import math
import os

import httpx
import pymupdf

logger = logging.getLogger(__name__)

# Directory where extracted assets are stored: new_proposal/extraction/output/
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")


class FactCheckExtractionError(Exception):
    """Raised when the fact_checks document cannot be processed."""


class FactCheckExtractor:
    """Extract the background image and transparent logo from a fact_checks PDF."""

    # Background must occupy more than this share of the page area.
    COVER_MIN_AREA_RATIO = 0.25
    # Search for the logo only within the upper share of the page.
    TOP_REGION_RATIO = 0.60
    # Logo area bounds (share of page area).
    LOGO_MIN_AREA_RATIO = 0.005
    LOGO_MAX_AREA_RATIO = 0.25
    # Logo must be horizontally centred within this share of page width.
    LOGO_H_TOLERANCE_RATIO = 0.35
    # Vertical target for the logo (share of page height).
    LOGO_TARGET_Y_RATIO = 0.25  # Center of the upper half

    def __init__(self, project_id: int, output_dir: str | None = None) -> None:
        self.project_id = project_id
        self.output_dir = output_dir or _OUTPUT_DIR

    # ── Public API ──────────────────────────────────────────────────────────
    def extract(self, force: bool = False) -> dict:
        """Run the extraction (or return cached assets).

        To avoid re-downloading the PDF on every call, already-extracted assets
        for this project are reused when present on disk — unless ``force`` is
        True (e.g. the project's documents fingerprint changed), in which case
        the PDF is re-downloaded and assets are re-extracted.

        Returns a dict with absolute paths of the saved files::

            {"background": "<path or None>", "logo": "<path or None>", "cached": bool}
        """
        if not force:
            cached = self._cached_assets()
            if cached is not None:
                logger.info(
                    "Using cached fact_checks assets for project %s (no download).",
                    self.project_id,
                )
                return {**cached, "cached": True}

        pdf_bytes = self._load_fact_check_pdf_bytes()
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            if doc.page_count == 0:
                raise FactCheckExtractionError("fact_checks PDF has no pages.")

            page = doc[0]
            candidates = self._collect_page_images(page)
            if not candidates:
                logger.warning(
                    "No images found on page 1 of fact_checks for project %s.",
                    self.project_id,
                )
                return {"background": None, "logo": None}

            page_area = page.rect.width * page.rect.height

            background_path = self._save_background(doc, page, candidates, page_area)
            logo_path = self._save_logo(doc, page, candidates, page_area)

            return {"background": background_path, "logo": logo_path, "cached": False}
        finally:
            doc.close()

    # ── Cache ─────────────────────────────────────────────────────────────────
    def _cached_assets(self) -> dict | None:
        """Return already-extracted asset paths if present on disk, else None.

        The background extension can vary, so the background is matched by its
        ``background.*`` filename; the logo is always ``logo.png``.
        """
        import glob

        project_dir = os.path.join(self.output_dir, str(self.project_id))
        if not os.path.isdir(project_dir):
            return None

        background_matches = sorted(glob.glob(os.path.join(project_dir, "background.*")))
        background_path = background_matches[0] if background_matches else None

        logo_candidate = os.path.join(project_dir, "logo.png")
        logo_path = logo_candidate if os.path.exists(logo_candidate) else None

        if not background_path and not logo_path:
            return None

        return {"background": background_path, "logo": logo_path}

    # ── Source document ───────────────────────────────────────────────────────
    # Granular HTTP timeouts for the hosted PDF (seconds).
    HTTP_CONNECT_TIMEOUT = 15.0
    HTTP_READ_TIMEOUT = 60.0
    # How many times to retry on transient network/TLS errors.
    HTTP_MAX_RETRIES = 3

    def _load_fact_check_pdf_bytes(self) -> bytes:
        """Download the hosted fact_checks PDF over HTTP and return its bytes.

        Retries on transient network / TLS-handshake timeouts before failing.
        """
        pdf_url = self._fact_check_pdf_url()

        timeout = httpx.Timeout(
            connect=self.HTTP_CONNECT_TIMEOUT,
            read=self.HTTP_READ_TIMEOUT,
            write=self.HTTP_READ_TIMEOUT,
            pool=self.HTTP_CONNECT_TIMEOUT,
        )

        last_exc: Exception | None = None
        for attempt in range(1, self.HTTP_MAX_RETRIES + 1):
            try:
                response = httpx.get(pdf_url, timeout=timeout, follow_redirects=True)
                response.raise_for_status()
                return response.content
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                logger.warning(
                    "fact_checks download attempt %d/%d failed for project %s: %s",
                    attempt, self.HTTP_MAX_RETRIES, self.project_id, exc,
                )

        raise FactCheckExtractionError(
            f"Could not download fact_checks PDF for project "
            f"{self.project_id} from {pdf_url} after {self.HTTP_MAX_RETRIES} "
            f"attempts: {last_exc}"
        ) from last_exc

    def _fact_check_pdf_url(self) -> str:
        """Resolve the hosted (S3/CDN) URL of the project's fact_checks PDF."""
        # Imported here to keep this module import-safe outside Django contexts.
        from projects.models import ProjectDocument

        document = (
            ProjectDocument.objects
            .filter(project_id=self.project_id, label="fact_checks")
            .order_by("-created_at")
            .first()
        )
        if document is None:
            raise FactCheckExtractionError(
                f"No 'fact_checks' document found for project {self.project_id}."
            )

        try:
            return document.file.url
        except Exception as exc:  # noqa: BLE001
            raise FactCheckExtractionError(
                f"Could not resolve fact_checks URL for project "
                f"{self.project_id}: {exc}"
            ) from exc

    # ── Image collection ──────────────────────────────────────────────────────
    @staticmethod
    def _collect_page_images(page: pymupdf.Page) -> list[dict]:
        candidates: list[dict] = []
        seen_xrefs: set[int] = set()

        for img in page.get_images(full=True):
            xref = img[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)

            try:
                rects = page.get_image_rects(xref)
            except Exception:  # noqa: BLE001
                continue

            for rect in rects:
                if rect.is_empty or rect.is_infinite:
                    continue
                candidates.append({
                    "xref": xref,
                    "rect": rect,
                    "area": rect.width * rect.height,
                    "cx": rect.x0 + rect.width / 2,
                    "cy": rect.y0 + rect.height / 2,
                })
        return candidates

    # ── Background ────────────────────────────────────────────────────────────
    def _save_background(
        self,
        doc: pymupdf.Document,
        page: pymupdf.Page,
        candidates: list[dict],
        page_area: float,
    ) -> str | None:
        cover_candidates = [
            c for c in candidates if c["area"] > page_area * self.COVER_MIN_AREA_RATIO
        ]
        if not cover_candidates:
            logger.warning(
                "No background candidate on page 1 for project %s.", self.project_id
            )
            return None

        best = max(cover_candidates, key=lambda c: c["area"])

        # Extract the embedded image asset itself (the actual hero photo placed
        # on page 1), preserving its soft-mask (alpha) so transparent areas like
        # the sky stay transparent instead of turning solid black.
        try:
            png_bytes = self._extract_image_with_alpha(doc, best["xref"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not extract background image: %s", exc)
            return None

        if png_bytes is None:
            return None

        return self._write_file("background.png", png_bytes)

    @staticmethod
    def _extract_image_with_alpha(doc: pymupdf.Document, xref: int) -> bytes | None:
        """Return PNG bytes of an embedded image, keeping its soft-mask alpha."""
        pix = pymupdf.Pixmap(doc, xref)

        # If the PDF stores a separate soft-mask (alpha) image, composite it in
        # so the photo keeps its transparency.
        info = doc.extract_image(xref)
        smask_xref = info.get("smask", 0)
        if smask_xref:
            try:
                mask = pymupdf.Pixmap(doc, smask_xref)
                pix = pymupdf.Pixmap(pix, mask)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not apply soft-mask for xref=%s: %s", xref, exc)

        # CMYK has no direct PNG encoder — convert to RGB first.
        if pix.colorspace and pix.colorspace.n > 3:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)

        return pix.tobytes("png")

    # ── Logo (transparent) ────────────────────────────────────────────────────
    def _save_logo(
        self,
        doc: pymupdf.Document,
        page: pymupdf.Page,
        candidates: list[dict],
        page_area: float,
    ) -> str | None:
        page_w = page.rect.width
        page_h = page.rect.height
        target_x = page_w / 2
        target_y = page_h * self.LOGO_TARGET_Y_RATIO
        h_tolerance = page_w * self.LOGO_H_TOLERANCE_RATIO
        top_limit = page_h * self.TOP_REGION_RATIO

        logo_candidates = [
            c for c in candidates
            if (
                # Only consider images fully contained in the upper region.
                c["rect"].y1 <= top_limit
                and self.LOGO_MIN_AREA_RATIO * page_area
                <= c["area"]
                <= self.LOGO_MAX_AREA_RATIO * page_area
                and abs(c["cx"] - target_x) <= h_tolerance
            )
        ]
        if not logo_candidates:
            logger.warning(
                "No logo candidate on page 1 for project %s.", self.project_id
            )
            return None

        # Choose the candidate closest to the expected logo position.
        for c in logo_candidates:
            c["distance"] = math.hypot(c["cx"] - target_x, c["cy"] - target_y)
        best = min(logo_candidates, key=lambda c: c["distance"])

        # Extract the embedded logo image directly, preserving its soft-mask
        # (alpha) so transparency is kept intact.
        try:
            png_bytes = self._extract_image_with_alpha(doc, best["xref"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not extract logo image: %s", exc)
            return None

        if png_bytes is None:
            return None

        return self._write_file("logo.png", png_bytes)

    # ── Disk helper ────────────────────────────────────────────────────────────
    def _write_file(self, filename: str, data: bytes) -> str:
        project_dir = os.path.join(self.output_dir, str(self.project_id))
        os.makedirs(project_dir, exist_ok=True)
        path = os.path.join(project_dir, filename)
        with open(path, "wb") as fh:
            fh.write(data)
        logger.info("Saved %s (%d bytes) for project %s", path, len(data), self.project_id)
        return path
