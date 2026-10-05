"""Concurrency tests for plan-limit enforcement.

These use `TransactionTestCase` with real threads, because the guarantee under
test is that two simultaneous requests cannot both read "9 of 10" and both
insert. Mocking or serialising would defeat the point.
"""

import threading

from django.db import connections
from django.test import TransactionTestCase

from billing import services
from billing.models import UsageCounter
from users.models import Company, User


def _run_in_threads(target, count):
    """Run `target(index)` in `count` threads and collect raised errors."""
    errors = []
    barrier = threading.Barrier(count)

    def wrapper(index):
        try:
            # Make the threads collide on the same instant.
            barrier.wait(timeout=10)
            target(index)
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            # Each thread needs its own connection inside the test transaction.
            connections.close_all()

    threads = [threading.Thread(target=wrapper, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return errors


class ConcurrentSeatLimitTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.company = Company.objects.create(
            name='Race Co', plan=Company.Plan.BASIC,
            operating_countries=['UAE'])
        self.admin = User.objects.create_user(
            email='admin@race.test', first_name='R', last_name='A',
            password='Passw0rd!23', company=self.company,
            role=User.Role.COMPANY_ADMIN, status=User.Status.ACTIVE)

    def _invite_one(self, index):
        """Mimic the serializer: assert the cap and insert, in one transaction."""
        from django.db import transaction

        with transaction.atomic():
            services.PlanLimitsService.for_company(self.company).assert_can_add('users')
            User.objects.create_user(
                email=f'race{index}@race.test', first_name='R', last_name='U',
                password='Passw0rd!23', company=self.company,
                role=User.Role.AGENT, status=User.Status.INVITED)

    def test_ten_concurrent_invites_yield_exactly_nine_seats(self):
        # Nine seats are already taken (admin + eight), so only one more fits.
        for index in range(8):
            User.objects.create_user(
                email=f'pre{index}@race.test', first_name='P', last_name='U',
                password='Passw0rd!23', company=self.company,
                role=User.Role.AGENT, status=User.Status.INVITED)

        errors = _run_in_threads(self._invite_one, 10)

        total = User.objects.filter(company=self.company).count()
        self.assertEqual(
            total, 10,
            f'expected the 10-seat cap to hold, got {total} users')
        self.assertEqual(len(errors), 9, 'exactly one invite should succeed')

    def test_ten_concurrent_manager_promotions_yield_two_managers(self):
        users = []
        for index in range(10):
            users.append(User.objects.create_user(
                email=f'mgr{index}@race.test', first_name='M', last_name='U',
                password='Passw0rd!23', company=self.company,
                role=User.Role.AGENT, status=User.Status.ACTIVE))

        def promote(index):
            from django.db import transaction

            with transaction.atomic():
                services.PlanLimitsService.for_company(self.company).assert_can_add(
                    'team_managers')
                User.objects.filter(pk=users[index].pk).update(
                    role=User.Role.TEAM_MANAGER)

        _run_in_threads(promote, 10)

        managers = User.objects.filter(
            company=self.company, role=User.Role.TEAM_MANAGER).count()
        self.assertEqual(managers, 2)


class ConcurrentCreditLimitTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.company = Company.objects.create(name='Race Credits Co', plan=Company.Plan.BASIC)

    def test_concurrent_charges_never_exceed_the_credit_cap(self):
        # Start one credit short of the cap so exactly one of five concurrent
        # 10-credit proposals can fit.
        UsageCounter.objects.create(
            company=self.company, kind=UsageCounter.Kind.AI_CREDITS,
            period_start=services.plan_limits.current_period_start(),
            used=490)

        def charge(index):
            services.consume_credits(self.company.pk, 'ai_proposal')

        errors = _run_in_threads(charge, 5)
        limit_errors = [e for e in errors if type(e).__name__ == 'PlanLimitReached']

        used = services.current_count(self.company.pk, 'ai_credits')
        self.assertEqual(used, 500, 'usage must stop exactly at the cap')
        self.assertEqual(len(limit_errors), 4)

    def test_concurrent_charges_with_the_same_receipt_charge_once(self):
        def charge(index):
            services.consume_credits(self.company.pk, 'ai_proposal', receipt_id='task-x')

        _run_in_threads(charge, 5)

        used = services.current_count(self.company.pk, 'ai_credits')
        self.assertEqual(used, 10, 'a retried task must only ever be charged once')
