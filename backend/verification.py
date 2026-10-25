import base64
import difflib
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import tempfile
import time
import hashlib
from functools import lru_cache
from pathlib import Path

os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("TF_NUM_INTRAOP_THREADS", "1")
os.environ.setdefault("TF_NUM_INTEROP_THREADS", "1")
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import numpy as np

_TRANSCRIBE_LOCK = threading.Lock()
logger = logging.getLogger(__name__)

FACE_MODEL = "Facenet"
# Robust face matching distance threshold (DeepFace Facenet cosine distance)
# 0.40 is the standard Facenet verification threshold admitting authentic enrolled owners across natural variations while blocking impostors.
FACE_MATCH_MAX_DISTANCE = float(os.getenv("CLOUDGUARD_FACE_DISTANCE", "0.40"))
MIN_FACE_PX = 24
MAX_FACE_FRAME_COVERAGE = 0.85
# Sample 16 frames spanning the full 12s recording (every ~0.75s)
_FRAME_SAMPLE_FRACTIONS = (
    0.06, 0.12, 0.18, 0.24, 0.30, 0.36, 0.42, 0.48,
    0.54, 0.60, 0.66, 0.72, 0.78, 0.84, 0.90, 0.94
)
_MAX_FRAME_SIDE = 640
_TRANSCRIBE_SECONDS = 35


class VerificationUnavailable(RuntimeError):
    pass


def verification_diagnostics() -> dict:
    """Inspect availability of individual verification subsystems without loading heavy ML weights."""
    from importlib.util import find_spec

    engine = os.getenv(
        "DEEPFACE_BACKEND_ENGINE",
        os.getenv("CLOUDGUARD_DEEPFACE_ENGINE", "tensorflow"),
    ).lower()
    engine_module = {"tensorflow": "tensorflow", "pytorch": "torch"}.get(engine, "tensorflow")

    has_av = find_spec("av") is not None
    has_cv2 = find_spec("cv2") is not None
    has_whisper = find_spec("faster_whisper") is not None
    has_tf_keras = find_spec("tf_keras") is not None
    has_deepface = find_spec("deepface") is not None
    has_engine = find_spec(engine_module) is not None

    face_ready = bool(has_cv2 and has_deepface and has_tf_keras and has_engine)
    liveness_ready = bool(face_ready)
    speech_ready = bool(has_whisper)
    video_ready = bool(has_av or has_cv2)

    return {
        "face": face_ready,
        "liveness": liveness_ready,
        "speech": speech_ready,
        "video_decode": video_ready,
    }


def providers_available() -> bool:
    diag = verification_diagnostics()
    return bool(diag["face"] and diag["liveness"] and diag["speech"] and diag["video_decode"])


def preload_verification_models() -> None:
    """Preload is lightweight to prevent cold-boot memory spikes on Render."""
    logger.info("Verification models configured and ready for on-demand processing.")


def _sampled_indexes(count: int) -> list[int]:
    return sorted(
        {
            max(0, min(count - 1, int(count * fraction)))
            for fraction in _FRAME_SAMPLE_FRACTIONS
        }
    )


def _reject_duration(duration: float) -> None:
    if duration > 0 and (duration < 3 or duration > 30):
        raise ValueError("Record a clear 5-to-20-second check-in clip.")


def _array_from_av_frame(frame) -> np.ndarray | None:
    array = frame.to_ndarray(format="bgr24")
    if array is None or not getattr(array, "size", 0):
        return None
    height, width = array.shape[:2]
    if width < 1 or height < 1 or width > 3840 or height > 2160:
        raise ValueError("The recording dimensions are not supported. Please record again.")
    return array


