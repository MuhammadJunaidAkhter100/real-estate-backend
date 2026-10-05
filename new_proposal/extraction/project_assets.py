from __future__ import annotations

import logging
import os
from io import BytesIO

from django.core.files.base import ContentFile

from new_proposal.extraction.ai_facts import (
    documents_fingerprint,
    extract_proposal_facts,
)
from new_proposal.extraction.fact_check_extractor import FactCheckExtractor

logger = logging.getLogger(__name__)


def _file_url(field) -> str:
    """Return the public URL of a FileField, or '' if not set / unavailable."""
    try:
        if field and field.name:
            return field.url
    except Exception:  # noqa: BLE001
        pass
    return ""


def _file_path(field) -> str:
    """Return a usable local-or-URL path for a FileField for HTML rendering."""
    if not field or not field.name:
        return ""
    try:
        return field.url
    except Exception:  # noqa: BLE001
        return field.name


def _save_field(field, filename: str, src_path: str) -> None:
    """Read bytes from `src_path` and save them to the given FileField (no commit)."""
    if not src_path or not os.path.exists(src_path):
        return
    with open(src_path, "rb") as fh:
        field.save(filename, ContentFile(fh.read()), save=False)


def ensure_project_proposal_assets(project, force_refresh: bool = False) -> dict:
    """Return a dict with proposal-ready asset paths and AI facts for a project.

    Re-extracts only when the project's documents fingerprint changes, or
    always when ``force_refresh`` is True. The returned dict looks like::

        {
            "background": "<url or local path>",
            "logo":       "<url or local path>",
            "facts":      {...AI facts...},
        }
    """
    fingerprint = documents_fingerprint(project.id) or ""
    cached_fp = getattr(project, "proposal_assets_fingerprint", "") or ""

    if (
        not force_refresh
        and fingerprint
        and cached_fp == fingerprint
        and project.proposal_cover_image
        and project.proposal_logo_image
        and project.proposal_ai_facts
    ):
        logger.info(
            "Reusing cached proposal assets for project %s (fingerprint match).",
            project.id,
        )
        return {
            "background": _file_path(project.proposal_cover_image),
            "logo": _file_path(project.proposal_logo_image),
            "facts": project.proposal_ai_facts or {},
        }

    logger.info(
        "Refreshing proposal assets for project %s (fingerprint changed).",
        project.id,
    )

    # 1) Extract background + logo from the fact_checks PDF (to local disk).
    #    force=True bypasses the on-disk cache — a document was added/updated,
    #    so the assets must be re-extracted from the latest PDF.
    extracted = FactCheckExtractor(project_id=project.id).extract(force=True)
    background_path = extracted.get("background") or ""
    logo_path = extracted.get("logo") or ""

    # 2) Upload to S3 / configured storage via FileField.
    #    Filenames MUST be unique per project — otherwise every project
    #    overwrites the same S3 key (e.g. project_proposal_assets/background.png)
    #    and all proposals end up sharing one background/logo.
    update_fields: list[str] = []
    if background_path:
        _save_field(
            project.proposal_cover_image,
            f"project_{project.id}_background.png",
            background_path,
        )
        update_fields.append("proposal_cover_image")
    if logo_path:
        _save_field(
            project.proposal_logo_image,
            f"project_{project.id}_logo.png",
            logo_path,
        )
        update_fields.append("proposal_logo_image")

    # 3) Run / refresh AI fact extraction.
    try:
        facts = extract_proposal_facts(project.id, force_refresh=True) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI fact extraction failed for project %s: %s", project.id, exc)
        facts = {}

    project.proposal_ai_facts = facts
    update_fields.append("proposal_ai_facts")

    if fingerprint:
        project.proposal_assets_fingerprint = fingerprint
        update_fields.append("proposal_assets_fingerprint")

    update_fields.append("updated_at")
    project.save(update_fields=list(set(update_fields)))

    return {
        "background": _file_path(project.proposal_cover_image),
        "logo": _file_path(project.proposal_logo_image),
        "facts": facts,
    }
