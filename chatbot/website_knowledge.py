"""Turn a company's public website into one knowledge-base document.

A company gives us its website at signup. We read a handful of its pages, keep
the readable text and file it in the knowledge base as a single domain-named
Word document, so the chatbot and the calling agent can answer from it.
"""

from __future__ import annotations

import heapq
import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from io import BytesIO
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString
from django.core.files.base import ContentFile
from django.utils import timezone
from docx import Document

from chatbot.models import KnowledgeBaseDocument

logger = logging.getLogger(__name__)

# How the document appears in the knowledge base, and the file it is stored as.
WEBSITE_DOCUMENT_NAME = 'Website Data.docx'


def website_document_filename(website: str, version_id: int | None = None) -> str:
    """Build a readable, unique filename for one website import version."""
    host = (urlparse(normalize_website_url(website)).hostname or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    site_name = re.sub(r'[^a-z0-9]+', '_', host.split('.')[0]).strip('_') or 'website'
    version = f'_{version_id}' if version_id is not None else ''
    return f'Website_Data_{site_name}{version}.docx'


# Crawl limits. Scaled up to cover full company sites and all projects.
MAX_PAGES = 100
MAX_FETCHES = MAX_PAGES * 3
MAX_PAGE_BYTES = 2_000_000
MAX_DOCUMENT_CHARS = 1_500_000
MIN_DOCUMENT_CHARS = 200
MAX_REDIRECTS = 5
REQUEST_TIMEOUT_SECONDS = 15
CRAWL_BUDGET_SECONDS = 300

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/131.0.0.0 Safari/537.36'
    )
}

_NON_CONTENT_TAGS = ('script', 'style', 'noscript', 'svg', 'iframe', 'template', 'canvas')
# Elements that start a new line of text. Table cells are left out on purpose
# so that a table row ("1-bed", "from 139,750") reads as one line.
_BLOCK_TAGS = (
    'address', 'article', 'aside', 'blockquote', 'br', 'dd', 'div', 'dl', 'dt',
    'fieldset', 'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3',
    'h4', 'h5', 'h6', 'header', 'hr', 'li', 'main', 'nav', 'ol', 'p', 'pre',
    'section', 'table', 'tr', 'ul',
)

_SKIP_EXTENSIONS = (
    '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico', '.css',
    '.js', '.json', '.xml', '.zip', '.rar', '.mp4', '.mp3', '.avi', '.mov',
    '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.woff', '.woff2', '.ttf',
)
# Whole path segments that never hold company information.
_SKIP_SEGMENTS = frozenset({
    'login', 'log-in', 'signin', 'sign-in', 'signup', 'sign-up', 'register',
    'logout', 'cart', 'checkout', 'account', 'my-account', 'wp-admin',
    'wp-login.php', 'wp-json', 'feed', 'search', 'tag', 'author',
    'privacy', 'privacy-policy', 'terms', 'terms-and-conditions',
    'terms-conditions', 'cookies', 'cookie-policy',
})
# Pages worth reading first when the site has more than MAX_PAGES.
_PRIORITY_WORDS = (
    'about', 'contact', 'service', 'project', 'development', 'propert',
    'listing', 'communit', 'location', 'team', 'faq', 'pricing', 'price',
    'invest', 'payment', 'offer', 'why', 'who-we-are', 'our-story',
)


class WebsiteImportError(Exception):
    """The website could not be read. The message is safe to show the user."""


@dataclass
class WebsitePage:
    url: str
    title: str
    description: str
    lines: list[str]


@dataclass
class WebsiteContent:
    website: str
    pages: list[WebsitePage] = field(default_factory=list)

    def as_text(self) -> str:
        """The site as one plain-text document, capped in size."""
        parts = [
            f'Website: {self.website}',
            f'Imported on: {timezone.now():%Y-%m-%d}',
            f'Pages read: {len(self.pages)}',
        ]
        for page in self.pages:
            parts += ['', f'==== {page.title or page.url} ====', f'URL: {page.url}']
            if page.description:
                parts.append(f'Summary: {page.description}')
            parts.append('')
            parts.extend(page.lines)
        return '\n'.join(parts)[:MAX_DOCUMENT_CHARS]