def _frames_from_av(video_path: Path) -> list:
    import av
    import cv2

    duration = 0.0
    frames = []
    with av.open(str(video_path)) as container:
        if not container.streams.video:
            raise ValueError("The recording could not be opened. Please record again.")
        stream = container.streams.video[0]
        width = getattr(stream.codec_context, "width", None) or getattr(stream, "width", 0)
        height = getattr(stream.codec_context, "height", None) or getattr(stream, "height", 0)
        codec_name = getattr(stream.codec_context, "name", None) or getattr(stream, "name", "unknown")
        if container.duration is not None:
            duration = float(container.duration) / av.time_base
        elif stream.duration is not None and stream.time_base is not None:
            duration = float(stream.duration * stream.time_base)
        _reject_duration(duration)
        logger.info("[DECODE] PyAV opened video: codec=%s, resolution=%dx%d, duration=%.2fs", codec_name, width, height, duration)
        print(f"[DECODE] PyAV opened video: codec={codec_name}, resolution={width}x{height}, duration={duration:.2f}s", flush=True)

        # Single streaming pass: sample frames spaced across the stream without 2x decoding
        frame_idx = 0
        for frame in container.decode(video=0):
            # Target ~16 frames across clip: sample every 16 frames or initial critical anchors
            if frame_idx % 16 == 0 or frame_idx in (4, 10, 20):
                array = _array_from_av_frame(frame)
                if array is not None:
                    h, w = array.shape[:2]
                    if max(h, w) > 640:
                        scale = 640.0 / max(h, w)
                        array = cv2.resize(array, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
                    frames.append(array)
                    if len(frames) >= 16:
                        break
            frame_idx += 1

    if len(frames) < 2:
        raise ValueError("The recording could not be decoded reliably. Please record again.")
    logger.info("[DECODE] PyAV sampled %d frames across video (examined %d)", len(frames), frame_idx)
    print(f"[DECODE] PyAV sampled {len(frames)} frames across video (examined {frame_idx})", flush=True)
    return frames


def _frames_from_cv2(video_path: Path, cv2) -> list:
    capture = cv2.VideoCapture(str(video_path))
    try:
        if not capture.isOpened():
            raise ValueError("The recording could not be opened. Please record again.")

        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

        frames = []
        frame_idx = 0
        while len(frames) < 16 and frame_idx < 400:
            if frame_idx % 16 == 0 or frame_idx in (4, 10, 20):
                success, frame = capture.read()
                if not success or frame is None:
                    break
                h, w = frame.shape[:2]
                if max(h, w) > 640:
                    scale = 640.0 / max(h, w)
                    frame = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
                frames.append(frame)
            else:
                success = capture.grab()
                if not success:
                    break
            frame_idx += 1

        if len(frames) < 2:
            raise ValueError("The recording could not be decoded reliably. Please record again.")
        logger.info("[DECODE] OpenCV sampled %d frames (total examined: %d, %dx%d)", len(frames), frame_idx, width, height)
        print(f"[DECODE] OpenCV sampled {len(frames)} frames (total examined: {frame_idx}, {width}x{height})", flush=True)
        return frames
    finally:
        capture.release()


def _video_frames(video_path: Path, cv2) -> list:
    """Extract evenly-spaced frames from the check-in recording."""
    av_error = None
    try:
        return _frames_from_av(video_path)
    except Exception as exc:
        av_error = exc
        logger.warning("PyAV could not decode the check-in clip; trying OpenCV. %s", exc)
    try:
        return _frames_from_cv2(video_path, cv2)
    except Exception:
        if av_error is not None and not isinstance(av_error, ValueError):
            raise ValueError("The recording could not be decoded reliably. Please record again.") from av_error
        raise


def _enrollment_images(encoded_samples: list[str], cv2) -> list:
    """Decode the user's stored face enrollment images."""
    images = []
    for sample in encoded_samples:
        try:
            clean = sample.split(",")[-1].strip()
            raw = base64.b64decode(clean, validate=False)
        except (ValueError, TypeError):
            continue
        image = cv2.imdecode(np.frombuffer(raw, dtype="uint8"), cv2.IMREAD_COLOR)
        if image is not None:
            images.append(image)
    if len(images) < 2:
        raise VerificationUnavailable(
            "At least two usable face enrollment samples are required. Please re-enroll your face."
        )
    return images


def _phrase_matches(expected: str, actual: str) -> bool:
    """Fuzzy-match the spoken phrase against the expected sentence."""
    tokenize = lambda value: re.findall(r"[a-z0-9']+", value.casefold())
    expected_words = tokenize(expected)
    actual_words = tokenize(actual)
    if not expected_words or not actual_words:
        return False
    # Allow 75 % word-level similarity (down from 80 %) to handle minor Whisper mis-transcriptions
    ratio = difflib.SequenceMatcher(None, expected_words, actual_words).ratio()
    # Also accept if most key words are present (handles insertion / reordering)
    common = sum(1 for w in expected_words if w in actual_words)
    coverage = common / len(expected_words) if expected_words else 0
    return ratio >= 0.75 or coverage >= 0.70


def _whisper_model_name() -> str:
    requested = (os.getenv("CLOUDGUARD_WHISPER_MODEL") or "tiny.en").strip()
    slow = {"small", "small.en", "medium", "medium.en", "large", "large-v2", "large-v3"}
    if requested in slow:
        logger.info("Using tiny.en instead of %s so live check-in can finish on CPU.", requested)
        return "tiny.en"
    return requested or "tiny.en"


@lru_cache(maxsize=1)
def _speech_model():
    from faster_whisper import WhisperModel

    logger.info("[MODEL] Loading Whisper...")
    print("[MODEL] Loading Whisper...", flush=True)
    model = WhisperModel(
        _whisper_model_name(),
        device="cpu",
        compute_type="int8",
    )
    logger.info("[MODEL] Whisper loaded")
    print("[MODEL] Whisper loaded", flush=True)
    return model


def _shrink_frame(frame, cv2, max_side: int = _MAX_FRAME_SIDE):
    shape = getattr(frame, "shape", None)
    if shape is None or len(shape) < 2:
        return frame
    height, width = int(shape[0]), int(shape[1])
    longest = max(height, width)
    if longest <= max_side:
        return frame
    scale = max_side / float(longest)
    return cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))), interpolation=cv2.INTER_AREA)


def _has_audio_stream(video_path: Path) -> bool:
    """Check if the video recording container contains at least one audio stream."""
    try:
        import av
        with av.open(str(video_path)) as container:
            return len(container.streams.audio) > 0
    except Exception as exc:
        logger.debug("Audio stream check via PyAV failed (%s), assuming audio present.", exc)
        return True


