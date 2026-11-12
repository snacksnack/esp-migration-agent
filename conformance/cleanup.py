"""Remove one conformance run's fixtures from the destination (RC1-641, ADR-0029).

The product never deletes destination records; this is the single narrow
exception, and it is fenced by the run's namespace:

- contacts are deleted only if their address matches `cik-<run>-NNNN@<domain>`,
- the list and the campaign only if their names are exactly the run's,
- anything on the run's list outside the namespace is reported as foreign
  and left alone (it was not created by this run).

After deleting, the destination is read back and whatever is still there is
reported as `remaining`. Cleanup is idempotent: a second pass finds nothing
and succeeds.
"""

from __future__ import annotations

from conformance.fixture import email_pattern

CLEANUP_SCHEMA = "esp.conformance-cleanup/1"


class CleanupRefused(ValueError):
    pass


def guard(expected: dict) -> None:
    """Refuse an expected record that names anything outside its run."""
    run_id = expected["run_id"]
    pattern = email_pattern(run_id, expected["domain"])
    wanted = f"cik-conformance {run_id}"
    if expected["audience"] != wanted or expected["campaign_name"] != wanted:
        raise CleanupRefused("audience/campaign name is not this run's namespace")
    if expected["list_name"] != f"{wanted} (imported from mailchimp)":
        raise CleanupRefused("list name is not this run's namespace")
    outside = [e for e in expected["emails"] if not pattern.match(e)]
    if outside:
        raise CleanupRefused(f"{len(outside)} expected address(es) outside the namespace")


def survey(client, expected: dict) -> dict:
    """What of this run is on the destination right now (read-only)."""
    pattern = email_pattern(expected["run_id"], expected["domain"])
    list_id = client.find_list(expected["list_name"])
    members = client.current_subscribers(list_id) if list_id is not None else {}
    found = client.lookup_subscribers(expected["emails"])
    contacts = {e: s for e, s in found.items() if pattern.match(e)}
    contacts.update({e: s for e, s in members.items() if pattern.match(e)})
    return {
        "list_id": list_id,
        "campaign_id": client.find_campaign_id(expected["campaign_name"]),
        "contacts": contacts,
        "foreign_members": sorted(e for e in members if not pattern.match(e)),
    }


def cleanup(client, expected: dict) -> dict:
    guard(expected)
    before = survey(client, expected)
    for sub in before["contacts"].values():
        client.delete_contact(sub.id)
    if before["campaign_id"] is not None:
        client.delete_campaign(before["campaign_id"])
    # The list goes last, and only when nothing foreign is on it: deleting a
    # list a non-run contact belongs to would touch something this run did
    # not create.
    list_deleted = False
    if before["list_id"] is not None and not before["foreign_members"]:
        client.delete_list(before["list_id"])
        list_deleted = True
    after = survey(client, expected)
    remaining = {
        "contacts": len(after["contacts"]),
        "list": after["list_id"] is not None,
        "campaign": after["campaign_id"] is not None,
        "foreign_members": len(after["foreign_members"]),
    }
    return {
        "schema": CLEANUP_SCHEMA,
        "run_id": expected["run_id"],
        "deleted": {
            "contacts": len(before["contacts"]),
            "campaign": before["campaign_id"] is not None,
            "list": list_deleted,
        },
        "remaining": remaining,
        "clean": not any(remaining.values()),
    }
