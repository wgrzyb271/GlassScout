"""Background media must not block the UI with embedded video payloads."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from dashboard import style_loader


SAFARI = 'Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15'
CHROME = 'Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36'
SCRIPT = 'from dashboard.style_loader import inject_styles\ninject_styles(daytime=True)'


class BackgroundTests(unittest.TestCase):
    def test_browser_detection(self):
        self.assertTrue(style_loader._is_safari(SAFARI))
        for ua in (CHROME, '', 'Mozilla/5.0 Firefox/130.0', 'CriOS/130 Safari/604.1'):
            self.assertFalse(style_loader._is_safari(ua))

    def test_safari_does_not_register_or_emit_video(self):
        original = style_loader._media_url
        with patch.object(style_loader, '_is_safari', return_value=True), patch.object(
            style_loader, '_media_url', wraps=original
        ) as media:
            app = AppTest.from_string(SCRIPT).run()
            self.assertFalse(app.exception, [e.message for e in app.exception])
            html = '\n'.join(item.value for item in app.markdown)
            self.assertNotIn('<video', html)
            self.assertIn('app-static-bg', html)
            self.assertNotIn('base64,', html)
            self.assertTrue(all(call.args[1] != 'video/mp4' for call in media.call_args_list))

    def test_video_is_a_small_url_and_reregistered_after_rerun(self):
        original = style_loader._media_url
        with patch.object(style_loader, '_is_safari', return_value=False), patch.object(
            style_loader, '_media_url', wraps=original
        ) as media:
            app = AppTest.from_string(SCRIPT).run()
            app.run()
            self.assertFalse(app.exception, [e.message for e in app.exception])
            html = '\n'.join(item.value for item in app.markdown)
            self.assertIn('<video', html)
            self.assertNotIn('base64,', html)
            self.assertLess(len(html), 100_000)
            self.assertEqual(sum(call.args[1] == 'video/mp4' for call in media.call_args_list), 2)

    def test_missing_video_keeps_photo(self):
        with TemporaryDirectory() as directory, patch.object(
            style_loader, 'DAY_BACKGROUND_VIDEO', Path(directory) / 'missing.mp4'
        ), patch.object(style_loader, '_is_safari', return_value=False):
            app = AppTest.from_string(SCRIPT).run()
            self.assertFalse(app.exception)
            html = '\n'.join(item.value for item in app.markdown)
            self.assertNotIn('<video', html)
            self.assertIn('class="app-static-bg"', html)


if __name__ == '__main__':
    unittest.main()
