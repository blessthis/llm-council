"""P2: caller `?` placeholders -> postgres `$N` conversion."""

from llm_council.db.postgres import to_dollar


def test_no_placeholders():
    assert to_dollar("SELECT 1 FROM councils") == "SELECT 1 FROM councils"


def test_single_placeholder():
    assert to_dollar("SELECT * FROM councils WHERE id=?") == \
        "SELECT * FROM councils WHERE id=$1"


def test_numbered_sequentially():
    assert to_dollar(
        "INSERT INTO councils (working_dir, brief, owner, kind) VALUES (?, ?, ?, ?)"
    ) == "INSERT INTO councils (working_dir, brief, owner, kind) VALUES ($1, $2, $3, $4)"


def test_update_mixed():
    assert to_dollar("UPDATE mcp_instances SET last_seen=? WHERE instance_id=?") == \
        "UPDATE mcp_instances SET last_seen=$1 WHERE instance_id=$2"
