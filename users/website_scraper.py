import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urljoin, urlparse

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
MAX_PROJECT_CATALOG_PAGES = 10


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


def _pagination_page_urls(soup: BeautifulSoup, current_url: str) -> List[str]:
    """Return pagination links on the current page, limited to its own route/site."""
    current = urlparse(current_url)
    page_parameter_names = {'page', 'paged', 'page_num', 'page_number', 'currentpage'}
    pagination_urls = []
    seen = set()

    for link in soup.find_all('a', href=True):
        page_url = urljoin(current_url, link['href'].strip())
        parsed = urlparse(page_url)
        if (
            parsed.scheme not in ('http', 'https')
            or parsed.netloc.lower() != current.netloc.lower()
            or page_url.split('#', 1)[0] == current_url.split('#', 1)[0]
        ):
            continue

        query_values = {name.lower(): value for name, value in parse_qsl(parsed.query)}
        query_names = set(query_values)
        same_route_pagination = (
            parsed.path.rstrip('/') == current.path.rstrip('/')
            and bool(query_names & page_parameter_names)
        )
        is_first_page_alias = (
            parsed.path.rstrip('/') == current.path.rstrip('/')
            and any(query_values.get(name) == '1' for name in page_parameter_names)
        )
        if is_first_page_alias:
            continue
        path_pagination = bool(re.search(r'/(?:page|pages)/\d+/?$', parsed.path, re.IGNORECASE))

        context = ' '.join([
            ' '.join(link.get('class', [])),
            str(link.get('aria-label', '')),
            str(link.get('rel', '')),
        ]).lower()
        parent = link.parent
        for _ in range(3):
            if not parent or getattr(parent, 'name', None) in ('body', 'html'):
                break
            context += ' ' + ' '.join(parent.get('class', []))
            context += ' ' + str(parent.get('aria-label', ''))
            parent = parent.parent

        link_text = link.get_text(' ', strip=True).lower()
        is_next_link = (
            'next' in context
            or 'next' in link_text
            or link_text in ('»', '›', '→')
            or 'next' in link.get('rel', [])
        )
        numbered_pagination = (
            'pagination' in context
            and bool(re.fullmatch(r'\d+', link_text))
        )

        if not (same_route_pagination or path_pagination or is_next_link or numbered_pagination):
            continue
        if page_url not in seen:
            seen.add(page_url)
            pagination_urls.append(page_url)

    return pagination_urls


def _candidate_project_urls_from_content(content: str, website: str) -> List[str]:
    """
    Identify the main real-estate project listing/catalog page
    from homepage content using OpenAI.

    Returns listing page URLs, NOT individual project URLs.
    """

    if not content or not content.strip():
        logger.warning("No homepage content provided for %s", website)
        return []

    api_key = getattr(settings, "OPENAI_API_KEY", None)

    if not api_key:
        logger.error("OpenAI API key is not configured.")
        return []

    client = OpenAI(api_key=api_key)

    prompt = f"""
You are an expert real-estate website analyzer.

Analyze the homepage content of this real-estate company.

GOAL:
Find the URL of the MAIN PAGE that lists multiple real-estate
projects, properties, communities, or developments.

IMPORTANT DISTINCTION:
- Return the page containing a collection/list/grid of projects.
- DO NOT return individual project detail URLs.
- DO NOT return URLs such as /properties/marlowe/ or
  /properties/vivienne/ when these represent individual projects.
- Look for links such as Properties, Our Properties, Communities,
  Developments, Projects, Portfolio, or similar.
- Prefer the page that lists multiple projects, not a single
  project's detail page.
- The listing page may have a URL such as /properties/,
  /communities/, /developments/, or another path.
- Do not assume a URL exists just because its path looks likely.
- Return only exact URLs found in the supplied homepage content.
- Never invent or construct URLs.
- If no listing page can be confidently identified, return [].
- Return the most relevant listing page first.
- Return unique URLs only.

Return valid JSON in this format:
{{
    "candidate_urls": [
        "https://example.com/properties/"
    ]
}}

Website: {website}

Homepage content:
{content}
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.3,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Identify real-estate project listing/catalog "
                        "pages, not individual project detail pages. "
                        "Only return exact URLs present in the supplied "
                        "content. Never invent URLs. Return valid JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
        )

        raw_response = response.choices[0].message.content

        if not raw_response:
            return []

        parsed = json.loads(raw_response)
        urls = parsed.get("candidate_urls", [])

        if not isinstance(urls, list):
            return []

        valid_urls = []
        seen = set()

        for url in urls:
            if not isinstance(url, str):
                continue

            url = url.strip()

            if not url.startswith(("https://", "http://")):
                continue

            if url in seen:
                continue

            seen.add(url)
            valid_urls.append(url)

        return valid_urls

    except Exception:
        logger.exception(
            "Failed to identify project listing pages for %s",
            website,
        )
        return []


def _ai_validate_project_detail_page(website: str, candidate_url: str, page_text: str) -> Optional[Dict[str, str]]:
    """
    Use AI to confirm whether a candidate detail page is a real-estate project page.
    Returns a normalized project dict for valid pages, otherwise None.
    """
    api_key = getattr(settings, "OPENAI_API_KEY", None) if settings.configured else os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None

    client = OpenAI(api_key=api_key)
    title_hint = candidate_url.rsplit("/", 1)[-1].replace("-", " ").strip()
    prompt = f"""
