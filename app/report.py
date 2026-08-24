from __future__ import annotations

import math
from io import BytesIO
from typing import Any, Sequence

from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


INK = HexColor("#152B43")
NAVY = HexColor("#183550")
BLUE = HexColor("#2F80ED")
YELLOW = HexColor("#F4C542")
PAPER = HexColor("#F5F7FB")
PANEL = HexColor("#FFFFFF")
LINE = HexColor("#D4DEE8")
MUTED = HexColor("#698096")
SOFT = HexColor("#EDF2F7")
SKY = HexColor("#DCEEFF")
GOLD_TINT = Color(244 / 255, 197 / 255, 66 / 255, alpha=0.35)
SILVER_TINT = Color(203 / 255, 213 / 255, 223 / 255, alpha=0.5)
BRONZE_TINT = Color(201 / 255, 130 / 255, 82 / 255, alpha=0.35)


def placement_tint(place: int | None) -> Color | None:
    if place == 1:
        return GOLD_TINT
    if place == 2:
        return SILVER_TINT
    if place in (3, 4):
        return BRONZE_TINT
    return None


def ordinal(value: int) -> str:
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def heat_finish_label(value: int) -> str:
    return "DNF" if value == 0 else ordinal(value)


def final_finish_label(value: int | None, dnf_place: int) -> str:
    if value is None:
        return "PENDING"
    if value == 0:
        return f"T-{ordinal(dnf_place)} DNF"
    return ordinal(value)


def fit_text(text: str, font: str, size: float, width: float) -> str:
    if stringWidth(text, font, size) <= width:
        return text
    while text and stringWidth(text + "...", font, size) > width:
        text = text[:-1]
    return text + "..."


def marble(c: canvas.Canvas, x: float, y: float, radius: float, color: str) -> None:
    c.setFillColor(HexColor(color))
    c.setStrokeColor(NAVY)
    c.setLineWidth(0.5)
    c.circle(x, y, radius, fill=1, stroke=1)
    c.setFillColor(Color(1, 1, 1, alpha=0.72))
    c.circle(x - radius * 0.3, y + radius * 0.32, radius * 0.27, fill=1, stroke=0)


def header(
    c: canvas.Canvas, width: float, height: float, race_name: str, subtitle: str, badge: str
) -> None:
    c.setFillColor(PAPER)
    c.rect(0, 0, width, height, fill=1, stroke=0)
    c.setFillColor(NAVY)
    c.rect(0, height - 76, width, 76, fill=1, stroke=0)
    c.setFillColor(YELLOW)
    c.rect(0, height - 80, width, 4, fill=1, stroke=0)
    c.setFillColor(YELLOW)
    c.setFont("Helvetica-Bold", 6.5)
    c.drawString(32, height - 17, "ROLLRANK")
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 19)
    c.drawString(32, height - 39, fit_text(race_name, "Helvetica-Bold", 19, width - 230))
    c.setFillColor(SKY)
    c.setFont("Helvetica", 8.5)
    c.drawString(33, height - 58, fit_text(subtitle, "Helvetica", 8.5, width - 230))
    badge_width = max(94, stringWidth(badge, "Helvetica-Bold", 9) + 25)
    c.setFillColor(BLUE)
    c.roundRect(width - badge_width - 32, height - 52, badge_width, 25, 12.5, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(width - badge_width / 2 - 32, height - 43, badge)


def footer(c: canvas.Canvas, width: float, page: int) -> None:
    c.setStrokeColor(LINE)
    c.line(32, 25, width - 32, 25)
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 7)
    c.drawString(32, 13, "ROLLRANK TOURNAMENT REPORT")
    c.drawRightString(width - 32, 13, f"PAGE {page}")


