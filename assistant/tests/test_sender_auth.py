from django.test import TestCase, override_settings

from core.models import HouseholdMember
from assistant.services.gmail import is_authorised_sender


@override_settings(AUTHORISED_EMAIL_IKE="ike@example.com", AUTHORISED_EMAIL_WIFE="wife@example.com")
class AuthorisedSenderTests(TestCase):
    def test_authorised_sender_from_env_is_accepted(self):
        self.assertTrue(is_authorised_sender("Ike@Example.com"))  # case-insensitive match
        self.assertTrue(is_authorised_sender(" wife@example.com "))

    def test_unauthorised_sender_is_rejected(self):
        self.assertFalse(is_authorised_sender("stranger@example.com"))
        self.assertFalse(is_authorised_sender(""))

    def test_authorised_household_member_from_db_is_accepted(self):
        HouseholdMember.objects.create(
            name="Nanny", email="nanny@example.com", role=HouseholdMember.Role.OTHER,
            authorised=True, active=True,
        )
        self.assertTrue(is_authorised_sender("nanny@example.com"))

    def test_inactive_or_unauthorised_household_member_is_rejected(self):
        HouseholdMember.objects.create(
            name="Old Nanny", email="old-nanny@example.com", role=HouseholdMember.Role.OTHER,
            authorised=True, active=False,
        )
        HouseholdMember.objects.create(
            name="Not Authorised", email="notauth@example.com", role=HouseholdMember.Role.OTHER,
            authorised=False, active=True,
        )
        self.assertFalse(is_authorised_sender("old-nanny@example.com"))
        self.assertFalse(is_authorised_sender("notauth@example.com"))

    def test_original_sender_inside_forwarded_content_is_never_authorised_by_itself(self):
        # The dentist's booking address appears inside the forwarded body but
        # is never the outer sender, so it must never be treated as authorised.
        self.assertFalse(is_authorised_sender("bookings@brightsmiles.example.com"))
