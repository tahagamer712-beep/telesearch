"""Application error classes."""


class AppError(Exception):
    """Base class for all application errors."""


class ConfigError(AppError):
    """Invalid or missing configuration."""


class StorageError(AppError):
    """Filesystem / storage problem."""


class UnsafePathError(StorageError):
    """A path tried to escape its allowed root."""


class DatabaseError(AppError):
    """Database problem."""


class MigrationError(DatabaseError):
    """Schema migration problem."""


class IngestError(AppError):
    """File intake problem."""


class ArchiveError(IngestError):
    """Archive extraction problem."""


class InsufficientSpaceError(ArchiveError):
    def __init__(self, required: int, available: int) -> None:
        super().__init__(f"مساحة غير كافية: المطلوب {required} بايت، المتاح {available} بايت")
        self.required, self.available = required, available


class ToolUnavailableError(ArchiveError):
    """Optional external tool (7z, unrar, pdftotext) not installed."""


class ParserError(IngestError):
    """Parser failure."""


class ParserUnavailableError(ParserError):
    """Parser needs an optional tool/library that is missing."""


class JobInterrupted(AppError):
    """Raised inside a job handler to stop it cleanly (pause/cancel/shutdown)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidQuery(AppError):
    """Search query failed validation."""


class SearchTimeout(AppError):
    """Search exceeded the configured timeout."""


class PermissionDenied(AppError):
    """User lacks the required permission."""


class BackupError(AppError):
    """Backup / restore problem."""
