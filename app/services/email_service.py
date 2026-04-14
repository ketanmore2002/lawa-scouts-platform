"""
Email service — sends scout reports via Gmail SMTP.
Mirrors the shared-report page design (dark theme), with sources hidden.
"""

import html as html_mod
import logging
import re
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from app.config import get_settings

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def _esc(text: str) -> str:
    return html_mod.escape(str(text)) if text else ""


def _build_report_html(report, scout_topic: str) -> str:
    """Build an HTML email that mirrors the shared-report page (sources excluded)."""
    findings = report.findings or {}
    sd = findings.get("structured_data")
    analysis = sd.get("analysis", "") if sd else ""

    created = ""
    if hasattr(report, "created_at") and report.created_at:
        dt = report.created_at if isinstance(report.created_at, datetime) else datetime.fromisoformat(str(report.created_at))
        created = dt.strftime("%m/%d/%Y %I:%M %p")

    # ── Outer wrapper — dark theme matching the app ──
    html = f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:680px;margin:0 auto;color:#e6edf3;background:#0d1117;border-radius:12px;overflow:hidden;">

    <!-- Header -->
    <div style="padding:32px 32px 0;">
        <div style="display:inline-block;padding:6px 14px;border-radius:20px;background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.25);font-size:12px;font-weight:600;color:#10b981;letter-spacing:.3px;margin-bottom:16px;">LAWA SCOUTS REPORT</div>
        <p style="font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:.6px;margin:0 0 8px;">LAWA Scouts &middot; {_esc(scout_topic)}</p>
        <h1 style="color:#e6edf3;font-size:22px;font-weight:700;margin:0 0 8px;line-height:1.3;">{_esc(report.title)}</h1>
        <p style="font-size:13px;color:#8b949e;margin:0 0 24px;">{_esc(created)}</p>
    </div>

    <!-- Summary card -->
    <div style="margin:0 24px 20px;padding:20px 24px;background:#161b22;border:1px solid #30363d;border-radius:10px;">
        <p style="font-size:11px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:.6px;margin:0 0 10px;">Summary</p>
        <p style="color:#c9d1d9;font-size:14px;line-height:1.75;margin:0;">{_esc(report.summary)}</p>
    </div>
"""

    # ── Key Insights (right after summary) ──
    if sd and sd.get("insights"):
        html += """\
    <div style="margin:0 24px 20px;padding:20px 24px;background:#161b22;border:1px solid #30363d;border-radius:10px;">
        <p style="font-size:11px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:.6px;margin:0 0 14px;">Key Insights</p>
"""
        for i, insight in enumerate(sd["insights"], 1):
            html += f"""\
        <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;margin-bottom:10px;"><tr>
            <td style="width:28px;vertical-align:top;padding-top:2px;">
                <div style="width:22px;height:22px;line-height:22px;text-align:center;border-radius:50%;background:rgba(16,185,129,.12);color:#10b981;font-size:11px;font-weight:700;">{i}</div>
            </td>
            <td style="color:#c9d1d9;font-size:13px;line-height:1.65;vertical-align:top;">{_esc(insight)}</td>
        </tr></table>
"""
        html += "    </div>\n"

    # ── Analysis & Key Findings ──
    if analysis:
        paragraphs = [p.strip() for p in analysis.split("\n\n") if p.strip()]
        analysis_html = ""
        for p in paragraphs:
            formatted = _esc(p).replace("\n", "<br>")
            formatted = re.sub(r"\*\*(.*?)\*\*", r'<strong style="color:#e6edf3;">\1</strong>', formatted)
            analysis_html += f'<p style="margin:0 0 14px;color:#c9d1d9;font-size:14px;line-height:1.8;">{formatted}</p>'

        html += f"""\
    <div style="margin:0 24px 20px;padding:20px 24px;background:#161b22;border:1px solid #30363d;border-radius:10px;">
        <p style="font-size:11px;font-weight:700;color:#0d9488;text-transform:uppercase;letter-spacing:.6px;margin:0 0 14px;">Analysis &amp; Key Findings</p>
        {analysis_html}
    </div>
