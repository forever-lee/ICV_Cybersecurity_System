"""Robust comparison helpers for automotive HMI screen validation.

The module is intentionally limited to NumPy and OpenCV so that it remains
usable in the project's Python 3.7 environment.  It treats the first frame as
the expected/reference image and the second frame as the actual/candidate
image.

The pipeline keeps geometric validity separate from visual similarity:

* ORB + RANSAC estimates an actual-to-reference homography.
* A tightly constrained ECC pass may remove only small residual camera jitter.
* A warped validity mask prevents black borders from becoming differences.
* Gray-scale MS-SSIM measures structure, while CIEDE2000 measures colour.
* Configurable ROIs and ignore regions prevent a full-screen average from
  hiding a failure in a small safety-critical indicator.

The reported percentages are deterministic test scores, not probabilities.
Thresholds should be calibrated using repeated captures from the target HMI,
camera, exposure, and lighting setup.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import cv2
import numpy as np


IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
VIDEO_SUFFIXES = {".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".wmv"}

_MS_SSIM_WEIGHTS = np.asarray([0.0448, 0.2856, 0.3001, 0.2363, 0.1333], dtype=np.float64)
_EPSILON = np.finfo(np.float64).eps


@dataclass
class AlignmentResult:
    """Result of mapping the actual image into reference coordinates."""

    aligned: np.ndarray
    valid_mask: np.ndarray
    success: bool
    status: str
    method: str
    confidence: float
    transform: np.ndarray
    matches: int = 0
    inliers: int = 0
    inlier_ratio: float = 0.0
    reprojection_error: float = float("inf")
    ecc_score: Optional[float] = None
    message: str = ""


@dataclass
class ComparisonResult:
    """Visual metrics, localisation output, and the final test decision."""

    # Backwards-compatible fields used by the existing comparison UI.
    similarity: float
    difference_ratio: float
    annotated: np.ndarray
    mask: np.ndarray
    boxes: List[Tuple[int, int, int, int]]

    # HMI-specific metrics.
    ms_ssim: float
    structural_similarity: float
    color_similarity: float
    mean_delta_e: float
    p95_delta_e: float
    color_difference_ratio: float
    valid_ratio: float
    alignment: AlignmentResult
    roi_results: List[Dict[str, Any]]
    roi_similarity: Optional[float]
    passed: bool
    status: str
    critical_failures: List[str]


def source_kind(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    raise ValueError("不支持的文件格式：{}".format(suffix or path))


def read_image(path: str) -> np.ndarray:
    """Read a BGR image while supporting Unicode paths on Windows."""

    data = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("无法读取图片：{}".format(path))
    return image


def write_image(path: str, image: np.ndarray) -> None:
    """Write an image while supporting Unicode paths on Windows."""

    suffix = Path(path).suffix or ".png"
    ok, encoded = cv2.imencode(suffix, image)
    if not ok:
        raise ValueError("无法编码结果图片：{}".format(path))
    encoded.tofile(path)


def resize_to_reference(reference: np.ndarray, candidate: np.ndarray) -> np.ndarray:
    """Resize the candidate frame to the reference frame dimensions."""

    height, width = reference.shape[:2]
    if candidate.shape[:2] == (height, width):
        return candidate.copy()
    interpolation = cv2.INTER_AREA if candidate.shape[0] > height or candidate.shape[1] > width else cv2.INTER_LINEAR
    return cv2.resize(candidate, (width, height), interpolation=interpolation)


def side_by_side(first: np.ndarray, annotated_second: np.ndarray) -> np.ndarray:
    annotated_second = resize_to_reference(first, annotated_second)
    return np.hstack((first, annotated_second))


def _validate_frame(frame: np.ndarray, name: str) -> None:
    if frame is None:
        raise ValueError("{}不能为空".format(name))
    if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("{}必须是三通道 BGR 图像".format(name))
    if frame.shape[0] < 2 or frame.shape[1] < 2:
        raise ValueError("{}尺寸过小".format(name))


def _gray_u8(image: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def _scale_for_features(image: np.ndarray, maximum_dimension: int = 1280) -> Tuple[np.ndarray, float]:
    height, width = image.shape[:2]
    scale = min(1.0, float(maximum_dimension) / float(max(height, width)))
    if scale >= 0.9999:
        return image, 1.0
    resized = cv2.resize(
        image,
        (max(2, int(round(width * scale))), max(2, int(round(height * scale)))),
        interpolation=cv2.INTER_AREA,
    )
    return resized, scale


def _masked_correlation(first: np.ndarray, second: np.ndarray, mask: np.ndarray) -> float:
    valid = mask > 0
    if np.count_nonzero(valid) < 64:
        return -1.0
    first_values = first[valid].astype(np.float64)
    second_values = second[valid].astype(np.float64)
    first_values -= first_values.mean()
    second_values -= second_values.mean()
    denominator = np.linalg.norm(first_values) * np.linalg.norm(second_values)
    if denominator <= _EPSILON:
        # Two flat images with the same value are still a perfect identity
        # baseline, while differently coloured flat images are not.
        difference = float(np.mean(np.abs(first[valid].astype(np.float64) - second[valid].astype(np.float64))))
        return 1.0 if difference < 0.5 else 0.0
    return float(np.clip(np.dot(first_values, second_values) / denominator, -1.0, 1.0))


def _gradient_correlation(reference: np.ndarray, candidate: np.ndarray, valid_mask: np.ndarray) -> float:
    reference_gray = _gray_u8(reference)
    candidate_gray = _gray_u8(candidate)
    ref_x = cv2.Sobel(reference_gray, cv2.CV_32F, 1, 0, ksize=3)
    ref_y = cv2.Sobel(reference_gray, cv2.CV_32F, 0, 1, ksize=3)
    can_x = cv2.Sobel(candidate_gray, cv2.CV_32F, 1, 0, ksize=3)
    can_y = cv2.Sobel(candidate_gray, cv2.CV_32F, 0, 1, ksize=3)
    ref_gradient = cv2.magnitude(ref_x, ref_y)
    can_gradient = cv2.magnitude(can_x, can_y)
    return _masked_correlation(ref_gradient, can_gradient, valid_mask)


def _warp_candidate(candidate: np.ndarray, transform: np.ndarray, size: Tuple[int, int]) -> Tuple[np.ndarray, np.ndarray]:
    width, height = size
    aligned = cv2.warpPerspective(
        candidate,
        transform,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )
    source_mask = np.full(candidate.shape[:2], 255, dtype=np.uint8)
    valid_mask = cv2.warpPerspective(
        source_mask,
        transform,
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    # Interpolation along a homography border is unreliable.  Removing a thin
    # rim also prevents it from being reported as a UI defect.
    if min(height, width) >= 8:
        valid_mask = cv2.erode(valid_mask, np.ones((3, 3), np.uint8), iterations=1)
    valid_mask[valid_mask < 255] = 0
    return aligned, valid_mask


def _homography_alignment(reference: np.ndarray, candidate: np.ndarray) -> Optional[AlignmentResult]:
    """Estimate a guarded actual-to-reference homography using ORB/RANSAC."""

    reference_small, reference_scale = _scale_for_features(reference)
    candidate_small, candidate_scale = _scale_for_features(candidate)
    reference_gray = _gray_u8(reference_small)
    candidate_gray = _gray_u8(candidate_small)

    # A low FAST threshold helps with flat UI screens; geometric checks below
    # are deliberately stricter to reject matches clustered on one icon.
    orb = cv2.ORB_create(nfeatures=3500, scaleFactor=1.2, nlevels=8, fastThreshold=10)
    keypoints_reference, descriptors_reference = orb.detectAndCompute(reference_gray, None)
    keypoints_candidate, descriptors_candidate = orb.detectAndCompute(candidate_gray, None)
    if descriptors_reference is None or descriptors_candidate is None:
        return None
    if len(keypoints_reference) < 12 or len(keypoints_candidate) < 12:
        return None

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    raw_matches = matcher.knnMatch(descriptors_reference, descriptors_candidate, k=2)
    forward_matches = []
    for pair in raw_matches:
        if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance:
            forward_matches.append(pair[0])
    reverse_raw_matches = matcher.knnMatch(descriptors_candidate, descriptors_reference, k=2)
    reverse_pairs = set()
    for pair in reverse_raw_matches:
        if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance:
            reverse_pairs.add((pair[0].queryIdx, pair[0].trainIdx))
    # Mutual ratio matches are substantially safer on dashboards containing
    # rows of visually identical buttons and repeated status icons.
    good_matches = [
        match for match in forward_matches if (match.trainIdx, match.queryIdx) in reverse_pairs
    ]
    if len(good_matches) < 10:
        return None

    # Matcher direction is reference -> candidate, therefore the arguments to
    # findHomography are deliberately candidate points followed by reference
    # points.  The resulting H maps actual pixels into reference coordinates.
    points_reference = np.float32([keypoints_reference[m.queryIdx].pt for m in good_matches])
    points_candidate = np.float32([keypoints_candidate[m.trainIdx].pt for m in good_matches])
    ransac_threshold = max(2.0, 4.0 * min(reference_scale, candidate_scale))
    homography_scaled, inlier_mask = cv2.findHomography(
        points_candidate,
        points_reference,
        cv2.RANSAC,
        ransac_threshold,
        maxIters=3000,
        confidence=0.995,
    )
    if homography_scaled is None or inlier_mask is None:
        return None

    inlier_flags = inlier_mask.ravel().astype(bool)
    inliers = int(np.count_nonzero(inlier_flags))
    inlier_ratio = float(inliers) / float(len(good_matches))
    if inliers < 10 or inlier_ratio < 0.35:
        return None

    inlier_reference = points_reference[inlier_flags]
    inlier_candidate = points_candidate[inlier_flags]
    if inlier_reference.shape[0] < 3 or inlier_candidate.shape[0] < 3:
        return None

    reference_area = float(reference_small.shape[0] * reference_small.shape[1])
    candidate_area = float(candidate_small.shape[0] * candidate_small.shape[1])
    reference_hull_area = abs(float(cv2.contourArea(cv2.convexHull(inlier_reference))))
    candidate_hull_area = abs(float(cv2.contourArea(cv2.convexHull(inlier_candidate))))
    reference_coverage = reference_hull_area / max(reference_area, 1.0)
    candidate_coverage = candidate_hull_area / max(candidate_area, 1.0)
    if reference_coverage < 0.003 or candidate_coverage < 0.003:
        return None

    projected_scaled = cv2.perspectiveTransform(inlier_candidate.reshape(-1, 1, 2), homography_scaled).reshape(-1, 2)
    reprojection_errors = np.linalg.norm(projected_scaled - inlier_reference, axis=1)
    reprojection_error_scaled = float(np.median(reprojection_errors))
    if not np.isfinite(reprojection_error_scaled) or reprojection_error_scaled > 5.0:
        return None

    scale_reference_matrix = np.asarray(
        [[reference_scale, 0.0, 0.0], [0.0, reference_scale, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64
    )
    scale_candidate_matrix = np.asarray(
        [[candidate_scale, 0.0, 0.0], [0.0, candidate_scale, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64
    )
    transform = np.linalg.inv(scale_reference_matrix).dot(homography_scaled).dot(scale_candidate_matrix)
    if not np.all(np.isfinite(transform)) or abs(float(transform[2, 2])) <= _EPSILON:
        return None
    transform /= transform[2, 2]
    if np.linalg.cond(transform) > 1.0e10:
        return None

    reference_height, reference_width = reference.shape[:2]
    candidate_height, candidate_width = candidate.shape[:2]
    candidate_corners = np.float32(
        [[[0.0, 0.0], [candidate_width - 1.0, 0.0], [candidate_width - 1.0, candidate_height - 1.0], [0.0, candidate_height - 1.0]]]
    )
    projected_corners = cv2.perspectiveTransform(candidate_corners, transform)[0]
    if not np.all(np.isfinite(projected_corners)):
        return None
    projected_contour = projected_corners.reshape(-1, 1, 2)
    if not cv2.isContourConvex(projected_contour.astype(np.float32)):
        return None
    projected_area = abs(float(cv2.contourArea(projected_contour)))
    area_ratio = projected_area / max(float(reference_height * reference_width), 1.0)
    if area_ratio < 0.45 or area_ratio > 2.50:
        return None

    aligned, valid_mask = _warp_candidate(candidate, transform, (reference_width, reference_height))
    valid_ratio = float(np.count_nonzero(valid_mask)) / float(valid_mask.size)
    if valid_ratio < 0.65:
        return None

    reprojection_error = reprojection_error_scaled / max(reference_scale, _EPSILON)
    confidence = (
        0.45 * min(1.0, inlier_ratio / 0.70)
        + 0.20 * min(1.0, min(reference_coverage, candidate_coverage) / 0.10)
        + 0.20 * max(0.0, 1.0 - reprojection_error_scaled / 5.0)
        + 0.15 * min(1.0, valid_ratio / 0.90)
    )
    return AlignmentResult(
        aligned=aligned,
        valid_mask=valid_mask,
        success=True,
        status="aligned",
        method="orb_homography",
        confidence=float(np.clip(confidence, 0.0, 1.0)),
        transform=transform,
        matches=len(good_matches),
        inliers=inliers,
        inlier_ratio=inlier_ratio,
        reprojection_error=reprojection_error,
        message="ORB/RANSAC 配准成功",
    )


def _ecc_refinement(
    reference: np.ndarray,
    candidate: np.ndarray,
    base_result: AlignmentResult,
) -> AlignmentResult:
    """Apply a small, guarded ECC correction on top of a coarse alignment."""

    reference_gray = _gray_u8(reference).astype(np.float32) / 255.0
    aligned_gray = _gray_u8(base_result.aligned).astype(np.float32) / 255.0
    mask = base_result.valid_mask.copy()
    if np.count_nonzero(mask) < 256:
        return base_result
    valid = mask > 0
    if float(np.std(reference_gray[valid])) < 0.01 or float(np.std(aligned_gray[valid])) < 0.01:
        return base_result

    reference_blurred = cv2.GaussianBlur(reference_gray, (5, 5), 0)
    aligned_blurred = cv2.GaussianBlur(aligned_gray, (5, 5), 0)
    warp = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1.0e-5)
    try:
        ecc_score, warp = cv2.findTransformECC(
            reference_blurred,
            aligned_blurred,
            warp,
            cv2.MOTION_EUCLIDEAN,
            criteria,
            inputMask=mask,
            gaussFiltSize=5,
        )
    except (cv2.error, TypeError, ValueError):
        return base_result

    if not np.isfinite(ecc_score) or float(ecc_score) < 0.50 or not np.all(np.isfinite(warp)):
        return base_result

    a = float(warp[0, 0])
    b = float(warp[1, 0])
    scale = float(np.hypot(a, b))
    rotation_degrees = float(np.degrees(np.arctan2(b, a)))
    translation = float(np.hypot(float(warp[0, 2]), float(warp[1, 2])))
    maximum_translation = max(2.0, 0.005 * float(min(reference.shape[:2])))
    if abs(scale - 1.0) > 0.005 or abs(rotation_degrees) > 0.5 or translation > maximum_translation:
        return base_result

    warp_homogeneous = np.eye(3, dtype=np.float64)
    warp_homogeneous[:2, :] = warp.astype(np.float64)
    try:
        # findTransformECC returns the mapping used with WARP_INVERSE_MAP.
        # Converting it to a forward actual->reference transform therefore
        # requires inversion before composing with the coarse homography.
        transform = np.linalg.inv(warp_homogeneous).dot(base_result.transform)
    except np.linalg.LinAlgError:
        return base_result
    transform /= transform[2, 2]

    height, width = reference.shape[:2]
    refined, refined_mask = _warp_candidate(candidate, transform, (width, height))
    before_correlation = _gradient_correlation(reference, base_result.aligned, base_result.valid_mask)
    after_correlation = _gradient_correlation(reference, refined, refined_mask)
    if after_correlation + 0.002 < before_correlation:
        return base_result

    method = base_result.method + "+ecc"
    message = base_result.message + "，ECC 小范围微调成功"
    confidence = max(base_result.confidence, float(np.clip(ecc_score, 0.0, 1.0)))
    return AlignmentResult(
        aligned=refined,
        valid_mask=refined_mask,
        success=True,
        status="aligned",
        method=method,
        confidence=confidence,
        transform=transform,
        matches=base_result.matches,
        inliers=base_result.inliers,
        inlier_ratio=base_result.inlier_ratio,
        reprojection_error=base_result.reprojection_error,
        ecc_score=float(ecc_score),
        message=message,
    )


def align_frames(reference: np.ndarray, candidate: np.ndarray, auto_align: bool = True) -> AlignmentResult:
    """Align ``candidate`` to ``reference`` without hiding alignment failure.

    Same-size inputs always retain an identity baseline.  This is important for
    direct screenshot regression tests and also means that two intentionally
    different same-size pages still produce a valid (low) similarity score.
    For differently sized inputs, an automatic alignment failure is returned
    explicitly; the resized image is present only to make diagnostics visible.
    """

    _validate_frame(reference, "期望图像")
    _validate_frame(candidate, "实际图像")
    reference_height, reference_width = reference.shape[:2]
    same_size = candidate.shape[:2] == reference.shape[:2]

    if same_size:
        baseline_aligned = candidate.copy()
        baseline_mask = np.full(reference.shape[:2], 255, dtype=np.uint8)
        baseline_correlation = _gradient_correlation(reference, baseline_aligned, baseline_mask)
        baseline = AlignmentResult(
            aligned=baseline_aligned,
            valid_mask=baseline_mask,
            success=True,
            status="identity_baseline",
            method="identity",
            confidence=float(np.clip((baseline_correlation + 1.0) * 0.5, 0.0, 1.0)),
            transform=np.eye(3, dtype=np.float64),
            reprojection_error=0.0,
            message="输入尺寸一致，保留 identity 基线",
        )
        if np.array_equal(reference, candidate):
            baseline.confidence = 1.0
            baseline.message = "输入像素完全一致，使用 identity 基线"
            return baseline
    else:
        resized = resize_to_reference(reference, candidate)
        baseline = AlignmentResult(
            aligned=resized,
            valid_mask=np.full(reference.shape[:2], 255, dtype=np.uint8),
            success=not auto_align,
            status="resize_only" if not auto_align else "alignment_failed",
            method="resize",
            confidence=0.0,
            transform=np.asarray(
                [
                    [float(reference_width) / float(candidate.shape[1]), 0.0, 0.0],
                    [0.0, float(reference_height) / float(candidate.shape[0]), 0.0],
                    [0.0, 0.0, 1.0],
                ],
                dtype=np.float64,
            ),
            message="已按尺寸缩放（未执行自动配准）" if not auto_align else "自动配准失败；缩放画面仅用于诊断",
        )

    if not auto_align:
        return baseline

    homography = _homography_alignment(reference, candidate)
    selected = baseline
    if homography is not None:
        if same_size:
            baseline_correlation = _gradient_correlation(reference, baseline.aligned, baseline.valid_mask)
            homography_correlation = _gradient_correlation(reference, homography.aligned, homography.valid_mask)
            corners = np.float32(
                [[[0.0, 0.0], [reference_width - 1.0, 0.0], [reference_width - 1.0, reference_height - 1.0], [0.0, reference_height - 1.0]]]
            )
            moved = cv2.perspectiveTransform(corners, homography.transform)[0]
            mean_displacement = float(np.mean(np.linalg.norm(moved - corners[0], axis=1)))
            diagonal = float(np.hypot(reference_width, reference_height))
            # Do not replace a trustworthy screenshot identity with a feature
            # homography unless it improves edge agreement, or a genuinely
            # tiny correction at least preserves edge agreement.  Merely
            # being small is not enough: sub-pixel resampling can otherwise
            # create a dense false-difference mask on an already aligned
            # screenshot.
            # A same-resolution screenshot should never jump to a large
            # content-derived transform: repeated icons can otherwise produce
            # a mathematically valid but semantically wrong homography.
            if mean_displacement <= 0.05 * diagonal and (
                homography_correlation >= baseline_correlation + 0.01
                or (
                    mean_displacement <= 0.005 * diagonal
                    and homography_correlation >= baseline_correlation - 0.001
                )
            ):
                selected = homography
        else:
            selected = homography

    if not selected.success:
        return selected
    refined = _ecc_refinement(reference, candidate, selected)
    if refined.method == "identity+ecc":
        refined.status = "aligned"
    return refined


def _normalise_luminance(reference_gray: np.ndarray, candidate_gray: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Apply a conservative global exposure correction to the structure branch."""

    valid = mask > 0
    if np.count_nonzero(valid) < 64:
        return candidate_gray.astype(np.float64)
    reference_values = reference_gray[valid].astype(np.float64)
    candidate_values = candidate_gray[valid].astype(np.float64)
    reference_low, reference_high = np.percentile(reference_values, [5.0, 95.0])
    candidate_low, candidate_high = np.percentile(candidate_values, [5.0, 95.0])
    if candidate_high - candidate_low < 5.0 or reference_high - reference_low < 5.0:
        return candidate_gray.astype(np.float64)
    gain = (reference_high - reference_low) / (candidate_high - candidate_low)
    gain = float(np.clip(gain, 0.80, 1.25))
    offset = float(np.median(reference_values) - gain * np.median(candidate_values))
    offset = float(np.clip(offset, -30.0, 30.0))
    return np.clip(candidate_gray.astype(np.float64) * gain + offset, 0.0, 255.0)


