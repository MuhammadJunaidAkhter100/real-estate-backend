"""One-off cleanup: delete projects 4, 5, 6 and everything hanging off them.

Run manually, review the printed inventory first. Nothing here alters the
schema -- no migration is created and no table is renamed or dropped. Rows are
removed only.

Deletes, in FK-safe order:
  - notifications_notification rows whose jsonb ``data`` references only these
    projects (jsonb has no FK, so Django leaves these behind)
  - users_lead rows whose project_id is in TARGET_IDS (Lead.project is CASCADE)
  - users_lead_projects M2M rows
    - users_lead_units M2M rows
  - users_agentcommission rows
  - new_proposal_generatedproposal rows
  - projects_promotion rows
  - projects_projectagentassignment rows
  - projects_projectdocument rows
  - projects_unit rows
  - projects_project_visible_to_companies rows
  - projects_project rows

Hard-refuses if the project table does not hold exactly TARGET_IDS, so it can
never touch anything else. Runs in a single transaction and rolls back on any
error.

Usage:
    cd C:\\Users\\arbz0\\Desktop\\FullStackRealEstate\\real_estate_ai_backend
    & C:\\rvenv\\Scripts\\python.exe .\\delete_projects_456.py

Then delete this file so it is not committed:
    Remove-Item .\\delete_projects_456.py -Force
"""

import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "api.settings")
django.setup()

from django.db import connection, transaction  # noqa: E402

from projects.models import (  # noqa: E402
    Project,
    ProjectAgentAssignment,
    ProjectDocument,
    Promotion,
    Unit,
)
from new_proposal.models import GeneratedProposal  # noqa: E402
from notifications.models import Notification  # noqa: E402
from users.models import AgentCommission, Lead  # noqa: E402

TARGET_IDS = [4, 5, 6]

# Notifications have no FK to Project; their jsonb "data" blob carries
# project_id. Only rows that reference one of the targets AND reference
# nothing else are removed.
NOTIFICATION_SQL = """
    SELECT id, type, left(title, 46) AS title, recipient_id, data::text
    FROM notifications_notification
    WHERE data ? 'project_id'
      AND (data->>'project_id')::int = ANY(%s)
    ORDER BY id
"""


def collect_notification_ids():
    """Notifications whose jsonb ``data`` references a target project.

    The blob has no FK, so Django cannot cascade it. Only rows whose
    ``project_id`` is a target are taken; the reported ``data`` is shown in the
    pre-flight output so anything unexpected is visible before deleting.
    """
    with connection.cursor() as cursor:
        cursor.execute(NOTIFICATION_SQL, [TARGET_IDS])
        return cursor.fetchall()


def build_report(notification_rows):
    return [
        (
            "projects_project (TARGETS)",
            Project.objects.filter(id__in=TARGET_IDS).count(),
        ),
        ("projects_unit", Unit.objects.filter(project_id__in=TARGET_IDS).count()),
        ("users_lead (CASCADE)", Lead.objects.filter(project_id__in=TARGET_IDS).count()),
        (
            "users_lead_projects (M2M)",
            Lead.projects.through.objects.filter(project_id__in=TARGET_IDS).count(),
        ),
        (
            "users_lead_units (M2M)",
            Lead.units.through.objects.filter(
                unit__project_id__in=TARGET_IDS
            ).count(),
        ),
        (
            "users_agentcommission",
            AgentCommission.objects.filter(project_id__in=TARGET_IDS).count(),
        ),
        (
            "new_proposal_generatedproposal",
            GeneratedProposal.objects.filter(project_id__in=TARGET_IDS).count(),
        ),
        (
            "projects_promotion",
            Promotion.objects.filter(project_id__in=TARGET_IDS).count(),
        ),
        (
            "projects_projectagentassignment",
            ProjectAgentAssignment.objects.filter(project_id__in=TARGET_IDS).count(),
        ),
        (
            "projects_projectdocument",
            ProjectDocument.objects.filter(project_id__in=TARGET_IDS).count(),
        ),
        (
            "projects_project_visible_to_companies",
            Project.visible_to_companies.through.objects.filter(
                project_id__in=TARGET_IDS
            ).count(),
        ),
        ("notifications_notification (jsonb)", len(notification_rows)),
    ]


def print_report(notification_rows):
    total_project_rows = Project.objects.count()
    actual_ids = sorted(Project.objects.values_list("id", flat=True))

    print("=" * 74)
    print("PRE-FLIGHT")
    print("=" * 74)
    print(f"Target project ids          : {TARGET_IDS}")
    print(f"Total rows in projects_project : {total_project_rows}")
    print(f"All project ids present    : {actual_ids}")

    if total_project_rows != len(TARGET_IDS) or actual_ids != sorted(TARGET_IDS):
        print()
        print("REFUSING TO RUN.")
        print(
            "projects_project must contain exactly the target projects and "
            "nothing else."
        )
        print(
            "Refusing so no unexpected row can ever be deleted. "
            "Re-inspect the table and update TARGET_IDS deliberately if the "
            "data changed."
        )
        sys.exit(1)

    print()
    print("Projects to delete:")
    for project in Project.objects.filter(id__in=TARGET_IDS).order_by("id"):
        print(
            f"  [{project.id}] {project.title!r}  "
            f"status={project.project_status}  created_by_id={project.created_by_id}"
        )

    print()
    print("Leads that will be deleted (Lead.project is CASCADE):")
    for lead in Lead.objects.filter(project_id__in=TARGET_IDS).order_by("id"):
        print(
            f"  [{lead.id}] {lead.name!r} <{lead.email}>  "
            f"status={lead.status}  project_id={lead.project_id}"
        )

    print()
    print("Notifications to delete (jsonb soft references, no FK):")
    if notification_rows:
        for row in notification_rows:
            print(f"  [{row[0]}] {row[1]}  {row[2]!r}  -> recipient {row[3]}")
            print(f"        data: {row[4]}")
    else:
        print("  (none)")

    print()
    print("Rows that will be removed:")
    print("-" * 74)
    report = build_report(notification_rows)
    grand_total = 0
    for label, count in report:
        grand_total += count
        print(f"  {label:<46} {count:>4}")
    print("-" * 74)
    print(f"  {'TOTAL':<46} {grand_total:>4}")
    print()


