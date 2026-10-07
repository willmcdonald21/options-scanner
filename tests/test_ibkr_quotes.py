"""The connect-time account guard in the quote/contract layer.

Only the guard is covered here: the rest of IBKRQuotes talks to a live Gateway
and is exercised by scripts/ib_probe.py rather than by unit tests.
"""

import pytest

from options_scanner.broker.base import BrokerError
from options_scanner.broker.ibkr_quotes import IBKRQuotes


class FakeIB:
    def __init__(self, accounts):
        self._accounts = accounts
        self.data_type = None

    def managedAccounts(self):
        return list(self._accounts)

    def reqMarketDataType(self, value):
        self.data_type = value

    def isConnected(self):
        return True


def quotes(accounts, account="") -> IBKRQuotes:
    q = IBKRQuotes("127.0.0.1", 4002, 12, account=account)
    q._ib = FakeIB(accounts)
    return q


# --- the accepted cases ---------------------------------------------------


def test_one_account_and_none_configured_is_fine():
    """Today's setup. IBKR assumes the only account, and ib_async reads all."""
    quotes(["DU111"])._check_account()


def test_a_configured_account_the_login_manages_is_fine():
    quotes(["DU111", "DU222"], account="DU222")._check_account()


def test_no_managed_accounts_warns_rather_than_refusing(caplog):
    """IB occasionally reports nothing here before it settles; refusing to run
    over that would be worse than carrying on."""
    with caplog.at_level("WARNING"):
        quotes([], account="DU222")._check_account()
    assert "no managed accounts" in caplog.text


# --- the refusals ---------------------------------------------------------


def test_several_accounts_with_none_configured_is_refused():
    """IBKR rejects every order in this state, and an unscoped position read
    would return the other bot's holdings. Better to fail at connect than on
    the first alert of the day."""
    with pytest.raises(BrokerError) as exc:
        quotes(["DU111", "DU222"])._check_account()

    message = str(exc.value)
    assert "manages 2 accounts" in message
    assert "DU111, DU222" in message
    assert "broker.account" in message


def test_a_configured_account_the_login_does_not_manage_is_refused():
    """A typo, or a paper id pasted from the wrong place."""
    with pytest.raises(BrokerError, match="is not managed by this login"):
        quotes(["DU111", "DU222"], account="DU999")._check_account()


def test_a_single_account_that_is_not_the_configured_one_is_refused():
    """The config describes a setup that does not exist."""
    with pytest.raises(BrokerError, match="is not managed by this login"):
        quotes(["DU111"], account="DU222")._check_account()


def test_whitespace_around_the_account_is_ignored():
    quotes(["DU111"], account="  DU111  ")._check_account()
