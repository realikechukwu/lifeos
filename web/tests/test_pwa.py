"""Install metadata, icons, and the privacy-safe service worker."""

from django.contrib.staticfiles import finders
from django.test import SimpleTestCase, override_settings
from django.urls import reverse


class PwaEndpointTests(SimpleTestCase):
    def test_manifest_is_public_and_installable(self):
        response = self.client.get(reverse("web:web_manifest"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("application/manifest+json"))
        self.assertIn("public", response["Cache-Control"])

        manifest = response.json()
        self.assertEqual(manifest["name"], "Family Assistant")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["theme_color"], "#5847e6")

        icon_variants = {(icon["sizes"], icon["purpose"]) for icon in manifest["icons"]}
        self.assertIn(("192x192", "any"), icon_variants)
        self.assertIn(("512x512", "any"), icon_variants)
        self.assertIn(("192x192", "maskable"), icon_variants)
        self.assertIn(("512x512", "maskable"), icon_variants)

        shortcut_urls = {shortcut["url"] for shortcut in manifest["shortcuts"]}
        self.assertIn(reverse("web:calendar"), shortcut_urls)
        self.assertIn(reverse("web:task_create"), shortcut_urls)
        self.assertIn(reverse("web:note_list"), shortcut_urls)

    def test_service_worker_has_root_scope_and_does_not_cache_private_pages(self):
        response = self.client.get(reverse("web:service_worker"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("application/javascript"))
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        self.assertIn("no-store", response["Cache-Control"])

        source = response.content.decode()
        self.assertIn('request.mode === "navigate"', source)
        self.assertIn("fetch(request).catch", source)
        self.assertNotIn("cache.put(request", source.split('request.mode === "navigate"')[1].split("return;", 1)[0])

    def test_required_static_assets_exist(self):
        assets = [
            "icons/app-icon.svg",
            "icons/app-icon-192.png",
            "icons/app-icon-512.png",
            "icons/app-icon-maskable-192.png",
            "icons/app-icon-maskable-512.png",
            "icons/apple-touch-icon.png",
            "icons/favicon.ico",
            "images/og-family-assistant.jpg",
            "offline.html",
        ]
        for asset in assets:
            with self.subTest(asset=asset):
                self.assertIsNotNone(finders.find(asset))


class PageMetadataTests(SimpleTestCase):
    @override_settings(APP_BASE_URL="https://family.example")
    def test_login_page_includes_pwa_and_social_metadata(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)

        html = response.content.decode()
        self.assertIn(f'href="{reverse("web:web_manifest")}"', html)
        self.assertIn(f'register("{reverse("web:service_worker")}"', html)
        self.assertIn('rel="apple-touch-icon"', html)
        self.assertIn('rel="mask-icon"', html)
        self.assertIn('property="og:image"', html)
        self.assertIn('name="twitter:card" content="summary_large_image"', html)
        self.assertIn(
            '<link rel="canonical" href="https://family.example/accounts/login/">',
            html,
        )
        self.assertIn(
            'content="https://family.example/static/images/og-family-assistant.jpg"',
            html,
        )
