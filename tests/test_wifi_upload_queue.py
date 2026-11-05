"""A slow HTTP request cannot block the UDP thread or the ACK listener."""
import threading
import unittest
from unittest.mock import patch
from jetson_edge.WiFi_Module import TelemetryUploader


class UploadQueueTests(unittest.TestCase):
    def test_slow_http_runs_off_receiver_thread_with_bounded_queue(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            release.wait(2)
            return True
        uploader = TelemetryUploader()
        with patch('jetson_edge.WiFi_Module.forward_to_cloud', side_effect=slow):
            uploader.start()
            try:
                uploader.enqueue(b'first', ('127.0.0.1', 1), 1)
                self.assertTrue(entered.wait(1))
                for _ in range(258):
                    uploader.enqueue(b'next', ('127.0.0.1', 1), 2)
                self.assertEqual(uploader.items.qsize(), 256)
                self.assertEqual(uploader.dropped, 2)
            finally:
                uploader.stopped.set()
                release.set()
                uploader.join(2)
        self.assertFalse(uploader.is_alive())


if __name__ == '__main__':
    unittest.main()
