class DualMakerError(RuntimeError):
    """Base class for expected dualmaker failures."""


class ConfigurationError(DualMakerError):
    """Startup configuration is missing, malformed, or unsafe."""


class UserCancelledError(DualMakerError):
    """The user cancelled an interactive workflow without making changes."""


class DependencyError(DualMakerError):
    """A required executable is unavailable or unsuitable."""


class MetadataError(DualMakerError):
    """A media file cannot be inspected safely."""


class PairingError(DualMakerError):
    """The supplied files do not form a valid pair."""


class AmbiguousPairError(PairingError):
    """More than one plausible pairing or track choice remains."""


class ExperimentalFPSRequiredError(PairingError):
    """A different-FPS candidate needs explicit beta approval or policy support."""


class ExperimentalTVRipRequiredError(PairingError):
    """An editorially different TVRip needs explicit segmented-sync approval."""


class ProcessingError(DualMakerError):
    """Synchronization or muxing failed."""


class TVRipValidationError(ProcessingError):
    """A TVRip segment map failed its configured safety policy."""

    def __init__(self, message: str, report: object) -> None:
        super().__init__(message)
        self.report = report


class OutputConflictError(DualMakerError):
    """An output path already exists and policy forbids replacing it."""
