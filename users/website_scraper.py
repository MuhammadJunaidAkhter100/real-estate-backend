import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString
from django.conf import settings
from openai import OpenAI

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
}


def normalize_website_url(url: str) -> str:
    """Normalize a website URL by ensuring valid scheme and trimming whitespace."""
    if not url:
        return ""
    clean = str(url).strip().strip('"\'')
    if not clean.startswith(("https://", "http://")):
        clean = "https://" + clean
    parsed = urlparse(clean)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return clean.rstrip("/")
    return clean


def scrape_website(url: str) -> str:
    """
    Scrape homepage text while preserving links in their original positions.
    """
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    # Remove non-content elements.
    for tag in soup([
        "script",
        "style",
        "noscript",
        "svg",
        "iframe",
    ]):
        tag.decompose()

    base_url = str(response.url) if getattr(response, "url", None) and isinstance(response.url, str) else str(url)

    # Replace links with their text and absolute URLs.
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        link_text = a.get_text(" ", strip=True)

        if link_text:
            a.replace_with(
                NavigableString(f"{link_text} [URL: {href}]")
            )
        else:
            a.replace_with(NavigableString(f"[URL: {href}]"))

    return soup.get_text(separator=" ", strip=True)


def extract_projects_from_html(html: str, base_url: str) -> List[Dict[str, Any]]:
    """
    Extract structured project cards directly from HTML.
    Supports card containers (e.g. .project-card) and anchor links.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc.lower()
    project_cards = []
    seen_urls = set()

    # 1. Look for explicit project card containers
    card_elements = soup.find_all(
        lambda tag: tag.name in ("div", "article", "section", "li")
        and any("project" in c.lower() or "property" in c.lower() or "development" in c.lower()
                for c in tag.get("class", []))
    )

    for card in card_elements:
        a_tag = card.find("a", href=True)
        if not a_tag:
            continue
        abs_url = urljoin(base_url, a_tag["href"].strip())
        parsed = urlparse(abs_url)
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != base_domain:
            continue
        clean_url = abs_url.split("?")[0].rstrip("/")
        if clean_url in seen_urls:
            continue

        h_tag = card.find(["h1", "h2", "h3", "h4", "h5", "h6"])
        title = h_tag.get_text(" ", strip=True) if h_tag else a_tag.get_text(" ", strip=True)
        if not title:
            continue

        img_tag = card.find("img")
        img_src = ""
        if img_tag:
            img_src = img_tag.get("src") or img_tag.get("data-src") or ""
            if img_src:
                img_src = urljoin(base_url, img_src)

        p_tag = card.find("p")
        desc = p_tag.get_text(" ", strip=True) if p_tag else ""

        seen_urls.add(clean_url)
        project_cards.append({
            "title": title[:255].strip(),
            "name": title[:255].strip(),
            "url": abs_url,
            "detail_page_url": abs_url,
            "image": img_src,
            "image_url": img_src,
            "description": desc,
        })

    # 2. Look for project links matching path conventions
    for a in soup.find_all("a", href=True):
        abs_url = urljoin(base_url, a["href"].strip())
        parsed = urlparse(abs_url)
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != base_domain:
            continue

        path = parsed.path.rstrip("/").lower()
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            continue

        if parts[0] in ("projects", "developments", "properties", "ansambluri", "ansamblu", "portfolio", "complex"):
            clean_url = abs_url.split("?")[0].rstrip("/")
            if clean_url in seen_urls:
                continue
            seen_urls.add(clean_url)

            h_tag = a.find(["h1", "h2", "h3", "h4", "h5", "h6"])
            name = h_tag.get_text(" ", strip=True) if h_tag else ""
            if not name:
                name = a.get("title", "").strip()
            if not name:
                img = a.find("img")
                if img and img.get("alt"):
                    name = img.get("alt").strip()
            if not name:
                raw_text = a.get_text(" ", strip=True)
                name = re.split(r"(?:from|€|\$|£|RON|AED)", raw_text, flags=re.IGNORECASE)[0].strip()
            if not name:
                name = parts[-1].replace("-", " ").replace("_", " ").title()

            img_tag = a.find("img")
            img_src = ""
            if img_tag:
                img_src = img_tag.get("src") or img_tag.get("data-src") or ""
                if img_src:
                    img_src = urljoin(base_url, img_src)

            project_cards.append({
                "title": name[:255].strip(),
                "name": name[:255].strip(),
                "url": abs_url,
                "detail_page_url": abs_url,
                "image": img_src,
                "image_url": img_src,
                "description": "",
            })

    return project_cards


def extract_projects_from_dom(website: str, html_contents: List[str]) -> List[Dict[str, str]]:
    """
    Extract all distinct project detail links from a list of HTML pages.
    """
    seen_urls = set()
    results = []
    for html in html_contents:
        if not html:
            continue
        cards = extract_projects_from_html(html, website)
        for c in cards:
            u = c.get("detail_page_url") or c.get("url")
            clean_u = u.split("?")[0].rstrip("/")
            if clean_u not in seen_urls:
                seen_urls.add(clean_u)
                results.append({
                    "name": c.get("name") or c.get("title") or "",
                    "title": c.get("title") or c.get("name") or "",
                    "detail_page_url": u,
                    "image_url": c.get("image_url") or c.get("image") or "",
                })
    return results


def _parse_projects_json_resiliently(raw: str) -> List[Dict[str, Any]]:
    """
    Parse JSON returned by AI with resilient fallback for truncated outputs
    or legacy response formats (e.g. {'selected': [...]}, {'projects': [...]}).
    Prevents 'Expecting property name enclosed in double quotes' when tokens truncate.
    """
    if not raw or not raw.strip():
        raise ValueError("AI returned an empty response.")

    # 1. Standard json.loads
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            if isinstance(parsed.get("projects"), list):
                return parsed["projects"]
            if isinstance(parsed.get("selected"), list):
                return [{"__selected_index__": idx} for idx in parsed["selected"]]
            if parsed.get("is_real_estate_project") or parsed.get("title"):
                return [parsed]
        elif isinstance(parsed, list):
            return parsed
    except Exception as exc:
        logger.warning(
            "Standard json.loads failed (%s). Attempting resilient regex recovery on raw content.",
            exc,
        )

    # 2. Resilient regex recovery: extract all complete {"name": "...", "detail_page_url": "..."} objects
    recovered = []
    pattern = re.compile(
        r'\{\s*"name"\s*:\s*"(?P<name>(?:[^"\\]|\\.)*)"\s*,\s*"detail_page_url"\s*:\s*"(?P<url>(?:[^"\\]|\\.)*)"\s*\}|'
        r'\{\s*"detail_page_url"\s*:\s*"(?P<url2>(?:[^"\\]|\\.)*)"\s*,\s*"name"\s*:\s*"(?P<name2>(?:[^"\\]|\\.)*)"\s*\}',
        re.DOTALL,
    )
    for m in pattern.finditer(raw):
        name_val = m.group("name") or m.group("name2")
        url_val = m.group("url") or m.group("url2")
        if name_val and url_val:
            try:
                name_clean = json.loads(f'"{name_val}"')
                url_clean = json.loads(f'"{url_val}"')
                recovered.append({"name": name_clean, "detail_page_url": url_clean})
            except Exception:
                recovered.append({"name": name_val, "detail_page_url": url_val})

    if recovered:
        logger.info("Resilient parser successfully recovered %d projects from truncated JSON.", len(recovered))
        return recovered

    # 3. Check for selected indices in truncated text
    sel_match = re.search(r'"selected"\s*:\s*\[([0-9,\s]+)\]', raw)
    if sel_match:
        indices = [int(i.strip()) for i in sel_match.group(1).split(",") if i.strip().isdigit()]
        return [{"__selected_index__": idx} for idx in indices]

    raise ValueError(f"Could not parse valid projects from AI response: {raw[:200]}")


def _ai_select_homepage_project_cards(website: str, homepage_content: str) -> List[Dict[str, str]]:
    """
    Ask AI to extract real-estate project names and their detail page URLs.
    Uses 16,000 max tokens and resilient parsing to prevent JSON truncation errors.
    """
    api_key = getattr(settings, "OPENAI_API_KEY", None) if settings.configured else os.environ.get("OPENAI_API_KEY")
    client = OpenAI(
        api_key=api_key
    )

    candidate_matches = re.findall(
        r"([^\[\n\r]{2,80})\s*\[URL:\s*(https?://[^\s\]]+)\s*\]",
        homepage_content,
    )
    source_urls = {
        url.split("?")[0].rstrip("/").lower(): url
        for _, url in candidate_matches
    }

    prompt = f"""