def run_deletion(notification_rows):
    notification_ids = [row[0] for row in notification_rows]

    with transaction.atomic():
        deleted = {}

        if notification_ids:
            deleted["notifications_notification"] = Notification.objects.filter(
                id__in=notification_ids
            ).delete()[0]

        deleted["users_lead_projects"] = (
            Lead.projects.through.objects.filter(project_id__in=TARGET_IDS).delete()[0]
        )
        deleted["users_lead_units"] = (
            Lead.units.through.objects.filter(
                unit__project_id__in=TARGET_IDS
            ).delete()[0]
        )
        deleted["users_agentcommission"] = AgentCommission.objects.filter(
            project_id__in=TARGET_IDS
        ).delete()[0]
        deleted["new_proposal_generatedproposal"] = GeneratedProposal.objects.filter(
            project_id__in=TARGET_IDS
        ).delete()[0]
        deleted["projects_promotion"] = Promotion.objects.filter(
            project_id__in=TARGET_IDS
        ).delete()[0]
        deleted["projects_projectagentassignment"] = (
            ProjectAgentAssignment.objects.filter(project_id__in=TARGET_IDS).delete()[0]
        )
        deleted["projects_projectdocument"] = ProjectDocument.objects.filter(
            project_id__in=TARGET_IDS
        ).delete()[0]
        deleted["projects_unit"] = Unit.objects.filter(
            project_id__in=TARGET_IDS
        ).delete()[0]
        deleted["users_lead"] = Lead.objects.filter(
            project_id__in=TARGET_IDS
        ).delete()[0]
        deleted["projects_project_visible_to_companies"] = (
            Project.visible_to_companies.through.objects.filter(
                project_id__in=TARGET_IDS
            ).delete()[0]
        )
        deleted["projects_project"] = Project.objects.filter(
            id__in=TARGET_IDS
        ).delete()[0]

        return deleted


def verify():
    print("=" * 74)
    print("POST-DELETE VERIFICATION")
    print("=" * 74)

    checks = [
        ("projects_project", Project.objects.filter(id__in=TARGET_IDS).count()),
        ("projects_unit", Unit.objects.filter(project_id__in=TARGET_IDS).count()),
        ("users_lead", Lead.objects.filter(project_id__in=TARGET_IDS).count()),
        (
            "users_lead_projects",
            Lead.projects.through.objects.filter(project_id__in=TARGET_IDS).count(),
        ),
        (
            "users_lead_units",
            Lead.units.through.objects.filter(
                unit__project_id__in=TARGET_IDS
            ).count(),
        ),
        (
            "users_agentcommission",
            AgentCommission.objects.filter(project_id__in=TARGET_IDS).count(),
        ),
        (
            "new_proposal_generatedproposal",
            GeneratedProposal.objects.filter(project_id__in=TARGET_IDS).count(),
        ),
        (
            "projects_promotion",
            Promotion.objects.filter(project_id__in=TARGET_IDS).count(),
        ),
        (
            "projects_projectagentassignment",
            ProjectAgentAssignment.objects.filter(project_id__in=TARGET_IDS).count(),
        ),
        (
            "projects_projectdocument",
            ProjectDocument.objects.filter(project_id__in=TARGET_IDS).count(),
        ),
        (
            "projects_project_visible_to_companies",
            Project.visible_to_companies.through.objects.filter(
                project_id__in=TARGET_IDS
            ).count(),
        ),
    ]

    failures = []
    for label, count in checks:
        status = "OK" if count == 0 else "FAIL"
        if count != 0:
            failures.append(label)
        print(f"  {status:<5} {label:<46} remaining={count}")

    remaining_notifications = collect_notification_ids()
    if remaining_notifications:
        failures.append("notifications_notification")
        print(
            f"  {'FAIL':<5} notifications_notification                  "
            f"remaining={len(remaining_notifications)}"
        )
    else:
        print(
            f"  {'OK':<5} notifications_notification                  remaining=0"
        )

    print()
    print(f"Total projects left in table: {Project.objects.count()}")
    print(f"Total leads left in table   : {Lead.objects.count()}")

    if failures:
        print()
        print("VERIFICATION FAILED:", ", ".join(failures))
        print("The transaction has already been rolled back.")
        sys.exit(1)

    print()
    print("VERIFICATION PASSED - all target rows removed.")


def main():
    notification_rows = collect_notification_ids()
    print_report(notification_rows)

    print("=" * 74)
    print("This is permanent. The 9 leads and 7 notifications listed above")
    print("will be deleted along with projects 4, 5, 6.")
    print("=" * 74)

    if "--yes" not in sys.argv:
        answer = input("\nType 'DELETE 4 5 6' to proceed: ").strip()
        if answer != "DELETE 4 5 6":
            print("Aborted. Nothing was deleted.")
            sys.exit(0)

    try:
        deleted = run_deletion(notification_rows)
    except Exception as exc:  # noqa: BLE001
        print(f"\nERROR: {exc}")
        print("Transaction rolled back. Nothing was deleted.")
        sys.exit(1)

    print()
    print("Deleted row counts (Django returns related cascades in these numbers):")
    for table, count in deleted.items():
        print(f"  {table:<46} {count}")

    verify()


if __name__ == "__main__":
    main()