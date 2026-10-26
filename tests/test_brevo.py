"""RC1-547: the Brevo adapter's translation layer - pure functions only.
The HTTP path is covered by the gated integration test, same as Listmonk."""

from __future__ import annotations

import pytest

from executor.brevo import (
    TAG_FROM_BREVO,
    TAG_TO_BREVO,
    attrib_from_brevo,
    attrib_to_brevo,
    body_from_brevo,
    body_to_brevo,
    consent_from_brevo,
    consent_to_brevo,
    normalize_value,
)
from executor.plan import DesiredSubscriber
from executor.target import TargetError


def desired(status: str, subscription: str, subscribable: bool) -> DesiredSubscriber:
    return DesiredSubscriber(
        email="a@example.com",
        name="A",
        status=status,
        subscription=subscription,
        subscribable=subscribable,
        suppressed=not subscribable,
        attribs={},
    )


# --- attribute and tag translation are bijections ------------------------------


def test_attrib_names_round_trip():
    for key in ("first_name", "zip", "mc_member_rating", "optin_time"):
        assert attrib_from_brevo(attrib_to_brevo(key)) == key


def test_tag_map_is_a_bijection():
    assert len(TAG_FROM_BREVO) == len(TAG_TO_BREVO)
    body = "<p>Hi {{ .Subscriber.Attribs.first_name }} from {{ .Subscriber.Attribs.state }},"
    body += ' <a href="{{ UnsubscribeURL }}">bye</a> <a href="{{ MessageURL }}">view</a></p>'
    assert body_from_brevo(body_to_brevo(body)) == body


def test_untranslatable_canonical_tag_refuses():
    with pytest.raises(TargetError, match="no\\s+Brevo translation"):
        body_to_brevo("<p>{{ .Subscriber.Attribs.shoe_size }}</p>")


def test_number_attributes_round_trip():
    assert normalize_value(2.0) == 2
    assert isinstance(normalize_value(2.0), int)
    assert normalize_value("01123") == "01123"  # padded zips stay text
    assert normalize_value(2.5) == 2.5


# --- consent: Brevo's one flag vs the canonical four states --------------------


def test_consent_lands_safe():
    assert consent_to_brevo(desired("enabled", "confirmed", True)) == (
        "enabled/confirmed", False,
    )
    # Brevo cannot represent unconfirmed: retained but NOT mailable.
    assert consent_to_brevo(desired("enabled", "unconfirmed", True)) == (
        "enabled/unconfirmed", True,
    )
    assert consent_to_brevo(desired("blocklisted", "unsubscribed", False)) == (
        "blocklisted", True,
    )


def test_consent_round_trips_while_coherent():
    for label, (status, subscription) in {
        "enabled/confirmed": ("enabled", "confirmed"),
        "enabled/unconfirmed": ("enabled", "unconfirmed"),
        "unsubscribed": ("enabled", "unsubscribed"),
        "blocklisted": ("blocklisted", "unsubscribed"),
    }.items():
        _, blacklisted = consent_to_brevo(desired(status, subscription, False))
        assert consent_from_brevo(label, blacklisted) == (status, subscription)


def test_unblacklisted_suppression_reports_actual_mailability():
    # A suppressed contact un-blacklisted in the Brevo UI must NOT keep its
    # stored label: reconciliation has to see it as mailable and fail.
    assert consent_from_brevo("blocklisted", blacklisted=False) == ("enabled", "confirmed")
    assert consent_from_brevo("unsubscribed", blacklisted=False) == ("enabled", "confirmed")


def test_unknown_label_falls_back_to_actual_flag():
    assert consent_from_brevo(None, blacklisted=True) == ("blocklisted", "unsubscribed")
    assert consent_from_brevo("garbage", blacklisted=False) == ("enabled", "confirmed")