You are validating a potential real-estate project page.

Task:
- Determine whether this URL is a real estate property or development project page, not a generic site page.
- If it is a project page, return the project name and the exact URL.
- Exclude about pages, team pages, news/blog pages, contact pages, careers, and generic search or portfolio pages.
- Return JSON only.

Expected JSON:
{{
  "is_project": true,
  "name": "The Clover at Olive Crossing",
  "detail_page_url": "https://example.com/properties/the-clover-at-olive-crossing/"
}}

Website: {website}
URL: {candidate_url}
Possible title hint: {title_hint}
Page text:
{page_text[:20000]}
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "You validate whether a webpage is a real estate project detail page. Return valid JSON only.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            max_tokens=8000,
        )
        raw = response.choices[0].message.content
        parsed = json.loads(raw)
    except Exception as exc:
        logger.warning("AI validation failed for %s: %s", candidate_url, exc)
        return None

    if not isinstance(parsed, dict) or parsed.get("is_project") is not True:
        return None

    name = parsed.get("name") or ""
    detail_url = parsed.get("detail_page_url") or candidate_url
    if not isinstance(name, str) or not name.strip():
        return None

    return {
        "name": name.strip(),
        "title": name.strip(),
        "detail_page_url": detail_url.strip(),
    }


def _ai_select_homepage_project_cards(website: str, homepage_content: str, candidate_urls: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """
    Ask AI to extract real-estate project names and their detail page URLs.
    Uses 16,000 max tokens and resilient parsing to prevent JSON truncation errors.
    """

    print("homepage_content: ",homepage_content)

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
{homepage_content}
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
            ]
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
    final_projects = []
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
        final_projects.append({
            "name": name,
            "title": name,
            "detail_page_url": detail_url,
            "ai_structured_data": ai_meta,
        })

    return final_projects


def _select_project_image(soup: BeautifulSoup, base_url: str, title: str = "") -> Optional[str]:
    selectors = [
        ("meta", {"property": "og:image"}),
        ("meta", {"property": "og:image:secure_url"}),
        ("meta", {"name": "twitter:image"}),
        ("meta", {"name": "twitter:image:src"}),
    ]
    candidates = []

    for tag_name, attrs in selectors:
        for tag in soup.find_all(tag_name, attrs):
            image_url = (tag.get("content") or "").strip()
            if image_url:
                candidates.append((image_url, ""))

    main = soup.find("main") or soup.find("article") or soup.body or soup
    title_tokens = {
        token for token in re.findall(r"[a-z0-9]+", title.lower()) if len(token) > 2
    }
    preferred_terms = (
        "project", "property", "development", "exterior", "hero", "featured",
        "gallery", "building", "residence", "residential", "cover",
    )
    rejected_terms = ("logo", "icon", "avatar", "flag", "placeholder")

    for img in main.find_all("img"):
        image_url = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original")
            or img.get("data-original-src")
        )
        if not image_url and img.get("srcset"):
            srcset = []
            for item in img["srcset"].split(","):
                parts = item.strip().split()
                if parts:
                    descriptor = parts[1] if len(parts) > 1 else "0w"
                    value = float(descriptor[:-1]) if descriptor[:-1].replace(".", "", 1).isdigit() else 0
                    srcset.append((value, parts[0]))
            if srcset:
                image_url = max(srcset)[1]
        if not image_url:
            continue

        context = [img.get("alt", ""), " ".join(img.get("class", [])), img.get("id", "")]
        parent = img.parent
        for _ in range(3):
            if not parent or getattr(parent, "name", None) in ("main", "article", "body"):
                break
            context.extend([" ".join(parent.get("class", [])), parent.get("id", "")])
            parent = parent.parent
        candidates.append((image_url, " ".join(part for part in context if part)))

    ranked = []
    for image_url, context in candidates:
        absolute_url = urljoin(base_url, image_url)
        parsed_image = urlparse(absolute_url)
        if parsed_image.scheme not in ("http", "https"):
            continue

        searchable = f"{context} {parsed_image.path}".lower()
        if any(term in searchable for term in rejected_terms):
            continue

        tokens = set(re.findall(r"[a-z0-9]+", searchable))
        score = len(title_tokens & tokens) * 5
        score += sum(term in searchable for term in preferred_terms) * 3
        ranked.append((score, absolute_url))

    return max(ranked, default=(0, None), key=lambda candidate: candidate[0])[1]


