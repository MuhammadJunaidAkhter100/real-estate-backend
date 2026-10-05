"""
Zoho CRM Integration Service
Handles OAuth authentication and lead fetching/searching from Zoho
"""
import requests
import logging
from datetime import timedelta
from django.core.cache import cache
from django.utils import timezone
from decouple import config
from typing import Dict, Any, Optional

from projects.models import ZohoCredentials

logger = logging.getLogger(__name__)


class ZohoService:
    """Service to interact with Zoho CRM API"""

    ZOHO_ACCOUNTS_URL = "https://accounts.zoho.com"
    ZOHO_API_BASE_URL = "https://www.zohoapis.com/crm/v8"

    def __init__(self):
        self.client_id = config('ZOHO_CLIENT_ID', default='')
        self.client_secret = config('ZOHO_SECRET_ID', default='')
        self.refresh_token_from_env = config('ZOHO_REFRESH_TOKEN', default='')
        self.redirect_uri = config('ZOHO_REDIRECT_URI', default='')
        self.cache_timeout = 3600  # 1 hour
        
    def get_authorization_url(self) -> str:
        """
        Generate Zoho OAuth authorization URL
        User ko yeh URL pe bhejenge jahan wo authorize karega
        """
        params = {
            'client_id': self.client_id,
            'response_type': 'code',
            'scope': 'ZohoCRM.modules.all,ZohoCRM.users.read',
            'redirect_uri': self.redirect_uri,
            'access_type': 'offline'
        }
        
        url = f"{self.ZOHO_ACCOUNTS_URL}/oauth/v2/auth"
        query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
        return f"{url}?{query_string}"
    
    def _get_credentials(self, user=None) -> ZohoCredentials | None:
        if not user:
            return None

        credentials, _ = ZohoCredentials.objects.get_or_create(
            user=user,
            defaults={
                'access_token': '',
                'refresh_token': self.refresh_token_from_env,
            },
        )
        return credentials

    def _save_credentials(self, credentials: ZohoCredentials, token_data: Dict[str, Any]) -> ZohoCredentials:
        credentials.access_token = token_data.get('access_token', '')
        credentials.refresh_token = token_data.get('refresh_token') or credentials.refresh_token
        expires_in = token_data.get('expires_in') or 3600
        credentials.token_expires_at = timezone.now() + timedelta(seconds=int(expires_in))
        credentials.is_active = True
        credentials.save(update_fields=['access_token', 'refresh_token', 'token_expires_at', 'is_active'])
        return credentials

    def get_access_token(self, auth_code: str, user=None) -> Dict[str, Any]:
        """
        Authorization code ko access token mein convert karo
        """
        try:
            payload = {
                'client_id': self.client_id,
                'client_secret': self.client_secret,
                'code': auth_code,
                'grant_type': 'authorization_code',
                'redirect_uri': self.redirect_uri,
            }

            response = requests.post(
                f"{self.ZOHO_ACCOUNTS_URL}/oauth/v2/token",
                data=payload,
                timeout=10,
            )
            response.raise_for_status()

            token_data = response.json()
            cache_key = f"zoho_access_token_{user.id}" if user else 'zoho_access_token'
            cache.set(cache_key, token_data, self.cache_timeout)

            if user:
                credentials = self._get_credentials(user)
                self._save_credentials(credentials, token_data)

            logger.info('Zoho access token obtained successfully')
            return token_data

        except requests.exceptions.RequestException as e:
            logger.error(f'Error getting Zoho access token: {str(e)}')
            raise Exception(f'Failed to get access token: {str(e)}')

    def refresh_access_token(self, refresh_token: str, user=None) -> Dict[str, Any]:
        """
        Refresh token use karke naya access token lao
        """
        try:
            payload = {
                'client_id': self.client_id,
                'client_secret': self.client_secret,
                'refresh_token': refresh_token,
                'grant_type': 'refresh_token',
            }

            response = requests.post(
                f"{self.ZOHO_ACCOUNTS_URL}/oauth/v2/token",
                data=payload,
                timeout=10,
            )
            response.raise_for_status()

            token_data = response.json()
            cache_key = f"zoho_access_token_{user.id}" if user else 'zoho_access_token'
            cache.set(cache_key, token_data, self.cache_timeout)

            if user:
                credentials = self._get_credentials(user)
                self._save_credentials(credentials, token_data)

            return token_data

        except requests.exceptions.RequestException as e:
            logger.error(f'Error refreshing Zoho access token: {str(e)}')
            raise Exception(f'Failed to refresh token: {str(e)}')

    def get_headers(self, access_token: str) -> Dict[str, str]:
        """API call ke liye headers prepare karo"""
        return {
            'Authorization': f'Zoho-oauthtoken {access_token}',
            'Content-Type': 'application/json',
        }

    def get_valid_access_token(self, user=None) -> str:
        if user:
            credentials = self._get_credentials(user)
            if credentials and credentials.access_token and not credentials.is_token_expired():
                return credentials.access_token

            refresh_token_value = credentials.refresh_token if credentials else self.refresh_token_from_env
            if not refresh_token_value:
                raise Exception('Zoho refresh token not found')

            token_data = self.refresh_access_token(refresh_token_value, user=user)
            return token_data.get('access_token', '')

        if self.refresh_token_from_env:
            token_data = self.refresh_access_token(self.refresh_token_from_env)
            return token_data.get('access_token', '')

        raise Exception('Zoho refresh token not found')

    def fetch_leads(
        self,
        user=None,
        access_token: str | None = None,
        page: int = 1,
        per_page: int = 200,
        search_word: str | None = None,
        criteria: str | None = None,
        email: str | None = None,
        phone: str | None = None,
    ) -> Dict[str, Any]:
        """
        Zoho se leads fetch / search karo.
        Agar search_word, criteria, email, ya phone diya gaya ho,
        to Zoho ka native search endpoint (/Leads/search) use hoga
        jo saare 10,000+ records mein se search karega.
        """
        try:
            token = access_token or self.get_valid_access_token(user=user)
            headers = self.get_headers(token)

            fields_str = ','.join([
                'id',
                'First_Name',
                'Last_Name',
                'Email',
                'Phone',
                'Company',
                'Lead_Status',
                'Lead_Source',
                'Owner',
                'Created_Time',
                'Modified_Time',
                'City',
                'State',
                'Country',
                'Website',
                'Designation',
                'Mobile',
                'Annual_Revenue',
                'Industry',
            ])

            params: Dict[str, Any] = {
                'page': page,
                'per_page': per_page,
                'fields': fields_str,
            }

            is_search = False
            if search_word and str(search_word).strip():
                params['word'] = str(search_word).strip()
                is_search = True
            elif criteria and str(criteria).strip():
                params['criteria'] = str(criteria).strip()
                is_search = True
            elif email and str(email).strip():
                params['email'] = str(email).strip()
                is_search = True
            elif phone and str(phone).strip():
                params['phone'] = str(phone).strip()
                is_search = True

            endpoint = "Leads/search" if is_search else "Leads"
            url = f"{self.ZOHO_API_BASE_URL}/{endpoint}"

            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=15,
            )

            # Zoho search API returns 204 No Content if no records match search
            if response.status_code == 204:
                return {'data': [], 'info': {'page': page, 'per_page': per_page, 'count': 0, 'more_records': False}}

            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            logger.error(f'Error fetching leads from Zoho: {str(e)}')
            raise Exception(f'Failed to fetch leads: {str(e)}')
