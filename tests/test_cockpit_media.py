import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server


class CockpitMediaApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_catalog_and_file_route_use_configured_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_dir = Path(temp_dir)
            video_path = media_dir / "实车测试.mp4"
            video_path.write_bytes(b"fake-mp4")
            (media_dir / "ignore.txt").write_text("ignore", encoding="utf-8")

            with mock.patch.object(server, "COCKPIT_VIDEO_DIR", media_dir.resolve()):
                catalog = await server.cockpit_video_catalog()
                response = await server.cockpit_video_file(video_path.name)

            self.assertEqual(catalog["count"], 1)
            self.assertEqual(catalog["videos"][0]["name"], video_path.name)
            self.assertIn("%E5%AE%9E%E8%BD%A6", catalog["videos"][0]["url"])
            self.assertEqual(Path(response.path), video_path)
            self.assertEqual(response.media_type, "video/mp4")

    async def test_expected_image_route_and_comparison_resolver_share_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_dir = Path(temp_dir).resolve()
            image_path = image_dir / "解锁主动换气功能开启.jpg"
            ok, encoded = server.cv2.imencode(
                ".jpg", server.np.zeros((8, 8, 3), dtype=server.np.uint8)
            )
            self.assertTrue(ok)
            image_path.write_bytes(encoded.tobytes())

            with mock.patch.object(server, "COCKPIT_EXPECTED_IMAGE_DIR", image_dir):
                response = await server.cockpit_expected_image(image_path.name)
                resolved, decoded = server.resolve_reference_image(
                    "/api/cockpit/expected/%E8%A7%A3%E9%94%81%E4%B8%BB%E5%8A%A8%E6%8D%A2%E6%B0%94%E5%8A%9F%E8%83%BD%E5%BC%80%E5%90%AF.jpg"
                )
                public_url = server.public_reference_image_url(resolved)

            self.assertEqual(Path(response.path), image_path)
            self.assertEqual(resolved, image_path)
            self.assertEqual(decoded.shape[:2], (8, 8))
            self.assertEqual(
                public_url,
                "/api/cockpit/expected/%E8%A7%A3%E9%94%81%E4%B8%BB%E5%8A%A8%E6%8D%A2%E6%B0%94%E5%8A%9F%E8%83%BD%E5%BC%80%E5%90%AF.jpg",
            )

    def test_media_path_rejects_directory_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(server.HTTPException) as context:
                server._safe_media_file(Path(temp_dir).resolve(), "../secret.mp4", {".mp4"})
        self.assertEqual(context.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
