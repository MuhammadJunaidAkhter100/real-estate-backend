"""
JWT auth middleware for Django Channels.

Accepts the access token either:
  - in the `Authorization: Bearer <token>` header (only available with custom
    WebSocket clients that can set headers), OR
  - as a `?token=<token>` query-string parameter (the common case for browsers).
"""
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser

User = get_user_model()


@database_sync_to_async
def _get_user_from_token(raw_token: str):
    try:
        from rest_framework_simplejwt.tokens import UntypedToken
        from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
    except ImportError:
        return AnonymousUser()

    try:
        validated = UntypedToken(raw_token)
    except (InvalidToken, TokenError):
        return AnonymousUser()

    user_id = validated.get('user_id')
    if not user_id:
        return AnonymousUser()

    try:
        return User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return AnonymousUser()


def _extract_token(scope) -> str:
    # 1) Authorization header (bytes)
    headers = dict(scope.get('headers') or [])
    auth_header = headers.get(b'authorization', b'').decode(errors='ignore')
    if auth_header.lower().startswith('bearer '):
        return auth_header.split(' ', 1)[1].strip()

    # 2) ?token=... query string
    query_string = scope.get('query_string', b'').decode(errors='ignore')
    if query_string:
        params = parse_qs(query_string)
        token_values = params.get('token') or params.get('access_token')
        if token_values:
            return token_values[0]

    return ""


class JWTAuthMiddleware(BaseMiddleware):
    async def __call__(self, scope, receive, send):
        token = _extract_token(scope)
        if token:
            scope['user'] = await _get_user_from_token(token)
        else:
            scope['user'] = AnonymousUser()
        return await super().__call__(scope, receive, send)


def JWTAuthMiddlewareStack(inner):
    return JWTAuthMiddleware(inner)
