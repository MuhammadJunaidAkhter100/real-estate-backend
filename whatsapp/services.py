import asyncio
import base64
import mimetypes
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from os.path import basename
from typing import Any
from urllib.parse import quote, unquote, urlparse
import time

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q, Subquery, OuterRef

import httpx
from httpx import HTTPStatusError, RequestError, TimeoutException
from asgiref.sync import async_to_sync, sync_to_async
from channels.layers import get_channel_layer
from decouple import config

from django.utils import timezone

from whatsapp.models import WhatsAppAccount, WhatsAppChat, WhatsAppMessage, WhatsAppDeletedMessage

logger = logging.getLogger(__name__)


VIDEO_MIME_MAP = {
    "video/mp4": "video/mp4",
    "video/quicktime": "video/quicktime",
    "video/x-msvideo": "video/x-msvideo",
    "video/x-matroska": "video/x-matroska",
    "video/webm": "video/webm",
    "video/mpeg": "video/mpeg",
    "video/x-m4v": "video/x-m4v",
    "video/3gpp": "video/3gpp",
}

class WhatsAppService:
    """Provider-based service for creating WhatsApp sessions and fetching QR codes."""

    def __init__(self, provider: str | None = None):
        self.provider = config('WHATSAPP_PROVIDER', default='')
        self.api_key = config('WAHA_API_PLAIN_KEY', default='')
        self.base_url = config('WAHA_BASE_URL', default='')
        self.backend_url = config('BACKEND_URL', default='')
        self.webhook_api_key = config('WAHA_WEBHOOK_API_KEY', default='')

    def _build_headers(self) -> dict[str, str]:
        return {
            "accept": "application/json",
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json",
        }

    def _parse_timestamp(self, value: Any) -> datetime | None:
        if value in (None, ""):
            return None

        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(int(value), tz=dt_timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None

        if isinstance(value, str):
            try:
                return datetime.fromtimestamp(int(float(value)), tz=dt_timezone.utc)
            except ValueError:
                return None

        return None

    def _build_session_name(self, user: Any, session_name: str | None = None) -> str:
        if session_name and session_name.strip():
            return session_name.strip()

        existing_account = WhatsAppAccount.objects.filter(user=user).first()
        if existing_account and existing_account.session_name:
            return existing_account.session_name.strip()

        user_pk = getattr(user, "pk", None) or getattr(user, "id", None)
        first_name = (getattr(user, "first_name", "") or "").strip().replace(" ", "_")
        role = (getattr(user, "role", "") or "").strip().replace(" ", "_")
        
        prefix = f"{first_name}_{role}".strip("_")
        if prefix:
            return f"{prefix}-{user_pk}"
        return f"wa-user-{user_pk}"

    def _save_file_to_storage(self, file_bytes: bytes, file_name: str) -> str:
        try:
            from django.core.files.base import ContentFile
            from django.core.files.storage import default_storage

            file_ext = os.path.splitext(file_name)[1] or ".bin"
            storage_key = f"whatsapp_media/{uuid.uuid4().hex}{file_ext}"
            saved_path = default_storage.save(storage_key, ContentFile(file_bytes))
            storage_url = default_storage.url(saved_path)
            if storage_url.startswith("/"):
                backend_base = (self.backend_url or "").rstrip("/")
                return f"{backend_base}{storage_url}"
            return storage_url
        except Exception:
            logger.exception("Failed to save media file to default_storage")
            return ""

    def _parse_media_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_data = payload.get("_data") or {}
        message_data = raw_data.get("message") or {}

        text = (
            payload.get("body")
            or payload.get("text")
            or payload.get("caption")
            or message_data.get("conversation")
            or ""
        )

        caption = payload.get("caption") or ""
        if not caption:
            if image_msg := message_data.get("imageMessage"):
                caption = image_msg.get("caption") or ""
            elif video_msg := message_data.get("videoMessage"):
                caption = video_msg.get("caption") or ""
            elif audio_msg := message_data.get("audioMessage"):
                caption = audio_msg.get("caption") or ""
            elif document_msg := message_data.get("documentMessage"):
                caption = document_msg.get("caption") or ""

        if not text and caption:
            text = caption

        media_url = ""
        mime_type = ""
        file_name = ""

        # Priority 1: Check root payload "media" dict directly
        media = payload.get("media")
        if isinstance(media, dict) and media.get("url"):
            media_url = media.get("url") or ""
            mime_type = media.get("mimetype") or ""
            file_name = media.get("filename") or ""

        # Priority 2: Check payload "hasMedia" dict
        if not media_url and payload.get("hasMedia"):
            media = payload.get("media") or {}
            media_url = media.get("url") or ""
            mime_type = media.get("mimetype") or ""
            file_name = media.get("filename") or ""

        # Priority 3: Fallback to _data -> message
        if not media_url and message_data:
            if image_msg := message_data.get("imageMessage"):
                media_url = image_msg.get("url") or ""
                mime_type = mime_type or (image_msg.get("mimetype") or "")
                file_name = file_name or (image_msg.get("fileName") or "")
            elif video_msg := message_data.get("videoMessage"):
                media_url = video_msg.get("url") or ""
                mime_type = mime_type or (video_msg.get("mimetype") or "")
                file_name = file_name or (video_msg.get("fileName") or "")
            elif audio_msg := message_data.get("audioMessage"):
                media_url = audio_msg.get("url") or ""
                mime_type = mime_type or (audio_msg.get("mimetype") or "")
                file_name = file_name or (audio_msg.get("fileName") or "")
            elif document_msg := message_data.get("documentMessage"):
                media_url = document_msg.get("url") or ""
                mime_type = mime_type or (document_msg.get("mimetype") or "")
                file_name = file_name or (document_msg.get("fileName") or "")

        message_type = payload.get("type") or ""
        if message_data.get("imageMessage") or (mime_type and mime_type.startswith("image/")):
            message_type = "image"
        elif message_data.get("videoMessage") or (mime_type and mime_type.startswith("video/")):
            message_type = "video"
        elif message_data.get("audioMessage") or (mime_type and mime_type.startswith("audio/")):
            message_type = "audio"
        elif message_data.get("documentMessage") or (mime_type and not mime_type.startswith("text/")):
            message_type = "document"
        elif not message_type:
            message_type = "text"

        return {
            "message_type": message_type,
            "text": text,
            "caption": caption,
            "media_url": media_url,
            "mime_type": mime_type,
            "file_name": file_name,
        }

    def create_session(self, session_name: str) -> dict[str, Any]:
        base_url = self.base_url
        api_key = self.api_key
        backend_url = self.backend_url
        webhook_api_key = self.webhook_api_key

        response = httpx.post(
            f"{base_url}/api/sessions",
            headers={
                "X-Api-Key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "name": session_name,
                "config": {
                    "noweb": {
                        "store": {
                            "enabled": True,
                            "full_sync": True,
                        }
                    },
                    "webhooks": [
                        {
                            "url": f"{backend_url}/api/whatsapp/webhook/",
                            "events": [
                                "message",
                                "session.status",
                                "message.ack",
                                "message.reaction",
                            ],
                            "hmac": {
                                "key": webhook_api_key,
                            },
                        }
                    ],
                },
            },
            timeout=30,
        )

        response.raise_for_status()

        payload = response.json()
        return {
            "provider": self.provider,
            "session_name": session_name,
            "status": payload.get("status") or payload.get("state") or "unknown",
            "raw": payload,
        }

    def get_or_create_media_api_key(self, user: Any = None, session_name: str | None = None) -> str:
        """Fetch or create a media API key for a WhatsApp session via POST /api/keys/media.

        Saves the key to WhatsAppAccount.media_api_key in the DB.
        """
        account = None
        if user:
            account = WhatsAppAccount.objects.filter(user=user).first()
        elif session_name:
            account = WhatsAppAccount.objects.filter(session_name=session_name).first()

        if account and account.media_api_key:
            return account.media_api_key

        target_session = (account.session_name if account else session_name) or ""
        if not target_session:
            return ""

        try:
            url = f"{self.base_url}/api/keys/media"
            response = httpx.post(
                url,
                json={"session": target_session},
                headers=self._build_headers(),
                timeout=15,
            )
            response.raise_for_status()
            data = response.json() or {}
            media_key = data.get("key") or ""

            if media_key and account:
                account.media_api_key = media_key
                account.save(update_fields=["media_api_key", "updated_at"])

            return media_key
        except Exception:
            logger.exception("Failed to generate WAHA media API key for session '%s'", target_session)
            return ""

    def build_media_proxy_url(self, raw_media_url: str) -> str:
        if not raw_media_url:
            return ""

        if "/api/whatsapp/media-proxy/" in raw_media_url or "/api/files/" in raw_media_url:
            return raw_media_url

        backend_base = (self.backend_url or "").rstrip("/")
        encoded_url = quote(raw_media_url, safe="")
        return f"{backend_base}/api/whatsapp/media-proxy/?url={encoded_url}"

    def session_start(self, session_name: str) -> dict[str, Any]:
        base_url = self.base_url
        api_key = self.api_key

        response = httpx.post(
            f"{base_url}/api/sessions/{session_name}/start",
            headers={
                "X-Api-Key": api_key,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()

        payload = response.json()
        return {
            "provider": self.provider,
            "session_name": session_name,
            "status": payload.get("status") or payload.get("state") or "unknown",
            "raw": payload,
        }

    def logout_session(self, session_name: str) -> dict[str, Any]:
        base_url = self.base_url
        api_key = self.api_key

        response = httpx.post(
            f"{base_url}/api/sessions/{session_name}/logout",
            headers={
                "X-Api-Key": api_key,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()

        payload = response.json()
        return {
            "provider": self.provider,
            "session_name": session_name,
            "status": payload.get("status") or payload.get("state") or "unknown",
            "raw": payload,
        }

    def delete_session(self, session_name: str) -> dict[str, Any]:
        base_url = self.base_url.rstrip("/")
        api_key = self.api_key

        max_retries = 3

        for attempt in range(1, max_retries + 1):
            try:
                response = httpx.delete(
                    f"{base_url}/api/sessions/{quote(session_name)}",
                    headers={
                        "X-Api-Key": api_key,
                        "Content-Type": "application/json",
                    },
                    timeout=30,
                )

                response.raise_for_status()

                return {
                    "provider": self.provider,
                    "session_name": session_name,
                    "status": "deleted",
                    "raw": response.json() if response.content else {},
                }

            except httpx.HTTPError as e:
                print(
                    f"Delete session failed "
                    f"(attempt {attempt}/{max_retries}): {e}"
                )

                if attempt == max_retries:
                    raise

                time.sleep(2)

        raise RuntimeError("Failed to delete session")

    def get_qr_code(self, session_name: str) -> dict[str, str]:
        if not session_name or not session_name.strip():
            raise ValueError("Unable to request WhatsApp QR code without a session_name.")

        base_url = self.base_url.rstrip("/")
        api_key = self.api_key

        response = httpx.get(
            f"{base_url}/api/{quote(session_name)}/auth/qr?format=image",
            headers={
                "accept": "image/png",
                "X-Api-Key": api_key,
            },
            timeout=30,
        )
        response.raise_for_status()

        encoded = base64.b64encode(response.content).decode("ascii")
        return {
            "format": "png",
            "base64": encoded,
            "data_url": f"data:image/png;base64,{encoded}",
        }
  
    def store_account(self, user: Any, *, session_name: str | None = None, session_status: str | None = None, provider: str | None = None, name: str | None = None, number: str | None = None, profile_picture: str | None = None, media_api_key: str | None = None, ) -> WhatsAppAccount:
        defaults = {}

        if provider is not None:
            defaults["provider"] = provider

        if session_name is not None:
            defaults["session_name"] = session_name

        if session_status is not None:
            defaults["session_status"] = session_status

        if name is not None:
            defaults["name"] = name

        if number is not None:
            defaults["number"] = number

        if profile_picture is not None:
            defaults["profile_picture"] = profile_picture

        if media_api_key is not None:
            defaults["media_api_key"] = media_api_key

        with transaction.atomic():
            account, _ = WhatsAppAccount.objects.update_or_create(
                user=user,
                defaults=defaults,
            )

        return account

    def get_profile(self, user: Any) -> dict[str, Any]:
        """Fetch profile information from WAHA (GET /api/{session}/profile)

        and update the local WhatsAppAccount model.
        """
        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account or not account.session_name:
            return {
                "status": "error",
                "message": "No WhatsApp session found for the user.",
            }

        try:
            response = httpx.get(
                f"{self.base_url}/api/{account.session_name}/profile",
                headers=self._build_headers(),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json() or {}

            profile_name = data.get("name","") 
            profile_number = data.get("id", "")
            profile_picture = data.get("picture","")

            self.store_account(
                user,
                name=profile_name,
                number=profile_number,
                profile_picture=profile_picture,
            )

            try:
                self.get_or_create_media_api_key(user=user)
            except Exception:
                logger.exception("Auto media_api_key generation failed in get_profile for user %s", getattr(user, "id", None))

            return {
                "status": "success",
                "profile": data,
                "account": {
                    "session_name": account.session_name,
                    "name": profile_name or account.name,
                    "number": profile_number or account.number,
                    "profile_picture": profile_picture or account.profile_picture,
                },
            }
        except (HTTPStatusError, RequestError, TimeoutException) as exc:
            logger.exception("Failed to fetch WhatsApp profile for user %s", getattr(user, "id", None))
            return {
                "status": "error",
                "message": str(exc),
            }

    def set_profile_name(self, user: Any, name: str) -> dict[str, Any]:
        """Set profile display name in WAHA (PUT /api/{session}/profile/name)."""
        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account or not account.session_name:
            return {
                "status": "error",
                "message": "No WhatsApp session found for the user.",
            }

        try:
            response = httpx.put(
                f"{self.base_url}/api/{account.session_name}/profile/name",
                json={"name": name},
                headers=self._build_headers(),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json() if response.content else {"success": True}

            self.store_account(user, name=name)

            return {
                "status": "success",
                "name": name,
                "result": data,
            }
        except (HTTPStatusError, RequestError, TimeoutException) as exc:
            logger.exception("Failed to update WhatsApp profile name for user %s", getattr(user, "id", None))
            return {
                "status": "error",
                "message": str(exc),
            }

    def set_profile_picture(self, user: Any, file_obj: Any = None, picture_url: str | None = None) -> dict[str, Any]:
        """Set profile picture in WAHA (PUT /api/{session}/profile/picture)."""
        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account or not account.session_name:
            return {
                "status": "error",
                "message": "No WhatsApp session found for the user.",
            }

        try:
            target_url = None

            # Case 1: Uploaded file object (multipart form upload from FE / Swagger UI)
            if file_obj:
                content_type = getattr(file_obj, "content_type", "image/jpeg") or "image/jpeg"
                file_bytes = file_obj.read()
                b64_encoded = base64.b64encode(file_bytes).decode("utf-8")
                target_url = f"data:{content_type};base64,{b64_encoded}"

            # Case 2: picture_url string (Base64 data URI, plain Base64 string, or web URL)
            elif picture_url and isinstance(picture_url, str) and picture_url.strip():
                clean_val = picture_url.strip()

                # Already a Data URI scheme (e.g. data:image/png;base64,...)
                if clean_val.startswith("data:image/") and ";base64," in clean_val:
                    target_url = clean_val

                # Plain web URL (http:// or https://)
                elif clean_val.startswith("http://") or clean_val.startswith("https://"):
                    target_url = clean_val

                # Plain Base64 string (without data: prefix)
                else:
                    mime_type = "image/jpeg"
                    try:
                        file_bytes = base64.b64decode(clean_val)
                        if file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                            mime_type = "image/png"
                        elif file_bytes.startswith(b"GIF8"):
                            mime_type = "image/gif"
                        elif file_bytes.startswith(b"RIFF") and len(file_bytes) >= 12 and file_bytes[8:12] == b"WEBP":
                            mime_type = "image/webp"
                    except Exception:
                        pass
                    target_url = f"data:{mime_type};base64,{clean_val}"

            if not target_url:
                return {
                    "status": "error",
                    "message": "Either 'file' or 'picture_url' (URL or Base64 string) must be provided.",
                }

            headers = self._build_headers()
            response = httpx.put(
                f"{self.base_url}/api/{account.session_name}/profile/picture",
                json={"file": {"url": target_url}},
                headers=headers,
                timeout=60,
            )

            response.raise_for_status()
            data = response.json() if response.content else {"success": True}

            new_pic = data.get("picture") or data.get("url") or (target_url if target_url.startswith("http") else "") or ""
            if new_pic:
                self.store_account(user, profile_picture=new_pic)

            return {
                "status": "success",
                "profile_picture": new_pic or account.profile_picture,
                "result": data,
            }
        except (HTTPStatusError, RequestError, TimeoutException) as exc:
            logger.exception("Failed to update WhatsApp profile picture for user %s", getattr(user, "id", None))
            return {
                "status": "error",
                "message": str(exc),
            }

    def delete_profile_picture(self, user: Any) -> dict[str, Any]:
        """Delete profile picture in WAHA (DELETE /api/{session}/profile/picture)."""
        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account or not account.session_name:
            return {
                "status": "error",
                "message": "No WhatsApp session found for the user.",
            }

        try:
            response = httpx.delete(
                f"{self.base_url}/api/{account.session_name}/profile/picture",
                headers=self._build_headers(),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json() if response.content else {"success": True}

            self.store_account(user, profile_picture="")

            return {
                "status": "success",
                "message": "Profile picture deleted successfully.",
                "result": data,
            }
        except (HTTPStatusError, RequestError, TimeoutException) as exc:
            logger.exception("Failed to delete WhatsApp profile picture for user %s", getattr(user, "id", None))
            return {
                "status": "error",
                "message": str(exc),
            }

    def check_number_exists(self, user: Any, phone: str) -> dict[str, Any]:
        """Check if a phone number exists on WhatsApp (GET /api/contacts/check-exists)."""
        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account or not account.session_name:
            return {
                "status": "error",
                "message": "No WhatsApp session found for the user.",
            }

        if not phone or not str(phone).strip():
            return {
                "status": "error",
                "message": "'phone' query parameter is required.",
            }

        try:
            clean_phone = str(phone).strip().lstrip("+")
            response = httpx.get(
                f"{self.base_url}/api/contacts/check-exists",
                params={
                    "phone": clean_phone,
                    "session": account.session_name,
                },
                headers=self._build_headers(),
                timeout=15,
            )
            response.raise_for_status()
            data = response.json() or {}

            chat_id = data.get("chatId")
       
            return {
                "status": "success",
                "numberExists": data.get("numberExists", False),
                "chatId": chat_id
            }
        except (HTTPStatusError, RequestError, TimeoutException) as exc:
            logger.exception("Failed to check WhatsApp number existence for phone %s", phone)
            return {
                "status": "error",
                "message": str(exc),
            }

    def save_contact(self, user: Any, phone: str, first_name: str, last_name: str = "") -> dict[str, Any]:
        """Save or update a WhatsApp contact (PUT /api/{session}/contacts/{contactId})."""
        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account or not account.session_name:
            return {
                "status": "error",
                "message": "No WhatsApp session found for the user.",
            }

        if not phone or not str(phone).strip():
            return {
                "status": "error",
                "message": "'phone' parameter is required.",
            }

        raw_phone = str(phone).strip()
        if "@" in raw_phone:
            contact_id = raw_phone
        else:
            clean_phone = raw_phone.lstrip("+")
            for char in [" ", "-", "(", ")"]:
                clean_phone = clean_phone.replace(char, "")
            contact_id = f"{clean_phone}@c.us"

        contact_id_encoded = quote(contact_id, safe="")

        payload = {
            "firstName": first_name or "",
            "lastName": last_name or "",
        }

        url = f"{self.base_url}/api/{account.session_name}/contacts/{contact_id_encoded}"

        try:
            response = httpx.put(
                url,
                json=payload,
                headers=self._build_headers(),
                timeout=15,
            )
            response.raise_for_status()
            try:
                data = response.json()
            except Exception:
                data = {}

            full_name = f"{first_name} {last_name}".strip()
            if full_name:
                chat_ids = [contact_id]
                if contact_id.endswith("@c.us"):
                    chat_ids.append(contact_id[:-len("@c.us")] + "@s.whatsapp.net")
                elif contact_id.endswith("@s.whatsapp.net"):
                    chat_ids.append(contact_id[:-len("@s.whatsapp.net")] + "@c.us")
                WhatsAppChat.objects.filter(
                    account=account,
                    chat_id__in=chat_ids,
                ).update(name=full_name)

            return {
                "status": "success",
                "message": "Contact saved successfully.",
                "contactId": contact_id,
                "data": data,
            }
        except HTTPStatusError as exc:
            logger.exception("Failed to save contact %s for user %s", contact_id, getattr(user, "id", None))
            error_detail = ""
            try:
                error_detail = exc.response.json()
            except Exception:
                error_detail = exc.response.text
            return {
                "status": "error",
                "message": f"Provider error ({exc.response.status_code}): {error_detail}",
            }
        except (RequestError, TimeoutException) as exc:
            logger.exception("Connection error when saving contact %s", contact_id)
            return {
                "status": "error",
                "message": str(exc),
            }

    def get_all_contacts(self, user: Any, limit: int | None = None, offset: int | None = None) -> dict[str, Any]:
        """Fetch all WhatsApp contacts for the authenticated user's session (GET /api/contacts/all?session=...)."""
        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account or not account.session_name:
            return {
                "status": "error",
                "message": "No WhatsApp session found for the user.",
            }

        params = {"session": account.session_name}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        try:
            response = httpx.get(
                f"{self.base_url}/api/contacts/all",
                params=params,
                headers=self._build_headers(),
                timeout=30,
            )
            data = response.json()
            raw_contacts = data if isinstance(data, list) else data.get("contacts", [])
            if not isinstance(raw_contacts, list):
                raw_contacts = []

            contacts = []
            groups = []
            for item in raw_contacts:
                if not isinstance(item, dict):
                    continue
                cid = item.get("id")
                if isinstance(cid, dict):
                    cid_str = str(cid.get("_serialized") or cid.get("user") or "")
                else:
                    cid_str = str(cid or item.get("chatId") or "")

                cid_str = cid_str.strip()
                if cid_str.endswith("@c.us"):
                    contacts.append(item)
                elif cid_str.endswith("@g.us"):
                    groups.append(item)

            return {
                "status": "success",
                "total_contacts_count": len(contacts),
                "total_groups_count": len(groups),
                "contacts": contacts,
                "groups": groups,
            }
        except HTTPStatusError as exc:
            logger.exception("Failed to fetch WhatsApp contacts for user %s", getattr(user, "id", None))
            error_detail = ""
            try:
                error_detail = exc.response.json()
            except Exception:
                error_detail = exc.response.text
            return {
                "status": "error",
                "message": f"Provider error ({exc.response.status_code}): {error_detail}",
            }
        except (RequestError, TimeoutException) as exc:
            logger.exception("Connection error when fetching WhatsApp contacts for user %s", getattr(user, "id", None))
            return {
                "status": "error",
                "message": str(exc),
            }

    def get_session_state(self, user: Any) -> dict[str, Any]:
        base_url = self.base_url
        api_key = self.api_key
        account = WhatsAppAccount.objects.filter(user=user).first()
        session_name = account.session_name if account else None

        if not session_name:
            return {
                "provider": self.provider,
                "session_name": None,
                "status": "not_created",
                "message": "No WhatsApp session found for the user.",
            }

        response = httpx.get(
            f"{base_url}/api/sessions/{session_name}",
            headers={
                "X-Api-Key": api_key,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()

        payload = response.json() or {}
        session_status = payload.get("status")

        if session_status:
            self.store_account(
                user=user,
                session_status=session_status,
            )
            if str(session_status).upper() == "WORKING":
                try:
                    self.get_profile(user=user)
                    self.get_or_create_media_api_key(user=user)
                except Exception:
                    logger.exception("Auto setup failed for user %s", getattr(user, "id", None))

        return {
            "name": payload.get("name"),
            "status": session_status,
            "me": payload.get("me"),
        }

    def send_text_message(self, user: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a text message through WhatsApp - simplified payload."""
        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account or not account.session_name:
            return {
                "status": "not_found",
                "message": "No WhatsApp session found for the user.",
            }

        # Accept both chat_id and chatId for backward compatibility
        chat_id = payload.get("chat_id") or payload.get("chatId")
        text = payload.get("text") or ""

        if not chat_id or not text:
            return {
                "status": "invalid_payload",
                "message": "chat_id and text are required.",
            }

        # Use session from user's account
        session_name = account.session_name

        body = {
            "chatId": chat_id,
            "text": text,
            "linkPreview": True,
            "linkPreviewHighQuality": False,
            "session": session_name
        }

        response = httpx.post(
            f"{self.base_url}/api/sendText",
            headers=self._build_headers(),
            json=body,
            timeout=30,
        )
        response.raise_for_status()

        result = response.json() or {}
        message_id = (
            self._extract_message_id(result.get("id"), chat_id=chat_id, is_from_me=True)
            or f"sent-{chat_id}-{datetime.now(tz=dt_timezone.utc).timestamp()}"
        )
        
        # Save the sent message to database
        with transaction.atomic():
            chat, _ = WhatsAppChat.objects.get_or_create(
                account=account,
                chat_id=chat_id,
                defaults={"name": chat_id},
            )
            
            WhatsAppMessage.objects.create(
                chat=chat,
                message_id=message_id,
                direction=WhatsAppMessage.Direction.OUTGOING,
                type="text",
                text=text,
                sender_id=account.user.email or "bot",
                sender_name="You",
                timestamp=datetime.now(tz=dt_timezone.utc),
                reaction="",
                raw=result,
            )
            
            # Update chat's last_message_at
            chat.last_message_at = datetime.now(tz=dt_timezone.utc)
            chat.save(update_fields=["last_message_at", "updated_at"])
        
        return {
            "status": result.get("status") or "sent",
            "id": message_id,
            "session": session_name,
            "chat_id": chat_id,
            "provider": self.provider,
            "raw": result,
        }

    def send_image_message(self, user: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """Send an image through WhatsApp."""

        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account or not account.session_name:
            return {
                "status": "not_found",
                "message": "No WhatsApp session found for the user.",
            }

        chat_id = payload.get("chat_id") or payload.get("chatId")
        caption = payload.get("caption") or ""

        uploaded_file = payload.get("file")

        if not chat_id or not uploaded_file:
            return {
                "status": "invalid_payload",
                "message": "chat_id and file are required.",
            }

        session_name = account.session_name

        file_bytes = uploaded_file.read()
        file_name = basename(getattr(uploaded_file, "name", "upload.bin") or "upload.bin")
        
        # Save sent file directly to Django default_storage (S3 or local media)
        media_url = self._save_file_to_storage(file_bytes, file_name)

        # Convert to Base64
        encoded_file = base64.b64encode(file_bytes).decode("utf-8")

        mimetype = (
            uploaded_file.content_type
            or mimetypes.guess_type(uploaded_file.name)[0]
            or "image/jpeg"
        )

        body = {
            "chatId": chat_id,
            "caption": caption,
            "reply_to": None,
            "session": session_name,
            "file": {
                "filename": uploaded_file.name,
                "mimetype": mimetype,
                "data": encoded_file,      # <-- Base64
            },
        }

        response = httpx.post(
            f"{self.base_url}/api/sendImage",
            headers=self._build_headers(),
            json=body,
            timeout=60,
        )

        response.raise_for_status()

        result = response.json() or {}

        parsed_media = self._parse_media_message(result)
        if parsed_media.get("media_url") and ("/api/files/" in parsed_media["media_url"] or "s3.amazonaws.com" in parsed_media["media_url"]):
            media_url = self.build_media_proxy_url(parsed_media["media_url"])

        message_id = (
            self._extract_message_id(result.get("id"), chat_id=chat_id, is_from_me=True)
            or f"sent-{chat_id}-{datetime.now(tz=dt_timezone.utc).timestamp()}"
        )

        with transaction.atomic():

            chat, _ = WhatsAppChat.objects.get_or_create(
                account=account,
                chat_id=chat_id,
                defaults={
                    "name": chat_id,
                },
            )

            WhatsAppMessage.objects.create(
                chat=chat,
                message_id=message_id,
                direction=WhatsAppMessage.Direction.OUTGOING,
                type="image",
                text=caption,
                sender_id=account.user.email or "bot",
                sender_name="You",
                timestamp=datetime.now(tz=dt_timezone.utc),
                media_url=media_url,
                file_name=file_name,
                mime_type=mimetype,
                caption=caption,
                reaction="",
                raw=result,
            )

            chat.last_message_at = datetime.now(tz=dt_timezone.utc)
            chat.save(
                update_fields=[
                    "last_message_at",
                    "updated_at",
                ]
            )

        return {
            "status": result.get("status") or "sent",
            "id": message_id,
            "session": session_name,
            "chat_id": chat_id,
            "provider": self.provider,
            "media_url": media_url,
            "raw": result,
        }

    def send_document_message(self, user: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a document/file through WhatsApp."""

        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account or not account.session_name:
            return {
                "status": "not_found",
                "message": "No WhatsApp session found for the user.",
            }

        chat_id = payload.get("chat_id") or payload.get("chatId")
        caption = payload.get("caption") or ""
        uploaded_file = payload.get("file")

        if not chat_id or not uploaded_file:
            return {
                "status": "invalid_payload",
                "message": "chat_id and file are required.",
            }

        session_name = account.session_name

        file_bytes = uploaded_file.read()
        file_name = basename(getattr(uploaded_file, "name", "upload.bin") or "upload.bin")

        # Save sent file directly to Django default_storage (S3 or local media)
        media_url = self._save_file_to_storage(file_bytes, file_name)

        # Convert to Base64
        encoded_file = base64.b64encode(file_bytes).decode("utf-8")

        # Detect mime type
        mimetype = (
            uploaded_file.content_type
            or mimetypes.guess_type(uploaded_file.name)[0]
            or "application/octet-stream"
        )

        body = {
            "chatId": chat_id,
            "reply_to": None,
            "caption": caption,
            "session": session_name,
            "file": {
                "filename": uploaded_file.name,
                "mimetype": mimetype,
                "data": encoded_file,      # Base64
            },
        }

        response = httpx.post(
            f"{self.base_url}/api/sendFile",
            headers=self._build_headers(),
            json=body,
            timeout=120,
        )

        response.raise_for_status()

        result = response.json() or {}

        parsed_media = self._parse_media_message(result)
        if parsed_media.get("media_url") and ("/api/files/" in parsed_media["media_url"] or "s3.amazonaws.com" in parsed_media["media_url"]):
            media_url = self.build_media_proxy_url(parsed_media["media_url"])

        message_id = (
            self._extract_message_id(result.get("id"), chat_id=chat_id, is_from_me=True)
            or f"sent-{chat_id}-{datetime.now(tz=dt_timezone.utc).timestamp()}"
        )

        with transaction.atomic():

            chat, _ = WhatsAppChat.objects.get_or_create(
                account=account,
                chat_id=chat_id,
                defaults={"name": chat_id},
            )

            WhatsAppMessage.objects.create(
                chat=chat,
                message_id=message_id,
                direction=WhatsAppMessage.Direction.OUTGOING,
                type="document",
                text=caption,
                sender_id=account.user.email or "bot",
                sender_name="You",
                timestamp=datetime.now(tz=dt_timezone.utc),
                media_url=media_url,
                file_name=file_name,
                mime_type=mimetype,
                caption=caption,
                reaction="",
                raw=result,
            )

            chat.last_message_at = datetime.now(tz=dt_timezone.utc)
            chat.save(update_fields=["last_message_at", "updated_at"])

        return {
            "status": result.get("status") or "sent",
            "id": message_id,
            "session": session_name,
            "chat_id": chat_id,
            "provider": self.provider,
            "media_url": media_url,
            "raw": result,
        }

    def send_audio_message(self, user: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a WhatsApp voice note."""

        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account or not account.session_name:
            return {
                "status": "not_found",
                "message": "No WhatsApp session found for the user.",
            }

        chat_id = payload.get("chat_id") or payload.get("chatId")
        uploaded_file = payload.get("file")

        if not chat_id or not uploaded_file:
            return {
                "status": "invalid_payload",
                "message": "chat_id and file are required.",
            }

        session_name = account.session_name

        file_bytes = uploaded_file.read()
        file_name = basename(getattr(uploaded_file, "name", "upload.bin") or "upload.bin")

        # Save sent file directly to Django default_storage (S3 or local media)
        media_url = self._save_file_to_storage(file_bytes, file_name)

        # Convert to Base64
        encoded_file = base64.b64encode(file_bytes).decode("utf-8")

        mimetype = uploaded_file.content_type

        if mimetype == "audio/webm":
            mimetype = "audio/webm"

        elif mimetype in ["audio/mp3", "audio/mpeg"]:
            mimetype = "audio/mpeg"

        elif mimetype in ["audio/x-m4a", "audio/mp4"]:
            mimetype = "audio/mp4"

        else:
            mimetype = "audio/ogg; codecs=opus"

        body = {
            "chatId": chat_id,
            "reply_to": None,
            "convert": True,
            "session": session_name,
            "file": {
                "filename": uploaded_file.name,
                "mimetype": mimetype,
                "data": encoded_file,      # Base64
            },
        }

        response = httpx.post(
            f"{self.base_url}/api/sendVoice",
            headers=self._build_headers(),
            json=body,
            timeout=120,
        )

        response.raise_for_status()

        result = response.json() or {}

        parsed_media = self._parse_media_message(result)
        if parsed_media.get("media_url") and ("/api/files/" in parsed_media["media_url"] or "s3.amazonaws.com" in parsed_media["media_url"]):
            media_url = self.build_media_proxy_url(parsed_media["media_url"])

        message_id = (
            self._extract_message_id(result.get("id"), chat_id=chat_id, is_from_me=True)
            or f"sent-{chat_id}-{datetime.now(tz=dt_timezone.utc).timestamp()}"
        )

        with transaction.atomic():

            chat, _ = WhatsAppChat.objects.get_or_create(
                account=account,
                chat_id=chat_id,
                defaults={"name": chat_id},
            )

            WhatsAppMessage.objects.create(
                chat=chat,
                message_id=message_id,
                direction=WhatsAppMessage.Direction.OUTGOING,
                type="audio",
                text="",
                sender_id=account.user.email or "bot",
                sender_name="You",
                timestamp=datetime.now(tz=dt_timezone.utc),
                media_url=media_url,
                file_name=file_name,
                mime_type=mimetype,
                reaction="",
                raw=result,
            )

            chat.last_message_at = datetime.now(tz=dt_timezone.utc)
            chat.save(update_fields=["last_message_at", "updated_at"])

        return {
            "status": result.get("status") or "sent",
            "id": message_id,
            "session": session_name,
            "chat_id": chat_id,
            "provider": self.provider,
            "media_url": media_url,
            "raw": result,
        }

    def send_video_message(self, user: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a video through WhatsApp."""

        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account or not account.session_name:
            return {
                "status": "not_found",
                "message": "No WhatsApp session found for the user.",
            }

        chat_id = payload.get("chat_id") or payload.get("chatId")
        caption = payload.get("caption") or ""
        uploaded_file = payload.get("file")

        if not chat_id or not uploaded_file:
            return {
                "status": "invalid_payload",
                "message": "chat_id and file are required.",
            }

        session_name = account.session_name

        # Read uploaded file
        file_bytes = uploaded_file.read()
        file_name = basename(getattr(uploaded_file, "name", "upload.bin") or "upload.bin")

        # Save sent file directly to Django default_storage (S3 or local media)
        media_url = self._save_file_to_storage(file_bytes, file_name)

        # Convert to Base64
        encoded_file = base64.b64encode(file_bytes).decode("utf-8")

        
        def get_video_mimetype(uploaded_file):
            # MIME reported by browser/app
            mime = uploaded_file.content_type

            # Fallback using filename
            if not mime:
                mime = mimetypes.guess_type(uploaded_file.name)[0]

            # Default
            if not mime:
                mime = "video/mp4"

            return VIDEO_MIME_MAP.get(mime, mime)
    

        mimetype = get_video_mimetype(uploaded_file)
        
        body = {
            "chatId": chat_id,
            "reply_to": None,
            "caption": caption,
            "convert": True,
            "asNote": False,
            "session": session_name,
            "file": {
                "filename": uploaded_file.name,
                "mimetype": mimetype,
                "data": encoded_file,
            },
        }

        response = httpx.post(
            f"{self.base_url}/api/sendVideo",
            headers=self._build_headers(),
            json=body,
            timeout=300,  # videos may take longer
        )

        response.raise_for_status()

        result = response.json() or {}

        parsed_media = self._parse_media_message(result)
        if parsed_media.get("media_url") and ("/api/files/" in parsed_media["media_url"] or "s3.amazonaws.com" in parsed_media["media_url"]):
            media_url = self.build_media_proxy_url(parsed_media["media_url"])

        message_id = (
            self._extract_message_id(result.get("id"))
            or f"sent-{chat_id}-{datetime.now(tz=dt_timezone.utc).timestamp()}"
        )

        with transaction.atomic():

            chat, _ = WhatsAppChat.objects.get_or_create(
                account=account,
                chat_id=chat_id,
                defaults={"name": chat_id},
            )

            WhatsAppMessage.objects.create(
                chat=chat,
                message_id=message_id,
                direction=WhatsAppMessage.Direction.OUTGOING,
                type="video",
                text=caption,
                sender_id=account.user.email or "bot",
                sender_name="You",
                timestamp=datetime.now(tz=dt_timezone.utc),
                media_url=media_url,
                file_name=file_name,
                mime_type=mimetype,
                caption=caption,
                reaction="",
                raw=result,
            )

            chat.last_message_at = datetime.now(tz=dt_timezone.utc)
            chat.save(update_fields=["last_message_at", "updated_at"])

        return {
            "status": result.get("status") or "sent",
            "id": message_id,
            "session": session_name,
            "chat_id": chat_id,
            "provider": self.provider,
            "media_url": media_url,
            "raw": result,
        }
    
    def _extract_message_id(self, id_val: Any, chat_id: str | None = None, is_from_me: bool = False) -> str | None:
        if not id_val:
            return None
        raw = None
        if isinstance(id_val, dict):
            raw = id_val.get("_serialized") or id_val.get("id") or id_val.get("serialized")
        else:
            raw = str(id_val).strip()

        if not raw:
            return None

        if raw.startswith("true_") or raw.startswith("false_") or raw.startswith("sent-") or raw.startswith("webhook-"):
            return raw

        if chat_id:
            prefix = "true" if is_from_me else "false"
            return f"{prefix}_{chat_id}_{raw}"

        return raw

    def _is_status_or_broadcast(self, payload: dict[str, Any], webhook_payload: dict[str, Any] | None = None) -> bool:
        """Check whether a payload represents a WhatsApp status (story) update or broadcast event."""
        if not isinstance(payload, dict):
            return False

        webhook_payload = webhook_payload or {}
        raw_data = payload.get("_data") if isinstance(payload.get("_data"), dict) else {}
        key = raw_data.get("key") if isinstance(raw_data.get("key"), dict) else {}

        # 1. Check boolean flags
        if payload.get("broadcast") is True or raw_data.get("broadcast") is True or key.get("broadcast") is True:
            return True
        if payload.get("isStatus") is True or raw_data.get("isStatus") is True or key.get("isStatus") is True:
            return True

        # 2. Check JIDs
        to_jid = str(payload.get("to") or "")
        from_jid = str(payload.get("from") or "")
        chat_jid = str(payload.get("chatId") or payload.get("chat_id") or "")
        remote_jid = str(key.get("remoteJid") or "")
        remote_jid_alt = str(key.get("remoteJidAlt") or "")

        jids_to_check = [to_jid, from_jid, chat_jid, remote_jid, remote_jid_alt]
        for jid in jids_to_check:
            if jid == "status@broadcast" or jid.endswith("@broadcast"):
                return True

        # 3. Check message IDs
        msg_id = str(payload.get("id") or key.get("id") or "")
        if "status@broadcast" in msg_id or "@broadcast" in msg_id:
            return True

        return False

    def normalize_chat_id(self, chat_id: str | None):
        if not chat_id:
            return None, False

        chat_id = str(chat_id).strip()

        if chat_id == "status@broadcast" or chat_id.endswith("@broadcast") or "status@broadcast" in chat_id:
            return None, False

        if chat_id.endswith("@c.us"):
            chat_id = chat_id[:-len("@c.us")] + "@s.whatsapp.net"

        if chat_id.endswith("@s.whatsapp.net"):
            user_part = chat_id[:-len("@s.whatsapp.net")]
            if user_part.isdigit() or user_part.replace("-", "").isdigit():
                return chat_id, False

        if chat_id.endswith("@g.us"):
            return chat_id, True

        return None, False

    def get_chat_info(self, chat_id: str, session_name: str) -> dict[str, Any] | None:
        api_key = self.api_key
        base_url = self.base_url
       
        url = f"{base_url}/api/{session_name}/chats/overview"

        response = httpx.get(
            url,
            params={"ids": chat_id},
            headers={
                "accept": "application/json",
                "X-Api-Key": api_key,
            },
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

        if not data:
            return None

        chat = data[0]

        return {
            "name": chat.get("name"),
            "profile_picture": chat.get("picture"),
        }

    def get_group_participants(self, session_name: str, group_id: str) -> dict[str, str]:
        """Fetch group participants from WAHA (GET /api/{session}/groups/{group_id}/participants/v2).

        Returns a dictionary mapping LID -> Phone Number JID:
        e.g., {"22102522470495@lid": "923242225347@c.us", ...}
        """
        if not session_name or not group_id:
            return {}

        cache_key = f"group_participants_{session_name}_{group_id}"
        cached_map = cache.get(cache_key)
        if cached_map is not None and isinstance(cached_map, dict):
            return cached_map

        participant_map: dict[str, str] = {}
        try:
            encoded_group_id = quote(group_id, safe="")
            url = f"{self.base_url}/api/{session_name}/groups/{encoded_group_id}/participants/v2"
            response = httpx.get(
                url,
                headers=self._build_headers(),
                timeout=15,
            )
            if response.status_code == 200:
                data = response.json() or []
                if isinstance(data, list):
                    for item in data:
                        lid = item.get("id") or ""
                        pn = item.get("pn") or ""
                        if lid and pn:
                            participant_map[lid] = pn
                cache.set(cache_key, participant_map, timeout=600)
        except Exception:
            logger.exception(
                "Failed to fetch group participants for session %s, group %s",
                session_name,
                group_id,
            )

        return participant_map

    # GROUP MESSAGES
    def sync_group_messages(self, chat: WhatsAppChat, message_payloads: list[dict[str, Any]], session_name: str | None = None) -> int:

        if not message_payloads:
            return 0

        session_name = session_name or (chat.account.session_name if chat.account else "")
        participant_map = self.get_group_participants(session_name, chat.chat_id) if session_name else {}

        # -------------------------------------------------------------
        # Get existing message IDs and deleted message IDs
        # -------------------------------------------------------------

        existing_ids = set(
            WhatsAppMessage.objects.filter(
                chat=chat
            ).values_list(
                "message_id",
                flat=True,
            )
        )

        deleted_ids = set(
            WhatsAppDeletedMessage.objects.filter(
                account=chat.account,
                chat_id=chat.chat_id,
            ).values_list(
                "message_id",
                flat=True,
            )
        )

        new_messages = []

        # -------------------------------------------------------------
        # Process every group message
        # -------------------------------------------------------------

        for message_payload in message_payloads:

            message = self._save_message_details(
                chat=chat,
                message_payload=message_payload,
                is_group=True,
                existing_ids=existing_ids,
                deleted_ids=deleted_ids,
                participant_map=participant_map,
            )

            if message:
                new_messages.append(message)

        # -------------------------------------------------------------
        # Bulk save
        # -------------------------------------------------------------

        if new_messages:

            with transaction.atomic():

                WhatsAppMessage.objects.bulk_create(
                    new_messages,
                    ignore_conflicts=True,
                )

        return len(new_messages)

    # CHAT MESSAGES
    def sync_chat_messages(self,chat: WhatsAppChat, message_payloads: list[dict[str, Any]] ) -> int:

        if not message_payloads:
            return 0

        # -------------------------------------------------------------
        # Get existing message IDs and deleted message IDs
        # -------------------------------------------------------------

        existing_ids = set(
            WhatsAppMessage.objects.filter(
                chat=chat
            ).values_list(
                "message_id",
                flat=True,
            )
        )

        deleted_ids = set(
            WhatsAppDeletedMessage.objects.filter(
                account=chat.account,
                chat_id=chat.chat_id,
            ).values_list(
                "message_id",
                flat=True,
            )
        )

        new_messages = []

        # -------------------------------------------------------------
        # Process every private chat message
        # -------------------------------------------------------------

        for message_payload in message_payloads:

            message = self._save_message_details(
                chat=chat,
                message_payload=message_payload,
                is_group=False,
                existing_ids=existing_ids,
                deleted_ids=deleted_ids,
            )

            if message:
                new_messages.append(message)

        # -------------------------------------------------------------
        # Bulk save
        # -------------------------------------------------------------

        if new_messages:

            with transaction.atomic():

                WhatsAppMessage.objects.bulk_create(
                    new_messages,
                    ignore_conflicts=True,
                )

        return len(new_messages)

    # SAVE / PREPARE MESSAGE DETAILS
    def _save_message_details(
        self,
        chat: WhatsAppChat,
        message_payload: dict[str, Any],
        is_group: bool = False,
        existing_ids: set[str] | None = None,
        deleted_ids: set[str] | None = None,
        participant_map: dict[str, str] | None = None,
    ) -> WhatsAppMessage | None:

        if self._is_status_or_broadcast(message_payload):
            return None

        if existing_ids is None:
            existing_ids = set()

        if deleted_ids is None:
            deleted_ids = set()

        # =============================================================
        # Raw WhatsApp data
        # =============================================================

        raw_data = message_payload.get("_data") or {}

        key_data = raw_data.get("key") or {}

        # =============================================================
        # Message ID
        # =============================================================

        message_id = ( message_payload.get("id") or key_data.get("id"))

        if not message_id:
            return None

        # -------------------------------------------------------------
        # Already exists or deleted?
        # -------------------------------------------------------------

        if message_id in existing_ids or message_id in deleted_ids:
            return None

        # =============================================================
        # Filter Duplicate Associated Child / Protocol Messages
        # =============================================================
        msg_obj = raw_data.get("message") or {}
        msg_ctx = msg_obj.get("messageContextInfo") or {}
        if (
            msg_obj.get("associatedChildMessage")
            or msg_ctx.get("messageAssociation")
            or raw_data.get("associatedChildMessage")
        ):
            logger.info("Skipping associated child / association wrapper message %s", message_id)
            return None

        # =============================================================
        # Parse message
        # =============================================================

        parsed_message = self._parse_media_message( message_payload )

        # =============================================================
        # Media proxy URL
        # =============================================================

        if parsed_message["media_url"]:
            parsed_message["media_url"] = self.build_media_proxy_url(parsed_message["media_url"])

        # =============================================================
        # Timestamp
        # =============================================================

        timestamp = self._parse_timestamp(
            message_payload.get("timestamp")
            or raw_data.get("messageTimestamp")
        )

        # =============================================================
        # Direction & Sender Details
        # =============================================================

        is_from_me = bool(message_payload.get("fromMe")) or bool(key_data.get("fromMe"))
        account = chat.account if chat else None

        if is_from_me:
            direction = WhatsAppMessage.Direction.OUTGOING

            # Sender ID for outgoing message
            sender_id = (
                (account.number if account and account.number else "")
                or (getattr(account.user, "username", "") if account and account.user else "")
                or "me"
            )
            if "@" not in sender_id and sender_id != "me":
                sender_id = f"{sender_id}@c.us"

            # Sender Name for outgoing message
            user_full_name = (
                account.user.get_full_name().strip()
                if (account and account.user and hasattr(account.user, "get_full_name"))
                else ""
            )
            sender_name = (
                message_payload.get("senderName")
                or raw_data.get("pushName")
                or (account.name if account and account.name else "")
                or user_full_name
                or (account.number if account and account.number else "")
                or "You"
            )
        else:
            direction = WhatsAppMessage.Direction.INCOMING

            if is_group:
                raw_participant = (
                    key_data.get("participantAlt")
                    or message_payload.get("participant")
                    or raw_data.get("participant")
                    or key_data.get("participant")
                    or ""
                )

                if participant_map and raw_participant in participant_map:
                    sender_id = participant_map[raw_participant]
                else:
                    sender_id = raw_participant
            else:
                # For private chat
                sender_id = (
                    message_payload.get("from")
                    or key_data.get("remoteJid")
                    or ""
                )

            sender_name = (
                message_payload.get("senderName")
                or raw_data.get("pushName")
                or raw_data.get("verifiedBizName")
                or ""
            )
            if not sender_name and sender_id:
                sender_name = sender_id.split("@")[0] if "@" in sender_id else sender_id

        # Create WhatsAppMessage object

        parsed_reaction = self._extract_reaction(message_payload)

        message = WhatsAppMessage(
            chat=chat,
            message_id=message_id,
            direction=direction,
            type=parsed_message["message_type"],
            text=parsed_message["text"],
            sender_id=sender_id,
            sender_name=sender_name,
            timestamp=timestamp,
            media_url=parsed_message["media_url"],
            file_name=parsed_message["file_name"],
            mime_type=parsed_message["mime_type"],
            caption=parsed_message["caption"],
            reaction=parsed_reaction,
            raw=message_payload,
        )

        existing_ids.add(message_id)

        return message

    async def _async_get_group_participants(
        self,
        client: httpx.AsyncClient,
        session_name: str,
        group_id: str,
    ) -> dict[str, str]:
        if not session_name or not group_id:
            return {}

        cache_key = f"group_participants_{session_name}_{group_id}"
        cached_map = cache.get(cache_key)
        if cached_map is not None and isinstance(cached_map, dict):
            return cached_map

        participant_map: dict[str, str] = {}
        try:
            encoded_group_id = quote(group_id, safe="")
            url = f"{self.base_url.rstrip('/')}/api/{session_name}/groups/{encoded_group_id}/participants/v2"
            response = await client.get(
                url,
                headers=self._build_headers(),
                timeout=15,
            )
            if response.status_code == 200:
                data = response.json() or []
                if isinstance(data, list):
                    for item in data:
                        lid = item.get("id") or ""
                        pn = item.get("pn") or ""
                        if lid and pn:
                            participant_map[lid] = pn
                cache.set(cache_key, participant_map, timeout=600)
        except Exception:
            logger.exception(
                "Failed to fetch group participants for session %s, group %s",
                session_name,
                group_id,
            )

        return participant_map

    def _db_sync_chats(
        self,
        account: WhatsAppAccount,
        valid_chats: list[dict[str, Any]],
        synced_chat_ids: set[str],
    ) -> dict[str, WhatsAppChat]:
        existing_chats = {
            c.chat_id: c
            for c in WhatsAppChat.objects.filter(account=account)
        }

        chats_to_create = []
        chats_to_update = []
        db_chats_map: dict[str, WhatsAppChat] = {}

        for item in valid_chats:
            cid = item["chat_id"]
            if cid in existing_chats:
                chat_obj = existing_chats[cid]
                updated_fields = []
                if item["name"] and chat_obj.name != item["name"]:
                    chat_obj.name = item["name"]
                    updated_fields.append("name")
                if item["profile_picture"] and chat_obj.profile_picture != item["profile_picture"]:
                    chat_obj.profile_picture = item["profile_picture"]
                    updated_fields.append("profile_picture")
                if item["last_message_at"] and chat_obj.last_message_at != item["last_message_at"]:
                    chat_obj.last_message_at = item["last_message_at"]
                    updated_fields.append("last_message_at")
                sync_unread = item.get("unread_count", 0)
                if sync_unread > 0 and chat_obj.unread_count < sync_unread:
                    chat_obj.unread_count = sync_unread
                    updated_fields.append("unread_count")

                if updated_fields:
                    chats_to_update.append(chat_obj)
                db_chats_map[cid] = chat_obj
            else:
                chat_obj = WhatsAppChat(
                    account=account,
                    chat_id=cid,
                    name=item["name"],
                    profile_picture=item["profile_picture"],
                    is_group=item["is_group"],
                    unread_count=item.get("unread_count", 0),
                    last_message_at=item["last_message_at"],
                )
                chats_to_create.append(chat_obj)

        if chats_to_create:
            created_objs = WhatsAppChat.objects.bulk_create(chats_to_create)
            for obj in created_objs:
                db_chats_map[obj.chat_id] = obj

        if chats_to_update:
            WhatsAppChat.objects.bulk_update(
                chats_to_update,
                fields=["name", "profile_picture", "last_message_at", "unread_count"],
            )

        chats_dict = {
            c.chat_id: c
            for c in WhatsAppChat.objects.filter(account=account, chat_id__in=synced_chat_ids)
        }
        for c in chats_dict.values():
            c.account = account
        return chats_dict

    def _db_get_message_ids(self, account: WhatsAppAccount) -> tuple[set[str], set[str]]:
        existing_message_ids = set(
            WhatsAppMessage.objects.filter(chat__account=account).values_list("message_id", flat=True)
        )
        deleted_message_ids = set(
            WhatsAppDeletedMessage.objects.filter(account=account).values_list("message_id", flat=True)
        )
        return existing_message_ids, deleted_message_ids

    def _db_save_messages_and_purge(
        self,
        account: WhatsAppAccount,
        user: Any,
        results: list[tuple[WhatsAppChat | None, list[dict[str, Any]], dict[str, str]]],
        existing_message_ids: set[str],
        deleted_message_ids: set[str],
        synced_chat_ids: set[str],
    ) -> int:
        new_messages_to_create = []

        for chat_obj, message_payloads, participant_map in results:
            if not chat_obj or not message_payloads:
                continue

            chat_obj.account = account
            is_group = chat_obj.is_group

            for msg_payload in message_payloads:
                msg_obj = self._save_message_details(
                    chat=chat_obj,
                    message_payload=msg_payload,
                    is_group=is_group,
                    existing_ids=existing_message_ids,
                    deleted_ids=deleted_message_ids,
                    participant_map=participant_map if is_group else None,
                )
                if msg_obj:
                    new_messages_to_create.append(msg_obj)

        messages_synced = 0
        if new_messages_to_create:
            for msg_item in new_messages_to_create:
                if getattr(msg_item, "reaction", None) is None:
                    msg_item.reaction = ""
            WhatsAppMessage.objects.bulk_create(
                new_messages_to_create,
                ignore_conflicts=True,
                batch_size=500,
            )
            messages_synced = len(new_messages_to_create)

        WhatsAppChat.objects.filter(account=account).filter(
            Q(chat_id__in=["(None, False)", "None", "", "status@broadcast"]) |
            Q(chat_id__endswith="@broadcast") |
            ~Q(chat_id__in=synced_chat_ids)
        ).delete()

        self.store_account(user=user, session_status="WORKING")
        return messages_synced

    def _db_purge_orphan_chats(self, account: WhatsAppAccount, user: Any, synced_chat_ids: set[str]) -> None:
        WhatsAppChat.objects.filter(account=account).filter(
            Q(chat_id__in=["(None, False)", "None", "", "status@broadcast"]) |
            Q(chat_id__endswith="@broadcast") |
            ~Q(chat_id__in=synced_chat_ids)
        ).delete()

        self.store_account(user=user, session_status="WORKING")

    async def _async_get_chat_info(
        self,
        client: httpx.AsyncClient,
        session_name: str,
        chat_id: str,
        sem: asyncio.Semaphore,
    ) -> tuple[str, dict[str, str]]:
        async with sem:
            try:
                url = f"{self.base_url.rstrip('/')}/api/{session_name}/chats/overview"
                resp = await client.get(
                    url,
                    params={"ids": chat_id},
                    headers={
                        "accept": "application/json",
                        "X-Api-Key": self.api_key,
                    },
                    timeout=15.0,
                )
                if resp.status_code == 200:
                    data = resp.json() or []
                    if isinstance(data, list) and data:
                        chat = data[0]
                        return chat_id, {
                            "name": chat.get("name") or "",
                            "picture": chat.get("picture") or "",
                        }
            except Exception:
                logger.exception("Failed to fetch chat info for chat %s", chat_id)
            return chat_id, {}

    async def _async_get_chats_overview_batch(
        self,
        client: httpx.AsyncClient,
        session_name: str,
        chat_ids: list[str],
    ) -> dict[str, dict[str, str]]:
        if not chat_ids:
            return {}

        sem = asyncio.Semaphore(20)
        tasks = [self._async_get_chat_info(client, session_name, cid, sem) for cid in chat_ids]
        results = await asyncio.gather(*tasks)

        overview_map: dict[str, dict[str, str]] = {}
        for cid, info in results:
            if info:
                overview_map[cid] = info

        return overview_map

    async def _async_sync_chats_and_messages(self, user: Any, account: WhatsAppAccount) -> dict[str, Any]:
        base_url = self.base_url.rstrip("/")
        session_name = account.session_name

        async with httpx.AsyncClient(timeout=30.0) as client:
            # 1. Get all chats from WAHA provider
            try:
                chats_response = await client.get(
                    f"{base_url}/api/{session_name}/chats",
                    params={"merge": "true"},
                    headers=self._build_headers(),
                )
                chats_response.raise_for_status()
                chat_payloads = chats_response.json() or []
            except (HTTPStatusError, RequestError, TimeoutException) as exc:
                return {
                    "status": "error",
                    "message": str(exc),
                    "chats_synced": 0,
                    "messages_synced": 0,
                }

            # 2. Extract and filter chat metadata
            candidate_chats = []
            synced_chat_ids: set[str] = set()

            for chat_payload in chat_payloads:
                raw_chat_payload_id = chat_payload.get("id")
                if (
                    self._is_status_or_broadcast(chat_payload)
                    or raw_chat_payload_id == "status@broadcast"
                    or str(raw_chat_payload_id).endswith("@broadcast")
                ):
                    continue

                chat_id, is_group = self.normalize_chat_id(raw_chat_payload_id)
                if not chat_id:
                    continue

                synced_chat_ids.add(chat_id)
                candidate_chats.append((chat_id, is_group, chat_payload))

            # Fetch chat overview for names and profile pictures concurrently (20 parallel requests)
            overview_map = await self._async_get_chats_overview_batch(client, session_name, list(synced_chat_ids))

            valid_chats = []
            for chat_id, is_group, chat_payload in candidate_chats:
                ov_data = overview_map.get(chat_id) or {}

                chat_name = (
                    ov_data.get("name")
                    or chat_payload.get("name")
                    or chat_payload.get("pushname")
                    or chat_payload.get("formattedTitle")
                    or chat_id
                )

                profile_picture = (
                    ov_data.get("picture")
                    or chat_payload.get("picture")
                    or chat_payload.get("profilePicture")
                    or ""
                )

                last_message_timestamp = (
                    chat_payload.get("lastMessageRecvTimestamp")
                    or chat_payload.get("conversationTimestamp")
                )
                parsed_last_message = self._parse_timestamp(last_message_timestamp)

                raw_unread = (
                    ov_data.get("unreadCount")
                    if "unreadCount" in ov_data
                    else (
                        ov_data.get("unread_count")
                        if "unread_count" in ov_data
                        else (
                            chat_payload.get("unreadCount")
                            if "unreadCount" in chat_payload
                            else chat_payload.get("unread_count", 0)
                        )
                    )
                )
                try:
                    unread_count = int(raw_unread) if raw_unread is not None else 0
                except (ValueError, TypeError):
                    unread_count = 0

                valid_chats.append({
                    "chat_id": chat_id,
                    "name": chat_name,
                    "profile_picture": profile_picture,
                    "is_group": is_group,
                    "unread_count": unread_count,
                    "last_message_at": parsed_last_message,
                    "payload": chat_payload,
                })

            if not valid_chats:
                await sync_to_async(self._db_purge_orphan_chats)(account, user, synced_chat_ids)
                return {
                    "status": "WORKING",
                    "chats_synced": 0,
                    "messages_synced": 0,
                    "message": "WhatsApp chats synced (no active chats found).",
                }

            # 3. Bulk create/update WhatsAppChat models in database
            all_chats_in_db = await sync_to_async(self._db_sync_chats)(account, valid_chats, synced_chat_ids)
            chats_synced = len(valid_chats)

            # 4. Pre-fetch existing and deleted message IDs across account
            existing_message_ids, deleted_message_ids = await sync_to_async(self._db_get_message_ids)(account)

            # 5. Fetch messages concurrently with Semaphore (up to 15 parallel requests)
            sem = asyncio.Semaphore(15)

            async def fetch_messages_for_chat(item: dict[str, Any]) -> tuple[WhatsAppChat | None, list[dict[str, Any]], dict[str, str]]:
                chat_id = item["chat_id"]
                is_group = item["is_group"]
                chat_obj = all_chats_in_db.get(chat_id)
                if not chat_obj:
                    return None, [], {}

                encoded_chat_id = quote(str(chat_id), safe="@")
                participant_map = {}

                async with sem:
                    if is_group:
                        participant_map = await self._async_get_group_participants(client, session_name, chat_id)

                    try:
                        resp = await client.get(
                            f"{base_url}/api/{session_name}/chats/{encoded_chat_id}/messages",
                            params={
                                "sortBy": "timestamp",
                                "downloadMedia": "true",
                                "merge": "true",
                                "limit": 1000000000,
                            },
                            headers=self._build_headers(),
                        )
                        if resp.status_code == 200:
                            message_payloads = resp.json() or []
                        else:
                            message_payloads = []
                    except Exception:
                        message_payloads = []

                return chat_obj, message_payloads, participant_map

            tasks = [fetch_messages_for_chat(item) for item in valid_chats]
            results = await asyncio.gather(*tasks)

            # 6, 7 & 8. Parse messages, bulk insert in DB, and purge orphan chats inside sync_to_async threadpool
            messages_synced = await sync_to_async(self._db_save_messages_and_purge)(
                account, user, results, existing_message_ids, deleted_message_ids, synced_chat_ids
            )

            return {
                "status": "WORKING",
                "chats_synced": chats_synced,
                "messages_synced": messages_synced,
                "message": "WhatsApp chats and messages synced successfully.",
            }

    def sync_chats_and_messages(self, user: Any) -> dict[str, Any]:
        account = WhatsAppAccount.objects.filter(user=user).first()

        if not account or not account.session_name:
            return {
                "status": "not_found",
                "chats_synced": 0,
                "messages_synced": 0,
                "message": "No WhatsApp session found for the user.",
            }

        if (account.session_status or "").upper() != "WORKING":
            return {
                "status": account.session_status or "not_working",
                "chats_synced": 0,
                "messages_synced": 0,
                "message": "WhatsApp session is not connected yet.",
            }

        # ---------------------------------------------------------
        # Sync lock
        # ---------------------------------------------------------

        user_pk = getattr(user, "pk", None) or getattr(user, "id", None)
        lock_key = f"whatsapp_sync_lock_{user_pk}"

        if not cache.add(lock_key, "locked", timeout=180):
            return {
                "status": "already_syncing",
                "chats_synced": 0,
                "messages_synced": 0,
                "message": "WhatsApp sync is already in progress for this user.",
            }

        try:
            return async_to_sync(self._async_sync_chats_and_messages)(user, account)
        finally:
            cache.delete(lock_key)

    def logout(self, user: Any, delete_remote_session: bool = True ) -> dict[str, Any]:

        account = WhatsAppAccount.objects.filter(user=user).first()

        if not account or not account.session_name:
            return {
                "status": "not_found",
                "message": "No WhatsApp session found for the user.",
            }

        session_name = account.session_name

        deletion_result = {
            "provider": self.provider,
            "session_name": session_name,
            "status": "deleted",
        }

        # Delete WAHA session only when requested
        if delete_remote_session:
            try:
                deletion_result = self.delete_session(session_name)
                print(f"Deleted WhatsApp provider session {session_name}: {deletion_result}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to delete WhatsApp provider session %s: %s",
                    session_name,
                    exc,
                )
                deletion_result["message"] = str(exc)

        with transaction.atomic():
            WhatsAppMessage.objects.filter(
                chat__account=account
            ).delete()

            WhatsAppChat.objects.filter(
                account=account
            ).delete()

            account.delete()

        return {
            "status": deletion_result.get("status", "deleted"),
            "provider": self.provider,
            "session_name": session_name,
            "message": "WhatsApp logout completed.",
            "provider_response": deletion_result.get("raw", {}),
        }
         
    def persist_incoming_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Persist incoming webhook events from WAHA.
        Handles:
            - Incoming messages
            - Session status updates
        """

        # -------------------------------------------------------
        # Handle Session Status Events
        # -------------------------------------------------------
        event = payload.get("event")
        print(f"Received webhook event: {event}")

        if event == "session.status":

            session_name = (
                payload.get("session")
                or payload.get("sessionName")
                or payload.get("payload", {}).get("session")
            )

            if not session_name:
                return {
                    "status": "invalid_payload",
                    "message": "Missing session name."
                }

            account = WhatsAppAccount.objects.filter(
                session_name=session_name
            ).first()

            if not account:
                return {
                    "status": "not_found",
                    "message": "WhatsApp account not found."
                }

            session_status = (
                payload.get("status")
                or payload.get("payload", {}).get("status")
                or ""
            ).upper()

            account.session_status = session_status
            account.save(update_fields=["session_status", "updated_at"])

            if session_status == "WORKING":
                try:
                    self.get_profile(user=account.user)
                    self.get_or_create_media_api_key(user=account.user)
                except Exception:
                    logger.exception("Auto setup failed in webhook for user %s", account.user_id)

            # User disconnected WhatsApp from mobile
            if session_status == "FAILED":

                # Skip WAHA delete API because session is already dead.
                self.logout(
                    user=account.user,
                    delete_remote_session=True,
                )

            return {
                "status": "session_updated",
                "session_status": session_status,
            }

        # -------------------------------------------------------
        # Handle Message Reaction Events
        # -------------------------------------------------------
        if event == "message.reaction":
            session_name = (
                payload.get("session")
                or payload.get("sessionName")
                or payload.get("payload", {}).get("session")
                or ""
            )

            if not session_name:
                return {
                    "status": "invalid_payload",
                    "message": "Missing session name in reaction webhook payload.",
                }

            account = WhatsAppAccount.objects.filter(session_name=session_name).first()
            if not account:
                return {
                    "status": "account_not_found",
                    "message": f"No active WhatsApp account matches session_name '{session_name}'.",
                }

            reaction_payload = payload.get("payload", {})
            reaction_obj = reaction_payload.get("reaction") or {}

            target_msg_id = ""
            reaction_text = ""

            if isinstance(reaction_obj, dict):
                target_msg_id = reaction_obj.get("messageId") or reaction_obj.get("id") or ""
                reaction_text = str(reaction_obj.get("text") or "").strip()
            elif isinstance(reaction_obj, str):
                reaction_text = reaction_obj.strip()

            if not target_msg_id:
                target_msg_id = (
                    reaction_payload.get("targetMessageId")
                    or reaction_payload.get("messageId")
                    or reaction_payload.get("msgId")
                    or reaction_payload.get("id")
                    or ""
                )

            if not reaction_text:
                reaction_text = self._extract_reaction(reaction_payload)

            is_from_me = bool(reaction_payload.get("fromMe")) or bool(reaction_payload.get("_data", {}).get("key", {}).get("fromMe"))
            raw_chat_id = (
                reaction_payload.get("chatId")
                or reaction_payload.get("chat_id")
                or (reaction_payload.get("to") if is_from_me else reaction_payload.get("from"))
                or reaction_payload.get("from")
            )
            chat_id, _ = self.normalize_chat_id(raw_chat_id)

            msg = None
            if target_msg_id:
                msg = WhatsAppMessage.objects.filter(chat__account=account, message_id=target_msg_id).first()
            if not msg and target_msg_id:
                stanza_id = str(target_msg_id).split("_")[-1].strip()
                if len(stanza_id) >= 6:
                    msg = WhatsAppMessage.objects.filter(chat__account=account, message_id__endswith=stanza_id).first()

            if msg:
                msg.reaction = reaction_text
                msg.save(update_fields=["reaction"])
                self._broadcast_message_reaction(
                    account=account,
                    chat_id=msg.chat.chat_id,
                    message_id=msg.message_id,
                    reaction=reaction_text,
                )
                return {
                    "status": "reaction_updated",
                    "message_id": msg.message_id,
                    "reaction": reaction_text,
                }
            elif chat_id and target_msg_id:
                self._broadcast_message_reaction(
                    account=account,
                    chat_id=chat_id,
                    message_id=target_msg_id,
                    reaction=reaction_text,
                )
                return {
                    "status": "reaction_broadcasted",
                    "message_id": target_msg_id,
                    "reaction": reaction_text,
                }

            return {
                "status": "reaction_ignored",
                "message": "Could not extract message_id or reaction text.",
            }

        # -------------------------------------------------------
        # Handle Message ACK Events (Sent / Delivered / Read)
        # -------------------------------------------------------
        if event == "message.ack":
            session_name = (
                payload.get("session")
                or payload.get("sessionName")
                or payload.get("payload", {}).get("session")
                or ""
            )

            if not session_name:
                return {
                    "status": "invalid_payload",
                    "message": "Missing session name in ACK webhook payload.",
                }

            account = WhatsAppAccount.objects.filter(
                session_name=session_name
            ).first()

            if not account:
                logger.warning("Incoming WhatsApp ACK webhook for unassigned session_name '%s'", session_name)
                return {
                    "status": "account_not_found",
                    "message": f"No active WhatsApp account matches session_name '{session_name}'.",
                }

            ack_payload = payload.get("payload", {})
            if self._is_status_or_broadcast(ack_payload, payload):
                logger.info("Ignoring status/broadcast ACK webhook event.")
                return {
                    "status": "ignored",
                    "message": "Status update / broadcast ACK ignored.",
                }

            raw_msg_id = ack_payload.get("id") or ack_payload.get("_data", {}).get("key", {}).get("id")
            ack_val = ack_payload.get("ack")
            ack_name = ack_payload.get("ackName")

            ACK_MAP = {
                -1: ("ERROR", -1),
                0: ("ERROR", 0),
                1: ("PENDING", 1),
                2: ("SERVER", 2),
                3: ("DEVICE", 3),
                4: ("READ", 4),
                5: ("PLAYED", 5),
            }
            ACK_NAME_TO_CODE = {
                "ERROR": 0,
                "PENDING": 1,
                "SERVER": 2,
                "DEVICE": 3,
                "READ": 4,
                "PLAYED": 5,
            }

            status_name, status_code = "PENDING", 1
            if ack_val is not None and isinstance(ack_val, int) and ack_val in ACK_MAP:
                status_name, status_code = ACK_MAP[ack_val]

            if ack_name and str(ack_name).upper() in ACK_NAME_TO_CODE:
                status_name = str(ack_name).upper()
                status_code = ACK_NAME_TO_CODE[status_name]

            is_from_me = bool(ack_payload.get("fromMe")) or bool(ack_payload.get("_data", {}).get("key", {}).get("fromMe", True))
            raw_chat_id = (
                ack_payload.get("chatId")
                or ack_payload.get("chat_id")
                or (ack_payload.get("to") if is_from_me else ack_payload.get("from"))
                or ack_payload.get("to")
                or ack_payload.get("from")
            )
            chat_id, _ = self.normalize_chat_id(raw_chat_id)
            message_id = self._extract_message_id(raw_msg_id, chat_id=chat_id, is_from_me=is_from_me)

            msg = None
            if message_id:
                msg = WhatsAppMessage.objects.filter(chat__account=account, message_id=message_id).first()
            if not msg and raw_msg_id:
                msg = WhatsAppMessage.objects.filter(chat__account=account, message_id=str(raw_msg_id)).first()

            # Robust matching: WAHA NOWEB engine uses @lid JIDs (e.g. true_116737563476171@lid_3EB0E3A5FC2CDBA61F4606)
            # Extract the unique stanza ID hash (e.g. 3EB0E3A5FC2CDBA61F4606) from the end of raw_msg_id
            stanza_id = str(raw_msg_id).split("_")[-1].strip() if raw_msg_id else ""
            if not msg and stanza_id and len(stanza_id) >= 6:
                msg = WhatsAppMessage.objects.filter(
                    chat__account=account,
                    message_id__endswith=stanza_id
                ).first()

            # Fallback for recent outgoing messages if message_id was temporary (e.g., sent-...)
            if not msg and is_from_me:
                cutoff = datetime.now(tz=dt_timezone.utc) - timedelta(seconds=90)
                msg = WhatsAppMessage.objects.filter(
                    chat__account=account,
                    direction=WhatsAppMessage.Direction.OUTGOING,
                    timestamp__gte=cutoff,
                    ack_code__lt=status_code,
                ).order_by("-timestamp").first()

            if msg:
                if status_code >= msg.ack_code:
                    msg.ack = status_name
                    msg.ack_code = status_code
                    msg.save(update_fields=["ack", "ack_code"])

                    self._broadcast_message_ack(
                        account=account,
                        chat_id=msg.chat.chat_id,
                        message_id=msg.message_id,
                        ack=msg.ack,
                        ack_code=msg.ack_code,
                    )

                return {
                    "status": "ack_updated",
                    "message_id": msg.message_id,
                    "ack": msg.ack,
                    "ack_code": msg.ack_code,
                }

            return {
                "status": "message_not_found",
                "message_id": message_id or str(raw_msg_id),
                "ack": status_name,
            }

        # -------------------------------------------------------
        # Existing Message Logic
        # -------------------------------------------------------

        # Complete webhook payload
        webhook_payload = payload

        # Actual message payload
        payload = webhook_payload.get("payload", {})

        session_name = (
            webhook_payload.get("session")
            or webhook_payload.get("sessionName")
            or ""
        )

        if not session_name:
            return {
                "status": "invalid_payload",
                "message": "Missing session name in webhook payload.",
            }

        account = WhatsAppAccount.objects.filter(
            session_name=session_name
        ).first()

        if not account:
            logger.warning("Incoming WhatsApp webhook for unassigned session_name '%s'", session_name)
            return {
                "status": "account_not_found",
                "message": f"No active WhatsApp account matches session_name '{session_name}'.",
            }

        # Check if status/broadcast event
        if self._is_status_or_broadcast(payload, webhook_payload):
            logger.info("Ignoring incoming status/broadcast webhook message for session '%s'", session_name)
            return {
                "status": "ignored",
                "message": "Status update and broadcast events are ignored.",
            }

        # Check if fromMe is true
        is_from_me = bool(payload.get("fromMe")) or bool(payload.get("_data", {}).get("key", {}).get("fromMe"))

        # Chat ID resolution (prioritize chatId/to when fromMe is true)
        raw_chat_id = (
            payload.get("chatId")
            or payload.get("chat_id")
            or payload.get("_data", {}).get("key", {}).get("remoteJidAlt")
            or (payload.get("to") if is_from_me else payload.get("from"))
            or payload.get("from")
        )

        chat_id, is_group = self.normalize_chat_id(raw_chat_id)
        if not chat_id:
            return {
                "status": "invalid_payload",
                "message": "Only valid WhatsApp user/group chats are supported.",
            }

        chat_name = (
            payload.get("_data", {}).get("pushName")
            or chat_id
        )

        chat, _ = WhatsAppChat.objects.get_or_create(
            account=account,
            chat_id=chat_id,
            defaults={
                "name": chat_name,
                "is_group": is_group,
            },
        )

        # -------------------------------------------------------
        # Default Message Values
        # -------------------------------------------------------

        parsed_message = self._parse_media_message(payload)
        message_type = parsed_message["message_type"]
        text = parsed_message["text"]
        caption = parsed_message["caption"]
        media_url = parsed_message["media_url"]
        mime_type = parsed_message["mime_type"]
        filename = parsed_message["file_name"]

        if media_url:
            media_url = self.build_media_proxy_url(media_url)

        # Parse ACK status from payload if present
        raw_ack_val = payload.get("ack")
        raw_ack_name = payload.get("ackName")
        msg_ack = "SERVER" if is_from_me else "READ"
        msg_ack_code = 2 if is_from_me else 4
        if raw_ack_name:
            msg_ack = str(raw_ack_name).upper()
            ACK_NAME_TO_CODE = {"ERROR": 0, "PENDING": 1, "SERVER": 2, "DEVICE": 3, "READ": 4, "PLAYED": 5}
            msg_ack_code = ACK_NAME_TO_CODE.get(msg_ack, 2 if is_from_me else 4)
        elif raw_ack_val is not None and isinstance(raw_ack_val, int):
            ACK_MAP = {-1: "ERROR", 0: "ERROR", 1: "PENDING", 2: "SERVER", 3: "DEVICE", 4: "READ", 5: "PLAYED"}
            msg_ack = ACK_MAP.get(raw_ack_val, "SERVER" if is_from_me else "READ")
            msg_ack_code = raw_ack_val if raw_ack_val > 0 else 0

        # -------------------------------------------------------
        # Message Id
        raw_msg_id = payload.get("id") or payload.get("_data", {}).get("key", {}).get("id")
        message_id = (
            self._extract_message_id(raw_msg_id, chat_id=chat_id, is_from_me=is_from_me)
            or f"webhook-{chat.id}-{datetime.now(tz=dt_timezone.utc).timestamp()}"
        )

        if message_id and WhatsAppDeletedMessage.objects.filter(
            account=account, chat_id=chat.chat_id, message_id=message_id
        ).exists():
            logger.info("Ignoring webhook for deleted message %s in chat %s", message_id, chat.chat_id)
            return {
                "status": "ignored",
                "message": f"Message {message_id} was deleted by user.",
            }

        # -------------------------------------------------------
        # Direction & Sender Details
        # -------------------------------------------------------
        user_full_name = (
            account.user.get_full_name().strip()
            if (account and account.user and hasattr(account.user, "get_full_name"))
            else ""
        )
        if is_from_me:
            direction = WhatsAppMessage.Direction.OUTGOING
            sender_id = account.user.email or "bot"
            sender_name = account.name or user_full_name or "You"
        else:
            direction = WhatsAppMessage.Direction.INCOMING
            sender_id = chat_id
            sender_name = chat_name

        # -------------------------------------------------------
        # Save / Update Message (Deduplication Logic)
        # -------------------------------------------------------

        existing_msg = WhatsAppMessage.objects.filter(chat=chat, message_id=message_id).first()

        if not existing_msg and is_from_me:
            cutoff = datetime.now(tz=dt_timezone.utc) - timedelta(seconds=45)
            existing_msg = WhatsAppMessage.objects.filter(
                chat=chat,
                direction=WhatsAppMessage.Direction.OUTGOING,
                type=message_type,
                message_id__startswith="sent-",
                timestamp__gte=cutoff,
            ).order_by("-timestamp").first()

        reaction = self._extract_reaction(payload)

        if existing_msg:
            existing_msg.message_id = message_id
            existing_msg.direction = direction
            if text:
                existing_msg.text = text
            if caption:
                existing_msg.caption = caption
            if media_url:
                existing_msg.media_url = media_url
            if mime_type:
                existing_msg.mime_type = mime_type
            if filename:
                existing_msg.file_name = filename
            if reaction:
                existing_msg.reaction = reaction
            if msg_ack_code >= existing_msg.ack_code:
                existing_msg.ack = msg_ack
                existing_msg.ack_code = msg_ack_code
            existing_msg.raw = webhook_payload
            existing_msg.save()
            message = existing_msg
            created = False
        else:
            defaults = {
                "direction": direction,
                "type": message_type,
                "text": text,
                "caption": caption,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "timestamp": self._parse_timestamp(payload.get("timestamp")) or datetime.now(tz=dt_timezone.utc),
                "media_url": media_url,
                "mime_type": mime_type,
                "file_name": filename,
                "reaction": reaction,
                "ack": msg_ack,
                "ack_code": msg_ack_code,
                "raw": webhook_payload,
            }
            message = WhatsAppMessage.objects.create(
                chat=chat,
                message_id=message_id,
                **defaults,
            )
            created = True

        if created and not is_from_me:
            chat.unread_count = (chat.unread_count or 0) + 1
            chat.save(update_fields=["unread_count", "updated_at"])

        # -------------------------------------------------------
        # Broadcast to WebSocket Clients
        # -------------------------------------------------------

        self._broadcast_message(
            account,
            chat,
            message,
        )

        # -------------------------------------------------------
        # Response
        # -------------------------------------------------------

        return {
            "status": "saved",
            "created": created,
            "chat_id": chat.chat_id,
            "message_id": message.message_id,
            "message_type": message.type,
        }

    def _broadcast_message(self, account: WhatsAppAccount, chat: WhatsAppChat, message: WhatsAppMessage) -> None:
        try:
            channel_layer = get_channel_layer()
            if channel_layer is None:
                return

            group_name = f"whatsapp_{account.user_id}"
            payload = {
                "type": "whatsapp.message",
                "payload": {
                    "chat_id": chat.chat_id,
                    "chat_name": chat.name,
                    "unread_count": getattr(chat, "unread_count", 0),
                    "is_read": (getattr(chat, "unread_count", 0) == 0),
                    "message_id": message.message_id,
                    "direction": message.direction,
                    "text": message.text,
                    "sender_id": message.sender_id,
                    "sender_name": message.sender_name,
                    "reaction": getattr(message, "reaction", ""),
                    "ack": message.ack,
                    "ack_code": message.ack_code,
                    "timestamp": message.timestamp.isoformat() if message.timestamp else None,
                },
            }

            print(f"Broadcasting WhatsApp webhook message for account {account.pk} to group {group_name}: {payload}")
            
            async_to_sync(channel_layer.group_send)(group_name, payload)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to broadcast WhatsApp webhook message for account %s", account.pk)

    def _broadcast_message_ack(
        self,
        account: WhatsAppAccount,
        chat_id: str,
        message_id: str,
        ack: str,
        ack_code: int,
    ) -> None:
        try:
            channel_layer = get_channel_layer()
            if channel_layer is None:
                return

            group_name = f"whatsapp_{account.user_id}"
            payload = {
                "type": "whatsapp.message_ack",
                "payload": {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "ack": ack,
                    "ack_code": ack_code,
                },
            }
            async_to_sync(channel_layer.group_send)(group_name, payload)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to broadcast WhatsApp message ACK for account %s", account.pk)

    def _broadcast_message_reaction(
        self,
        account: WhatsAppAccount,
        chat_id: str,
        message_id: str,
        reaction: str,
    ) -> None:
        try:
            channel_layer = get_channel_layer()
            if channel_layer is None:
                return

            group_name = f"whatsapp_{account.user_id}"
            payload = {
                "type": "whatsapp.message_reaction",
                "payload": {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reaction": reaction,
                },
            }
            async_to_sync(channel_layer.group_send)(group_name, payload)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to broadcast WhatsApp message reaction for account %s", account.pk)

    @staticmethod
    def _serialize_whatsapp_message(message: WhatsAppMessage | None) -> dict[str, Any] | None:
        if not message:
            return None
        return {
            "id": str(message.id),
            "message_id": message.message_id,
            "direction": message.direction,
            "type": message.type,
            "text": message.text,
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
            "ack": getattr(message, "ack", "PENDING"),
            "ack_code": getattr(message, "ack_code", 1),
            "timestamp": message.timestamp.isoformat() if message.timestamp else None,
            "media_url": message.media_url,
            "file_name": message.file_name,
            "mime_type": message.mime_type,
            "caption": message.caption,
            "reaction": getattr(message, "reaction", ""),
            "created_at": message.created_at.isoformat(),
        }

    def get_chats(self, user: Any) -> dict[str, Any]:
        """Retrieve all chats for the authenticated user."""
        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account:
            return {
                "status": "not_found",
                "chats": [],
                "message": "No WhatsApp account found for the user.",
            }

        # Purge legacy dummy/corrupt/broadcast chats if present
        WhatsAppChat.objects.filter(account=account).filter(
            Q(chat_id__in=["(None, False)", "None", "", "status@broadcast"]) |
            Q(chat_id__endswith="@broadcast")
        ).delete()

        latest_msg_subquery = WhatsAppMessage.objects.filter(
            chat=OuterRef("pk")
        ).order_by("-timestamp", "-created_at", "-id").values("id")[:1]

        chats = (
            WhatsAppChat.objects.filter(account=account)
            .exclude(chat_id__startswith="(")
            .annotate(latest_message_id=Subquery(latest_msg_subquery))
            .order_by("-last_message_at", "-updated_at")
        )

        message_ids = [chat.latest_message_id for chat in chats if getattr(chat, "latest_message_id", None) is not None]
        messages_by_id = {
            msg.id: msg
            for msg in WhatsAppMessage.objects.filter(id__in=message_ids)
        }

        chat_data = [
            {
                "id": str(chat.id),
                "chat_id": chat.chat_id,
                "name": chat.name,
                "profile_picture_url": chat.profile_picture,
                "is_group": chat.is_group,
                "unread_count": getattr(chat, "unread_count", 0),
                "is_read": (getattr(chat, "unread_count", 0) == 0),
                "last_message_at": chat.last_message_at.isoformat() if chat.last_message_at else None,
                "last_message": self._serialize_whatsapp_message(messages_by_id.get(getattr(chat, "latest_message_id", None))),
                "created_at": chat.created_at.isoformat(),
                "updated_at": chat.updated_at.isoformat(),
            }
            for chat in chats
        ]

        return {
            "status": "success",
            "count": len(chat_data),
            "chats": chat_data,
        }

    def get_chat_messages(self, user: Any, chat_id: str) -> dict[str, Any]:
        """Retrieve all messages for a specific chat."""
        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account:
            return {
                "status": "not_found",
                "messages": [],
                "message": "No WhatsApp account found for the user.",
            }

        chat = WhatsAppChat.objects.filter(account=account, chat_id=chat_id).first()
        if not chat:
            return {
                "status": "chat_not_found",
                "messages": [],
                "message": f"Chat with ID {chat_id} not found.",
            }

        # Mark chat as read when fetching its messages
        if getattr(chat, "unread_count", 0) != 0:
            chat.unread_count = 0
            chat.save(update_fields=["unread_count", "updated_at"])

        # Cleanup temporary sent- duplicate messages if real webhook message exists
        sent_msgs = WhatsAppMessage.objects.filter(chat=chat, message_id__startswith="sent-")
        for sent_msg in sent_msgs:
            if WhatsAppMessage.objects.filter(
                chat=chat,
                direction=sent_msg.direction,
                type=sent_msg.type,
            ).exclude(id=sent_msg.id).filter(
                timestamp__gte=sent_msg.timestamp - timedelta(seconds=60),
                timestamp__lte=sent_msg.timestamp + timedelta(seconds=60),
            ).exists():
                sent_msg.delete()

        messages = WhatsAppMessage.objects.filter(chat=chat).order_by("-timestamp")
        message_data = [
            self._serialize_whatsapp_message(message)
            for message in messages
        ]

        return {
            "status": "success",
            "count": len(message_data),
            "chat_id": chat_id,
            "chat_name": chat.name,
            "messages": message_data,
        }

    def mark_chat_as_read(self, user: Any, chat_id: str) -> dict[str, Any]:
        """Explicitly mark a WhatsApp chat as read."""
        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account:
            return {
                "status": "not_found",
                "message": "No WhatsApp account found for the user.",
            }

        chat = WhatsAppChat.objects.filter(account=account, chat_id=chat_id).first()
        if not chat:
            return {
                "status": "chat_not_found",
                "message": f"Chat with ID {chat_id} not found.",
            }

        if chat.unread_count != 0:
            chat.unread_count = 0
            chat.save(update_fields=["unread_count", "updated_at"])

        return {
            "status": "success",
            "chat_id": chat_id,
            "unread_count": 0,
            "is_read": True,
        }

    def delete_chat(self, user: Any, chat_id: str) -> dict[str, Any]:
        """Delete a chat and all its messages from the database."""
        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account:
            return {
                "status": "not_found",
                "message": "No WhatsApp account found for the user.",
            }

        chat = WhatsAppChat.objects.filter(account=account, chat_id=chat_id).first()
        if not chat:
            return {
                "status": "chat_not_found",
                "message": f"Chat with ID {chat_id} not found.",
            }

        messages = list(WhatsAppMessage.objects.filter(chat=chat))

        # Also delete from provider if needed
        try:
            session_name = account.session_name
            response = httpx.delete(
                f"{self.base_url}/api/{session_name}/chats/{quote(chat_id)}",
                headers=self._build_headers(),
                timeout=30,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning(f"Failed to delete chat from provider: {exc}")

        # Delete from database and store in WhatsAppDeletedMessage
        with transaction.atomic():
            deleted_objects = [
                WhatsAppDeletedMessage(
                    account=account,
                    chat_id=chat_id,
                    message_id=msg.message_id,
                )
                for msg in messages
            ]
            if deleted_objects:
                WhatsAppDeletedMessage.objects.bulk_create(deleted_objects, ignore_conflicts=True)

            WhatsAppMessage.objects.filter(chat=chat).delete()
            chat.delete()

        return {
            "status": "deleted",
            "chat_id": chat_id,
            "message": "Chat and all messages deleted successfully.",
        }

    def delete_message(self, user: Any, chat_id: str, message_id: str) -> dict[str, Any]:
        """Delete a single message from a chat."""
        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account:
            return {
                "status": "not_found",
                "message": "No WhatsApp account found for the user.",
            }

        chat = WhatsAppChat.objects.filter(account=account, chat_id=chat_id).first()
        if not chat:
            return {
                "status": "chat_not_found",
                "message": f"Chat with ID {chat_id} not found.",
            }

        message = WhatsAppMessage.objects.filter(chat=chat, message_id=message_id).first()

        # Record in WhatsAppDeletedMessage even if local WhatsAppMessage object was missing
        # so that future sync won't pull it from WhatsApp provider.
        with transaction.atomic():
            WhatsAppDeletedMessage.objects.get_or_create(
                account=account,
                chat_id=chat_id,
                message_id=message_id,
            )
            if message:
                message.delete()

        # Also delete from provider if needed
        try:
            session_name = account.session_name
            response = httpx.delete(
                f"{self.base_url}/api/{session_name}/chats/{quote(chat_id)}/messages/{quote(message_id)}",
                headers=self._build_headers(),
                timeout=30,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning(f"Failed to delete message from provider: {exc}")

        return {
            "status": "deleted",
            "chat_id": chat_id,
            "message_id": message_id,
            "message": "Message deleted successfully.",
        }

    def delete_all_messages(self, user: Any, chat_id: str) -> dict[str, Any]:
        """Delete all messages from a chat."""
        account = WhatsAppAccount.objects.filter(user=user).first()
        if not account:
            return {
                "status": "not_found",
                "message": "No WhatsApp account found for the user.",
            }

        chat = WhatsAppChat.objects.filter(account=account, chat_id=chat_id).first()
        if not chat:
            return {
                "status": "chat_not_found",
                "message": f"Chat with ID {chat_id} not found.",
            }

        messages = list(WhatsAppMessage.objects.filter(chat=chat))
        message_count = len(messages)

        # Also delete from provider if needed
        try:
            session_name = account.session_name
            response = httpx.delete(
                f"{self.base_url}/api/{session_name}/chats/{quote(chat_id)}/messages",
                headers=self._build_headers(),
                timeout=30,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning(f"Failed to delete messages from provider: {exc}")

        # Delete from database and record deletions
        with transaction.atomic():
            deleted_objects = [
                WhatsAppDeletedMessage(
                    account=account,
                    chat_id=chat_id,
                    message_id=msg.message_id,
                )
                for msg in messages
            ]
            if deleted_objects:
                WhatsAppDeletedMessage.objects.bulk_create(deleted_objects, ignore_conflicts=True)

            WhatsAppMessage.objects.filter(chat=chat).delete()

        return {
            "status": "deleted",
            "chat_id": chat_id,
            "messages_deleted": message_count,
            "message": f"All {message_count} messages deleted successfully.",
        }

    def get_or_create_qr_session( self, user: Any, session_name: str | None = None ) -> dict[str, Any]:
        current_user = WhatsAppAccount.objects.filter(user=user).first()

        resolved_name = self._build_session_name(user, session_name)
        if current_user and current_user.session_name:
            resolved_name = current_user.session_name.strip()

        if current_user and not current_user.session_name:
            self.store_account(
                user=user,
                session_name=resolved_name,
                provider=self.provider,
            )
            current_user.session_name = resolved_name

        if current_user:
            status = (current_user.session_status or "").upper()
            print(f"Existing WhatsApp session for user {user.id}: {resolved_name} (status: {status})")

            if status in ("STARTING", "SCAN_QR_CODE"):
                qr_payload = self.get_qr_code(resolved_name)

            elif status == "FAILED":
                logout = self.logout_session(resolved_name)
                qr_payload = self.get_qr_code(resolved_name)

                self.store_account(
                    user=user,
                    session_status=logout.get("status", "STARTING"),
                )

            elif status == "STOPPED":
                started = self.session_start(resolved_name)
                qr_payload = self.get_qr_code(resolved_name)

                self.store_account(
                    user=user,
                    session_status=started.get("status", "STARTING"),
                )

            elif status in ("NOT_CREATED", "NOT_CREATED", "UNKNOWN", "", "NOT_CREATED"):
                self.create_session(resolved_name)
                started = self.session_start(resolved_name)
                qr_payload = self.get_qr_code(resolved_name)

                self.store_account(
                    user=user,
                    session_status=started.get("status", "STARTING"),
                )

            elif status == "WORKING":
                try:
                    self.get_profile(user=user)
                    self.get_or_create_media_api_key(user=user)
                except Exception:
                    logger.exception("Auto setup failed in qr_session for user %s", getattr(user, "id", None))
                return {
                    "provider": self.provider,
                    "session_name": resolved_name,
                    "status": status,
                    "message": "WhatsApp session is already connected.",
                }

            else:
                qr_payload = self.get_qr_code(resolved_name)

            return {
                "provider": self.provider,
                "session_name": resolved_name,
                "qr_code": qr_payload["data_url"],
                "status": current_user.session_status,
                "message": "QR session ready.",
            }

        # ---------- New User ----------
        self.create_session(resolved_name)
        started = self.session_start(resolved_name)
        qr_payload = self.get_qr_code(resolved_name)

        account = self.store_account(
            user=user,
            session_name=resolved_name,
            provider=self.provider,
            session_status=started.get("status", "STARTING"),
        )

        return {
            "provider": self.provider,
            "session_name": account.session_name,
            "qr_code": qr_payload["data_url"],
            "status": account.session_status,
            "message": "New QR session generated.",
        }

    def _extract_reaction(self, payload: dict[str, Any]) -> str:
        """Extract reaction emoji string from WAHA message payload."""
        if not isinstance(payload, dict):
            return ""

        # Priority 1: Check root payload 'reaction' field
        reaction = payload.get("reaction")
        if isinstance(reaction, str) and reaction.strip():
            return reaction.strip()
        elif isinstance(reaction, dict) and reaction.get("text"):
            return str(reaction["text"]).strip()

        # Priority 2: Check _data -> reactions list
        raw_data = payload.get("_data")
        if isinstance(raw_data, dict):
            reactions = raw_data.get("reactions")
            if isinstance(reactions, list) and reactions:
                last_reaction = reactions[-1]
                if isinstance(last_reaction, dict) and last_reaction.get("text"):
                    return str(last_reaction["text"]).strip()
                elif isinstance(last_reaction, str) and last_reaction.strip():
                    return last_reaction.strip()

        return ""

    def send_reaction(self, user: Any, message_id: str, reaction: str, session_name: str | None = None) -> dict[str, Any]:
        """Send a reaction to a WhatsApp message (PUT /api/reaction)."""
        target_session = (session_name or "").strip()
        account = WhatsAppAccount.objects.filter(user=user).first()
        if not target_session and account and account.session_name:
            target_session = account.session_name.strip()

        if not target_session:
            target_session = "default"

        payload = {
            "messageId": message_id,
            "reaction": reaction,
            "session": target_session,
        }

        url = f"{self.base_url.rstrip('/')}/api/reaction"

        try:
            response = httpx.put(
                url,
                json=payload,
                headers=self._build_headers(),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json() if response.content else {"success": True}

            # Update local WhatsAppMessage reaction in DB if found
            if account:
                msg = WhatsAppMessage.objects.filter(chat__account=account, message_id=message_id).first()
                if not msg and message_id:
                    stanza_id = message_id.split("_")[-1].strip()
                    if len(stanza_id) >= 6:
                        msg = WhatsAppMessage.objects.filter(chat__account=account, message_id__endswith=stanza_id).first()
                if msg:
                    msg.reaction = reaction
                    msg.save(update_fields=["reaction"])
                    self._broadcast_message_reaction(
                        account=account,
                        chat_id=msg.chat.chat_id,
                        message_id=msg.message_id,
                        reaction=reaction,
                    )
                else:
                    parts = message_id.split("_")
                    target_chat_id = parts[1] if len(parts) >= 3 and "@" in parts[1] else ""
                    self._broadcast_message_reaction(
                        account=account,
                        chat_id=target_chat_id,
                        message_id=message_id,
                        reaction=reaction,
                    )

            return {
                "status": "success",
                "message": "Reaction sent successfully.",
                "data": data,
            }
        except HTTPStatusError as exc:
            logger.exception("Failed to send reaction to message %s for user %s", message_id, getattr(user, "id", None))
            error_detail = ""
            try:
                error_detail = exc.response.json()
            except Exception:
                error_detail = exc.response.text
            return {
                "status": "error",
                "message": f"Provider error ({exc.response.status_code}): {error_detail}",
                "status_code": exc.response.status_code,
            }
        except (RequestError, TimeoutException) as exc:
            logger.exception("Connection error when sending reaction to message %s", message_id)
            return {
                "status": "error",
                "message": str(exc),
            }
           