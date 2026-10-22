from __future__ import annotations
import logging
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from drf_yasg.utils import swagger_auto_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

_SOURCE_URL = "https://www.khaleejtimes.com/business/property"

_GULF_NEWS_SOURCE_URL = "https://gulfnews.com/business/property"

_CNBC_REAL_ESTATE_URL = "https://www.cnbc.com/real-estate/"

_ARTICLE_BOX_CLASS = "subsection-listing-article-box"

_COUNTRY_SOURCE_MAP = {
    "us": "cnbc"
}

_SOURCE_URL_BY_KEY = {
    "cnbc": _CNBC_REAL_ESTATE_URL,
    "khaleejtimes": _SOURCE_URL,
    "gulfnews": _GULF_NEWS_SOURCE_URL,
    "propertywire": "https://www.propertywire.com/category/news/uk/",
}

_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _fetch_html(url: str) -> httpx.Response:
    """Fetch a page with browser-like headers. Sends a Referer of the site root
    which helps bypass some basic bot filters."""
    headers = dict(_REQUEST_HEADERS)
    # A same-origin referer looks more like real navigation.
    scheme_host = "/".join(url.split("/")[:3])
    headers["Referer"] = scheme_host + "/"
    return httpx.get(
        url,
        headers=headers,
        timeout=25.0,
        follow_redirects=True,
    )


def _absolute_url(href: str, base_url: str = "https://www.khaleejtimes.com") -> str:
    """Make a relative href absolute against the matching source domain."""
    if not href:
        return ""
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        return f"https:{href}"
    return urljoin(base_url.rstrip("/") + "/", href)


def _parse_cnbc_card(card) -> dict:
    """Extract headline, image and detail-url from a CNBC property card."""
    title_el = card.select_one(".Card-title")
    title = title_el.get_text(" ", strip=True) if title_el else ""

    detail_link = ""
    if title_el and title_el.get("href"):
        detail_link = title_el["href"]
    else:
        anchor = card.select_one("a[href]")
        if anchor:
            detail_link = anchor.get("href", "")
    if detail_link and detail_link.startswith("/"):
        detail_link = urljoin(_CNBC_REAL_ESTATE_URL, detail_link)

    image = ""
    img = card.select_one("img")
    if img:
        image = img.get("src") or img.get("data-src") or img.get("data-original") or ""
        if image and image.startswith("/"):
            image = urljoin(_CNBC_REAL_ESTATE_URL, image)

    text = ""
    byline = card.select_one(".Card-byline")
    if byline:
        text = byline.get_text(" ", strip=True)
    elif title:
        text = title

    return {
        "title": title,
        "link": detail_link,
        "details_link": detail_link,
        "url": detail_link,
        "text": text,
        "image": image,
        "image_url": image,
    }


def _parse_article_box(box) -> dict:
    """Extract title, link, summary and image from a single article box."""
    link_tag = box.find("a", href=True)
    link = _absolute_url(link_tag["href"]) if link_tag else ""

    heading = box.find(["h1", "h2", "h3", "h4"])
    title = heading.get_text(strip=True) if heading else ""
    if not title and link_tag:
        title = link_tag.get_text(strip=True)

    paragraph = box.find("p")
    summary = paragraph.get_text(strip=True) if paragraph else ""

    img_tag = box.find("img")
    image = ""
    if img_tag:
        image = _absolute_url(
            img_tag.get("src")
            or img_tag.get("data-src")
            or img_tag.get("data-original")
            or ""
        )

    return {
        "title": title,
        "link": link,
        "text": summary,
        "image": image,
    }


def _normalize_country(country: str | None) -> str:
    """Normalize a country string to a known key for routing."""
    if not country:
        return ""
    normalized = country.strip().lower().replace("_", " ").replace("-", " ")
    return normalized


def _resolve_country_source(country: str | None) -> str:
    """Return the known source key for the provided country name."""
    normalized = _normalize_country(country)
    return _COUNTRY_SOURCE_MAP.get(normalized, "cnbc")