You are an expert real-estate website data extraction assistant.

Extract all genuine real-estate development projects from the website content below.

For every project, return:
- name: The displayed project name.
- detail_page_url: Its dedicated project detail page URL.

Rules:
1. Include all identifiable real-estate development projects and communities.
2. Include featured projects and portfolio cards.
3. Prefer dedicated project detail URLs (e.g. under /projects/, /developments/, /properties/, /ansambluri/, etc.).
4. Exclude contact pages, blogs, city guides, generic navigation, company/about pages, and social media links.
5. Use URLs exactly as they appear in the supplied content [URL: ...].
6. Never invent or construct URLs.
7. Only include projects with an identifiable detail URL.
8. Remove duplicate projects.
9. Return valid JSON only.

Expected JSON structure:
{{
    "projects": [
        {{
            "name": "London House",
            "detail_page_url": "https://example.com/developments/london-house/"
        }}
    ]
}}

Website: {website}

Content:
{homepage_content[:100000]}
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract real-estate development projects accurately. "
                        "Use only supplied URLs. Never fabricate URLs. "
                        "Return valid JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            max_tokens=16000,
        )
        raw = response.choices[0].message.content
        projects = _parse_projects_json_resiliently(raw)
    except Exception as exc:
        logger.warning("_ai_select_homepage_project_cards encountered an issue: %s", exc)
        projects = []

    if projects and isinstance(projects[0], dict) and "__selected_index__" in projects[0]:
        selected_projects = []
        for p in projects:
            idx = p["__selected_index__"]
            if 0 <= idx < len(candidate_matches):
                c_name, c_url = candidate_matches[idx]
                selected_projects.append({
                    "name": c_name.strip(),
                    "title": c_name.strip(),
                    "detail_page_url": c_url.strip(),
                })
        return selected_projects

    base_domain = urlparse(website).netloc.lower()
    validated_projects = []
    seen = set()

    for project in projects:
        if not isinstance(project, dict):
            continue

        name = project.get("name") or project.get("title")
        detail_url = project.get("detail_page_url") or project.get("url")

        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()

        if not isinstance(detail_url, str) or not detail_url.strip():
            matched_url = None
            for c_name, c_url in candidate_matches:
                if name.lower() in c_name.lower() or c_name.lower() in name.lower():
                    matched_url = c_url
                    break
            if not matched_url and candidate_matches:
                matched_url = candidate_matches[0][1]
            detail_url = matched_url or ""

        detail_url = detail_url.strip()
        if not detail_url:
            continue

        clean_detail_url = detail_url.split("?")[0].rstrip("/").lower()
        if source_urls and clean_detail_url not in source_urls:
            if detail_url not in source_urls.values():
                continue

        parsed_url = urlparse(detail_url)
        if parsed_url.scheme not in ("http", "https") or parsed_url.netloc.lower() != base_domain:
            continue

        if parsed_url.path.rstrip("/") == "":
            continue

        key = (name.casefold(), clean_detail_url)
        if key in seen:
            continue
        seen.add(key)

        ai_meta = project.get("ai_structured_data") or {
            k: v for k, v in project.items()
            if k in ("developer", "location", "starting_price", "currency", "status", "bedrooms", "project_type")
        }
        validated_projects.append({
            "name": name,
            "title": name,
            "detail_page_url": detail_url,
            "ai_structured_data": ai_meta,
        })

    return validated_projects


