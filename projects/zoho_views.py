"""
Zoho CRM Integration Views/Endpoints
"""
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
import logging
from drf_yasg.utils import swagger_auto_schema

from .zoho_service import ZohoService
from .zoho_filters import ZohoLeadFilter, ZohoLeadQuerySerializer, build_zoho_criteria

logger = logging.getLogger(__name__)


@api_view(['GET'])
@swagger_auto_schema(auto_schema=None)
@permission_classes([AllowAny])
def zoho_auth_url(request):
    """
    Zoho authorization URL generate karo
    Client ko yeh URL dedo, user authorize karega
    """
    try:
        zoho_service = ZohoService()
        auth_url = zoho_service.get_authorization_url()
        
        return Response({
            'status': 'success',
            'auth_url': auth_url,
            'message': 'Click karo link par aur Zoho mein authorize karo'
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Error generating auth URL: {str(e)}")
        return Response({
            'status': 'error',
            'message': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@swagger_auto_schema(auto_schema=None)
@permission_classes([AllowAny])
def zoho_callback(request):
    """
    Zoho OAuth callback handler
    Jab user authorize kare to Zoho yahan code bhejega
    """
    try:
        auth_code = request.GET.get('code')
        
        if not auth_code:
            return Response({
                'status': 'error',
                'message': 'Authorization code nahi mila'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        zoho_service = ZohoService()
        token_data = zoho_service.get_access_token(auth_code)
        
        logger.info("Zoho authorization successful")
        
        return Response({
            'status': 'success',
            'message': 'Zoho authorization successful!',
            'access_token': token_data.get('access_token'),
            'refresh_token': token_data.get('refresh_token')
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Error in Zoho callback: {str(e)}")
        return Response({
            'status': 'error',
            'message': f'Authorization failed: {str(e)}'
        }, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@swagger_auto_schema(auto_schema=None)
@permission_classes([IsAuthenticated])
def get_access_token(request):
    """
    Authorization code se access token generate karo
    Request body: {"auth_code": "code_from_zoho"}
    """
    try:
        auth_code = request.data.get('auth_code')
        
        if not auth_code:
            return Response({
                'status': 'error',
                'message': 'auth_code required hai'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        zoho_service = ZohoService()
        token_data = zoho_service.get_access_token(auth_code)
        
        return Response({
            'status': 'success',
            'data': token_data
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Error getting access token: {str(e)}")
        return Response({
            'status': 'error',
            'message': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@swagger_auto_schema(auto_schema=None)
@permission_classes([IsAuthenticated])
def refresh_token(request):
    """
    Refresh token use karke naya access token lao
    Request body: {"refresh_token": "refresh_token_value"}
    """
    try:
        refresh_token = request.data.get('refresh_token')
        
        if not refresh_token:
            return Response({
                'status': 'error',
                'message': 'refresh_token required hai'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        zoho_service = ZohoService()
        token_data = zoho_service.refresh_access_token(refresh_token)
        
        return Response({
            'status': 'success',
            'data': token_data
        }, status=status.HTTP_200_OK)
        
    except Exception as e:
        logger.error(f"Error refreshing token: {str(e)}")
        return Response({
            'status': 'error',
            'message': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)


@swagger_auto_schema(
    method='get',
    tags=['Zoho Leads'],
    query_serializer=ZohoLeadQuerySerializer,
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def fetch_leads(request):
    """
    Zoho se leads fetch karo using authenticated user credentials.
    Query params:
    - search: General search across key fields
    - id, first_name, last_name, name, email, phone, mobile, company
    - lead_status, lead_source, city, state, country, designation, industry
    - owner_name, owner_email, owner_id
    - created_after, created_before, modified_after, modified_before
    - page: Page number (default 1)
    - per_page: Leads per page (default 200)
    """
    try:
        page = int(request.GET.get('page', 1))
        per_page = int(request.GET.get('per_page', 200))

        search_word = request.GET.get('search') or request.GET.get('q')
        criteria = build_zoho_criteria(request.GET)

        zoho_service = ZohoService()
        leads_data = zoho_service.fetch_leads(
            user=request.user,
            page=page,
            per_page=per_page,
            search_word=search_word,
            criteria=criteria,
        )

        # Apply filters using ZohoLeadFilter
        lead_filter = ZohoLeadFilter(request.GET)
        if isinstance(leads_data, dict) and 'data' in leads_data and isinstance(leads_data['data'], list):
            filtered_leads = lead_filter.filter_leads(leads_data['data'])
            leads_data['data'] = filtered_leads
            if 'info' in leads_data and isinstance(leads_data['info'], dict):
                leads_data['info']['count'] = len(filtered_leads)
        elif isinstance(leads_data, list):
            leads_data = lead_filter.filter_leads(leads_data)

        return Response({
            'status': 'success',
            'data': leads_data
        }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Error fetching leads: {str(e)}")
        return Response({
            'status': 'error',
            'message': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)
