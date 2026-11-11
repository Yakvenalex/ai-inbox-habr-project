class AppError(Exception):
    code = "application_error"


class InvalidInput(AppError):
    code = "invalid_input"


class NotFound(AppError):
    code = "not_found"


class Unauthorized(AppError):
    code = "unauthorized"


class Forbidden(AppError):
    code = "forbidden"


class Conflict(AppError):
    code = "conflict"


class ProviderUnavailable(AppError):
    code = "provider_unavailable"


class TooLarge(AppError):
    code = "too_large"


class AssistantFailure(AppError):
    code = "assistant_error"