def scrape_project_image(detail_page_url: str) -> Optional[str]:
    """
    Extract the project's primary image from its detail page.
    """
    try:
        response = requests.get(
            detail_page_url,
            headers=HEADERS,
            timeout=25,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # 1. Prefer Open Graph / Twitter image
        selectors = [
            ("meta", {"property": "og:image"}),
            ("meta", {"property": "og:image:secure_url"}),
            ("meta", {"name": "twitter:image"}),
            ("meta", {"name": "twitter:image:src"}),
        ]
        for tag_name, attrs in selectors:
            tag = soup.find(tag_name, attrs)
            if not tag:
                continue
            image_url = tag.get("content", "").strip()
            if not image_url:
                continue
            image_url = urljoin(response.url, image_url)
            parsed_image = urlparse(image_url)
            if parsed_image.scheme in ("http", "https"):
                return image_url

        # 2. Main content image
        main = soup.find("main") or soup.find("article") or soup.body or soup
        for img in main.find_all("img"):
            image_url = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-lazy-src")
                or img.get("data-original")
                or img.get("data-original-src")
            )
            if not image_url and img.get("srcset"):
                candidates = img["srcset"].split(",")
                if candidates:
                    image_url = candidates[0].strip().split()[0]

            if not image_url:
                continue

            image_url = urljoin(response.url, image_url)
            parsed_image = urlparse(image_url)
            if parsed_image.scheme not in ("http", "https"):
                continue

            alt = img.get("alt", "").lower()
            image_classes = " ".join(img.get("class", [])).lower()
            if any(word in f"{alt} {image_classes}" for word in ("logo", "icon", "avatar", "flag")):
                continue

            return image_url

    except Exception as exc:
        logger.warning("Could not retrieve project image from %s: %s", detail_page_url, exc)

    return None


