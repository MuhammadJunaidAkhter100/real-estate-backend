from __future__ import annotations
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from django.template import Context, Engine
from django.utils import timezone

from new_proposal.extraction.ai_facts import extract_proposal_facts

logger = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_TEMPLATE_PATH = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "template", "proposal.html")
)
_OUTPUT_DIR = os.path.join(_THIS_DIR, "output")


class ProposalPdfError(Exception):
    """Raised when the proposal PDF cannot be generated."""


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _money(value, currency: str = "") -> str:
    """Format a numeric value as a currency string."""
    if value is None or value == "":
        return "N/A"
    try:
        amount = f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)
    symbol = {"GBP": "£", "USD": "$", "EUR": "€", "AED": "AED "}.get(
        (currency or "").upper(), ""
    )
    if symbol:
        return f"{symbol}{amount}"
    return f"{amount} {currency}".strip() if currency else amount


def _parse_percentage(value) -> float | None:
    """Parse '20%', '70% LTV', '20', etc. into a float. Returns None if unparseable."""
    import re

    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "TBC":
        return None
    match = re.search(r"-?\d+(?:[.,]\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def _parse_money(value: str) -> float:
    """Parse money string ('£12,345' or '£12345') to float (12345.0)."""
    if not value:
        return 0.0
    # Remove currency symbols and commas
    value = value.strip().lstrip("£€$¥₹")
    value = value.replace(",", "")
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _short_location(ai_facts: dict, raw_location: str) -> str:
    """Return a short "City, Country" label for the cover.

    Prefers the LLM-extracted ``location_label`` from ai_facts. Falls back to a
    heuristic (town segment of the address, postcode dropped) so the cover UI
    never breaks on long addresses.
    """
    label = str((ai_facts or {}).get("location_label") or "").strip()
    if label and label.upper() != "TBC":
        return label

    location = (raw_location or "").strip()
    if not location:
        return ""
    # Short already (e.g. "Preston, UK") — use as-is.
    if location.count(",") <= 1 and len(location) <= 32:
        return location
    # Heuristic: town part (2nd-last comma segment), dropping trailing postcode.
    parts = [p.strip() for p in location.split(",") if p.strip()]
    return parts[-2] if len(parts) >= 2 else location


def _file_uri(path: str | None) -> str:
    """Convert a local file path to a file:// URI usable by HTML engines.

    Already-absolute URLs (http/https/file/data/blob) are returned unchanged.
    """
    if not path:
        return ""
    lowered = path.lower()
    if lowered.startswith(("http://", "https://", "file://", "data:", "blob:")):
        return path
    try:
        return Path(path).resolve().as_uri()
    except Exception:  # noqa: BLE001
        return path


# ─────────────────────────────────────────────────────────────────────────────
# Context building
# ─────────────────────────────────────────────────────────────────────────────

def _user_display_name(user) -> str:
    """Return a friendly display name for a user object."""
    if user is None:
        return ""
    full = ""
    full_attr = getattr(user, "full_name", "")
    if callable(full_attr):
        try:
            full = full_attr() or ""
        except Exception:  # noqa: BLE001
            full = ""
    else:
        full = full_attr or ""
    if not full:
        first = getattr(user, "first_name", "") or ""
        last = getattr(user, "last_name", "") or ""
        full = f"{first} {last}".strip()
    if not full:
        full = getattr(user, "name", "") or getattr(user, "email", "") or ""
    return full.strip().title()


def _download_image_to_local(url: str, dest_dir: str, filename: str) -> str:
    """Download a remote image URL to dest_dir/filename and return a file:// URI.

    Returns an empty string if the download fails or the URL is blank.
    Already-local file:// URIs are returned unchanged.
    """
    if not url:
        return ""
    if url.startswith("file://"):
        return url
    try:
        import urllib.request
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, filename)
        urllib.request.urlretrieve(url, dest_path)
        logger.info("Downloaded gallery image %s -> %s", url, dest_path)
        return Path(dest_path).resolve().as_uri()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to download gallery image %s: %s", url, exc)
        return ""


