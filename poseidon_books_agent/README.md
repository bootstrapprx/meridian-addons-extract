# Meridian Books Agent

Historical journal intake and personal review worklists for Meridian. See
[architecture](../../../../context/architecture.md) and
[decision](../../../../docs/reference/meridian/books-intake-decision.md).

The journal-only history request creates a durable backend task. Its worker uses
the saved requester's company and accounting permissions. Source revisions are
retained independently of the ledger, including unresolved report evidence.
Preparing an API journal proposal checks confirmed account mapping, journal/bank
setup, optional source Class linkage, supported currency and accounting locks.
A manager reviews the exact current snapshot before creating one native draft.
The agent does not post entries. Repeated ingress and draft approval are idempotent.

Personal inbox records have separate owner-scoped read state, folders and private
correction notes. The Journal BFF exposes current proposals and paging; the
read-only MCP worklist exposes blockers and source references but excludes full
transaction previews and private notes from model context.

Install through the USGAAP profile contract. MD is the deployed workspace for
this slice. The normal QBO importer remains unchanged in databases where this
addon is absent. Do not directly call the old live-journal upsert to bypass intake.

Module-scoped tests: `--test-tags=/poseidon_books_agent`, using the disposable
`ktest` database. Mock external QBO calls; see `tests/test_books_intake.py`.
