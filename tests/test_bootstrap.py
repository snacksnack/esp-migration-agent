"""The dev-Listmonk bootstrap, and the one thing it must never do.

RC1-617: the API token goes to a file the caller names, at mode 0600, and
never to stdout. The old version printed it, which both call sites happened
to capture rather than log — but "the caller redirects it" is not a property
this module can guarantee, and one dropped redirect in a public repo's
workflow puts a credential in a build log.

The token-never-printed test is the point of this file; the rest guard the
file handling it now depends on.
"""

from __future__ import annotations

import os
import pathlib
import stat

import pytest

from executor import bootstrap

TOKEN = "listmonk-api-token-should-never-be-printed"
URL = "http://localhost:9000"


@pytest.fixture
def created(monkeypatch):
    """Stand in for the live Listmonk call, which needs a container."""
    monkeypatch.setattr(
        bootstrap, "create_api_user",
        lambda url, user, password: (bootstrap.API_USERNAME, TOKEN))


def run(tmp_path: pathlib.Path, *extra: str) -> int:
    return bootstrap.main(["--out", str(tmp_path / ".env.listmonk"), *extra])


def test_env_lines_carry_the_three_settings_the_adapter_reads():
    assert bootstrap.env_lines(URL, "u", "t") == [
        f"ESP_LISTMONK_URL={URL}",
        "ESP_LISTMONK_USERNAME=u",
        "ESP_LISTMONK_PASSWORD=t",
    ]


def test_the_token_is_never_printed(created, tmp_path, capsys):
    """The CodeQL finding, as a test. This is the one that matters."""
    assert run(tmp_path) == 0
    captured = capsys.readouterr()
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


def test_the_token_is_written_to_the_named_file(created, tmp_path):
    assert run(tmp_path) == 0
    written = (tmp_path / ".env.listmonk").read_text()
    assert f"ESP_LISTMONK_PASSWORD={TOKEN}" in written
    assert f"ESP_LISTMONK_URL={URL}" in written
    assert f"ESP_LISTMONK_USERNAME={bootstrap.API_USERNAME}" in written


def test_stdout_still_says_where_the_token_went(created, tmp_path, capsys):
    """Silent success on a credential tool reads as a failure."""
    assert run(tmp_path) == 0
    out = capsys.readouterr().out
    assert ".env.listmonk" in out
    assert "not shown here" in out
    # The non-secret settings are still printed, so a human can see the shape.
    assert f"ESP_LISTMONK_URL={URL}" in out


def test_a_file_this_process_creates_is_owner_only(created, tmp_path):
    assert run(tmp_path) == 0
    mode = stat.S_IMODE((tmp_path / ".env.listmonk").stat().st_mode)
    assert mode == bootstrap.SECRET_MODE


def test_the_default_overwrites_rather_than_accumulating(created, tmp_path):
    path = tmp_path / ".env.listmonk"
    path.write_text("ESP_LISTMONK_PASSWORD=stale\n")
    assert run(tmp_path) == 0
    assert "stale" not in path.read_text()
    assert path.read_text().count("ESP_LISTMONK_PASSWORD=") == 1


def test_append_keeps_what_an_earlier_step_wrote(created, tmp_path):
    """`$GITHUB_ENV` belongs to the runner; overwriting it loses earlier steps."""
    path = tmp_path / ".env.listmonk"
    path.write_text("SET_BY_AN_EARLIER_STEP=1\n")
    assert run(tmp_path, "--append") == 0
    written = path.read_text()
    assert "SET_BY_AN_EARLIER_STEP=1" in written
    assert f"ESP_LISTMONK_PASSWORD={TOKEN}" in written


def test_appending_to_a_file_we_did_not_create_leaves_its_mode_alone(
        created, tmp_path):
    """Tightening a file the caller owns is a surprise, not a hardening."""
    path = tmp_path / ".env.listmonk"
    path.write_text("EARLIER=1\n")
    os.chmod(path, 0o644)
    assert run(tmp_path, "--append") == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_appending_to_a_file_that_is_not_there_yet_is_still_owner_only(
        created, tmp_path):
    assert run(tmp_path, "--append") == 0
    mode = stat.S_IMODE((tmp_path / ".env.listmonk").stat().st_mode)
    assert mode == bootstrap.SECRET_MODE


def test_a_failed_write_says_the_token_is_unrecoverable(created, tmp_path,
                                                        capsys):
    """The API user exists by then; a bare OSError invites a useless retry."""
    unwritable = tmp_path / "no-such-dir" / ".env.listmonk"
    assert bootstrap.main(["--out", str(unwritable)]) == 1
    err = capsys.readouterr().err
    assert "could not write" in err
    assert "only shown at creation" in err
    assert TOKEN not in err


def test_a_failed_login_reports_without_writing_anything(monkeypatch, tmp_path,
                                                         capsys):
    def boom(url, user, password):
        raise bootstrap.BootstrapError("no login nonce")

    monkeypatch.setattr(bootstrap, "create_api_user", boom)
    assert run(tmp_path) == 1
    assert "no login nonce" in capsys.readouterr().err
    assert not (tmp_path / ".env.listmonk").exists()


def test_the_default_output_path_is_gitignored():
    """A credential file the repo would happily commit is the whole risk."""
    root = pathlib.Path(__file__).resolve().parent.parent
    patterns = [
        line.strip() for line in (root / ".gitignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert ".env.*" in patterns
    assert "!.env.example" in patterns
    assert bootstrap.DEFAULT_OUT.startswith(".env.")
