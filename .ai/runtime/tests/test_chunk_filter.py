"""Tests for the CODEFILTER-style chunk impact filter PoC."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.chunk_filter import filter_chunks, score_chunk  # noqa: E402


# ---------------------------------------------------------------------------
# score_chunk
# ---------------------------------------------------------------------------


def test_score_chunk_high_overlap() -> None:
    """Snippet that re-uses query identifiers should be positive."""
    query = "get user balance"
    chunk = {
        "path": "wallet/service.py",
        "snippet": (
            "def get_user_balance(user_id: int) -> Decimal:\n"
            "    user = User.get(user_id)\n"
            "    balance = user.wallet.balance\n"
            "    return balance\n"
        ),
    }
    result = score_chunk(query, chunk)
    assert result["polarity"] == "pos"
    assert result["score"] >= 0.5
    assert any(r.startswith("shared_ids=") for r in result["reasons"])


def test_score_chunk_unrelated() -> None:
    """Snippet with zero token overlap should not be positive."""
    query = "get user balance"
    chunk = {
        "path": "render/svg.py",
        "snippet": (
            "def draw_rectangle(canvas, width, height):\n"
            "    canvas.fillStyle = 'red'\n"
            "    canvas.fillRect(0, 0, width, height)\n"
        ),
    }
    result = score_chunk(query, chunk)
    assert result["polarity"] in {"neg", "neu"}
    assert result["score"] < 0.5


def test_score_chunk_comment_only() -> None:
    """A comment-only block should be downgraded out of 'pos'."""
    query = "get user balance"
    chunk = {
        "path": "notes.py",
        "snippet": (
            "// TODO: get user balance later\n"
            "// note about user balance\n"
            "// another comment line\n"
            "// yet another comment\n"
            "// fifth comment about user\n"
        ),
    }
    result = score_chunk(query, chunk)
    assert "comment_only" in result["reasons"]
    assert result["polarity"] in {"neu", "neg"}


def test_score_chunk_too_short() -> None:
    """Tiny snippets pick up the snippet_too_short penalty."""
    query = "configure logging level"
    chunk = {"path": "x.py", "snippet": "pass"}
    result = score_chunk(query, chunk)
    assert "snippet_too_short" in result["reasons"]


# ---------------------------------------------------------------------------
# filter_chunks
# ---------------------------------------------------------------------------


def _sample_five_chunks() -> list[dict]:
    return [
        {
            "path": "wallet.py",
            "snippet": (
                "def get_user_balance(user_id):\n"
                "    return wallet_repo.balance_for(user_id)\n"
            ),
        },
        {
            "path": "user_service.py",
            "snippet": (
                "class UserService:\n"
                "    def fetch_user(self, user_id):\n"
                "        return self.db.get(user_id)\n"
            ),
        },
        {
            "path": "balance_utils.py",
            "snippet": (
                "def format_balance(balance: Decimal) -> str:\n"
                "    return f'{balance:.2f}'\n"
            ),
        },
        {
            "path": "totally_unrelated.py",
            "snippet": (
                "def render_button(label, onclick):\n"
                "    return f'<button>{label}</button>'\n"
            ),
        },
        # Comment-only noise chunk — should be flagged negative/neutral.
        {
            "path": "todo.py",
            "snippet": (
                "# random note one\n"
                "# random note two\n"
                "# random note three\n"
                "# random note four\n"
                "# random note five\n"
            ),
        },
    ]


def test_filter_drops_negatives_by_default() -> None:
    query = "get user balance"
    chunks = _sample_five_chunks()
    out = filter_chunks(query, chunks)
    assert out["ok"] is True
    # At least one chunk should be dropped as negative.
    assert len(out["dropped"]) >= 1
    for d in out["dropped"]:
        assert d["reason"] == "neg"
    # Kept chunks must all carry annotation fields.
    for ch in out["kept"]:
        assert "cf_score" in ch
        assert ch["cf_polarity"] in {"pos", "neu"}


def test_filter_drop_negatives_false_keeps_all() -> None:
    query = "get user balance"
    chunks = _sample_five_chunks()
    out = filter_chunks(query, chunks, drop_negatives=False)
    assert len(out["kept"]) == len(chunks)
    assert out["dropped"] == []


def test_filter_max_keep_truncates() -> None:
    query = "get user balance"
    chunks = [
        {
            "path": f"f{i}.py",
            "snippet": f"def get_user_balance_{i}(user_id):\n    return user_id + {i}\n",
        }
        for i in range(10)
    ]
    out = filter_chunks(query, chunks, max_keep=3)
    assert len(out["kept"]) == 3
    truncated = [d for d in out["dropped"] if d["reason"] == "truncated"]
    # 10 input -> 3 kept; remaining 7 should appear as truncated (or neg+truncated).
    assert len(truncated) + sum(1 for d in out["dropped"] if d["reason"] == "neg") == 7


def test_filter_summary_counts_match() -> None:
    query = "get user balance"
    chunks = _sample_five_chunks()
    out = filter_chunks(query, chunks)
    s = out["summary"]
    assert s["pos"] + s["neu"] + s["neg"] == len(chunks)


def test_empty_input() -> None:
    out = filter_chunks("anything", [])
    assert out == {
        "ok": True,
        "kept": [],
        "dropped": [],
        "summary": {"pos": 0, "neg": 0, "neu": 0},
    }


def test_none_query_does_not_crash() -> None:
    # The contract says we tolerate empty/None-ish query; verify no exception.
    out = filter_chunks("", _sample_five_chunks())
    assert out["ok"] is True
    assert out["summary"]["pos"] + out["summary"]["neu"] + out["summary"]["neg"] == 5
