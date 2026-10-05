import calendar
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.db.models import Q, QuerySet
from django.utils import timezone

from api.constants import COUNTRY_CURRENCY_MAP
from projects.models import ProjectAgentAssignment, Unit
from users.models import Company, Lead, User
from users.utils import get_exchange_rate

logger = logging.getLogger(__name__)

CURRENCY_SYMBOLS = {
    'GBP': '£',
    'USD': '$',
    'EUR': '€',
    'AED': 'AED',
    'SAR': 'SAR',
    'OMR': 'OMR',
    'SGD': 'S$',
    'INR': '₹',
    'PKR': 'Rs',
    'CAD': 'C$',
    'AUD': 'A$',
}


class CommissionAnalyticsService:
    """
    Service for calculating and aggregating role-scoped commissions and deal sales
    across multiple timeframes (1D, 1W, 1M, 3M, 1Y, or custom range).
    """

    AVAILABLE_TIMEFRAMES = ['1D', '1W', '1M', '3M', '1Y']

    def __init__(self, user: User, target_currency: Optional[str] = None):
        self.user = user
        self.role = getattr(user, 'role', User.Role.AGENT)

        # Determine user currency
        user_country = getattr(user, 'current_country', '') or ''
        if not user_country and getattr(user, 'company', None) and user.company.operating_countries:
            user_country = user.company.operating_countries[0]
        if not user_country and getattr(user, 'countries', None) and len(user.countries) > 0:
            user_country = user.countries[0]

        default_currency = COUNTRY_CURRENCY_MAP.get(user_country, '') or 'USD'
        self.currency = (target_currency or default_currency).upper()
        self.currency_symbol = CURRENCY_SYMBOLS.get(self.currency, self.currency)

        # Pre-cache project agent assignments: (agent_id, project_id) -> assignment
        self.assignments = {
            (a.agent_id, a.project_id): a
            for a in ProjectAgentAssignment.objects.all()
        }

    def get_scoped_leads_queryset(
        self,
        company_id: Optional[int] = None,
        agent_id: Optional[int] = None,
        project_id: Optional[int] = None,
    ) -> QuerySet:
        """
        Scope won leads based on user's role and requested filters:
        - SUPERADMIN: Sees all companies (or filtered by company_id).
        - AXIYON_ADMIN / COMPANY_ADMIN: Scoped to their company.
        - TEAM_MANAGER: Scoped to their managed team members or self.
        - AGENT: Scoped strictly to leads assigned to them.
        """
        qs = (
            Lead.objects.filter(
                status=Lead.Status.CONVERTED_WON,
                assigned_to__isnull=False,
            )
            .select_related('project', 'created_by', 'assigned_to', 'unit', 'created_by__company', 'assigned_to__company')
            .prefetch_related('units')
        )

        if self.role == User.Role.SUPERADMIN:
            if company_id:
                qs = qs.filter(
                    Q(created_by__company_id=company_id) | Q(assigned_to__company_id=company_id)
                )
        elif self.role in (User.Role.AXIYON_ADMIN, User.Role.COMPANY_ADMIN):
            if getattr(self.user, 'company_id', None):
                qs = qs.filter(
                    Q(created_by__company=self.user.company) | Q(assigned_to__company=self.user.company)
                )
            else:
                qs = qs.filter(created_by=self.user)
        elif self.role == User.Role.TEAM_MANAGER:
            managed_team = getattr(self.user, 'managed_team', None)
            if managed_team:
                qs = qs.filter(
                    Q(assigned_to__team=managed_team) |
                    Q(created_by__team=managed_team) |
                    Q(assigned_to=self.user)
                ).distinct()
            else:
                qs = qs.filter(Q(assigned_to=self.user) | Q(created_by=self.user))
        else:  # Agent
            qs = qs.filter(assigned_to=self.user)

        # Query param filters
        if agent_id and self.role != User.Role.AGENT:
            qs = qs.filter(assigned_to_id=agent_id)
        if project_id:
            qs = qs.filter(project_id=project_id)

        return qs

    def _extract_lead_deal_values(self, lead: Lead) -> List[Dict[str, Any]]:
        """
        Extract deal price, split percentages, and commissions for a lead.
        Handles multi-unit deals if present.
        """
        if not lead.assigned_to_id:
            return []

        linked_units = list(lead.units.all())
        if not linked_units and lead.unit:
            linked_units = [lead.unit]

        if not linked_units:
            units_to_process = [(None, lead.project)]
        else:
            units_to_process = [(u, u.project or lead.project) for u in linked_units]

        deal_records = []

        for matched_unit, proj in units_to_process:
            unit_price = Decimal('0.00')
            if matched_unit:
                if matched_unit.discounted_price is not None and Decimal(str(matched_unit.discounted_price)) > Decimal('0.00'):
                    unit_price = Decimal(str(matched_unit.discounted_price))
                elif matched_unit.list_price is not None:
                    unit_price = Decimal(str(matched_unit.list_price))

            if unit_price == Decimal('0.00'):
                if lead.estimated_budget:
                    unit_price = Decimal(str(lead.estimated_budget))
                elif proj and proj.starting_price:
                    unit_price = Decimal(str(proj.starting_price))

            list_price = unit_price

            # Currency conversion
            stored_currency = (proj.currency if (proj and proj.currency) else 'USD') or 'USD'
            if self.currency and stored_currency and stored_currency.upper() != self.currency:
                try:
                    rate = get_exchange_rate(stored_currency, self.currency)
                    list_price = round(list_price * Decimal(str(rate)), 2)
                except Exception as ex:
                    logger.warning(f"Currency conversion failed ({stored_currency} -> {self.currency}): {ex}")

            proj_id = proj.id if proj else lead.project_id
            assignment = self.assignments.get((lead.assigned_to_id, proj_id))

            if assignment:
                agent_split = assignment.agent_split
                company_split = assignment.company_split
                agent_commission = round(list_price * (agent_split / Decimal('100.00')), 2)
                company_commission = round(list_price * (company_split / Decimal('100.00')), 2)
            else:
                agent_split = Decimal('0.00')
                company_split = Decimal('0.00')
                agent_commission = Decimal('0.00')
                company_commission = Decimal('0.00')

            total_commission = agent_commission + company_commission

            # Determine commission relevant to caller's role
            if self.role == User.Role.AGENT:
                role_commission = agent_commission
            elif self.role in (User.Role.COMPANY_ADMIN, User.Role.AXIYON_ADMIN):
                role_commission = company_commission
            elif self.role == User.Role.TEAM_MANAGER:
                role_commission = agent_commission
            else:  # SUPERADMIN
                role_commission = total_commission if total_commission > Decimal('0.00') else company_commission

            deal_date = lead.updated_at or lead.created_at

            company_id = None
            company_name = 'N/A'
            if lead.assigned_to and getattr(lead.assigned_to, 'company', None):
                company_id = lead.assigned_to.company.id
                company_name = lead.assigned_to.company.name
            elif lead.created_by and getattr(lead.created_by, 'company', None):
                company_id = lead.created_by.company.id
                company_name = lead.created_by.company.name

            deal_records.append({
                'lead_id': lead.id,
                'lead_name': lead.name,
                'deal_date': deal_date,
                'company_id': company_id,
                'company_name': company_name,
                'agent_id': lead.assigned_to_id,
                'agent_name': lead.assigned_to.full_name if lead.assigned_to else 'Unassigned',
                'project_id': proj_id,
                'project_title': proj.title if proj else 'N/A',
                'unit_label': matched_unit.label if matched_unit else None,
                'list_price': list_price,
                'agent_split': agent_split,
                'company_split': company_split,
                'agent_commission': agent_commission,
                'company_commission': company_commission,
                'total_commission': total_commission,
                'role_commission': role_commission,
            })

        return deal_records

    def _build_time_buckets(
        self,
        timeframe: str,
        start_date_str: Optional[str] = None,
        end_date_str: Optional[str] = None,
        year: Optional[int] = None,
    ) -> Tuple[datetime, datetime, List[Dict[str, Any]], str]:
        """
        Construct time range and discrete chart buckets based on timeframe:
        - 1D: 24 hourly buckets
        - 1W: 7 daily buckets
        - 1M: daily buckets across last 30 days
        - 3M: 3 monthly buckets
        - 1Y: 12 monthly buckets (Jan - Dec)
        """
        now = timezone.now()
        tf = (timeframe or '1D').upper()

        if tf not in self.AVAILABLE_TIMEFRAMES and not (start_date_str and end_date_str):
            tf = '1D'

        if start_date_str and end_date_str:
            start_d = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_d = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            start_dt = timezone.make_aware(datetime.combine(start_d, time.min))
            end_dt = timezone.make_aware(datetime.combine(end_d, time.max))

            buckets = []
            cur = start_d
            while cur <= end_d:
                buckets.append({
                    'key': cur.strftime('%Y-%m-%d'),
                    'label': cur.strftime('%d %b'),
                    'period': cur.strftime('%d %b %Y'),
                    'start_dt': timezone.make_aware(datetime.combine(cur, time.min)),
                    'end_dt': timezone.make_aware(datetime.combine(cur, time.max)),
                })
                cur += timedelta(days=1)

            return start_dt, end_dt, buckets, 'custom'

        if tf == '1D':
            ref_date = now.date()
            if start_date_str:
                ref_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()

            start_dt = timezone.make_aware(datetime.combine(ref_date, time.min))
            end_dt = timezone.make_aware(datetime.combine(ref_date, time.max))

            buckets = []
            for h in range(24):
                bucket_start = start_dt + timedelta(hours=h)
                bucket_end = bucket_start + timedelta(hours=1) - timedelta(microseconds=1)
                hour_str = f"{h:02d}:00"
                buckets.append({
                    'key': hour_str,
                    'label': hour_str,
                    'period': f"{hour_str} - {h+1:02d}:00" if h < 23 else "23:00 - 00:00",
                    'start_dt': bucket_start,
                    'end_dt': bucket_end,
                })
            return start_dt, end_dt, buckets, tf

        elif tf == '1W':
            end_d = now.date()
            start_d = end_d - timedelta(days=6)
            start_dt = timezone.make_aware(datetime.combine(start_d, time.min))
            end_dt = timezone.make_aware(datetime.combine(end_d, time.max))

            buckets = []
            for i in range(7):
                d = start_d + timedelta(days=i)
                buckets.append({
                    'key': d.strftime('%Y-%m-%d'),
                    'label': d.strftime('%a'),
                    'period': d.strftime('%a, %d %b %Y'),
                    'start_dt': timezone.make_aware(datetime.combine(d, time.min)),
                    'end_dt': timezone.make_aware(datetime.combine(d, time.max)),
                })
            return start_dt, end_dt, buckets, tf

        elif tf == '1M':
            end_d = now.date()
            start_d = end_d - timedelta(days=29)
            start_dt = timezone.make_aware(datetime.combine(start_d, time.min))
            end_dt = timezone.make_aware(datetime.combine(end_d, time.max))

            buckets = []
            for i in range(30):
                d = start_d + timedelta(days=i)
                buckets.append({
                    'key': d.strftime('%Y-%m-%d'),
                    'label': d.strftime('%d %b'),
                    'period': d.strftime('%d %b %Y'),
                    'start_dt': timezone.make_aware(datetime.combine(d, time.min)),
                    'end_dt': timezone.make_aware(datetime.combine(d, time.max)),
                })
            return start_dt, end_dt, buckets, tf

        elif tf == '3M':
            # 3 Calendar months ending current month
            target_year = year or now.year
            cur_month = now.month
            start_month = cur_month - 2
            start_year = target_year
            if start_month <= 0:
                start_month += 12
                start_year -= 1

            start_d = date(start_year, start_month, 1)
            last_day_of_month = calendar.monthrange(target_year, cur_month)[1]
            end_d = date(target_year, cur_month, last_day_of_month)

            start_dt = timezone.make_aware(datetime.combine(start_d, time.min))
            end_dt = timezone.make_aware(datetime.combine(end_d, time.max))

            buckets = []
            m_year = start_year
            m = start_month
            for _ in range(3):
                last_day = calendar.monthrange(m_year, m)[1]
                b_start = timezone.make_aware(datetime.combine(date(m_year, m, 1), time.min))
                b_end = timezone.make_aware(datetime.combine(date(m_year, m, last_day), time.max))
                m_date = date(m_year, m, 1)
                buckets.append({
                    'key': m_date.strftime('%Y-%m'),
                    'label': m_date.strftime('%b'),
                    'period': m_date.strftime('%b %Y').upper(),
                    'start_dt': b_start,
                    'end_dt': b_end,
                })
                m += 1
                if m > 12:
                    m = 1
                    m_year += 1

            return start_dt, end_dt, buckets, tf

        else:  # '1Y' (Default matching screenshot)
            target_year = year or now.year
            start_d = date(target_year, 1, 1)
            end_d = date(target_year, 12, 31)

            start_dt = timezone.make_aware(datetime.combine(start_d, time.min))
            end_dt = timezone.make_aware(datetime.combine(end_d, time.max))

            buckets = []
            for m in range(1, 13):
                last_day = calendar.monthrange(target_year, m)[1]
                b_start = timezone.make_aware(datetime.combine(date(target_year, m, 1), time.min))
                b_end = timezone.make_aware(datetime.combine(date(target_year, m, last_day), time.max))
                m_date = date(target_year, m, 1)
                buckets.append({
                    'key': m_date.strftime('%Y-%m'),
                    'label': m_date.strftime('%b'),
                    'period': m_date.strftime('%b %Y').upper(),
                    'start_dt': b_start,
                    'end_dt': b_end,
                })

            return start_dt, end_dt, buckets, tf

    def get_analytics(
        self,
        timeframe: str = '1D',
        company_id: Optional[int] = None,
        agent_id: Optional[int] = None,
        project_id: Optional[int] = None,
        start_date_str: Optional[str] = None,
        end_date_str: Optional[str] = None,
        year: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Main calculation entrypoint. Returns summary metrics and periodic chart breakdown.
        """
        start_dt, end_dt, buckets, active_tf = self._build_time_buckets(
            timeframe=timeframe,
            start_date_str=start_date_str,
            end_date_str=end_date_str,
            year=year,
        )

        qs = self.get_scoped_leads_queryset(
            company_id=company_id,
            agent_id=agent_id,
            project_id=project_id,
        )

        # Filter by time range
        qs = qs.filter(updated_at__gte=start_dt, updated_at__lte=end_dt)

        # Collect all deal transactions
        all_deals = []
        for lead in qs:
            deals = self._extract_lead_deal_values(lead)
            all_deals.extend(deals)

        # Initialize chart buckets data structure
        chart_map = {}
        for b in buckets:
            chart_map[b['key']] = {
                'key': b['key'],
                'label': b['label'],
                'period': b['period'],
                'sales': Decimal('0.00'),
                'commission': Decimal('0.00'),
                'agent_commission': Decimal('0.00'),
                'company_commission': Decimal('0.00'),
                'deals_count': 0,
            }

        total_sales = Decimal('0.00')
        total_commission = Decimal('0.00')
        total_agent_commission = Decimal('0.00')
        total_company_commission = Decimal('0.00')
        total_deals_won = len(all_deals)

        # Company-wise breakdown (for Superadmin or multi-company reporting)
        company_stats: Dict[int, Dict[str, Any]] = {}

        for deal in all_deals:
            deal_dt = deal['deal_date']
            list_price = deal['list_price']
            role_comm = deal['role_commission']
            ag_comm = deal['agent_commission']
            co_comm = deal['company_commission']

            total_sales += list_price
            total_commission += role_comm
            total_agent_commission += ag_comm
            total_company_commission += co_comm

            # Find matching bucket
            matched_bucket_key = None
            if active_tf == '1D':
                matched_bucket_key = f"{deal_dt.hour:02d}:00"
            elif active_tf in ('1W', '1M', 'custom'):
                matched_bucket_key = deal_dt.strftime('%Y-%m-%d')
            elif active_tf in ('3M', '1Y'):
                matched_bucket_key = deal_dt.strftime('%Y-%m')

            if matched_bucket_key and matched_bucket_key in chart_map:
                chart_map[matched_bucket_key]['sales'] += list_price
                chart_map[matched_bucket_key]['commission'] += role_comm
                chart_map[matched_bucket_key]['agent_commission'] += ag_comm
                chart_map[matched_bucket_key]['company_commission'] += co_comm
                chart_map[matched_bucket_key]['deals_count'] += 1

            # Track company breakdown
            cid = deal['company_id']
            if cid:
                if cid not in company_stats:
                    company_stats[cid] = {
                        'company_id': cid,
                        'company_name': deal['company_name'],
                        'total_sales': Decimal('0.00'),
                        'total_commission': Decimal('0.00'),
                        'agent_commission': Decimal('0.00'),
                        'company_commission': Decimal('0.00'),
                        'deals_count': 0,
                    }
                company_stats[cid]['total_sales'] += list_price
                company_stats[cid]['total_commission'] += (
                    deal['total_commission'] if deal['total_commission'] > Decimal('0.00') else co_comm
                )
                company_stats[cid]['agent_commission'] += ag_comm
                company_stats[cid]['company_commission'] += co_comm
                company_stats[cid]['deals_count'] += 1

        chart_data = []
        for b in buckets:
            item = chart_map[b['key']]
            chart_data.append({
                'key': item['key'],
                'label': item['label'],
                'period': item['period'],
                'sales': float(item['sales']),
                'commission': float(item['commission']),
                'agent_commission': float(item['agent_commission']),
                'company_commission': float(item['company_commission']),
                'deals_count': item['deals_count'],
            })

        # Superadmin company breakdown list
        companies_breakdown = []
        if self.role == User.Role.SUPERADMIN:
            # Also include any registered companies with zero sales
            all_companies = Company.objects.all().order_by('name')
            for comp in all_companies:
                st = company_stats.get(comp.id, {
                    'company_id': comp.id,
                    'company_name': comp.name,
                    'total_sales': Decimal('0.00'),
                    'total_commission': Decimal('0.00'),
                    'agent_commission': Decimal('0.00'),
                    'company_commission': Decimal('0.00'),
                    'deals_count': 0,
                })
                companies_breakdown.append({
                    'company_id': comp.id,
                    'company_name': comp.name,
                    'total_sales': float(st['total_sales']),
                    'total_commission': float(st['total_commission']),
                    'agent_commission': float(st['agent_commission']),
                    'company_commission': float(st['company_commission']),
                    'deals_count': st['deals_count'],
                })

        formatted_sales = f"{self.currency} {total_sales:,.0f}" if total_sales == total_sales.to_integral_value() else f"{self.currency} {total_sales:,.2f}"
        formatted_commission = f"{self.currency} {total_commission:,.0f}" if total_commission == total_commission.to_integral_value() else f"{self.currency} {total_commission:,.2f}"

        return {
            'role': self.role,
            'timeframe': active_tf,
            'available_timeframes': self.AVAILABLE_TIMEFRAMES,
            'currency': self.currency,
            'currency_symbol': self.currency_symbol,
            'summary': {
                'total_sales': float(total_sales),
                'total_sales_formatted': formatted_sales,
                'total_commission': float(total_commission),
                'total_commission_formatted': formatted_commission,
                'total_agent_commission': float(total_agent_commission),
                'total_company_commission': float(total_company_commission),
                'total_deals_won': total_deals_won,
            },
            'chart_data': chart_data,
            'companies_breakdown': companies_breakdown,
        }

    def get_latest_commissions(
        self,
        limit: int = 3,
        company_id: Optional[int] = None,
        agent_id: Optional[int] = None,
        project_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Return the latest N commission items for the 'Commission Intelligence' / 'Earnings Outlook' dashboard widget.
        Scoped by role:
        - Agent: only their won deals & agent commission.
        - Team Manager: team members + self.
        - Company Admin / Axiyon Admin: company's team managers & agents + company commission.
        - Super Admin: across all companies + company name.
        """
        qs = self.get_scoped_leads_queryset(
            company_id=company_id,
            agent_id=agent_id,
            project_id=project_id,
        ).order_by('-updated_at')

        latest_items = []
        total_latest_amount = Decimal('0.00')

        for lead in qs:
            deals = self._extract_lead_deal_values(lead)
            for deal in deals:
                comm_amount = deal['role_commission']
                list_price = deal['list_price']

                # Compute effective rate percentage
                if list_price > Decimal('0.00'):
                    rate_pct = round(float((comm_amount / list_price) * Decimal('100.00')), 1)
                else:
                    rate_pct = float(deal['agent_split'] if self.role == User.Role.AGENT else deal['company_split'])

                if rate_pct == int(rate_pct):
                    rate_badge = f"{int(rate_pct)}%"
                else:
                    rate_badge = f"{rate_pct:.1f}%"

                # Compute tier label based on percentage
                if rate_pct >= 5.0:
                    tier_label = "PREMIUM RATE"
                elif rate_pct >= 3.0:
                    tier_label = "STRATEGIC TIER"
                else:
                    tier_label = "BASE COMMISSION"

                formatted_comm = (
                    f"{self.currency} {comm_amount:,.0f}"
                    if comm_amount == comm_amount.to_integral_value()
                    else f"{self.currency} {comm_amount:,.2f}"
                )

                # Title: developer name if available, else project title or lead name
                proj_dev = getattr(lead.project, 'developer', '') if lead.project else ''
                proj_title = deal['project_title']
                display_title = proj_dev if proj_dev else (proj_title if proj_title != 'N/A' else deal['lead_name'])

                latest_items.append({
                    'id': deal['lead_id'],
                    'lead_id': deal['lead_id'],
                    'lead_name': deal['lead_name'],
                    'title': display_title,
                    'developer': proj_dev or 'N/A',
                    'project_title': proj_title,
                    'unit_label': deal['unit_label'],
                    'rate': rate_pct,
                    'rate_badge': rate_badge,
                    'tier_label': tier_label,
                    'amount': float(comm_amount),
                    'amount_formatted': formatted_comm,
                    'sales_amount': float(list_price),
                    'currency': self.currency,
                    'currency_symbol': self.currency_symbol,
                    'agent_id': deal['agent_id'],
                    'agent_name': deal['agent_name'],
                    'agent_role': getattr(lead.assigned_to, 'role', None) if lead.assigned_to else None,
                    'company_id': deal['company_id'],
                    'company_name': deal['company_name'],
                    'date': deal['deal_date'].strftime('%d %b %Y') if deal['deal_date'] else '',
                    'closed_at': deal['deal_date'].isoformat() if deal['deal_date'] else None,
                })
                total_latest_amount += comm_amount
                if len(latest_items) >= limit:
                    break
            if len(latest_items) >= limit:
                break

        formatted_total = (
            f"{self.currency} {total_latest_amount:,.0f}"
            if total_latest_amount == total_latest_amount.to_integral_value()
            else f"{self.currency} {total_latest_amount:,.2f}"
        )

        return {
            'widget_title': 'Commission Intelligence',
            'widget_subtitle': 'EARNINGS OUTLOOK',
            'role': self.role,
            'currency': self.currency,
            'currency_symbol': self.currency_symbol,
            'total_latest_amount': float(total_latest_amount),
            'total_latest_amount_formatted': formatted_total,
            'latest_commissions': latest_items,
        }

