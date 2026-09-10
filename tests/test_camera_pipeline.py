import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from src.microscope.onway_axis_camera import AxisCameraMixin
from src.microscope.onway_camera_pipeline import LatestItemWorker


class CameraPipelineTests(unittest.TestCase):
    def test_image_write_submission_runs_off_calling_thread(self):
        camera = object.__new__(AxisCameraMixin)
        camera._camera_writer_executor = ThreadPoolExecutor(max_workers=1)
        camera._write_image_file = lambda _path, _frame: threading.get_ident()
        try:
            worker_id = camera._submit_image_write(
                "unused.png", np.zeros((2, 2, 3), dtype=np.uint8)
            ).result(timeout=1.0)
            self.assertNotEqual(worker_id, threading.get_ident())
        finally:
            camera._camera_writer_executor.shutdown(wait=True)

    def test_camera_open_request_returns_before_slow_open_finishes(self):
        camera = object.__new__(AxisCameraMixin)
        camera._camera_open_lock = threading.Lock()
        camera._camera_open_future = None
        camera._camera_pipeline_shutdown = False
        camera._camera_lifecycle_executor = ThreadPoolExecutor(max_workers=1)
        started = threading.Event()
        release = threading.Event()

        def slow_open():
            started.set()
            release.wait(1.0)

        camera._open_sdk_camera_impl = slow_open
        try:
            before = time.monotonic()
            future = camera._request_sdk_camera_open()
            elapsed = time.monotonic() - before

            self.assertLess(elapsed, 0.05)
            self.assertTrue(started.wait(1.0))
            self.assertFalse(future.done())
            self.assertIs(camera._request_sdk_camera_open(), future)
            release.set()
            future.result(timeout=1.0)
        finally:
            release.set()
            camera._camera_lifecycle_executor.shutdown(wait=True)

    def test_latest_item_worker_drops_stale_pending_items(self):
        first_started = threading.Event()
        release_first = threading.Event()
        processed = []
        worker_thread_ids = set()

        def process(value):
            worker_thread_ids.add(threading.get_ident())
            processed.append(value)
            if value == "first":
                first_started.set()
                release_first.wait(1.0)
            return value.upper()

        worker = LatestItemWorker(process, name="test-latest-camera-frame")
        try:
            worker.submit(1, "first")
            self.assertTrue(first_started.wait(1.0))
            worker.submit(2, "stale")
            worker.submit(3, "newest")
            release_first.set()

            deadline = time.monotonic() + 1.0
            result = None
            while time.monotonic() < deadline:
                result = worker.latest_result_after(1)
                if result is not None and result[0] == 3:
                    break
                time.sleep(0.01)

            self.assertIsNotNone(result)
            self.assertEqual(result[:2], (3, "NEWEST"))
            self.assertIsNone(result[2])
            self.assertEqual(processed, ["first", "newest"])
            self.assertEqual(worker.dropped_count, 1)
            self.assertNotIn(threading.get_ident(), worker_thread_ids)
        finally:
            release_first.set()
            worker.shutdown()

    def test_preview_processing_returns_requested_letterbox_size(self):
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        frame[:, :, 2] = 255

        image = AxisCameraMixin._prepare_camera_preview(
            (frame, 320, 240, "Test camera 200x100")
        )

        self.assertEqual(image.size, (320, 240))
        rgb = np.asarray(image)
        self.assertEqual(rgb.shape, (240, 320, 3))
        self.assertTrue(np.all(rgb[0, 0] == (0x0D, 0x11, 0x17)))


if __name__ == "__main__":
    unittest.main()
