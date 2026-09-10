import unittest
from unittest.mock import MagicMock, patch
from artemis.clients.accessibility_client import AccessibilityClient, DEFAULT_PORT


class TestAccessibilityClient(unittest.TestCase):

    def setUp(self):
        self.client = AccessibilityClient(device_id="dummy_device", local_port=DEFAULT_PORT)

    @patch("artemis.clients.accessibility_client.urllib.request.urlopen")
    def test_ping_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "service": "ArtemisAccessibilityService"}'
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        self.assertTrue(self.client.ping())

    @patch("artemis.clients.accessibility_client.urllib.request.urlopen")
    def test_get_hierarchy(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true, "elements": [{"text": "OK"}]}'
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        data = self.client.get_hierarchy()
        self.assertTrue(data["success"])
        self.assertEqual(data["elements"][0]["text"], "OK")

    @patch("artemis.clients.accessibility_client.urllib.request.urlopen")
    def test_tap_action(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"success": true}'
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

    @patch("artemis.clients.accessibility_client.urllib.request.urlopen")
    def test_get_hierarchy_xml(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"<hierarchy rotation=\"0\"><node text=\"Save\" /></hierarchy>"
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        xml = self.client.get_hierarchy_xml()
        self.assertIn("<hierarchy", xml)
        self.assertIn("Save", xml)

    @patch.object(AccessibilityClient, "get_atomic_snapshot")
    def test_get_screen_data_atomic(self, mock_atomic):
        mock_atomic.return_value = {
            "success": True,
            "has_screenshot": True,
            "screenshot_base64": "dummy_b64",
            "xml": "<hierarchy rotation=\"0\"><node text=\"OK\" /></hierarchy>",
            "elements": [{"text": "OK"}],
            "width": 1080,
            "height": 2400,
        }
        screen_data = self.client.get_screen_data()
        self.assertEqual(screen_data.width, 1080)
        self.assertEqual(screen_data.height, 2400)
        self.assertEqual(screen_data.base64, "dummy_b64")
        self.assertIn("rotation=\"0\"", screen_data.hierarchy_xml)
        self.assertEqual(len(screen_data.elements), 1)

    @patch.object(AccessibilityClient, "get_atomic_snapshot", return_value=None)
    @patch.object(AccessibilityClient, "get_screenshot")
    @patch.object(AccessibilityClient, "get_hierarchy")
    def test_get_screen_data_fallback(self, mock_get_hierarchy, mock_get_screenshot, _mock_atomic):
        from PIL import Image
        mock_get_screenshot.return_value = Image.new("RGB", (1080, 2400))
        mock_get_hierarchy.return_value = {
            "success": True,
            "xml": "<hierarchy rotation=\"0\"><node text=\"Fallback\" /></hierarchy>",
            "elements": [{"text": "Fallback"}],
        }

        screen_data = self.client.get_screen_data()
        self.assertEqual(screen_data.width, 1080)
        self.assertEqual(screen_data.height, 2400)
        self.assertIn("Fallback", screen_data.hierarchy_xml)
        self.assertEqual(len(screen_data.elements), 1)


if __name__ == "__main__":
    unittest.main()

