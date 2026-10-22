"""Regression tests: export download tokens must not bypass ownership checks."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app.api.v1.exports import download
from app.core.errors import NotFoundError
from vms_contracts.enums import Permission


def test_export_download_rejects_other_users_token_before_verification():
    job = SimpleNamespace(requested_by="owner-user")
    session = Mock()
    session.get.return_value = job
    principal = SimpleNamespace(user_id="different-user", is_admin=False,
                                require=Mock())
    exports = SimpleNamespace(verify_download_token=Mock())
    container = SimpleNamespace(exports=exports)

    with pytest.raises(NotFoundError):
        download("export-1", "valid-but-stolen-token", session, container, principal)

    principal.require.assert_called_once_with(Permission.EXPORT_DOWNLOAD)
    exports.verify_download_token.assert_not_called()
    session.commit.assert_not_called()


def test_export_download_rejects_unknown_export_before_verification():
    session = Mock()
    session.get.return_value = None
    principal = SimpleNamespace(user_id="user-1", is_admin=False, require=Mock())
    exports = SimpleNamespace(verify_download_token=Mock())
    container = SimpleNamespace(exports=exports)

    with pytest.raises(NotFoundError):
        download("missing", "any-token", session, container, principal)

    exports.verify_download_token.assert_not_called()
