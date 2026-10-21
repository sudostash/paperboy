"""Result types: every key always present, so schemas never need optional keys."""
from __future__ import annotations

import inspect
import typing

import pytest
from typing_extensions import is_typeddict

from paperboy import engine, results
from paperboy import server as S

TYPES = [obj for _, obj in inspect.getmembers(results, is_typeddict)
         if obj.__module__ == results.__name__]
PROTECTION = {"password_protected", "permissions_restricted", "user_password_changed"}


@pytest.mark.parametrize("typ", TYPES, ids=lambda t: t.__name__)
def test_every_key_is_required(typ):
    assert typ.__optional_keys__ == frozenset()
    assert typ.__required_keys__ == frozenset(typ.__annotations__)


def _result_type(fn):
    return typing.get_type_hints(inspect.unwrap(fn))["return"]


@pytest.mark.parametrize("name", engine.__all__)
def test_engine_and_server_declare_the_same_result_type(name):
    engine_type = _result_type(getattr(engine, name))
    assert engine_type in TYPES
    assert _result_type(getattr(S, name)) is engine_type


@pytest.mark.parametrize("name", [
    "rearrange_pages", "delete_pages", "extract_pages", "rotate_pages", "merge_pdfs",
    "split_pdf", "optimize_pdf", "repair_pdf", "flatten_pdf", "set_metadata",
])
def test_tools_that_preserve_protection_report_it(name):
    assert PROTECTION <= _result_type(getattr(engine, name)).__required_keys__
