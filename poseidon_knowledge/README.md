# Meridian Knowledge

Extends the existing Knowledge stack with company-group sharing, ownership/review
metadata and persistent `document.page` → `account.account` links. The USGAAP
contract adopts `knowledge` and this bridge explicitly; regenerate only that
profile and the generated umbrella dependency list when changing adoption.

The frontend is `/dashboard/knowledge`. BFF reads/writes run as the signed-in
operator. Pages remain in the selected workspace's tenant backend database, not
in frontend storage. The existing base page/history ACLs remain authoritative;
additive group sharing extends their global company rules. Members read,
accountants edit company/group drafts, and managers govern workspace-wide pages
and publication. Direct ORM publication/content checks protect the same boundaries.

Publication requires Draft → Review → Published. Content edits return review
pages to Draft; published content must be explicitly returned to Draft first.
A row lock and source revision reject stale BFF saves. Account links must belong
to the declared scope and do not create accounting entries.

The existing account-context agent tool optionally reads up to five accessible,
published linked pages with source references. Content remains untrusted guidance;
no draft is published to the agent, no model training is performed, and the reader
does not bypass ACLs with `sudo()`.

Validation: `/poseidon_knowledge` Odoo tests in disposable `ktest`; BFF authorization
and source-confirmation tests in `src/test/integration/knowledge.integration.ts`.

Account relations target active company Kernel L3 destinations. QBO account rows
are historical sources and are excluded even when carrying a standard mapping or
L3 metadata. An unmapped QBO chart never substitutes for the operational chart.