def _masked_mean(values: np.ndarray, mask: np.ndarray) -> float:
    valid = mask > 0
    if np.count_nonzero(valid) == 0:
        return float("nan")
    selected = values[valid]
    selected = selected[np.isfinite(selected)]
    if selected.size == 0:
        return float("nan")
    return float(np.mean(selected))


def _ssim_components(first: np.ndarray, second: np.ndarray, mask: np.ndarray) -> Tuple[float, float, np.ndarray]:
    """Return mean SSIM, mean contrast-structure, and the local SSIM map."""

    first = first.astype(np.float64)
    second = second.astype(np.float64)
    minimum_dimension = min(first.shape[:2])
    if minimum_dimension < 3:
        similarity_map = 1.0 - np.abs(first - second) / 255.0
        similarity_map = np.clip(similarity_map, -1.0, 1.0)
        score = _masked_mean(similarity_map, mask)
        return score, score, similarity_map

    kernel_size = min(11, minimum_dimension if minimum_dimension % 2 == 1 else minimum_dimension - 1)
    kernel_size = max(3, kernel_size)
    sigma = max(0.5, 1.5 * float(kernel_size) / 11.0)
    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2

    mu_first = cv2.GaussianBlur(first, (kernel_size, kernel_size), sigma)
    mu_second = cv2.GaussianBlur(second, (kernel_size, kernel_size), sigma)
    mu_first_sq = mu_first * mu_first
    mu_second_sq = mu_second * mu_second
    mu_product = mu_first * mu_second

    sigma_first_sq = np.maximum(0.0, cv2.GaussianBlur(first * first, (kernel_size, kernel_size), sigma) - mu_first_sq)
    sigma_second_sq = np.maximum(0.0, cv2.GaussianBlur(second * second, (kernel_size, kernel_size), sigma) - mu_second_sq)
    sigma_product = cv2.GaussianBlur(first * second, (kernel_size, kernel_size), sigma) - mu_product

    luminance = (2.0 * mu_product + c1) / np.maximum(mu_first_sq + mu_second_sq + c1, _EPSILON)
    contrast_structure = (2.0 * sigma_product + c2) / np.maximum(sigma_first_sq + sigma_second_sq + c2, _EPSILON)
    similarity_map = np.nan_to_num(luminance * contrast_structure, nan=-1.0, posinf=1.0, neginf=-1.0)
    score = _masked_mean(similarity_map, mask)
    contrast_score = _masked_mean(contrast_structure, mask)
    return score, contrast_score, similarity_map


