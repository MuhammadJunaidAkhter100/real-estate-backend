from django.utils import timezone
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Notification
from .serializers import NotificationSerializer


class NotificationViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """
    Endpoints:
      GET    /api/notifications/                  → list current user's notifications
      GET    /api/notifications/{id}/             → retrieve one
      DELETE /api/notifications/{id}/             → delete a single notification
      POST   /api/notifications/{id}/mark_read/   → mark a single notification as read
      POST   /api/notifications/mark_all_read/    → mark all of current user's notifications as read
      DELETE /api/notifications/delete_all/       → delete all of current user's notifications
      GET    /api/notifications/unread_count/     → unread count for the current user
    """
    serializer_class = NotificationSerializer
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ['get', 'post', 'delete']

    _TAGS = ['Notifications']

    def get_queryset(self):
        user = self.request.user
        if user.is_anonymous:
            return Notification.objects.none()
        return Notification.objects.filter(recipient=user)

    @swagger_auto_schema(tags=_TAGS)
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @swagger_auto_schema(tags=_TAGS)
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @swagger_auto_schema(tags=_TAGS, request_body=None, responses={200: NotificationSerializer})
    @action(detail=True, methods=['post'], url_path='mark_read')
    def mark_read(self, request, pk=None):
        notification = self.get_object()
        if not notification.is_read:
            notification.is_read = True
            notification.read_at = timezone.now()
            notification.save(update_fields=['is_read', 'read_at'])
        return Response(self.get_serializer(notification).data)

    @swagger_auto_schema(tags=_TAGS, request_body=None)
    @action(detail=False, methods=['post'], url_path='mark_all_read')
    def mark_all_read(self, request):
        updated = self.get_queryset().filter(is_read=False).update(
            is_read=True, read_at=timezone.now(),
        )
        return Response({'updated': updated}, status=status.HTTP_200_OK)

    @swagger_auto_schema(tags=_TAGS)
    @action(detail=False, methods=['get'], url_path='unread_count')
    def unread_count(self, request):
        count = self.get_queryset().filter(is_read=False).count()
        return Response({'unread_count': count})

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description='Delete a single notification belonging to the current user.',
        responses={204: 'Deleted', 404: 'Not found'},
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description="Delete all of the current user's notifications.",
        responses={200: openapi.Response(
            description='Deletion summary',
            schema=openapi.Schema(
                type=openapi.TYPE_OBJECT,
                properties={'deleted': openapi.Schema(type=openapi.TYPE_INTEGER)},
            ),
        )},
    )
    @action(detail=False, methods=['delete'], url_path='delete_all')
    def delete_all(self, request):
        deleted, _ = self.get_queryset().delete()
        return Response({'deleted': deleted}, status=status.HTTP_200_OK)
