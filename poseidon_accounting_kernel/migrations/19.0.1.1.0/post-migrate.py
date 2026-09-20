"""Retire the superseded 2025.2 kernel version on existing databases.

The 2025.3 L0/L1/L2 records are created by the (noupdate) data file during this
same upgrade. Here we only deactivate the prior 2025.2 version so the active
kernel resolves to 2025.3. Raw SQL is used deliberately: `active`/`status` are
protected on frozen records by the ORM write guard, and a migration must be able
to retire a frozen record without tripping that guard.
"""


def migrate(cr, version):
    cr.execute(
        """
        UPDATE poseidon_kernel_version
        SET active = FALSE,
            status = 'deprecated'
        WHERE kernel_version = '2025.2'
        """
    )