def _fetch_country_news(country: str | None) -> dict:
    """Fetch property news for a country using the configured source."""
    source_key = _resolve_country_source(country)
    source_url = _SOURCE_URL_BY_KEY.get(source_key, _CNBC_REAL_ESTATE_URL)

    if source_key == "cnbc":
        try:
            response = _fetch_html(source_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Failed to fetch CNBC real-estate news: %s", exc)
            return {"source": source_url, "count": 0, "articles": [], "error": str(exc)}

        soup = BeautifulSoup(response.text, "html.parser")
        cards = soup.select("div.Card-standardBreakerCard") or soup.select("article")
        articles = []
        for card in cards[1:2]:
            parsed = _parse_cnbc_card(card)
            if parsed["title"] or parsed["link"] or parsed["image"]:
                articles.append(parsed)
        return {"source": source_url, "count": len(articles), "articles": articles}

    if source_key == "khaleejtimes":
        try:
            response = _fetch_html(_SOURCE_URL)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Failed to fetch Khaleej Times property news: %s", exc)
            return {"source": _SOURCE_URL, "count": 0, "articles": [], "error": str(exc)}

        soup = BeautifulSoup(response.text, "html.parser")
        boxes = soup.find_all(class_=_ARTICLE_BOX_CLASS)
        articles = [_parse_article_box(box) for box in boxes]
        articles = [a for a in articles if a["title"] or a["link"] or a["text"]]
        return {"source": _SOURCE_URL, "count": len(articles), "articles": articles}

    if source_key == "gulfnews":
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.google.com/",
            "Connection": "keep-alive",
        }
        try:
            response = httpx.get(
                _GULF_NEWS_SOURCE_URL,
                headers=headers,
                timeout=30,
                follow_redirects=True,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.exception(exc)
            return {"source": _GULF_NEWS_SOURCE_URL, "count": 0, "articles": [], "error": str(exc)}

        soup = BeautifulSoup(response.text, "html.parser")
        article = soup.find("div", class_="eJkNT")
        if article is None:
            return {"source": _GULF_NEWS_SOURCE_URL, "count": 0, "articles": []}

        title = ""
        heading = article.find("h1")
        if heading:
            title = heading.get_text(" ", strip=True)

        text = ""
        p = article.find("h2")
        if p:
            text = p.get_text(" ", strip=True)

        link = ""
        a = article.find("a", href=True)
        if a:
            href = a["href"]
            link = href if href.startswith("http") else f"https://gulfnews.com{href}"

        image = ""
        img = article.find("img")
        if img:
            image = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or img.get("data-original") or ""
            if image.startswith("//"):
                image = "https:" + image
            elif image.startswith("/"):
                image = "https://gulfnews.com" + image

        result = {
            "title": title,
            "text": text,
            "image": image,
            "link": link,
            "details_link": link,
            "url": link,
        }
        return {"source": _GULF_NEWS_SOURCE_URL, "count": 1, "articles": [result]}

    try:
        response = _fetch_html(source_url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch Property Wire news: %s", exc)
        return {"source": source_url, "count": 0, "articles": [], "error": str(exc)}

    soup = BeautifulSoup(response.text, "html.parser")
    featured_heading = soup.select_one("article h3.text-32")
    cards = [featured_heading.find_parent("article")] if featured_heading else soup.find_all("article")
    article = None
    for card in cards:
        parsed = _parse_property_wire_card(card)
        if parsed["title"] and parsed["link"]:
            article = parsed
            break
    if article is None:
        return {"source": source_url, "count": 0, "articles": []}
    return {"source": source_url, "count": 1, "articles": [article]}


class KhaleejTimesPropertyView(APIView):
    """GET the latest property listings scraped from Khaleej Times."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(tags=["Property News"])
    def get(self, request):
        result = _fetch_country_news("uae")
        if result.get("error"):
            return Response({"detail": f"Failed to fetch source page: {result['error']}"}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({
            "source": result["source"],
            "count": result["count"],
            "articles": result["articles"],
        })


class PropertyNewsCountryView(APIView):
    """Return property news for a country through one API route."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(tags=["Property News"])
    def get(self, request, country: str | None = None):
        requested_country = country or request.query_params.get("country") or request.query_params.get("region") or "usa"
        result = _fetch_country_news(requested_country)
        if result.get("error"):
            return Response({"detail": f"Failed to fetch source page: {result['error']}"}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({
            "country": requested_country,
            "source": result["source"],
            "count": result["count"],
            "articles": result["articles"],
        })


# Arabian Business — Real Estate

class GulfNewsPropertyView(APIView):
    """
    Fetch first property article from Gulf News.
    """

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(tags=["Property News"])
    def get(self, request):

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.google.com/",
            "Connection": "keep-alive",
        }

        try:
            response = httpx.get(
                _GULF_NEWS_SOURCE_URL,
                headers=headers,
                timeout=30,
                follow_redirects=True,
            )
            response.raise_for_status()

        except Exception as e:
            logger.exception(e)
            return Response(
                {
                    "detail": str(e),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        soup = BeautifulSoup(response.text, "html.parser")

        # First article
        article = soup.find("div", class_="eJkNT")

        if article is None:
            return Response(
                {
                    "detail": "Article not found."
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        # Title
        title = ""
        heading = article.find("h1")
        if heading:
            title = heading.get_text(" ", strip=True)

        # Description
        text = ""
        p = article.find("h2")
        if p:
            text = p.get_text(" ", strip=True)

        # URL
        link = ""
        a = article.find("a", href=True)
        if a:
            href = a["href"]
            if href.startswith("http"):
                link = href
            else:
                link = f"https://gulfnews.com{href}"

        # Image
        image = ""
        img = article.find("img")
        if img:
            image = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-lazy-src")
                or img.get("data-original")
                or ""
            )

            if image.startswith("//"):
                image = "https:" + image

            elif image.startswith("/"):
                image = "https://gulfnews.com" + image

        return Response(
            {
                "source": _GULF_NEWS_SOURCE_URL,
                "article": {
                    "title": title,
                    "text": text,
                    "image": image,
                    "link": link,
                },
            }
        )


# Property Wire — UK Property News

_PROPERTY_WIRE_SOURCE_URL = "https://www.propertywire.com/category/news/uk/"


def _parse_property_wire_card(card) -> dict:
    """Extract headline, link, category and image from a Property Wire article card."""
    heading = card.find(["h2", "h3"])
    # Link is either inside the heading (list cards) or wrapping it (featured card).
    heading_link = None
    if heading:
        heading_link = heading.find("a", href=True) or heading.find_parent("a", href=True)

    title = heading.get_text(" ", strip=True) if heading else ""
    link = heading_link["href"] if heading_link else ""

    category_link = card.select_one("span a[href*='/category/']")
    category = category_link.get_text(" ", strip=True) if category_link else ""

    img = card.find("img")
    image = ""
    if img:
        image = img.get("src") or img.get("data-src") or ""
        if image.startswith("//"):
            image = f"https:{image}"

    return {
        "title": title,
        "link": link,
        "image": image,
    }


class PropertyWireView(APIView):
    """GET the first UK property article scraped from Property Wire."""

    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(tags=["Property News"])
    def get(self, request):
        try:
            response = _fetch_html(_PROPERTY_WIRE_SOURCE_URL)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Failed to fetch Property Wire news: %s", exc)
            return Response(
                {"detail": f"Failed to fetch source page: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        soup = BeautifulSoup(response.text, "html.parser")
        # The featured (first) post is the only one with an h3.text-32 headline;
        # fall back to the first article on the page if the markup changes.
        featured_heading = soup.select_one("article h3.text-32")
        cards = [featured_heading.find_parent("article")] if featured_heading else soup.find_all("article")

        article = None
        for card in cards:
            parsed = _parse_property_wire_card(card)
            if parsed["title"] and parsed["link"]:
                article = parsed
                break

        if article is None:
            return Response(
                {"detail": "Article not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response({
            "source": _PROPERTY_WIRE_SOURCE_URL,
            "article": article,
        })