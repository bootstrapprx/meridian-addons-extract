"""Evidence reads and atomic, reviewed Meridian setup operations."""
import hashlib
import json
import math
import calendar
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError

KINDS = ("cost_center", "project", "budget", "chart_account", "mapping", "close_period", "close_reconciliation", "close_controls")
MAX_ACTIONS = 25


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def text(value, label, maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValidationError(_("%s must be non-empty text of at most %s characters.") % (label, maximum))
    return value.strip()


def positive_id(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(_("A positive integer ID is required."))
    return value


def planning_run_rate(monthly, start, end, mapping_ready):
    """A labelled scenario from three complete observed months, never missing-data zeros."""
    if not mapping_ready:
        return {"state": "blocked", "reason": "Resolve mapping before producing classified projections."}
    last = end.replace(day=1)
    if end.day != calendar.monthrange(end.year, end.month)[1]:
        last = (last - timedelta(days=1)).replace(day=1)
    periods = [last]
    for _index in range(2):
        periods.insert(0, (periods[0] - timedelta(days=1)).replace(day=1))
    keys = [period.strftime("%Y-%m") for period in periods]
    by_month = {row["month"]: row for row in monthly}
    if periods[0] < start or any(key not in by_month for key in keys):
        return {"state": "insufficient_evidence", "reason": "Three consecutive complete months with posted evidence are required."}
    following = (last.replace(day=28) + timedelta(days=4)).replace(day=1)
    return {"state": "scenario", "basis": "three complete observed months; arithmetic mean; assumes unchanged activity",
            "source_months": keys, "month": following.strftime("%Y-%m"),
            "revenue": sum(by_month[key]["revenue"] for key in keys) / 3,
            "expenses": sum(by_month[key]["expenses"] for key in keys) / 3,
            "net": sum(by_month[key]["net"] for key in keys) / 3,
            "applied": False}


class PoseidonPlanningTools(models.AbstractModel):
    _inherit = "poseidon.mcp.tools"

    @api.model
    def _planning_company(self, company_id):
        company_id = positive_id(company_id)
        if company_id not in self.env.user.company_ids.ids:
            raise AccessError(_("Company is outside your authorized workspace."))
        return self.env["res.company"].browse(company_id).exists()

    @api.model
    def _planning_dates(self, payload):
        start = fields.Date.to_date(payload.get("date_from"))
        end = fields.Date.to_date(payload.get("date_to"))
        if not start or not end or end < start or (end - start).days > 730:
            raise ValidationError(_("Choose an ordered date range of at most 730 days."))
        return start, end

    @api.model
    def _mapping_counts(self, company):
        if "poseidon.mapping.decision" not in self.env:
            return {"available": False, "pending": None, "confirmed": None, "rejected": None, "unresolved_sources": None}
        Decision = self.env["poseidon.mapping.decision"]
        sources = self.env["account.account"].search([("company_ids", "in", [company.id]), ("qbo_id", "!=", False)])
        confirmed = Decision.search([("company_id", "=", company.id), ("state", "=", "confirmed")]).mapped("source_account_id")
        return {"available": True, "unresolved_sources": len(sources - confirmed), **{
            state: Decision.search_count([("company_id", "=", company.id), ("state", "=", state)])
            for state in ("pending", "confirmed", "rejected")
        }}

    @api.model
    def _execute_planning_history(self, job, payload):
        del job
        start, end = self._planning_dates(payload)
        ids = payload.get("company_ids")
        if not isinstance(ids, list) or not ids or len(ids) > 10 or len(set(ids)) != len(ids):
            raise ValidationError(_("Select one to ten distinct authorized companies."))
        companies = [self._planning_company(value) for value in ids]
        limit = positive_id(payload.get("limit", 10))
        if limit > 25:
            raise ValidationError(_("The per-company sample limit is 25."))
        result = []
        for company in companies:
            scoped = self.with_company(company).with_context(allowed_company_ids=[company.id])
            mapping = scoped._mapping_counts(company)
            account_domain = [("company_ids", "in", [company.id])]
            source_accounts = scoped.env["account.account"].search(account_domain + [("qbo_id", "!=", False)])
            decisions = scoped.env["poseidon.mapping.decision"].search([
                ("company_id", "=", company.id), ("state", "=", "pending")
            ], order="id", limit=limit) if mapping["available"] else []
            unresolved = []
            for decision in decisions:
                candidates = scoped.env["qbo.standard.account"].sudo().search([
                    ("entry_type", "=", "detail"),
                    ("odoo_account_type", "=", decision.source_account_id.account_type),
                ], order="code", limit=5)
                unresolved.append({
                    "decision_id": decision.id, "source_account_id": decision.source_account_id.id,
                    "source_name": decision.qbo_account_name, "source_code": decision.qbo_account_number,
                    "source_type": decision.qbo_account_type,
                    "candidates": [{"code": row.code, "name": row.description} for row in candidates],
                    "candidate_basis": "account type only; not a confirmed recommendation",
                })
            domain = [("company_id", "=", company.id), ("date", ">=", start), ("date", "<=", end)]
            Line = scoped.env["account.move.line"]
            # Currency and state remain separate. Draft imports never become actuals.
            groups = Line._read_group(domain, ["parent_state", "currency_id"], ["debit:sum", "credit:sum", "__count"])
            totals = [{"state": state, "transaction_currency": currency.name if currency else None,
                       "debit": debit, "credit": credit, "line_count": count}
                      for state, currency, debit, credit, count in groups]
            monthly_groups = Line._read_group(domain + [("parent_state", "=", "posted"),
                ("account_id.account_type", "in", ["income", "income_other", "expense", "expense_direct_cost", "expense_depreciation"])],
                ["date:month", "account_id"], ["balance:sum", "__count"])
            monthly = {}
            for date, account, balance, count in monthly_groups:
                key = date.strftime("%Y-%m")
                row = monthly.setdefault(key, {"month": key, "revenue": 0, "expenses": 0, "net": 0, "line_count": 0})
                if account.account_type in ("income", "income_other"):
                    row["revenue"] -= balance
                else:
                    row["expenses"] += balance
                row["net"] = row["revenue"] - row["expenses"]
                row["line_count"] += count
            monthly_rows = [monthly[key] for key in sorted(monthly)]
            ready = mapping["available"] and not mapping["unresolved_sources"] and not mapping["pending"] and not mapping["rejected"]
            source_groups = Line._read_group(domain + [("account_id", "in", source_accounts.ids)],
                                            ["account_id", "parent_state"], ["debit:sum", "credit:sum", "__count"])
            activity = [{"source_account_id": account.id, "state": state, "debit": debit,
                         "credit": credit, "line_count": count}
                        for account, state, debit, credit, count in source_groups[:limit]]
            analytic = scoped.env["account.analytic.account"].search([
                ("company_id", "=", company.id)
            ], order="id", limit=limit) if "account.analytic.account" in scoped.env else []
            projects = scoped.env["project.project"].search([("company_id", "=", company.id)], limit=limit) if "project.project" in scoped.env else []
            budgets = scoped.env["crossovered.budget"].search([("company_id", "=", company.id)], limit=limit) if "crossovered.budget" in scoped.env else []
            positions = scoped.env["account.budget.post"].search([("company_id", "=", company.id)], limit=limit) if "account.budget.post" in scoped.env else []
            close_read = "accounting.close" in scoped.env and scoped.env["accounting.close"].has_access("read")
            closes = scoped.env["accounting.close"].search([("company_id", "=", company.id), ("date_end", ">=", start), ("date_start", "<=", end)], order="date_end desc", limit=limit) if close_read else []
            result.append({
                "company_id": company.id, "company": company.name, "company_currency": company.currency_id.name, "debit_credit_currency": company.currency_id.name,
                "mapping": mapping, "canonical_classification_ready": mapping["available"] and not mapping["unresolved_sources"] and not mapping["pending"] and not mapping["rejected"],
                "monthly_posted_flows": monthly_rows, "monthly_flows_classification": "canonical" if ready else "provisional source classification",
                "run_rate_scenario": planning_run_rate(monthly_rows, start, end, ready),
                "insights": ["Resolve %s unresolved source accounts before classified reporting." % mapping["unresolved_sources"]] if not ready else ["Mapping is complete; projections still require dated posted evidence."],
                "totals": totals, "source_activity_sample": activity,
                "source_activity_truncated": len(source_groups) > limit,
                "pending_mapping_sample": unresolved, "pending_mapping_truncated": (mapping["pending"] or 0) > limit,
                "dimensions_sample": [{"id": a.id, "code": a.code, "name": a.name, "plan_id": a.plan_id.id} for a in analytic],
                "projects_sample": [{"id": p.id, "name": p.name, "analytic_account_id": p.account_id.id} for p in projects],
                "budget_positions_sample": [{"id": p.id, "name": p.name} for p in positions],
                "close_capability": "available" if close_read else "not installed or role unavailable",
                "close_sample": [{"id": c.id, "name": c.name, "state": c.state, "pending_entries": c.pending_entry_count,
                                  "open_reconciliations": c.open_reconciliation_count, "sox_failures": c.sox_failure_count,
                                  "controls_tested": bool(c.sox_test_ids), "date_from": str(c.date_start), "date_to": str(c.date_end)} for c in closes],
                "budgets_sample": [{"id": b.id, "name": b.name, "state": b.state, "date_from": str(b.date_from), "date_to": str(b.date_to)} for b in budgets],
            })
        return {"date_from": str(start), "date_to": str(end), "companies": result,
                "evidence_policy": "Posted and draft totals are separate. Unmapped source history supports triage, not classified actuals. Never add company currencies together. Samples are not complete inventories."}

    @api.model
    def _planning_standard(self, code):
        standard = self.env["qbo.standard.account"].sudo().search([
            ("code", "=", text(code, "kernel_code", 64)), ("entry_type", "=", "detail")
        ], limit=1)
        if not standard:
            raise ValidationError(_("Choose a detail account from the canonical master chart."))
        return standard

    @api.model
    def _planning_action(self, company, raw):
        if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
            raise ValidationError(_("Unsupported setup action."))
        kind = raw["kind"]
        # Normalized preview metadata is accepted when revalidating the stored proposal.
        action = {"kind": kind, "rationale": text(raw.get("rationale"), "rationale", 1000)}
        if kind in ("cost_center", "project"):
            if "poseidon_create_analytic_dimension" not in dir(self.env["account.analytic.account"]):
                raise ValidationError(_("Meridian analytic creation is not installed."))
            plan = self.env.ref("poseidon_cost_center.analytic_plan_cost_center" if kind == "cost_center" else "analytic.analytic_plan_projects", raise_if_not_found=False)
            if not plan:
                raise ValidationError(_("The requested analytic plan is not installed."))
            action.update(name=text(raw.get("name"), "name"), code=text(raw.get("code"), "code", 64), plan_id=plan.id)
            if self.env["account.analytic.account"].with_context(active_test=False).search_count([
                ("company_id", "=", company.id), ("root_plan_id", "=", plan.id), ("code", "=", action["code"])
            ]):
                raise ValidationError(_("This analytic code already exists. Reuse the existing dimension."))
        elif kind == "budget":
            if "crossovered.budget" not in self.env:
                raise ValidationError(_("Meridian budgets are not installed."))
            start, end = self._planning_dates(raw)
            basis = raw.get("basis")
            if basis not in ("assumption", "historical", "operator"):
                raise ValidationError(_("Budget basis must be assumption, historical or operator."))
            mapping = self._mapping_counts(company)
            if basis == "historical" and (not mapping["available"] or mapping["pending"] or mapping["rejected"] or mapping["unresolved_sources"]):
                raise ValidationError(_("Resolve mapping before treating history as classified budget actuals. Use an explicit assumption draft instead."))
            if basis == "historical" and not self.env["account.move.line"].search_count([
                ("company_id", "=", company.id), ("parent_state", "=", "posted"), ("date", ">=", start), ("date", "<=", end)
            ], limit=1):
                raise ValidationError(_("No posted historical evidence exists in the budget period. Use an explicit assumption draft."))
            action.update(name=text(raw.get("name"), "name"), date_from=str(start), date_to=str(end), basis=basis)
            allocations = raw.get("allocations", [])
            if not isinstance(allocations, list) or len(allocations) > 25:
                raise ValidationError(_("A budget supports at most 25 allocations per proposal."))
            action["allocations"] = []
            for allocation in allocations:
                analytic = self.env["account.analytic.account"].browse(positive_id(allocation.get("analytic_account_id"))).exists()
                post = self.env["account.budget.post"].browse(positive_id(allocation.get("general_budget_id"))).exists()
                if not analytic or analytic.company_id != company or not post or post.company_id != company:
                    raise ValidationError(_("Budget allocations must reference dimensions and positions in the target company."))
                analytic.check_access("read"); post.check_access("read")
                amount = allocation.get("planned_amount")
                if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or not amount:
                    raise ValidationError(_("Allocation amount must be finite and nonzero."))
                action["allocations"].append({"analytic_account_id": analytic.id, "general_budget_id": post.id, "planned_amount": amount, "analytic_name": analytic.name, "position_name": post.name, "analytic_version": str(analytic.write_date), "position_version": str(post.write_date)})
        elif kind == "close_controls":
            if "accounting.close" not in self.env:
                raise ValidationError(_("Accounting Close is not installed."))
            close = self.env["accounting.close"].browse(positive_id(raw.get("close_id"))).exists()
            if not close or close.company_id != company or close.state == "closed":
                raise ValidationError(_("Choose an open close in the target company."))
            close.check_access("write")
            close._require_reviewer()
            action.update(close_id=close.id, close_name=close.name, close_state=close.state,
                          snapshot_digest=close._control_snapshot_digest())
        elif kind in ("close_period", "close_reconciliation"):
            if "accounting.close" not in self.env:
                raise ValidationError(_("Accounting Close is not installed."))
            Model = self.env["accounting.close" if kind == "close_period" else "accounting.close.reconciliation"]
            Model.check_access("create")
            if kind == "close_period":
                start, end = self._planning_dates(raw)
                if Model.search_count([("company_id", "=", company.id), ("date_start", "=", start), ("date_end", "=", end)]):
                    raise ValidationError(_("Reuse the existing close for this period."))
                action.update(name=text(raw.get("name"), "name"), date_from=str(start), date_to=str(end))
            else:
                close = self.env["accounting.close"].browse(positive_id(raw.get("close_id"))).exists()
                account = self.env["account.account"].browse(positive_id(raw.get("account_id"))).exists()
                if not close or close.company_id != company or close.state == "closed" or not account or company not in account.company_ids:
                    raise ValidationError(_("Choose an open close and account in the target company."))
                close.check_access("read"); account.check_access("read")
                if Model.search_count([("close_id", "=", close.id), ("account_id", "=", account.id)]):
                    raise ValidationError(_("Reuse the existing account reconciliation."))
                amount = raw.get("subledger_balance")
                if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount):
                    raise ValidationError(_("Provide a finite explicit subledger balance; missing evidence is not zero."))
                action.update(close_id=close.id, account_id=account.id, subledger_balance=amount,
                              close_name=close.name, account_name=account.display_name,
                              close_version=str(close.write_date), account_version=str(account.write_date))
        else:
            standard = self._planning_standard(raw.get("kernel_code"))
            action.update(kernel_code=standard.code, kernel_name=standard.description, standard_id=standard.id, account_type=standard.odoo_account_type)
            if kind == "mapping":
                if "poseidon.mapping.decision" not in self.env:
                    raise ValidationError(_("Mapping governance is not installed."))
                decision = self.env["poseidon.mapping.decision"].browse(positive_id(raw.get("decision_id"))).exists()
                if not decision or decision.company_id != company or decision.state != "pending":
                    raise ValidationError(_("Choose a pending mapping in the target company."))
                decision.check_access("read")
                source = decision.source_account_id
                if not source or source.account_type != standard.odoo_account_type or not source.qbo_id:
                    raise ValidationError(_("The mapping source and canonical destination must have compatible account types and a source ID."))
                action.update(decision_id=decision.id, source_account_id=source.id, source_name=decision.qbo_account_name, source_code=decision.qbo_account_number)
        return action

    @api.model
    def _planning_actions(self, payload):
        company = self._planning_company(payload.get("company_id"))
        if company.fiscalyear_lock_date or company.tax_lock_date:
            raise ValidationError(_("The target company has a locked accounting period."))
        actions = payload.get("actions")
        if not isinstance(actions, list) or not actions or len(actions) > MAX_ACTIONS:
            raise ValidationError(_("Propose one to 25 actions per company."))
        scoped = self.with_company(company).with_context(allowed_company_ids=[company.id])
        normalized = [scoped._planning_action(company, action) for action in actions]
        keys = []
        for action in normalized:
            kind = action["kind"]
            if kind == "close_controls":
                identity = action["close_id"]
            elif kind == "close_reconciliation":
                identity = (action["close_id"], action["account_id"])
            elif kind == "close_period":
                identity = (action["date_from"], action["date_to"])
            else:
                identity = action.get("decision_id") or action.get("code") or action.get("kernel_code") or (action["name"], action["date_from"], action["date_to"])
            keys.append((kind, identity))
        if len(set(keys)) != len(keys):
            raise ValidationError(_("Duplicate actions in the proposal."))
        if len(json.dumps(normalized, ensure_ascii=False)) > 12000:
            raise ValidationError(_("The proposal is too large to review. Use a smaller batch."))
        return company, normalized

    @api.model
    def _execute_planning_preview(self, job, payload):
        company, actions = self._planning_actions(payload)
        return {"preview": True, "preview_job_id": job.id, "requested_by_id": self.env.uid, "company_id": company.id,
                "company": company.name, "currency": company.currency_id.name, "actions": actions,
                "fingerprint": digest(actions), "mapping": self._mapping_counts(company),
                "warning": "Review every action. Budgets and close periods remain draft; reconciliations start unevaluated. Close controls create evidence but never certify or sign off. No transactions are posted or reimported."}

    @api.model
    def _execute_planning_apply(self, job, payload):
        if not self.env.user.has_group("account.group_account_manager"):
            raise AccessError(_("Setup application requires an accounting manager."))
        preview_id = positive_id(payload.get("preview_job_id"))
        preview = self.env["kodoo.mcp.job"].get_job_for_ai_center(preview_id)
        saved = preview.get("result") or {}
        if preview["operation"] != "poseidon.preview_setup" or preview["state"] != "done" or saved.get("requested_by_id") != self.env.uid:
            raise AccessError(_("The reviewed preview is unavailable or belongs to another operator."))
        if "actions" not in saved or "fingerprint" not in saved:
            raise ValidationError(_("The proposal exceeds the review limit. Generate a smaller preview."))
        company = self._planning_company(payload.get("company_id"))
        if company.id != saved["company_id"]:
            raise AccessError(_("Preview company mismatch."))
        company, actions = self._planning_actions({"company_id": company.id, "actions": saved["actions"]})
        if digest(actions) != saved["fingerprint"]:
            raise ValidationError(_("The proposal changed since preview. Generate and review a new preview."))
        scoped = self.with_company(company).with_context(allowed_company_ids=[company.id])
        results = [scoped._planning_apply_action(company, action) for action in actions]
        return {"applied": True, "preview_job_id": preview_id, "company_id": company.id,
                "results": results, "audit_job_id": job.id, "replayed": False}

    @api.model
    def _planning_apply_action(self, company, action):
        kind = action["kind"]
        if kind == "close_controls":
            close = self.env["accounting.close"].browse(action["close_id"])
            close.action_run_sox_tests()
            return {"kind": kind, "close_id": close.id, "control_count": len(close._latest_sox_tests()),
                    "failed_controls": close.sox_failure_count, "state": close.state, "signed_off": False}
        if kind == "close_period":
            close = self.env["accounting.close"].create({"company_id": company.id, "name": action["name"],
                        "date_start": action["date_from"], "date_end": action["date_to"], "notes": action["rationale"]})
            close._audit("agent_close_prepared", _("Created from an approved canonical setup preview."))
            return {"kind": kind, "id": close.id, "name": close.name, "state": close.state, "signed_off": False}
        if kind == "close_reconciliation":
            rec = self.env["accounting.close.reconciliation"].create({"close_id": action["close_id"], "account_id": action["account_id"],
                        "subledger_balance": action["subledger_balance"], "reconciling_items": action["rationale"]})
            rec.close_id._audit("agent_reconciliation_prepared", _("Prepared from an approved canonical setup preview."), rec.account_id.code)
            return {"kind": kind, "id": rec.id, "close_id": rec.close_id.id, "state": rec.state, "certified": False}
        if kind in ("cost_center", "project"):
            item = self.env["account.analytic.account"].poseidon_create_analytic_dimension({
                "company_id": company.id, "name": action["name"], "code": action["code"], "plan_id": action["plan_id"]})
            return {"kind": kind, "id": item["id"], "name": item["name"], "code": item["code"]}
        if kind == "budget":
            Budget = self.env["crossovered.budget"]
            budget = Budget.poseidon_create_budget({**action, "company_id": company.id})
            for allocation in action["allocations"]:
                Budget.poseidon_allocate_budget(budget["id"], allocation)
            return {"kind": kind, "id": budget["id"], "name": budget["name"], "state": "draft", "basis": action["basis"]}
        target = self.env["account.chart.template"].poseidon_activate_standard_account_for_company(company.id, code=action["kernel_code"])
        if kind == "chart_account":
            return {"kind": kind, **target}
        decision = self.env["poseidon.mapping.decision"].browse(action["decision_id"])
        destination = self.env["account.account"].browse(target["account"]["id"])
        source = decision.source_account_id
        numeric_id = "".join(c for c in source.qbo_id if c.isdigit())
        if not numeric_id or source == destination:
            raise ValidationError(_("A distinct mapping source with a numeric QuickBooks ID is required."))
        rule = self.env["qbo.account.bridge.rule"].create({
            "company_id": company.id,
            "match_acct_num": decision.qbo_account_number or False,
            "match_name": decision.qbo_account_name,
            "match_account_type": decision.qbo_account_type,
            "match_account_subtype": decision.qbo_account_subtype or False,
            "standard_account_id": action["standard_id"], "active": True,
            "notes": action["rationale"],
        })
        source.write({"code": destination.code + "." + numeric_id, "name": destination.name,
                      "poseidon_parent_account_id": destination.id, "qbo_bridge_rule_id": rule.id})
        decision.write({"state": "confirmed", "destination_account_id": destination.id, "bridge_rule_id": rule.id,
                        "reason": action["rationale"], "decision_by_id": self.env.user.id, "decision_at": fields.Datetime.now()})
        return {"kind": kind, "decision_id": decision.id, "source_account_id": source.id,
                "destination_account_id": destination.id, "kernel_code": action["kernel_code"],
                "state": "confirmed", "standardized": True, "posted_transactions": False}

    @api.model
    def _get_mcp_operation_catalog(self):
        catalog = super()._get_mcp_operation_catalog()
        descriptions = {
            "historical_evidence": "Read dated workspace evidence and mapping backlog. Separates drafts from posted entries and companies/currencies; bounded samples are labelled. Use before historical planning.",
            "preview_setup": "Preview up to 25 cost_center, project, budget, chart_account, mapping, close_period, close_reconciliation or close_controls actions for one company. Each needs rationale. Close periods need name/dates; reconciliations need existing close_id/account_id and an explicit subledger_balance. Close preparation requires its domain role; close_controls explicitly runs the evidence suite for an existing close_id and requires a reviewer plus a current snapshot. No action certifies or signs off. Returns canonical preview_job_id and exact actions for review.",
            "apply_setup": "Apply exactly a reviewed canonical preview for one company. Requires explicit human approval, accounting manager rights and target domain roles. Revalidates scope, locks and stale evidence; atomic and replay safe. Budgets and close periods remain draft; reconciliations are unevaluated. Control-suite execution requires reviewer rights and the approved accounting snapshot. Never posts, certifies or signs off.",
        }
        for name, description in descriptions.items():
            catalog["poseidon." + name] = {
                "key": "poseidon." + name, "version": 1, "label": name.replace("_", " ").title(),
                "category": "poseidon", "bundle": "poseidon", "description": description,
                "available": True, "heavy": False, "supports_process_now": True,
                "requires_modules": ["qbo_bridge_standard_chart"],
                "payload_outline": {"required": ["company_ids", "date_from", "date_to"] if name == "historical_evidence" else ["company_id", "actions"] if name == "preview_setup" else ["company_id", "preview_job_id"], "optional": ["limit"] if name == "historical_evidence" else []},
                "result_outline": {"result": "Evidence, reviewed proposal or canonical apply outcome."},
            }
        return catalog


class PoseidonPlanningJob(models.Model):
    _inherit = "kodoo.mcp.job"

    @api.model
    def create_or_get_job(self, operation_key, payload=None, user=None, idempotency_key=None, **kwargs):
        if operation_key == "poseidon.apply_setup":
            idempotency_key = "setup:%s" % positive_id((payload or {}).get("preview_job_id"))
        return super().create_or_get_job(operation_key=operation_key, payload=payload, user=user,
                                        idempotency_key=idempotency_key, **kwargs)

    def _execute_operation(self, user, payload):
        handler = {"poseidon.historical_evidence": "_execute_planning_history", "poseidon.preview_setup": "_execute_planning_preview", "poseidon.apply_setup": "_execute_planning_apply"}.get(self.operation_key)
        if handler:
            return getattr(self.env["poseidon.mcp.tools"].with_user(user), handler)(self, payload)
        return super()._execute_operation(user, payload)

    @api.model
    def _json_schema_from_payload_outline(self, descriptor):
        key = descriptor["key"]
        if key not in ("poseidon.historical_evidence", "poseidon.preview_setup", "poseidon.apply_setup"):
            return super()._json_schema_from_payload_outline(descriptor)
        integer = {"type": "integer", "minimum": 1}
        string = {"type": "string"}
        allocation = {"type": "object", "properties": {
            "analytic_account_id": integer, "general_budget_id": integer, "planned_amount": {"type": "number"}},
            "required": ["analytic_account_id", "general_budget_id", "planned_amount"], "additionalProperties": False}
        action = {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "rationale": {"type": "string", "maxLength": 1000}, "name": string, "code": string,
            "kernel_code": string, "decision_id": integer, "date_from": string, "date_to": string,
            "close_id": integer, "account_id": integer, "subledger_balance": {"type": "number"},
            "basis": {"type": "string", "enum": ["assumption", "historical", "operator"]},
            "allocations": {"type": "array", "items": allocation, "maxItems": 25}},
            "required": ["kind", "rationale"], "additionalProperties": False}
        properties = {"company_ids": {"type": "array", "items": integer, "minItems": 1, "maxItems": 10},
                      "date_from": string, "date_to": string, "limit": {**integer, "maximum": 25}} if key.endswith("historical_evidence") else {
            "company_id": integer, "actions": {"type": "array", "items": action, "minItems": 1, "maxItems": 25}} if key.endswith("preview_setup") else {
            "company_id": integer, "preview_job_id": integer}
        return {"type": "object", "properties": properties,
                "required": descriptor["payload_outline"]["required"], "additionalProperties": False}