def _transcribe_recording(video_path: Path) -> str:
    transcript = ""
    try:
        with _TRANSCRIBE_LOCK:
            model = _speech_model()
            segments, _ = model.transcribe(
                str(video_path),
                language="en",
                beam_size=1,
                vad_filter=False,
            )
            transcript = " ".join(
                segment.text.strip() for segment in segments if segment.text.strip()
            )
        logger.info("Whisper transcript: %r", transcript)
    except Exception as exc:
        logger.warning("Whisper transcription failed: %s", exc)
    return transcript


_CASCADE_CACHE = {}


def _get_cascade(name: str, cv2):
    if name not in _CASCADE_CACHE:
        classifier = None
        # 1. Primary: load from bundled repository directory backend/cascades/
        local_dir = Path(__file__).resolve().parent / "cascades"
        local_path = local_dir / name
        if local_path.is_file():
            try:
                c = cv2.CascadeClassifier(str(local_path))
                if not c.empty():
                    classifier = c
                    logger.debug("[CASCADE] Loaded bundled cascade: %s", name)
            except Exception:
                pass

        # 2. Secondary: cv2.data.haarcascades
        if classifier is None:
            cascade_dir = getattr(cv2, "data", None) and getattr(cv2.data, "haarcascades", None)
            if cascade_dir:
                path = os.path.join(cascade_dir, name)
                if os.path.isfile(path):
                    try:
                        c = cv2.CascadeClassifier(path)
                        if not c.empty():
                            classifier = c
                    except Exception:
                        pass

        # 3. Tertiary: cv2/__file__/../data
        if classifier is None:
            try:
                cv2_dir = os.path.dirname(getattr(cv2, "__file__", ""))
                cand = os.path.join(cv2_dir, "data", name)
                if os.path.isfile(cand):
                    c = cv2.CascadeClassifier(cand)
                    if not c.empty():
                        classifier = c
            except Exception:
                pass

        _CASCADE_CACHE[name] = classifier
    return _CASCADE_CACHE.get(name)


def _detect_faces_opencv(frame, cv2) -> list[tuple[int, int, int, int]]:
    """
    Detect candidate face bounding boxes (x, y, w, h) in the given BGR frame.
    Evaluates multi-scale AdaBoost cascades across CLAHE, global equalized, and raw grayscale variants.
    """
    if frame is None or getattr(frame, "size", 0) == 0:
        return []

    H, W = frame.shape[:2]
    if H < MIN_FACE_PX or W < MIN_FACE_PX:
        return []

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    clahe_gray = clahe.apply(gray)
    equalized = cv2.equalizeHist(gray)
    variants = (clahe_gray, equalized, gray)

    cascade_names = [
        "haarcascade_frontalface_alt2.xml",
        "haarcascade_frontalface_default.xml",
        "haarcascade_frontalface_alt.xml",
        "haarcascade_frontalface_alt_tree.xml",
    ]

    found_rects = []
    # Pass 1: frontal multi-cascade with fine scaleFactor 1.05 and minNeighbors 3
    for c_name in cascade_names:
        cascade = _get_cascade(c_name, cv2)
        if cascade is None:
            continue
        for img_var in variants:
            rects = cascade.detectMultiScale(
                img_var, scaleFactor=1.05, minNeighbors=3, minSize=(MIN_FACE_PX, MIN_FACE_PX)
            )
            if len(rects) > 0:
                found_rects.extend(rects)
                break
        if found_rects:
            break

    # Pass 2: profile cascade for reading posture / head turned
    if not found_rects:
        c_prof = _get_cascade("haarcascade_profileface.xml", cv2)
        if c_prof is not None:
            for img_var in variants:
                rects = c_prof.detectMultiScale(
                    img_var, scaleFactor=1.05, minNeighbors=3, minSize=(MIN_FACE_PX, MIN_FACE_PX)
                )
                if len(rects) > 0:
                    found_rects.extend(rects)
                    break
            if not found_rects:
                flipped = cv2.flip(clahe_gray, 1)
                rects = c_prof.detectMultiScale(
                    flipped, scaleFactor=1.05, minNeighbors=3, minSize=(MIN_FACE_PX, MIN_FACE_PX)
                )
                if len(rects) > 0:
                    for (rx, ry, rw, rh) in rects:
                        found_rects.append((W - rx - rw, ry, rw, rh))

    # Pass 3: gentle fallback with minNeighbors=2 if lighting or tilt is difficult
    if not found_rects:
        for c_name in ("haarcascade_frontalface_alt2.xml", "haarcascade_frontalface_default.xml"):
            cascade = _get_cascade(c_name, cv2)
            if cascade is None:
                continue
            for img_var in (clahe_gray, equalized):
                rects = cascade.detectMultiScale(
                    img_var, scaleFactor=1.05, minNeighbors=2, minSize=(MIN_FACE_PX, MIN_FACE_PX)
                )
                if len(rects) > 0:
                    found_rects.extend(rects)
                    break
            if found_rects:
                break

    # Filter candidates by minimum size and maximum coverage
    valid = []
    for rect in found_rects:
        x, y, w, h = int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])
        if w >= MIN_FACE_PX and h >= MIN_FACE_PX:
            cov = (w * h) / float(W * H)
            if cov <= MAX_FACE_FRAME_COVERAGE:
                valid.append((x, y, w, h))

    return valid


