
import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString
from django.conf import settings
from openai import OpenAI


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
}


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

    # Replace links with their text and absolute URLs.
    for a in soup.find_all("a", href=True):
        href = urljoin(response.url, a["href"].strip())
        link_text = a.get_text(" ", strip=True)

        if link_text:
            a.replace_with(
                NavigableString(f"{link_text} [URL: {href}]")
            )
        else:
            a.replace_with(NavigableString(f"[URL: {href}]"))

    return soup.get_text(separator=" ", strip=True)


def _ai_select_homepage_project_cards(website: str, homepage_content: str) -> List[Dict[str, str]]:
    """
    Ask AI to extract project names and their existing detail page URLs.
    """

    client = OpenAI(
        api_key=getattr(settings, "OPENAI_API_KEY", None)
    )

    prompt = f"""
You are an expert real-estate website data extraction assistant.

Extract genuine real-estate development projects from the homepage.

For every project, return:
- name: The displayed project name.
- detail_page_url: Its dedicated project detail page URL.

Rules:
1. Include all identifiable real-estate development projects.
2. Include featured projects and portfolio cards.
3. Prefer dedicated project URLs under /developments/.
4. Exclude contact pages, city guides, blogs, generic navigation,
   company pages and social media links.
5. Use URLs exactly as they appear in the supplied content.
6. Never invent or construct URLs.
7. Only include projects with a directly identifiable detail URL.
8. Remove duplicate projects.
9. Preserve project names as displayed.
10. Return valid JSON only.

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

Homepage content:
{homepage_content}
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "Extract real-estate projects accurately. "
                    "Use only supplied URLs. Never fabricate URLs. "
                    "Return valid JSON only."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        max_tokens=3000,
    )

    raw = response.choices[0].message.content

    if not raw:
        raise ValueError("AI returned an empty response.")

    parsed = json.loads(raw)
    projects = parsed.get("projects", [])

    if not isinstance(projects, list):
        raise ValueError("'projects' must be a JSON list.")

    # Only accept URLs actually present in the scraped homepage.
    source_urls = set(
        re.findall(
            r"\[URL:\s*(https?://[^\s\]]+)\s*\]",
            homepage_content,
        )
    )

    base_domain = urlparse(website).netloc.lower()
    validated_projects = []
    seen = set()

    for project in projects:
        if not isinstance(project, dict):
            continue

        name = project.get("name")
        detail_url = project.get("detail_page_url")

        if not isinstance(name, str) or not isinstance(detail_url, str):
            continue

        name = name.strip()
        detail_url = detail_url.strip()

        if not name or detail_url not in source_urls:
            continue

        parsed_url = urlparse(detail_url)

        if parsed_url.scheme not in ("http", "https"):
            continue

        if parsed_url.netloc.lower() != base_domain:
            continue

        if parsed_url.path.rstrip("/") == "":
            continue

        key = (name.casefold(), detail_url.rstrip("/"))

        if key in seen:
            continue

        seen.add(key)

        validated_projects.append({
            "name": name,
            "detail_page_url": detail_url,
        })

    return validated_projects


def scrape_project_image(detail_page_url: str) -> Optional[str]:
    """
    Extract the project's primary image from its detail page.

    Priority:
    1. Open Graph image.
    2. Twitter image.
    3. Main content image.
    """

    try:
        response = requests.get(
            detail_page_url,
            headers=HEADERS,
            timeout=25,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # Prefer explicit page preview/hero image metadata.
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

        # Fall back to images in the page content.
        main = (
            soup.find("main")
            or soup.find("article")
            or soup.body
            or soup
        )

        for img in main.find_all("img"):
            image_url = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-lazy-src")
                or img.get("data-original")
                or img.get("data-original-src")
            )

            # Use the first image in srcset if src is unavailable.
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

            # Avoid obvious logos, icons and avatars.
            if any(
                word in f"{alt} {image_classes}"
                for word in ("logo", "icon", "avatar", "flag")
            ):
                continue

            return image_url

    except requests.RequestException as exc:
        print(
            f"Could not retrieve project image from "
            f"{detail_page_url}: {exc}"
        )

    return None


def scrape_company_website(website: str) -> Dict[str, Any]:
    """
    Scrape a company homepage, extract projects using AI,
    and retrieve an image URL for each project.
    """

    try:
        website = website.strip()

        if not website.startswith(("https://", "http://")):
            website = "https://" + website

        print(f"Scraping website: {website}")

        homepage_content = scrape_website(website)

        projects = _ai_select_homepage_project_cards(
            website,
            homepage_content,
        )

        for index, project in enumerate(projects, start=1):
            print(
                f"[{index}/{len(projects)}] "
                f"Fetching image for: {project['name']}"
            )

            project["image_url"] = scrape_project_image(
                project["detail_page_url"]
            )

        return {
            "website": website,
            "status": "completed",
            "projects": projects,
            "total_projects": len(projects),
        }

    except Exception as exc:
        return {
            "website": website,
            "status": "failed",
            "projects": [],
            "total_projects": 0,
            "error": str(exc),
        }