"""

    # ── Stats cards (table-based for email compatibility) ──
    if sd and sd.get("stats"):
        color_map = {"green": "#34d399", "red": "#f87171", "blue": "#818cf8", "default": "#22d3ee"}
        stats = sd["stats"][:4]
        html += '    <div style="margin:0 24px 20px;">\n'
        html += '    <table role="presentation" cellpadding="0" cellspacing="8" style="width:100%;border-collapse:separate;"><tr>\n'
        for stat in stats:
            color = color_map.get(stat.get("color", "default"), "#22d3ee")
            html += f"""\
        <td style="background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px 16px;text-align:center;width:{100 // len(stats)}%;">
            <div style="font-size:22px;font-weight:700;color:{color};margin-bottom:4px;">{_esc(str(stat.get('value', '')))}</div>
            <div style="font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;">{_esc(str(stat.get('label', '')))}</div>
        </td>
"""
        html += "    </tr></table>\n    </div>\n"

    # ── CTA footer ──
    html += """\
    <div style="padding:28px 32px;text-align:center;border-top:1px solid #21262d;">
        <p style="color:#8b949e;font-size:13px;margin:0 0 16px;">This report was generated by <strong style="color:#10b981;">LAWA Scouts</strong> &mdash; AI agents that monitor the web for you.</p>
        <a href="https://lawa.app" target="_blank" style="display:inline-block;padding:10px 28px;background:#10b981;color:#0d1117;font-size:13px;font-weight:600;border-radius:8px;text-decoration:none;">Try LAWA Scouts</a>
    </div>

    <div style="padding:14px 32px;text-align:center;background:#0a0e14;">
        <p style="color:#484f58;font-size:11px;margin:0;">Sent by LAWA Scouts &mdash; Automated Research Platform</p>
    </div>