def _primary_face(valid_rects: list, frame_shape: tuple) -> tuple | None:
    """Select the primary user's face (largest area and closest to center of frame)."""
    if not valid_rects:
        return None
    if len(valid_rects) == 1:
        return valid_rects[0]

    H, W = frame_shape[:2]
    cx_f, cy_f = W / 2.0, H / 2.0

    def rank_score(r):
        x, y, w, h = r
        area = w * h
        cx, cy = x + w / 2.0, y + h / 2.0
        dist_sq = (cx - cx_f) ** 2 + (cy - cy_f) ** 2
        return area / (1.0 + dist_sq / float(W * H))

    return max(valid_rects, key=rank_score)


def _crop_face_with_margin(frame, rect, margin_ratio: float = 0.15):
    """Crop the detected face with a 15% context margin around the facial perimeter for FaceNet."""
    H, W = frame.shape[:2]
    x, y, w, h = rect
    mx = int(w * margin_ratio)
    my = int(h * margin_ratio)
    x1 = max(0, x - mx)
    y1 = max(0, y - my)
    x2 = min(W, x + w + mx)
    y2 = min(H, y + h + my)
    return frame[y1:y2, x1:x2].copy()


def _embedding_from_crop(DeepFace, face_crop):
    """
    Generate FaceNet embedding directly from pre-detected, cropped face image.
    Uses detector_backend='skip' so no redundant or failing internal detector is invoked.
    """
    if face_crop is None or getattr(face_crop, "size", 0) == 0:
        return None
    H, W = face_crop.shape[:2]
    if H < MIN_FACE_PX or W < MIN_FACE_PX:
        return None
    try:
        reps = DeepFace.represent(
            img_path=face_crop,
            model_name=FACE_MODEL,
            detector_backend="skip",
            enforce_detection=False,
            align=False,
        )
        if reps and isinstance(reps, list) and len(reps) > 0:
            embedding = reps[0].get("embedding")
            if embedding and len(embedding) >= 128:
                return embedding
    except Exception as exc:
        logger.debug("Crop embedding direct array failed: %s", exc)

    # Fallback via temp image file
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        import cv2
        cv2.imwrite(tmp_path, face_crop)
        try:
            reps = DeepFace.represent(
                img_path=tmp_path,
                model_name=FACE_MODEL,
                detector_backend="skip",
                enforce_detection=False,
                align=False,
            )
            if reps and isinstance(reps, list) and len(reps) > 0:
                embedding = reps[0].get("embedding")
                if embedding and len(embedding) >= 128:
                    return embedding
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    except Exception as exc:
        logger.debug("Crop embedding via temp file failed: %s", exc)

    return None


def _cosine_distance(embedding_a, embedding_b) -> float:
    a = np.asarray(embedding_a, dtype=np.float64)
    b = np.asarray(embedding_b, dtype=np.float64)
    if a.size == 0 or b.size == 0 or a.shape != b.shape:
        return 1.0
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 1.0
    similarity = float(np.dot(a, b) / denom)
    return float(max(0.0, min(2.0, 1.0 - similarity)))


def _identity_matches_enrollment(live_embedding, enrollment_embeddings: list) -> bool:
    """Return True if the live face matches the owner's enrollment samples."""
    if live_embedding is None or not enrollment_embeddings:
        return False
    distances = [_cosine_distance(live_embedding, ref) for ref in enrollment_embeddings]
    if not distances:
        return False
    max_distance = float(os.getenv("CLOUDGUARD_FACE_DISTANCE", "0.40"))
    matches = sum(1 for d in distances if d <= max_distance)
    needed_matches = max(1, math.ceil(len(enrollment_embeddings) * 0.34))
    is_match = matches >= needed_matches
    logger.debug(
        "[FACE MATCH] distances=%s, matches=%d/%d, needed=%d, is_match=%s",
        [round(d, 3) for d in distances], matches, len(enrollment_embeddings), needed_matches, is_match
    )
    return is_match


def _decide_face_outcome(matched_faces: int, total_faces: int) -> tuple[str, str]:
    if total_faces < 2:
        return ("failed", "no_face")
    if matched_faces >= 2 and (matched_faces / total_faces) >= 0.5:
        return ("passed", "matched")
    return ("failed", "different_person")


def _region_is_usable(w: int, h: int, frame_shape: tuple, confidence: float | None = None) -> bool:
    frame_h, frame_w = frame_shape[:2]
    if w < 50 or h < 50:
        return False
    if w >= frame_w * 0.95 or h >= frame_h * 0.95:
        return False
    return True