def _ms_ssim(first: np.ndarray, second: np.ndarray, mask: np.ndarray) -> Tuple[float, np.ndarray]:
    """Compute gray-scale MS-SSIM and return its native-scale SSIM map."""

    current_first = first.astype(np.float64)
    current_second = second.astype(np.float64)
    current_mask = mask.astype(np.uint8)
    scores = []
    contrast_scores = []
    native_map = None

    for level in range(len(_MS_SSIM_WEIGHTS)):
        score, contrast_score, similarity_map = _ssim_components(current_first, current_second, current_mask)
        if native_map is None:
            native_map = similarity_map
        if not np.isfinite(score) or not np.isfinite(contrast_score):
            break
        scores.append(float(np.clip(score, _EPSILON, 1.0)))
        contrast_scores.append(float(np.clip(contrast_score, _EPSILON, 1.0)))

        next_height = current_first.shape[0] // 2
        next_width = current_first.shape[1] // 2
        if level == len(_MS_SSIM_WEIGHTS) - 1 or min(next_height, next_width) < 11:
            break
        current_first = cv2.pyrDown(current_first)
        current_second = cv2.pyrDown(current_second)
        current_mask = cv2.resize(
            current_mask,
            (current_first.shape[1], current_first.shape[0]),
            interpolation=cv2.INTER_NEAREST,
        )
        current_mask[current_mask < 255] = 0
        if np.count_nonzero(current_mask) == 0:
            break

    if not scores:
        return 0.0, np.zeros(first.shape[:2], dtype=np.float64)
    if len(scores) == 1:
        return float(np.clip(scores[0], 0.0, 1.0)), native_map

    weights = _MS_SSIM_WEIGHTS[: len(scores)].copy()
    weights /= weights.sum()
    value = 1.0
    for index in range(len(scores) - 1):
        value *= contrast_scores[index] ** float(weights[index])
    value *= scores[-1] ** float(weights[-1])
    return float(np.clip(value, 0.0, 1.0)), native_map


