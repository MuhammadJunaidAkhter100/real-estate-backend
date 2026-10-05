from django.urls import path

from new_proposal.views import (
    GeneratedProposalDetailView,
    GeneratedProposalDownloadView,
    NewProposalView,
    ProposalTaskStatusView,
)

urlpatterns = [
    path("", NewProposalView.as_view(), name="new-proposal"),
    path("task/<str:task_id>/", ProposalTaskStatusView.as_view(), name="new-proposal-task-status"),
    path("<int:pk>/download/", GeneratedProposalDownloadView.as_view(), name="new-proposal-download"),
    path("<int:pk>/", GeneratedProposalDetailView.as_view(), name="new-proposal-detail"),
]