</div>
"""
    return html


def _build_plain_text(report, scout_topic: str) -> str:
    """Plain-text fallback with the same content structure."""
    findings = report.findings or {}
    sd = findings.get("structured_data")

    lines = [
        "LAWA SCOUTS REPORT",
        "=" * 40,
        f"Topic: {scout_topic}",
        f"Title: {report.title}",
        "",
        "SUMMARY",
        "-" * 40,
        report.summary,
        "",
    ]

    # Insights (right after summary)
    if sd and sd.get("insights"):
        lines.append("KEY INSIGHTS")
        lines.append("-" * 40)
        for i, insight in enumerate(sd["insights"], 1):
            lines.append(f"  {i}. {insight}")
        lines.append("")

    # Analysis
    analysis = sd.get("analysis", "") if sd else ""
    if analysis:
        lines += ["ANALYSIS & KEY FINDINGS", "-" * 40, analysis, ""]

    # Stats
    if sd and sd.get("stats"):
        lines.append("STATS")
        lines.append("-" * 40)
        for stat in sd["stats"][:4]:
            lines.append(f"  {stat.get('label', '')}: {stat.get('value', '')}")
        lines.append("")

    lines += [
        "-" * 40,
        "This report was generated by LAWA Scouts.",
        "Visit https://lawa.app to learn more.",
    ]
    return "\n".join(lines)


def send_invitation_email(
    to_email: str,
    workspace_name: str,
    inviter_name: str,
    role: str,
    token: str,
) -> bool:
    """Send a workspace invitation email. Returns True on success."""
    settings = get_settings()
    if not settings.email_host_password:
        logger.warning("Email not configured — skipping invitation email")
        return False

    base_url = settings.base_url.rstrip("/") if hasattr(settings, "base_url") and settings.base_url else "https://lawa.app"
    accept_url = f"{base_url}/invitations/{token}"

    html_body = f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:580px;margin:0 auto;color:#e6edf3;background:#0d1117;border-radius:12px;overflow:hidden;">
    <div style="padding:32px 32px 0;">
        <div style="display:inline-block;padding:6px 14px;border-radius:20px;background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.25);font-size:12px;font-weight:600;color:#10b981;letter-spacing:.3px;margin-bottom:20px;">WORKSPACE INVITATION</div>
        <h1 style="color:#e6edf3;font-size:22px;font-weight:700;margin:0 0 12px;line-height:1.3;">You've been invited to join<br><span style="color:#10b981;">{_esc(workspace_name)}</span></h1>
        <p style="color:#8b949e;font-size:14px;line-height:1.7;margin:0 0 24px;">
            <strong style="color:#c9d1d9;">{_esc(inviter_name)}</strong> has invited you to join the workspace
            <strong style="color:#c9d1d9;">{_esc(workspace_name)}</strong> as a <strong style="color:#c9d1d9;">{_esc(role)}</strong>.
        </p>
    </div>
    <div style="padding:0 32px 32px;text-align:center;">
        <a href="{accept_url}" target="_blank"
           style="display:inline-block;padding:14px 40px;background:linear-gradient(135deg,#10b981,#059669);color:#fff;font-size:15px;font-weight:600;border-radius:10px;text-decoration:none;margin-bottom:16px;">
            View Invitation
        </a>
        <p style="color:#484f58;font-size:12px;margin:16px 0 0;">This invitation expires in 7 days.</p>
    </div>
    <div style="padding:14px 32px;text-align:center;background:#0a0e14;">
        <p style="color:#484f58;font-size:11px;margin:0;">Sent by LAWA Scouts</p>
    </div>
</div>"""

    plain_body = (
        f"WORKSPACE INVITATION\n\n"
        f"{inviter_name} has invited you to join '{workspace_name}' as a {role}.\n\n"
        f"Accept the invitation: {accept_url}\n\n"
        f"This invitation expires in 7 days.\n"
    )

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Invitation to join {workspace_name} on LAWA Scouts"
        msg["From"] = settings.email_from
        msg["To"] = to_email
        msg.attach(MIMEText(plain_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(settings.email_from, settings.email_host_password)
            server.sendmail(settings.email_from, to_email, msg.as_string())

        logger.info(f"Invitation email sent to {to_email} for workspace '{workspace_name}'")
        return True
    except Exception as e:
        logger.error(f"Failed to send invitation email to {to_email}: {e}")
        return False


def send_mention_email(
    to_email: str,
    mentioner_name: str,
    context: str,
    link: str | None = None,
) -> bool:
    """Send a mention notification email. Returns True on success."""
    settings = get_settings()
    if not settings.email_host_password:
        return False

    base_url = settings.base_url.rstrip("/") if hasattr(settings, "base_url") and settings.base_url else "https://lawa.app"
    full_link = f"{base_url}{link}" if link else base_url

    html_body = f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:580px;margin:0 auto;color:#e6edf3;background:#0d1117;border-radius:12px;overflow:hidden;">
    <div style="padding:32px;">
        <h2 style="color:#e6edf3;font-size:18px;font-weight:600;margin:0 0 12px;">{_esc(mentioner_name)} mentioned you</h2>
        <div style="padding:16px;background:#161b22;border:1px solid #30363d;border-radius:10px;margin:0 0 20px;">
            <p style="color:#c9d1d9;font-size:14px;line-height:1.7;margin:0;">{_esc(context[:500])}</p>
        </div>
        <a href="{full_link}" target="_blank"
           style="display:inline-block;padding:10px 24px;background:#10b981;color:#0d1117;font-size:13px;font-weight:600;border-radius:8px;text-decoration:none;">View</a>
    </div>
    <div style="padding:14px 32px;text-align:center;background:#0a0e14;">
        <p style="color:#484f58;font-size:11px;margin:0;">Sent by LAWA Scouts</p>
    </div>
</div>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"{mentioner_name} mentioned you on LAWA Scouts"
        msg["From"] = settings.email_from
        msg["To"] = to_email
        msg.attach(MIMEText(f"{mentioner_name} mentioned you:\n\n{context[:500]}\n\nView: {full_link}", "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(settings.email_from, settings.email_host_password)
            server.sendmail(settings.email_from, to_email, msg.as_string())

        logger.info(f"Mention email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send mention email to {to_email}: {e}")
        return False


def send_report_email(to_email: str, report, scout_topic: str) -> bool:
    """Send a report email. Returns True on success, False on failure."""
    settings = get_settings()

    if not settings.email_host_password:
        logger.warning("Email not configured (EMAIL_HOST_PASSWORD missing) — skipping email")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Scout Report: {report.title}"
        msg["From"] = settings.email_from
        msg["To"] = to_email

        msg.attach(MIMEText(_build_plain_text(report, scout_topic), "plain"))
        msg.attach(MIMEText(_build_report_html(report, scout_topic), "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(settings.email_from, settings.email_host_password)
            server.sendmail(settings.email_from, to_email, msg.as_string())

        logger.info(f"Report email sent to {to_email} — '{report.title}'")
        return True

    except Exception as e:
        logger.error(f"Failed to send report email to {to_email}: {e}")
        return False
