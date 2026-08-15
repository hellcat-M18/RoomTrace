class RoomTraceError(Exception):
    """Expected, user-actionable RoomTrace failure."""


class CaptureFormatError(RoomTraceError):
    """The capture package is malformed or incompatible."""


class ProcessingError(RoomTraceError):
    """The capture is valid but cannot produce the requested output."""

