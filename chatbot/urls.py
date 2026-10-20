from django.urls import path

from chatbot.views import (
    ChatStreamView,
    PublicChatStreamView,
    ChatHistoryView,
    ChatSessionListView,
    ChatSessionDeleteView,
    KnowledgeBaseView,
    KnowledgeBaseDeleteView,
    KnowledgeBaseWebsiteView,
    AdminPublicKnowledgeBaseView,
    AdminPublicKnowledgeBaseDeleteView,
    AdminPublicKnowledgeBaseStatusView,
)

urlpatterns = [
    path('stream/',                         ChatStreamView.as_view(),        name='chat-stream'),
    path('public/stream/',                  PublicChatStreamView.as_view(),  name='chat-public-stream'),
    path('sessions/',                       ChatSessionListView.as_view(),   name='chat-sessions'),
    path('sessions/<int:session_id>/',      ChatSessionDeleteView.as_view(), name='chat-session-delete'),
    path('history/<int:session_id>/',       ChatHistoryView.as_view(),       name='chat-history'),
    path('knowledge-base/',                   KnowledgeBaseView.as_view(http_method_names=['get', 'post']), name='knowledge-base'),
    path('knowledge-base/<int:document_id>/', KnowledgeBaseDeleteView.as_view(http_method_names=['delete']),     name='knowledge-base-delete'),
    path('knowledge-base/website/',           KnowledgeBaseWebsiteView.as_view(http_method_names=['get', 'post']), name='knowledge-base-website'),
    path('admin/public-kb/',                  AdminPublicKnowledgeBaseView.as_view(http_method_names=['get', 'post']), name='admin-public-kb'),
    path('admin/public-kb/<int:document_id>/', AdminPublicKnowledgeBaseDeleteView.as_view(http_method_names=['delete']), name='admin-public-kb-delete'),
    path('admin/public-kb/<int:document_id>/status/', AdminPublicKnowledgeBaseStatusView.as_view(http_method_names=['get']), name='admin-public-kb-status'),
]