def scrape_project_details(detail_page_url: str) -> Dict[str, Any]:
    """
    Extract comprehensive details for a single project detail page:
    image_url, description, price, currency, location, developer, title.
    """
    details: Dict[str, Any] = {
        "image_url": "",
        "title": "",
        "description": "",
        "starting_price": 0,
        "currency": "",
        "location": "",
        "developer": "",
        "ai_structured_data": {},
    }
    try:
        response = requests.get(
            detail_page_url,
            headers=HEADERS,
            timeout=25,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # 1. Clean Title Extraction
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            details["title"] = h1.get_text(" ", strip=True)[:255]
        elif soup.find("meta", {"property": "og:title"}):
            raw_og = soup.find("meta", {"property": "og:title"}).get("content", "").strip()
            details["title"] = raw_og.split("|")[0].split("-")[0].strip()[:255]

        # 2. Image URL
        selectors = [
            ("meta", {"property": "og:image"}),
            ("meta", {"property": "og:image:secure_url"}),
            ("meta", {"name": "twitter:image"}),
            ("meta", {"name": "twitter:image:src"}),
        ]
        for tag_name, attrs in selectors:
            tag = soup.find(tag_name, attrs)
            if not tag:
                continue
            image_url = tag.get("content", "").strip()
            if not image_url:
                continue
            image_url = urljoin(response.url, image_url)
            parsed_image = urlparse(image_url)
            if parsed_image.scheme in ("http", "https"):
                details["image_url"] = image_url
                break

        if not details["image_url"]:
            main = soup.find("main") or soup.find("article") or soup.body or soup
            for img in main.find_all("img"):
                image_url = (
                    img.get("src")
                    or img.get("data-src")
                    or img.get("data-lazy-src")
                    or img.get("data-original")
                    or img.get("data-original-src")
                )
                if not image_url and img.get("srcset"):
                    candidates = img["srcset"].split(",")
                    if candidates:
                        image_url = candidates[0].strip().split()[0]
                if not image_url:
                    continue
                image_url = urljoin(response.url, image_url)
                parsed_image = urlparse(image_url)
                if parsed_image.scheme not in ("http", "https"):
                    continue
                alt = img.get("alt", "").lower()
                image_classes = " ".join(img.get("class", [])).lower()
                if any(word in f"{alt} {image_classes}" for word in ("logo", "icon", "avatar", "flag")):
                    continue
                details["image_url"] = image_url
                break

        # 3. Description
        desc_tag = soup.find("meta", {"name": "description"}) or soup.find("meta", {"property": "og:description"})
        description = desc_tag.get("content", "").strip() if desc_tag else ""
        details["description"] = description

        # 4. Text for price, location, developer
        title_text = soup.title.string.strip() if soup.title and soup.title.string else ""
        combined_text = f"{title_text}. {description}"

        # Starting price
        price_match = re.search(
            r'(?:€|\$|£|AED|RON|Prices?\s*(?:from|:)?|From\s*[:\s]*)\s*([0-9]{1,3}(?:[.,\s][0-9]{3})*(?:[.,][0-9]{2})?)',
            combined_text,
            re.IGNORECASE,
        )
        if price_match:
            raw_p = price_match.group(1).replace(' ', '').replace(',', '')
            try:
                details["starting_price"] = float(raw_p)
            except ValueError:
                pass

        # Currency
        if '€' in combined_text or 'EUR' in combined_text:
            details["currency"] = 'EUR'
        elif '$' in combined_text or 'USD' in combined_text:
            details["currency"] = 'USD'
        elif 'AED' in combined_text:
            details["currency"] = 'AED'
        elif '£' in combined_text or 'GBP' in combined_text:
            details["currency"] = 'GBP'
        elif 'RON' in combined_text:
            details["currency"] = 'RON'

        # Developer
        dev_match = re.search(r'Developed\s+by\s+([^.,;\n]+)', combined_text, re.IGNORECASE)
        if dev_match:
            details["developer"] = dev_match.group(1).strip()[:255]

        # Location
        loc_match = re.search(
            r'(?:in|location:?)\s+([A-Z][a-zA-Z\s,]+(?:Bucure[șs]ti|Bucharest|Dubai|London|Paris|Berlin|Abu Dhabi|[A-Z][a-z]+))',
            combined_text,
            re.IGNORECASE,
        )
        if loc_match:
            details["location"] = loc_match.group(1).strip()[:255]

        details["ai_structured_data"] = {
            "developer": details["developer"],
            "location": details["location"],
            "starting_price": details["starting_price"],
            "currency": details["currency"],
        }

    except Exception as exc:
        logger.warning("Could not retrieve project details from %s: %s", detail_page_url, exc)

    return details


def scrape_company_website(website: str, on_progress=None) -> Dict[str, Any]:
    """
    Scrape a company homepage, extract genuine real estate projects,
    and retrieve image URLs & project metadata for each project.
    """
    try:
        normalized_website = normalize_website_url(website)
        if not normalized_website:
            return {
                "website": website,
                "status": "empty",
                "projects": [],
                "total_projects": 0,
            }

        logger.info("Scraping website: %s", normalized_website)
        resp = requests.get(normalized_website, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        homepage_html = resp.text

        base_url = str(resp.url) if getattr(resp, "url", None) and isinstance(resp.url, str) else str(normalized_website)
        soup = BeautifulSoup(homepage_html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
            tag.decompose()
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a["href"].strip())
            link_text = a.get_text(" ", strip=True)
            if link_text:
                a.replace_with(NavigableString(f"{link_text} [URL: {href}]"))
            else:
                a.replace_with(NavigableString(f"[URL: {href}]"))
        homepage_content = soup.get_text(separator=" ", strip=True)

        # Discover dedicated project catalog pages
        catalog_urls = []
        for url_match in re.findall(r"\[URL:\s*(https?://[^\s\]]+)\s*\]", homepage_content):
            path = urlparse(url_match).path.rstrip("/").lower()
            if any(path.endswith(cat) for cat in (
                "/projects", "/developments", "/properties",
                "/our-projects", "/portfolio", "/all-projects", "/residential"
            )):
                if url_match not in catalog_urls:
                    catalog_urls.append(url_match)

        raw_html_list = [homepage_html]
        combined_content = homepage_content

        for cat_url in catalog_urls[:2]:
            try:
                logger.info("Scraping project catalog page: %s", cat_url)
                cat_resp = requests.get(cat_url, headers=HEADERS, timeout=30)
                cat_resp.raise_for_status()
                raw_html_list.append(cat_resp.text)

                cat_base_url = str(cat_resp.url) if getattr(cat_resp, "url", None) and isinstance(cat_resp.url, str) else str(cat_url)
                cat_soup = BeautifulSoup(cat_resp.text, "html.parser")
                for tag in cat_soup(["script", "style", "noscript", "svg", "iframe"]):
                    tag.decompose()
                for a in cat_soup.find_all("a", href=True):
                    href = urljoin(cat_base_url, a["href"].strip())
                    link_text = a.get_text(" ", strip=True)
                    if link_text:
                        a.replace_with(NavigableString(f"{link_text} [URL: {href}]"))
                    else:
                        a.replace_with(NavigableString(f"[URL: {href}]"))
                cat_content = cat_soup.get_text(separator=" ", strip=True)
                combined_content += f"\n\n--- CATALOG PAGE ({cat_url}) ---\n" + cat_content
            except Exception as exc:
                logger.warning("Could not scrape catalog page %s: %s", cat_url, exc)

        # 1. AI-based project selection
        projects = _ai_select_homepage_project_cards(
            normalized_website,
            combined_content,
        )

        # 2. DOM-based extraction acts as fallback if AI returns nothing or fails
        if not projects:
            logger.info("AI returned 0 projects. Falling back to DOM extraction.")
            projects = extract_projects_from_dom(normalized_website, raw_html_list)

        if not projects:
            return {
                "website": normalized_website,
                "status": "empty",
                "projects": [],
                "total_projects": 0,
            }

        # 3. Concurrent detail scraping
        total_projects = len(projects)
        completed_count = 0
        progress_lock = Lock()

        def _fetch_details(project_item: Dict[str, Any]):
            nonlocal completed_count
            detail_url = project_item.get("detail_page_url") or project_item.get("url")
            if detail_url:
                details = scrape_project_details(detail_url)
                if project_item.get("ai_structured_data"):
                    if not details.get("developer"):
                        details["developer"] = project_item["ai_structured_data"].get("developer", "")
                    details.setdefault("ai_structured_data", {}).update(project_item["ai_structured_data"])
                if details.get("title") and not project_item.get("name"):
                    project_item["name"] = details["title"]
                if not details.get("title") and project_item.get("name"):
                    details["title"] = project_item["name"]
                project_item.update(details)

            with progress_lock:
                completed_count += 1
                if on_progress:
                    try:
                        on_progress(completed_count, total_projects, project_item.get("name") or project_item.get("title", ""))
                    except Exception:
                        pass

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(_fetch_details, projects))

        return {
            "website": normalized_website,
            "status": "completed",
            "projects": projects,
            "total_projects": len(projects),
        }

    except Exception as exc:
        logger.exception("Scraping failed for website %s: %s", website, exc)
        return {
            "website": website,
            "status": "failed",
            "projects": [],
            "total_projects": 0,
            "error": str(exc),
        }
