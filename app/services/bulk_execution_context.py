from contextvars import ContextVar


durable_execution: ContextVar[bool] = ContextVar('durable_execution', default=False)
