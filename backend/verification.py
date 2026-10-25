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
# OpenCV Haar cascade: ultra-fast on CPU (0MB extra RAM, ~10ms execution)
FACE_DETECTORS = ("opencv",)
# DeepFace's Facenet cosine default is 0.40. Keep this tighter so a different
# person cannot pass live check-in against the owner's enrollment photos.
FACE_MATCH_MAX_DISTANCE = float(os.getenv("CLOUDGUARD_FACE_DISTANCE", "0.24"))
MIN_FACE_PX = 40
MAX_FACE_FRAME_COVERAGE = 0.82
_FRAME_SAMPLE_FRACTIONS = (0.2, 0.45, 0.7, 0.9)
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

    frames = []
    with av.open(str(video_path)) as container:
        if not container.streams.video:
            raise ValueError("The recording could not be opened. Please record again.")
        stream = container.streams.video[0]
        duration = 0.0
        if container.duration is not None:
            duration = float(container.duration) / av.time_base
        elif stream.duration is not None and stream.time_base is not None:
            duration = float(stream.duration * stream.time_base)
        _reject_duration(duration)
        if duration > 0:
            next_index = 0
            targets = [duration * fraction for fraction in _FRAME_SAMPLE_FRACTIONS]
            for frame in container.decode(video=0):
                timestamp = frame.time
                if timestamp is None and frame.pts is not None and stream.time_base is not None:
                    timestamp = float(frame.pts * stream.time_base)
                if timestamp is None or timestamp + 0.08 < targets[next_index]:
                    continue
                array = _array_from_av_frame(frame)
                if array is not None:
                    frames.append(array)
                    next_index += 1
                    if next_index >= len(targets):
                        break
        else:
            kept = []
            for index, frame in enumerate(container.decode(video=0)):
                if index % 10 == 0:
                    array = _array_from_av_frame(frame)
                    if array is not None:
                        kept.append(array)
                if index > 280:
                    break
            frames = [kept[index] for index in _sampled_indexes(len(kept))] if len(kept) >= 2 else kept
    if len(frames) < 2:
        raise ValueError("The recording could not be decoded reliably. Please record again.")
    return frames


def _frames_from_cv2(video_path: Path, cv2) -> list:
    capture = cv2.VideoCapture(str(video_path))
    try:
        if not capture.isOpened():
            raise ValueError("The recording could not be opened. Please record again.")

        actual_frame_count = 0
        while True:
            if not capture.grab():
                break
            actual_frame_count += 1

        if actual_frame_count < 2:
            raise ValueError("The recording could not be decoded reliably. Please record again.")

        capture.release()
        capture = cv2.VideoCapture(str(video_path))

        frame_count_metadata = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = capture.get(cv2.CAP_PROP_FPS)
        duration = frame_count_metadata / fps if fps and fps > 0 else 0
        _reject_duration(duration)

        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if width < 1 or height < 1 or width > 3840 or height > 2160:
            raise ValueError("The recording dimensions are not supported. Please record again.")

        target_frames = _sampled_indexes(actual_frame_count)
        frames = []
        current_frame = 0
        target_idx = 0

        while target_idx < len(target_frames):
            success, frame = capture.read()
            if not success:
                break
            while target_idx < len(target_frames) and current_frame == target_frames[target_idx]:
                if frame is not None and frame.size:
                    frames.append(frame.copy())
                target_idx += 1
            current_frame += 1

        if len(frames) < 2:
            raise ValueError("The recording could not be decoded reliably. Please record again.")
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
            raw = base64.b64decode(sample, validate=True)
        except (ValueError, TypeError) as error:
            raise VerificationUnavailable(
                "Face enrollment data is invalid. Please contact support."
            ) from error
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

    return WhisperModel(
        _whisper_model_name(),
        device="cpu",
        compute_type="int8",
    )


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