def _bgr_to_lab(image: np.ndarray) -> np.ndarray:
    # Float input is essential: OpenCV then returns L in [0,100] and signed
    # a/b channels.  uint8 Lab has a different scale and +128 chroma offsets.
    bgr = image.astype(np.float32) / 255.0
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float64)


def _ciede2000(lab_first: np.ndarray, lab_second: np.ndarray) -> np.ndarray:
    """Vectorised CIEDE2000 colour difference for OpenCV float Lab arrays."""

    l1, a1, b1 = lab_first[..., 0], lab_first[..., 1], lab_first[..., 2]
    l2, a2, b2 = lab_second[..., 0], lab_second[..., 1], lab_second[..., 2]
    c1 = np.sqrt(a1 * a1 + b1 * b1)
    c2 = np.sqrt(a2 * a2 + b2 * b2)
    c_bar = (c1 + c2) * 0.5
    c_bar_seventh = np.power(c_bar, 7)
    twenty_five_seventh = 25.0 ** 7
    g = 0.5 * (1.0 - np.sqrt(c_bar_seventh / (c_bar_seventh + twenty_five_seventh + _EPSILON)))

    a1_prime = (1.0 + g) * a1
    a2_prime = (1.0 + g) * a2
    c1_prime = np.sqrt(a1_prime * a1_prime + b1 * b1)
    c2_prime = np.sqrt(a2_prime * a2_prime + b2 * b2)
    h1_prime = np.mod(np.degrees(np.arctan2(b1, a1_prime)), 360.0)
    h2_prime = np.mod(np.degrees(np.arctan2(b2, a2_prime)), 360.0)

    delta_l_prime = l2 - l1
    delta_c_prime = c2_prime - c1_prime
    delta_h_degrees = h2_prime - h1_prime
    zero_chroma = c1_prime * c2_prime <= _EPSILON
    delta_h_degrees = np.where(zero_chroma, 0.0, delta_h_degrees)
    delta_h_degrees = np.where((~zero_chroma) & (delta_h_degrees > 180.0), delta_h_degrees - 360.0, delta_h_degrees)
    delta_h_degrees = np.where((~zero_chroma) & (delta_h_degrees < -180.0), delta_h_degrees + 360.0, delta_h_degrees)
    delta_h_prime = 2.0 * np.sqrt(c1_prime * c2_prime) * np.sin(np.radians(delta_h_degrees) * 0.5)

    l_bar_prime = (l1 + l2) * 0.5
    c_bar_prime = (c1_prime + c2_prime) * 0.5
    hue_difference = np.abs(h1_prime - h2_prime)
    hue_sum = h1_prime + h2_prime
    h_bar_prime = np.where(zero_chroma, hue_sum, hue_sum * 0.5)
    h_bar_prime = np.where((~zero_chroma) & (hue_difference > 180.0) & (hue_sum < 360.0), (hue_sum + 360.0) * 0.5, h_bar_prime)
    h_bar_prime = np.where((~zero_chroma) & (hue_difference > 180.0) & (hue_sum >= 360.0), (hue_sum - 360.0) * 0.5, h_bar_prime)

    t = (
        1.0
        - 0.17 * np.cos(np.radians(h_bar_prime - 30.0))
        + 0.24 * np.cos(np.radians(2.0 * h_bar_prime))
        + 0.32 * np.cos(np.radians(3.0 * h_bar_prime + 6.0))
        - 0.20 * np.cos(np.radians(4.0 * h_bar_prime - 63.0))
    )
    delta_theta = 30.0 * np.exp(-np.square((h_bar_prime - 275.0) / 25.0))
    c_bar_prime_seventh = np.power(c_bar_prime, 7)
    r_c = 2.0 * np.sqrt(c_bar_prime_seventh / (c_bar_prime_seventh + twenty_five_seventh + _EPSILON))
    s_l = 1.0 + (0.015 * np.square(l_bar_prime - 50.0)) / np.sqrt(20.0 + np.square(l_bar_prime - 50.0))
    s_c = 1.0 + 0.045 * c_bar_prime
    s_h = 1.0 + 0.015 * c_bar_prime * t
    r_t = -np.sin(np.radians(2.0 * delta_theta)) * r_c

    l_term = delta_l_prime / np.maximum(s_l, _EPSILON)
    c_term = delta_c_prime / np.maximum(s_c, _EPSILON)
    h_term = delta_h_prime / np.maximum(s_h, _EPSILON)
    squared = l_term * l_term + c_term * c_term + h_term * h_term + r_t * c_term * h_term
    return np.sqrt(np.maximum(0.0, squared))