def points_legend(c: canvas.Canvas, x: float, top: float, width: float, values: Sequence[int]) -> float:
    displayed_values = list(values)
    while len(displayed_values) > 1 and displayed_values[-1] == 0:
        displayed_values.pop()
    tokens = [f"{ordinal(index)}  {value} pts" for index, value in enumerate(displayed_values, start=1)]
    pad = 9
    label_width = 92
    pill_height = 18
    rows: list[list[tuple[str, float]]] = [[]]
    used = label_width
    for token in tokens:
        token_width = stringWidth(token, "Helvetica-Bold", 6.8) + 15
        if rows[-1] and used + token_width + 5 > width - 2 * pad:
            rows.append([])
            used = 0
        rows[-1].append((token, token_width))
        used += token_width + 5
    height = 2 * pad + len(rows) * pill_height + max(0, len(rows) - 1) * 4
    bottom = top - height
    c.setFillColor(NAVY)
    c.roundRect(x, bottom, width, height, 8, fill=1, stroke=0)
    c.setFillColor(YELLOW)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(x + pad, top - pad - 12, "POINTS LEGEND")
    for row_index, row in enumerate(rows):
        cursor = x + pad + (label_width if row_index == 0 else 0)
        y = top - pad - pill_height - row_index * (pill_height + 4)
        for token, token_width in row:
            c.setFillColor(Color(1, 1, 1, alpha=0.12))
            c.roundRect(cursor, y, token_width, pill_height, 9, fill=1, stroke=0)
            c.setFillColor(white)
            c.setFont("Helvetica-Bold", 6.8)
            c.drawCentredString(cursor + token_width / 2, y + 6, token)
            cursor += token_width + 5
    return bottom


def overview_pages(c: canvas.Canvas, state: dict[str, Any], width: float, height: float, page: int) -> int:
    competition = state["competition"]
    standings = state["standings"]
    round_chunks = [list(range(start, min(start + 10, competition["rounds"] + 1))) for start in range(1, competition["rounds"] + 1, 10)]
    racer_chunks = [standings[start : start + 16] for start in range(0, len(standings), 16)]
    page_count = len(round_chunks) * len(racer_chunks)
    page_index = 0
    for round_chunk in round_chunks:
        for racer_chunk in racer_chunks:
            page_index += 1
            badge = "STANDINGS" if page_count == 1 else f"STANDINGS {page_index}/{page_count}"
            header(c, width, height, competition["name"], "Round placings and overall tournament ranking", badge)
            x = 32
            usable = width - 64
            metrics = [
                ("RACE ROUNDS", competition["rounds"]),
                ("HEATS / RACER / ROUND", competition["heatsPerRacerPerRound"]),
                (
                    "MARBLES / HEAT",
                    f'{competition["marblesPerHeat"]}/{competition["maxMarblesPerHeat"]} max',
                ),
                ("MARBLES / RACER", competition["marblesPerRacer"]),
                ("MAX FINALISTS", competition["maxFinalRacers"]),
            ]
            metric_width = (usable - 8 * (len(metrics) - 1)) / len(metrics)
            for index, (label, value) in enumerate(metrics):
                metric_x = x + index * (metric_width + 8)
                c.setFillColor(PANEL)
                c.setStrokeColor(LINE)
                c.roundRect(metric_x, height - 125, metric_width, 33, 7, fill=1, stroke=1)
                c.setFillColor(MUTED)
                c.setFont("Helvetica-Bold", 6)
                c.drawString(metric_x + 9, height - 104, label)
                c.setFillColor(INK)
                c.setFont("Helvetica-Bold", 11)
                c.drawString(metric_x + 9, height - 119, str(value))
            legend_bottom = points_legend(c, x, height - 136, usable, state["points"])
            table_top = legend_bottom - 12
            header_height = 24
            row_height = min(20, (table_top - 34 - header_height) / len(racer_chunk))
            name_width = 178
            total_width = 57
            round_width = (usable - name_width - total_width) / len(round_chunk)
            c.setFillColor(NAVY)
            c.roundRect(x, table_top - header_height, usable, header_height, 6, fill=1, stroke=0)
            c.setFillColor(white)
            c.setFont("Helvetica-Bold", 7)
            c.drawString(x + 10, table_top - 16, "RACER")
            cursor = x + name_width
            for round in round_chunk:
                c.drawCentredString(cursor + round_width / 2, table_top - 16, f"ROUND {round}")
                cursor += round_width
            c.setFillColor(YELLOW)
            c.drawCentredString(cursor + total_width / 2, table_top - 16, "WINS")
            y = table_top - header_height
            for index, racer in enumerate(racer_chunk):
                y -= row_height
                c.setFillColor(PANEL if index % 2 == 0 else SOFT)
                c.setStrokeColor(LINE)
                c.rect(x, y, usable, row_height, fill=1, stroke=1)
                marble(c, x + 13, y + row_height / 2, 4.5, racer["color"])
                c.setFillColor(INK)
                c.setFont("Helvetica-Bold", 7.5)
                c.drawString(x + 24, y + row_height / 2 - 2.5, fit_text(f'{racer["rank"]:02d}  {racer["name"]}', "Helvetica-Bold", 7.5, name_width - 30))
                cursor = x + name_width
                for round in round_chunk:
                    place = racer["roundPlacements"][round - 1]
                    tint = placement_tint(place)
                    if tint is not None:
                        c.setFillColor(tint)
                        c.rect(cursor, y, round_width, row_height, fill=1, stroke=0)
                    c.setStrokeColor(LINE)
                    c.line(cursor, y, cursor, y + row_height)
                    label = ordinal(place) if place is not None else "–"
                    c.setFillColor(INK)
                    c.setFont("Helvetica-Bold", 7.5)
                    c.drawCentredString(cursor + round_width / 2, y + row_height / 2 - 2.5, label)
                    cursor += round_width
                c.setFillColor(SKY)
                c.rect(cursor, y, total_width, row_height, fill=1, stroke=1)
                c.setFillColor(INK)
                c.drawCentredString(cursor + total_width / 2, y + row_height / 2 - 2.5, str(racer["wins"]))
            footer(c, width, page)
            c.showPage()
            page += 1
    return page


