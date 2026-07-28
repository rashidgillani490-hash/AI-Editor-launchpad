"""Encoding detection and global constants."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Default text encoding used for all read/write operations. Centralised
# here so it can be changed in one place if NexCore ever needs to support
# alternate encodings (e.g. reading legacy latin-1 source files).
DEFAULT_ENCODING = "utf-8"

# Threshold (in bytes) at which to treat a file as "large" and stream it.
LARGE_FILE_THRESHOLD = 5 * 1024 * 1024  # 5 MB default; configurable by callers if needed

# Size of read chunks when streaming (binary read then incremental decode).
_READ_CHUNK_SIZE = 64 * 1024  # 64 KB

# Optional encoding detectors (try preferred ones first). If not installed,
# we'll fall back to latin-1 for a best-effort decode.
_charset_detector = None
try:
    # Preferred: charset-normalizer (PyPI: charset-normalizer)
    from charset_normalizer import from_bytes as _cn_from_bytes  # type: ignore

    def _detect_encoding_with_charset_normalizer(sample: bytes) -> Optional[str]:
        try:
            results = _cn_from_bytes(sample)
            if results:
                best = results.best()
                if best and best.encoding:
                    return best.encoding
        except Exception:
            logger.debug("charset_normalizer detection failed", exc_info=True)
        return None

    _charset_detector = _detect_encoding_with_charset_normalizer
    logger.debug("Using charset_normalizer for encoding detection")
except Exception:
    try:
        # Fallback: chardet
        import chardet  # type: ignore

        def _detect_encoding_with_chardet(sample: bytes) -> Optional[str]:
            try:
                info = chardet.detect(sample)
                encoding = info.get("encoding")
                return encoding
            except Exception:
                logger.debug("chardet detection failed", exc_info=True)
                return None

        _charset_detector = _detect_encoding_with_chardet
        logger.debug("Using chardet for encoding detection")
    except Exception:
        logger.debug("No optional encoding detectors available; will fall back to latin-1 when necessary")


def _detect_encoding(sample: bytes, preferred: Optional[str] = None) -> Optional[str]:
    """Detect encoding for a byte sample.

    If a preferred encoding was supplied, we return it first (caller uses
    that before invoking this). Otherwise, use available detectors. May
    return None if detection fails.
    """
    if preferred:
        return preferred
    if _charset_detector:
        try:
            detected = _charset_detector(sample)
            if detected:
                logger.debug("Detected encoding %r from sample", detected)
                return detected
        except Exception:
            logger.debug("Encoding detector raised", exc_info=True)
    return None