def _unit_image_prefix(unit) -> str:
    """Return the proposal_images key prefix for a unit (e.g. '1_bed', 'studio')."""
    if not unit:
        return ""
    category = (getattr(unit, "category", "") or "").strip().lower()
    if not category:
        return ""

    _MAP = {
        "1 bed": "1_bed",
        "2 bed": "2_bed",
        "3 bed": "3_bed",
        "studio": "studio",
    }
    return _MAP.get(category, category.replace(" ", "_"))


def _unit_category_key(unit) -> str:
    """Sample-layout key for a unit (e.g. '1_bed_sample_layout')."""
    prefix = _unit_image_prefix(unit)
    return f"{prefix}_sample_layout" if prefix else ""


def _to_full_url(raw: str) -> str:
    """Convert a relative storage path to a full URL if needed."""
    if not raw:
        return ""
    if raw.lower().startswith(("http://", "https://", "file://", "data:")):
        return raw
    try:
        from django.core.files.storage import default_storage
        return default_storage.url(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not build URL for proposal image '%s': %s", raw, exc)
        return ""


def _proposal_image_list(project, label: str) -> list[str]:
    """Return all URLs for project.proposal_images[label] (with key fallbacks)."""
    if not label:
        return []
    proposal_images = getattr(project, "proposal_images", None) or {}
    images = proposal_images.get(label)
    # Fallback: try non-prefixed key (legacy data) for kitchen/bedroom labels.
    if not images:
        for suffix in ("_kitchen_dining", "_master_bedroom"):
            if label.endswith(suffix):
                images = proposal_images.get(suffix.lstrip("_"))
                if images:
                    break
    if not (isinstance(images, list) and images):
        logger.warning(
            "Gallery image NOT found for label '%s'. Available labels: %s",
            label, list(proposal_images.keys()),
        )
        return []
    urls = [_to_full_url(img) for img in images if img]
    return [u for u in urls if u]


def _pick_proposal_image(project, label: str) -> str:
    """Return the first full URL from project.proposal_images[label], or empty string."""
    urls = _proposal_image_list(project, label)
    if not urls:
        return ""
    raw = urls[0]
    logger.info("Gallery image found for label '%s': %s", label, raw)
    return raw


def build_context(project, unit, lead, assets: dict, current_user=None) -> dict:
    """Build the template context from model instances and extracted assets."""
    currency = (
        getattr(project, "currency", "")
        or getattr(unit, "currency", "")
        or ""
    )

    background = _file_uri(assets.get("background"))
    logo = _file_uri(assets.get("logo"))

    # Refresh proposal_images from DB — the in-memory project object may be stale.
    project_id = getattr(project, "id", 0) or 0
    if project_id:
        try:
            from projects.models import Project as _Project
            fresh = _Project.objects.only("proposal_images").get(pk=project_id)
            project.proposal_images = fresh.proposal_images
            logger.info(
                "Project %s proposal_images keys: %s",
                project_id, list((fresh.proposal_images or {}).keys()),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not refresh proposal_images for project %s: %s", project_id, exc)

    # Local dir for downloading gallery images so Chromium can load them.
    _gallery_dir = os.path.join(_OUTPUT_DIR, str(project_id), "gallery")

    list_price = getattr(unit, "list_price", None) if unit else None
    discounted_price = getattr(unit, "discounted_price", None) if unit else None
    discount_pct = 0
    discounted_price_val = 0
    savings_val = 0
    if list_price is not None and discounted_price is not None:
        try:
            lp = float(list_price)
            dp = float(discounted_price)
            if lp > 0 and dp > 0:
                discount_pct = round(((lp - dp) / lp) * 100, 2)
                discounted_price_val = dp
                savings_val = lp - dp
        except (TypeError, ValueError):
            pass

    agent_name = _user_display_name(current_user)

    ai_facts: dict = assets.get("facts") if isinstance(assets, dict) else {}
    if not ai_facts and project_id:
        try:
            ai_facts = extract_proposal_facts(project_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI fact extraction failed for project %s: %s", project_id, exc)
            ai_facts = {}

    def _fact(key: str, fallback: str = "TBC") -> str:
        value = str(ai_facts.get(key) or "").strip()
        return value if value and value.upper() != "TBC" else fallback

    investment_cases = ai_facts.get("investment_cases") or []
    if not isinstance(investment_cases, list):
        investment_cases = []

    db_total_units = (
        str(project.number_of_units)
        if getattr(project, "number_of_units", None) is not None
        else ""
    )
    db_rental_yield = (
        f"{project.yield_percentage}%"
        if getattr(project, "yield_percentage", None) is not None
        else ""
    )
    
    short_location = _short_location(ai_facts, getattr(project, "location", "") or "")

    context = {
        # Cover
        "background_image": background,
        "logo_image": logo,
        "project_name": (project.title).title() or "",
        "location": short_location.upper() if short_location else "n/a",
        "country_name": (getattr(project, "associated_country", "") or "").title(),
        "city_name": short_location.split(",")[0].strip().title() if short_location else "n/a",
        "client_name": (lead.name if lead else "").title() or "CLIENT NAME",
        "agent_name": agent_name or "AGENT NAME",
        "date": timezone.now().strftime('%d %B %Y'),
        "target_budget": (
            _money(lead.estimated_budget, currency)
            if lead and lead.estimated_budget is not None
            else "N/A"
        ),
        "price_from": _money(getattr(project, "starting_price", None), currency),

        # Fast facts (AI-extracted from project PDFs, with DB / sensible fallbacks)
        "fact_address": _fact("fact_address", project.location or "n/a"),
        "fact_completion": _fact("fact_completion","n/a"),
        "fact_lease_length": _fact("fact_lease_length", "n/a"),
        "fact_building_height": _fact("fact_building_height", "n/a"),
        "fact_total_units": _fact("fact_total_units", db_total_units or "n/a"),
        "fact_rental_yield": _fact("fact_rental_yield", db_rental_yield or "n/a"),
        "has_second_installment": bool(ai_facts.get("second_installments_percentage")),

        # Unit block
        "has_unit": bool(unit),
        "unit_label": (unit.label or "") if unit else "",
        "unit_category": (unit.category or "") if unit else "",
        "unit_floor": (unit.floor or "") if unit else "",
        "unit_list_price": _money(list_price, currency),
        "unit_discount_percentage": discount_pct,
        "unit_discounted_price": _money(discounted_price_val, currency),
        "unit_savings": _money(savings_val, currency),
        "unit_area_ft2": (
            f"{float(unit.area_ft2):.0f}"
            if unit and unit.area_ft2 is not None
            else ""
        ),
        "unit_area_m2": (
            f"{float(unit.area_ft2) / 10.7639:.0f}"
            if unit and unit.area_ft2 is not None
            else ""
        ),
        "unit_monthly_rent_market": (unit.est_market_rent or 0) if unit else 0,
        "unit_rent_assurance_pct": "0",
        "unit_monthly_rent_guaranteed": "0",
        "unit_gross_yield_market": (unit.est_yield_gross or 0) if unit else 0,

        # Interior specification (AI-extracted from project PDFs)
        "spec_flooring": _fact("spec_flooring", ""),
        "spec_kitchen": _fact("spec_kitchen", ""),
        "spec_bathroom": _fact("spec_bathroom", ""),
        "spec_lifts": _fact("spec_lifts", ""),
        "spec_cycling": _fact("spec_cycling", ""),

        # Gallery — download S3 URLs to local disk so Chromium headless can load them.
        # Image keys are unit-prefixed (e.g. '1_bed_kitchen_dining'); 'exterior' is shared.
        "gallery_image_1": _download_image_to_local(
            _pick_proposal_image(project, "exterior"), _gallery_dir, "gallery_1.jpg"
        ),
        "gallery_image_2": _download_image_to_local(
            _pick_proposal_image(project, f"{_unit_image_prefix(unit)}_kitchen_dining"),
            _gallery_dir, "gallery_2.jpg",
        ),
        "gallery_image_3": _download_image_to_local(
            _pick_proposal_image(project, f"{_unit_image_prefix(unit)}_master_bedroom"),
            _gallery_dir, "gallery_3.jpg",
        ),
        "gallery_image_4": _download_image_to_local(
            _pick_proposal_image(project, _unit_category_key(unit)), _gallery_dir, "gallery_4.jpg"
        ),
        "sample_layout_images": [
            url for i, raw in enumerate(_proposal_image_list(project, _unit_category_key(unit))[:2])
            if (url := _download_image_to_local(raw, _gallery_dir, f"sample_layout_{i+1}.jpg"))
        ],
        "gallery_desc_1": _fact("gallery_desc_1", ""),
        "gallery_desc_2": _fact("gallery_desc_2", ""),
        "gallery_desc_3": _fact("gallery_desc_3", ""),
        "gallery_desc_4": _fact("gallery_desc_4", ""),

        # Floor plan (from the selected unit's uploaded floor_plan_image)
        "floor_plan_image": _download_image_to_local(
            (unit.floor_plan_image.url if unit and getattr(unit, 'floor_plan_image', None) else ""),
            _gallery_dir,
            "floor_plan.jpg",
        ),
       

        # Market analysis (template provides sensible defaults if blank)
        "market_intro": _fact("market_intro", ""),
        "investment_cases": investment_cases,
        "highlight_title": _fact("highlight_title", ""),
        "highlight_body": _fact("highlight_body", ""),

        "average_rental_yield": _fact("average_rental_yield", ""),
        "annual_house_price_growth": _fact("annual_house_price_growth", ""),
        "local_employment_rate": _fact("local_employment_rate", ""),
        "growing_city_population": _fact("growing_city_population", ""),

        # Payment plan
        "deposit_amount": "",
        "stage_1_amount": "",
        "stage_2_amount": "",
        "stage_3_amount": "",
        "reservation_fee": _fact("reservation_fee", ""),
        "mortgage_amount": _fact("mortgage_amount", ""),
        "mortgage_pct": "",
        "exchange_percentage": _fact("exchange_percentage", ""),
        "completion_percentage": _fact("completion_percentage", ""),
        "exchange_amount": "",
        "reservation_amount": _fact("reservation_fee", ""),
        "total_investment": "",
        "second_installments_percentage": _fact("second_installments_percentage", ""),
        "second_installments_amount": "",
    }

    exchange_pct = _parse_percentage(context["exchange_percentage"])
    completion_pct = _parse_percentage(context["completion_percentage"])
    if exchange_pct is not None and completion_pct is None:
        completion_pct = max(0.0, 100.0 - exchange_pct)
        context["completion_percentage"] = f"{completion_pct:.0f}%"
    elif completion_pct is not None and exchange_pct is None:
        exchange_pct = max(0.0, 100.0 - completion_pct)
        context["exchange_percentage"] = f"{exchange_pct:.0f}%"

    if exchange_pct is None:
        exchange_pct = 20.0
        context["exchange_percentage"] = "20%"
    if completion_pct is None:
        completion_pct = 80.0
        context["completion_percentage"] = "80%"

    mortgage_pct = _parse_percentage(context["mortgage_amount"])
    second_installments_pct = _parse_percentage(context["second_installments_percentage"])

    if list_price is not None:
        try:
            lp = float(list_price)
            context["stage_1_amount"] = _money(lp * exchange_pct / 100.0, currency)
            context["stage_2_amount"] = _money(lp * 0.10, currency)
            # Calculate stage 3 amount (completion) minus reservation fee already paid
            stage_3_gross = lp * completion_pct / 100.0
            reservation_fee_amount = _parse_money(context["reservation_fee"])
            stage_3_net = stage_3_gross - reservation_fee_amount if reservation_fee_amount else stage_3_gross
            context["stage_3_amount"] = _money(stage_3_net, currency)
            context["exchange_amount"] = _money(lp * exchange_pct / 100.0, currency)
            context["total_investment"] = _money(lp, currency)
            context["deposit_amount"] = _money(lp * exchange_pct / 100.0, currency)
            if mortgage_pct is not None:
                context["mortgage_pct"] = _money(lp * mortgage_pct / 100.0, currency)
            if second_installments_pct is not None:
                context["second_installments_amount"] = _money(lp * second_installments_pct / 100.0, currency)
        except (TypeError, ValueError):
            pass

    return context


# ─────────────────────────────────────────────────────────────────────────────
# HTML rendering
# ─────────────────────────────────────────────────────────────────────────────

def _render_html(context: dict) -> str:
    """Render proposal.html with the given context and return the HTML string."""
    if not os.path.exists(_TEMPLATE_PATH):
        raise ProposalPdfError(f"Template not found: {_TEMPLATE_PATH}")

    with open(_TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        template_source = fh.read()

    engine = Engine(debug=False)
    template = engine.from_string(template_source)
    return template.render(Context(context))


def _save_html(project_id: int, html: str) -> str:
    """Write the rendered HTML to <output>/<project_id>/proposal.html."""
    project_dir = os.path.join(_OUTPUT_DIR, str(project_id))
    os.makedirs(project_dir, exist_ok=True)
    html_path = os.path.join(project_dir, "proposal.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    logger.info(
        "Wrote rendered proposal HTML for project %s to %s",
        project_id, html_path,
    )
    return html_path


# ─────────────────────────────────────────────────────────────────────────────
# HTML → PDF engines (no Playwright)
# ─────────────────────────────────────────────────────────────────────────────

def _find_chromium_binary() -> str | None:
    """Locate a Chrome/Edge/Chromium binary that supports --headless --print-to-pdf."""
    env_path = os.environ.get("CHROME_BIN") or os.environ.get("CHROMIUM_BIN")
    if env_path and os.path.exists(env_path):
        return env_path

    candidates: list[str] = []

    if sys.platform == "win32":
        program_files = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LocalAppData", ""),
        ]
        relative_paths = [
            r"Google\Chrome\Application\chrome.exe",
            r"Microsoft\Edge\Application\msedge.exe",
            r"Chromium\Application\chrome.exe",
        ]
        for base in program_files:
            if not base:
                continue
            for rel in relative_paths:
                candidates.append(os.path.join(base, rel))
    else:
        candidates.extend([
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/microsoft-edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ])

    for path in candidates:
        if path and os.path.exists(path):
            return path

    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found

    return None


def _render_pdf_chromium(html_path: str) -> bytes:
    """Convert an HTML file to PDF using headless Chrome/Edge --print-to-pdf."""
    binary = _find_chromium_binary()
    if not binary:
        raise RuntimeError(
            "No Chrome / Edge / Chromium binary found. Install Chrome or Edge, "
            "or set the CHROME_BIN environment variable."
        )

    with tempfile.TemporaryDirectory(prefix="proposal_pdf_") as tmp_dir:
        out_pdf = os.path.join(tmp_dir, "out.pdf")
        user_data = os.path.join(tmp_dir, "user-data")
        os.makedirs(user_data, exist_ok=True)

        source_uri = Path(html_path).resolve().as_uri()
        cmd = [
            binary,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-pdf-header-footer",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=10000",
            "--hide-scrollbars",
            f"--user-data-dir={user_data}",
            f"--print-to-pdf={out_pdf}",
            source_uri,
        ]

        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=120, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Chromium PDF rendering timed out: {exc}") from exc

        if proc.returncode != 0 or not os.path.exists(out_pdf):
            stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
            raise RuntimeError(
                f"Chromium --print-to-pdf failed (exit {proc.returncode}): {stderr.strip()}"
            )

        with open(out_pdf, "rb") as fh:
            pdf_bytes = fh.read()

        if not pdf_bytes:
            raise RuntimeError("Chromium produced an empty PDF.")
        return pdf_bytes


def _render_pdf_weasyprint(html_path: str) -> bytes:
    from weasyprint import HTML

    return HTML(filename=html_path).write_pdf()


def _html_to_pdf(html_path: str) -> bytes:
    """Convert the rendered HTML file to PDF bytes using available engines."""
    engines = (
        ("chromium", _render_pdf_chromium),
        ("weasyprint", _render_pdf_weasyprint),
    )

    errors: list[str] = []
    for name, fn in engines:
        try:
            pdf_bytes = fn(html_path)
            if pdf_bytes:
                logger.info("Rendered proposal PDF using %s", name)
                return pdf_bytes
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s render failed: %s", name, exc)
            errors.append(f"{name}: {exc}")

    raise ProposalPdfError(
        "All HTML→PDF engines failed. " + " | ".join(errors)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def generate_pdf_bytes(context: dict, project_id: int = 0) -> bytes:
    """Render the template with `context`, save the HTML, return PDF bytes."""
    html = _render_html(context)
    html_path = _save_html(project_id, html)
    return _html_to_pdf(html_path)


def generate_proposal_pdf(project, unit, lead, assets: dict, current_user=None) -> bytes:
    """Build context from models + assets and return the proposal PDF bytes."""
    context = build_context(project, unit, lead, assets, current_user=current_user)
    return generate_pdf_bytes(context, project_id=getattr(project, "id", 0))
