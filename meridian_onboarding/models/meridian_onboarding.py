import logging
import re
from calendar import month_name, monthrange
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.addons.qbo_bridge.models.qbo_account_bridge_rule import (
    account_type_guide_labels,
    mapping_match_reason,
)
from odoo.addons.qbo_bridge.models.qbo_sync_log import QboSyncLog
from odoo.addons.qbo_bridge.services.qbo_api_client import QBOApiClient

_logger = logging.getLogger(__name__)

# Frontend-driven wizard: the Next.js BFF calls these @api.model methods via
# /web/dataset/call_kw with the operator's session. Step completion is derived
# from real records on every status call — no stored wizard state — matching
# the kernel-readiness design note in meridian_saas._configure_company.
REQUIRED_STEPS = ("entities", "company_info", "fiscal_periods", "taxes", "chart")
OPTIONAL_STEPS = ("data_import", "mapping", "banks", "team")

# Journals every operating company needs before it can post a document.
JOURNAL_TYPES = {"sale", "purchase", "general"}
ENTITY_TYPES = {"llc", "s_corp", "c_corp", "partnership", "sole_proprietor", "unknown"}
ACCOUNTING_METHODS = {"cash", "accrual", "hybrid", "unknown"}
TAX_REGIMES = {"pass_through", "c_corp", "unknown"}
QBO_CONFIGURATION_FIELDS = {
    "legal_name",
    "ein",
    "address",
    "city",
    "zip",
    "phone",
    "email",
    "website",
    "state_code",
    "entity_type",
    "accounting_method",
    "tax_regime",
    "fiscalyear_last_month",
    "fiscalyear_last_day",
}

# Didactic guidance content, PT + EN, per concept. Every concept answers "what
# does this mean in plain language" (for non-accountants) and "what is the
# accounting reality" (for accountants). The "próxima ação única" coach on the
# frontend renders one concept at a time so the operator is never overwhelmed
# (Lei de Hick) — content is served on demand via get_guidance_content /
# get_production_guidance.
GUIDANCE_CONTENT = {
    "jurisdiction": {
        "pt": {
            "title": "Jurisdição (estado)",
            "plain": (
                "O estado de constituição e endereço da empresa. Ele define em "
                "qual jurisdição a empresa deve cumprir obrigações (impostos, "
                "registros). Preencha o endereço e o estado antes de continuar."
            ),
            "accountant": (
                "Define o nexus legal/fiscal primário. Sem jurisdição não há base "
                "para tax nexus, annual reports nem withholding. Preencha country e "
                "state no parceiro da empresa."
            ),
        },
        "en": {
            "title": "Jurisdiction (state)",
            "plain": (
                "The state where the company is formed and located. It defines the "
                "jurisdiction the company must comply with (taxes, filings). Fill "
                "the address and state before continuing."
            ),
            "accountant": (
                "Establishes the primary legal/tax nexus. Without a jurisdiction "
                "there is no basis for sales-tax nexus, annual reports or "
                "withholding. Fill country and state on the company partner."
            ),
        },
    },
    "entity_type": {
        "pt": {
            "title": "Tipo de entidade",
            "plain": (
                "A forma jurídica da empresa (LLC, S-Corp, C-Corp, parceria ou "
                "individual). Ela decide como o imposto chega — se a empresa paga "
                "por conta própria (C-Corp) ou se o lucro passa para a declaração "
                "dos sócios (LLC, S-Corp, parceria)."
            ),
            "accountant": (
                "Determina o regime de tributação (pass-through vs C-corp entity "
                "tax) e o tratamento de distribuições, compensações e SALT. Alimenta "
                "tax_regime no perfil de impostos."
            ),
        },
        "en": {
            "title": "Entity type",
            "plain": (
                "The legal form of the company (LLC, S-Corp, C-Corp, partnership "
                "or sole proprietor). It decides how tax lands — whether the "
                "company pays on its own (C-Corp) or profits flow to the owners' "
                "returns (LLC, S-Corp, partnership)."
            ),
            "accountant": (
                "Determines the tax regime (pass-through vs C-corp entity tax) and "
                "the treatment of distributions, officer compensation and SALT. "
                "Feeds tax_regime on the tax profile."
            ),
        },
    },
    "accounting_method": {
        "pt": {
            "title": "Método contábil",
            "plain": (
                "A regra que decide quando uma receita ou despesa entra nos "
                "números. No regime de caixa, entra quando o dinheiro muda de mão; "
                "no regime de competência, entra quando a venda ou a obrigação "
                "acontece. Em competência, faturas em aberto já contam como "
                "receita e contas a pagar como despesa."
            ),
            "accountant": (
                "Cash vs accrual (vs hybrid) define o reconhecimento de receitas e "
                "despesas e a materialização de receivables/payables. Meridian "
                "preset para US GAAP é accrual; confirme se a empresa elegeu cash "
                "para fins fiscais (IRC §446)."
            ),
        },
        "en": {
            "title": "Accounting method",
            "plain": (
                "The rule that decides when a revenue or expense enters the books. "
                "On cash basis, money is recorded when it changes hands; on "
                "accrual basis, when the sale or obligation happens. On accrual, "
                "open invoices already count as revenue and unpaid bills as "
                "expenses."
            ),
            "accountant": (
                "Cash vs accrual (vs hybrid) drives revenue/expense recognition and "
                "receivables/payables materialization. Meridian US GAAP preset is "
                "accrual; confirm whether the entity elected cash for tax "
                "(IRC §446)."
            ),
        },
    },
    "fiscal_year_end": {
        "pt": {
            "title": "Fim do ano fiscal",
            "plain": (
                "A data em que o ano contábil da empresa termina (quase sempre 31 "
                "de dezembro). É quando os períodos se fecham e o resultado anual "
                "é apurado."
            ),
            "accountant": (
                "Define o ciclo anual de encerramento (closing calendar). O mês e o "
                "dia alimentam fiscalyear_last_month/day e a geração dos períodos "
                "fiscais no kernel."
            ),
        },
        "en": {
            "title": "Fiscal year end",
            "plain": (
                "The date when the company's accounting year ends (almost always "
                "December 31). It is when periods close and the annual result is "
                "measured."
            ),
            "accountant": (
                "Defines the annual closing calendar. The month and day feed "
                "fiscalyear_last_month/day and fiscal-period generation on the "
                "kernel."
            ),
        },
    },
    "fiscal_periods": {
        "pt": {
            "title": "Períodos fiscais",
            "plain": (
                "Os períodos de fechamento (mensais ou trimestrais) que dividem o "
                "ano contábil. Sem eles, o sistema não sabe quando 'virar a página' "
                "nem bloquear lançamentos de um mês já fechado."
            ),
            "accountant": (
                "Períodos (poseidon.kernel.period) habilitam o ciclo de fechamento "
                "e lock de contas por mês/trimestre. Criados abertos; o lock é ato "
                "deliberado pós-onboarding."
            ),
        },
        "en": {
            "title": "Fiscal periods",
            "plain": (
                "The closing periods (monthly or quarterly) that divide the "
                "accounting year. Without them the system cannot know when to 'turn "
                "the page' or lock entries from an already-closed month."
            ),
            "accountant": (
                "Periods (poseidon.kernel.period) enable the closing cycle and "
                "per-account locking by month/quarter. Created open; locking is a "
                "deliberate post-onboarding act."
            ),
        },
    },
    "taxes": {
        "pt": {
            "title": "Impostos e nexus",
            "plain": (
                "Os impostos que a empresa cobra ou paga (ex.: sales tax) e o "
                "estado onde ela tem obrigação. Se a empresa não tem vendas "
                "tributáveis no estado, pode não precisar de nenhuma alíquota — "
                "mas o estado precisa estar correto no perfil."
            ),
            "accountant": (
                "Sales/purchase tax config e tax nexus (state_code no perfil). "
                "Meridian não carrega taxas por estado: alíquotas são entrada do "
                "operador. Sem nexus, setup_taxes cria nada — intencional."
            ),
        },
        "en": {
            "title": "Taxes and nexus",
            "plain": (
                "The taxes the company charges or pays (e.g. sales tax) and the "
                "state where it has an obligation. If the company has no taxable "
                "sales in the state it may need no rate at all — but the state "
                "still needs to be correct on the profile."
            ),
            "accountant": (
                "Sales/purchase tax config and tax nexus (state_code on profile). "
                "Meridian carries no per-state rates: rates are operator-entered. "
                "Without nexus, setup_taxes creates nothing — intentional."
            ),
        },
    },
    "journals": {
        "pt": {
            "title": "Diários",
            "plain": (
                "Os 'livros' onde os lançamentos entram: vendas (faturas), compras "
                "(contas a pagar) e geral. Cada empresa precisa dos três para "
                "lançar documentos."
            ),
            "accountant": (
                "Diários de venda, compra e geral (account.journal). Sem eles não "
                "há como lançar faturas/bills nem o diário geral que a "
                "reconciliação espera. Criados por ensure_journals."
            ),
        },
        "en": {
            "title": "Journals",
            "plain": (
                "The 'books' where entries land: sales (invoices), purchases "
                "(bills) and general. Every company needs the three to post "
                "documents."
            ),
            "accountant": (
                "Sale, purchase and general journals (account.journal). Without "
                "them no invoice/bill can post, nor the general journal "
                "reconciliation expects. Created by ensure_journals."
            ),
        },
    },
    "bank": {
        "pt": {
            "title": "Contas bancárias",
            "plain": (
                "Cada conta bancária da empresa vira uma conta contábil de caixa e "
                "um diário bancário. É por onde a conciliação e os saldos bancários "
                "entram."
            ),
            "accountant": (
                "Conta asset_cash + diário bank por banco; o diário geral é "
                "garantido (poseidon_reconciliation espera por ele). Saldos iniciais "
                "ficam para o fluxo de import/conciliação."
            ),
        },
        "en": {
            "title": "Bank accounts",
            "plain": (
                "Each company bank account becomes a cash account and a bank "
                "journal. This is where reconciliation and bank balances come in."
            ),
            "accountant": (
                "asset_cash account + bank journal per bank; the general journal is "
                "guaranteed (poseidon_reconciliation expects it). Opening balances "
                "stay with the import/reconciliation flow."
            ),
        },
    },
    "chart": {
        "pt": {
            "title": "Plano de contas",
            "plain": (
                "A lista de contas que a empresa usa para registrar dinheiro. O "
                "kernel congela o plano US GAAP de referência; a empresa recebe a "
                "camada padrão automaticamente e pode aprofundar depois."
            ),
            "accountant": (
                "Plano publicado a partir do master chart (qbo.standard.account). "
                "L0 obrigatórias + camada operacional (default L1) são publicadas; "
                "L3 analítica é sob demanda e nunca em massa."
            ),
        },
        "en": {
            "title": "Chart of accounts",
            "plain": (
                "The list of accounts the company uses to record money. The kernel "
                "freezes the reference US GAAP chart; the company gets the default "
                "layer automatically and can go deeper later."
            ),
            "accountant": (
                "Chart published from the master chart (qbo.standard.account). L0 "
                "required + operational layer (default L1) are published; L3 "
                "analytic is on demand and never bulk."
            ),
        },
    },
    "kernel_layers": {
        "pt": {
            "title": "Camadas do kernel (L0–L3)",
            "plain": (
                "O plano de contas vem em camadas. L0 são as contas obrigatórias — "
                "o mínimo para fechar o balanço. L1 é o plano operacional padrão. "
                "L2 são agrupamentos derivados. L3 é a camada analítica 'a pedido': "
                "contas prontas para quem precisa de mais detalhe (ex.: contas de "
                "caixa separadas, despesas funcionais) — ative apenas o que a "
                "operação usa. Não ative as 251 de uma vez."
            ),
            "accountant": (
                "L0 required ⊂ L1; L2 derived collapse_rule mappings; L3 analytic "
                "expansion (sempre no master chart, nunca publicada em massa). "
                "Ativação L3 individual/lote, reversível, com rollup ao pai L0/L1 "
                "via poseidon_parent_account_id. activity_tag é pré-seleção, nunca "
                "gate."
            ),
        },
        "en": {
            "title": "Kernel layers (L0–L3)",
            "plain": (
                "The chart comes in layers. L0 are the required accounts — the "
                "minimum to close the balance sheet. L1 is the standard operational "
                "chart. L2 are derived groupings. L3 is the on-demand analytic "
                "layer: ready-made accounts for those who need more detail (e.g. "
                "split cash accounts, functional expenses) — activate only what the "
                "operation uses. Don't activate all 251 at once."
            ),
            "accountant": (
                "L0 required ⊂ L1; L2 derived collapse_rule mappings; L3 analytic "
                "expansion (always in the master chart, never bulk-published). L3 "
                "activation is individual/batch, reversible, with rollup to the "
                "L0/L1 parent via poseidon_parent_account_id. activity_tag is "
                "preselection, never a gate."
            ),
        },
    },
    "l3_activation": {
        "pt": {
            "title": "Dois caminhos para aprofundar",
            "plain": (
                "Precisa de uma conta que não existe? Você tem dois caminhos: (1) "
                "criar uma conta própria abaixo de uma conta do kernel — o sistema "
                "protege o kernel; ou (2) ativar uma conta analítica L3 pronta, "
                "individualmente ou em lote por atividade/área. Ativar um lote cria "
                "só aquele subconjunto — nunca as 251."
            ),
            "accountant": (
                "Caminho 1: poseidon_create_subaccount (subconta sob conta do "
                "kernel, com guardrails). Caminho 2: ativação L3 (individual "
                "poseidon_activate_l3_accounts ou lote por activity_tag / "
                "functional_group / dashboard_account_set). L3 nunca dispara "
                "fail-closed de required-L0."
            ),
        },
        "en": {
            "title": "Two ways to go deeper",
            "plain": (
                "Need an account that does not exist? Two paths: (1) create your "
                "own account under a kernel account — the system protects the "
                "kernel; or (2) activate a ready-made L3 analytic account, "
                "individually or in a batch by activity/area. A batch activates "
                "only that subset — never all 251."
            ),
            "accountant": (
                "Path 1: poseidon_create_subaccount (subaccount under a kernel "
                "account, with guardrails). Path 2: L3 activation (individual "
                "poseidon_activate_l3_accounts or batch by activity_tag / "
                "functional_group / dashboard_account_set). L3 never triggers "
                "required-L0 fail-closed."
            ),
        },
    },
    "mapping": {
        "pt": {
            "title": "Mapeamento de contas QBO",
            "plain": (
                "Quando a empresa vem do QuickBooks, cada conta do QBO precisa "
                "'casar' com uma conta do plano de contas. Veja a sugestão, entenda "
                "por que ela foi feita e confirme — ou escolha outra conta."
            ),
            "accountant": (
                "Decisões de mapeamento (poseidon.mapping.decision) apoiadas por "
                "bridge rules (qbo.account.bridge.rule). Confirme para liberar "
                "reconciliação e push. Destino L3 não ativado sugere 'Ativar L3'."
            ),
        },
        "en": {
            "title": "QBO account mapping",
            "plain": (
                "When the company comes from QuickBooks, each QBO account needs to "
                "'match' a chart account. Review the suggestion, understand why it "
                "was made, and confirm — or pick another account."
            ),
            "accountant": (
                "Mapping decisions (poseidon.mapping.decision) backed by bridge "
                "rules (qbo.account.bridge.rule). Confirm to unlock reconciliation "
                "and push. An unactivated L3 destination suggests 'Activate L3'."
            ),
        },
    },
    "data_import": {
        "pt": {
            "title": "Importação de dados",
            "plain": (
                "Trazer os dados históricos (saldos iniciais, lançamentos, contas "
                "do QBO) para dentro do sistema. Conecte o QuickBooks ou importe "
                "manualmente."
            ),
            "accountant": (
                "Pull via qbo.company.mapping (pull_only) ou import manual. "
                "Saldos iniciais pertencem ao fluxo de import/reconciliação, não ao "
                "onboarding silencioso."
            ),
        },
        "en": {
            "title": "Data import",
            "plain": (
                "Bring historical data (opening balances, entries, QBO accounts) "
                "into the system. Connect QuickBooks or import manually."
            ),
            "accountant": (
                "Pull via qbo.company.mapping (pull_only) or manual import. Opening "
                "balances belong to the import/reconciliation flow, not silent "
                "onboarding."
            ),
        },
    },
    "team": {
        "pt": {
            "title": "Equipe",
            "plain": (
                "As pessoas que vão usar o sistema. Convide colegas e defina o "
                "papel de cada um (gerente, contador, operacional)."
            ),
            "accountant": (
                "Usuários internos com papéis Meridian (gerente/contador/operacional). "
                "Separation of duties exige mais de um usuário ativo."
            ),
        },
        "en": {
            "title": "Team",
            "plain": (
                "The people who will use the system. Invite colleagues and set "
                "each one's role (manager, accountant, operational)."
            ),
            "accountant": (
                "Internal users with Meridian roles (manager/accountant/operational). "
                "Separation of duties requires more than one active user."
            ),
        },
    },
}

