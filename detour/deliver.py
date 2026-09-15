"""Render a brief as standalone HTML.

Styles are inline rather than in a stylesheet, and the layout leans on tables,
because this file has to survive being pasted into an email body — mail
clients strip `<style>` blocks and ignore most modern CSS. It is equally
openable as a file, which is how it is delivered until a transport is chosen.

Transport is deliberately not decided here. Whether this ends up in an email,
a push payload or a static page, the rendering is the same and the choice
should not be baked into the renderer.
"""

from __future__ import annotations

import datetime as dt
import html
from pathlib import Path as FsPath

from . import correspondent, severity, workcal

INK = "#1A1917"
MUTE = "#6A665E"
FAINT = "#8D887F"
RULE = "#D4D1CA"
GROUND = "#EFEEEA"
SURFACE = "#FFFFFF"

TIER_COLOUR = {
    severity.Tier.BLOCKING: "#D8471A",
    severity.Tier.SLOWING: "#B07C00",
    severity.Tier.BACKGROUND: "#4E6575",
}

NEWS_LABEL = {
    correspondent.News.NEW: "New",
    correspondent.News.CHANGED: "Changed",
    correspondent.News.RESURFACED: "Still going",
}

FONT = "'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "'SFMono-Regular',Consolas,'Liberation Mono',monospace"


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _item_block(item, decision) -> str:
    colour = TIER_COLOUR.get(item.tier, MUTE)
    badge = NEWS_LABEL.get(decision.news, "")
    why = f" &middot; {_esc(decision.reason)}" if decision.reason else ""

    action = ""
    if item.action:
        action = (
            f'<div style="margin-top:9px;padding-left:11px;border-left:2px solid {RULE};'
            f'font-family:{MONO};font-size:12px;color:{INK};line-height:1.5">'
            f"{_esc(item.action)}</div>"
        )

    return f"""
<tr><td style="padding:0 0 14px 0">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="border-collapse:collapse;background:{SURFACE};border:1px solid {RULE}">
    <tr>
      <td width="4" style="background:{colour};font-size:0;line-height:0">&nbsp;</td>
      <td style="padding:13px 16px 15px 16px">
        <div style="font-family:{MONO};font-size:10px;letter-spacing:.12em;
                    text-transform:uppercase;color:{colour};margin-bottom:5px">
          {_esc(severity.LABELS[item.tier])}
          <span style="color:{FAINT}">&middot; {_esc(item.verdict.label)}
          {f'&middot; {badge}' if badge else ''}{why}</span>
        </div>
        <div style="font-family:{FONT};font-size:15px;font-weight:600;
                    color:{INK};line-height:1.35;margin-bottom:4px">
          {_esc(item.headline)}
        </div>
        <div style="font-family:{FONT};font-size:13.5px;color:{MUTE};line-height:1.5">
          {_esc(item.detail)}
        </div>
        {action}
      </td>
    </tr>
  </table>
</td></tr>"""


