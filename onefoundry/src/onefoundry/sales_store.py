"""One offline Sales continuation and exact, one-shot local email delivery."""

# SQL fragments in this module are constant; all values use binds.
# ruff: noqa: S608

import hashlib
import json
import re
import smtplib
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any
from urllib.error import URLError
from urllib.request import urlopen
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry import run_audit

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

RECIPIENT = "sales-preview@onefoundry.invalid"
SENDER = "onefoundry@onefoundry.invalid"
DRAFT_COLUMNS = (
    "id, project_id, actor_id, source_run_id, run_id, approval_id, recipient, "
    "subject, body, body_sha256, delivery_status, sent_at, created_at"
)


def proposal_digest(target: str, content: str) -> str:
    raw = json.dumps(
        {"action_type": "email", "target": target, "content": content},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def email_content(subject: str, body: str) -> str:
    return f"Subject: {subject}\n\n{body}"


class SalesStore:
    def __init__(self, gateway: "ToolGateway", smtp_host: str, smtp_port: int, mailpit_http_port: int):
        self.gateway = gateway
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.mailpit_http_port = mailpit_http_port

    def eligibility(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            return self._eligibility(connection, project_id, run_id)

    def _eligibility(self, connection: Any, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        from onefoundry.tool_gateway import NotFound

        row = (
            connection.execute(
                text(
                    "SELECT r.id, r.work_order_id, r.status, r.mode, p.name AS profile_name, "
                    "p.kind AS profile_kind FROM runs r JOIN profiles p "
                    "ON p.project_id=r.project_id AND p.id=r.profile_id "
                    "WHERE r.project_id=:project_id AND r.id=:run_id"
                ),
                {"project_id": project_id, "run_id": run_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("run not found")
        sales_id = connection.scalar(
            text("SELECT id FROM profiles WHERE project_id=:project_id AND kind='builtin' AND name='Sales'"),
            {"project_id": project_id},
        )
        reason = None
        if row["mode"] != "synthetic_text" or row["status"] != "done":
            reason = "completed Researcher to GTM synthetic run required"
        elif row["profile_name"] != "GTM" or row["profile_kind"] != "builtin":
            reason = "built-in GTM handoff required"
        elif sales_id is None:
            reason = "built-in Sales profile missing"
        elif (
            connection.scalar(
                text(
                    "SELECT 1 FROM text_outputs WHERE project_id=:project_id AND run_id=:run_id "
                    "AND role='GTM' LIMIT 1"
                ),
                {"project_id": project_id, "run_id": run_id},
            )
            is None
        ):
            reason = "GTM output missing"
        elif (
            connection.scalar(
                text("SELECT 1 FROM sales_drafts WHERE project_id=:project_id AND run_id=:run_id"),
                {"project_id": project_id, "run_id": run_id},
            )
            is not None
        ):
            reason = "Sales draft already exists"
        return {"eligible": reason is None, "reason": reason, "sales_profile_id": sales_id}

    def create(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        from onefoundry.tool_gateway import Conflict

        draft_id, approval_id = uuid4(), uuid4()
        with self.gateway.engine.begin() as connection:
            connection.execute(
                text("SELECT id FROM runs WHERE project_id=:project_id AND id=:run_id FOR UPDATE"),
                {"project_id": project_id, "run_id": run_id},
            )
            eligible = self._eligibility(connection, project_id, run_id)
            if not eligible["eligible"]:
                raise Conflict(str(eligible["reason"]))
            source = (
                connection.execute(
                    text(
                        "SELECT w.title, r.work_order_id, o.text AS gtm_text, o.text_sha256 "
                        "FROM runs r JOIN work_orders w "
                        "ON w.project_id=r.project_id AND w.id=r.work_order_id "
                        "JOIN text_outputs o ON o.project_id=r.project_id AND o.run_id=r.id AND o.role='GTM' "
                        "WHERE r.project_id=:project_id AND r.id=:run_id"
                    ),
                    {"project_id": project_id, "run_id": run_id},
                )
                .mappings()
                .one()
            )
            safe_title = " ".join(str(source["title"]).split())
            subject = f"OneFoundry local preview {draft_id}: {safe_title}".encode()[:200].decode(
                "utf-8", errors="ignore"
            )
            body = (
                (
                    "Local synthetic Sales draft for operator review.\n\n"
                    "Source: completed GTM output on this work order.\n\n"
                    f"{source['gtm_text']}"
                )
                .encode()[:16000]
                .decode("utf-8", errors="ignore")
            )
            digest = hashlib.sha256(body.encode()).hexdigest()
            content = email_content(subject, body)
            proposal = proposal_digest(RECIPIENT, content)
            sales_id = eligible["sales_profile_id"]
            actor = f"profile:{sales_id}"
            connection.execute(
                text("UPDATE runs SET profile_id=:sales_id WHERE project_id=:project_id AND id=:run_id"),
                {"sales_id": sales_id, "project_id": project_id, "run_id": run_id},
            )
            run_audit.record_handoff(connection, project_id, run_id, sales_id, actor)
            connection.execute(
                text(
                    "UPDATE work_orders SET profile_id=:sales_id "
                    "WHERE project_id=:project_id AND id=:order_id"
                ),
                {"sales_id": sales_id, "project_id": project_id, "order_id": source["work_order_id"]},
            )
            self.gateway._event(
                connection,
                project_id,
                source["work_order_id"],
                run_id,
                "synthetic_sales_handed_off",
                {
                    "to_profile_id": str(sales_id),
                    "source_role": "GTM",
                    "source_text_sha256": source["text_sha256"],
                    "tokens_used": 0,
                    "cost_usd": 0,
                },
                actor_id=actor,
            )
            connection.execute(
                text(
                    "INSERT INTO approval_requests "
                    "(id, project_id, actor_id, work_order_id, run_id, action_type, target, "
                    "content, proposal_digest, status) VALUES "
                    "(:id,:project_id,:actor_id,:order_id,:run_id,'email',:target,:content,:digest,'pending')"
                ),
                {
                    "id": approval_id,
                    "project_id": project_id,
                    "actor_id": actor,
                    "order_id": source["work_order_id"],
                    "run_id": run_id,
                    "target": RECIPIENT,
                    "content": content,
                    "digest": proposal,
                },
            )
            row = (
                connection.execute(
                    text(
                        "INSERT INTO sales_drafts (id,project_id,actor_id,source_run_id,run_id,approval_id,"
                        "recipient,subject,body,body_sha256) VALUES "
                        "(:id,:project_id,:actor_id,:source_run_id,:run_id,:approval_id,:recipient,"
                        f":subject,:body,:body_sha256) RETURNING {DRAFT_COLUMNS}"
                    ),
                    {
                        "id": draft_id,
                        "project_id": project_id,
                        "actor_id": actor,
                        "source_run_id": run_id,
                        "run_id": run_id,
                        "approval_id": approval_id,
                        "recipient": RECIPIENT,
                        "subject": subject,
                        "body": body,
                        "body_sha256": digest,
                    },
                )
                .mappings()
                .one()
            )
            self.gateway._event(
                connection,
                project_id,
                source["work_order_id"],
                run_id,
                "synthetic_sales_draft_recorded",
                {
                    "draft_id": str(draft_id),
                    "approval_id": str(approval_id),
                    "body_sha256": digest,
                    "proposal_digest": proposal,
                    "status": "pending",
                    "tokens_used": 0,
                    "cost_usd": 0,
                },
                actor_id=actor,
            )
            return dict(row)

    def get(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        from onefoundry.tool_gateway import NotFound

        with self.gateway.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        f"SELECT {DRAFT_COLUMNS} FROM sales_drafts "
                        "WHERE project_id=:project_id AND run_id=:run_id"
                    ),
                    {"project_id": project_id, "run_id": run_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("Sales draft not found")
            return dict(row)

    def decision(self, connection: Any, project_id: UUID, approval: dict[str, Any], decision: str) -> bool:
        """Bind the decided proposal to immutable draft bytes; return whether to deliver."""
        from onefoundry.tool_gateway import Conflict

        draft = (
            connection.execute(
                text(
                    f"SELECT {DRAFT_COLUMNS} FROM sales_drafts WHERE project_id=:project_id "
                    "AND approval_id=:approval_id FOR UPDATE"
                ),
                {"project_id": project_id, "approval_id": approval["id"]},
            )
            .mappings()
            .one_or_none()
        )
        if draft is None:
            return False
        if draft["delivery_status"] != "pending":
            raise Conflict("Sales delivery already decided")
        exact = (
            approval["action_type"] == "email"
            and approval["target"] == draft["recipient"] == RECIPIENT
            and approval["content"] == email_content(draft["subject"], draft["body"])
            and approval["proposal_digest"] == proposal_digest(draft["recipient"], approval["content"])
            and draft["body_sha256"] == hashlib.sha256(draft["body"].encode()).hexdigest()
        )
        if not exact:
            raise Conflict("Sales approval no longer matches exact draft")
        status = "rejected" if decision == "rejected" else "unknown"
        connection.execute(
            text("UPDATE sales_drafts SET delivery_status=:status WHERE project_id=:project_id AND id=:id"),
            {"status": status, "project_id": project_id, "id": draft["id"]},
        )
        self.gateway._event(
            connection,
            project_id,
            approval["work_order_id"],
            approval["run_id"],
            "synthetic_sales_delivery_decided",
            {
                "draft_id": str(draft["id"]),
                "approval_id": str(approval["id"]),
                "decision": decision,
                "delivery_status": status,
                "proposal_digest": approval["proposal_digest"],
                "tokens_used": 0,
                "cost_usd": 0,
            },
        )
        if decision == "approved":
            self.gateway.reconcile_external_action(
                connection,
                project_id,
                approval["work_order_id"],
                approval["run_id"],
                confirmed=False,
            )
        return decision == "approved"

    def deliver_once(self, project_id: UUID, approval_id: UUID) -> None:
        """A committed unknown state makes crashes and ambiguous SMTP failures non-retryable."""
        with self.gateway.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        f"SELECT {DRAFT_COLUMNS} FROM sales_drafts "
                        "WHERE project_id=:project_id AND approval_id=:approval_id"
                    ),
                    {"project_id": project_id, "approval_id": approval_id},
                )
                .mappings()
                .one()
            )
            draft = dict(row)
            work_order_id = connection.scalar(
                text("SELECT work_order_id FROM runs WHERE project_id=:project_id AND id=:run_id"),
                {"project_id": project_id, "run_id": draft["run_id"]},
            )
        message = EmailMessage()
        message["From"] = SENDER
        message["To"] = RECIPIENT
        message["Subject"] = draft["subject"]
        message["Message-ID"] = f"<{draft['id']}@onefoundry.invalid>"
        message.set_content(draft["body"])
        confirmed = False
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=5) as smtp:
                smtp.send_message(message, from_addr=SENDER, to_addrs=[RECIPIENT])
            confirmed = self._mailpit_readback(draft)
        except (OSError, smtplib.SMTPException, URLError, ValueError):
            confirmed = False
        with self.gateway.engine.begin() as connection:
            if confirmed:
                connection.execute(
                    text(
                        "UPDATE sales_drafts SET delivery_status='delivered', sent_at=clock_timestamp() "
                        "WHERE project_id=:project_id AND id=:id AND delivery_status='unknown'"
                    ),
                    {"project_id": project_id, "id": draft["id"]},
                )
            self.gateway._event(
                connection,
                project_id,
                work_order_id,
                draft["run_id"],
                "synthetic_sales_delivery_result",
                {
                    "draft_id": str(draft["id"]),
                    "approval_id": str(approval_id),
                    "delivery_status": "delivered" if confirmed else "unknown",
                    "tokens_used": 0,
                    "cost_usd": 0,
                },
            )
            self.gateway.reconcile_external_action(
                connection, project_id, work_order_id, draft["run_id"], confirmed=confirmed
            )

    def _mailpit_readback(self, draft: dict[str, Any]) -> bool:
        url = f"http://{self.smtp_host}:{self.mailpit_http_port}/api/v1/messages"
        with urlopen(url, timeout=5) as response:  # noqa: S310 - validated internal Mailpit host
            listing = self._bounded_json(response)
        messages = listing.get("messages")
        if not isinstance(messages, list):
            raise ValueError("Invalid Mailpit message listing")
        for item in messages:
            if not isinstance(item, dict):
                raise ValueError("Invalid Mailpit message entry")
            if item.get("Subject") != draft["subject"]:
                continue
            recipients = item.get("To")
            if not isinstance(recipients, list) or any(not isinstance(r, dict) for r in recipients):
                raise ValueError("Invalid Mailpit recipients")
            if not any(r.get("Address") == RECIPIENT for r in recipients):
                continue
            message_id = item.get("ID")
            if not isinstance(message_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", message_id) is None:
                continue
            with urlopen(  # noqa: S310 - validated internal Mailpit host
                f"http://{self.smtp_host}:{self.mailpit_http_port}/api/v1/message/{message_id}", timeout=5
            ) as response:
                detail = self._bounded_json(response)
            body = detail.get("Text")
            if not isinstance(body, str):
                raise ValueError("Invalid Mailpit message detail")
            observed_body = body.replace("\r\n", "\n")
            if observed_body.endswith("\n"):
                observed_body = observed_body[:-1]
            if hashlib.sha256(observed_body.encode()).hexdigest() == draft["body_sha256"]:
                return True
        return False

    @staticmethod
    def _bounded_json(response: Any) -> dict[str, Any]:
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("Mailpit response too large")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Invalid Mailpit response")
        return result