# Short imperative label for the single next action, per setup-gap key.
NEXT_ACTION_LABELS = {
    "jurisdiction": {"pt": "Preencher endereço e estado da empresa", "en": "Fill company address and state"},
    "accounting_method": {"pt": "Definir o método contábil (caixa ou competência)", "en": "Set the accounting method (cash or accrual)"},
    "fiscal_periods": {"pt": "Gerar os períodos fiscais", "en": "Generate the fiscal periods"},
    "journals": {"pt": "Criar os diários obrigatórios", "en": "Create the required journals"},
    "taxes": {"pt": "Configurar impostos e estado de nexus", "en": "Configure taxes and nexus state"},
    "bank": {"pt": "Cadastrar as contas bancárias", "en": "Set up the bank accounts"},
    "l3_activation": {
        "pt": "Ativar contas analíticas L3 sob demanda",
        "en": "Activate L3 analytic accounts on demand",
    },
    "kernel_layers": {
        "pt": "Explorar as camadas do plano de contas",
        "en": "Explore the chart of accounts layers",
    },
}

# Deterministic order of the "próxima ação única" coach: the first unresolved
# setup gap wins (block before warn), mirroring the setup flow's dependencies.
GUIDANCE_GAP_ORDER = (
    "jurisdiction",
    "accounting_method",
    "fiscal_periods",
    "journals",
    "taxes",
    "bank",
)

# Canonical L3 activity tags used by the batch preselection. Compound tags in
# the artifact (e.g. "Trade/Retail/Manufacturing") are tokenized so a batch by
# "Manufacturing" also selects its shared rows — matching _l3_batch_codes.
L3_CANONICAL_TAGS = ("Universal", "Services", "Trade/Retail", "Manufacturing")
_L3_TAG_TOKEN_MAP = {
    "Universal": "Universal",
    "Services": "Services",
    "Trade": "Trade/Retail",
    "Retail": "Trade/Retail",
    "Manufacturing": "Manufacturing",
}


