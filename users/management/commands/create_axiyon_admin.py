"""Create an `axiyon_admin` user with superadmin-scoped credentials.

The axiyon_admin role has the same effective permissions as a company_admin,
but is created under a superadmin-owned company and is managed by superadmin.

Usage:
    python manage.py create_axiyon_admin --email admin@axiyon.ai
    python manage.py create_axiyon_admin --email admin@axiyon.ai --company-name "Axiyon HQ"

The generated password is printed to the console once — store it safely.
"""
from django.core.management.base import BaseCommand, CommandError

from api.constants import AVAILABLE_COUNTRIES
from users.models import Company, User
from users.utils import generate_password


class Command(BaseCommand):
    help = "Create an axiyon_admin user (company_admin-level permissions) with generated credentials."

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True, help='Email for the axiyon_admin user.')
        parser.add_argument('--first-name', default='Axiyon', help='First name (default: Axiyon).')
        parser.add_argument('--last-name', default='Admin', help='Last name (default: Admin).')
        parser.add_argument('--company', type=int, help='Existing company ID to attach the user to.')
        parser.add_argument('--company-name', default='Axiyon Admin',
                            help='Company name for the axiyon_admin user. Defaults to "Axiyon Admin".')
        parser.add_argument('--password', help='Optional password for the axiyon_admin user. If omitted, a password is generated.')

    def handle(self, *args, **options):
        email = options['email'].strip().lower()

        if User.objects.filter(email=email).exists():
            raise CommandError(f"A user with email {email!r} already exists.")

        # Resolve / create the company
        company = None
        if options.get('company'):
            company = Company.objects.filter(pk=options['company']).first()
            if company is None:
                raise CommandError(f"Company with id {options['company']} not found.")
        elif options.get('company_name'):
            company, created = Company.objects.get_or_create(
                name=options['company_name'],
                defaults={'operating_countries': AVAILABLE_COUNTRIES},
            )
            if created:
                self.stdout.write(f"Created company: {company.name} (id={company.pk})")
        else:
            raise CommandError("Provide either --company <id> or --company-name <name>.")

        password = options.get('password') or generate_password()

        user = User.objects.create_user(
            email=email,
            first_name=options['first_name'],
            last_name=options['last_name'],
            password=password,
            role=User.Role.AXIYON_ADMIN,
            status=User.Status.ACTIVE,
            company=company,
            countries=AVAILABLE_COUNTRIES,
            is_staff=False,
            is_superuser=False,
        )

        self.stdout.write(self.style.SUCCESS("\naxiyon_admin user created successfully:"))
        self.stdout.write(f"  ID:       {user.pk}")
        self.stdout.write(f"  Email:    {user.email}")
        self.stdout.write(f"  Password: {password}")
        self.stdout.write(f"  Role:     {user.role}")
        self.stdout.write(f"  Company:  {company.name} (id={company.pk})")
        self.stdout.write(self.style.WARNING(
            "\nStore the password securely — it is shown only once."
        ))
