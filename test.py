import os
import sys
import time
import logging
from urllib.parse import urlparse

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'api.settings')
import django
django.setup()

# Configure comprehensive logging to standard output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger("debug_knowledge_scraper")
logger.setLevel(logging.DEBUG)
logging.getLogger("chatbot.website_knowledge").setLevel(logging.DEBUG)
logging.getLogger("chatbot.tasks").setLevel(logging.DEBUG)


def debug_website_knowledge_flow(website_url: str, company_id: int | None = None, save_to_db: bool = True):
    from chatbot.website_knowledge import (
        normalize_website_url,
        crawl_website,
        prepare_website_document,
        WebsiteImportError,
        WEBSITE_DOCUMENT_FILE,
        MAX_DOCUMENT_CHARS,
    )
    from chatbot.models import KnowledgeBaseDocument
    from chatbot.pinecone_service import PineconeService, CHUNK_SIZE, CHUNK_OVERLAP, PineconeServiceError
    from django.core.files.base import ContentFile
    from users.models import Company, User
    from projects.models import Project
    from users.website_scraper import scrape_company_website
    from projects.tasks import import_scraped_proposal_image_task

    print("\n" + "=" * 75)
    print(f"🚀 STARTING COMPLETE SCRAPING FLOW: KNOWLEDGE BASE + REAL ESTATE PROJECTS")
    print(f"🌐 Target Website: {website_url}")
    print("=" * 75 + "\n")

    # Step 1: Normalize URL
    logger.info("👉 Step 1: Normalizing website address...")
    normalized = normalize_website_url(website_url)
    if not normalized:
        logger.error("❌ Invalid website URL provided: %s", website_url)
        return

    logger.info("✅ Normalized URL: %s", normalized)

    # Resolve company
    company = None
    if company_id:
        company = Company.objects.filter(pk=company_id).first()
        if not company:
            logger.warning("⚠️ Company with ID %s not found. Proceeding with fallback.", company_id)
    if not company:
        company = Company.objects.first()
        if company:
            logger.info("ℹ️ Using default active company: '%s' (ID: %s)", company.name, company.pk)
        else:
            logger.warning("⚠️ No companies found in database.")

    # ──────────────────────────────────────────────────────────────────────────
    # PART A: KNOWLEDGE BASE CRAWLING (ALL PAGES)
    # ──────────────────────────────────────────────────────────────────────────
    logger.info("\n👉 Part A: Crawling ALL website pages for AI Knowledge Base (up to 100 pages)...")
    start_time = time.monotonic()
    try:
        content = crawl_website(normalized)
    except WebsiteImportError as exc:
        logger.error("❌ Crawling failed: %s", exc)
        return
    except Exception as exc:
        logger.exception("❌ Unexpected error during crawl: %s", exc)
        return

    duration = time.monotonic() - start_time
    logger.info(
        "✅ Crawling completed in %.2f seconds! Total pages crawled: %d",
        duration, len(content.pages)
    )

    print("\n" + "-" * 75)
    print(f"📑 CRAWLED PAGES SUMMARY (Total: {len(content.pages)} pages):")
    print("-" * 75)
    for i, page in enumerate(content.pages, 1):
        line_count = len(page.lines)
        char_count = sum(len(line) for line in page.lines)
        print(f"[{i:02d}] {page.title or 'No Title'}")
        print(f"     URL: {page.url}")
        print(f"     Stats: {line_count} text blocks | {char_count:,} characters")
        if page.description:
            print(f"     Description: {page.description[:120]}...")
        print("-" * 75)

    text = content.as_text()
    total_chars = len(text)
    total_words = len(text.split())
    logger.info(
        "📄 Consolidated Document generated: %d total characters (~%d words).",
        total_chars, total_words
    )

    if save_to_db and company:
        logger.info("\n👉 Updating KnowledgeBaseDocument ('Website Data.txt') in database and S3...")
        owner = company.users.filter(role='company_admin').first() or company.users.first()
        doc = prepare_website_document(company, normalized, owner=owner)
        if doc:
            doc.status = KnowledgeBaseDocument.Status.PROCESSING
            doc.save(update_fields=["status", "updated_at"])

            if doc.file:
                doc.file.delete(save=False)
            doc.file.save(WEBSITE_DOCUMENT_FILE, ContentFile(text.encode("utf-8")), save=False)
            try:
                doc.hosted_url = doc.file.url
            except Exception:
                doc.hosted_url = ""
            doc.source_url = content.website
            doc.save(update_fields=["file", "hosted_url", "source_url", "updated_at"])
            logger.info("✅ Saved raw document to storage: %s", doc.hosted_url or WEBSITE_DOCUMENT_FILE)

            # Pinecone Vector Embeddings & Indexing
            logger.info("👉 Upserting vector embeddings to Pinecone...")
            pinecone_service = PineconeService()
            try:
                chunks_stored = pinecone_service.upsert_document(
                    document_id=doc.pk,
                    text=text,
                    metadata={
                        "user_id": doc.user_id,
                        "company_id": company.pk,
                        "original_filename": doc.original_filename,
                        "file_type": doc.file_type,
                        "associated_country": doc.associated_country,
                        "source": KnowledgeBaseDocument.Source.WEBSITE.value,
                        "source_url": content.website,
                    },
                )
                logger.info("✅ Stored %d vector chunks in Pinecone for Document ID %s.", chunks_stored, doc.pk)

                max_chunks = MAX_DOCUMENT_CHARS // (CHUNK_SIZE - CHUNK_OVERLAP) + 1
                try:
                    pinecone_service.delete_document_chunks(doc.pk, chunks_stored, max_chunks)
                except PineconeServiceError:
                    pass

                doc.status = KnowledgeBaseDocument.Status.COMPLETED
                doc.save(update_fields=["status", "updated_at"])
                logger.info("🎉 Knowledge Base updated! Chatbot & Calling Agent now have all %d pages.", len(content.pages))
            except Exception as exc:
                doc.status = KnowledgeBaseDocument.Status.FAILED
                doc.save(update_fields=["status", "updated_at"])
                logger.exception("❌ Error during Pinecone vector indexing: %s", exc)

    # ──────────────────────────────────────────────────────────────────────────
    # PART B: REAL ESTATE PROJECT EXTRACTION & CREATION
    # ──────────────────────────────────────────────────────────────────────────
    print("\n" + "=" * 75)
    print("🏗️ PART B: EXTRACTING REAL ESTATE PROJECTS & DOWNLOADING INTERIOR IMAGES")
    print("=" * 75 + "\n")

    logger.info("👉 Scanning website for genuine real estate development projects...")
    report = scrape_company_website(normalized)
    projects_found = report.get("projects", [])
    logger.info("✅ Discovered %d real estate projects from website!", len(projects_found))

    if not projects_found:
        logger.warning("No projects could be extracted from the website.")
        return

    company_admin = company.users.filter(role='company_admin').order_by('pk').first() if company else None
    project_creator = company_admin or (company.users.order_by('pk').first() if company else None)

    created_projects = []
    print("\n" + "-" * 75)
    print("🏠 CREATING/UPDATING PROJECTS IN DATABASE:")
    print("-" * 75)

    for idx, sp in enumerate(projects_found, 1):
        title = sp.get("name") or sp.get("title") or f"Project {idx}"
        source_url = sp.get("detail_page_url") or sp.get("url") or normalized
        description = sp.get("description") or ""
        location = sp.get("location") or ""
        developer = sp.get("developer") or ""
        starting_price = sp.get("starting_price") or 0
        currency = sp.get("currency") or "EUR"
        image_url = sp.get("image_url")

        print(f"\n[{idx:02d}] 🏷️  Title: {title}")
        print(f"     📍 Location: {location or 'Not specified'}")
        print(f"     🏢 Developer: {developer or 'Not specified'}")
        print(f"     💰 Starting Price: {currency} {starting_price:,.2f}" if starting_price else "     💰 Starting Price: Price on request")
        print(f"     🔗 Detail Page: {source_url}")
        print(f"     🖼️ Scraped Image URL: {image_url or 'None'}")

        if save_to_db and company:
            project = Project.objects.filter(
                visible_to_companies=company,
                source_url=source_url,
            ).first()

            if project is None:
                project = Project.objects.create(
                    title=title,
                    source_url=source_url,
                    description=description,
                    location=location,
                    developer=developer,
                    starting_price=starting_price,
                    currency=currency,
                    yield_percentage=None,
                    project_type=Project.ProjectType.RESIDENTIAL,
                    status=Project.Status.PLANNED,
                    project_status=Project.ProjectStatus.DRAFT,
                    created_by=project_creator,
                )
                action = "CREATED"
            else:
                fields_to_update = []
                if project.title != title:
                    project.title = title
                    fields_to_update.append('title')
                if not project.description and description:
                    project.description = description
                    fields_to_update.append('description')
                if not project.location and location:
                    project.location = location
                    fields_to_update.append('location')
                if not project.developer and developer:
                    project.developer = developer
                    fields_to_update.append('developer')
                if (not project.starting_price or project.starting_price == 0) and starting_price:
                    project.starting_price = starting_price
                    fields_to_update.append('starting_price')
                if not project.currency and currency:
                    project.currency = currency
                    fields_to_update.append('currency')
                if fields_to_update:
                    project.save(update_fields=fields_to_update)
                action = "UPDATED"

            project.visible_to_companies.add(company)

            # Download image and attach to EXTERIOR slot
            if image_url and image_url.startswith(('http://', 'https://')):
                logger.info("     ⬇️ Downloading image and storing into 'exterior' proposal image slot...")
                try:
                    # Run image import inline (synchronously) so user sees the result immediately
                    img_result = import_scraped_proposal_image_task.run(project.pk, image_url, 'exterior')
                    if img_result.get('success'):
                        project.refresh_from_db()
                        exterior_paths = project.proposal_images.get('exterior', [])
                        print(f"     ✅ Attached to EXTERIOR proposal images: {exterior_paths}")
                    else:
                        print(f"     ⚠️ Image download notice: {img_result.get('error')}")
                except Exception as exc:
                    logger.warning("     ⚠️ Could not download image: %s", exc)

            created_projects.append(project)
            print(f"     ✨ Database Record {action}: ID={project.pk} (Visible to '{company.name}')")

    print("\n" + "=" * 75)
    print(f"🎯 ALL TASKS COMPLETED SUCCESSFULLY!")
    print(f"   • Total Pages in Knowledge Base: {len(content.pages)}")
    print(f"   • Total Real Estate Projects Processed: {len(created_projects or projects_found)}")
    print("=" * 75 + "\n")


if __name__ == "__main__":
    target_url = sys.argv[1] if len(sys.argv) > 1 else None

    if not target_url:
        print("\n=== Real Estate AI Scraper & Knowledge Base Debugger ===")
        user_input = input("Enter website URL (or press Enter for default 'https://www.northbucharestinvestments.ro/'): ").strip()
        target_url = user_input or "https://www.northbucharestinvestments.ro/"

    debug_website_knowledge_flow(target_url)
