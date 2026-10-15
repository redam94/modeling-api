"""Executors that run fit/score work (see :mod:`mapi_spec.executor` for the protocol)."""

from mapi_sdk.executors.inproc import DataResolver, InprocExecutor, mapping_resolver

__all__ = ["DataResolver", "InprocExecutor", "mapping_resolver"]