def heat_item(c: canvas.Canvas, heat: dict[str, Any], x: float, top: float, width: float, item_height: float) -> None:
    bottom = top - item_height
    rail_width = 65
    c.setFillColor(PANEL)
    c.setStrokeColor(LINE)
    c.roundRect(x, bottom, width, item_height, 7, fill=1, stroke=1)
    c.setFillColor(BLUE if heat["complete"] else NAVY)
    c.roundRect(x, bottom, rail_width, item_height, 7, fill=1, stroke=0)
    c.rect(x + rail_width - 7, bottom, 7, item_height, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(x + rail_width / 2, top - 20, f'HEAT {heat["heatNumber"]}')
    c.setFillColor(SKY)
    c.setFont("Helvetica-Bold", 6)
    c.drawCentredString(x + rail_width / 2, top - 32, f'RACE #{heat["globalNumber"]}')
    c.setFillColor(YELLOW if heat["complete"] else SKY)
    c.drawCentredString(x + rail_width / 2, bottom + 10, "COMPLETE" if heat["complete"] else "PENDING")

    columns = min(4, len(heat["entries"]))
    rows = math.ceil(len(heat["entries"]) / columns)
    content_x = x + rail_width
    cell_width = (width - rail_width) / columns
    row_height = item_height / rows
    for index, entry in enumerate(heat["entries"]):
        row = index // columns
        column = index % columns
        cell_x = content_x + column * cell_width
        cell_top = top - row * row_height
        cell_bottom = cell_top - row_height
        c.setFillColor(PANEL if (row + column) % 2 == 0 else SOFT)
        c.setStrokeColor(LINE)
        c.rect(cell_x, cell_bottom, cell_width, row_height, fill=1, stroke=1)
        marble(c, cell_x + 10, cell_top - 12, 4, entry["color"])
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(cell_x + 18, cell_top - 14, fit_text(f'{entry["lane"]}. {entry["name"]}', "Helvetica-Bold", 7, cell_width - 25))
        c.setFillColor(MUTED)
        c.setFont("Helvetica-Bold", 5.5)
        if not entry["complete"]:
            result = "PENDING"
        elif len(entry["marbles"]) == 1:
            race_marble = entry["marbles"][0]
            result = f'{heat_finish_label(race_marble["finish"])}  -  {race_marble["points"]} pts'
        else:
            marble_results = "  ".join(
                f'M{race_marble["number"]}:{heat_finish_label(race_marble["finish"])}/{race_marble["points"]}'
                for race_marble in entry["marbles"]
            )
            result = f'{marble_results}  |  {entry["points"]} pts'
        c.drawString(
            cell_x + 9,
            cell_bottom + 8,
            fit_text(result, "Helvetica-Bold", 5.5, cell_width - 18),
        )


def round_pages(c: canvas.Canvas, state: dict[str, Any], width: float, height: float, page: int) -> int:
    competition = state["competition"]
    for round in state["rounds"]:
        columns = min(4, competition["racersPerHeat"])
        rows = math.ceil(competition["racersPerHeat"] / columns)
        item_height = max(58, 18 + rows * 25)
        available = height - 80 - 52 - 48
        per_page = max(1, int((available + 7) // (item_height + 7)))
        chunks = [round["heats"][start : start + per_page] for start in range(0, len(round["heats"]), per_page)]
        for sheet_index, chunk in enumerate(chunks, start=1):
            badge = f'ROUND {round["round"]}/{competition["rounds"]}'
            if len(chunks) > 1:
                badge += f' - SHEET {sheet_index}/{len(chunks)}'
            header(c, width, height, competition["name"], "Recorded marble finishes and awarded heat points", badge)
            legend_bottom = points_legend(c, 32, height - 92, width - 64, state["points"])
            top = legend_bottom - 8
            for index, heat in enumerate(chunk):
                heat_item(c, heat, 32, top - index * (item_height + 7), width - 64, item_height)
            footer(c, width, page)
            c.showPage()
            page += 1
    return page


def origin_label(entry: dict[str, Any]) -> str:
    origin = entry.get("originStage")
    if origin == "bye":
        return f'BYE - ROUND {entry.get("originRound")}'
    if origin == "stage-skip":
        return "ADVANCED (SMALL FIELD)"
    if origin == "preliminary":
        return "PRELIMINARY"
    if origin == "wildcard":
        return "WILDCARD"
    if origin == "quarterfinal":
        return "QUARTERFINAL"
    if origin == "semifinal":
        return "SEMIFINAL"
    if origin == "staging-round":
        return f'ROUND {entry.get("originRound")} #2'
    return "-"


def stage_heats_page(
    c: canvas.Canvas,
    state: dict[str, Any],
    heats: list[dict[str, Any]],
    badge_prefix: str,
    subtitle: str,
    width: float,
    height: float,
    page: int,
) -> int:
    if not heats:
        return page
    competition = state["competition"]
    max_entries = max(len(heat["entries"]) for heat in heats)
    columns = min(4, max(1, max_entries))
    rows = math.ceil(max_entries / columns)
    item_height = max(58, 18 + rows * 25)
    available = height - 80 - 52 - 48
    per_page = max(1, int((available + 7) // (item_height + 7)))
    chunks = [heats[start : start + per_page] for start in range(0, len(heats), per_page)]
    for sheet_index, chunk in enumerate(chunks, start=1):
        badge = badge_prefix
        if len(chunks) > 1:
            badge += f' - SHEET {sheet_index}/{len(chunks)}'
        header(c, width, height, competition["name"], subtitle, badge)
        legend_bottom = points_legend(c, 32, height - 92, width - 64, state["points"])
        top = legend_bottom - 8
        for index, heat in enumerate(chunk):
            heat_item(c, heat, 32, top - index * (item_height + 7), width - 64, item_height)
        footer(c, width, page)
        c.showPage()
        page += 1
    return page


def wildcard_page(c: canvas.Canvas, state: dict[str, Any], width: float, height: float, page: int) -> int:
    heats = state["championship"]["wildcard"]["heats"]
    return stage_heats_page(
        c, state, heats, "WILDCARD", "Wildcard heats: 3rd/4th place finishers from each round", width, height, page
    )


def preliminary_page(c: canvas.Canvas, state: dict[str, Any], width: float, height: float, page: int) -> int:
    heats = state["championship"]["preliminary"]["heats"]
    promoted = state["competition"]["wildcardRacersPromotedPerHeat"]
    return stage_heats_page(
        c,
        state,
        heats,
        "PRELIMINARY",
        f"Preliminary heats: top {promoted} racer{'s' if promoted != 1 else ''} from each wildcard heat and round runners-up",
        width,
        height,
        page,
    )


def quarterfinal_page(c: canvas.Canvas, state: dict[str, Any], width: float, height: float, page: int) -> int:
    heats = state["championship"]["quarterfinal"]["heats"]
    return stage_heats_page(
        c, state, heats, "QUARTERFINAL", "Quarterfinal heats: the final field split for being too large for one heat", width, height, page
    )


def semifinal_page(c: canvas.Canvas, state: dict[str, Any], width: float, height: float, page: int) -> int:
    heats = state["championship"]["semifinal"]["heats"]
    return stage_heats_page(
        c, state, heats, "SEMIFINAL", "Semifinal heats: top finishers from each quarterfinal heat (or a split final field)", width, height, page
    )


def final_page(c: canvas.Canvas, state: dict[str, Any], width: float, height: float, page: int) -> int:
    competition = state["competition"]
    final = state["championship"]["final"]
    header(c, width, height, competition["name"], "Championship final qualification and result", "FINAL")
    x = 32
    usable = width - 64
    banner_top = height - 96
    heat = final["heat"]
    entries = heat["entries"] if heat else []
    c.setFillColor(NAVY)
    c.roundRect(x, banner_top - 47, usable, 47, 8, fill=1, stroke=0)
    c.setFillColor(YELLOW)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(x + 15, banner_top - 21, f'{len(entries)} FINALISTS' if entries else "FINAL FIELD PENDING")
    c.setFillColor(white)
    c.setFont("Helvetica", 8)
    status = (
        "Final complete"
        if final["complete"]
        else "Final field locked"
        if final["ready"]
        else "The final field is provisional until the preliminary round is complete"
    )
    c.drawString(x + 15, banner_top - 37, status)
    table_top = banner_top - 60
    dnf_place = sum(1 for entry in entries if (entry.get("finish") or 0) > 0) + 1
    header_height = 24
    result_height = 68
    row_height = min(36, (table_top - 35 - result_height - 12 - header_height) / max(1, len(entries)))
    c.setFillColor(NAVY)
    c.roundRect(x, table_top - header_height, usable, header_height, 6, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(x + 12, table_top - 16, "QUALIFIED VIA")
    c.drawString(x + 130, table_top - 16, "FINAL RACER")
    c.drawRightString(x + usable - 15, table_top - 16, "FINAL PLACE")
    y = table_top - header_height
    for index, entry in enumerate(entries):
        y -= row_height
        c.setFillColor(PANEL if index % 2 == 0 else SOFT)
        c.setStrokeColor(LINE)
        c.rect(x, y, usable, row_height, fill=1, stroke=1)
        c.setFillColor(MUTED)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawString(x + 12, y + row_height / 2 - 2.5, origin_label(entry))
        marble(c, x + 118, y + row_height / 2, 5, entry["color"])
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(x + 130, y + row_height / 2 - 3, entry["name"])
        finish = entry.get("finish")
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 8)
        c.drawRightString(
            x + usable - 15,
            y + row_height / 2 - 3,
            final_finish_label(finish, dnf_place),
        )
    result_y = max(34, y - result_height - 12)
    c.setFillColor(NAVY)
    c.roundRect(x, result_y, usable, result_height, 8, fill=1, stroke=0)
    champion = final["champion"]
    c.setFillColor(YELLOW)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(x + 15, result_y + 44, "CHAMPION")
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(x + 85, result_y + 40, champion["name"] if champion else "Pending final result")
    c.setFillColor(SKY)
    c.setFont("Helvetica", 7.5)
    c.drawString(x + 15, result_y + 16, "Generated from this tournament's saved database records.")
    footer(c, width, page)
    c.showPage()
    return page + 1


def build_report(state: dict[str, Any]) -> bytes:
    output = BytesIO()
    width, height = landscape(letter)
    pdf = canvas.Canvas(output, pagesize=(width, height), pageCompression=1)
    pdf.setTitle(f'{state["competition"]["name"]} - Tournament Report')
    pdf.setAuthor("RollRank")
    page = overview_pages(pdf, state, width, height, 1)
    page = round_pages(pdf, state, width, height, page)
    page = wildcard_page(pdf, state, width, height, page)
    page = preliminary_page(pdf, state, width, height, page)
    page = quarterfinal_page(pdf, state, width, height, page)
    page = semifinal_page(pdf, state, width, height, page)
    final_page(pdf, state, width, height, page)
    pdf.save()
    return output.getvalue()
