"""Public HTTP errors with fixed messages and sanitized validation paths."""

import logging
from collections.abc import Mapping
from typing import Literal

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

_logger = logging.getLogger(__name__)
_ERRORS = {
    400: ("invalid_json", "Проверьте формат JSON."),
    401: ("invalid_credentials", "Неверный email или пароль."),
    403: ("forbidden", "Доступ запрещён."),
    404: ("not_found", "Ресурс не найден."),
    405: ("method_not_allowed", "Метод не поддерживается."),
    409: ("conflict", "Запрос конфликтует с текущим состоянием."),
    415: ("unsupported_media_type", "Тип содержимого не поддерживается."),
    422: ("validation_error", "Проверьте параметры запроса."),
    429: ("rate_limited", "Слишком много запросов. Повторите позже."),
    500: ("internal_error", "Не удалось обработать запрос."),
}


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class ValidationField(BaseModel):
    location: Literal["body", "query", "path", "header"]
    path: list[str | int]
    code: Literal["required", "invalid_type", "invalid_value", "unknown_field"]


class ValidationErrorBody(ErrorBody):
    fields: list[ValidationField]


class ValidationErrorResponse(BaseModel):
    error: ValidationErrorBody


def error_response(
    status: int, *, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    code, message = _ERRORS[status]
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


async def http_error_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, HTTPException)
    if error.status_code not in _ERRORS:
        _logger.error("internal_error")
        return error_response(500)
    return error_response(error.status_code, headers=error.headers)


async def request_validation_error_handler(
    request: Request, error: Exception
) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    fields: list[ValidationField] = []
    for issue in error.errors():
        kind = issue["type"]
        if kind == "json_invalid":
            return error_response(400)
        location, *path = issue["loc"]
        code: Literal["required", "invalid_type", "invalid_value", "unknown_field"]
        if kind == "missing":
            code = "required"
        elif kind == "extra_forbidden":
            code = "unknown_field"
            path.pop()
        elif kind.endswith(("_type", "_parsing")):
            code = "invalid_type"
        else:
            code = "invalid_value"
        fields.append(ValidationField(location=location, path=path, code=code))
    code_422, message = _ERRORS[422]
    body = ValidationErrorResponse(
        error=ValidationErrorBody(code=code_422, message=message, fields=fields)
    )
    return JSONResponse(status_code=422, content=body.model_dump())


async def database_error_handler(request: Request, error: Exception) -> JSONResponse:
    _logger.error("database_error")
    return error_response(500)