def render(brief, *, tell, held, reader: str = "you") -> str:
    """Full HTML for one reader's brief."""
    local = workcal.to_central(brief.generated_at)
    blocking = [i for i, _ in tell if i.tier is severity.Tier.BLOCKING]

    if blocking:
        summary = f"{len(blocking)} thing{'s' if len(blocking) > 1 else ''} to plan around"
        summary_colour = TIER_COLOUR[severity.Tier.BLOCKING]
    elif tell:
        summary = f"{len(tell)} update{'s' if len(tell) > 1 else ''}, nothing blocking"
        summary_colour = TIER_COLOUR[severity.Tier.SLOWING]
    else:
        summary = "Nothing new on your route"
        summary_colour = TIER_COLOUR[severity.Tier.BACKGROUND]

    rows = "".join(_item_block(item, decision) for item, decision in tell)
    if not tell:
        rows = (
            f'<tr><td style="padding:16px 0;font-family:{FONT};font-size:14px;'
            f'color:{MUTE}">Nothing on your route has changed since we last '
            f"wrote. Quiet is the normal state.</td></tr>"
        )

    delta_block = ""
    if brief.delta is not None and brief.delta.affected and not brief.delta.implausible:
        via = " &rarr; ".join(_esc(v) for v in brief.delta.via[:5])
        delta_block = f"""
<tr><td style="padding:6px 0 18px 0">
  <div style="font-family:{MONO};font-size:10px;letter-spacing:.12em;
              text-transform:uppercase;color:{TIER_COLOUR[severity.Tier.BLOCKING]};
              margin-bottom:6px">Route impact</div>
  <div style="font-family:{FONT};font-size:15px;color:{INK};margin-bottom:3px">
    {_esc(brief.delta.summary())}</div>
  <div style="font-family:{MONO};font-size:11.5px;color:{MUTE}">via {via}</div>
</td></tr>"""

    held_block = ""
    if held:
        names = "<br>".join(f"&middot; {_esc(i.headline)}" for i, _ in held[:6])
        more = f"<br>&middot; and {len(held) - 6} more" if len(held) > 6 else ""
        held_block = f"""
<tr><td style="padding:16px 0 0 0;border-top:1px solid {RULE}">
  <div style="font-family:{MONO};font-size:10px;letter-spacing:.12em;
              text-transform:uppercase;color:{FAINT};margin-bottom:7px">
    Still ongoing, told before</div>
  <div style="font-family:{FONT};font-size:12.5px;color:{FAINT};line-height:1.7">
    {names}{more}</div>
</td></tr>"""

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(brief.route.name)} &mdash; Detour ATX</title></head>
<body style="margin:0;padding:0;background:{GROUND}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="border-collapse:collapse;background:{GROUND}">
<tr><td align="center" style="padding:26px 14px 40px 14px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="border-collapse:collapse;max-width:600px;text-align:left">

    <tr><td style="padding-bottom:4px">
      <span style="font-family:{FONT};font-size:20px;font-weight:700;
                   letter-spacing:-.01em;color:{INK}">DETOUR ATX</span>
    </td></tr>
    <tr><td style="padding-bottom:14px;font-family:{MONO};font-size:11px;color:{FAINT}">
      {_esc(brief.route.name)} &middot; {brief.route.length_mi:.1f} mi &middot;
      {local.strftime('%A %d %B, %H:%M')}
    </td></tr>
    <tr><td style="padding-bottom:18px;border-bottom:2px solid {INK}">
      <span style="font-family:{FONT};font-size:19px;font-weight:600;color:{summary_colour}">
        {_esc(summary)}</span>
    </td></tr>

    <tr><td style="padding-top:20px"></td></tr>
    {delta_block}
    {rows}
    {held_block}

    <tr><td style="padding:22px 0 0 0;border-top:1px solid {RULE};
                   font-family:{MONO};font-size:10.5px;color:{FAINT};line-height:1.7">
      Built from the City of Austin open data portal. Nothing here is confirmed
      unless it says so &mdash; of 3,853 work zone records the city has verified
      a position on none.<br>
      Passed one of these? Telling us whether it was really there is what makes
      the next brief better.
    </td></tr>

  </table>
</td></tr></table>
</body></html>"""


def write(brief, *, tell, held, directory: FsPath, reader: str = "you") -> FsPath:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = workcal.to_central(brief.generated_at).strftime("%Y-%m-%d")
    slug = "".join(c if c.isalnum() else "-" for c in brief.route.name.lower()).strip("-")
    target = directory / f"{stamp}-{slug}.html"
    target.write_text(render(brief, tell=tell, held=held, reader=reader), encoding="utf-8")
    return target


def plain_text(brief, *, tell) -> str:
    """A terse version for a push payload or an SMS."""
    blocking = [i for i, _ in tell if i.tier is severity.Tier.BLOCKING]
    if not blocking:
        return f"{brief.route.name}: nothing blocking today."
    first = blocking[0]
    extra = f" (+{len(blocking) - 1} more)" if len(blocking) > 1 else ""
    return f"{brief.route.name}: {first.headline}{extra}"
