import json
import logging
from typing import Any, Dict, List

import fitz  # PyMuPDF
from openai import OpenAI
from django.conf import settings

logger = logging.getLogger(__name__)

# Initialize OpenAI client
_client = OpenAI(api_key=settings.OPENAI_API_KEY)
_MODEL = "gpt-4o"


def _clean_json_response(raw: str) -> str:
    """
    Clean JSON response from OpenAI to ensure valid JSON.
    Removes markdown fences, comments, and other common issues.
    """
    import re
    
    cleaned = raw.strip()
    
    # Remove markdown fences
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:])
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    
    # Remove single-line comments (// ...)
    cleaned = re.sub(r'//[^\n]*', '', cleaned)
    
    # Remove multi-line comments (/* ... */)
    cleaned = re.sub(r'/\*.*?\*/', '', cleaned, flags=re.DOTALL)
    
    # Remove trailing commas before closing braces/brackets
    cleaned = re.sub(r',(\s*[}\]])', r'\1', cleaned)
    
    return cleaned.strip()


def _pymupdf_page_texts(raw: bytes) -> List[str]:
    """Per-page text-layer content via PyMuPDF (cheap, no rendering)."""
    texts = []
    with fitz.open(stream=raw, filetype="pdf") as doc:
        for page in doc:
            texts.append(page.get_text("text") or "")
    return texts


def extract_document_text(file_field) -> str:
    """Extract the text layer of an uploaded PDF document using PyMuPDF only.

    Args:
        file_field: a Django FieldFile / file-like object pointing at a PDF.

    Returns:
        Extracted text as a single string, or '' if nothing could be read.
    """
    try:
        if hasattr(file_field, 'open'):
            file_field.open('rb')
        try:
            raw = file_field.read()
        finally:
            if hasattr(file_field, 'close'):
                file_field.close()

        if not raw:
            return ''

        page_texts = _pymupdf_page_texts(raw)
        return '\n'.join(t for t in page_texts if t.strip()).strip()
    except Exception as e:  # noqa: BLE001 - never let extraction crash the caller
        logger.error(f"Document text extraction failed: {e}", exc_info=True)
        return ''


def _extract_pdf_content(pdf_file) -> Dict[str, Any]:
    """
    Extract text and table data from PDF using PyMuPDF.
    
    Returns:
        Dict with extracted text, tables, and metadata
    """
    content = {
        "text": "",
        "tables": [],
        "page_count": 0
    }
    
    try:
        # Read PDF bytes
        pdf_bytes = pdf_file.read()
        
        # Open PDF with PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        content["page_count"] = len(doc)
        
        all_text = []
        
        for page_num in range(len(doc)):
            page = doc[page_num]
            
            # Extract text from page
            page_text = page.get_text("text")
            all_text.append(f"--- Page {page_num + 1} ---\n{page_text}")
            
            # Try to extract tables (structured data)
            # PyMuPDF's table extraction looks for grid-like structures
            try:
                tables = page.find_tables()
                if tables:
                    for table_idx, table in enumerate(tables.tables):
                        # Extract table as list of lists
                        table_data = table.extract()
                        if table_data:
                            content["tables"].append({
                                "page": page_num + 1,
                                "table_index": table_idx,
                                "data": table_data
                            })
            except Exception as e:
                logger.warning(f"Could not extract tables from page {page_num + 1}: {e}")
        
        content["text"] = "\n\n".join(all_text)
        doc.close()
        
        return content
        
    except Exception as e:
        logger.error(f"PDF content extraction failed: {e}", exc_info=True)
        return {}


def _structure_with_ai(pdf_content: Dict[str, Any]) -> Dict[str, Any]:
    """
    Use OpenAI to convert extracted PDF content into structured unit data.
    
    This handles varying PDF formats by using AI to intelligently parse
    and structure the content into a consistent format.
    """
    
    # Prepare content for AI
    content_for_ai = f"""PDF Content ({pdf_content['page_count']} pages):

TEXT CONTENT:
{pdf_content['text'][:10000]}  # Limit to first 10k chars to save tokens

"""
    
    # Add table data if available
    if pdf_content['tables']:
        content_for_ai += "\nTABLE DATA:\n"
        for table in pdf_content['tables']:
            content_for_ai += f"\n--- Page {table['page']}, Table {table['table_index'] + 1} ---\n"
            # Convert table to readable format
            for row in table['data'][:50]:  # Limit rows
                content_for_ai += " | ".join(str(cell or "").strip() for cell in row) + "\n"
    
    # Prepare prompt for OpenAI
    system_prompt = """You are a property data extraction specialist. Extract ALL columns from PDF pricelists.

TASK:
Extract EVERY column/field from the PDF pricelist. Common columns include:
- Floor (Ground Floor, 1st Floor, etc.)
- Unit Number/Label
- Category/Type (1 Bed, 2 Bed, Studio, etc.)
- Area in m² (square meters)
- Area in ft² (square feet)
- List Price / Original Price
- Discounted Price / Sale Price
- Discount Percentage
- Status (available/reserved/sold)
- Currency

RULES:
1. Extract ALL units from the document
2. Extract ALL columns - do not skip any data
3. Keep areas in BOTH m² and ft² if both are provided
4. Extract BOTH list_price and discounted_price if both exist
5. Extract only numerical values for prices (remove currency symbols like £, $)
6. If a column is missing, use null
7. Infer unit_type: "1 Bed"/"2 Bed" -> "apartment", "Villa" -> "villa", etc.
8. Extract project name, developer, location if mentioned

OUTPUT FORMAT:
Return ONLY valid JSON. Use this structure:
{
    "project_info": {
        "name": "Project Name or null",
        "developer": "Developer Name or null",
        "location": "Location or null"
    },
    "units": [
        {
            "label": "001",
            "unit_type": "apartment",
            "category": "1 Bed",
            "floor": "Ground Floor",
            "area_m2": 53.0,
            "area_ft2": 574.0,
            "list_price": 155155.00,
            "discounted_price": 150500.00,
            "discount_percentage": 3.0,
            "currency": "GBP",
            "status": "available"
        }
    ]
}

CRITICAL: Return pure JSON only. NO comments, NO markdown fences, NO explanations.
"""

    user_prompt = content_for_ai
    
    try:
        # Call OpenAI
        response = _client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.1,  # Low temperature for consistent extraction
            max_tokens=16000,  # Increased for large pricelists with many units
            response_format={"type": "json_object"}  # Force JSON output
        )
        
        # Parse response
        raw_content = response.choices[0].message.content.strip()
        
        # Clean the response
        cleaned_content = _clean_json_response(raw_content)
        
        # Parse JSON
        structured_data = json.loads(cleaned_content)
        
        return structured_data
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON parsing failed: {e}", exc_info=True)
        logger.error(f"Raw OpenAI response: {raw_content[:1000]}...")  # Log first 1000 chars
        # Return empty structure on failure
        return {
            "project_info": {},
            "units": []
        }
    except Exception as e:
        logger.error(f"AI structuring failed: {e}", exc_info=True)
        # Return empty structure on failure
        return {
            "project_info": {},
            "units": []
        }


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Safely convert value to float."""
    try:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            # Remove common non-numeric characters
            cleaned = value.replace(",", "").replace("£", "").replace("$", "").strip()
            return float(cleaned)
        return default
    except (ValueError, TypeError):
        return default
