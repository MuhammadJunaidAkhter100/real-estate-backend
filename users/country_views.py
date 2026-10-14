"""Countries a company works in, managed by the company's own admins.

GET    /api/users/countries/                     every supported country, with its currency
GET    /api/users/company/countries/             the countries of the caller's company
POST   /api/users/company/countries/             add one   {"country": "Andorra"}
DELETE /api/users/company/countries/<country>/   remove one (name or ISO code)

A country is stored by name, as everywhere else in the app. UAE and the UK
keep the short names the app has always used ("UAE", "UK").
"""
import unicodedata

from django.contrib.auth import get_user_model
from django.db import transaction
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from api.constants import (
    COUNTRY_ALIASES,
    COUNTRY_CODE_MAP,
    COUNTRY_CURRENCY_MAP,
    COUNTRY_DATA,
    COUNTRY_DIAL_CODE_MAP,
    COUNTRY_NAME_BY_CODE,
)
from users.models import Company
from users.permissions import IsCompanyAdmin

User = get_user_model()

_TAGS = ['Company countries']

# "United Kingdom" -> "UK", "United Arab Emirates" -> "UAE".
_SHORT_NAME = {full: short for short, full in COUNTRY_ALIASES.items()}
_FULL_NAME_BY_LOWER = {name.lower(): name for name in COUNTRY_CODE_MAP}
_FULL_NAME_BY_LOWER.update({alias.lower(): full for alias, full in COUNTRY_ALIASES.items()})
# Other spellings older data and the frontend use.
_FULL_NAME_BY_LOWER.update({
    'usa': 'United States',
    'united states of america': 'United States',
    'great britain': 'United Kingdom',
    'emirates': 'United Arab Emirates',
})


def _full_name(value):
    """The country list's name for `value` (a name, short name or ISO code), or ''."""
    raw = str(value or '').strip()
    if len(raw) == 2 and raw.upper() in COUNTRY_NAME_BY_CODE:
        return COUNTRY_NAME_BY_CODE[raw.upper()]
    return _FULL_NAME_BY_LOWER.get(raw.lower(), '')


def resolve_country(value):
    """The name to store for `value`, or '' when it is not a supported country."""
    full = _full_name(value)
    return _SHORT_NAME.get(full, full)


def same_country(first, second):
    """True when two stored values name the same country ("UK" and "United Kingdom")."""
    first_full, second_full = _full_name(first), _full_name(second)
    if first_full and second_full:
        return first_full == second_full
    return str(first or '').strip().lower() == str(second or '').strip().lower()


def country_entry(value):
    full = _full_name(value)
    return {
        'name': _SHORT_NAME.get(full, full) or str(value),
        'code': COUNTRY_CODE_MAP.get(full, ''),
        'dial_code': COUNTRY_DIAL_CODE_MAP.get(full, ''),
        'currency': COUNTRY_CURRENCY_MAP.get(full, ''),
    }


def _sort_key(entry):
    # "Côte d'Ivoire" sorts as "Cote d'Ivoire", next to Costa Rica.
    plain = unicodedata.normalize('NFKD', entry['name']).encode('ascii', 'ignore').decode()
    return plain.lower()


ALL_COUNTRIES = sorted(
    (country_entry(country['name']) for country in COUNTRY_DATA),
    key=_sort_key,
)

_COUNTRY_SCHEMA = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    properties={
        'name': openapi.Schema(type=openapi.TYPE_STRING, example='Andorra'),
        'code': openapi.Schema(type=openapi.TYPE_STRING, example='AD'),
        'dial_code': openapi.Schema(type=openapi.TYPE_STRING, example='+376'),
        'currency': openapi.Schema(type=openapi.TYPE_STRING, example='EUR'),
    },
)
_COMPANY_COUNTRIES_SCHEMA = openapi.Schema(
    type=openapi.TYPE_OBJECT,
    properties={
        'countries': openapi.Schema(type=openapi.TYPE_ARRAY, items=_COUNTRY_SCHEMA),
        'user': openapi.Schema(
            type=openapi.TYPE_OBJECT,
            description='The caller after the change (add / remove only).',
            properties={
                'countries': openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Schema(type=openapi.TYPE_STRING)),
                'current_country': openapi.Schema(type=openapi.TYPE_STRING),
            },
        ),
    },
)

_ADMIN_ROLES = (User.Role.COMPANY_ADMIN, User.Role.AXIYON_ADMIN)


def _company_payload(company, user=None):
    # One entry per country, even when it is stored twice ("UK" and "United Kingdom").
    entries, seen = [], set()
    for stored in company.operating_countries or []:
        entry = country_entry(stored)
        key = entry['code'] or entry['name'].lower()
        if key not in seen:
            seen.add(key)
            entries.append(entry)
    payload = {'countries': entries}
    if user is not None:
        # The caller's own countries change too; send them so the app can
        # update its country switcher without reloading the profile.
        user.refresh_from_db(fields=['countries', 'current_country'])
        payload['user'] = {
            'countries': list(user.countries or []),
            'current_country': user.current_country,
        }
    return payload


def _no_company():
    return Response(
        {'detail': 'Your user account is not linked to a company.'},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _is_platform_company(company):
    """The superadmin's own company lists every country; it is not edited here."""
    return company.users.filter(role=User.Role.SUPERADMIN).exists()


def _platform_company_refused():
    return Response(
        {'detail': "This company's countries cannot be changed here."},
        status=status.HTTP_403_FORBIDDEN,
    )


class CountryListView(APIView):
    """Every country the app supports, with its ISO code, dial code and currency."""
    # Public data: a missing, expired or unpaid-company token must not get in
    # the way (billing/guards.py exempts this path too).
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description=(
            'Every supported country, sorted by name: `name` (the value to send '
            'and store), ISO `code`, `dial_code` and `currency` (ISO 4217). '
            'No login needed.'
        ),
        responses={200: openapi.Schema(type=openapi.TYPE_ARRAY, items=_COUNTRY_SCHEMA)},
    )
    def get(self, request):
        return Response(ALL_COUNTRIES)


