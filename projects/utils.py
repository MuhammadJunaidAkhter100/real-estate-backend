"""
Utility helpers for the `projects` app.

Currently exposes a single helper, ``import_units_from_csv``, that ingests a
CSV pricelist directly (no LLM) and creates Unit rows. Designed to be called
synchronously from a DRF action — files are expected to be small.
"""
from __future__ import annotations

import csv
import io
import logging
from decimal import Decimal, InvalidOperation
from typing import IO

from api.constants import COUNTRY_CURRENCY_MAP
from projects.models import Project, Unit

logger = logging.getLogger(__name__)


# Required header columns (exact spelling, case-insensitive match)
REQUIRED_HEADERS = {
    'Unit Label/Name',
    'Type',
    'Floor',
    'Area (sq ft)',
    'Price',
    'Status',
}

# Optional headers we still read
OPTIONAL_HEADERS = {
    'Discounted Price (Selling Price)',
    'EST. MARKET RENT (PCM)',
    'EST. YIELD (GROSS)',
}

# All headers we actually consume — everything else (e.g. "Project", "Notes") is ignored
_RECOGNISED_HEADERS = REQUIRED_HEADERS | OPTIONAL_HEADERS

_VALID_STATUSES = {choice[0] for choice in Unit.UnitStatus.choices}


def _normalise_header(name: str) -> str:
    if name is None:
        return ''
    s = str(name).replace('\r', ' ').replace('\n', ' ').replace('\xa0', ' ').replace('\ufeff', ' ')
    return ' '.join(s.split())


def _is_known_header(name: str) -> bool:
    norm = _normalise_header(name).lower()
    if not norm:
        return False
    for known in _RECOGNISED_HEADERS:
        if norm == known.lower():
            return True
    keywords = {'floor', 'unit', 'price', 'status', 'area', 'type', 'category', 'rent', 'yield', 'number', 'label', 'cost', 'sqft', 'ft'}
    return any(kw in norm for kw in keywords)


def _find_header_row_index(raw_rows: list) -> int:
    """Find the index of the row that contains the most recognized headers."""
    best_idx = 0
    best_score = 0
    for idx, row in enumerate(raw_rows[:30]):
        if not row:
            continue
        score = sum(1 for cell in row if cell is not None and _is_known_header(str(cell)))
        if score > best_score:
            best_score = score
            best_idx = idx
    return best_idx


def _to_decimal(value, field_label: str):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Strip common currency symbols / thousands separators / percent signs
    s = s.replace(',', '').replace('$', '').replace('£', '').replace('€', '').replace('%', '')
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid number for '{field_label}': {value!r}")


def _normalise_status(value: str) -> str:
    s = (value or '').strip().lower()
    if not s:
        return Unit.UnitStatus.AVAILABLE
    # Allow human variants
    aliases = {
        'available': 'available',
        'reserved': 'reserved',
        'sold': 'sold',
    }
    norm = aliases.get(s, s)
    if norm not in _VALID_STATUSES:
        raise ValueError(
            f"Invalid status {value!r}. Allowed: {sorted(_VALID_STATUSES)}."
        )
    return norm


