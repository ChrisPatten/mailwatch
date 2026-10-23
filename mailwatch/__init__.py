"""mailwatch: a quarantined email reader for AI agents.

The model is a pure function (text in, schema-validated JSON out). Plain code owns
every action: IMAP is read-only, links are fetched only by the broker's rules, and
the downstream agent reads only code-built records.
"""

__version__ = "0.1.0"
