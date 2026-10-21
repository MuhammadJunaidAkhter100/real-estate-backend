import logging
import httpx
from django.conf import settings
from rest_framework import status
from rest_framework.exceptions import APIException

logger = logging.getLogger(__name__)


class ServiceUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Speech transcription is not configured."
    default_code = "service_unavailable"


class BadGateway(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "Speech transcription provider request failed."
    default_code = "bad_gateway"


def create_realtime_transcription_session() -> dict:
    api_key = getattr(settings, "OPENAI_API_KEY", None)
    if not api_key:
        raise ServiceUnavailable(
            detail="Speech transcription is not configured.",
        )

    try:
        response = httpx.post(
            "https://api.openai.com/v1/realtime/client_secrets",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "expires_after": {"anchor": "created_at", "seconds": 60},
                "session": {
                    "type": "transcription",
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": 24000},
                            "transcription": {
                                "model": "gpt-live-transcribe",
                                "delay": "low",
                            },
                            "turn_detection": None,
                        }
                    },
                },
            },
            timeout=15,
        )
        response.raise_for_status()
        result = response.json()
        return {
            "client_secret": result["value"],
            "expires_at": result["expires_at"],
            "session": result["session"],
        }
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.exception("Could not create a realtime transcription session: %s", exc)
        raise BadGateway(
            detail="Could not create a realtime transcription session.",
        ) from exc