def website_document_docx_bytes(content: WebsiteContent) -> bytes:
    """Return the same scraped website content as a DOCX file."""
    doc = Document()
    doc.add_heading('Website Data', level=1)
    doc.add_paragraph(f'Website: {content.website}')

    for page in content.pages:
        doc.add_heading(page.title or page.url or 'Website Page', level=2)
        if page.description:
            doc.add_paragraph(page.description)
        if page.url:
            doc.add_paragraph(f'URL: {page.url}')
        for line in page.lines:
            if line.strip():
                doc.add_paragraph(line)

    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def save_website_document_versions(doc: KnowledgeBaseDocument, content: WebsiteContent, text: str) -> None:
    """Persist the scraped site as a DOCX document only."""
    filename = doc.original_filename or website_document_filename(content.website, doc.pk)
    if doc.file:
        doc.file.delete(save=False)

    doc.file.save(filename, ContentFile(website_document_docx_bytes(content)), save=False)
    doc.original_filename = filename
    doc.file_type = 'docx'
    doc.save(update_fields=['original_filename', 'file_type', 'updated_at'])


# ── URLs ────────────────────────────────────────────────────────────────────

def normalize_website_url(raw: str | None) -> str:
    """Return a clean http(s) URL, or '' when `raw` is not a usable website."""
    value = (raw or '').strip().strip('"\'')
    if not value:
        return ''
    if not value.lower().startswith(('http://', 'https://')):
        value = f'https://{value}'
    try:
        parsed = urlparse(value)
        host = parsed.hostname or ''
    except ValueError:
        return ''
    if parsed.scheme.lower() not in ('http', 'https') or '.' not in host:
        return ''
    return value


def is_public_url(url: str) -> bool:
    """True when `url` is plain http(s) on a default port and resolves only to
    public addresses.

    The crawler fetches addresses supplied by users, so anything that points at
    this server, the private network or the cloud metadata service is refused.
    """
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return False
    if port not in (None, 80, 443):
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
        addresses = {info[4][0].split('%')[0] for info in infos}
        return bool(addresses) and all(
            ipaddress.ip_address(address).is_global for address in addresses
        )
    except (OSError, ValueError):
        return False


def _site_key(url: str) -> str:
    """Host without `www.`, used to keep the crawl on the company's own site."""
    host = (urlparse(url).hostname or '').lower()
    return host[4:] if host.startswith('www.') else host


def _url_key(url: str) -> str:
    """Identity of a page, ignoring scheme, `www.` and a trailing slash."""
    parsed = urlparse(url)
    query = f'?{parsed.query}' if parsed.query else ''
    return f'{_site_key(url)}{parsed.path.rstrip("/")}{query}'


def _is_crawlable(link: str, site: str) -> bool:
    try:
        parsed = urlparse(link)
    except ValueError:
        return False
    if parsed.scheme not in ('http', 'https') or _site_key(link) != site:
        return False
    path = parsed.path.lower()
    if path.endswith(_SKIP_EXTENSIONS):
        return False
    return _SKIP_SEGMENTS.isdisjoint(segment for segment in path.split('/') if segment)


def _link_rank(link: str) -> int:
    """Lower is read sooner: useful-looking pages first, then shallow ones."""
    path = urlparse(link).path.lower()
    depth = len([segment for segment in path.split('/') if segment])
    wanted = any(word in path for word in _PRIORITY_WORDS)
    return (0 if wanted else 10) + min(depth, 9)


# ── Fetching and reading ────────────────────────────────────────────────────

def fetch_page(url: str) -> tuple[str, bytes | None]:
    """Fetch one page and return `(final_url, html)`.

    `html` is None when the address does not serve an HTML page. Redirects are
    followed by hand so that every hop is checked by `is_public_url`.
    """
    for _ in range(MAX_REDIRECTS + 1):
        if not is_public_url(url):
            raise WebsiteImportError('This website address cannot be reached.')
        with requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
            stream=True,
        ) as response:
            location = response.headers.get('Location')
            if response.status_code in (301, 302, 303, 307, 308) and location:
                url = urljoin(url, location)
                continue
            response.raise_for_status()
            if 'html' not in response.headers.get('Content-Type', '').lower():
                return url, None
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                body.extend(chunk)
                if len(body) >= MAX_PAGE_BYTES:
                    break
            return url, bytes(body)
    raise WebsiteImportError('The website redirected too many times.')


def _text_lines(root) -> list[str]:
    """The text of `root`, one line per block element.

    Inline pieces (a name split across <span>s, a link inside a sentence) stay
    on the same line, which keeps sentences whole for search.
    """
    # Line breaks in the HTML source mean nothing; only block elements do.
    for node in root.find_all(string=True):
        if type(node) is NavigableString:
            node.replace_with(' '.join(node.split()) or ' ')
    for tag in root.find_all(_BLOCK_TAGS):
        tag.insert_before('\n')
        tag.insert_after('\n')
    return [' '.join(line.split()) for line in root.get_text(' ').split('\n')]


