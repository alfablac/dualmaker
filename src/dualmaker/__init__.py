"""Build synchronized dual-audio Matroska releases."""

from .api import make_dual, plan_pair, scan_directory
from .configuration import (
    default_user_config_path,
    initialize_config_file,
    load_configuration,
    validate_configuration,
)
from .models import (
    Attachment,
    AudioTrackSelection,
    ContentIdentity,
    DualMakerConfig,
    DubGapFallback,
    FPSDecision,
    FrameRate,
    JobPlan,
    JobResult,
    MediaAsset,
    PairCandidate,
    Track,
    TVRipInterval,
    TVRipSegment,
    TVRipSyncReport,
    TVRipValidationPoint,
)

__all__ = [
    "Attachment",
    "AudioTrackSelection",
    "ContentIdentity",
    "DualMakerConfig",
    "DubGapFallback",
    "FPSDecision",
    "FrameRate",
    "JobPlan",
    "JobResult",
    "MediaAsset",
    "PairCandidate",
    "TVRipInterval",
    "TVRipSegment",
    "TVRipSyncReport",
    "TVRipValidationPoint",
    "Track",
    "default_user_config_path",
    "initialize_config_file",
    "load_configuration",
    "make_dual",
    "plan_pair",
    "scan_directory",
    "validate_configuration",
]

__version__ = "0.8.0"
