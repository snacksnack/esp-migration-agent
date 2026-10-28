"""RC1-572: malformed exports die loudly with a named error - never a silent
zero, never a pydantic stack. Each variant is built from a VALID synthetic
export and corrupted in tmp_path, so no broken file is ever committed.

One corruption cannot be caught by the parser at all: truncation exactly at
a row boundary leaves a valid, shorter CSV. That one is caught a layer up,
by the risk register's comparison against the source's own recipient count."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from discovery.inventory import build_inventory
from discovery.risks import build_risk_register
from ingest.mailchimp import IngestError, load_bundle
from seeding.bundle_writer import make_campaign, write_audience_csvs, write_supplement
from seeding.generator import generate

SUBSCRIBED_GLOB = "subscribed_*.csv"


@pytest.fixture()
def export(tmp_path) -> tuple[Path, Path]:
    """A valid, complete export: audience CSVs + a supplement whose one
    unsegmented campaign counts the subscribed rows exactly."""
    subs, _ = generate(200, seed=99)
    audience, supplement = tmp_path / "audience", tmp_path / "supplement"
    write_audience_csvs(subs, audience)
    subscribed = sum(1 for s in subs if s.status == "subscribed")
    write_supplement(
        supplement,
        campaigns=[make_campaign("c1", "News", "News", "<p>Hi *|FNAME|*</p>",
                                 "L1", subscribed)],
    )
    load_bundle(audience, supplement)  # the pristine export is valid
    return audience, supplement


def subscribed_csv(audience: Path) -> Path:
    return next(audience.glob(SUBSCRIBED_GLOB))


# --- truncation ---------------------------------------------------------------------


@pytest.mark.parametrize("fraction", [0.31, 0.5, 0.77, 0.93])
def test_mid_row_truncation_is_a_named_error(export, fraction):
    audience, supplement = export
    path = subscribed_csv(audience)
    data = path.read_bytes()
    cut = int(len(data) * fraction)
    if data[cut - 1:cut] == b"\n":  # a boundary cut is the next test's case
        cut += 1
    path.write_bytes(data[:cut])
    with pytest.raises(IngestError, match=path.name):
        load_bundle(audience, supplement)


def test_row_boundary_truncation_parses_but_the_risk_register_catches_it(export):
    audience, supplement = export
    path = subscribed_csv(audience)
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[: len(lines) // 2]))  # whole rows only

    bundle = load_bundle(audience, supplement)  # parses: the CSV is valid
    risks = build_risk_register(bundle, build_inventory(bundle), "listmonk")
    mismatch = [r for r in risks if r.code == "export_row_count_mismatch"]
    assert len(mismatch) == 1
    assert mismatch[0].severity == "high"
    assert mismatch[0].count == len(lines) - len(lines) // 2


# --- encodings ----------------------------------------------------------------------


def _with_accented_name(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    header, first, rest = text.split("\n", 2)
    cells = first.split(",")
    cells[1] = "José"  # First Name
    return "\n".join([header, ",".join(cells), rest])


@pytest.mark.parametrize("encoding", ["latin-1", "utf-16"])
def test_wrong_encoding_is_a_named_error(export, encoding):
    audience, supplement = export
    path = subscribed_csv(audience)
    path.write_bytes(_with_accented_name(path).encode(encoding))
    with pytest.raises(IngestError, match="not valid UTF-8"):
        load_bundle(audience, supplement)


def test_utf8_bom_from_an_excel_resave_is_accepted(export):
    """Excel re-saves prepend a byte-order mark. The file is still UTF-8 and
    must parse identically - a BOM is not a reason to refuse an export."""
    audience, supplement = export
    before = load_bundle(audience, supplement)
    path = subscribed_csv(audience)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    after = load_bundle(audience, supplement)
    assert after.members == before.members
    assert after.extra_columns == before.extra_columns == {}


def test_semicolon_delimited_resave_is_a_named_error(export):
    """European-locale spreadsheet re-saves switch the delimiter: every
    expected column goes missing at once, and the error says which."""
    audience, supplement = export
    path = subscribed_csv(audience)
    path.write_text(path.read_text(encoding="utf-8").replace(",", ";"), encoding="utf-8")
    with pytest.raises(IngestError, match="missing expected columns"):
        load_bundle(audience, supplement)


# --- missing and empty files ----------------------------------------------------------


@pytest.mark.parametrize("name", ["segments.json", "templates.json", "campaigns.json"])
def test_missing_supplement_file_is_a_named_error(export, name):
    audience, supplement = export
    (supplement / name).unlink()
    with pytest.raises(IngestError, match=f"{name}: missing"):
        load_bundle(audience, supplement)


def test_truncated_supplement_json_is_a_named_error(export):
    audience, supplement = export
    path = supplement / "campaigns.json"
    path.write_text(path.read_text()[:40])
    with pytest.raises(IngestError, match="campaigns.json: invalid JSON"):
        load_bundle(audience, supplement)


def test_headers_only_bucket_is_a_named_error(export):
    audience, supplement = export
    path = subscribed_csv(audience)
    path.write_text(path.read_text(encoding="utf-8").split("\n", 1)[0] + "\n")
    with pytest.raises(IngestError, match="no member rows"):
        load_bundle(audience, supplement)


def test_missing_bucket_file_is_a_named_error(export):
    audience, supplement = export
    next(audience.glob("nonsubscribed_*.csv")).unlink()
    with pytest.raises(IngestError, match="expected exactly one nonsubscribed"):
        load_bundle(audience, supplement)


def test_row_with_surplus_cells_is_a_named_error(export):
    """An unquoted comma inside a value (a hand-edited export) shifts every
    later cell right: the row has more cells than the header has columns."""
    audience, supplement = export
    path = subscribed_csv(audience)
    header, first, rest = path.read_text(encoding="utf-8").split("\n", 2)
    path.write_text("\n".join([header, first + ",stray", rest]), encoding="utf-8")
    with pytest.raises(IngestError, match="row 2: more cells than the header"):
        load_bundle(audience, supplement)


def test_two_audiences_dropped_in_one_directory_are_refused(export, tmp_path):
    """The parser reads ONE audience. Two exports unpacked into one folder
    must not be merged into a single migration - refused, by name."""
    audience, supplement = export
    other = tmp_path / "other"
    write_audience_csvs(generate(50, seed=7)[0], other, export_id="second")
    for path in other.glob("*.csv"):
        shutil.copy(path, audience / path.name)
    with pytest.raises(IngestError, match="expected exactly one subscribed"):
        load_bundle(audience, supplement)