def _region_is_usable(width: int, height: int, image_shape, confidence=None) -> bool:
    if width < MIN_FACE_PX or height < MIN_FACE_PX:
        return False
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = None
        if confidence is not None and 0 <= confidence < 0.45:
            return False
    if image_shape is not None and len(image_shape) >= 2:
        frame_height, frame_width = int(image_shape[0]), int(image_shape[1])
        if frame_width > 0 and frame_height > 0:
            coverage = (width * height) / float(frame_width * frame_height)
            if coverage > MAX_FACE_FRAME_COVERAGE:
                return False
    return True


def _region_is_usable_from_item(item: dict, image_shape) -> bool:
    area = item.get("facial_area") or item.get("region")
    if not area:
        return True
    width = int(area.get("w") or 0)
    height = int(area.get("h") or 0)
    confidence = item.get("face_confidence", item.get("confidence"))
    return _region_is_usable(width, height, image_shape, confidence)


def _usable_detected_face(detected, frame):
    if not detected or len(detected) != 1:
        return None
    face = detected[0]
    if not _region_is_usable_from_item(face, getattr(frame, "shape", None)):
        return None
    return face


def _safe_extract_faces(DeepFace, frame, anti_spoofing: bool):
    """Call DeepFace.extract_faces and swallow non-critical errors."""
    image_shape = getattr(frame, "shape", None)
    if image_shape is None or len(image_shape) < 2 or image_shape[0] < MIN_FACE_PX or image_shape[1] < MIN_FACE_PX:
        return []
    for detector in FACE_DETECTORS:
        try:
            detected = DeepFace.extract_faces(
                img_path=frame,
                detector_backend=detector,
                enforce_detection=False,
                anti_spoofing=anti_spoofing,
            )
        except Exception as exc:
            logger.debug("extract_faces failed (%s): %s", detector, exc)
            continue
        usable = _usable_detected_face(detected, frame)
        if usable is not None:
            return [usable]
        if not detected and not anti_spoofing:
            break
    return []