def _read_page(
    url: str, html: bytes, seen_lines: set[str],
) -> tuple[WebsitePage | None, list[str]]:
    """Return the page's text and its links.

    Only lines not already seen on an earlier page are kept, so the menu and
    footer repeated on every page appear once in the document.
    """
    soup = BeautifulSoup(html, 'html.parser')

    links = []
    for anchor in soup.find_all('a', href=True):
        try:
            links.append(urldefrag(urljoin(url, anchor['href'].strip()))[0])
        except ValueError:
            continue

    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    meta = soup.find('meta', attrs={'name': 'description'})
    description = ' '.join((meta.get('content') or '').split()) if meta else ''

    for tag in soup(_NON_CONTENT_TAGS):
        tag.decompose()

    lines = []
    for line in _text_lines(soup.body or soup):
        key = line.casefold()
        if len(line) < 2 or key in seen_lines:
            continue
        seen_lines.add(key)
        lines.append(line)

    if not lines:
        return None, links
    return WebsitePage(url=url, title=title, description=description, lines=lines), links


def crawl_website(website: str) -> WebsiteContent:
    """Read the homepage and the most useful pages of the same site.

    Raises `WebsiteImportError` when the site cannot be opened or holds no
    readable text (for example a site that only renders in the browser).
    """
    start_url = normalize_website_url(website)
    if not start_url:
        raise WebsiteImportError('The website address is not valid.')

    deadline = time.monotonic() + CRAWL_BUDGET_SECONDS
    try:
        home_url, home_html = fetch_page(start_url)
    except requests.RequestException as exc:
        raise WebsiteImportError('The website could not be opened.') from exc
    if home_html is None:
        raise WebsiteImportError('The website did not return a web page.')

    site = _site_key(home_url)
    content = WebsiteContent(website=home_url)
    seen_lines: set[str] = set()
    queued = {_url_key(start_url), _url_key(home_url)}
    read: set[str] = set()
    candidates: list[tuple[int, int, str]] = []
    chars = 0

    def take(url: str, html: bytes) -> None:
        nonlocal chars
        read.add(_url_key(url))
        page, links = _read_page(url, html, seen_lines)
        if page is not None:
            content.pages.append(page)
            chars += sum(len(line) for line in page.lines)
        for link in links:
            key = _url_key(link) if _is_crawlable(link, site) else None
            if key is None or key in queued:
                continue
            queued.add(key)
            heapq.heappush(candidates, (_link_rank(link), len(queued), link))

    take(home_url, home_html)

    fetches = 1
    while (
        candidates
        and len(content.pages) < MAX_PAGES
        and fetches < MAX_FETCHES
        and chars < MAX_DOCUMENT_CHARS
        and time.monotonic() < deadline
    ):
        _, _, url = heapq.heappop(candidates)
        fetches += 1
        try:
            final_url, html = fetch_page(url)
        except (requests.RequestException, WebsiteImportError):
            logger.info('Website import: skipped %s', url)
            continue
        # A page that redirected off the site, or to one already read, adds nothing.
        if html is None or _site_key(final_url) != site or _url_key(final_url) in read:
            continue
        take(final_url, html)

    if chars < MIN_DOCUMENT_CHARS:
        raise WebsiteImportError('No readable text was found on the website.')
    return content


# ── The knowledge-base document ─────────────────────────────────────────────

def website_document(company) -> KnowledgeBaseDocument | None:
    """The company's website document, if it has one."""
    return (
        KnowledgeBaseDocument.objects
        .filter(user__company=company, source=KnowledgeBaseDocument.Source.WEBSITE)
        .order_by('-pk')
        .first()
    )


def prepare_website_document(company, website: str, owner=None) -> KnowledgeBaseDocument | None:
    """Create the company's website document, or reset the one it has, so it
    shows as pending while the import runs.

    Each import gets a separate document, except that a pending document made
    by the API is reused by its queued task. It is filed under `owner` when
    given, otherwise under the company admin.
    """
    latest_doc = website_document(company)
    reusable_statuses = (
        KnowledgeBaseDocument.Status.PENDING,
        KnowledgeBaseDocument.Status.PROCESSING,
    )
    doc = latest_doc if (
        latest_doc
        and latest_doc.status in reusable_statuses
        and latest_doc.source_url == website
    ) else None
    is_new = doc is None

    if is_new:
        owner = owner or (
            company.users.filter(role='company_admin').order_by('pk').first()
            or company.users.order_by('pk').first()
        )
        if owner is None:
            return None
        doc = KnowledgeBaseDocument(
            user=owner,
            source=KnowledgeBaseDocument.Source.WEBSITE,
            original_filename=website_document_filename(website),
            file_type='docx',
        )
    doc.source_url = website
    doc.status = KnowledgeBaseDocument.Status.PENDING
    doc.error = ''
    doc.save()
    if is_new:
        doc.original_filename = website_document_filename(website, doc.pk)
        doc.save(update_fields=['original_filename', 'updated_at'])
    return doc
