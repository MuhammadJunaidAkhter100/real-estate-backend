"""
Zoho Leads Filtering Module
Provides comprehensive filtering capabilities and serializers for Zoho CRM leads response data.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime
import logging
from rest_framework import serializers

logger = logging.getLogger(__name__)


class ZohoLeadQuerySerializer(serializers.Serializer):
    """
    Serializer for Swagger query parameter documentation & validation.
    """
    search = serializers.CharField(required=False, help_text="Search across name, email, phone, mobile, company, lead source, country, city, owner")
    id = serializers.CharField(required=False, help_text="Filter by Lead ID")
    first_name = serializers.CharField(required=False, help_text="Filter by First Name")
    last_name = serializers.CharField(required=False, help_text="Filter by Last Name")
    name = serializers.CharField(required=False, help_text="Filter by Full Name")
    email = serializers.CharField(required=False, help_text="Filter by Email address")
    phone = serializers.CharField(required=False, help_text="Filter by Phone number")
    mobile = serializers.CharField(required=False, help_text="Filter by Mobile number")
    company = serializers.CharField(required=False, help_text="Filter by Company name")
    lead_status = serializers.CharField(required=False, help_text="Filter by Lead Status")
    lead_source = serializers.CharField(required=False, help_text="Filter by Lead Source")
    city = serializers.CharField(required=False, help_text="Filter by City")
    state = serializers.CharField(required=False, help_text="Filter by State")
    country = serializers.CharField(required=False, help_text="Filter by Country")
    designation = serializers.CharField(required=False, help_text="Filter by Designation")
    industry = serializers.CharField(required=False, help_text="Filter by Industry")
    website = serializers.CharField(required=False, help_text="Filter by Website")
    annual_revenue = serializers.CharField(required=False, help_text="Filter by Annual Revenue")
    owner_name = serializers.CharField(required=False, help_text="Filter by Owner Name")
    owner_email = serializers.CharField(required=False, help_text="Filter by Owner Email")
    owner_id = serializers.CharField(required=False, help_text="Filter by Owner ID")
    created_after = serializers.CharField(required=False, help_text="Filter created after date (YYYY-MM-DD)")
    created_before = serializers.CharField(required=False, help_text="Filter created before date (YYYY-MM-DD)")
    modified_after = serializers.CharField(required=False, help_text="Filter modified after date (YYYY-MM-DD)")
    modified_before = serializers.CharField(required=False, help_text="Filter modified before date (YYYY-MM-DD)")
    page = serializers.IntegerField(required=False, default=1, help_text="Page number (default 1)")
    per_page = serializers.IntegerField(required=False, default=200, help_text="Leads per page (default 200)")


def build_zoho_criteria(query_params: Any) -> Optional[str]:
    """
    Build Zoho CRM v8 API criteria string for server-side search across all 10,000+ records.
    Example output: "((Country:equals:SA)and(Lead_Source:equals:Magnate Assets))"
    """
    params_dict = {}
    if hasattr(query_params, 'items'):
        for k, v in query_params.items():
            if v is not None and str(v).strip() != "":
                params_dict[str(k).lower().strip()] = str(v).strip()

    conditions = []

    field_mappings = [
        (('country', 'Country'), 'equals', 'Country'),
        (('lead_source', 'source', 'Lead_Source'), 'equals', 'Lead_Source'),
        (('lead_status', 'status', 'Lead_Status'), 'equals', 'Lead_Status'),
        (('company', 'Company'), 'starts_with', 'Company'),
        (('email', 'Email'), 'equals', 'Email'),
        (('phone', 'Phone'), 'starts_with', 'Phone'),
        (('mobile', 'Mobile'), 'starts_with', 'Mobile'),
        (('first_name', 'firstname', 'First_Name'), 'starts_with', 'First_Name'),
        (('last_name', 'lastname', 'Last_Name'), 'starts_with', 'Last_Name'),
        (('city', 'City'), 'equals', 'City'),
        (('state', 'State'), 'equals', 'State'),
        (('industry', 'Industry'), 'equals', 'Industry'),
        (('designation', 'Designation'), 'starts_with', 'Designation'),
    ]

    for aliases, operator, zoho_field in field_mappings:
        val = None
        for alias in aliases:
            if alias.lower() in params_dict:
                val = params_dict[alias.lower()]
                break
        if val:
            conditions.append(f"({zoho_field}:{operator}:{val})")

    if not conditions:
        return None

    if len(conditions) == 1:
        return conditions[0]

    result = conditions[0]
    for cond in conditions[1:]:
        result = f"({result}and{cond})"

    return result


class ZohoLeadFilter:
    """
    Filter class for Zoho CRM leads list.
    Supports filtering by all standard and nested fields, as well as date ranges and general search.
    """

    def __init__(self, query_params: Any):
        """
        Initialize filter with query parameters (dict or Django QueryDict).
        """
        self.params: Dict[str, str] = {}
        
        # Handle dict, QueryDict, or objects with items()
        if hasattr(query_params, 'items'):
            for key, val in query_params.items():
                if val is not None and str(val).strip() != "":
                    self.params[str(key).lower().strip()] = str(val).strip()

    def _get_param(self, *keys: str) -> Optional[str]:
        """Helper to get query param matching any alias keys (case-insensitive)"""
        for k in keys:
            k_lower = k.lower().strip()
            if k_lower in self.params:
                return self.params[k_lower]
        return None

    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse string to datetime object for date comparison"""
        if not date_str:
            return None
        date_str = date_str.strip()
        formats = [
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y/%m/%d",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        if len(date_str) >= 10:
            try:
                return datetime.strptime(date_str[:10], "%Y-%m-%d")
            except ValueError:
                pass
        return None

    def _match_string(self, value: Any, target: str, exact: bool = False) -> bool:
        """Check if target string matches value (case-insensitive)"""
        if value is None or target is None:
            return False
        val_str = str(value).strip().lower()
        target_str = str(target).strip().lower()
        if exact:
            return val_str == target_str
        return target_str in val_str

    def _match_date_range(
        self,
        item_date_str: Any,
        after_str: Optional[str],
        before_str: Optional[str],
    ) -> bool:
        """Check if item_date falls within after_str and before_str range"""
        if not after_str and not before_str:
            return True
        if not item_date_str:
            return False

        item_dt = self._parse_date(str(item_date_str))
        if not item_dt:
            return False

        item_date_only = item_dt.date()

        if after_str:
            after_dt = self._parse_date(after_str)
            if after_dt and item_date_only < after_dt.date():
                return False

        if before_str:
            before_dt = self._parse_date(before_str)
            if before_dt and item_date_only > before_dt.date():
                return False

        return True

    def filter_leads(self, leads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Filter a list of lead dicts based on initialized query params.
        """
        if not leads:
            return []

        # General Search
        search_query = self._get_param('search', 'q')

        # Field filters
        lead_id = self._get_param('id', 'lead_id')
        first_name = self._get_param('first_name', 'firstname', 'First_Name')
        last_name = self._get_param('last_name', 'lastname', 'Last_Name')
        full_name = self._get_param('name', 'full_name')
        email = self._get_param('email', 'Email')
        phone = self._get_param('phone', 'Phone')
        mobile = self._get_param('mobile', 'Mobile')
        company = self._get_param('company', 'Company')
        lead_status = self._get_param('lead_status', 'status', 'Lead_Status')
        lead_source = self._get_param('lead_source', 'source', 'Lead_Source')
        city = self._get_param('city', 'City')
        state = self._get_param('state', 'State')
        country = self._get_param('country', 'Country')
        designation = self._get_param('designation', 'Designation')
        industry = self._get_param('industry', 'Industry')
        website = self._get_param('website', 'Website')
        annual_revenue = self._get_param('annual_revenue', 'Annual_Revenue')

        # Owner filters
        owner_name = self._get_param('owner_name', 'owner')
        owner_email = self._get_param('owner_email')
        owner_id = self._get_param('owner_id')

        # Date range filters
        created_after = self._get_param('created_after', 'created_time_after', 'created_at_min', 'created_min')
        created_before = self._get_param('created_before', 'created_time_before', 'created_at_max', 'created_max')
        modified_after = self._get_param('modified_after', 'modified_time_after', 'modified_at_min', 'modified_min')
        modified_before = self._get_param('modified_before', 'modified_time_before', 'modified_at_max', 'modified_max')

        filtered = []
        for lead in leads:
            if not isinstance(lead, dict):
                continue

            # ID filter
            if lead_id and not self._match_string(lead.get('id'), lead_id, exact=True):
                continue

            # First Name filter
            if first_name and not self._match_string(lead.get('First_Name'), first_name):
                continue

            # Last Name filter
            if last_name and not self._match_string(lead.get('Last_Name'), last_name):
                continue

            # Full Name filter
            if full_name:
                combined_name = f"{lead.get('First_Name') or ''} {lead.get('Last_Name') or ''}".strip()
                if not self._match_string(combined_name, full_name):
                    continue

            # Email filter
            if email and not self._match_string(lead.get('Email'), email):
                continue

            # Phone filter
            if phone and not self._match_string(lead.get('Phone'), phone):
                continue

            # Mobile filter
            if mobile and not self._match_string(lead.get('Mobile'), mobile):
                continue

            # Company filter
            if company and not self._match_string(lead.get('Company'), company):
                continue

            # Lead Status filter
            if lead_status and not self._match_string(lead.get('Lead_Status'), lead_status):
                continue

            # Lead Source filter
            if lead_source and not self._match_string(lead.get('Lead_Source'), lead_source):
                continue

            # City filter
            if city and not self._match_string(lead.get('City'), city):
                continue

            # State filter
            if state and not self._match_string(lead.get('State'), state):
                continue

            # Country filter
            if country and not self._match_string(lead.get('Country'), country):
                continue

            # Designation filter
            if designation and not self._match_string(lead.get('Designation'), designation):
                continue

            # Industry filter
            if industry and not self._match_string(lead.get('Industry'), industry):
                continue

            # Website filter
            if website and not self._match_string(lead.get('Website'), website):
                continue

            # Annual Revenue filter
            if annual_revenue and not self._match_string(lead.get('Annual_Revenue'), annual_revenue):
                continue

            # Owner filters (Owner dict: {"name": ..., "id": ..., "email": ...})
            owner_data = lead.get('Owner') or {}
            if isinstance(owner_data, dict):
                if owner_name and not self._match_string(owner_data.get('name'), owner_name):
                    continue
                if owner_email and not self._match_string(owner_data.get('email'), owner_email):
                    continue
                if owner_id and not self._match_string(owner_data.get('id'), owner_id, exact=True):
                    continue
            elif owner_name or owner_email or owner_id:
                continue

            # Created Time range filter
            if not self._match_date_range(lead.get('Created_Time'), created_after, created_before):
                continue

            # Modified Time range filter
            if not self._match_date_range(lead.get('Modified_Time'), modified_after, modified_before):
                continue

            # General Search filter across key attributes
            if search_query:
                s = search_query.lower()
                first = str(lead.get('First_Name') or '').lower()
                last = str(lead.get('Last_Name') or '').lower()
                em = str(lead.get('Email') or '').lower()
                ph = str(lead.get('Phone') or '').lower()
                mb = str(lead.get('Mobile') or '').lower()
                comp = str(lead.get('Company') or '').lower()
                src = str(lead.get('Lead_Source') or '').lower()
                st = str(lead.get('Lead_Status') or '').lower()
                ct = str(lead.get('City') or '').lower()
                cnt = str(lead.get('Country') or '').lower()
                own_n = str(owner_data.get('name') or '').lower() if isinstance(owner_data, dict) else ''

                match_found = any(s in val for val in [first, last, em, ph, mb, comp, src, st, ct, cnt, own_n])
                if not match_found:
                    continue

            filtered.append(lead)

        return filtered
