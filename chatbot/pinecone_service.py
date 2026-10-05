"""
Pinecone vector-store service for the chatbot knowledge base.

Responsibilities:
- Hold all Pinecone configuration in one place.
- Embed extracted document text with OpenAI embeddings.
- Chunk long text and upsert vectors (with metadata) into the index.
- Delete a document's vectors when the document is removed.

Designed for efficiency:
- A single, lazily-initialised Pinecone client + index handle is reused.
- Embeddings are requested in batches to minimise round-trips.
- Vectors are upserted in batches to stay within request-size limits.
"""

from __future__ import annotations

import logging
from pinecone import Pinecone
from django.conf import settings
logger = logging.getLogger(__name__)

# Chunking: ~1000 chars per chunk with a small overlap keeps embeddings
# semantically coherent without exploding the vector count.
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150

# Batch sizes for embedding + upsert requests.
EMBED_BATCH = 100
UPSERT_BATCH = 100


class PineconeServiceError(Exception):
    """Raised when a Pinecone operation cannot be completed."""


class PineconeService:
    """Thin wrapper around Pinecone + OpenAI embeddings for the knowledge base."""

    _index = None  # class-level cache so the handle is reused across calls

    def __init__(self) -> None:
        self.api_key = settings.PINECONE_API_KEY
        self.index_name = settings.PINECONE_INDEX_NAME
        self.embedding_model = settings.PINECONE_EMBEDDING_MODEL
        self._dimension = None  # resolved lazily from the live index

        if not self.api_key:
            raise PineconeServiceError("PINECONE_API_KEY is not configured.")
        if not self.index_name:
            raise PineconeServiceError("INDEX_NAME (Pinecone index) is not configured.")

    # ── clients ─────────────────────────────────────────────────────────────

    @property
    def index(self):
        """Lazily create and cache the Pinecone index handle."""
        if PineconeService._index is None:
            PineconeService._index = self._connect_index()
        return PineconeService._index

    def _connect_index(self):
        pc = Pinecone(api_key=self.api_key)
        return pc.Index(self.index_name)

    @property
    def dimension(self) -> int:
        """Read the index's vector dimension once and cache it."""
        if self._dimension is None:
            self._dimension = self.index.describe_index_stats()["dimension"]
        return self._dimension

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts with OpenAI, batching to limit round-trips."""
        from openai import OpenAI

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start:start + EMBED_BATCH]
            resp = client.embeddings.create(
                model=self.embedding_model,
                input=batch,
                dimensions=self.dimension,
            )
            vectors.extend(item.embedding for item in resp.data)
        return vectors

    # ── public API ──────────────────────────────────────────────────────────

    def upsert_document(self, document_id: int, text: str, metadata: dict | None = None) -> int:
        """
        Chunk, embed and upsert the text of a knowledge-base document.

        Vector ids are namespaced as ``doc-<document_id>-<chunk_index>`` so the
        whole document can be deleted later via an id prefix.

        Returns the number of chunks stored.
        """
        chunks = self._chunk_text(text)
        if not chunks:
            logger.info("No text to index for document %s", document_id)
            return 0

        embeddings = self._embed(chunks)
        base_meta = metadata or {}

        vectors = []
        for idx, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            vectors.append({
                "id": f"doc-{document_id}-{idx}",
                "values": embedding,
                "metadata": {
                    **base_meta,
                    "document_id": document_id,
                    "chunk_index": idx,
                    "text": chunk,
                },
            })

        try:
            for start in range(0, len(vectors), UPSERT_BATCH):
                self.index.upsert(vectors=vectors[start:start + UPSERT_BATCH])
        except Exception as exc:  # noqa: BLE001
            raise PineconeServiceError(f"Failed to upsert vectors: {exc}") from exc

        logger.info("Upserted %d chunks for document %s", len(vectors), document_id)
        return len(vectors)

    def delete_document(self, document_id: int) -> None:
        """Delete all vectors belonging to a document."""
        try:
            self.index.delete(filter={"document_id": document_id})
        except Exception as exc:  # noqa: BLE001
            raise PineconeServiceError(f"Failed to delete vectors: {exc}") from exc

    def upsert_project_document(
        self, document_id: int, text: str, metadata: dict | None = None,
    ) -> int:
        """
        Chunk, embed and upsert the text of a ProjectDocument (brochure / floor
        plan / fact sheet) so the chatbot can retrieve it alongside user KB
        documents.

        Vector ids are namespaced as ``project-doc-<document_id>-<chunk_index>``
        and tagged with ``source="project_document"`` in metadata so they can
        be filtered or deleted independently from user uploads.
        """
        chunks = self._chunk_text(text)
        if not chunks:
            logger.info("No text to index for project document %s", document_id)
            return 0

        embeddings = self._embed(chunks)
        base_meta = {**(metadata or {}), "source": "project_document"}

        vectors = []
        for idx, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            vectors.append({
                "id": f"project-doc-{document_id}-{idx}",
                "values": embedding,
                "metadata": {
                    **base_meta,
                    "project_document_id": document_id,
                    "chunk_index": idx,
                    "text": chunk,
                },
            })

        try:
            # Replace any previous vectors for this document first so re-uploads
            # don't leave stale chunks behind.
            self.index.delete(filter={"project_document_id": document_id})
        except Exception:  # noqa: BLE001
            # Non-fatal — proceed with the upsert either way.
            pass

        try:
            for start in range(0, len(vectors), UPSERT_BATCH):
                self.index.upsert(vectors=vectors[start:start + UPSERT_BATCH])
        except Exception as exc:  # noqa: BLE001
            raise PineconeServiceError(f"Failed to upsert vectors: {exc}") from exc

        logger.info(
            "Upserted %d chunks for project document %s", len(vectors), document_id,
        )
        return len(vectors)

    def delete_project_document(self, document_id: int) -> None:
        """Delete all vectors belonging to a ProjectDocument."""
        try:
            self.index.delete(filter={"project_document_id": document_id})
        except Exception as exc:  # noqa: BLE001
            raise PineconeServiceError(f"Failed to delete vectors: {exc}") from exc

    def search(self, query: str, top_k: int = 5, metadata_filter: dict | None = None) -> list[dict]:
        """
        Semantic search over the knowledge base.

        Embeds the query and returns the most similar chunks as a list of
        dicts: ``{"text": ..., "score": ..., "metadata": {...}}``.
        """
        query = (query or "").strip()
        if not query:
            return []

        query_vector = self._embed([query])[0]
        try:
            resp = self.index.query(
                vector=query_vector,
                top_k=top_k,
                include_metadata=True,
                filter=metadata_filter or None,
            )
        except Exception as exc:  # noqa: BLE001
            raise PineconeServiceError(f"Failed to query vectors: {exc}") from exc

        results = []
        for match in resp.get("matches", []):
            meta = match.get("metadata", {}) or {}
            results.append({
                "text": meta.get("text", ""),
                "score": match.get("score"),
                "metadata": {k: v for k, v in meta.items() if k != "text"},
            })
        return results

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _chunk_text(text: str) -> list[str]:
        """Split text into overlapping chunks on a character window."""
        text = (text or "").strip()
        if not text:
            return []

        chunks: list[str] = []
        step = max(1, CHUNK_SIZE - CHUNK_OVERLAP)
        for start in range(0, len(text), step):
            chunk = text[start:start + CHUNK_SIZE].strip()
            if chunk:
                chunks.append(chunk)
        return chunks
