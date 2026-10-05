"""Append-only protection for the decision audit trail (PostgreSQL).

Decision records (signals, features, scores, AI analyses, orders, execution/position events, trades, risk/system
events, outcome samples, labels, operator command log) can never be UPDATEd or DELETEd. Raw market data
(market_events, market_snapshots) may be DELETEd by retention jobs but never UPDATEd.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

IMMUTABLE = (
    "token_state_transitions", "wallet_events", "features", "scores", "ai_analyses", "signals", "orders",
    "execution_events", "position_events", "trades", "portfolio_snapshots", "risk_events", "system_events",
    "outcome_samples", "signal_outcomes", "operator_commands_log",
)
NO_UPDATE = ("market_events", "market_snapshots")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
    CREATE OR REPLACE FUNCTION ta_forbid_mutation() RETURNS trigger AS $$
    BEGIN
      RAISE EXCEPTION 'table % is append-only (% forbidden)', TG_TABLE_NAME, TG_OP;
    END;
    $$ LANGUAGE plpgsql;
    """)
    for t in IMMUTABLE:
        op.execute(f"CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {t} "
                   f"FOR EACH ROW EXECUTE FUNCTION ta_forbid_mutation();")
        op.execute(f"CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t} "
                   f"FOR EACH STATEMENT EXECUTE FUNCTION ta_forbid_mutation();")
    for t in NO_UPDATE:
        op.execute(f"CREATE TRIGGER {t}_no_update BEFORE UPDATE ON {t} "
                   f"FOR EACH ROW EXECUTE FUNCTION ta_forbid_mutation();")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for t in IMMUTABLE:
        op.execute(f"DROP TRIGGER IF EXISTS {t}_append_only ON {t};")
        op.execute(f"DROP TRIGGER IF EXISTS {t}_no_truncate ON {t};")
    for t in NO_UPDATE:
        op.execute(f"DROP TRIGGER IF EXISTS {t}_no_update ON {t};")
    op.execute("DROP FUNCTION IF EXISTS ta_forbid_mutation();")
