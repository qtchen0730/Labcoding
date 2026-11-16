"""Public account/authorization failures shared with the quota ledger."""


class AccountError(RuntimeError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


class QuotaError(AccountError):
    def __init__(self, message, code, status_code=402):
        super().__init__(message, status_code)
        self.code = code
