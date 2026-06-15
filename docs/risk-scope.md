# Risk Scope

Daily-loss, start-equity, profit-drawdown, and rolling-equity protections are
account-wide. They include manual positions and positions opened by other EAs.
This prevents another process on the same MT5 account from consuming loss
capacity that an AQ worker cannot see.

Per-symbol exposure, duplicate-signal checks, and position management remain
per agent and per configured magic number. Emergency stop is engine-scoped: it
pauses new entries and closes only positions carrying that agent's magic number.

Operators must assign a unique magic number to every agent.
