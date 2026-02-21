"""Custom exception hierarchy for the Meta AI scraper."""


class MetaAIError(Exception):
    """Base exception for all Meta AI scraper errors."""


class ChallengeError(MetaAIError):
    """Raised when a bot-detection challenge cannot be solved."""


class CookieExtractionError(MetaAIError):
    """Raised when required cookies cannot be extracted from the homepage."""


class TokenError(MetaAIError):
    """Raised when the access token request or parsing fails."""


class SessionExhaustedError(MetaAIError):
    """Raised when a Meta AI session is exhausted even after refresh."""


class SendMessageError(MetaAIError):
    """Raised when the sendMessage GraphQL call fails."""


class FetchSourcesError(MetaAIError):
    """Raised when source URL fetching fails."""


class LowQualityResponseError(MetaAIError):
    """Raised when a response is empty or too short to be useful."""
