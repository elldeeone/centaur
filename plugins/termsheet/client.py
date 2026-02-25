"""Termsheet plugin client — wraps deal CRUD, generation, and document output."""

from typing import Optional

from .models import Deal, DealStatus, TermSheet


class TermsheetClient:
    """Client for term sheet generation and deal tracking."""

    def create_term_sheet(self, **kwargs) -> TermSheet:
        """Create a TermSheet model from keyword arguments.

        Args:
            **kwargs: Fields for the TermSheet dataclass.
        """
        return TermSheet(**kwargs)

    def create_deal(
        self,
        company_name: str,
        term_sheet: TermSheet,
        requester_user_id: str,
        requester_user_name: str = "",
        slack_channel: str = "",
        slack_thread_ts: str = "",
    ) -> Deal:
        """Create and persist a new deal.

        Args:
            company_name: Company name for the deal.
            term_sheet: The term sheet data.
            requester_user_id: Slack user ID of the requester.
            requester_user_name: Slack username of the requester.
            slack_channel: Slack channel ID.
            slack_thread_ts: Slack thread timestamp.
        """
        from .store import create_deal

        return create_deal(
            company_name=company_name,
            term_sheet=term_sheet,
            requester_user_id=requester_user_id,
            requester_user_name=requester_user_name,
            slack_channel=slack_channel,
            slack_thread_ts=slack_thread_ts,
        )

    def list_deals(self, status: Optional[DealStatus] = None) -> list[Deal]:
        """List all tracked deals, optionally filtered by status.

        Args:
            status: Optional status filter.
        """
        from .store import list_deals

        return list_deals(status)

    def get_deal(self, identifier: str) -> Optional[Deal]:
        """Get a deal by ID or company name.

        Args:
            identifier: Deal ID or company name.
        """
        from .store import get_deal, get_deal_by_company

        return get_deal(identifier) or get_deal_by_company(identifier)

    def get_deal_by_thread(self, channel: str, thread_ts: str) -> Optional[Deal]:
        """Get a deal by Slack thread.

        Args:
            channel: Slack channel ID.
            thread_ts: Slack thread timestamp.
        """
        from .store import get_deal_by_thread

        return get_deal_by_thread(channel, thread_ts)

    def update_deal(
        self,
        deal_id: str,
        status: Optional[DealStatus] = None,
        term_sheet: Optional[TermSheet] = None,
        approved_by: Optional[str] = None,
        revision_note: Optional[str] = None,
    ) -> Optional[Deal]:
        """Update an existing deal.

        Args:
            deal_id: The deal ID.
            status: New status.
            term_sheet: Updated term sheet.
            approved_by: Approver username.
            revision_note: Note for revision history.
        """
        from .store import update_deal

        return update_deal(
            deal_id=deal_id,
            status=status,
            term_sheet=term_sheet,
            approved_by=approved_by,
            revision_note=revision_note,
        )

    def approve_deal(self, deal_id: str, approved_by: str) -> Optional[Deal]:
        """Approve a deal and move to approved status.

        Args:
            deal_id: The deal ID.
            approved_by: Approver username.
        """
        return self.update_deal(
            deal_id=deal_id,
            status=DealStatus.APPROVED,
            approved_by=approved_by,
            revision_note=f"Approved by {approved_by}",
        )

    def submit_for_approval(self, deal_id: str) -> Optional[Deal]:
        """Submit a deal for approval.

        Args:
            deal_id: The deal ID.
        """
        return self.update_deal(
            deal_id=deal_id,
            status=DealStatus.PENDING_APPROVAL,
            revision_note="Submitted for approval",
        )

    def mark_sent(self, deal_id: str) -> Optional[Deal]:
        """Mark a deal as sent to the company.

        Args:
            deal_id: The deal ID.
        """
        return self.update_deal(
            deal_id=deal_id,
            status=DealStatus.SENT,
            revision_note="Marked as sent",
        )

    def delete_deal(self, deal_id: str) -> bool:
        """Delete a deal.

        Args:
            deal_id: The deal ID.
        """
        from .store import delete_deal

        return delete_deal(deal_id)

    def generate_text(self, term_sheet: TermSheet) -> str:
        """Generate a plain-text term sheet.

        Args:
            term_sheet: The term sheet data.
        """
        from .generator import generate_term_sheet_text

        return generate_term_sheet_text(term_sheet)

    def generate_docx(
        self, term_sheet: TermSheet, template_file: Optional[str] = None
    ) -> bytes:
        """Generate a .docx term sheet document.

        Args:
            term_sheet: The term sheet data.
            template_file: Optional path to a .docx template to fill.
        """
        if template_file:
            from .template_filler import fill_template

            return fill_template(template_file, term_sheet)

        from .template import generate_term_sheet_docx_strict

        return generate_term_sheet_docx_strict(term_sheet)

    def generate_email(
        self, term_sheet: TermSheet, dri_name: Optional[str] = None
    ) -> str:
        """Generate a draft email for a term sheet.

        Args:
            term_sheet: The term sheet data.
            dri_name: Optional DRI name override.
        """
        from .generator import generate_draft_email

        return generate_draft_email(term_sheet, dri_name=dri_name or "")


def _client() -> TermsheetClient:
    return TermsheetClient()