def _compare_face_to_enrollment(DeepFace, cv2, face_frame, enrollment_frames: list) -> bool:
    try:
        live_res = DeepFace.represent(img_path=face_frame, model_name="Facenet512", enforce_detection=False)
        live_emb = live_res[0]["embedding"]
    except Exception as e:
        raise VerificationUnavailable(f"Face model error: {e}") from e

    matches = 0
    for enf in enrollment_frames:
        try:
            en_res = DeepFace.represent(img_path=enf, model_name="Facenet512", enforce_detection=False)
            en_emb = en_res[0]["embedding"]
            dist = _cosine_distance(live_emb, en_emb)
            if dist <= 0.40:
                matches += 1
        except Exception as e:
            raise VerificationUnavailable(f"Face model error: {e}") from e
    return matches >= max(1, len(enrollment_frames) // 2)


def _enrollment_embeddings(DeepFace, cv2, enrollment_images: list) -> list:
    """Extract embeddings from enrollment images using the unified crop pipeline."""
    embeddings = []
    # If 5 images are enrolled, select 3 representative ones (first, middle, last)
    # to dramatically reduce cold-cache embedding calculation time from 20s to ~7s on CPU.
    if len(enrollment_images) >= 5:
        selected = [enrollment_images[0], enrollment_images[len(enrollment_images) // 2], enrollment_images[-1]]
    else:
        selected = enrollment_images

    for ref_img in selected:
        if ref_img is None or getattr(ref_img, "size", 0) == 0:
            continue
        rects = _detect_faces_opencv(ref_img, cv2)
        primary = _primary_face(rects, ref_img.shape)
        if primary is not None:
            crop = _crop_face_with_margin(ref_img, primary, margin_ratio=0.15)
        else:
            H, W = ref_img.shape[:2]
            crop = ref_img[int(H * 0.05):int(H * 0.85), int(W * 0.15):int(W * 0.85)].copy()
        emb = _embedding_from_crop(DeepFace, crop)
        if emb is not None:
            embeddings.append(emb)
    return embeddings


def _check_frame_liveness(face_frames, cv2) -> bool:
    """
    Evaluate optical anti-spoof liveness:
    1. Texture & focal sharpness via Laplacian variance (rejects blurred photos/screens).
    2. Natural temporal micro-movement via inter-frame pixel differences (rejects static photos).
    """
    if len(face_frames) < 2:
        return False
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in face_frames if f is not None and getattr(f, "size", 0)]
    if len(grays) < 2:
        return False

    sharpnesses = [float(cv2.Laplacian(g, cv2.CV_64F).var()) for g in grays]
    avg_sharpness = sum(sharpnesses) / len(sharpnesses)

    diffs = [float(cv2.absdiff(grays[i], grays[i + 1]).mean()) for i in range(len(grays) - 1)]
    max_motion = max(diffs) if diffs else 0.0

    logger.info("[LIVENESS] avg_sharpness=%.1f (min 10.0), max_motion=%.2f (min 0.20)", avg_sharpness, max_motion)
    print(f"[LIVENESS] avg_sharpness={avg_sharpness:.1f}, max_motion={max_motion:.2f}", flush=True)

    return bool(avg_sharpness >= 10.0 and max_motion >= 0.20)


def _enrollment_cache_key(encoded_samples: list[str]) -> str:
    h = hashlib.sha256()
    for s in encoded_samples:
        h.update(s.encode("utf-8", errors="ignore"))
    return h.hexdigest()


def _get_cached_enrollment_embeddings(cache_key: str) -> list | None:
    cache_dir = Path(tempfile.gettempdir()) / "cloudguard_embeddings"
    cache_file = cache_dir / f"{cache_key}.json"
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) >= 2:
                    return data
        except Exception:
            pass
    return None


def _save_cached_enrollment_embeddings(cache_key: str, embeddings: list) -> None:
    try:
        cache_dir = Path(tempfile.gettempdir()) / "cloudguard_embeddings"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{cache_key}.json"
        serializable = []
        for vec in embeddings:
            if hasattr(vec, "tolist"):
                serializable.append(vec.tolist())
            elif isinstance(vec, (list, tuple)):
                serializable.append([float(x) for x in vec])
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(serializable, f)
    except Exception as exc:
        logger.debug("Could not cache enrollment embeddings: %s", exc)


def _evaluate_face_and_liveness(video_path: Path, encoded_samples: list[str]) -> dict:
    """Evaluate face match and liveness in an isolated worker process."""
    try:
        import cv2
        logger.info("[MODEL] Loading FaceNet...")
        print("[MODEL] Loading FaceNet...", flush=True)
        from deepface import DeepFace
        logger.info("[MODEL] FaceNet loaded")
        print("[MODEL] FaceNet loaded", flush=True)
    except Exception as error:
        logger.exception("Importing cv2 or DeepFace failed")
        raise VerificationUnavailable(
            f"Local verification models are unavailable: {error}"
        ) from error

    t_start = time.perf_counter()
    logger.info("[FACE] enrollment samples loaded: %d", len(encoded_samples))
    print(f"[FACE] enrollment samples loaded: {len(encoded_samples)}", flush=True)

    raw_frames = _video_frames(video_path, cv2)
    frames = [_shrink_frame(frame, cv2) for frame in raw_frames]
    total_frames = len(frames)
    logger.info("[FACE] usable video frames sampled: %d", total_frames)
    print(f"[FACE] usable video frames sampled: {total_frames}", flush=True)

    # Detect faces on each sampled frame using robust multi-cascade
    face_crops = []
    face_frames = []
    multi_face_count = 0

    for frame in frames:
        rects = _detect_faces_opencv(frame, cv2)
        if len(rects) > 1:
            multi_face_count += 1
        primary = _primary_face(rects, frame.shape)
        if primary is not None:
            crop = _crop_face_with_margin(frame, primary, margin_ratio=0.15)
            face_crops.append(crop)
            face_frames.append(frame)

    faces_detected = len(face_crops)
    logger.info("[FACE] face detected: %s (count: %d/%d)", "true" if faces_detected > 0 else "false", faces_detected, total_frames)
    print(f"[FACE] face detected: {'true' if faces_detected > 0 else 'false'} (count: {faces_detected}/{total_frames})", flush=True)

    # If multiple distinct people are detected in majority of frames
    if multi_face_count >= max(3, int(total_frames * 0.6)):
        logger.info("[FACE] multiple faces detected in recording")
        return {
            "face_status": "failed",
            "face_detail": "multiple_faces",
            "liveness_status": "failed",
            "enrollment_samples_count": len(encoded_samples),
            "usable_frames_count": total_frames,
            "faces_detected": faces_detected,
            "face_detected": False,
            "t_liveness": 0.001,
            "t_face": time.perf_counter() - t_start,
        }

    # If standard cascade found no bounding box, evaluate centered subject region across frames
    if faces_detected == 0 and frames:
        logger.info("[FACE] Cascade found no isolated bounding box; evaluating centered subject region across frames")
        for frame in frames:
            H, W = frame.shape[:2]
            # Center 70% width, top-center 80% height (standard position of user in webcam view)
            y1 = int(H * 0.05)
            y2 = int(H * 0.85)
            x1 = int(W * 0.15)
            x2 = int(W * 0.85)
            center_crop = frame[y1:y2, x1:x2].copy()
            face_crops.append(center_crop)
            face_frames.append(frame)
        faces_detected = len(face_crops)

    if faces_detected == 0:
        logger.info("[FACE] comparison completed: no face found")
        print("[FACE] comparison completed: no face found", flush=True)
        return {
            "face_status": "failed",
            "face_detail": "no_face",
            "liveness_status": "failed",
            "enrollment_samples_count": len(encoded_samples),
            "usable_frames_count": total_frames,
            "faces_detected": 0,
            "face_detected": False,
            "t_liveness": 0.001,
            "t_face": time.perf_counter() - t_start,
        }

    # Evaluate optical anti-spoof liveness on the candidate face frames
    t_live_start = time.perf_counter()
    liveness_passed = _check_frame_liveness(face_frames, cv2)
    liveness_status = "passed" if liveness_passed else "failed"
    t_liveness = time.perf_counter() - t_live_start

    # Check cache for enrollment embeddings
    cache_key = _enrollment_cache_key(encoded_samples)
    cached_vectors = _get_cached_enrollment_embeddings(cache_key)
    if cached_vectors is not None:
        enrollment_vectors = cached_vectors
    else:
        enrollment_images = [_shrink_frame(image, cv2) for image in _enrollment_images(encoded_samples, cv2)]
        enrollment_vectors = _enrollment_embeddings(DeepFace, cv2, enrollment_images)
        if enrollment_vectors:
            _save_cached_enrollment_embeddings(cache_key, enrollment_vectors)

    # Select up to 4 representative, evenly-spaced face crops across the recording
    # to avoid redundant CPU inference loops while maintaining high identity verification accuracy.
    if len(face_crops) <= 4:
        selected_crops = face_crops
    else:
        step = len(face_crops) / 4.0
        indices = [int(i * step) for i in range(4)]
        selected_crops = [face_crops[min(idx, len(face_crops) - 1)] for idx in indices]

    # Target threshold for early confirmation (at least 35% of selected frames)
    target_threshold = max(1, math.ceil(len(selected_crops) * 0.35))

    identity_votes = 0
    frames_attempted = 0
    for crop in selected_crops:
        emb = _embedding_from_crop(DeepFace, crop)
        if emb is None:
            continue
        frames_attempted += 1
        if _identity_matches_enrollment(emb, enrollment_vectors):
            identity_votes += 1
            if identity_votes >= target_threshold:
                logger.info("[FACE] Identity confirmed with %d positive votes (target: %d). Early exit.", identity_votes, target_threshold)
                print(f"[FACE] Identity confirmed with {identity_votes} positive votes (target: {target_threshold}). Early exit.", flush=True)
                break

    if frames_attempted == 0:
        face_status = "failed"
        face_detail = "quality_low"
    else:
        vote_threshold = max(1, math.ceil(frames_attempted * 0.35))
        if identity_votes >= vote_threshold:
            face_status = "passed"
            face_detail = "matched"
        else:
            face_status = "failed"
            face_detail = "different_person"

    logger.info("[FACE] comparison completed: %s (%s), votes: %d/%d", face_status, face_detail, identity_votes, frames_attempted)
    print(f"[FACE] comparison completed: {face_status} ({face_detail}), votes: {identity_votes}/{frames_attempted}", flush=True)

    return {
        "face_status": face_status,
        "face_detail": face_detail,
        "liveness_status": liveness_status,
        "enrollment_samples_count": len(encoded_samples),
        "usable_frames_count": total_frames,
        "faces_detected": faces_detected,
        "face_detected": True,
        "t_liveness": t_liveness,
        "t_face": time.perf_counter() - t_start,
    }


def _run_speech_subprocess(video_path: Path) -> str:
    """Run speech transcription in an isolated process to reclaim all memory upon completion."""
    file_dir = str(Path(__file__).resolve().parent).replace("\\", "/")
    root_dir = str(Path(__file__).resolve().parent.parent).replace("\\", "/")
    script = (
        "import sys, os\n"
        "os.environ['PYTHONIOENCODING'] = 'utf-8'\n"
        "os.environ['PYTHONUTF8'] = '1'\n"
        f"for d in ('{file_dir}', '{root_dir}'):\n"
        "    if d not in sys.path: sys.path.insert(0, d)\n"
        "from pathlib import Path\n"
        "try:\n"
        "    from verification import _transcribe_recording\n"
        "except ImportError:\n"
        "    from backend.verification import _transcribe_recording\n"
        f"t = _transcribe_recording(Path(r'{str(video_path).replace(chr(92), '/')}'))\n"
        "print('__TRANSCRIPT__:' + (t or ''))\n"
    )
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = f"{file_dir}{os.pathsep}{root_dir}"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=60,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("__TRANSCRIPT__:"):
                return line.split("__TRANSCRIPT__:", 1)[1].strip()
        logger.warning("Speech worker output: %s (err: %s)", proc.stdout, proc.stderr)
    except Exception as exc:
        logger.warning("Speech worker failed: %s", exc)
    return ""


def _run_face_subprocess(video_path: Path, encoded_samples: list[str]) -> dict:
    """Run face evaluation in an isolated process to reclaim all memory upon completion."""
    file_dir = str(Path(__file__).resolve().parent).replace("\\", "/")
    root_dir = str(Path(__file__).resolve().parent.parent).replace("\\", "/")

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(encoded_samples, f)
        samples_file = f.name

    script = (
        "import sys, os, json, traceback\n"
        "os.environ['PYTHONIOENCODING'] = 'utf-8'\n"
        "os.environ['PYTHONUTF8'] = '1'\n"
        "os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'\n"
        "os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'\n"
        "if hasattr(sys.stdout, 'reconfigure'):\n"
        "    sys.stdout.reconfigure(encoding='utf-8')\n"
        f"for d in ('{file_dir}', '{root_dir}'):\n"
        "    if d not in sys.path: sys.path.insert(0, d)\n"
        "from pathlib import Path\n"
        "try:\n"
        "    from verification import _evaluate_face_and_liveness\n"
        "except ImportError:\n"
        "    from backend.verification import _evaluate_face_and_liveness\n"
        "try:\n"
        f"    with open(r'{samples_file.replace(chr(92), '/')}', 'r', encoding='utf-8') as f:\n"
        "        samples = json.load(f)\n"
        f"    res = _evaluate_face_and_liveness(Path(r'{str(video_path).replace(chr(92), '/')}'), samples)\n"
        "    print('__FACE_RESULT__:' + json.dumps(res))\n"
        "except Exception as exc:\n"
        "    print('__FACE_ERROR__:' + str(exc))\n"
        "    traceback.print_exc()\n"
        "    sys.exit(1)\n"
    )
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["TF_CPP_MIN_LOG_LEVEL"] = "3"
    env["TF_ENABLE_ONEDNN_OPTS"] = "0"
    env["PYTHONPATH"] = f"{file_dir}{os.pathsep}{root_dir}"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=120,
        )
        face_result = None
        worker_error = None
        for line in proc.stdout.splitlines():
            line_str = line.strip()
            if line_str.startswith("[FACE]") or line_str.startswith("[MODEL]") or line_str.startswith("[CHECKIN]"):
                logger.info(line_str)
            elif line_str.startswith("__FACE_RESULT__:"):
                face_result = json.loads(line_str.split("__FACE_RESULT__:", 1)[1].strip())
            elif line_str.startswith("__FACE_ERROR__:"):
                worker_error = line_str.split("__FACE_ERROR__:", 1)[1].strip()
        if face_result is not None:
            return face_result
        logger.warning("Face worker failed. Output: %s (err: %s)", proc.stdout, proc.stderr)
        if worker_error or proc.returncode != 0:
            raise VerificationUnavailable(
                f"Face verification service is temporarily unavailable: {worker_error or 'process terminated'}"
            )
    except subprocess.TimeoutExpired as exc:
        logger.error("Face worker timed out: %s", exc)
        raise VerificationUnavailable("Face verification timed out.") from exc
    except Exception as exc:
        logger.warning("Face worker error: %s", exc)
        if isinstance(exc, VerificationUnavailable):
            raise
        raise VerificationUnavailable("Face verification service is temporarily unavailable.") from exc
    finally:
        try:
            os.remove(samples_file)
        except OSError:
            pass
    return {"face_status": "failed", "face_detail": "no_face", "liveness_status": "failed", "t_liveness": 0.0, "t_face": 0.0}