def scrape_project_image(detail_page_url: str) -> Optional[str]:
    """Extract the most project-specific image available from a detail page."""
    try:
        response = requests.get(detail_page_url, headers=HEADERS, timeout=25)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        heading = soup.find("h1")
        title = heading.get_text(" ", strip=True) if heading else ""
        return _select_project_image(soup, response.url, title)
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

        # 2. Prefer images tied to this project over generic social-share images.
        details["image_url"] = _select_project_image(soup, response.url, details["title"]) or ""

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


def extract_website_branding(homepage_html: str, base_url: str) -> Dict[str, Any]:
    """Extract a company's logo URL and basic visual theme from its homepage."""
    soup = BeautifulSoup(homepage_html, "html.parser")
    logo_candidates = []

    for tag in soup.find_all(["meta", "link"]):
        identity = " ".join([
            str(tag.get("property", "")),
            str(tag.get("name", "")),
            " ".join(tag.get("rel", [])) if isinstance(tag.get("rel"), list) else str(tag.get("rel", "")),
        ]).lower()
        if "logo" in identity:
            image_url = tag.get("content") or tag.get("href")
            if image_url:
                logo_candidates.append((10, urljoin(base_url, image_url.strip())))

    for image in soup.find_all("img"):
        image_url = (
            image.get("src") or image.get("data-src") or image.get("data-lazy-src")
            or image.get("data-original")
        )
        if not image_url:
            continue

        context = " ".join([
            str(image.get("alt", "")),
            str(image.get("title", "")),
            " ".join(image.get("class", [])),
            str(image.get("id", "")),
            str(image_url),
        ]).lower()
        parent = image.parent
        in_header = False
        for _ in range(4):
            if not parent or getattr(parent, "name", None) in ("body", "html"):
                break
            parent_context = " ".join([
                str(getattr(parent, "name", "")),
                " ".join(parent.get("class", [])),
                str(parent.get("id", "")),
            ]).lower()
            in_header = in_header or any(term in parent_context for term in ("header", "navbar", "navigation"))
            context += f" {parent_context}"
            parent = parent.parent

        if any(term in context for term in ("icon", "avatar", "flag")):
            continue
        score = sum(term in context for term in ("logo", "brand", "wordmark")) * 10
        if in_header:
            score += 3
        if score:
            logo_candidates.append((score, urljoin(base_url, image_url.strip())))

    logo_url = max(logo_candidates, default=(0, ""), key=lambda item: item[0])[1]
    favicon = soup.find("link", rel=lambda value: value and "icon" in value)
    favicon_url = urljoin(base_url, favicon.get("href", "").strip()) if favicon and favicon.get("href") else ""

    css_text = " ".join(tag.get_text(" ", strip=True) for tag in soup.find_all("style"))
    css_text += " " + " ".join(tag.get("style", "") for tag in soup.find_all(style=True))
    base_domain = urlparse(base_url).netloc.lower()
    stylesheet_links = soup.find_all(
        "link",
        rel=lambda value: value and "stylesheet" in value,
        href=True,
    )
    for link in stylesheet_links[:3]:
        stylesheet_url = urljoin(base_url, link["href"].strip())
        if urlparse(stylesheet_url).netloc.lower() != base_domain:
            continue
        try:
            stylesheet_response = requests.get(
                stylesheet_url,
                headers=HEADERS,
                timeout=5,
            )
            stylesheet_response.raise_for_status()
            css_text += " " + stylesheet_response.text
        except requests.RequestException:
            logger.debug("Could not retrieve website stylesheet %s", stylesheet_url)

    color_pattern = r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)|\b(?:black|white|gray|grey|red|blue|green|navy|teal|silver|transparent)\b"
    colors = {}
    palette = []

    for variable, value in re.findall(
        rf"--([\w-]+)\s*:\s*({color_pattern})", css_text, re.IGNORECASE,
    ):
        variable = variable.lower()
        value = value.strip()
        if value.lower() not in {color.lower() for color in palette}:
            palette.append(value)
        if "primary" in variable:
            colors.setdefault("primary", value)
        elif "secondary" in variable:
            colors.setdefault("secondary", value)
        elif "accent" in variable:
            colors.setdefault("accent", value)
        elif "brand" in variable:
            colors.setdefault("primary", value)
        elif "background" in variable or variable in ("bg", "surface"):
            colors.setdefault("background", value)
        elif "text" in variable or "foreground" in variable:
            colors.setdefault("text", value)

    for value in re.findall(rf"(?:theme-color|background-color|color)\s*[=:]\s*({color_pattern})", homepage_html, re.IGNORECASE):
        if value.lower() not in {color.lower() for color in palette}:
            palette.append(value)

    theme_color = soup.find("meta", attrs={"name": "theme-color"})
    if theme_color and theme_color.get("content"):
        colors.setdefault("primary", theme_color["content"].strip())

    fonts = []
    for declaration in re.findall(r"font-family\s*:\s*([^;}]+)", css_text, re.IGNORECASE):
        for font in declaration.split(","):
            font = font.strip().strip("\"'")
            if font and font.lower() not in {existing.lower() for existing in fonts}:
                fonts.append(font)

    return {
        "logo_url": logo_url,
        "favicon_url": favicon_url,
        "theme": {
            "colors": colors,
            "palette": palette[:12],
            "fonts": fonts[:5],
        },
    }


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
        website_branding = extract_website_branding(homepage_html, base_url)
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
        
        catalog_urls = _candidate_project_urls_from_content(homepage_content, normalized_website)

        print(f"catalog_urls: {catalog_urls}")

        combined_content = homepage_content

        for cat_url in catalog_urls:
            pages_to_scrape = [cat_url]
            visited_catalog_pages = set()
            while pages_to_scrape and len(visited_catalog_pages) < MAX_PROJECT_CATALOG_PAGES:
                page_url = pages_to_scrape.pop(0)
                if page_url in visited_catalog_pages:
                    continue
                visited_catalog_pages.add(page_url)
                try:
                    logger.info("Scraping project catalog page: %s", page_url)
                    cat_resp = requests.get(page_url, headers=HEADERS, timeout=30)
                    cat_resp.raise_for_status()

                    cat_base_url = str(cat_resp.url) if getattr(cat_resp, "url", None) and isinstance(cat_resp.url, str) else page_url
                    if urlparse(cat_base_url).netloc.lower() != urlparse(normalized_website).netloc.lower():
                        logger.warning("Skipping off-site catalog redirect: %s", cat_base_url)
                        continue

                    cat_soup = BeautifulSoup(cat_resp.text, "html.parser")
                    for next_page in _pagination_page_urls(cat_soup, cat_base_url):
                        if next_page not in visited_catalog_pages and next_page not in pages_to_scrape:
                            pages_to_scrape.append(next_page)

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
                    combined_content += f"\n\n--- CATALOG PAGE ({page_url}) ---\n" + cat_content
                except Exception as exc:
                    logger.warning("Could not scrape project catalog page %s: %s", page_url, exc)

        # 1. AI-based project selection: use homepage candidates, then validate each candidate detail page.
        projects = _ai_select_homepage_project_cards(
            normalized_website,
            combined_content,
            catalog_urls,
        )

        # 2. DOM-based extraction acts as fallback if AI returns nothing or fails
        if not projects:
            logger.info("AI returned 0 projects. Falling back to DOM extraction.")
   
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
            "website_branding": website_branding,
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
