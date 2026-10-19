from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient
from users.models import Company, User


class CompanyScrapingStatusTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = Company.objects.create(
            name="Scrape Co",
            website="https://www.example.com",
            website_scrape_status="processing",
            website_data={
                "status": "processing",
                "progress": 65,
                "stage": "Extracting details for Project A (2/4)...",
                "total_projects": 4,
                "projects_created_or_updated": 2,
            }
        )
        self.admin = User.objects.create_user(
            email="admin@scrape.test",
            first_name="Admin",
            last_name="User",
            password="password123",
            company=self.company,
            role=User.Role.COMPANY_ADMIN,
            status=User.Status.ACTIVE,
        )

    def test_get_scraping_status_authenticated(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get('/api/users/company/scraping-status/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'processing')
        self.assertEqual(response.data['progress'], 65)
        self.assertEqual(response.data['total_projects'], 4)
        self.assertEqual(response.data['projects_created_or_updated'], 2)
        self.assertIn('Project A', response.data['stage'])

    def test_get_scraping_status_unauthenticated(self):
        response = self.client.get('/api/users/company/scraping-status/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