def _rect_to_pixels(
    specification: Union[Mapping[str, Any], Sequence[float]], width: int, height: int
) -> Optional[Tuple[int, int, int, int]]:
    if isinstance(specification, Mapping):
        if "rect" in specification:
            values = specification["rect"]
        else:
            values = [
                specification.get("x"),
                specification.get("y"),
                specification.get("w", specification.get("width")),
                specification.get("h", specification.get("height")),
            ]
        normalised = specification.get("normalized", specification.get("normalised"))
    else:
        values = specification
        normalised = None
    if values is None or len(values) != 4 or any(value is None for value in values):
        return None
    try:
        x, y, rectangle_width, rectangle_height = [float(value) for value in values]
    except (TypeError, ValueError):
        return None
    if not np.all(np.isfinite([x, y, rectangle_width, rectangle_height])):
        return None
    if normalised is None:
        normalised = 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and 0.0 < rectangle_width <= 1.0 and 0.0 < rectangle_height <= 1.0
    if bool(normalised):
        x *= width
        rectangle_width *= width
        y *= height
        rectangle_height *= height
    x1 = max(0, min(width, int(np.floor(x))))
    y1 = max(0, min(height, int(np.floor(y))))
    x2 = max(0, min(width, int(np.ceil(x + rectangle_width))))
    y2 = max(0, min(height, int(np.ceil(y + rectangle_height))))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2 - x1, y2 - y1


def _split_roi_config(roi_config: Optional[Any]) -> Tuple[List[Any], List[Any], Dict[str, Any]]:
    if roi_config is None:
        return [], [], {}
    if isinstance(roi_config, Mapping):
        rois = list(roi_config.get("rois", roi_config.get("regions", [])))
        ignore_regions = list(roi_config.get("ignore", roi_config.get("ignore_regions", [])))
        global_config = dict(roi_config.get("global", {}))
        for key in (
            "roi_score_weight",
            "all_rois_required",
            "min_region_area",
            "color_cap",
            "ocr_enabled",
            "ocr_language",
            "language",
            "tesseract_path",
            "engine_path",
        ):
            if key in roi_config and key not in global_config:
                global_config[key] = roi_config[key]
        return rois, ignore_regions, global_config
    if isinstance(roi_config, Sequence) and not isinstance(roi_config, (str, bytes)):
        rois = []
        ignore_regions = []
        for item in roi_config:
            if isinstance(item, Mapping) and bool(item.get("ignore", False)):
                ignore_regions.append(item)
            else:
                rois.append(item)
        return rois, ignore_regions, {}
    raise ValueError("roi_config 必须是字典、列表或 None")