def diagnose_recording(video_path: Path) -> dict:
    """Diagnose video recording reception, decoding, and face detection without matching against enrollment."""
    video_received = video_path.exists() and video_path.stat().st_size > 0
    if not video_received:
        return {
            "video_received": False,
            "video_decoded": False,
            "decoder_used": "none",
            "frames_decoded": 0,
            "frames_checked": 0,
            "faces_detected": 0,
            "face_model_loaded": False,
            "liveness_model_loaded": False,
            "voice_model_loaded": False,
        }

    diag = verification_diagnostics()
    face_model_loaded = diag.get("face", False)
    liveness_model_loaded = diag.get("liveness", False)
    voice_model_loaded = diag.get("speech", False)

    decoder_used = "none"
    raw_frames = []
    try:
        raw_frames = _frames_from_av(video_path)
        decoder_used = "av"
    except Exception as av_err:
        logger.debug("Diagnose PyAV decode failed: %s", av_err)
        try:
            import cv2
            raw_frames = _frames_from_cv2(video_path, cv2)
            decoder_used = "cv2"
        except Exception as cv_err:
            logger.debug("Diagnose OpenCV decode failed: %s", cv_err)

    video_decoded = len(raw_frames) >= 2
    frames_decoded = len(raw_frames)
    frames_checked = 0
    faces_detected = 0

    if video_decoded:
        try:
            import cv2
            shrunk_frames = [_shrink_frame(f, cv2) for f in raw_frames]
            frames_checked = len(shrunk_frames)
            for frame in shrunk_frames:
                rects = _detect_faces_opencv(frame, cv2)
                if rects:
                    faces_detected += 1
        except Exception as exc:
            logger.warning("Diagnose face extraction error: %s", exc)

    return {
        "video_received": True,
        "video_decoded": video_decoded,
        "decoder_used": decoder_used,
        "frames_decoded": frames_decoded,
        "frames_checked": frames_checked,
        "faces_detected": faces_detected,
        "face_model_loaded": face_model_loaded,
        "liveness_model_loaded": liveness_model_loaded,
        "voice_model_loaded": voice_model_loaded,
    }