def _rows_from_csv(raw: bytes | str) -> tuple[list[str], list[dict]]:
    """Parse CSV bytes/str into (fieldnames, list-of-row-dicts)."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('latin-1')
    else:
        text = raw

    raw_rows = list(csv.reader(io.StringIO(text)))
    if not raw_rows:
        return [], []

    header_idx = _find_header_row_index(raw_rows)
    fieldnames = [('' if c is None else str(c).strip()) for c in raw_rows[header_idx]]

    rows: list[dict] = []
    for values in raw_rows[header_idx + 1:]:
        if not any(v is not None and str(v).strip() != '' for v in values):
            continue
        row = {}
        for i, name in enumerate(fieldnames):
            if not name:
                continue
            value = values[i] if i < len(values) else ''
            row[name] = '' if value is None else value
        rows.append(row)

    return fieldnames, rows


def _rows_from_xlsx(raw: bytes) -> tuple[list[str], list[dict]]:
    """Parse .xlsx bytes into (fieldnames, list-of-row-dicts) using openpyxl."""
    from openpyxl import load_workbook

    wb = load_workbook(filename=io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb.active
    raw_rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if not raw_rows:
        return [], []

    header_idx = _find_header_row_index(raw_rows)
    fieldnames = [('' if c is None else str(c).strip()) for c in raw_rows[header_idx]]

    rows: list[dict] = []
    for values in raw_rows[header_idx + 1:]:
        if not any(v is not None and str(v).strip() != '' for v in values):
            continue
        row = {}
        for i, name in enumerate(fieldnames):
            if not name:
                continue
            value = values[i] if i < len(values) else None
            row[name] = '' if value is None else value
        rows.append(row)

    return fieldnames, rows


def _rows_from_xls(raw: bytes) -> tuple[list[str], list[dict]]:
    """Parse legacy .xls bytes into (fieldnames, list-of-row-dicts) using xlrd."""
    import xlrd

    book = xlrd.open_workbook(file_contents=raw)
    sheet = book.sheet_by_index(0)
    if sheet.nrows == 0:
        return [], []

    raw_rows = [sheet.row_values(r) for r in range(sheet.nrows)]
    header_idx = _find_header_row_index(raw_rows)
    fieldnames = [('' if v is None else str(v).strip()) for v in raw_rows[header_idx]]

    rows: list[dict] = []
    for values in raw_rows[header_idx + 1:]:
        if not any(v is not None and str(v).strip() != '' for v in values):
            continue
        row = {}
        for i, name in enumerate(fieldnames):
            if not name:
                continue
            value = values[i] if i < len(values) else ''
            row[name] = '' if value is None else value
        rows.append(row)

    return fieldnames, rows


def _extract_rows(file_obj: IO, filename: str = '') -> tuple[list[str], list[dict]]:
    """Read the uploaded file and return (fieldnames, rows) for CSV/XLSX/XLS.

    Format is detected from the filename extension, with a fallback to CSV.
    """
    raw = file_obj.read()
    name = (filename or getattr(file_obj, 'name', '') or '').lower()

    if name.endswith('.xlsx'):
        return _rows_from_xlsx(raw)
    if name.endswith('.xls'):
        return _rows_from_xls(raw)
    if name.endswith('.csv'):
        return _rows_from_csv(raw)

    # Unknown extension: sniff the Excel magic bytes, else treat as CSV.
    head = raw[:8] if isinstance(raw, (bytes, bytearray)) else b''
    if head[:4] == b'PK\x03\x04':  # ZIP container -> .xlsx
        return _rows_from_xlsx(raw)
    if head[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':  # OLE2 -> legacy .xls
        return _rows_from_xls(raw)
    return _rows_from_csv(raw)


def _ai_map_headers(fieldnames: list[str], sample_rows: list[dict], missing_headers: list[str]) -> dict[str, str]:
    """Use gpt-4o-mini to intelligently map arbitrary/foreign column headers to target fields.

    Sends only header names + 2 sample data rows for maximum token/cost efficiency.
    Returns dict mapping target canonical header -> uploaded raw column name.
    """
    try:
        from django.conf import settings
        from openai import OpenAI

        api_key = getattr(settings, 'OPENAI_API_KEY', '')
        if not api_key:
            return {}

        client = OpenAI(api_key=api_key)

        sample_data = []
        for r in sample_rows[:2]:
            clean_r = {k: str(v) for k, v in r.items() if k and v is not None}
            if clean_r:
                sample_data.append(clean_r)

        system_prompt = (
            "You are an expert real estate data parser. Your task is to map spreadsheet "
            "column headers (in any language, spelling, or format) to standard target fields.\n\n"
            "Target fields available:\n"
            "- 'Unit Label/Name': Unit ID, number, label, flat, apartment number (e.g. '001', 'A-101')\n"
            "- 'Type': Unit category, type, bedrooms, beds (e.g. '1 Bed', 'Studio', 'Villa')\n"
            "- 'Floor': Floor level or number (e.g. 'Ground Floor', '1', 'Level 3')\n"
            "- 'Area (sq ft)': Unit area, size, sq ft, sqft, m2, size (e.g. 574, 60m2)\n"
            "- 'Price': List price, asking price, selling price (e.g. 158247, £150000)\n"
            "- 'Status': Availability status (e.g. 'Available', 'Sold', 'Reserved')\n"
            "- 'Discounted Price (Selling Price)': Offer price, discounted price, net price\n"
            "- 'EST. MARKET RENT (PCM)': Estimated monthly rent\n"
            "- 'EST. YIELD (GROSS)': Estimated gross yield or yield percentage\n\n"
            "Return a JSON object where key is the EXACT Target field name and value is "
            "the EXACT uploaded column header name from the provided column list. "
            "Only map columns if you are confident. Return ONLY valid JSON."
        )

        user_content = (
            f"Uploaded Column Headers: {fieldnames}\n"
            f"Missing required target fields: {missing_headers}\n"
            f"Sample Data Rows: {sample_data}\n\n"
            "Map the uploaded column headers to target field names in JSON."
        )

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )

        content = response.choices[0].message.content or "{}"
        import json
        ai_mapping = json.loads(content)

        result = {}
        for target_key, col_name in ai_mapping.items():
            if target_key in _RECOGNISED_HEADERS and col_name in fieldnames:
                result[target_key] = col_name

        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI header mapping fallback failed: %s", exc)
        return {}


def import_units(file_obj: IO, *, project: Project, requested_by, filename: str = '') -> dict:
    """
    Parse the uploaded CSV/XLSX/XLS pricelist and create ``Unit`` rows.

    Required columns: "Unit Label/Name", "Type", "Floor", "Area (sq ft)",
    "Price", "Status".
    Optional columns: "Discounted Price (Selling Price)",
    "EST. MARKET RENT (PCM)" (variants like "Market Rent", "Rent", "Est. Rent"
    are accepted) and "EST. YIELD (GROSS)" (variants like "Gross Yield",
    "Yield", "Est. Yield" are accepted).
    Any other columns (e.g. "Project", "Notes") are ignored.

    Returns a summary dict::

        {
          "created": int,
          "failed": int,
          "errors": [str, ...],
        }
    """
    fieldnames, data_rows = _extract_rows(file_obj, filename)
    if not fieldnames:
        raise ValueError("File is empty or missing a header row.")

    # Map header → normalised canonical header
    header_map = {}
    for raw_header in fieldnames:
        norm = _normalise_header(raw_header)
        # Match either exactly (case-sensitive) or case-insensitively
        for known in _RECOGNISED_HEADERS:
            if norm == known or norm.lower() == known.lower():
                header_map.setdefault(known, raw_header)
                break

    missing = [h for h in REQUIRED_HEADERS if h not in header_map]
    if missing:
        # Try AI header mapping fallback (gpt-4o-mini) for unmapped headers
        ai_mapping = _ai_map_headers(fieldnames, data_rows[:2], missing)
        for target_key, col_name in ai_mapping.items():
            if target_key not in header_map:
                header_map[target_key] = col_name

        missing = [h for h in REQUIRED_HEADERS if h not in header_map]

    if missing:
        raise ValueError(
            "Missing required column(s): " + ", ".join(sorted(missing))
        )

    currency = COUNTRY_CURRENCY_MAP.get(getattr(requested_by, 'current_country', ''), '')
    country = getattr(requested_by, 'current_country', '') or ''

    created = 0
    failed = 0
    errors: list[str] = []

    def _cell(row: dict, key: str) -> str:
        column = header_map.get(key)
        if column is None:
            return ''
        value = row.get(column, '')
        if value is None:
            return ''
        # Excel returns numbers as int/float. Render whole floats without the
        # trailing ".0" (e.g. a unit label "101" stored as 101.0).
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value).strip()

    for idx, row in enumerate(data_rows, start=2):  # row 1 is the header
        try:
            label = _cell(row, 'Unit Label/Name')
            if not label:
                raise ValueError("'Unit Label/Name' is required.")

            list_price = _to_decimal(_cell(row, 'Price'), 'Price')
            if list_price is None:
                raise ValueError("'Price' is required.")

            discounted_raw = _cell(row, 'Discounted Price (Selling Price)')
            discounted_price = _to_decimal(discounted_raw, 'Discounted Price (Selling Price)') \
                if discounted_raw else None

            rent_raw = _cell(row, 'EST. MARKET RENT (PCM)')
            est_market_rent = _to_decimal(rent_raw, 'EST. MARKET RENT (PCM)') \
                if rent_raw else None

            yield_raw = _cell(row, 'EST. YIELD (GROSS)')
            est_yield_gross = _to_decimal(yield_raw, 'EST. YIELD (GROSS)') \
                if yield_raw else None
            if (
                est_yield_gross is not None
                and '%' not in yield_raw
                and est_yield_gross < 1
            ):
                est_yield_gross = est_yield_gross * 100

            area_ft2 = _to_decimal(_cell(row, 'Area (sq ft)'), 'Area (sq ft)')

            unit_status = _normalise_status(_cell(row, 'Status'))

            Unit.objects.create(
                project=project,
                associated_country=country,
                label=label,
                category=_cell(row, 'Type'),
                floor=_cell(row, 'Floor'),
                area_ft2=area_ft2,
                list_price=list_price,
                discounted_price=discounted_price,
                est_market_rent=est_market_rent,
                est_yield_gross=est_yield_gross,
                currency=currency,
                status=unit_status,
                created_by=requested_by,
            )
            created += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            errors.append(f"Row {idx}: {exc}")
            logger.warning("CSV import row %s failed: %s", idx, exc)

    return {"created": created, "failed": failed, "errors": errors}
