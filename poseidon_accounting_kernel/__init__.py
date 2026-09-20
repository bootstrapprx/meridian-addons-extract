from . import models


def post_init_hook(env):
    env["account.account"].sudo()._poseidon_lock_accounts_with_transactions()
    env["poseidon.kernel.version"].sudo().log_localization_warnings()