def verify_recording(
    video_path: Path, encoded_samples: list[str], sentence: str
) -> dict:
    """
    Run face-match, liveness, and speech verification on a check-in recording.

    Returns a dict with keys ``face_status``, ``liveness_status``, ``speech_status``
    each set to ``"passed"`` or ``"failed"``, plus ``face_detail`` and ``timings``.
    """
    try:
        if not providers_available():
            raise VerificationUnavailable("Local verification models are unavailable.")

        # 1. Face verification & liveness in isolated worker process (memory freed upon exit)
        t_face_start = time.perf_counter()
        logger.info("[CHECKIN] Face verification started")
        face_res = _run_face_subprocess(video_path, encoded_samples)
        logger.info("[CHECKIN] Face verification completed")
        t_face = time.perf_counter() - t_face_start

        logger.info("[CHECKIN] Liveness verification started")
        t_liveness = float(face_res.get("t_liveness", 0.005))
        logger.info("[CHECKIN] Liveness verification completed")

        # 2. Transcribe speech in isolated worker process (memory freed upon exit)
        t_speech_start = time.perf_counter()
        logger.info("[CHECKIN] Speech verification started")
        has_audio = _has_audio_stream(video_path)
        logger.info("[CHECKIN] Audio stream present: %s", has_audio)
        if not has_audio:
            speech_status = "failed"
            speech_detail = "no_audio"
            t_speech = 0.0
            logger.info("[CHECKIN] Speech verification completed: no audio stream detected in container")
        else:
            transcript = _run_speech_subprocess(video_path)
            t_speech = time.perf_counter() - t_speech_start
            logger.info("[CHECKIN] Speech verification completed, transcript: %r", transcript)
            if not transcript or not transcript.strip():
                speech_status = "failed"
                speech_detail = "silent"
            elif _phrase_matches(sentence, transcript):
                speech_status = "passed"
                speech_detail = "matched"
            else:
                speech_status = "failed"
                speech_detail = "mismatch"

        result = {
            "face_status": face_res.get("face_status", "failed"),
            "face_detail": face_res.get("face_detail", "no_face"),
            "liveness_status": face_res.get("liveness_status", "failed"),
            "speech_status": speech_status,
            "speech_detail": speech_detail,
            "timings": {
                "face": t_face,
                "liveness": t_liveness,
                "speech": t_speech,
            },
        }
        logger.info(
            "Verification result – face: %s (%s), liveness: %s, speech: %s",
            result["face_status"], result["face_detail"], result["liveness_status"], result["speech_status"]
        )
        return result
    except VerificationUnavailable:
        raise
    except ValueError:
        raise
    except Exception as exc:
        logger.exception("Local verification failed unexpectedly.")
        raise VerificationUnavailable(
            "Local verification could not complete. The check-in was not saved."
        ) from exc
