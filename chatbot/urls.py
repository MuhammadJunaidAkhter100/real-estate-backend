from django.urls import path

from chatbot.views import (
    ChatStreamView,
    ChatHistoryView,
    ChatSessionListView,
    ChatSessionDeleteView,
    KnowledgeBaseView,
    KnowledgeBaseDeleteView
)

urlpatterns = [
    path('stream/',                         ChatStreamView.as_view(),        name='chat-stream'),
    path('sessions/',                       ChatSessionListView.as_view(),   name='chat-sessions'),
    path('sessions/<int:session_id>/',      ChatSessionDeleteView.as_view(), name='chat-session-delete'),
    path('history/<int:session_id>/',       ChatHistoryView.as_view(),       name='chat-history'),
    path('knowledge-base/',                   KnowledgeBaseView.as_view(http_method_names=['get', 'post']), name='knowledge-base'),
    path('knowledge-base/<int:document_id>/', KnowledgeBaseDeleteView.as_view(http_method_names=['delete']),     name='knowledge-base-delete'),
]
