"""The Notes list: search, category filter and the search-as-you-type fragment."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Note

User = get_user_model()


class NoteListTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="member", password="pw123456!")
        self.client.force_login(self.user)
        Note.objects.create(title="Weekly grocery list", body="Milk\nEggs", category="Lists")
        Note.objects.create(title="Wifi", body="Router in the hallway", category="Home")

    def titles(self, response):
        return [note.title for note in response.context["notes"]]

    def test_search_matches_title_and_body(self):
        response = self.client.get(reverse("web:note_list"), {"q": "eggs"})
        self.assertEqual(self.titles(response), ["Weekly grocery list"])

    def test_category_filter(self):
        response = self.client.get(reverse("web:note_list"), {"category": "Home"})
        self.assertEqual(self.titles(response), ["Wifi"])

    def test_search_fragment_returns_only_the_results(self):
        response = self.client.get(reverse("web:note_list"), {"q": "router"}, HTTP_X_FRAGMENT="1")
        self.assertTemplateUsed(response, "web/_note_results.html")
        self.assertTemplateNotUsed(response, "base.html")
        self.assertEqual(self.titles(response), ["Wifi"])

    def test_note_form_suggests_existing_categories(self):
        response = self.client.get(reverse("web:note_create"))
        self.assertContains(response, '<option value="Lists">')
