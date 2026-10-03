import pytest

from tools.check_public_docstrings import source_violations


def test_public_docstring_gate_accepts_documented_interfaces_and_local_helpers():
    """Allow documented public APIs while ignoring nested implementation helpers."""
    source = '''
def public_function():
    """Documented function."""
    def local_helper():
        return 1
    return local_helper()


class PublicClass:
    """Documented class."""

    def public_method(self):
        """Documented method."""
        return 1

    def _private_method(self):
        return 2


def _private_function():
    return 3
'''

    assert source_violations(source, filename="sample.py") == []


def test_public_docstring_gate_reports_top_level_class_and_method_debt():
    """Report each true public interface that lacks a docstring."""
    source = '''
def missing_function():
    return 1


class MissingClass:
    def missing_method(self):
        return 2
'''

    assert source_violations(source, filename="sample.py") == [
        "sample.py:2: public function missing_function missing docstring",
        "sample.py:6: public class MissingClass missing docstring",
        "sample.py:7: public method MissingClass.missing_method missing docstring",
    ]


def test_public_docstring_gate_surfaces_invalid_python():
    """Propagate syntax errors so CI cannot mistake invalid source for compliance."""
    with pytest.raises(SyntaxError):
        source_violations("def broken(:\n", filename="broken.py")