def _save_frame_as_temp_image(frame, cv2) -> str:
    """Save an OpenCV frame to a temporary file and return the path."""
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    if not cv2.imwrite(path, frame):
        os.remove(path)
        raise VerificationUnavailable("A face image could not be prepared for matching. Please try again.")
    return path


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
    """Return True only if the live face matches a majority of enrollment samples."""
    if live_embedding is None or len(enrollment_embeddings) < 2:
        return False
    distances = [_cosine_distance(live_embedding, reference) for reference in enrollment_embeddings]
    matches = sum(1 for distance in distances if distance <= FACE_MATCH_MAX_DISTANCE)
    if len(enrollment_embeddings) >= 5:
        required = max(3, math.ceil(len(enrollment_embeddings) * 0.6))
    else:
        required = max(2, (len(enrollment_embeddings) + 1) // 2)
    median = float(np.median(np.asarray(distances, dtype=np.float64)))
    best = float(min(distances))
    logger.debug(
        "Face distances vs enrollment: %s (matches %d/%d, need %d, median %.3f, best %.3f, max %.2f)",
        [round(distance, 3) for distance in distances],
        matches,
        len(enrollment_embeddings),
        required,
        median,
        best,
        FACE_MATCH_MAX_DISTANCE,
    )
    return (
        matches >= required
        and median <= FACE_MATCH_MAX_DISTANCE
        and best <= FACE_MATCH_MAX_DISTANCE
    )


def _decide_face_outcome(identity_votes: int, frames_attempted: int) -> tuple[str, str]:
    """Return (face_status, face_detail) for live-vs-enrollment votes."""
    if frames_attempted < 2:
        return "failed", "no_face"
    identity_threshold = max(2, math.ceil(frames_attempted * 0.6))
    if identity_votes >= identity_threshold:
        return "passed", "matched"
    return "failed", "different_person"


def _embedding_from_array(DeepFace, image):
    """Return a single-face embedding, or None if no unambiguous face is found."""
    image_shape = getattr(image, "shape", None)
    if image_shape is None or len(image_shape) < 2 or image_shape[0] < MIN_FACE_PX or image_shape[1] < MIN_FACE_PX:
        return None
    last_error = None
    for detector in FACE_DETECTORS:
        try:
            representations = DeepFace.represent(
                img_path=image,
                model_name=FACE_MODEL,
                detector_backend=detector,
                enforce_detection=True,
                align=True,
            )
        except Exception as exc:
            last_error = exc
            logger.debug("Face embedding failed with %s: %s", detector, exc)
            continue
        if not representations or len(representations) != 1:
            continue
        item = representations[0]
        if not _region_is_usable_from_item(item, image_shape):
            continue
        embedding = item.get("embedding")
        if embedding:
            return embedding
        if not representations:
            break
    if last_error:
        logger.debug("No usable face embedding: %s", last_error)
    return None


def _embedding_from_frame(DeepFace, cv2, frame):
    shrunk = _shrink_frame(frame, cv2)
    embedding = _embedding_from_array(DeepFace, shrunk)
    if embedding is not None:
        return embedding
    path = _save_frame_as_temp_image(shrunk, cv2)
    try:
        return _embedding_from_array(DeepFace, path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _enrollment_embeddings(DeepFace, cv2, enrollment_images: list) -> list:
    embeddings = []
    for reference in enrollment_images:
        embedding = _embedding_from_frame(DeepFace, cv2, reference)
        if embedding is not None:
            embeddings.append(embedding)
    return embeddings


def _compare_face_to_enrollment(DeepFace, cv2, live_frame, enrollment_images: list) -> bool:
    """
    Return True if the live face matches a majority of enrollment images.
    Faces must be detected; whole-frame / background comparisons are not used.
    """
    live_embedding = _embedding_from_frame(DeepFace, cv2, live_frame)
    if live_embedding is None:
        return False
    return _identity_matches_enrollment(
        live_embedding, _enrollment_embeddings(DeepFace, cv2, enrollment_images)
    )


def _check_frame_liveness(frames, cv2) -> bool:
    """
    Evaluate optical anti-spoof liveness:
    1. Texture & focal sharpness via Laplacian variance (rejects blurred photos/screens).
    2. Natural temporal micro-movement via inter-frame pixel differences (rejects static photos).
    """
    if len(frames) < 2:
        return False
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames if f is not None and getattr(f, "size", 0)]
    if len(grays) < 2:
        return False

    sharpnesses = [float(cv2.Laplacian(g, cv2.CV_64F).var()) for g in grays]
    avg_sharpness = sum(sharpnesses) / len(sharpnesses)

    diffs = [float(cv2.absdiff(grays[i], grays[i + 1]).mean()) for i in range(len(grays) - 1)]
    max_motion = max(diffs) if diffs else 0.0

    return bool(avg_sharpness >= 35.0 and max_motion >= 0.8)


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
        from deepface import DeepFace
    except Exception as error:
        logger.exception("Importing cv2 or DeepFace failed")
        raise VerificationUnavailable(
            f"Local verification models are unavailable: {error}"
        ) from error

    t_start = time.perf_counter()
    logger.info("[FACE] enrollment samples loaded: %d", len(encoded_samples))
    print(f"[FACE] enrollment samples loaded: {len(encoded_samples)}", flush=True)

    raw_frames = _video_frames(video_path, cv2)
    logger.info("[FACE] usable video frames detected: %d", len(raw_frames))
    print(f"[FACE] usable video frames detected: {len(raw_frames)}", flush=True)

    frames = [_shrink_frame(frame, cv2) for frame in raw_frames]

    # Fast face extraction using lightweight OpenCV Haar cascade
    live_frames: list = []
    for frame in frames:
        detected = _safe_extract_faces(DeepFace, frame, anti_spoofing=False)
        if detected and len(detected) == 1:
            live_frames.append(frame)

    face_detected = len(live_frames) > 0
    logger.info("[FACE] face detected: %s", "true" if face_detected else "false")
    print(f"[FACE] face detected: {'true' if face_detected else 'false'}", flush=True)

    # If no face is found in the check-in recording, reject immediately without loading embeddings
    if not live_frames:
        logger.info("[FACE] comparison completed")
        print("[FACE] comparison completed", flush=True)
        return {
            "face_status": "failed",
            "face_detail": "no_face",
            "liveness_status": "failed",
            "enrollment_samples_count": len(encoded_samples),
            "usable_frames_count": len(raw_frames),
            "face_detected": False,
            "t_liveness": 0.001,
            "t_face": time.perf_counter() - t_start,
        }

    # Evaluate optical anti-spoof liveness on the candidate frames
    t_live_start = time.perf_counter()
    liveness_passed = _check_frame_liveness(live_frames, cv2)
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

    identity_votes = 0
    frames_attempted = 0
    for frame in live_frames:
        try:
            live_embedding = _embedding_from_frame(DeepFace, cv2, frame)
        except Exception as exc:
            logger.debug("Live face embedding failed for a frame: %s", exc)
            continue
        if live_embedding is None:
            continue
        frames_attempted += 1
        if _identity_matches_enrollment(live_embedding, enrollment_vectors):
            identity_votes += 1

    face_status, face_detail = _decide_face_outcome(identity_votes, frames_attempted)
    logger.info("[FACE] comparison completed")
    print("[FACE] comparison completed", flush=True)

    return {
        "face_status": face_status,
        "face_detail": face_detail,
        "liveness_status": liveness_status,
        "enrollment_samples_count": len(encoded_samples),
        "usable_frames_count": len(raw_frames),
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
            timeout=25,
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
        "import sys, os, json\n"
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
        f"with open(r'{samples_file.replace(chr(92), '/')}', 'r', encoding='utf-8') as f:\n"
        "    samples = json.load(f)\n"
        f"res = _evaluate_face_and_liveness(Path(r'{str(video_path).replace(chr(92), '/')}'), samples)\n"
        "print('__FACE_RESULT__:' + json.dumps(res))\n"
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
            timeout=45,
        )
        face_result = None
        for line in proc.stdout.splitlines():
            line_str = line.strip()
            if line_str.startswith("[FACE]"):
                logger.info(line_str)
            elif line_str.startswith("__FACE_RESULT__:"):
                face_result = json.loads(line_str.split("__FACE_RESULT__:", 1)[1].strip())
        if face_result is not None:
            return face_result
        logger.warning("Face worker output: %s (err: %s)", proc.stdout, proc.stderr)
    except Exception as exc:
        logger.warning("Face worker error: %s", exc)
    finally:
        try:
            os.remove(samples_file)
        except OSError:
            pass
    return {"face_status": "failed", "face_detail": "no_face", "liveness_status": "failed", "t_liveness": 0.0, "t_face": 0.0}


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
        logger.info("[CHECKIN] face verification started")
        face_res = _run_face_subprocess(video_path, encoded_samples)
        logger.info("[CHECKIN] face verification completed")
        t_face = time.perf_counter() - t_face_start

        logger.info("[CHECKIN] liveness verification started")
        t_liveness = float(face_res.get("t_liveness", 0.005))
        logger.info("[CHECKIN] liveness verification completed")

        # 2. Transcribe speech in isolated worker process (memory freed upon exit)
        t_speech_start = time.perf_counter()
        logger.info("[CHECKIN] speech verification started")
        transcript = _run_speech_subprocess(video_path)
        speech_status = "passed" if _phrase_matches(sentence, transcript) else "failed"
        logger.info("[CHECKIN] speech verification completed")
        t_speech = time.perf_counter() - t_speech_start

        result = {
            "face_status": face_res.get("face_status", "failed"),
            "face_detail": face_res.get("face_detail", "no_face"),
            "liveness_status": face_res.get("liveness_status", "failed"),
            "speech_status": speech_status,
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
