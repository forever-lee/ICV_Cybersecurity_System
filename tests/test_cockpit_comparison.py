import pytest


cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from fastapi import HTTPException

import server
from hmi_comparison import compare_frames


def test_identical_hmi_images_pass():
    image = np.full((240, 320, 3), 127, dtype=np.uint8)

    result = compare_frames(image, image.copy())

    assert result.status == "PASS"
    assert result.similarity == pytest.approx(100.0)


def test_reference_image_must_stay_inside_library():
    with pytest.raises(HTTPException) as error:
        server.resolve_reference_image(
            "/static/reference-library/../styles.css"
        )

    assert error.value.status_code == 400
    assert error.value.detail == "invalid expected_image path"


def test_vehicle_snapshot_exposes_cockpit_stream_without_changing_domains():
    state = server.VehicleState("VHC-TEST")
    state.metrics.update(codec="h265", encoded_fps=20, upload_kbps=2200)

    snapshot = state.snapshot()

    assert snapshot["cockpit_stream"]["codec"] == "h265"
    assert snapshot["cockpit_stream"]["encoded_fps"] == 20
    assert snapshot["domains"]["body"]["status"] == "offline"
