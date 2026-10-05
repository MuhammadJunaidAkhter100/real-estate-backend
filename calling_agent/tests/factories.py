from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from projects.models import Project, Unit
from users.models import Company, Lead, User


def create_company(name: str = 'Example Company') -> Company:
    return Company.objects.create(name=name)


def create_user(
    *,
    company: Company,
    email: str = 'agent@example.com',
    role: str = User.Role.AGENT,
) -> User:
    return User.objects.create_user(
        email=email,
        first_name='Test',
        last_name='User',
        password='test-password-123',
        company=company,
        role=role,
        status=User.Status.ACTIVE,
    )


def create_lead(
    *,
    user: User,
    name: str = 'Test Lead',
    phone_number: str = '+447911123456',
    scheduled_at: datetime | None = None,
) -> Lead:
    return Lead.objects.create(
        name=name,
        phone_no=phone_number,
        estimated_budget='250000.00',
        created_by=user,
        scheduled_at=scheduled_at,
    )


def create_project(
    *,
    user: User,
    title: str = 'Test Project',
    project_status: str = Project.ProjectStatus.LIVE,
) -> Project:
    return Project.objects.create(
        title=title,
        description='A residential project suitable for investors.',
        location='London',
        developer='Example Developer',
        status=Project.Status.IN_PROGRESS,
        project_status=project_status,
        starting_price=Decimal('200000.00'),
        yield_percentage=Decimal('6.50'),
        currency='GBP',
        project_type=Project.ProjectType.RESIDENTIAL,
        created_by=user,
    )


def create_unit(
    *,
    project: Project,
    label: str = 'A-101',
    price: Decimal = Decimal('225000.00'),
    status: str = Unit.UnitStatus.AVAILABLE,
    discounted_price: Decimal | None = None,
    est_yield_gross: Decimal | None = None,
) -> Unit:
    return Unit.objects.create(
        project=project,
        label=label,
        category='1 Bed',
        floor='1st Floor',
        area_m2=Decimal('65.00'),
        area_ft2=Decimal('699.65'),
        list_price=price,
        discounted_price=discounted_price,
        est_yield_gross=est_yield_gross,
        currency='GBP',
        status=status,
    )