def _threshold_percent(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if 0.0 <= number <= 1.0:
        return number * 100.0
    return number


def _filter_difference_mask(mask: np.ndarray, valid_mask: np.ndarray, minimum_area: float) -> Tuple[np.ndarray, List[Tuple[int, int, int, int]]]:
    binary = np.where((mask > 0) & (valid_mask > 0), 255, 0).astype(np.uint8)
    if min(binary.shape[:2]) >= 3:
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        binary[valid_mask == 0] = 0
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    filtered = np.zeros_like(binary)
    boxes = []
    for label in range(1, count):
        x, y, width, height, area = stats[label]
        if float(area) >= minimum_area:
            filtered[labels == label] = 255
            boxes.append((int(x), int(y), int(width), int(height)))
    return filtered, boxes


def _region_metrics(
    reference_gray: np.ndarray,
    candidate_gray: np.ndarray,
    delta_e: np.ndarray,
    mask: np.ndarray,
    difference_threshold: float,
    color_threshold: float,
    color_cap: float,
    minimum_area: float,
    color_enabled: bool = True,
) -> Dict[str, Any]:
    valid = mask > 0
    valid_count = int(np.count_nonzero(valid))
    if valid_count == 0:
        return {"valid": False}

    # Filling ignored pixels with the reference prevents Gaussian SSIM windows
    # from leaking differences across an ignore/valid boundary.
    candidate_for_ssim = candidate_gray.copy()
    candidate_for_ssim[~valid] = reference_gray[~valid]
    ms_ssim, similarity_map = _ms_ssim(reference_gray, candidate_for_ssim, mask)
    structural_difference = 1.0 - np.clip(similarity_map, 0.0, 1.0)
    if color_enabled:
        raw_difference = ((structural_difference > difference_threshold) | (delta_e > color_threshold)) & valid
    else:
        raw_difference = (structural_difference > difference_threshold) & valid
    filtered_mask, boxes = _filter_difference_mask(raw_difference.astype(np.uint8) * 255, mask, minimum_area)

    delta_values = delta_e[valid]
    mean_delta_e = float(np.mean(delta_values))
    p95_delta_e = float(np.percentile(delta_values, 95.0))
    color_difference_ratio = 100.0 * float(np.count_nonzero(delta_values > color_threshold)) / float(valid_count)
    color_similarity = 100.0 * (1.0 - float(np.mean(np.clip(delta_values / max(color_cap, _EPSILON), 0.0, 1.0))))
    difference_ratio = 100.0 * float(np.count_nonzero(filtered_mask)) / float(valid_count)
    structural_similarity = 100.0 * ms_ssim
    similarity = 0.80 * structural_similarity + 0.20 * color_similarity if color_enabled else structural_similarity
    return {
        "valid": True,
        "ms_ssim": ms_ssim,
        "structural_similarity": structural_similarity,
        "color_similarity": color_similarity,
        "similarity": similarity,
        "mean_delta_e": mean_delta_e,
        "p95_delta_e": p95_delta_e,
        "color_difference_ratio": color_difference_ratio,
        "difference_ratio": difference_ratio,
        "mask": filtered_mask,
        "boxes": boxes,
    }


def compare_frames(
    first: np.ndarray,
    second: np.ndarray,
    difference_threshold: float = 0.15,
    highlight_alpha: float = 0.45,
    auto_align: bool = True,
    color_threshold: Optional[float] = 8.0,
    roi_config: Optional[Any] = None,
    normalize_luminance: bool = True,
) -> ComparisonResult:
    """Compare an expected HMI frame with an actual screen frame.

    ``difference_threshold`` is applied to ``1 - local_ssim``.  The colour
    threshold is CIEDE2000 Delta-E.  ROI coordinates may be normalised (0..1)
    or absolute reference pixels.  A dictionary configuration can contain::

        {
          "ignore": [{"rect": [0, 0, 0.2, 0.1], "normalized": true}],
          "rois": [{"name": "gear", "rect": [...], "critical": true}],
          "global": {"overall_min": 95, "difference_ratio_max": 5}
        }

    Critical ROI failures always fail the test.  Alignment failure yields the
    explicit ``INVALID`` status, even though diagnostic scores are returned.
    """

    _validate_frame(first, "期望图像")
    _validate_frame(second, "实际图像")
    difference_threshold = float(np.clip(difference_threshold, 0.01, 1.0))
    highlight_alpha = float(np.clip(highlight_alpha, 0.0, 1.0))
    color_enabled = color_threshold is not None
    # Retain a finite threshold for diagnostic Delta-E fields even when colour
    # does not participate in the score, mask, or pass/fail decision.
    color_threshold_value = 8.0 if color_threshold is None else max(0.0, float(color_threshold))

    rois, ignore_regions, global_config = _split_roi_config(roi_config)
    alignment = align_frames(first, second, auto_align=auto_align)
    aligned = alignment.aligned
    height, width = first.shape[:2]

    evaluation_mask = alignment.valid_mask.copy()
    for ignore_specification in ignore_regions:
        rectangle = _rect_to_pixels(ignore_specification, width, height)
        if rectangle is not None:
            x, y, rectangle_width, rectangle_height = rectangle
            evaluation_mask[y : y + rectangle_height, x : x + rectangle_width] = 0
    # Also accept ignore entries embedded in the ROI list.
    comparison_rois = []
    for roi in rois:
        if isinstance(roi, Mapping) and bool(roi.get("ignore", False)):
            rectangle = _rect_to_pixels(roi, width, height)
            if rectangle is not None:
                x, y, rectangle_width, rectangle_height = rectangle
                evaluation_mask[y : y + rectangle_height, x : x + rectangle_width] = 0
        else:
            comparison_rois.append(roi)

    valid_count = int(np.count_nonzero(evaluation_mask))
    valid_ratio = 100.0 * float(valid_count) / float(evaluation_mask.size)
    if valid_count == 0:
        annotated = aligned.copy()
        cv2.putText(annotated, "NO VALID PIXELS", (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        return ComparisonResult(
            similarity=0.0,
            difference_ratio=0.0,
            annotated=annotated,
            mask=np.zeros(first.shape[:2], dtype=np.uint8),
            boxes=[],
            ms_ssim=0.0,
            structural_similarity=0.0,
            color_similarity=0.0,
            mean_delta_e=0.0,
            p95_delta_e=0.0,
            color_difference_ratio=0.0,
            valid_ratio=0.0,
            alignment=alignment,
            roi_results=[],
            roi_similarity=None,
            passed=False,
            status="INVALID",
            critical_failures=["没有可比较的有效像素"],
        )

    reference_gray = _gray_u8(first).astype(np.float64)
    candidate_gray = _gray_u8(aligned).astype(np.float64)
    if normalize_luminance:
        candidate_gray = _normalise_luminance(reference_gray, candidate_gray, evaluation_mask)

    delta_e = _ciede2000(_bgr_to_lab(first), _bgr_to_lab(aligned))
    color_cap = max(float(global_config.get("color_cap", 20.0)), color_threshold_value, 1.0)
    default_minimum_area = max(4.0, float(valid_count) * 0.00001)
    minimum_area = max(1.0, float(global_config.get("min_region_area", default_minimum_area)))
    global_metrics = _region_metrics(
        reference_gray,
        candidate_gray,
        delta_e,
        evaluation_mask,
        difference_threshold,
        color_threshold_value,
        color_cap,
        minimum_area,
        color_enabled=color_enabled,
    )

    structural_minimum = _threshold_percent(global_config.get("structural_min", 95.0), 95.0)
    overall_minimum = _threshold_percent(
        global_config.get("overall_min", global_config.get("min_similarity", 95.0)), 95.0
    )
    difference_ratio_maximum = _threshold_percent(
        global_config.get("difference_ratio_max", global_config.get("max_difference_ratio", 10.0)), 10.0
    )
    color_bad_ratio_maximum = _threshold_percent(global_config.get("color_bad_ratio_max", 10.0), 10.0)
    mean_delta_e_maximum = float(global_config.get("mean_delta_e_max", global_config.get("max_mean_delta_e", float("inf"))))
    global_passed = (
        global_metrics["structural_similarity"] >= structural_minimum
        and global_metrics["similarity"] >= overall_minimum
        and global_metrics["difference_ratio"] <= difference_ratio_maximum
        and (not color_enabled or global_metrics["color_difference_ratio"] <= color_bad_ratio_maximum)
        and (not color_enabled or global_metrics["mean_delta_e"] <= mean_delta_e_maximum)
    )

    roi_results = []
    critical_failures = []
    ocr_errors = []
    ocr_enabled = bool(global_config.get("ocr_enabled", False))
    weighted_score_sum = 0.0
    weight_sum = 0.0
    all_roi_results_passed = True
    roi_rectangles = []
    for index, roi_specification in enumerate(comparison_rois):
        roi_mapping = dict(roi_specification) if isinstance(roi_specification, Mapping) else {"rect": roi_specification}
        name = str(roi_mapping.get("name", "ROI {}".format(index + 1)))
        critical = bool(roi_mapping.get("critical", False))
        rectangle = _rect_to_pixels(roi_mapping, width, height)
        if rectangle is None:
            result = {"name": name, "valid": False, "critical": critical, "passed": False, "reason": "ROI 坐标无效"}
            roi_results.append(result)
            all_roi_results_passed = False
            if critical:
                critical_failures.append("{}：ROI 坐标无效".format(name))
            continue

        x, y, roi_width, roi_height = rectangle
        local_mask = evaluation_mask[y : y + roi_height, x : x + roi_width]
        local_valid_count = int(np.count_nonzero(local_mask))
        if local_valid_count == 0:
            result = {"name": name, "valid": False, "critical": critical, "passed": False, "reason": "ROI 没有有效像素", "rect": rectangle}
            roi_results.append(result)
            all_roi_results_passed = False
            if critical:
                critical_failures.append("{}：ROI 没有有效像素".format(name))
            roi_rectangles.append((rectangle, False, critical))
            continue

        local_difference_threshold = float(np.clip(roi_mapping.get("difference_threshold", difference_threshold), 0.01, 1.0))
        local_color_threshold = max(0.0, float(roi_mapping.get("color_threshold", color_threshold_value)))
        local_minimum_area = max(1.0, float(roi_mapping.get("min_area", max(4.0, local_valid_count * 0.0001))))
        metrics = _region_metrics(
            reference_gray[y : y + roi_height, x : x + roi_width],
            candidate_gray[y : y + roi_height, x : x + roi_width],
            delta_e[y : y + roi_height, x : x + roi_width],
            local_mask,
            local_difference_threshold,
            local_color_threshold,
            max(color_cap, local_color_threshold, 1.0),
            local_minimum_area,
            color_enabled=color_enabled,
        )
        roi_structural_minimum = _threshold_percent(roi_mapping.get("structural_min", structural_minimum), structural_minimum)
        roi_overall_minimum = _threshold_percent(
            roi_mapping.get("overall_min", roi_mapping.get("min_similarity", overall_minimum)), overall_minimum
        )
        roi_difference_maximum = _threshold_percent(
            roi_mapping.get("difference_ratio_max", roi_mapping.get("max_difference_ratio", difference_ratio_maximum)),
            difference_ratio_maximum,
        )
        roi_color_bad_maximum = _threshold_percent(roi_mapping.get("color_bad_ratio_max", color_bad_ratio_maximum), color_bad_ratio_maximum)
        delta_e_p95_maximum = float(roi_mapping.get("delta_e_p95_max", roi_mapping.get("color_de_max", float("inf"))))
        mean_delta_e_maximum = float(
            roi_mapping.get("mean_delta_e_max", roi_mapping.get("max_mean_delta_e", float("inf")))
        )
        ocr_required = bool(roi_mapping.get("ocr", False))
        expected_text = roi_mapping.get("expected_text")
        roi_passed = (
            metrics["structural_similarity"] >= roi_structural_minimum
            and metrics["similarity"] >= roi_overall_minimum
            and metrics["difference_ratio"] <= roi_difference_maximum
            and (not color_enabled or metrics["color_difference_ratio"] <= roi_color_bad_maximum)
            and (not color_enabled or metrics["p95_delta_e"] <= delta_e_p95_maximum)
            and (not color_enabled or metrics["mean_delta_e"] <= mean_delta_e_maximum)
        )
        ocr_status = "NOT_REQUESTED"
        ocr_passed = None
        ocr_details = None
        actual_text = None
        if ocr_required and not ocr_enabled:
            ocr_status = "SKIPPED"
        elif ocr_required:
            language = str(
                roi_mapping.get(
                    "language",
                    global_config.get("ocr_language", global_config.get("language", "chi_sim+eng")),
                )
            )
            tesseract_path = str(
                roi_mapping.get(
                    "tesseract_path",
                    roi_mapping.get(
                        "engine_path",
                        global_config.get("tesseract_path", global_config.get("engine_path", "")),
                    ),
                )
                or ""
            )
            try:
                # OCR stays optional and is imported only for a requested ROI.
                from ocr_adapter import compare_roi_text

                ocr_comparison = compare_roi_text(
                    first[y : y + roi_height, x : x + roi_width],
                    aligned[y : y + roi_height, x : x + roi_width],
                    expected_text=str(expected_text or ""),
                    language=language,
                    tesseract_path=tesseract_path,
                )
                ocr_details = ocr_comparison.as_dict()
                actual_text = ocr_comparison.actual_text
                if ocr_comparison.completed:
                    ocr_passed = bool(ocr_comparison.passed)
                    ocr_status = "PASS" if ocr_passed else "MISMATCH"
                    roi_passed = roi_passed and ocr_passed
                else:
                    ocr_status = "UNAVAILABLE" if not ocr_comparison.available else "ERROR"
                    ocr_errors.append("{}：{}".format(name, ocr_comparison.message))
            except Exception as error:
                ocr_status = "ERROR"
                ocr_errors.append("{}：OCR 调用失败：{}".format(name, error))
        all_roi_results_passed = all_roi_results_passed and roi_passed
        weight = max(0.0, float(roi_mapping.get("weight", 1.0)))
        if weight > 0.0:
            weighted_score_sum += weight * metrics["similarity"]
            weight_sum += weight
        result = {
            "name": name,
            "rect": rectangle,
            "valid": True,
            "critical": critical,
            "weight": weight,
            "passed": roi_passed,
            "similarity": metrics["similarity"],
            "ms_ssim": metrics["ms_ssim"],
            "structural_similarity": metrics["structural_similarity"],
            "color_similarity": metrics["color_similarity"],
            "mean_delta_e": metrics["mean_delta_e"],
            "p95_delta_e": metrics["p95_delta_e"],
            "color_difference_ratio": metrics["color_difference_ratio"],
            "difference_ratio": metrics["difference_ratio"],
            "ocr_required": ocr_required,
            "expected_text": expected_text,
            "ocr_status": ocr_status,
            "ocr_passed": ocr_passed,
            "actual_text": actual_text,
            "ocr_details": ocr_details,
        }
        roi_results.append(result)
        roi_rectangles.append((rectangle, roi_passed, critical))
        if critical and not roi_passed:
            if ocr_status == "MISMATCH":
                critical_failures.append("{}：关键区域文字不一致".format(name))
            else:
                critical_failures.append("{}：关键区域未达到视觉阈值".format(name))

    roi_similarity = weighted_score_sum / weight_sum if weight_sum > 0.0 else None
    final_similarity = global_metrics["similarity"]
    if roi_similarity is not None:
        roi_score_weight = float(np.clip(global_config.get("roi_score_weight", 0.50), 0.0, 1.0))
        final_similarity = (1.0 - roi_score_weight) * final_similarity + roi_score_weight * roi_similarity

    all_rois_required = bool(global_config.get("all_rois_required", False))
    passed = (
        alignment.success
        and not ocr_errors
        and global_passed
        and not critical_failures
        and (all_roi_results_passed or not all_rois_required)
    )
    invalid_result = not alignment.success or bool(ocr_errors)
    status = "PASS" if passed else ("INVALID" if invalid_result else "FAIL")

    difference_mask = global_metrics["mask"]
    boxes = global_metrics["boxes"]
    red_layer = aligned.copy()
    red_layer[difference_mask > 0] = (0, 0, 255)
    annotated = cv2.addWeighted(aligned, 1.0 - highlight_alpha, red_layer, highlight_alpha, 0.0)
    for x, y, box_width, box_height in boxes:
        cv2.rectangle(annotated, (x, y), (x + box_width, y + box_height), (0, 0, 255), 2)
    for rectangle, roi_passed, critical in roi_rectangles:
        x, y, roi_width, roi_height = rectangle
        colour = (0, 180, 0) if roi_passed else ((0, 0, 255) if critical else (0, 165, 255))
        cv2.rectangle(annotated, (x, y), (x + roi_width, y + roi_height), colour, 1)

    label = "{}  Score:{:.2f}%  MS-SSIM:{:.2f}%  dE95:{:.2f}".format(
        status,
        final_similarity,
        global_metrics["structural_similarity"],
        global_metrics["p95_delta_e"],
    )
    label_width = min(annotated.shape[1], max(360, 11 * len(label)))
    cv2.rectangle(annotated, (0, 0), (label_width, 34), (0, 0, 0), -1)
    cv2.putText(annotated, label, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    if not alignment.success:
        cv2.putText(annotated, "ALIGNMENT FAILED", (10, 64), cv2.FONT_HERSHEY_SIMPLEX, 0.70, (0, 0, 255), 2)

    return ComparisonResult(
        similarity=float(np.clip(final_similarity, 0.0, 100.0)),
        difference_ratio=global_metrics["difference_ratio"],
        annotated=annotated,
        mask=difference_mask,
        boxes=boxes,
        ms_ssim=global_metrics["ms_ssim"],
        structural_similarity=global_metrics["structural_similarity"],
        color_similarity=global_metrics["color_similarity"],
        mean_delta_e=global_metrics["mean_delta_e"],
        p95_delta_e=global_metrics["p95_delta_e"],
        color_difference_ratio=global_metrics["color_difference_ratio"],
        valid_ratio=valid_ratio,
        alignment=alignment,
        roi_results=roi_results,
        roi_similarity=roi_similarity,
        passed=passed,
        status=status,
        critical_failures=critical_failures + ["OCR 无效：{}".format(message) for message in ocr_errors],
    )


def _video_frame_descriptor(frame: np.ndarray) -> np.ndarray:
    gray = _gray_u8(frame)
    small = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    mean = float(np.mean(small))
    standard_deviation = float(np.std(small))
    normalised = (small - mean) / max(standard_deviation, 0.05)
    gradient_x = cv2.Sobel(small, cv2.CV_32F, 1, 0, ksize=3)
    gradient_y = cv2.Sobel(small, cv2.CV_32F, 0, 1, ksize=3)
    gradient = cv2.magnitude(gradient_x, gradient_y)
    # Repeated mean/std entries keep brightness transitions visible without
    # allowing exposure differences to dominate structural content.
    descriptor = np.concatenate(
        (normalised.ravel(), gradient.ravel(), np.full(16, mean, dtype=np.float32), np.full(16, standard_deviation, dtype=np.float32))
    ).astype(np.float64)
    descriptor -= descriptor.mean()
    norm = float(np.linalg.norm(descriptor))
    if norm <= _EPSILON:
        return descriptor
    return descriptor / norm


def _sample_video_descriptors(path: str, sample_fps: float, max_samples: int) -> Tuple[np.ndarray, np.ndarray]:
    capture = cv2.VideoCapture(path)
    if not capture.isOpened():
        raise ValueError("无法打开视频：{}".format(path))
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        frame_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        descriptors = []
        timestamps = []
        interval = 1.0 / sample_fps
        if fps > 0.0 and frame_count > 0.0:
            duration = frame_count / fps
            sample_count = min(max_samples, max(1, int(np.floor(duration * sample_fps)) + 1))
            for index in range(sample_count):
                timestamp = index * interval
                if timestamp > duration:
                    break
                capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
                ok, frame = capture.read()
                if not ok:
                    continue
                descriptors.append(_video_frame_descriptor(frame))
                timestamps.append(timestamp)
        else:
            # Some codecs report neither FPS nor frame count.  Sequential
            # sampling still provides a useful offset estimate.
            assumed_fps = fps if fps > 0.0 else 25.0
            step = max(1, int(round(assumed_fps / sample_fps)))
            frame_index = 0
            while len(descriptors) < max_samples:
                ok, frame = capture.read()
                if not ok:
                    break
                if frame_index % step == 0:
                    descriptors.append(_video_frame_descriptor(frame))
                    timestamps.append(frame_index / assumed_fps)
                frame_index += 1
        if not descriptors:
            raise ValueError("视频中没有可读取的帧：{}".format(path))
        return np.vstack(descriptors), np.asarray(timestamps, dtype=np.float64)
    finally:
        capture.release()


def estimate_video_offset(
    source_a: str,
    source_b: str,
    max_offset_seconds: float = 5.0,
    sample_fps: float = 2.0,
    max_samples: int = 120,
) -> Dict[str, Any]:
    """Estimate the coarse temporal offset between two videos.

    A positive result means video B lags video A: compare ``A(t)`` with
    ``B(t + offset_seconds)``.  Static videos are deliberately reported as
    ambiguous because an offset cannot be inferred from unchanged content.
    """

    if source_kind(source_a) != "video" or source_kind(source_b) != "video":
        raise ValueError("时间偏移估计仅支持两个视频文件")
    sample_fps = float(sample_fps)
    max_offset_seconds = abs(float(max_offset_seconds))
    max_samples = int(max_samples)
    if sample_fps <= 0.0 or max_samples < 3:
        raise ValueError("sample_fps 必须大于 0，max_samples 必须至少为 3")

    descriptors_a, timestamps_a = _sample_video_descriptors(source_a, sample_fps, max_samples)
    descriptors_b, timestamps_b = _sample_video_descriptors(source_b, sample_fps, max_samples)
    maximum_shift = int(round(max_offset_seconds * sample_fps))
    minimum_pairs = max(3, min(len(descriptors_a), len(descriptors_b)) // 3)
    candidates = []
    for shift in range(-maximum_shift, maximum_shift + 1):
        start_a = max(0, -shift)
        start_b = max(0, shift)
        pairs = min(len(descriptors_a) - start_a, len(descriptors_b) - start_b)
        if pairs < minimum_pairs:
            continue
        first = descriptors_a[start_a : start_a + pairs]
        second = descriptors_b[start_b : start_b + pairs]
        frame_scores = np.sum(first * second, axis=1)
        score = float(np.mean(np.clip(frame_scores, -1.0, 1.0)))
        candidates.append((score, shift, pairs))
    if not candidates:
        return {
            "success": False,
            "status": "insufficient_overlap",
            "offset_seconds": 0.0,
            "score": 0.0,
            "confidence": 0.0,
            "samples": 0,
            "message": "两个视频没有足够的重叠采样帧",
        }

    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_shift, best_pairs = candidates[0]
    non_neighbour_scores = [score for score, shift, _ in candidates[1:] if abs(shift - best_shift) > 1]
    second_score = max(non_neighbour_scores) if non_neighbour_scores else (candidates[1][0] if len(candidates) > 1 else -1.0)

    def temporal_activity(descriptors: np.ndarray) -> float:
        if len(descriptors) < 2:
            return 0.0
        adjacent_similarity = np.sum(descriptors[:-1] * descriptors[1:], axis=1)
        return float(np.mean(np.clip(1.0 - adjacent_similarity, 0.0, 2.0)))

    activity = 0.5 * (temporal_activity(descriptors_a) + temporal_activity(descriptors_b))
    uniqueness = max(0.0, best_score - second_score)
    score_quality = np.clip((best_score - 0.20) / 0.80, 0.0, 1.0)
    activity_quality = np.clip(activity / 0.05, 0.0, 1.0)
    uniqueness_quality = np.clip(uniqueness / 0.05, 0.0, 1.0)
    confidence = float(score_quality * activity_quality * uniqueness_quality)
    success = confidence >= 0.10
    offset_seconds = float(best_shift) / sample_fps
    return {
        "success": success,
        "status": "ok" if success else "ambiguous",
        "offset_seconds": offset_seconds,
        "score": 100.0 * float(np.clip((best_score + 1.0) * 0.5, 0.0, 1.0)),
        "confidence": confidence,
        "samples": int(best_pairs),
        "sample_fps": sample_fps,
        "activity": activity,
        "tested_offsets": len(candidates),
        "duration_a": float(timestamps_a[-1]) if len(timestamps_a) else 0.0,
        "duration_b": float(timestamps_b[-1]) if len(timestamps_b) else 0.0,
        "message": "视频时间偏移估计成功" if success else "视频内容变化不足或最佳偏移不唯一",
    }


__all__ = [
    "AlignmentResult",
    "ComparisonResult",
    "align_frames",
    "compare_frames",
    "estimate_video_offset",
    "read_image",
    "resize_to_reference",
    "side_by_side",
    "source_kind",
    "write_image",
]