class CompanyCountriesView(APIView):
    """The countries of the caller's company; company admins can add one."""

    def get_permissions(self):
        if self.request.method == 'POST':
            return [permissions.IsAuthenticated(), IsCompanyAdmin()]
        return [permissions.IsAuthenticated()]

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description="The countries your company works in, with their currency.",
        responses={200: _COMPANY_COUNTRIES_SCHEMA, 400: 'Not linked to a company'},
    )
    def get(self, request):
        company = request.user.company
        if company is None:
            return _no_company()
        return Response(_company_payload(company))

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description=(
            "Company admins only. Adds a country to your company. `country` is a "
            "name from GET /api/users/countries/ or an ISO code (\"AD\"). The "
            "company's admins get the country too, so they can switch to it."
        ),
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['country'],
            properties={'country': openapi.Schema(type=openapi.TYPE_STRING, example='Andorra')},
        ),
        responses={201: _COMPANY_COUNTRIES_SCHEMA, 400: 'Unknown country or already added', 403: 'Not a company admin'},
    )
    def post(self, request):
        if request.user.company_id is None:
            return _no_company()

        raw = request.data.get('country') if hasattr(request.data, 'get') else None
        name = resolve_country(raw)
        if not name:
            return Response(
                {'country': ['Choose a country from the country list.']},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            company = Company.objects.select_for_update().get(pk=request.user.company_id)
            if _is_platform_company(company):
                return _platform_company_refused()
            current = list(company.operating_countries or [])
            if any(same_country(existing, name) for existing in current):
                return Response(
                    {'country': [f'Your company already works in {name}.']},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            company.operating_countries = [*current, name]
            company.save(update_fields=['operating_countries', 'updated_at'])

            # Company admins work in every company country, as they do from signup.
            admins = company.users.select_for_update().filter(
                role__in=[User.Role.COMPANY_ADMIN, User.Role.AXIYON_ADMIN],
            )
            for admin in admins:
                countries = list(admin.countries or [])
                if not any(same_country(existing, name) for existing in countries):
                    admin.countries = [*countries, name]
                    admin.save(update_fields=['countries'])

        return Response(_company_payload(company, request.user), status=status.HTTP_201_CREATED)


class CompanyCountryDetailView(APIView):
    """Company admins remove a country from their company."""
    permission_classes = [permissions.IsAuthenticated, IsCompanyAdmin]

    @swagger_auto_schema(
        tags=_TAGS,
        operation_description=(
            "Company admins only. Removes a country (name or ISO code in the URL, "
            "e.g. /company/countries/Andorra/ or /company/countries/AD/). It is "
            "also taken off every user of the company; a user who was working in "
            "it moves to their first remaining country. Refused for the last "
            "country, and while some user has no other country. Projects and "
            "leads of that country are kept; they only stop showing in the "
            "country switcher."
        ),
        responses={
            200: _COMPANY_COUNTRIES_SCHEMA,
            400: 'Last country, or users who only have this country',
            403: 'Not a company admin',
            404: 'Your company does not have this country',
        },
    )
    def delete(self, request, country):
        if request.user.company_id is None:
            return _no_company()

        name = resolve_country(country) or str(country).strip()

        with transaction.atomic():
            company = Company.objects.select_for_update().get(pk=request.user.company_id)
            if _is_platform_company(company):
                return _platform_company_refused()
            current = list(company.operating_countries or [])
            remaining = [existing for existing in current if not same_country(existing, name)]
            if len(remaining) == len(current):
                return Response(
                    {'detail': f'Your company does not work in {name}.'},
                    status=status.HTTP_404_NOT_FOUND,
                )
            if not remaining:
                return Response(
                    {'detail': 'A company needs at least one country.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            users = list(company.users.select_for_update())
            # Admins are never left without a country: they get the remaining
            # ones below. Anyone else must have another country first.
            stranded = [
                user.email for user in users
                if user.role not in _ADMIN_ROLES
                and user.countries
                and all(same_country(existing, name) for existing in user.countries)
            ]
            if stranded:
                return Response(
                    {
                        'detail': (
                            f'Some users only work in {name}. Give them another '
                            'country first, then remove it.'
                        ),
                        'users': stranded,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            company.operating_countries = remaining
            company.save(update_fields=['operating_countries', 'updated_at'])

            for user in users:
                changed = []
                countries = list(user.countries or [])
                if user.role in _ADMIN_ROLES:
                    # Admins work in every company country (older admins may
                    # have been given fewer).
                    missing = [
                        country for country in remaining
                        if not any(same_country(existing, country) for existing in countries)
                    ]
                    if missing:
                        countries = countries + missing
                        user.countries = countries
                        changed.append('countries')
                if any(same_country(existing, name) for existing in countries):
                    countries = [existing for existing in countries if not same_country(existing, name)]
                    user.countries = countries
                    changed.append('countries')
                if user.current_country and same_country(user.current_country, name):
                    user.current_country = countries[0] if countries else ''
                    changed.append('current_country')
                if changed:
                    user.save(update_fields=changed)

        return Response(_company_payload(company, request.user))
