class CuaJevError(RuntimeError):
    """Base error for controlled runtime failures."""


class PolicyError(CuaJevError):
    """The decision backend returned an unusable answer."""


class GuardRejected(CuaJevError):
    """The action guard rejected a candidate before execution."""


class ObservationExpired(GuardRejected):
    """The captured GUI evidence must be observed and bound again before input."""


class CapabilityUnavailable(CuaJevError):
    """An optional platform capability is not installed or connected."""


class ExecutionFailure(CuaJevError):
    """An executor could not complete a guarded action."""