class MeridianOnboarding(models.AbstractModel):
    _name = "meridian.onboarding"
    _description = "Meridian Onboarding Wizard API"

    # ------------------------------------------------------------------
    # Access + resolution helpers
    # ------------------------------------------------------------------

    @api.model
    def _ensure_operator(self):
        """Same administrative gate as meridian.saas.create_owned_company."""
        if not self.env.user.has_group("base.group_user"):
            raise AccessError(_("Only internal users can run onboarding."))
        allowed = (
            self.env.user.has_group("meridian_saas.group_meridian_saas_manager")
            or self.env.user.has_group("meridian_saas.group_meridian_saas_accountant")
            or self.env.user.has_group("account.group_account_manager")
        )
        if not allowed:
            raise AccessError(_("Only workspace managers or accountants can run onboarding."))

    @api.model
    def _resolve_company(self, company_id=None, required=True):
        company = self.env["res.company"]
        if company_id:
            company = self.env["res.company"].browse(int(company_id)).exists()
            if company and company.id not in self.env.user.company_ids.ids:
                raise AccessError(_("You do not have access to that company."))
        if not company:
            company = self.env.company if self.env.company in self.env.user.company_ids else self.env.user.company_id
        if not company and required:
            raise UserError(_("Create or select a company first."))
        return company

    @api.model
    def _tax_profile(self, company):
        return self.env["poseidon.us.tax.profile"].sudo().search(
            [("company_id", "=", company.id)], limit=1
        )

    @api.model
    def _upsert_tax_profile(self, company, vals):
        profile = self._tax_profile(company)
        if profile:
            profile.write(vals)
        else:
            profile = self.env["poseidon.us.tax.profile"].sudo().create(
                dict(vals, company_id=company.id)
            )
        return profile

    @api.model
    def _company_group(self, company):
        member = self.env["poseidon.group.member"].sudo().search(
            [("company_id", "=", company.id), ("active", "=", True)], limit=1
        )
        return member.group_id

    # ------------------------------------------------------------------
    # Status (derived, never stored)
    # ------------------------------------------------------------------

    # Substantive per-company completeness, distinct from the wizard's entry
    # gate (REQUIRED_STEPS). The gate passes on the cheapest possible evidence
    # — `taxes` on a tax-profile state_code alone, `company_info` on an
    # entity_type — because the wizard deliberately collects a 3-field row per
    # subsidiary. These checks ask what a company actually needs to operate.
    #
    # Severity/fix vocabulary is the one qbo_bridge/services/qbo_readiness.py
    # already ships, so the two diagnoses can be merged later without a
    # translation layer. `fix="auto"` means a one-click endpoint can do it with
    # no operator input; anything needing a human decision is "manual".
    @api.model
    def get_setup_summary(self, company_ids):
        self._ensure_operator()
        allowed = set(self.env.user.company_ids.ids)
        companies = self.env["res.company"].browse(
            [cid for cid in (company_ids or []) if cid in allowed]
        ).exists()
        res = {}
        for company in companies:
            missing = []

            def add(key, severity, fix):
                missing.append({"key": key, "severity": severity, "fix": fix})

            if not company.partner_id.state_id or not company.partner_id.country_id:
                add("jurisdiction", "block", "manual")

            profile = self._tax_profile(company)
            if not profile or profile.accounting_method == "unknown":
                add("accounting_method", "block", "manual")

            if not self.env["poseidon.kernel.period"].sudo().search_count(
                [("company_id", "=", company.id)]
            ):
                add("fiscal_periods", "block", "auto")

            types = set(
                self.env["account.journal"].sudo()
                .search([("company_id", "=", company.id)])
                .mapped("type")
            )
            if not JOURNAL_TYPES.issubset(types):
                add("journals", "block", "auto")

            # Not a blocker: poseidon_us_tax carries no per-state rate data, so
            # rates are operator-entered and setup_taxes legitimately creates
            # nothing when an entity has no taxable nexus. Blocking on a tax
            # record would make the setup page unclearable for those entities.
            if not self.env["account.tax"].sudo().search_count(
                [("company_id", "=", company.id)]
            ):
                add("taxes", "warn", "manual")

            if "bank" not in types:
                add("bank", "warn", "manual")

            res[company.id] = {
                "required_complete": self.get_onboarding_status(company.id).get(
                    "required_complete", False
                ),
                "missing": missing,
            }
        return res

    @api.model
    def _ensure_qbo_configuration_manager(self):
        if not self.env.user.has_group("base.group_user"):
            raise AccessError(_("Only internal users can import QuickBooks configuration."))
        if not (
            self.env.user.has_group("qbo_bridge.group_qbo_bridge_manager")
            or self.env.user.has_group("base.group_system")
        ):
            raise AccessError(_("Only a workspace or QuickBooks manager can apply configuration."))

    @api.model
    def _qbo_mapping(self, company):
        mapping = self.env["qbo.company.mapping"].sudo().search(
            [
                ("company_id", "=", company.id),
                ("sync_enabled", "=", True),
                ("realm_id.state", "=", "connected"),
                ("realm_id.sync_mode", "=", "pull_only"),
            ],
            order="last_sync_date desc, id desc",
            limit=1,
        )
        if not mapping:
            raise UserError(_("Connect QuickBooks for this company before importing configuration."))
        return mapping

    @api.model
    def _qbo_configuration_source(self, mapping):
        try:
            return QBOApiClient(mapping.realm_id).get_configuration(), False
        except Exception as exc:
            if mapping.configuration_snapshot:
                return mapping.configuration_snapshot, _(
                    "Live QuickBooks settings were unavailable; showing the latest received snapshot."
                )
            raise UserError(_("Could not read QuickBooks company settings: %s") % exc) from exc

    @api.model
    def _infer_qbo_entity_type(self, legal_name, current):
        if current and current != "unknown":
            return current, "high", "Meridian current profile"
        normalized = re.sub(r"[^A-Z0-9]+", " ", (legal_name or "").upper()).strip()
        tokens = set(normalized.split())
        if "LLC" in tokens:
            return "llc", "medium", "QBO legal-name suffix"
        if tokens.intersection({"INC", "CORP", "CORPORATION"}):
            return "c_corp", "medium", "QBO legal-name suffix"
        if tokens.intersection({"LP", "LLP", "PARTNERSHIP"}):
            return "partnership", "medium", "QBO legal-name suffix"
        return "unknown", "low", "Manual confirmation required"

    @api.model
    def _qbo_month_number(self, value):
        text = str(value or "").strip().lower()
        for number in range(1, 13):
            if text in {str(number), month_name[number].lower(), month_name[number][:3].lower()}:
                return number
        return 0

    @api.model
    def preview_qbo_configuration(self, company_id):
        """Build an editable proposal; no company configuration is changed."""
        self._ensure_qbo_configuration_manager()
        company = self._resolve_company(company_id)
        mapping = self._qbo_mapping(company)
        snapshot, warning = self._qbo_configuration_source(mapping)
        info = snapshot.get("company_info") or {}
        preferences = snapshot.get("preferences") or {}
        accounting_prefs = preferences.get("AccountingInfoPrefs") or {}
        tax_prefs = preferences.get("TaxPrefs") or {}
        address = info.get("LegalAddr") or info.get("CompanyAddr") or {}
        profile = self._tax_profile(company)

        def contact(value, *keys):
            if isinstance(value, dict):
                return next((str(value.get(key) or "").strip() for key in keys if value.get(key)), "")
            return str(value or "").strip()

        legal_name = str(info.get("LegalName") or info.get("CompanyName") or company.name or "").strip()
        entity_type, entity_confidence, entity_source = self._infer_qbo_entity_type(
            legal_name,
            profile.entity_type if profile else "unknown",
        )
        accounting_method = (
            profile.accounting_method
            if profile and profile.accounting_method != "unknown"
            else "accrual"
        )
        accounting_is_current = bool(profile and profile.accounting_method != "unknown")
        fiscal_start = self._qbo_month_number(
            info.get("FiscalYearStartMonth") or accounting_prefs.get("FirstMonthOfFiscalYear")
        )
        fiscal_end_month = 12 if fiscal_start == 1 else fiscal_start - 1 if fiscal_start else int(
            company.fiscalyear_last_month or 12
        )
        fiscal_end_day = monthrange(fields.Date.today().year, fiscal_end_month)[1]
        tax_regime = profile.tax_regime if profile else "unknown"
        if tax_regime == "unknown" and entity_type in {"s_corp", "partnership", "sole_proprietor"}:
            tax_regime = "pass_through"
        elif tax_regime == "unknown" and entity_type == "c_corp":
            tax_regime = "c_corp"

        qbo_state_code = str(address.get("CountrySubDivisionCode") or "").upper()
        values = {
            "legal_name": legal_name,
            "ein": profile.federal_ein if profile and profile.federal_ein else company.vat or "",
            "address": str(address.get("Line1") or company.street or "").strip(),
            "city": str(address.get("City") or company.city or "").strip(),
            "zip": str(address.get("PostalCode") or company.zip or "").strip(),
            "phone": contact(info.get("PrimaryPhone"), "FreeFormNumber") or company.phone or "",
            "email": contact(info.get("Email"), "Address") or company.email or "",
            "website": contact(info.get("WebAddr"), "URI", "Address") or company.website or "",
            "state_code": qbo_state_code or str((profile.state_code if profile else "") or "").upper(),
            "entity_type": entity_type,
            "accounting_method": accounting_method,
            "tax_regime": tax_regime,
            "fiscalyear_last_month": fiscal_end_month,
            "fiscalyear_last_day": fiscal_end_day,
        }
        evidence = {
            "legal_name": {"source": "QBO CompanyInfo.LegalName", "confidence": "high"},
            "address": {"source": "QBO CompanyInfo.LegalAddr", "confidence": "high"},
            "city": {"source": "QBO CompanyInfo.LegalAddr", "confidence": "high"},
            "zip": {"source": "QBO CompanyInfo.LegalAddr", "confidence": "high"},
            "phone": {"source": "QBO CompanyInfo.PrimaryPhone", "confidence": "high"},
            "email": {"source": "QBO CompanyInfo.Email", "confidence": "high"},
            "website": {"source": "QBO CompanyInfo.WebAddr", "confidence": "high"},
            "state_code": {
                "source": "QBO legal address" if qbo_state_code else "Meridian current profile",
                "confidence": "medium" if qbo_state_code else "high",
            },
            "entity_type": {"source": entity_source, "confidence": entity_confidence},
            "accounting_method": {
                "source": "Meridian current profile" if accounting_is_current else "Meridian US GAAP preset",
                "confidence": "high" if accounting_is_current else "low",
            },
            "tax_regime": {"source": "Entity-type preset", "confidence": "medium" if tax_regime != "unknown" else "low"},
            "fiscalyear_last_month": {"source": "QBO fiscal-year preference", "confidence": "high" if fiscal_start else "medium"},
            "fiscalyear_last_day": {"source": "QBO fiscal-year preference", "confidence": "high" if fiscal_start else "medium"},
        }
        journals = self.env["account.journal"].sudo().search([("company_id", "=", company.id)])
        journal_names = {name.lower() for name in journals.filtered(lambda row: row.type == "bank").mapped("name")}
        bank_accounts = []
        for account in snapshot.get("bank_accounts") or []:
            name = str(account.get("Name") or "Bank account").strip()
            bank_accounts.append(
                {
                    "qbo_id": str(account.get("Id") or ""),
                    "name": name,
                    "journal_name": name,
                    "account_code": str(account.get("AcctNum") or "").strip(),
                    "subtype": str(account.get("AccountSubType") or ""),
                    "selected": name.lower() not in journal_names,
                }
            )

        warnings = [warning] if warning else []
        if tax_prefs.get("UsingSalesTax"):
            warnings.append(_("QuickBooks uses sales tax; confirm nexus and rates manually."))
        if entity_confidence != "high":
            warnings.append(_("Confirm the legal entity type before applying the proposal."))
        if qbo_state_code:
            warnings.append(_("Confirm that the QBO legal-address state is also the incorporation jurisdiction."))
        return {
            "company_id": company.id,
            "company_name": company.name,
            "realm_id": mapping.realm_id.realm_id,
            "received_at": fields.Datetime.to_string(mapping.configuration_synced_at) if mapping.configuration_synced_at else False,
            "values": values,
            "evidence": evidence,
            "bank_accounts": bank_accounts,
            "actions": {
                "ensure_journals": not JOURNAL_TYPES.issubset(set(journals.mapped("type"))),
                "create_fiscal_periods": not self.env["poseidon.kernel.period"].sudo().search_count(
                    [("company_id", "=", company.id)]
                ),
            },
            "warnings": warnings,
        }

    @api.model
    def apply_qbo_configuration(self, company_id, payload):
        """Apply a confirmed subset of the latest QBO-backed proposal."""
        self._ensure_qbo_configuration_manager()
        if not payload or payload.get("confirmed") is not True:
            raise UserError(_("Confirm the QuickBooks configuration preview before applying it."))
        company = self._resolve_company(company_id)
        mapping = self._qbo_mapping(company)
        proposal = self.preview_qbo_configuration(company.id)
        selected = set(payload.get("selected_fields") or []) & QBO_CONFIGURATION_FIELDS
        submitted = payload.get("values") or {}
        values = {key: submitted.get(key, proposal["values"].get(key)) for key in selected}

        for key, allowed in (
            ("entity_type", ENTITY_TYPES),
            ("accounting_method", ACCOUNTING_METHODS),
            ("tax_regime", TAX_REGIMES),
        ):
            if key in values and values[key] not in allowed:
                raise UserError(_("Invalid value for %s.") % key)
        state_code = str(values.get("state_code") or "").strip().upper()
        if "state_code" in values and state_code and not self.env["res.country.state"].search_count(
            [("code", "=", state_code), ("country_id.code", "=", "US")]
        ):
            raise UserError(_("Choose a valid two-letter US state."))
        if "state_code" in values:
            values["state_code"] = state_code
        for key, maximum in (("fiscalyear_last_month", 12), ("fiscalyear_last_day", 31)):
            if key in values:
                values[key] = int(values[key] or 0)
                if values[key] < 1 or values[key] > maximum:
                    raise UserError(_("Invalid value for %s.") % key)
        for key in QBO_CONFIGURATION_FIELDS - {"fiscalyear_last_month", "fiscalyear_last_day"}:
            if key in values and isinstance(values[key], str):
                values[key] = values[key].strip()[:255]

        company_payload = dict(values)
        if "state_code" in company_payload:
            company_payload["state_of_incorporation"] = company_payload.pop("state_code")
        tax_regime = company_payload.pop("tax_regime", None)
        if company_payload:
            self.save_company_info(company.id, company_payload)
        if tax_regime:
            self._upsert_tax_profile(company, {"tax_regime": tax_regime})

        applied = sorted(selected)
        actions = payload.get("actions") or {}
        if actions.get("ensure_journals"):
            self.ensure_journals(company.id)
            applied.append("journals")
        if actions.get("create_fiscal_periods") and not self.env["poseidon.kernel.period"].sudo().search_count(
            [("company_id", "=", company.id)]
        ):
            self.generate_fiscal_periods(company.id, fields.Date.today().year, "monthly")
            applied.append("fiscal_periods")

        proposed_banks = {row["qbo_id"]: row for row in proposal["bank_accounts"] if row["qbo_id"]}
        bank_lines = []
        for row in payload.get("bank_accounts") or []:
            source = proposed_banks.get(str(row.get("qbo_id") or ""))
            if not source or row.get("selected") is not True:
                continue
            bank_lines.append(
                {
                    "name": str(row.get("name") or source["name"]).strip()[:255],
                    "journal_name": str(row.get("journal_name") or source["journal_name"]).strip()[:255],
                    "account_code": str(row.get("account_code") or source["account_code"]).strip()[:64],
                }
            )
        if bank_lines:
            self.setup_banks(company.id, bank_lines)
            applied.append("banks")

        log = QboSyncLog.log(
            self.env,
            mapping,
            "mapping",
            "pull",
            "success",
            "update",
            qbo_id=mapping.realm_id.realm_id,
            odoo_model="res.company",
            odoo_record_id=company.id,
            message=_("Applied confirmed QBO configuration: %s") % ", ".join(applied),
        )
        return {
            "status": "completed",
            "company_id": company.id,
            "applied": applied,
            "audit_ref": str(log.id),
            "setup": self.get_setup_summary([company.id]).get(company.id),
        }

    @api.model
    def ensure_journals(self, company_id):
        """Create the sale/purchase/general journals a company is missing.

        The QBO path already does this for a mapped company
        (qbo_bridge/services/qbo_prepare.py), but that is keyed to a
        qbo.company.mapping. A company with no QuickBooks connection had no way
        to get a journal at all, which left the `journals` setup gap
        unclearable.
        """
        self._ensure_operator()
        company = self._resolve_company(company_id)
        Journal = self.env["account.journal"].sudo()
        present = set(
            Journal.search([("company_id", "=", company.id)]).mapped("type")
        )
        created = []
        for journal_type, name, prefix in (
            ("sale", _("Customer Invoices"), "INV"),
            ("purchase", _("Vendor Bills"), "BIL"),
            ("general", _("General"), "GEN"),
        ):
            if journal_type in present:
                continue
            journal = Journal.create(
                {
                    "name": name,
                    "code": self._free_journal_code(company, prefix),
                    "type": journal_type,
                    "company_id": company.id,
                }
            )
            created.append(
                {"id": journal.id, "type": journal_type, "code": journal.code}
            )
        return {"status": "completed", "company_id": company.id, "created": created}

    @api.model
    def _free_journal_code(self, company, prefix):
        taken = {
            row["code"]
            for row in self.env["account.journal"].sudo().search_read(
                [("company_id", "=", company.id)], ["code"]
            )
        }
        if prefix not in taken:
            return prefix
        counter = 2
        while f"{prefix}{counter}" in taken:
            counter += 1
        return f"{prefix}{counter}"

    @api.model
    def get_onboarding_status(self, company_id=None):
        self._ensure_operator()
        company = self._resolve_company(company_id, required=False)
        kernel = self.env["poseidon.kernel.version"].get_installed_kernel_status()

        if not company:
            steps = {name: {"done": False} for name in REQUIRED_STEPS + OPTIONAL_STEPS}
            return {
                "company_id": False,
                "company_name": False,
                "group": False,
                "steps": steps,
                "required_complete": False,
                "kernel": kernel,
                "activity_catalog": self.env["poseidon.us.tax.profile"].activity_catalog(),
            }

        Account = self.env["account.account"].sudo()
        profile = self._tax_profile(company)
        group = self._company_group(company)

        period_count = self.env["poseidon.kernel.period"].sudo().search_count(
            [("company_id", "=", company.id)]
        )
        chart_count = Account.search_count(
            [("company_ids", "=", company.id), ("qbo_standard_account_id", "!=", False)]
        )
        qbo_account_count = Account.search_count(
            [("company_ids", "=", company.id), ("qbo_id", "!=", False)]
        )
        confirmed_mappings = self.env["poseidon.mapping.decision"].sudo().search_count(
            [("company_id", "=", company.id), ("state", "=", "confirmed")]
        )
        qbo_connected = bool(
            self.env["qbo.company.mapping"].sudo().search_count(
                [("company_id", "=", company.id)]
            )
        )
        move_count = self.env["account.move"].sudo().search_count(
            [("company_id", "=", company.id)]
        )
        bank_journals = self.env["account.journal"].sudo().search_count(
            [("company_id", "=", company.id), ("type", "=", "bank")]
        )
        member_count = self.env["res.users"].sudo().search_count(
            [("share", "=", False), ("active", "=", True)]
        )

        steps = {
            "entities": {
                "done": bool(company),
            },
            "company_info": {
                "done": bool(profile and profile.entity_type != "unknown"),
                "entity_type": profile.entity_type if profile else "unknown",
                "activity_tag": profile.activity_tag if profile else "unknown",
                "subject_to_sales_tax": profile.subject_to_sales_tax if profile else False,
                "subject_to_business_tax": profile.subject_to_business_tax if profile else False,
                "accounting_method": profile.accounting_method if profile else "unknown",
                "fiscalyear_last_month": int(company.fiscalyear_last_month or 12),
                "fiscalyear_last_day": int(company.fiscalyear_last_day or 31),
            },
            "fiscal_periods": {
                "done": period_count > 0,
                "period_count": period_count,
            },
            "taxes": {
                "done": bool(profile and profile.state_code),
            },
            "chart": {
                "done": bool(kernel.get("installed")) and chart_count > 0,
                "account_count": chart_count,
            },
            "data_import": {
                "done": qbo_connected or move_count > 0,
                "qbo_connected": qbo_connected,
                "move_count": move_count,
            },
            "mapping": {
                "done": qbo_account_count > 0 and confirmed_mappings >= qbo_account_count,
                "qbo_account_count": qbo_account_count,
                "confirmed_count": confirmed_mappings,
            },
            "banks": {
                "done": bank_journals > 0,
                "bank_journal_count": bank_journals,
            },
            "team": {
                "done": member_count > 1,
                "member_count": member_count,
            },
        }
        return {
            "company_id": company.id,
            "company_name": company.name,
            "group": {
                "id": group.id,
                "name": group.name,
                "members": [
                    {
                        "company_id": m.company_id.id,
                        "company_name": m.company_id.name,
                        "role": m.role,
                    }
                    for m in group.member_ids.filtered("active")
                ],
            }
            if group
            else False,
            "steps": steps,
            "required_complete": all(steps[name]["done"] for name in REQUIRED_STEPS),
            "kernel": kernel,
            "activity_catalog": self.env["poseidon.us.tax.profile"].activity_catalog(),
        }

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @api.model
    def _generate_avatar_base64(self, name):
        import base64
        from PIL import Image, ImageDraw, ImageFont
        import io
        
        name = (name or "A").strip()
        initial = name[0].upper() if name else "A"
        
        img = Image.new("RGB", (512, 512), color=(44, 62, 80))
        draw = ImageDraw.Draw(img)
        
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 256)
        except Exception:
            font = ImageFont.load_default()
            
        left, top, right, bottom = draw.textbbox((0, 0), initial, font=font)
        text_width = right - left
        text_height = bottom - top
        x = (512 - text_width) / 2
        y = (512 - text_height) / 2 - top
        
        draw.text((x, y), initial, fill=(255, 255, 255), font=font)
        
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("utf-8")

    # ------------------------------------------------------------------
    # Step 1 — entities (solo / umbrella)
    # ------------------------------------------------------------------

    @api.model
    def _ensure_company(self, name, exclude_id=None):
        """Find or reuse an existing company by name (case-insensitive), or create one idempotently.

        ``exclude_id`` prevents the holding company from matching as a
        subsidiary when names accidentally overlap — e.g. after
        ``setup_entities`` renames company 1 to the holding name.
        """
        name = (name or "").strip()
        if not name:
            raise UserError(_("Company name is required."))

        # 1. Search in user's allowed companies (case-insensitive)
        existing_user_company = self.env.user.company_ids.filtered(
            lambda c: c.name.strip().lower() == name.lower() and (exclude_id is None or c.id != exclude_id)
        )
        if existing_user_company:
            return existing_user_company[0]

        # 2. Search database-wide for existing res.company with matching name (case-insensitive)
        domain = [("name", "=ilike", name)]
        if exclude_id:
            domain.append(("id", "!=", exclude_id))
        existing_db_company = self.env["res.company"].sudo().search(domain, limit=1)
        if existing_db_company:
            self.env["meridian.saas"]._batch_grant_company_access(self.env.user, [existing_db_company.id])
            return existing_db_company

        # 3. Create new company if it doesn't exist anywhere in DB
        result = self.env["meridian.saas"].with_context(defer_company_access=True).create_owned_company(name)
        if result.get("status") != "completed":
            raise UserError(result.get("message") or _("Company creation failed."))
        return self.env["res.company"].browse(result["company_id"])


    @api.model
    def _group_code(self, name):
        code = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
        return code[:32] or "group"

    @api.model
    def _ensure_group_member(self, group, company, role):
        Member = self.env["poseidon.group.member"].sudo()
        member = Member.search(
            [("group_id", "=", group.id), ("company_id", "=", company.id)], limit=1
        )
        if member:
            if not member.active:
                member.write({"active": True})
            return member
        return Member.create(
            {"group_id": group.id, "company_id": company.id, "role": role}
        )

    @api.model
    def setup_entities(self, payload):
        """Solo: one company. Umbrella: holding + subsidiaries + company group.

        For umbrella/umbrella_logical the subsidiaries are created with
        ``defer_company_access=True`` so ``create_owned_company`` does NOT write
        ``company_ids`` on every iteration (which would invalidate the Odoo
        session cookie N times and log the operator out).  A single batched
        write via ``_batch_grant_company_access`` is issued once all companies
        are ready.
        """
        self._ensure_operator()
        payload = payload or {}
        company_type = payload.get("company_type")
        
        # Provisioning already created the tenant's base company (base.main_company,
        # renamed to the workspace display name).  Bind the Solo/Holding company to
        # THAT record deterministically — not to self.env.company, which follows the
        # session's active cids and can drift to another company, spawning a second,
        # undeletable holding.  Fall back to env.company only if the ref is missing.
        # ponytail: base.main_company is the sole provisioned company at onboarding
        # time in the dedicated-db-per-tenant model; revisit if shared-db tenants land.
        provisioned = self.env.ref("base.main_company", raise_if_not_found=False)
        primary_company = (provisioned or self.env.company).sudo()

        if company_type == "solo":
            name = (payload.get("company_name") or "").strip()
            if not name:
                raise UserError(_("Company name is required."))
            if primary_company.name != name:
                primary_company.sudo().write({"name": name})
            return {"status": "completed", "company_id": primary_company.id, "group_id": False}

        if company_type not in ("umbrella", "umbrella_logical"):
            raise UserError(_("company_type must be 'solo', 'umbrella', or 'umbrella_logical'."))

        name = (payload.get("holding_name") or "").strip()
        if not name:
            raise UserError(_("Holding name is required."))
        if primary_company.name != name:
            primary_company.sudo().write({"name": name})
            
        holding = primary_company

        # Collect all subsidiary companies.  _ensure_company is called with
        # exclude_id=holding.id so the freshly-renamed holding is never matched
        # as a subsidiary by name, and with defer_company_access=True (injected
        # inside _ensure_company) so no per-company session write happens here.
        subsidiaries = []
        new_company_ids = []  # IDs of companies created in this call (need batched grant)
        for line in payload.get("subsidiaries") or []:
            sub = self._ensure_company(line.get("name"), exclude_id=holding.id)
            # Track freshly-created companies (not already in user.company_ids)
            if sub.id not in self.env.user.company_ids.ids:
                new_company_ids.append(sub.id)

            state_code = (line.get("state_code") or "").strip().upper()
            entity_type = (line.get("entity_type") or "").strip()
            ein = (line.get("ein") or "").strip()
            
            tax_profile_vals = {}
            if state_code:
                tax_profile_vals["state_code"] = state_code
            if entity_type:
                tax_profile_vals["entity_type"] = entity_type
            if ein:
                tax_profile_vals["federal_ein"] = ein
            if tax_profile_vals:
                self._upsert_tax_profile(sub, tax_profile_vals)
                
            sub_vals = {}
            if ein:
                sub_vals["vat"] = ein
            if not sub.country_id:
                us = self.env.ref("base.us", raise_if_not_found=False)
                if us:
                    sub_vals["country_id"] = us.id
                    
            if state_code:
                state = self.env["res.country.state"].search(
                    [("code", "=", state_code), ("country_id.code", "=", "US")], limit=1
                )
                if state:
                    sub_vals["state_id"] = state.id
                    
            if line.get("logoBase64"):
                sub_vals["logo"] = line.get("logoBase64")
            elif line.get("useInitials"):
                sub_vals["logo"] = self._generate_avatar_base64(line.get("name"))
                
            if sub_vals:
                sub.sudo().write(sub_vals)
                
            subsidiaries.append(sub)
        if not subsidiaries:
            raise UserError(_("An umbrella setup needs at least one subsidiary."))

        # Single batched company_ids write — avoids N session-cookie
        # invalidations that would log the operator out.
        if new_company_ids:
            self.env["meridian.saas"]._batch_grant_company_access(
                self.env.user, new_company_ids
            )

        group = self._company_group(holding)
        if not group:
            group = self.env["poseidon.company.group"].sudo().create(
                {
                    "name": holding.name,
                    "code": self._group_code(holding.name),
                    "currency_id": holding.currency_id.id,
                }
            )
        self._ensure_group_member(group, holding, "holding")
        for sub in subsidiaries:
            self._ensure_group_member(group, sub, "subsidiary")

        return {
            "status": "completed",
            "company_id": holding.id,
            "group_id": group.id,
            "company_ids": [holding.id] + [s.id for s in subsidiaries],
        }

    # ------------------------------------------------------------------
    # Step 2 — company info + tax profile
    # ------------------------------------------------------------------

    @api.model
    def save_company_info(self, company_id, payload):
        self._ensure_operator()
        company = self._resolve_company(company_id)
        payload = payload or {}

        company_vals = {}
        ein = (payload.get("ein") or "").strip()
        if ein:
            company_vals["vat"] = ein
        for src, dest in (
            ("address", "street"),
            ("city", "city"),
            ("zip", "zip"),
            ("phone", "phone"),
            ("email", "email"),
            ("website", "website"),
        ):
            value = (payload.get(src) or "").strip()
            if value:
                company_vals[dest] = value
        if not company.country_id:
            us = self.env.ref("base.us", raise_if_not_found=False)
            if us:
                company_vals["country_id"] = us.id
        # Only accept a code that resolves to a real US state. The UI prefills
        # this field from a display value that can be a placeholder ("—") or a
        # state name; writing it unchecked would persist junk into the tax
        # profile, where state_code is what the `taxes` gate reads.
        state_code = (payload.get("state_of_incorporation") or payload.get("state_code") or "").strip().upper()
        if state_code:
            state = self.env["res.country.state"].search(
                [("code", "=", state_code), ("country_id.code", "=", "US")], limit=1
            )
            if state:
                company_vals["state_id"] = state.id
            else:
                state_code = ""
        fy_month = int(payload.get("fiscalyear_last_month") or 0)
        fy_day = int(payload.get("fiscalyear_last_day") or 0)
        if fy_month:
            company_vals["fiscalyear_last_month"] = str(fy_month)
        if fy_day:
            company_vals["fiscalyear_last_day"] = fy_day
        if payload.get("legal_name"):
            company_vals["name"] = payload["legal_name"].strip()
            
        if payload.get("logo"):
            company_vals["logo"] = payload["logo"]
        elif payload.get("generate_initials"):
            name = payload.get("legal_name") or company.name or "A"
            company_vals["logo"] = self._generate_avatar_base64(name)

        if company_vals:
            company.sudo().write(company_vals)

        profile_vals = {}
        if ein:
            profile_vals["federal_ein"] = ein
        if state_code:
            profile_vals["state_code"] = state_code
        if payload.get("entity_type"):
            profile_vals["entity_type"] = payload["entity_type"]
        if payload.get("activity_tag"):
            profile_vals["activity_tag"] = payload["activity_tag"]
        if "subject_to_sales_tax" in payload:
            profile_vals["subject_to_sales_tax"] = payload["subject_to_sales_tax"]
        if "subject_to_business_tax" in payload:
            profile_vals["subject_to_business_tax"] = payload["subject_to_business_tax"]
        if payload.get("accounting_method"):
            profile_vals["accounting_method"] = payload["accounting_method"]
        if fy_month:
            profile_vals["fiscal_year_end_month"] = fy_month
        if fy_day:
            profile_vals["fiscal_year_end_day"] = fy_day
        profile = self._upsert_tax_profile(company, profile_vals or {"entity_type": "unknown"})

        return {"status": "completed", "company_id": company.id, "profile_id": profile.id}

    # ------------------------------------------------------------------
    # Step 3 — fiscal periods (all created open; closing/locking stays a
    # deliberate post-onboarding act so historical imports are not blocked)
    # ------------------------------------------------------------------

    def _active_subsidiaries(self, company):
        """res.company recordset of active subsidiary members in company's group,
        excluding the company itself. Empty if not grouped."""
        group = self._company_group(company)
        if not group or group.holding_company_id.id != company.id:
            return self.env["res.company"]
        members = group.member_ids.filtered(lambda m: m.active and m.role == "subsidiary")
        return members.mapped("company_id").filtered(lambda c: c.id != company.id)

    @api.model
    def _generate_periods_for_company(self, company, year, period_type="monthly"):
        fy_month = int(company.fiscalyear_last_month or 12)
        fy_day = int(company.fiscalyear_last_day or 31)
        fy_end = date(year, fy_month, min(fy_day, monthrange(year, fy_month)[1]))
        fy_start = fy_end - relativedelta(years=1) + timedelta(days=1)

        months_per_period = 1 if period_type == "monthly" else 3
        Period = self.env["poseidon.kernel.period"]
        created, skipped = [], []
        cursor = fy_start
        index = 0
        while cursor <= fy_end:
            index += 1
            next_start = cursor + relativedelta(months=months_per_period)
            period_end = min(next_start - timedelta(days=1), fy_end)
            if period_type == "monthly":
                name = cursor.strftime("%Y-%m")
            else:
                name = f"FY{year} Q{index}"
            overlap = Period.search_count(
                [
                    ("company_id", "=", company.id),
                    ("date_from", "<=", period_end),
                    ("date_to", ">=", cursor),
                ]
            )
            if overlap:
                skipped.append(name)
            else:
                period = Period.create(
                    {
                        "name": name,
                        "company_id": company.id,
                        "date_from": cursor,
                        "date_to": period_end,
                    }
                )
                created.append({"id": period.id, "name": name})
            cursor = next_start
        return {"created": created, "skipped": skipped}

    @api.model
    def generate_fiscal_periods(self, company_id, year, period_type="monthly"):
        self._ensure_operator()
        company = self._resolve_company(company_id)
        year = int(year)
        if period_type not in ("monthly", "quarterly"):
            raise UserError(_("period_type must be 'monthly' or 'quarterly'."))

        r = self._generate_periods_for_company(company, year, period_type)
        
        sub_results = []
        for sub in self._active_subsidiaries(company):
            try:
                with self.env.cr.savepoint():
                    sub_r = self.sudo()._generate_periods_for_company(sub, year, period_type)
                sub_results.append({"company_id": sub.id, **sub_r})
            except Exception as e:
                _logger.exception("onboarding fan-out failed for company %s", sub.id)
                sub_results.append({"company_id": sub.id, "error": str(e)})

        return {
            "status": "completed",
            "company_id": company.id,
            "created": r["created"],
            "skipped": r["skipped"],
            "subsidiaries": sub_results,
        }

    # ------------------------------------------------------------------
    # Step 4 — taxes. No per-state rate data exists in the suite
    # (poseidon_us_tax is planning-only), so rates are operator-entered.
    # ------------------------------------------------------------------

    @api.model
    def _ensure_percent_tax(self, company, tax_use, rate, state_code):
        Tax = self.env["account.tax"]
        tax = Tax.search(
            [
                ("company_id", "=", company.id),
                ("type_tax_use", "=", tax_use),
                ("amount_type", "=", "percent"),
                ("amount", "=", rate),
            ],
            limit=1,
        )
        if tax:
            return tax
        label = _("Sales Tax") if tax_use == "sale" else _("Purchase Tax")
        suffix = f" ({state_code})" if state_code else ""
        # account.tax country_id and tax_group_id are NOT NULL in Odoo 19, and
        # their defaults come from a loaded chart template — which Poseidon
        # companies do not use (kernel JSON instead). Provide both explicitly.
        country = company.country_id or self.env.ref("base.us")
        TaxGroup = self.env["account.tax.group"].sudo()
        tax_group = TaxGroup.search([("company_id", "=", company.id)], limit=1)
        if not tax_group:
            tax_group = TaxGroup.create(
                {"name": _("Taxes"), "company_id": company.id, "country_id": country.id}
            )
        return Tax.create(
            {
                "name": f"{label} {rate:g}%{suffix}",
                "type_tax_use": tax_use,
                "amount_type": "percent",
                "amount": rate,
                "company_id": company.id,
                "country_id": country.id,
                "tax_group_id": tax_group.id,
            }
        )

    @api.model
    def setup_taxes(self, company_id, payload):
        self._ensure_operator()
        company = self._resolve_company(company_id)
        payload = payload or {}

        state_code = (payload.get("state_code") or "").strip().upper()
        if not state_code:
            raise UserError(_("Select the company's tax state."))
        if not self.env["res.country.state"].search_count(
            [("code", "=", state_code), ("country_id.code", "=", "US")]
        ):
            raise UserError(_("%s is not a US state code.") % state_code)
        # Persist the nexus on the partner too: every setup card reads the
        # jurisdiction from the partner, so a profile-only write made the
        # selection look unsaved (empty select on reload, gap never closes).
        self.save_company_info(company.id, {"state_of_incorporation": state_code})

        taxes = []
        if payload.get("enable_sales_tax"):
            rate = float(payload.get("sales_tax_rate") or 0)
            if rate <= 0:
                raise UserError(_("Enter a sales tax rate greater than zero."))
            taxes.append(self._ensure_percent_tax(company, "sale", rate, state_code))
        if payload.get("enable_purchase_tax"):
            rate = float(payload.get("purchase_tax_rate") or 0)
            if rate <= 0:
                raise UserError(_("Enter a purchase tax rate greater than zero."))
            taxes.append(self._ensure_percent_tax(company, "purchase", rate, state_code))

        return {
            "status": "completed",
            "company_id": company.id,
            "tax_ids": [tax.id for tax in taxes],
        }

    # ------------------------------------------------------------------
    # Step 5 — chart of accounts (publish frozen kernel to the company)
    # ------------------------------------------------------------------

    @api.model
    def ensure_chart(self, company_id):
        self._ensure_operator()
        company = self._resolve_company(company_id)
        publish = self.env["account.chart.template"].sudo().poseidon_publish_missing_standard_accounts(
            company.id
        )
        l3 = self._seed_l3_for_company(company)
        
        sub_results = []
        for sub in self._active_subsidiaries(company):
            try:
                with self.env.cr.savepoint():
                    r = self.env["account.chart.template"].sudo().poseidon_publish_missing_standard_accounts(sub.id)
                    sub_l3 = self._seed_l3_for_company(sub)
                sub_results.append({"company_id": sub.id, "publish": r, "l3": sub_l3})
            except Exception as e:
                _logger.exception("onboarding fan-out failed for company %s", sub.id)
                sub_results.append({"company_id": sub.id, "error": str(e)})

        kernel = self.env["poseidon.kernel.version"].get_installed_kernel_status()
        accounts = self.env["account.account"].sudo().search_read(
            [("company_ids", "=", company.id), ("qbo_standard_account_id", "!=", False)],
            ["code", "name", "account_type"],
            order="code",
        )
        return {
            "status": "completed",
            "company_id": company.id,
            "publish": publish,
            "l3_activated": l3.get("activated", 0),
            "subsidiaries": sub_results,
            "kernel": kernel,
            "accounts": accounts,
        }

    @api.model
    def _seed_l3_for_company(self, company):
        """Seed the L3 analytic accounts for the company's activity profile.

        L3 is what gives the company its analytic accounts — the chart is not
        operationally complete without them and the visual connector needs them
        as mapping targets. The profile activity_tag preselection keeps the seed
        relevant to the business (services/trade/manufacturing-only L3 stays
        out for a Universal company); without a profile every L3 is seeded.
        Reuses the L3 activation path so each account gets its rollup parent
        link. Idempotent: already-enabled L3 is skipped.
        """
        Chart = self.env["account.chart.template"].sudo()
        Std = self.env["qbo.standard.account"].sudo()
        Std._ensure_l3_master_chart_imported()
        enabled_ids = (
            self.env["account.account"]
            .sudo()
            .search(
                [("company_ids", "=", company.id), ("qbo_standard_account_id", "!=", False)]
            )
            .mapped("qbo_standard_account_id")
            .ids
        )
        domain = [
            ("entry_type", "=", "detail"),
            ("kernel_layer", "=", "L3"),
            ("active", "=", True),
            ("id", "not in", enabled_ids or [0]),
        ]
        codes = Chart._l3_batch_codes(company, "activity_tag", False)
        if codes:
            domain.append(("code", "in", codes))
        l3 = Std.search(domain, order="code")
        if not l3:
            return {"activated": 0, "accounts": []}
        return Chart.poseidon_activate_l3_accounts(company.id, l3.mapped("code"))

    @api.model
    def list_suggested_accounts(self, company_id, parent_code=False):
        """Master detail accounts not yet enabled for the company — the L1
        additions plus the L3 analytic accounts for the company's activity
        profile, offered as a suggestion after L0 was auto-seeded at creation.
        Accepting the step seeds exactly the same list.

        When `parent_code` is given, only L3 accounts rolling up into that
        parent are returned (the "add a subaccount" smart options), skipping
        the activity-tag preselection — a parent choice is more specific than
        the batch default, and the L3 artifact's rollup is the authority.
        """
        self._ensure_operator()
        company = self._resolve_company(company_id)
        Std = self.env["qbo.standard.account"].sudo()
        Chart = self.env["account.chart.template"].sudo()
        Std._ensure_master_chart_imported()
        Account = self.env["account.account"].sudo()
        enabled = Account.search(
            [("company_ids", "=", company.id), ("qbo_standard_account_id", "!=", False)]
        )
        enabled_ids = enabled.mapped("qbo_standard_account_id").ids
        suggested = Std.search(
            [
                ("entry_type", "=", "detail"),
                ("kernel_layer", "!=", False),
                ("id", "not in", enabled_ids or [0]),
            ],
            order="code",
        )
        parent_code = str(parent_code or "").strip()
        if parent_code:
            suggested = suggested.filtered(
                lambda s: s.rollup_to_l1_parent == parent_code
            )
        else:
            l3_codes = set(Chart._l3_batch_codes(company, "activity_tag", False))
            if l3_codes:
                suggested = suggested.filtered(
                    lambda s: s.kernel_layer != "L3" or s.code in l3_codes
                )
        return {
            "company_id": company.id,
            "accounts": [
                {
                    "code": s.code,
                    "name": s.description,
                    "category": s.category,
                    "kernel_required": s.kernel_required,
                    "kernel_layer": s.kernel_layer,
                    "rollup_to_l1_parent": s.rollup_to_l1_parent,
                }
                for s in suggested
            ],
        }

    # ------------------------------------------------------------------
    # Step 7 — QBO -> kernel mapping (minimal viable: suggest + confirm;
    # group propagation stays in the existing propagation wizard)
    # ------------------------------------------------------------------

    @api.model
    def list_mapping_candidates(self, company_id):
        self._ensure_operator()
        company = self._resolve_company(company_id)
        Account = self.env["account.account"].sudo()
        Decision = self.env["poseidon.mapping.decision"].sudo()
        Rule = self.env["qbo.account.bridge.rule"].sudo()
        StandardAccount = self.env["qbo.standard.account"].sudo()

        sources = Account.search(
            [("company_ids", "=", company.id), ("qbo_id", "!=", False)], order="code"
        )
        decisions = {
            d.qbo_id: d
            for d in Decision.search([("company_id", "=", company.id)])
        }

        rows = []
        for account in sources:
            qbo_record = {
                "AcctNum": account.qbo_source_account_number or account.code,
                "Name": account.qbo_source_name or account.name,
                "AccountType": account.qbo_source_account_type,
                "AccountSubType": account.qbo_source_account_subtype,
            }
            rule = Rule.match_qbo_record(qbo_record)
            destination = Account.browse()
            if rule:
                destination = Account.search(
                    [
                        ("company_ids", "=", company.id),
                        ("code", "=", rule.canonical_code),
                        ("id", "!=", account.id),
                    ],
                    limit=1,
                )
            decision = decisions.get(account.qbo_id)
            guide = account_type_guide_labels(
                rule.canonical_account_type if rule else False, lang="pt"
            )
            suggest_l3 = False
            l3_code = False
            if rule and not destination:
                l3_standard = StandardAccount.search(
                    [
                        ("code", "=", rule.canonical_code),
                        ("entry_type", "=", "detail"),
                        ("kernel_layer", "=", "L3"),
                        ("active", "=", True),
                    ],
                    limit=1,
                )
                if l3_standard and not Account.search(
                    [
                        ("company_ids", "=", company.id),
                        ("qbo_standard_account_id", "=", l3_standard.id),
                    ],
                    limit=1,
                ):
                    suggest_l3 = True
                    l3_code = l3_standard.code
            rows.append(
                {
                    "source_account_id": account.id,
                    "code": account.code,
                    "name": account.name,
                    "qbo_id": account.qbo_id,
                    "decision_state": decision.state if decision else False,
                    "destination_account_id": decision.destination_account_id.id
                    if decision and decision.destination_account_id
                    else destination.id or False,
                    "suggested_rule_id": rule.id or False,
                    "suggested_code": rule.canonical_code if rule else False,
                    "suggested_name": rule.canonical_name if rule else False,
                    "match_reason": mapping_match_reason(rule, qbo_record, lang="pt"),
                    "normal_balance": guide.get("normal_balance") or False,
                    "normal_balance_label": guide.get("normal_balance_label") or False,
                    "category": guide.get("category") or False,
                    "type_label": guide.get("type_label") or False,
                    "statement": guide.get("statement") or False,
                    "suggest_l3": suggest_l3,
                    "l3_code": l3_code or False,
                }
            )
        return {"company_id": company.id, "candidates": rows}

    @api.model
    def confirm_mapping(self, company_id, source_account_id, destination_account_id, bridge_rule_id=None):
        self._ensure_operator()
        company = self._resolve_company(company_id)
        if not bridge_rule_id:
            source = self.env["account.account"].sudo().browse(int(source_account_id)).exists()
            if not source:
                raise UserError(_("QBO source account not found."))
            rule = self.env["qbo.account.bridge.rule"].sudo().match_qbo_record(
                {
                    "AcctNum": source.qbo_source_account_number or source.code,
                    "Name": source.qbo_source_name or source.name,
                    "AccountType": source.qbo_source_account_type,
                    "AccountSubType": source.qbo_source_account_subtype,
                }
            )
            bridge_rule_id = rule.id
        if not bridge_rule_id:
            raise UserError(
                _("No bridge rule matches this account; map it from the governance console.")
            )
        decision_id = self.env["poseidon.mapping.decision"].sudo().record_mapping_decision(
            {
                "state": "confirmed",
                "company_id": company.id,
                "source_account_id": int(source_account_id),
                "destination_account_id": int(destination_account_id),
                "bridge_rule_id": int(bridge_rule_id),
                "reason": "Confirmed during guided onboarding.",
            }
        )
        return {"status": "completed", "decision_id": decision_id}

    # ------------------------------------------------------------------
    # Step 8 — banks: asset_cash account + bank journal per line, plus the
    # general journal poseidon_reconciliation expects to find.
    # ------------------------------------------------------------------

    @api.model
    def _ensure_general_journal(self, company):
        Journal = self.env["account.journal"].sudo()
        journal = Journal.search(
            [("company_id", "=", company.id), ("type", "=", "general")], limit=1
        )
        if journal:
            return journal
        return Journal.create(
            {"name": _("General"), "code": "GEN", "type": "general", "company_id": company.id}
        )

    @api.model
    def _next_free_account_code(self, company):
        # active_test=False is required, not defensive: core's
        # _ensure_code_is_unique checks archived accounts too, so an archived
        # account still reserves its code. Without this we would hand back a
        # code that looks free and then fail on create with a duplicate-code
        # error pointing at a record the operator cannot see.
        taken = {
            row["code"]
            for row in self.env["account.account"].sudo().with_context(active_test=False).search_read(
                [("company_ids", "=", company.id)], ["code"]
            )
        }
        counter = 10100
        while str(counter) in taken:
            counter += 10
        return str(counter)

    @api.model
    def setup_banks(self, company_id, lines):
        self._ensure_operator()
        company = self._resolve_company(company_id)
        Account = self.env["account.account"].sudo().with_company(company)
        Journal = self.env["account.journal"].sudo()

        self._ensure_general_journal(company)

        journals = []
        for line in lines or []:
            name = (line.get("name") or "").strip()
            if not name:
                raise UserError(_("Each bank account needs a name."))

            bank_account_id = line.get("bank_account_id") or False
            if bank_account_id:
                bank_account_id = int(bank_account_id)

            code = (line.get("account_code") or "").strip()
            if not code:
                code = self._next_free_account_code(company)
            account = Account.search(
                [("company_ids", "=", company.id), ("code", "=", code)], limit=1
            )
            if not account:
                account = Account.create(
                    {
                        "name": name,
                        "code": code,
                        "account_type": "asset_cash",
                        "company_ids": [(4, company.id)],
                    }
                )

            journal_name = (line.get("journal_name") or "").strip() or name
            journal_domain = [
                ("company_id", "=", company.id),
                ("type", "=", "bank"),
                ("name", "=", journal_name),
            ]
            journal = Journal
            if bank_account_id:
                journal = Journal.search(
                    [*journal_domain, ("bank_account_id", "=", bank_account_id)],
                    limit=1,
                )
                if not journal:
                    journal = Journal.search(
                        [*journal_domain, ("bank_account_id", "=", False)],
                        limit=1,
                    )
            if not journal:
                journal = Journal.search(journal_domain, limit=1)

            def _new_journal():
                journal_vals = {
                    "name": journal_name,
                    "type": "bank",
                    "code": self._next_free_journal_code(company),
                    "company_id": company.id,
                    "default_account_id": account.id,
                }
                if bank_account_id:
                    journal_vals["bank_account_id"] = bank_account_id
                return Journal.create(journal_vals)

            if not journal:
                journal = _new_journal()
            else:
                update_vals = {}
                if not bank_account_id or not journal.bank_account_id:
                    update_vals["default_account_id"] = account.id
                    if bank_account_id and not journal.bank_account_id:
                        update_vals["bank_account_id"] = bank_account_id
                if update_vals:
                    journal.write(update_vals)
                if bank_account_id and journal.bank_account_id.id != bank_account_id:
                    journal = _new_journal()
            journals.append(
                {
                    "journal_id": journal.id,
                    "journal_name": journal.name,
                    "account_id": account.id,
                    "account_code": account.code,
                    "bank_account_id": bank_account_id or False,
                }
            )
            # ponytail: initial_balance is accepted but not posted — opening
            # balances belong to the import/reconciliation flow, not a silent
            # onboarding journal entry.

        return {"status": "completed", "company_id": company.id, "journals": journals}

    @api.model
    def _next_free_journal_code(self, company):
        Journal = self.env["account.journal"].sudo()
        taken = {
            row["code"]
            for row in Journal.search_read([("company_id", "=", company.id)], ["code"])
        }
        counter = 1
        while f"BNK{counter}" in taken:
            counter += 1
        return f"BNK{counter}"

    # ------------------------------------------------------------------
    # Production guidance (didactic coach, PT/EN)
    # ------------------------------------------------------------------
    #
    # The "próxima ação única" panel: one concept at a time (Lei de Hick).
    # Gaps reuse the get_setup_summary severity vocabulary, so the two
    # diagnoses stay mergeable. Content is served on demand via
    # get_guidance_content; get_production_guidance returns the single next
    # action plus a compact kernel/L3 snapshot for the coach panel.

    @api.model
    def get_guidance_content(self, concept, pt=True):
        """Didactic content for one concept, plain language + accountant view."""
        self._ensure_operator()
        concept = (concept or "").strip().lower()
        if concept not in GUIDANCE_CONTENT:
            raise UserError(_("Unknown guidance concept '%s'.") % concept)
        lang = "pt" if pt else "en"
        content = GUIDANCE_CONTENT[concept][lang]
        return {
            "concept": concept,
            "lang": lang,
            "title": content["title"],
            "plain": content["plain"],
            "accountant": content["accountant"],
        }

    @api.model
    def get_production_guidance(self, company_id=None, pt=True):
        """Single next action + didactic content for one company.

        The first unresolved gap in setup order wins (block before warn); its
        NEXT_ACTION_LABELS label and didactic content become the coach's only
        call to action. When nothing is missing the next step is going deeper:
        L3 activation if the analytic layer is available, otherwise the kernel
        layers explainer. The kernel/L3 snapshot lets the panel render the
        "two ways to go deeper" tiles without an extra round trip.
        """
        self._ensure_operator()
        company = self._resolve_company(company_id)
        lang = "pt" if pt else "en"

        status = self.get_onboarding_status(company.id)
        summary = self.get_setup_summary([company.id]).get(company.id) or {}
        missing = list(summary.get("missing") or [])
        blocked = [m for m in missing if m["severity"] == "block"]
        warnings = [m for m in missing if m["severity"] == "warn"]

        ordered = {m["key"]: m for m in blocked + warnings}
        gap = None
        for key in GUIDANCE_GAP_ORDER:
            if key in ordered:
                gap = ordered[key]
                break

        l3 = self._l3_snapshot(company)
        if gap:
            next_action = {
                "key": gap["key"],
                "label": NEXT_ACTION_LABELS[gap["key"]][lang],
                "severity": gap["severity"],
                "fix": gap.get("fix"),
            }
        else:
            key = "l3_activation" if (l3["available"] and l3["available_count"]) else "kernel_layers"
            next_action = {
                "key": key,
                "label": NEXT_ACTION_LABELS[key][lang],
                "severity": "info",
                "fix": "manual",
            }

        return {
            "company_id": company.id,
            "company_name": company.name,
            "lang": lang,
            "phase": self._guidance_phase(status, missing),
            "required_complete": bool(status.get("required_complete")),
            "blocked": blocked,
            "warnings": warnings,
            "next_action": next_action,
            "next_content": self.get_guidance_content(next_action["key"], pt=pt),
            "kernel": status.get("kernel") or {},
            "l3": l3,
        }

    @api.model
    def _guidance_phase(self, status, missing):
        if not status.get("required_complete"):
            return "setup"
        if any(m["severity"] == "warn" for m in missing):
            return "operational"
        return "production"

    @api.model
    def _l3_snapshot(self, company):
        """Compact L3 availability for the coach panel.

        Activated = company accounts linked to L3 master accounts. Available =
        L3 master accounts not yet activated in the company, aggregated into
        canonical activity-tag / dashboard-set / functional-group buckets that
        match poseidon_activate_l3_batch preselection semantics.
        """
        if "qbo.standard.account" not in self.env:
            return {"available": False}
        StandardAccount = self.env["qbo.standard.account"].sudo()
        if "kernel_layer" not in StandardAccount._fields:
            return {"available": False}
        StandardAccount._ensure_l3_master_chart_imported()
        if not StandardAccount.search(
            [("kernel_layer", "=", "L3"), ("active", "=", True)], limit=1
        ):
            return {"available": False}

        Account = self.env["account.account"].sudo()
        activated = Account.search(
            [
                ("company_ids", "=", company.id),
                ("qbo_standard_account_id.kernel_layer", "=", "L3"),
            ]
        )
        activated_codes = sorted(activated.mapped("code"))
        activated_set = set(activated_codes)

        standards = StandardAccount.search(
            [
                ("kernel_layer", "=", "L3"),
                ("entry_type", "=", "detail"),
                ("active", "=", True),
            ],
            order="code",
        )
        available = [s for s in standards if s.code not in activated_set]

        tags = {tag: 0 for tag in L3_CANONICAL_TAGS}
        for standard in available:
            buckets = set()
            for token in (standard.activity_tag or "").split("/"):
                token = token.strip()
                if token in _L3_TAG_TOKEN_MAP:
                    buckets.add(_L3_TAG_TOKEN_MAP[token])
            for bucket in buckets:
                tags[bucket] += 1

        sets = {}
        groups = {}
        for standard in available:
            for dset in (standard.dashboard_account_sets or "").split(","):
                dset = dset.strip()
                if dset:
                    sets[dset] = sets.get(dset, 0) + 1
            group = standard.functional_group or ""
            if group:
                groups[group] = groups.get(group, 0) + 1

        profile = self._tax_profile(company)
        profile_tags = profile.poseidon_l3_activity_tags() if profile else ["Universal"]

        return {
            "available": True,
            "activated_count": len(activated_codes),
            "activated_codes": activated_codes,
            "total": len(standards),
            "available_count": len(available),
            "by_activity_tag": tags,
            "by_dashboard_set": sets,
            "by_functional_group": groups,
            "profile_activity_tags": profile_tags,
        }
