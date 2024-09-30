# this file should be mirroring
# https://github.com/BerriAI/litellm/blob/main/litellm/exceptions.py

import litellm


class Error(Exception):
    def __init__(self, litellm_error: Exception):
        self.status_code = litellm_error.status_code
        self.num_retries = litellm_error.num_retries
        self.max_retries = litellm_error.max_retries

    def __str__(self):
        _message = self.message
        if self.num_retries:
            _message += f" Retried: {self.num_retries} times"
        if self.max_retries:
            _message += f", Max Retries: {self.max_retries}"
        return _message

    def __repr__(self):
        _message = self.message
        if self.num_retries:
            _message += f" Retried: {self.num_retries} times"
        if self.max_retries:
            _message += f", Max Retries: {self.max_retries}"
        return _message


class AuthenticationError(Error):
    def __init__(self, litellm_error: litellm.AuthenticationError):
        self.message = "UnifyAuthenticationError: " + litellm_error.message.strip(
            "litellm.AuthenticationError: ",
        )
        super().__init__(litellm_error)


class NotFoundError(Error):
    def __init__(self, litellm_error: litellm.NotFoundError):
        self.message = "UnifyNotFoundError: " + litellm_error.message.strip(
            "litellm.NotFoundError: ",
        )
        super().__init__(litellm_error)


class BadRequestError(Error):
    def __init__(self, litellm_error: litellm.BadRequestError):
        self.message = "UnifyBadRequestError: " + litellm_error.message.strip(
            "litellm.BadRequestError: ",
        )
        super().__init__(litellm_error)


class UnprocessableEntityError(Error):
    def __init__(self, litellm_error: litellm.UnprocessableEntityError):
        self.message = "UnifyUnprocessableEntityError: " + litellm_error.message.strip(
            "litellm.UnprocessableEntityError: ",
        )
        super().__init__(litellm_error)


class Timeout(Error):
    def __init__(self, litellm_error: litellm.Timeout):
        self.message = "UnifyTimeout: " + litellm_error.message.strip(
            "litellm.Timeout: ",
        )
        super().__init__(litellm_error)


class RateLimitError(Error):
    def __init__(self, litellm_error: litellm.RateLimitError):
        self.message = "UnifyRateLimitError: " + litellm_error.message.strip(
            "litellm.RateLimitError: ",
        )
        super().__init__(litellm_error)


class ContextWindowExceededError(Error):
    def __init__(self, litellm_error: litellm.ContextWindowExceededError):
        self.message = (
            "UnifyContextWindowExceededError: "
            + litellm_error.message.strip(
                "litellm.ContextWindowExceededError: ",
            )
        )
        super().__init__(litellm_error)


class RejectedRequestError(BadRequestError):
    def __init__(self, litellm_error: litellm.BadRequestError):
        super().__init__(litellm_error)
        self.message = "UnifyRejectedRequestError: " + self.message.strip(
            "litellm.RejectedRequestError: ",
        )


class ContentPolicyViolationError(BadRequestError):
    def __init__(self, litellm_error: litellm.BadRequestError):
        super().__init__(litellm_error)
        self.message = "UnifyContentPolicyViolationError: " + self.message.strip(
            "litellm.ContentPolicyViolationError: ",
        )


class ServiceUnavailableError(Error):
    def __init__(self, litellm_error: litellm.ServiceUnavailableError):
        self.message = "UnifyServiceUnavailableError: " + litellm_error.message.strip(
            "litellm.ServiceUnavailableError: ",
        )
        super().__init__(litellm_error)


class InternalServerError(Error):
    def __init__(self, litellm_error: litellm.InternalServerError):
        self.message = "UnifyError: " + litellm_error.message.strip(
            "litellm.InternalServerError: ",
        )
        super().__init__(litellm_error)


class APIError(Error):
    def __init__(self, litellm_error: litellm.APIError):
        self.message = "UnifyAPIError: " + litellm_error.message.strip(
            "litellm.APIError: ",
        )
        super().__init__(litellm_error)


class APIConnectionError(Error):
    def __init__(self, litellm_error: litellm.APIConnectionError):
        self.message = "UnifyAPIConnectionError: " + litellm_error.message.strip(
            "litellm.APIConnectionError: ",
        )
        super().__init__(litellm_error)


class APIResponseValidationError(Error):
    def __init__(self, litellm_error: litellm.APIResponseValidationError):
        self.message = (
            "UnifyAPIResponseValidationError: "
            + litellm_error.message.strip(
                "litellm.APIResponseValidationError: ",
            )
        )
        super().__init__(litellm_error)


class JSONSchemaValidationError(APIError):
    def __init__(self, litellm_error: litellm.APIError):
        super().__init__(litellm_error)
        self.message = "UnifyJSONSchemaValidationError: " + self.message.strip(
            "litellm.JSONSchemaValidationError: ",
        )


class UnsupportedParamsError(BadRequestError):
    def __init__(self, litellm_error: litellm.BadRequestError):
        super().__init__(litellm_error)
        self.message = "UnifyUnsupportedParamsError: " + self.message.strip(
            "litellm.UnsupportedParamsError: ",
        )
