"""HTTP errors raised by the routes, so status codes and wording stay in one place."""
from fastapi import HTTPException, status


def not_found(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"No such {what}.")


def bad_request(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail)


def unauthorized(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


def conflict(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail)


def too_large(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail)


def unsupported_media(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail)


def unprocessable(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


def failed_dependency(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_424_FAILED_DEPENDENCY, detail)


def rate_limited(retry_after: int) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        f"Too many requests. Try again in {retry_after} seconds.",
        headers={"Retry-After": str(retry_after)})


def bad_gateway(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_502_BAD_GATEWAY, detail)


def unavailable(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail)
