from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction

from projects.models import Project, ProjectAgentAssignment, Unit
from users.models import Company, Lead, Task, Team, User

PASSWORD = 'Test@1234'


class Command(BaseCommand):
    help = 'Seeds the database with test users, companies, projects, leads, and tasks.'

    @transaction.atomic
    def handle(self, *args, **options):
        self.stdout.write('Seeding test data...')

        # ── Superadmin ──────────────────────────────────────────────────────
        superadmin_company, _ = Company.objects.get_or_create(
            name='Axiyon HQ',
            defaults={'operating_countries': ['UK', 'UAE']},
        )
        superadmin = self._create_user(
            email='superadmin@axiyon.com',
            first_name='Super',
            last_name='Admin',
            role=User.Role.SUPERADMIN,
            company=superadmin_company,
            is_staff=True,
        )

        # ── Company A ───────────────────────────────────────────────────────
        company_a, _ = Company.objects.get_or_create(
            name='Alpha Real Estate',
            defaults={'operating_countries': ['UK', 'UAE', 'USA']},
        )
        admin_a = self._create_user(
            email='admin@alpha.com',
            first_name='Alice',
            last_name='Admin',
            role=User.Role.COMPANY_ADMIN,
            company=company_a,
        )
        team_a, _ = Team.objects.get_or_create(name='Alpha Sales Team', company=company_a)
        manager_a = self._create_user(
            email='manager@alpha.com',
            first_name='Mark',
            last_name='Manager',
            role=User.Role.TEAM_MANAGER,
            company=company_a,
            team=team_a,
        )
        # Team.manager is a OneToOneField
        if not hasattr(team_a, 'manager') or team_a.manager_id != manager_a.id:
            team_a.manager = manager_a
            team_a.save()

        agent_a1 = self._create_user(
            email='agent1@alpha.com',
            first_name='Anna',
            last_name='Agent',
            role=User.Role.AGENT,
            company=company_a,
            team=team_a,
        )
        agent_a2 = self._create_user(
            email='agent2@alpha.com',
            first_name='Adam',
            last_name='Smith',
            role=User.Role.AGENT,
            company=company_a,
            team=team_a,
        )

        # ── Company B ───────────────────────────────────────────────────────
        company_b, _ = Company.objects.get_or_create(
            name='Beta Properties',
            defaults={'operating_countries': ['UAE', 'Singapore']},
        )
        admin_b = self._create_user(
            email='admin@beta.com',
            first_name='Bob',
            last_name='Admin',
            role=User.Role.COMPANY_ADMIN,
            company=company_b,
        )
        team_b, _ = Team.objects.get_or_create(name='Beta Deals Team', company=company_b)
        manager_b = self._create_user(
            email='manager@beta.com',
            first_name='Maria',
            last_name='Manager',
            role=User.Role.TEAM_MANAGER,
            company=company_b,
            team=team_b,
        )
        if not hasattr(team_b, 'manager') or team_b.manager_id != manager_b.id:
            team_b.manager = manager_b
            team_b.save()

        agent_b1 = self._create_user(
            email='agent1@beta.com',
            first_name='Ben',
            last_name='Agent',
            role=User.Role.AGENT,
            company=company_b,
            team=team_b,
        )

        # ── Projects ─────────────────────────────────────────────────────────
        project1 = self._create_project(
            title='London House',
            location='Preston, UK',
            developer='Prestige Homes',
            project_type=Project.ProjectType.RESIDENTIAL,
            status=Project.Status.READY,
            starting_price=175376,
            yield_pct=6.5,
            currency='GBP',
            created_by=superadmin,
            companies=[company_a, company_b],
            units=[
                {'label': 'Unit 101', 'category': '1 Bed', 'floor': 'Ground Floor', 'list_price': 175376, 'area_ft2': 520},
                {'label': 'Unit 102', 'category': '2 Bed', 'floor': 'Ground Floor', 'list_price': 210000, 'area_ft2': 750},
                {'label': 'Unit 201', 'category': '1 Bed', 'floor': '2nd Floor', 'list_price': 180000, 'area_ft2': 520},
            ],
        )
        project2 = self._create_project(
            title='Dubai Marina Tower',
            location='Dubai, UAE',
            developer='Emirates Developers',
            project_type=Project.ProjectType.RESIDENTIAL,
            status=Project.Status.IN_PROGRESS,
            starting_price=320000,
            yield_pct=7.2,
            currency='AED',
            created_by=superadmin,
            companies=[company_a, company_b],
            units=[
                {'label': 'Studio A', 'category': 'Studio', 'floor': '5th Floor', 'list_price': 320000, 'area_ft2': 400},
                {'label': 'Unit 501', 'category': '1 Bed', 'floor': '5th Floor', 'list_price': 450000, 'area_ft2': 650},
            ],
        )
        project3 = self._create_project(
            title='Singapore Commercial Hub',
            location='Singapore',
            developer='SGP Capital',
            project_type=Project.ProjectType.COMMERCIAL,
            status=Project.Status.PLANNED,
            starting_price=500000,
            yield_pct=5.8,
            currency='SGD',
            created_by=superadmin,
            companies=[company_b],
            units=[],
        )

        # ── Agent assignments ─────────────────────────────────────────────
        for agent, split in [(agent_a1, 70), (agent_a2, 65)]:
            ProjectAgentAssignment.objects.get_or_create(
                agent=agent, project=project1,
                defaults={'agent_split': split, 'company_split': 100 - split},
            )
        ProjectAgentAssignment.objects.get_or_create(
            agent=agent_b1, project=project2,
            defaults={'agent_split': 60, 'company_split': 40},
        )

        # ── Leads ─────────────────────────────────────────────────────────
        leads_a1 = [
            self._create_lead('James Wilson', 'james@email.com', '+44-7700-900001', Lead.Status.NEW, 150000, 'UK', company_a, agent_a1, project1),
            self._create_lead('Sarah Connor', 'sarah@email.com', '+44-7700-900002', Lead.Status.CONTACTED, 200000, 'UK', company_a, agent_a1, project1),
            self._create_lead('Michael Lee', 'michael@email.com', '+971-50-1234567', Lead.Status.INTERESTED, 320000, 'UAE', company_a, agent_a1, project2),
            self._create_lead('Emily Brown', 'emily@email.com', '+1-555-0101', Lead.Status.NEGOTIATION_ONGOING, 450000, 'USA', company_a, agent_a1, project2),
        ]
        leads_a2 = [
            self._create_lead('David Park', 'david@email.com', '+44-7700-900003', Lead.Status.NEW, 180000, 'UK', company_a, agent_a2, project1),
            self._create_lead('Laura Martinez', 'laura@email.com', '+44-7700-900004', Lead.Status.INTERESTED, 210000, 'UK', company_a, agent_a2, project1),
        ]
        leads_b1 = [
            self._create_lead('Chen Wei', 'chen@email.com', '+65-9123-4567', Lead.Status.CONTACTED, 500000, 'Singapore', company_b, agent_b1, project3),
            self._create_lead('Aisha Malik', 'aisha@email.com', '+971-55-9876543', Lead.Status.NEGOTIATION_ONGOING, 380000, 'UAE', company_b, agent_b1, project2),
            self._create_lead('Omar Hassan', 'omar@email.com', '+971-50-1111111', Lead.Status.NEW, 290000, 'UAE', company_b, agent_b1, project2),
        ]

        # ── Tasks ─────────────────────────────────────────────────────────
        today = date.today()
        self._create_task('Follow up with James Wilson', Task.Priority.HIGH, today + timedelta(days=2), Task.Status.PENDING, agent_a1, leads_a1[0])
        self._create_task('Send proposal to Sarah Connor', Task.Priority.MEDIUM, today + timedelta(days=5), Task.Status.IN_PROGRESS, agent_a1, leads_a1[1])
        self._create_task('Schedule viewing for Michael Lee', Task.Priority.HIGH, today + timedelta(days=1), Task.Status.PENDING, agent_a1, leads_a1[2])
        self._create_task('Negotiate contract with Emily Brown', Task.Priority.HIGH, today + timedelta(days=3), Task.Status.IN_PROGRESS, agent_a1, leads_a1[3])
        self._create_task('Send brochure to David Park', Task.Priority.LOW, today + timedelta(days=7), Task.Status.PENDING, agent_a2, leads_a2[0])
        self._create_task('Arrange site visit for Laura Martinez', Task.Priority.MEDIUM, today + timedelta(days=4), Task.Status.PENDING, agent_a2, leads_a2[1])
        self._create_task('Follow up Chen Wei on Singapore Hub', Task.Priority.HIGH, today + timedelta(days=2), Task.Status.PENDING, agent_b1, leads_b1[0])
        self._create_task('Send payment plan to Aisha Malik', Task.Priority.MEDIUM, today + timedelta(days=6), Task.Status.IN_PROGRESS, agent_b1, leads_b1[1])

        self.stdout.write(self.style.SUCCESS('\nTest data seeded successfully!\n'))
        self.stdout.write('─' * 50)
        self.stdout.write(f'Password for all users: {PASSWORD}\n')
        self.stdout.write('─' * 50)
        self.stdout.write('SUPERADMIN')
        self.stdout.write(f'  superadmin@axiyon.com\n')
        self.stdout.write('COMPANY A (Alpha Real Estate)')
        self.stdout.write('  admin@alpha.com       → company_admin')
        self.stdout.write('  manager@alpha.com     → team_manager')
        self.stdout.write('  agent1@alpha.com      → agent (4 leads, 4 tasks)')
        self.stdout.write('  agent2@alpha.com      → agent (2 leads, 2 tasks)')
        self.stdout.write('COMPANY B (Beta Properties)')
        self.stdout.write('  admin@beta.com        → company_admin')
        self.stdout.write('  manager@beta.com      → team_manager')
        self.stdout.write('  agent1@beta.com       → agent (3 leads, 2 tasks)')
        self.stdout.write('─' * 50)

    def _create_user(self, email, first_name, last_name, role, company, team=None, is_staff=False):
        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                'first_name': first_name,
                'last_name': last_name,
                'role': role,
                'company': company,
                'team': team,
                'status': User.Status.ACTIVE,
                'is_staff': is_staff,
                'countries': list(company.operating_countries),
                'current_country': company.operating_countries[0] if company.operating_countries else '',
            },
        )
        if created:
            user.set_password(PASSWORD)
            user.save()
        return user

    def _create_project(self, title, location, developer, project_type, status, starting_price, yield_pct, currency, created_by, companies, units):
        project, _ = Project.objects.get_or_create(
            title=title,
            defaults={
                'location': location,
                'developer': developer,
                'project_type': project_type,
                'status': status,
                'starting_price': starting_price,
                'yield_percentage': yield_pct,
                'currency': currency,
                'description': f'{title} — a premium {project_type} development in {location}.',
                'created_by': created_by,
                'image': [],
            },
        )
        for company in companies:
            project.visible_to_companies.add(company)

        for u in units:
            Unit.objects.get_or_create(
                project=project,
                label=u['label'],
                defaults={
                    'category': u['category'],
                    'floor': u.get('floor', ''),
                    'list_price': u['list_price'],
                    'area_ft2': u.get('area_ft2'),
                    'currency': project.currency,
                    'created_by': created_by,
                },
            )
        return project

    def _create_lead(self, name, email, phone, status, budget, country, company, agent, project):
        lead, _ = Lead.objects.get_or_create(
            email=email,
            company=company,
            defaults={
                'name': name,
                'phone_no': phone,
                'status': status,
                'estimated_budget': budget,
                'country': country,
                'source': 'test_seed',
                'created_by': agent,
                'assigned_to': agent,
                'project': project,
            },
        )
        return lead

    def _create_task(self, name, priority, due_date, status, agent, lead=None):
        Task.objects.get_or_create(
            name=name,
            created_by=agent,
            defaults={
                'priority': priority,
                'due_date': due_date,
                'status': status,
                'related_lead': lead,
            },
        )
