# Transactional email for Crystal Genie (order confirmations).
#
# Uses plain SMTP against the crystalgenie.com mailbox configured in .env, so
# there is no third-party email provider to sign up for.

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
# 465 = implicit SSL, 587 = STARTTLS. Derived from the port unless overridden.
SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "").lower() in ("1", "true", "yes") or (
    os.getenv("SMTP_USE_SSL", "") == "" and SMTP_PORT == 465
)
MAIL_FROM = os.getenv("MAIL_FROM", SMTP_USER)
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "Crystal Genie")
# Optional internal copy of every order (e.g. the shop's own mailbox).
MAIL_BCC = os.getenv("MAIL_BCC", "")


def is_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD and MAIL_FROM)


def send_email(to: str, subject: str, text_body: str, html_body: str) -> None:
    """Send one message. Raises on failure — callers decide how loud to be."""
    if not is_configured():
        raise RuntimeError("Email is not configured (set SMTP_* in the backend's .env)")

    msg = EmailMessage()
    msg["From"] = formataddr((MAIL_FROM_NAME, MAIL_FROM))
    msg["To"] = to
    msg["Subject"] = subject
    if MAIL_BCC:
        msg["Bcc"] = MAIL_BCC
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    if SMTP_USE_SSL:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls(context=context)
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _shipping_lines(order: dict) -> list[str]:
    parts = [
        order.get("ship_name") or "",
        order.get("ship_address") or "",
        " ".join(
            p for p in (order.get("ship_city") or "", order.get("ship_postal") or "") if p
        ),
        order.get("ship_phone") or "",
    ]
    return [p for p in parts if p.strip()]


def build_order_confirmation(order: dict, items: list[dict]) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body) for a placed order."""
    order_id = order.get("id")
    total = float(order.get("total") or 0)
    subject = f"Your Crystal Genie order #{order_id} is confirmed"
    greeting_name = (order.get("ship_name") or "").strip().split(" ")[0] or "there"
    shipping = _shipping_lines(order)

    text_lines = [
        f"Hi {greeting_name},",
        "",
        "Thank you for your order! We've received your payment and we're getting "
        "your crystals ready.",
        "",
        f"Order number: #{order_id}",
        "",
        "Items",
        "-----",
    ]
    for item in items:
        qty = int(item.get("quantity") or 0)
        line_total = float(item.get("unit_price") or 0) * qty
        text_lines.append(
            f"{item.get('product_name', '')} x{qty} — {_money(line_total)}"
        )
    text_lines += ["", f"Total: {_money(total)}"]
    if shipping:
        text_lines += ["", "Shipping to", "-----------", *shipping]
    text_lines += [
        "",
        "We'll email you again as soon as your order ships.",
        "",
        "With light,",
        "The Crystal Genie team",
    ]
    text_body = "\n".join(text_lines)

    item_rows = "".join(
        f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #eee;font-size:14px;color:#1A181B;">
            {item.get('product_name', '')} <span style="color:#888;">&times;{int(item.get('quantity') or 0)}</span>
          </td>
          <td style="padding:10px 0;border-bottom:1px solid #eee;font-size:14px;color:#1A181B;text-align:right;white-space:nowrap;">
            {_money(float(item.get('unit_price') or 0) * int(item.get('quantity') or 0))}
          </td>
        </tr>"""
        for item in items
    )
    shipping_html = (
        f"""
      <h3 style="margin:28px 0 8px;font-size:15px;color:#1A181B;">Shipping to</h3>
      <p style="margin:0;font-size:14px;line-height:22px;color:#555;">
        {"<br>".join(shipping)}
      </p>"""
        if shipping
        else ""
    )

    html_body = f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:24px;background:#F6F4F8;font-family:Helvetica,Arial,sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="max-width:560px;margin:0 auto;background:#fff;border-radius:16px;padding:32px;">
      <tr><td>
        <h1 style="margin:0 0 4px;font-size:22px;color:#1A181B;">Thank you for your order</h1>
        <p style="margin:0 0 24px;font-size:14px;color:#888;">Order #{order_id}</p>
        <p style="margin:0 0 24px;font-size:15px;line-height:24px;color:#1A181B;">
          Hi {greeting_name}, we've received your payment and we're getting your
          crystals ready.
        </p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          {item_rows}
          <tr>
            <td style="padding:14px 0 0;font-size:16px;font-weight:bold;color:#1A181B;">Total</td>
            <td style="padding:14px 0 0;font-size:16px;font-weight:bold;color:#1A181B;text-align:right;">
              {_money(total)}
            </td>
          </tr>
        </table>
        {shipping_html}
        <p style="margin:28px 0 0;font-size:14px;line-height:22px;color:#555;">
          We'll email you again as soon as your order ships.
        </p>
        <p style="margin:20px 0 0;font-size:14px;color:#555;">With light,<br>The Crystal Genie team</p>
      </td></tr>
    </table>
  </body>
</html>"""
    return subject, text_body, html_body
